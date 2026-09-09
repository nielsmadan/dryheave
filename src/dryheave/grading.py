import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import psutil

from dryheave.cases import DeterministicCriterion, FrozenCase
from dryheave.controller_watch import ControllerWatch
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import DryheaveError, InputError, IntegrityError
from dryheave.filesystem import atomic_write, ensure_directory, read_bytes
from dryheave.final_capture import materialize_files
from dryheave.models import CommandSpec
from dryheave.process_ownership import ProcessOwner
from dryheave.processes import CommandControl, run_command
from dryheave.repositories import Git, SnapshotLimits, materialize_repository
from dryheave.result_models import Calibration, CheckExecution, CriterionResult, Outcome
from dryheave.runner_models import CapturedAttempt
from dryheave.serialization import digest
from dryheave.storage import ObjectStore


@dataclass
class GradingContext:
    store: ObjectStore
    case: FrozenCase
    capture: CapturedAttempt
    blobs: dict[str, bytes]
    root: Path
    original_workspace: Path
    on_identity: Callable[[ProcessIdentity], None]
    cancelled: Event


def _command(
    criterion: DeterministicCriterion, hidden: Path, forbidden: tuple[Path, ...]
) -> CommandSpec:
    entrypoint = str(hidden / criterion.entrypoint)
    argv = tuple(argument.replace("{verifier}", str(hidden)) for argument in criterion.command.argv)
    index = argv.index(entrypoint)
    if index > 0 and not Path(argv[0]).is_absolute() and "/" in argv[0]:
        raise InputError(
            "Verifier executable must be an absolute trusted path or a system executable name."
        )
    path = os.pathsep.join((str(Path(sys.executable).parent), os.defpath))
    executable = entrypoint if index == 0 else shutil.which(argv[0], path=path)
    if executable is None:
        raise InputError("Trusted verifier executable is unavailable.")
    resolved = Path(executable).resolve(strict=True)
    if any(resolved.is_relative_to(root.resolve()) for root in forbidden):
        raise InputError("Verifier executable resolves into subject-owned content.")
    if index > 0 and any(
        argument not in {"-I", "-B", "-E", "-s", "-S", "-u", "-bb"} for argument in argv[1:index]
    ):
        raise InputError(
            "Verifier must directly invoke its trusted hidden entrypoint; wrappers and inline programs are unsupported."
        )
    if index == 0:
        resolved.chmod(0o700)
    return criterion.command.model_copy(update={"argv": (str(resolved), *argv[1:])})


def _environment(command: CommandSpec) -> dict[str, str]:
    environment = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for reference in command.environment:
        if reference.name not in os.environ:
            raise InputError(
                "Required verifier environment reference is unavailable: " + reference.name
            )
        environment[reference.name] = os.environ[reference.name]
    return environment


def execute_check(
    criterion: DeterministicCriterion,
    workspace: Path,
    hidden: Path,
    artifacts: ArtifactWriter,
    context: GradingContext,
) -> CheckExecution:
    started = time.monotonic()
    owner = ProcessOwner()
    watch = ControllerWatch(
        artifacts.root,
        context.cancelled,
        max_bytes=8 * criterion.command.max_output_bytes,
        output_bytes=criterion.command.max_output_bytes,
    )

    def own(pid: int) -> None:
        owner.add(psutil.Process(pid))
        publish()

    def publish() -> None:
        owner.publish(context.on_identity)

    try:
        if context.cancelled.is_set():
            raise KeyboardInterrupt
        command = _command(criterion, hidden, (workspace, context.original_workspace))
        artifacts.record("command", command)
        executable = Path(command.argv[0])
        artifacts.record(
            "executable", {"sha256": digest(read_bytes(executable, limit=256 * 1024 * 1024))}
        )
        result = run_command(
            command,
            workspace,
            environment=_environment(command),
            on_start=own,
            control=CommandControl(cancelled=watch.cancelled, on_poll=publish),
        )
        artifacts.append("stdout.bin", result.stdout)
        artifacts.append("stderr.bin", result.stderr)
        expected = (
            criterion.expected_failure_stdout or criterion.expected_stdout
            if result.returncode > 0
            else criterion.expected_stdout
        )
        marker = expected.encode() in result.stdout
        outcome: Outcome = (
            "error"
            if result.outcome != "exited" or result.returncode < 0 or not marker
            else "fail"
            if result.returncode
            else "pass"
        )
        execution = CheckExecution(
            outcome=outcome,
            execution_observed=marker,
            returncode=result.returncode,
            process_outcome=result.outcome,
            elapsed_seconds=time.monotonic() - started,
            stdout_sha256=digest(result.stdout),
            stderr_sha256=digest(result.stderr),
            error="expected_execution_evidence_missing" if not marker else None,
        )
    except (DryheaveError, OSError, ValueError) as error:
        artifacts.record("error", {"type": type(error).__name__, "message": str(error)})
        execution = CheckExecution(
            outcome="error", error=type(error).__name__, elapsed_seconds=time.monotonic() - started
        )
    finally:
        cleanup = owner.stop(timeout=3, terminal_closed=True)
        watch.close()
        publish()
        artifacts.record("cleanup", cleanup)
    if not cleanup.known_writers_stopped or cleanup.errors or watch.error:
        execution = execution.model_copy(
            update={"outcome": "error", "error": "verifier_cleanup_failed"}
        )
    artifacts.record("execution", execution)
    if context.cancelled.is_set():
        raise KeyboardInterrupt
    return execution


