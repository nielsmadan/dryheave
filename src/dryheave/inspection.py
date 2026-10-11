import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from threading import Event
from typing import Literal

from pydantic import Field, JsonValue

from dryheave.capture_models import WorkspaceCapture
from dryheave.cases import DeterministicCriterion, FrozenCase, InspectStep, RubricCriterion
from dryheave.controller_models import ControllerRecipe, InspectionFinding, RoleCall
from dryheave.controllers import CallContext
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import DryheaveError, InputError
from dryheave.filesystem import ensure_directory
from dryheave.final_capture import capture_workspace
from dryheave.grading import VerifierContext, check_snapshot
from dryheave.judges import JudgeInput, TranscriptEntry, fit_transcript, invoke_judge, judge_files
from dryheave.models import Name, StrictModel
from dryheave.result_models import CheckExecution
from dryheave.rubrics import graded_criteria
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore

FINDINGS_CHARACTERS = 4000
SNAPSHOT_ATTEMPTS = 2
JUDGE_ATTEMPTS = 2
CLEANUP_ERRORS = frozenset({"judge_cleanup_failed", "verifier_cleanup_failed"})
VERIFIER_INFRASTRUCTURE_ERRORS = frozenset({"verifier_cleanup_failed", "hidden_verifier_changed"})


class InspectionResult(StrictModel):
    step_id: Name
    status: Literal["passed", "failed", "unavailable"]
    findings: tuple[InspectionFinding, ...]
    calls: tuple[RoleCall, ...]
    snapshot_digest: str | None
    error: str | None = None
    seconds: float = Field(ge=0)


@dataclass(frozen=True)
class InspectionRequest:
    store: ObjectStore
    case_id: str
    case: FrozenCase
    step: InspectStep
    workspace: Path
    transcript: tuple[TranscriptEntry, ...]
    recipe: ControllerRecipe | None
    scratch: Path
    retained: Path
    call_context: Callable[[float, Path], CallContext]
    deadline: float
    cancelled: Event
    on_identities: Callable[[tuple[ProcessIdentity, ...]], None]
    previous: InspectionResult | None = None
    private: Path | None = None


class _Unavailable(Exception):
    def __init__(self, error: str, calls: tuple[RoleCall, ...] = ()) -> None:
        super().__init__(error)
        self.error = error
        self.calls = calls


def snapshot_digest(snapshot: WorkspaceCapture) -> str:
    payload: dict[str, JsonValue] = {
        "directories": list(snapshot.directories),
        "files": [[entry.path, entry.mode, entry.sha256] for entry in snapshot.files],
    }
    return digest(canonical_json(payload))


