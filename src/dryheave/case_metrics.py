import os
import stat
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import psutil
from pydantic import Field, ValidationError, model_validator

from dryheave.cases import MetricsCommand
from dryheave.controller_watch import ControllerWatch
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import CleanupReport
from dryheave.errors import DryheaveError, InputError, LimitError
from dryheave.filesystem import atomic_write, ensure_directory, read_bytes
from dryheave.grading import (
    GradingContext,
    VerifierContext,
    hidden_inputs,
    materialize_check,
    trusted_command,
)
from dryheave.models import CommandSpec, StrictModel
from dryheave.process_ownership import ProcessOwner
from dryheave.processes import CommandControl, CommandResult, run_command
from dryheave.result_models import (
    MAX_ERROR_LENGTH,
    MAX_METRIC_VALUES,
    CheckExecution,
    MetricsResult,
    MetricValue,
)
from dryheave.serialization import canonical_json, digest, parse_json

MAX_METRICS_OUTPUT_BYTES = 1024 * 1024
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class MetricsOutput(StrictModel):
    metrics: tuple[MetricValue, ...] = Field(max_length=MAX_METRIC_VALUES)

    @model_validator(mode="after")
    def distinct_metrics(self) -> Self:
        if len({item.metric_id for item in self.metrics}) != len(self.metrics):
            raise ValueError("metric IDs must be unique")
        return self


@dataclass(frozen=True)
class MetricsPaths:
    workspace: Path
    baseline: Path
    hidden: Path
    output: Path


def parse_metrics(content: bytes) -> tuple[MetricValue, ...]:
    try:
        parse_json(content)
    except InputError as error:
        raise InputError(f"metrics output is not valid JSON: {error}") from error
    try:
        return MetricsOutput.model_validate_json(content).metrics
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        )
        raise InputError(
            ("metrics output violates the contract: " + problems)[:MAX_ERROR_LENGTH]
        ) from error


def process_failure(result: CommandResult, timeout_seconds: int) -> str | None:
    if result.outcome == "timeout":
        return f"metrics command timed out after {timeout_seconds} seconds"
    if result.outcome == "output_limit":
        return "metrics command exceeded its stdout/stderr byte limit"
    if result.returncode < 0:
        return f"metrics command was killed by signal {-result.returncode}"
    if result.returncode:
        return f"metrics command exited with status {result.returncode}"
    return None


def metrics_environment(
    spec: CommandSpec, inherited: Mapping[str, str], paths: MetricsPaths
) -> dict[str, str]:
    missing = [item.name for item in spec.environment if item.name not in inherited]
    if missing:
        raise InputError("Required metrics environment reference is unavailable: " + missing[0])
    return (
        {item.name: inherited[item.name] for item in spec.environment}
        | {name: inherited[name] for name in ("HOME", "PATH") if name in inherited}
        | {
            "DRYHEAVE_BASELINE_DIR": str(paths.baseline),
            "DRYHEAVE_METRICS_OUTPUT": str(paths.output),
        }
    )


@dataclass(frozen=True)
class LockedPath:
    path: Path
    device: int
    inode: int


def _locked_paths(baseline: Path) -> tuple[LockedPath, ...]:
    for directory in (baseline.parent, baseline):
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise InputError(f"refusing to lock a non-directory baseline path: {directory.name}")
    walked = [
        Path(directory) / name
        for directory, directories, files in os.walk(baseline)
        for name in (*directories, *files)
    ]
    infos = ((path, path.lstat()) for path in (baseline.parent, baseline, *walked))
    return tuple(
        LockedPath(path, info.st_dev, info.st_ino)
        for path, info in infos
        if not stat.S_ISLNK(info.st_mode)
    )


def _set_writable(locked: tuple[LockedPath, ...], *, writable: bool) -> tuple[Path, ...]:
    skipped: list[Path] = []
    for item in locked:
        try:
            info = item.path.lstat()
            if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (
                item.device,
                item.inode,
            ):
                skipped.append(item.path)
                continue
            mode = stat.S_IMODE(info.st_mode)
            os.chmod(
                item.path,
                mode | stat.S_IWUSR if writable else mode & ~_WRITE_BITS,
                follow_symlinks=False,
            )
        except (OSError, NotImplementedError):
            skipped.append(item.path)
    return tuple(skipped)