def _hidden(context: GradingContext, root: Path) -> Path:
    hidden = root / "hidden"
    if not hidden.exists():
        for name, blob in context.case.hidden_files.items():
            atomic_write(hidden / name, context.blobs[blob], replace=False)
    for name, blob in context.case.hidden_files.items():
        if read_bytes(hidden / name, limit=256 * 1024 * 1024) != context.blobs[blob]:
            raise IntegrityError("Materialized hidden verifier input changed.")
    return hidden


def _check_copy(
    context: GradingContext, criterion: DeterministicCriterion, kind: str
) -> CheckExecution:
    root = (
        context.root
        / ("suites" if criterion.suite_id else "criteria")
        / (criterion.suite_id or criterion.criterion_id)
        / kind
    )
    ensure_directory(root)
    workspace = root / "workspace"
    if not workspace.exists():
        _materialize_check(context, kind, workspace)
    hidden = _hidden(context, root)
    execution = execute_check(
        criterion,
        workspace,
        hidden,
        ArtifactWriter(
            root / (criterion.criterion_id + "-evidence"), 8 * criterion.command.max_output_bytes
        ),
        context,
    )
    try:
        _hidden(context, root)
    except (DryheaveError, OSError):
        return execution.model_copy(update={"outcome": "error", "error": "hidden_verifier_changed"})
    return execution


def _materialize_check(context: GradingContext, kind: str, workspace: Path) -> None:
    if kind == "final":
        materialize_files(
            workspace,
            context.capture.workspace.files,
            context.blobs,
            directories=context.capture.workspace.directories,
        )
    else:
        materialize_repository(context.store, context.case.repository_id, workspace)
        if kind == "reference":
            patch_name = context.case.reference_patch_file
            if patch_name is None:
                raise InputError("Reference calibration requires a frozen reference patch.")
            Git(workspace, SnapshotLimits()).run(
                "apply",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_bytes=context.blobs[patch_name],
            )


def grade_criterion(context: GradingContext, criterion: DeterministicCriterion) -> CriterionResult:
    try:
        final = _check_copy(context, criterion, "final")
    except (DryheaveError, OSError) as error:
        final = CheckExecution(outcome="error", error=type(error).__name__)
    baseline = reference = None
    try:
        baseline = _check_copy(context, criterion, "baseline")
        if context.case.reference_patch_file:
            reference = _check_copy(context, criterion, "reference")
    except (DryheaveError, OSError) as error:
        reference = CheckExecution(outcome="error", error=type(error).__name__)
    demonstrated = (
        baseline is not None
        and baseline.outcome == "fail"
        and reference is not None
        and reference.outcome == "pass"
    )
    ineffective = baseline is not None and baseline.outcome == "pass"
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        kind="deterministic",
        required=criterion.required,
        outcome=final.outcome,
        execution=final,
        calibration=Calibration(
            status="demonstrated"
            if demonstrated
            else "ineffective"
            if ineffective
            else "unavailable",
            baseline=baseline,
            reference=reference,
        ),
    )