def fit_findings(findings: tuple[InspectionFinding, ...]) -> tuple[InspectionFinding, ...]:
    if not findings:
        return ()
    share = max(2, FINDINGS_CHARACTERS // len(findings))
    return tuple(
        item
        if len(item.rationale) <= share
        else item.model_copy(update={"rationale": item.rationale[: share - 1] + "…"})
        for item in findings
    )


def inspect_workspace(request: InspectionRequest) -> InspectionResult:
    started = time.monotonic()
    attempt = Path(os.path.realpath(request.workspace)).parent
    if any(
        Path(os.path.realpath(root)).is_relative_to(attempt)
        for root in (request.scratch, request.retained)
    ):
        raise InputError("Inspection material must stay outside the attempt tree.")
    ensure_directory(request.scratch)
    if request.private is not None:
        ensure_directory(request.private)
        private = request.private
    else:
        private = Path(mkdtemp(prefix="inspection-", dir=request.scratch))
    try:
        result = _inspect(request, private)
    except BaseException:
        if writers_confirmed_stopped(private):
            _remove(private)
        raise
    if result.error not in CLEANUP_ERRORS and not _remove(private):
        result = result.model_copy(
            update={
                "status": "unavailable",
                "findings": (),
                "error": result.error or "inspection_cleanup_failed",
            }
        )
    return result.model_copy(update={"seconds": time.monotonic() - started})


def _remove(private: Path) -> bool:
    try:
        shutil.rmtree(private)
    except OSError:
        return False
    return True


def writers_confirmed_stopped(private: Path) -> bool:
    for path in private.glob("**/*-cleanup.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if not payload.get("known_writers_stopped", False):
            return False
    return True


def _inspect(request: InspectionRequest, private: Path) -> InspectionResult:
    step_id = request.step.step_id
    current: str | None = None
    try:
        snapshot, blobs = _snapshot(request, private)
        current = snapshot_digest(snapshot)
        previous = request.previous
        if (
            previous is not None
            and previous.step_id == step_id
            and previous.snapshot_digest == current
            and previous.status != "unavailable"
        ):
            return previous.model_copy(update={"calls": ()})
        criteria = {item.criterion_id: item for item in graded_criteria(request.case, judged=True)}
        if any(name not in criteria for name in request.step.criteria):
            raise _Unavailable("unknown_criterion")
        selected = tuple(criteria[name] for name in request.step.criteria)
        verified = _verify(
            request,
            private,
            tuple(item for item in selected if isinstance(item, DeterministicCriterion)),
            (snapshot, blobs),
        )
        rubrics = tuple(item for item in selected if isinstance(item, RubricCriterion))
        calls, judged = (
            _judge(request, private, rubrics, (snapshot, blobs)) if rubrics else ((), ())
        )
    except _Unavailable as unavailable:
        return InspectionResult(
            step_id=step_id,
            status="unavailable",
            findings=(),
            calls=unavailable.calls,
            snapshot_digest=current,
            error=unavailable.error,
            seconds=0,
        )
    findings = fit_findings((*verified, *judged))
    return InspectionResult(
        step_id=step_id,
        status="failed" if findings else "passed",
        findings=findings,
        calls=calls,
        snapshot_digest=current,
        seconds=0,
    )


def _available(request: InspectionRequest, calls: tuple[RoleCall, ...] = ()) -> None:
    if request.cancelled.is_set():
        raise _Unavailable("cancelled", calls)
    if time.monotonic() >= request.deadline:
        raise _Unavailable("deadline_exhausted", calls)


def _snapshot(
    request: InspectionRequest, private: Path
) -> tuple[WorkspaceCapture, dict[str, bytes]]:
    for attempt in range(SNAPSHOT_ATTEMPTS):
        _available(request)
        try:
            snapshot, blobs = capture_workspace(
                request.store,
                request.case.repository_id,
                request.workspace,
                request.case.capture_policy,
                private / f"capture-{attempt}",
            )
        except (DryheaveError, OSError, UnicodeError):
            continue
        if snapshot.complete:
            _available(request)
            return snapshot, blobs
    raise _Unavailable("snapshot_unavailable")


def _retain(source: Path, target: Path) -> None:
    ensure_directory(target.parent)
    shutil.copytree(source, target)


def _verdict(execution: CheckExecution, deadline: float) -> tuple[str, str | None]:
    if execution.error in VERIFIER_INFRASTRUCTURE_ERRORS or execution.returncode is None:
        return "unavailable", execution.error or "verifier_unavailable"
    if execution.process_outcome == "timeout" and time.monotonic() >= deadline:
        return "unavailable", "deadline_exhausted"
    return ("pass" if execution.outcome == "pass" else "fail"), None


def _outcome_category(execution: CheckExecution) -> str:
    if execution.process_outcome == "timeout":
        return "timed out"
    return "failed" if execution.outcome == "fail" else "crashed"


def _verifier_rationale(criterion: DeterministicCriterion, execution: CheckExecution) -> str:
    return f"{criterion.description} ({_outcome_category(execution)})"


def _verify(
    request: InspectionRequest,
    private: Path,
    criteria: tuple[DeterministicCriterion, ...],
    snapshot: tuple[WorkspaceCapture, dict[str, bytes]],
) -> tuple[InspectionFinding, ...]:
    if not criteria:
        return ()
    capture, blobs = snapshot
    try:
        hidden = request.store.read_blobs(
            request.case_id, tuple(request.case.hidden_files.values())
        )
    except (DryheaveError, OSError) as error:
        raise _Unavailable(type(error).__name__) from error
    context = VerifierContext(
        request.store,
        request.case,
        blobs | hidden,
        private / "verifiers",
        request.workspace,
        request.on_identities,
        request.cancelled,
    )
    findings = []
    for criterion in criteria:
        _available(request)
        try:
            execution, evidence = check_snapshot(context, criterion, capture, request.deadline)
            _retain(evidence, request.retained / private.name / "criteria" / criterion.criterion_id)
        except (DryheaveError, OSError) as error:
            raise _Unavailable(type(error).__name__) from error
        verdict, error_name = _verdict(execution, request.deadline)
        if error_name is not None:
            raise _Unavailable(error_name)
        if verdict == "fail":
            findings.append(
                InspectionFinding(
                    criterion_id=criterion.criterion_id,
                    rationale=_verifier_rationale(criterion, execution),
                )
            )
    return tuple(findings)


def _judge(
    request: InspectionRequest,
    private: Path,
    rubrics: tuple[RubricCriterion, ...],
    snapshot: tuple[WorkspaceCapture, dict[str, bytes]],
) -> tuple[tuple[RoleCall, ...], tuple[InspectionFinding, ...]]:
    recipe = request.recipe
    if recipe is None:
        raise _Unavailable("no_inspector")
    files, omitted = judge_files(snapshot[0].files, snapshot[1])
    judge_input = fit_transcript(
        JudgeInput(
            initial_prompt=request.case.initial_prompt,
            criteria=rubrics,
            files=files,
            omitted_binary_files=omitted,
            transcript=request.transcript,
        ),
        recipe.budget.max_input_bytes,
    )
    calls: list[RoleCall] = []
    for attempt in range(JUDGE_ATTEMPTS):
        _available(request, tuple(calls))
        try:
            context = request.call_context(
                min(request.deadline, time.monotonic() + recipe.budget.call_seconds),
                private / "runtime" / str(attempt),
            )
            artifacts = ArtifactWriter(
                private / "judge" / context.call_id,
                recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes,
            )
            call, _, _, judgments = invoke_judge(recipe, judge_input, artifacts, context)
            calls.append(call)
            _retain(artifacts.root, request.retained / private.name / "judge" / context.call_id)
        except (DryheaveError, OSError) as error:
            raise _Unavailable(type(error).__name__, tuple(calls)) from error
        if call.cleanup is not None and not call.cleanup.known_writers_stopped:
            raise _Unavailable("judge_cleanup_failed", tuple(calls))
        verdicts = {item.criterion_id: item for item in judgments if item.outcome != "error"}
        if call.status == "completed" and all(item.criterion_id in verdicts for item in rubrics):
            return tuple(calls), tuple(
                InspectionFinding(
                    criterion_id=item.criterion_id,
                    rationale=verdicts[item.criterion_id].rationale,
                )
                for item in rubrics
                if verdicts[item.criterion_id].outcome == "fail"
            )
    raise _Unavailable("judge_unavailable", tuple(calls))