def _read_output(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return read_bytes(path, limit=MAX_METRICS_OUTPUT_BYTES)
    except LimitError as error:
        raise InputError(
            f"metrics output exceeds the {MAX_METRICS_OUTPUT_BYTES}-byte limit"
        ) from error


def _outcome(
    command: MetricsCommand,
    result: CommandResult | None,
    output: bytes | None,
    failure: str | None,
) -> tuple[tuple[MetricValue, ...], str | None]:
    if failure is not None:
        return (), failure
    if result is None:
        return (), "metrics command did not run"
    process = process_failure(result, command.command.timeout_seconds)
    if process is not None:
        return (), process
    if output is None:
        return (), "metrics command did not write DRYHEAVE_METRICS_OUTPUT"
    try:
        return parse_metrics(output), None
    except InputError as error:
        return (), str(error)


def execute_metrics(
    command: MetricsCommand,
    paths: MetricsPaths,
    artifacts: ArtifactWriter,
    context: VerifierContext,
    inherited: Mapping[str, str],
) -> MetricsResult:
    started = time.monotonic()
    owner = ProcessOwner()
    watch = ControllerWatch(
        (artifacts.root, paths.output.parent),
        context.cancelled,
        max_bytes=artifacts.max_bytes + MAX_METRICS_OUTPUT_BYTES,
    )

    def own(pid: int) -> None:
        owner.add(psutil.Process(pid))
        publish(force=True)

    def publish(*, force: bool = False) -> None:
        owner.publish(context.on_identities, force=force)

    result: CommandResult | None = None
    output: bytes | None = None
    failure: str | None = None
    try:
        if context.cancelled.is_set():
            raise KeyboardInterrupt
        forbidden = (
            paths.workspace,
            paths.baseline,
            *((context.original_workspace,) if context.original_workspace else ()),
        )
        spec = trusted_command(command.command, command.entrypoint, paths.hidden, forbidden)
        artifacts.record("command", spec)
        artifacts.record(
            "executable",
            {"sha256": digest(read_bytes(Path(spec.argv[0]), limit=256 * 1024 * 1024))},
        )
        result = run_command(
            spec,
            paths.workspace,
            environment=metrics_environment(spec, inherited, paths),
            on_start=own,
            control=CommandControl(cancelled=watch.cancelled, on_poll=publish),
        )
        artifacts.append("stdout.bin", result.stdout)
        artifacts.append("stderr.bin", result.stderr)
    except (DryheaveError, OSError, ValueError) as error:
        artifacts.record("error", {"type": type(error).__name__, "message": str(error)})
        failure = type(error).__name__
    finally:
        owner.flush(context.on_identities)
        cleanup = owner.stop(timeout=3, terminal_closed=True)
        watch.close()
        publish(force=True)
        artifacts.record("cleanup", cleanup)
    if context.cancelled.is_set():
        raise KeyboardInterrupt
    try:
        output = _read_output(paths.output)
        if output is not None:
            artifacts.append("metrics.json", output)
    except (DryheaveError, OSError) as error:
        artifacts.record("error", {"type": type(error).__name__, "message": str(error)})
        failure = failure or type(error).__name__
    return _result(
        command,
        _outcome(command, result, output, failure),
        _execution(result, output, started),
        (cleanup, watch.error),
    )


def _execution(
    result: CommandResult | None, output: bytes | None, started: float
) -> CheckExecution:
    elapsed = time.monotonic() - started
    if result is None:
        return CheckExecution(outcome="error", elapsed_seconds=elapsed)
    exited = result.outcome == "exited" and result.returncode >= 0
    return CheckExecution(
        outcome="error" if not exited else "fail" if result.returncode else "pass",
        execution_observed=output is not None,
        returncode=result.returncode,
        process_outcome=result.outcome,
        elapsed_seconds=elapsed,
        stdout_sha256=digest(result.stdout),
        stderr_sha256=digest(result.stderr),
    )


def _result(
    command: MetricsCommand,
    outcome: tuple[tuple[MetricValue, ...], str | None],
    execution: CheckExecution,
    cleanup: tuple[CleanupReport, str | None],
) -> MetricsResult:
    values, error = outcome
    report, watch_error = cleanup
    if not report.known_writers_stopped or report.errors:
        values, error = (), "metrics_cleanup_failed"
    elif watch_error is not None:
        values, error = (), f"metrics files exceeded their limits: {watch_error}"
    return MetricsResult(
        metrics_id=command.metrics_id,
        outcome="error" if error else "completed",
        values=values,
        execution=execution,
        error=error,
    )


def run_metrics(
    context: GradingContext, command: MetricsCommand, inherited: Mapping[str, str]
) -> MetricsResult:
    root = context.root / "metrics" / command.metrics_id
    paths = MetricsPaths(
        workspace=root / "workspace",
        baseline=root / "baseline" / "workspace",
        hidden=root / "hidden",
        output=root / "output" / "metrics.json",
    )
    artifacts: ArtifactWriter | None = None
    restore_error: str | None = None
    locked: tuple[LockedPath, ...] = ()
    try:
        ensure_directory(root)
        artifacts = ArtifactWriter(
            root / (command.metrics_id + "-evidence"),
            max(65536, 8 * command.command.max_output_bytes) + MAX_METRICS_OUTPUT_BYTES,
        )
        materialize_check(context, "final", paths.workspace)
        materialize_check(context, "baseline", paths.baseline)
        locked = _locked_paths(paths.baseline)
        if _set_writable(locked, writable=False):
            raise InputError("metrics baseline could not be locked")
        ensure_directory(paths.output.parent)
        hidden_inputs(context, root)
        result = execute_metrics(command, paths, artifacts, context, inherited)
    except (DryheaveError, OSError) as error:
        if artifacts is not None:
            artifacts.record("error", {"type": type(error).__name__, "message": str(error)})
        result = MetricsResult(
            metrics_id=command.metrics_id, outcome="error", error=type(error).__name__
        )
    finally:
        skipped = _set_writable(locked, writable=True)
        if skipped:
            restore_error = "metrics_baseline_changed"
            if artifacts is not None:
                with suppress(DryheaveError, OSError):
                    artifacts.record(
                        "restore", {"skipped": [str(path.relative_to(root)) for path in skipped]}
                    )
    if restore_error is not None:
        result = result.model_copy(
            update={"outcome": "error", "values": (), "error": restore_error}
        )
    try:
        hidden_inputs(context, root)
    except (DryheaveError, OSError):
        result = result.model_copy(
            update={"outcome": "error", "values": (), "error": "hidden_metrics_input_changed"}
        )
    if artifacts is not None:
        with suppress(OSError):
            atomic_write(artifacts.root / "result.json", canonical_json(result), replace=False)
    return result
