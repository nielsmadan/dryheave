import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import psutil

from dryheave.capture_models import WorkspaceCapture
from dryheave.cases import FrozenCase, TaskFact, fact_verifier
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
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore


@dataclass
class VerifierContext:
    store: ObjectStore
    case: FrozenCase
    blobs: dict[str, bytes]
    root: Path
    original_workspace: Path | None
    on_identities: Callable[[tuple[ProcessIdentity, ...]], None]
    cancelled: Event


@dataclass
class GradingContext(VerifierContext):
    capture: CapturedAttempt


def trusted_command(
    spec: CommandSpec, relative: str, hidden: Path, forbidden: tuple[Path, ...]
) -> CommandSpec:
    entrypoint = str(hidden / relative)
    argv = tuple(argument.replace("{verifier}", str(hidden)) for argument in spec.argv)
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
    return spec.model_copy(update={"argv": (str(resolved), *argv[1:])})


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
    fact: TaskFact,
    workspace: Path,
    hidden: Path,
    artifacts: ArtifactWriter,
    context: VerifierContext,
    deadline: float | None = None,
) -> CheckExecution:
    verifier = fact_verifier(fact)
    started = time.monotonic()
    owner = ProcessOwner()
    watch = ControllerWatch(
        artifacts.root,
        context.cancelled,
        max_bytes=artifacts.max_bytes,
        output_bytes=verifier.command.max_output_bytes,
    )

    def own(pid: int) -> None:
        owner.add(psutil.Process(pid))
        publish(force=True)

    def publish(*, force: bool = False) -> None:
        owner.publish(context.on_identities, force=force)

    try:
        if context.cancelled.is_set():
            raise KeyboardInterrupt
        forbidden = (
            workspace,
            *((context.original_workspace,) if context.original_workspace else ()),
        )
        command = trusted_command(verifier.command, verifier.entrypoint, hidden, forbidden)
        artifacts.record("command", command)
        executable = Path(command.argv[0])
        artifacts.record(
            "executable", {"sha256": digest(read_bytes(executable, limit=256 * 1024 * 1024))}
        )
        launched = time.monotonic()
        result = run_command(
            command,
            workspace,
            environment=_environment(command),
            on_start=own,
            control=CommandControl(cancelled=watch.cancelled, deadline=deadline, on_poll=publish),
        )
        artifacts.append("stdout.bin", result.stdout)
        artifacts.append("stderr.bin", result.stderr)
        expected = (
            verifier.expected_failure_stdout or verifier.expected_stdout
            if result.returncode > 0
            else verifier.expected_stdout
        )
        marker = expected.encode() in result.stdout
        cut_short = (
            result.outcome == "timeout"
            and deadline is not None
            and deadline <= launched + command.timeout_seconds
        )
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
            error="deadline_exhausted"
            if cut_short
            else "expected_execution_evidence_missing"
            if not marker
            else None,
        )
    except (DryheaveError, OSError, ValueError) as error:
        artifacts.record("error", {"type": type(error).__name__, "message": str(error)})
        execution = CheckExecution(
            outcome="error", error=type(error).__name__, elapsed_seconds=time.monotonic() - started
        )
    finally:
        owner.flush(context.on_identities)
        cleanup = owner.stop(timeout=3, terminal_closed=True)
        watch.close()
        publish(force=True)
        artifacts.record("cleanup", cleanup)
    if not cleanup.known_writers_stopped or cleanup.errors or watch.error:
        execution = execution.model_copy(
            update={"outcome": "error", "error": "verifier_cleanup_failed"}
        )
    artifacts.record("execution", execution)
    if context.cancelled.is_set():
        raise KeyboardInterrupt
    return execution


def hidden_inputs(context: VerifierContext, root: Path) -> Path:
    hidden = root / "hidden"
    if not hidden.exists():
        for name, blob in context.case.hidden_files.items():
            atomic_write(hidden / name, context.blobs[blob], replace=False)
    for name, blob in context.case.hidden_files.items():
        if read_bytes(hidden / name, limit=256 * 1024 * 1024) != context.blobs[blob]:
            raise IntegrityError("Materialized hidden verifier input changed.")
    return hidden


def check_path(fact: TaskFact, kind: str) -> Path:
    suite = fact_verifier(fact).suite_id
    return Path("suites" if suite else "facts") / (suite or fact.fact_id) / kind


def evidence_writer(fact: TaskFact, root: Path) -> ArtifactWriter:
    return ArtifactWriter(
        root / (fact.fact_id + "-evidence"),
        max(65536, 8 * fact_verifier(fact).command.max_output_bytes),
    )


def check_copy(context: VerifierContext, fact: TaskFact, kind: str) -> CheckExecution:
    root = context.root / check_path(fact, kind)
    ensure_directory(root)
    artifacts = evidence_writer(fact, root)

    def materialize(workspace: Path) -> None:
        if not workspace.exists():
            materialize_check(context, kind, workspace)

    return _run_copy(context, fact, root, artifacts, materialize, None)


def check_snapshot(
    context: VerifierContext,
    fact: TaskFact,
    snapshot: WorkspaceCapture,
    deadline: float,
) -> tuple[CheckExecution, Path]:
    root = context.root / "facts" / fact.fact_id / "final"
    if root.exists():
        raise InputError("Snapshot verification requires a fresh directory.")
    ensure_directory(root)
    artifacts = evidence_writer(fact, root)

    def materialize(workspace: Path) -> None:
        materialize_files(
            workspace, snapshot.files, context.blobs, directories=snapshot.directories
        )

    return _run_copy(context, fact, root, artifacts, materialize, deadline), artifacts.root


def _run_copy(
    context: VerifierContext,
    fact: TaskFact,
    root: Path,
    artifacts: ArtifactWriter,
    materialize: Callable[[Path], None],
    deadline: float | None,
) -> CheckExecution:
    try:
        workspace = root / "workspace"
        materialize(workspace)
        hidden = hidden_inputs(context, root)
        execution = execute_check(fact, workspace, hidden, artifacts, context, deadline)
    except (DryheaveError, OSError) as error:
        artifacts.record("error", {"type": type(error).__name__, "message": str(error)})
        execution = CheckExecution(outcome="error", error=type(error).__name__)
    try:
        hidden_inputs(context, root)
    except (DryheaveError, OSError):
        if execution.error != "verifier_cleanup_failed":
            execution = execution.model_copy(
                update={"outcome": "error", "error": "hidden_verifier_changed"}
            )
    atomic_write(artifacts.root / "result.json", canonical_json(execution), replace=False)
    return execution


def materialize_check(context: VerifierContext, kind: str, workspace: Path) -> None:
    if kind == "final":
        if not isinstance(context, GradingContext):
            raise InputError("Final grading requires a captured workspace.")
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


def grade_fact(context: GradingContext, fact: TaskFact) -> CriterionResult:
    try:
        final = check_copy(context, fact, "final")
    except (DryheaveError, OSError) as error:
        final = CheckExecution(outcome="error", error=type(error).__name__)
    baseline = reference = None
    try:
        baseline = check_copy(context, fact, "baseline")
        if context.case.reference_patch_file:
            reference = check_copy(context, fact, "reference")
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
        criterion_id=fact.fact_id,
        kind="deterministic",
        required=fact.kind == "requirement",
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
