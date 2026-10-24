import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from tempfile import mkdtemp
from threading import Event

from pydantic import Field, JsonValue

from dryheave.capture_models import WorkspaceCapture
from dryheave.cases import FrozenCase, TaskFact, fact_verifier
from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.controllers import CallContext
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import DryheaveError, InputError
from dryheave.filesystem import ensure_directory
from dryheave.final_capture import capture_workspace
from dryheave.grading import VerifierContext, check_snapshot
from dryheave.inspection_scratch import writers_confirmed_stopped
from dryheave.judges import (
    FACT_OBSERVATION_CHARACTERS,
    FactInspection,
    FactJudgment,
    TranscriptEntry,
    fact_judge_recipe,
    fact_projection,
    fit_transcript,
    invoke_fact_judge,
    judge_files,
)
from dryheave.models import StrictModel
from dryheave.result_models import (
    VERIFIER_HARNESS_ERRORS,
    CheckExecution,
    VerifierVerdict,
    verifier_verdict,
)
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore

SNAPSHOT_ATTEMPTS = 2
JUDGE_ATTEMPTS = 2
CLEANUP_ERRORS = frozenset({"judge_cleanup_failed", "verifier_cleanup_failed"})


class InspectionResult(StrictModel):
    inspection: FactInspection
    calls: tuple[RoleCall, ...]
    seconds: float = Field(ge=0)


@dataclass(frozen=True)
class InspectionRequest:
    store: ObjectStore
    case_id: str
    case: FrozenCase
    facts: tuple[TaskFact, ...]
    workspace: Path
    transcript: tuple[TranscriptEntry, ...]
    recipe: ControllerRecipe | None
    scratch: Path
    retained: Path
    call_context: Callable[[float, Path], CallContext]
    deadline: float
    cancelled: Event
    on_identities: Callable[[tuple[ProcessIdentity, ...]], None]
    inspection_id: str
    turn: int
    previous: tuple[FactInspection, ...] = ()
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


def input_digest(
    snapshot: str, transcript: tuple[TranscriptEntry, ...], facts: tuple[TaskFact, ...]
) -> str:
    payload: dict[str, JsonValue] = {
        "snapshot": snapshot,
        "transcript": [entry.model_dump(mode="json") for entry in transcript],
        "facts": [fact.model_dump(mode="json") for fact in facts],
    }
    return digest(canonical_json(payload))


def unavailable(
    request: InspectionRequest, error: str, snapshot: str | None = None
) -> FactInspection:
    return FactInspection(
        inspection_id=request.inspection_id,
        turn=request.turn,
        snapshot_digest=snapshot,
        input_digest=None,
        status="unavailable",
        error=error,
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
    error = result.inspection.error
    if error not in CLEANUP_ERRORS and not _remove(private):
        result = result.model_copy(
            update={
                "inspection": unavailable(
                    request,
                    error or "inspection_cleanup_failed",
                    result.inspection.snapshot_digest,
                ),
            }
        )
    return result.model_copy(update={"seconds": time.monotonic() - started})


def _remove(private: Path) -> bool:
    try:
        shutil.rmtree(private)
    except OSError:
        return False
    return True


def _inspect(request: InspectionRequest, private: Path) -> InspectionResult:
    current: str | None = None
    try:
        snapshot, blobs = _snapshot(request, private)
        current = snapshot_digest(snapshot)
        key = input_digest(current, request.transcript, request.facts)
        previous = next(
            (
                item
                for item in reversed(request.previous)
                if item.status == "completed" and item.input_digest == key
            ),
            None,
        )
        if previous is not None:
            return InspectionResult(
                inspection=previous.model_copy(
                    update={"inspection_id": request.inspection_id, "turn": request.turn}
                ),
                calls=(),
                seconds=0,
            )
        verified = _verify(
            request,
            private,
            tuple(item for item in request.facts if item.verifier is not None),
            (snapshot, blobs),
        )
        judged_facts = tuple(item for item in request.facts if item.verifier is None)
        calls, judged = (
            _judge(request, private, judged_facts, (snapshot, blobs)) if judged_facts else ((), {})
        )
    except _Unavailable as failure:
        return InspectionResult(
            inspection=unavailable(request, failure.error, current),
            calls=failure.calls,
            seconds=0,
        )
    settled = verified | judged
    return InspectionResult(
        inspection=FactInspection(
            inspection_id=request.inspection_id,
            turn=request.turn,
            snapshot_digest=current,
            input_digest=key,
            status="completed",
            judgments=tuple(settled[item.fact_id] for item in request.facts),
        ),
        calls=calls,
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


def _irregular(directory: str, names: list[str]) -> set[str]:
    modes = {name: os.lstat(os.path.join(directory, name)).st_mode for name in names}
    return {name for name, mode in modes.items() if not (S_ISREG(mode) or S_ISDIR(mode))}


def _retain(source: Path, target: Path) -> None:
    ensure_directory(target.parent)
    shutil.copytree(source, target, symlinks=True, ignore=_irregular)


def _outcome_category(verdict: VerifierVerdict | None, execution: CheckExecution) -> str:
    if verdict is None:
        return "verifier gave no result"
    if verdict == "met":
        return "passed"
    if execution.process_outcome == "timeout":
        return "timed out"
    if execution.process_outcome == "output_limit":
        return "output limit"
    return "failed" if execution.outcome == "fail" else "crashed"


def _execution_details(execution: CheckExecution) -> str:
    details = (
        execution.process_outcome or "not launched",
        None if execution.returncode is None else f"exit code {execution.returncode}",
        execution.error,
    )
    return ", ".join(item for item in details if item)


def _observation(text: str) -> str:
    if len(text) <= FACT_OBSERVATION_CHARACTERS:
        return text
    return text[: FACT_OBSERVATION_CHARACTERS - 1] + "…"


def verifier_judgment(fact: TaskFact, execution: CheckExecution) -> FactJudgment:
    verdict = verifier_verdict(execution)
    return FactJudgment(
        fact_id=fact.fact_id,
        verdict=verdict or "unclear",
        observation=_observation(f"{fact.text} ({_outcome_category(verdict, execution)})"),
        evidence=f"verifier {fact_verifier(fact).entrypoint}: {_execution_details(execution)}",
    )


def _verify(
    request: InspectionRequest,
    private: Path,
    facts: tuple[TaskFact, ...],
    snapshot: tuple[WorkspaceCapture, dict[str, bytes]],
) -> dict[str, FactJudgment]:
    if not facts:
        return {}
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
    judgments = {}
    for fact in facts:
        _available(request)
        try:
            execution, evidence = check_snapshot(context, fact, capture, request.deadline)
            _retain(evidence, request.retained / private.name / "facts" / fact.fact_id)
        except (DryheaveError, OSError) as error:
            raise _Unavailable(type(error).__name__) from error
        if execution.error is not None and execution.error in VERIFIER_HARNESS_ERRORS:
            raise _Unavailable(execution.error)
        judgments[fact.fact_id] = verifier_judgment(fact, execution)
    return judgments


def _judge_call(
    request: InspectionRequest,
    private: Path,
    facts: tuple[TaskFact, ...],
    snapshot: tuple[WorkspaceCapture, dict[str, bytes]],
    attempt: str,
) -> tuple[RoleCall, dict[str, FactJudgment]]:
    if request.recipe is None:
        raise _Unavailable("no_inspector")
    recipe = fact_judge_recipe(request.recipe, len(facts))
    judge_input = fit_transcript(
        fact_projection(
            request.case, facts, judge_files(snapshot[0].files, snapshot[1]), request.transcript
        ),
        recipe.budget.max_input_bytes,
    )
    context = request.call_context(
        min(request.deadline, time.monotonic() + recipe.budget.call_seconds),
        private / "runtime" / attempt,
    )
    artifacts = ArtifactWriter(
        private / "judge" / context.call_id,
        recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes,
    )
    call, judgments = invoke_fact_judge(recipe, judge_input, artifacts, context)
    _retain(artifacts.root, request.retained / private.name / "judge" / context.call_id)
    if call.cleanup is not None and not call.cleanup.known_writers_stopped:
        raise _Unavailable("judge_cleanup_failed", (call,))
    verdicts = {item.fact_id: item for item in judgments} if call.status == "completed" else {}
    return call, verdicts


def _judge(
    request: InspectionRequest,
    private: Path,
    facts: tuple[TaskFact, ...],
    snapshot: tuple[WorkspaceCapture, dict[str, bytes]],
) -> tuple[tuple[RoleCall, ...], dict[str, FactJudgment]]:
    calls: list[RoleCall] = []
    judged: dict[str, FactJudgment] | None = None
    for attempt in range(JUDGE_ATTEMPTS):
        _available(request, tuple(calls))
        try:
            call, verdicts = _judge_call(request, private, facts, snapshot, str(attempt))
        except _Unavailable as failure:
            raise _Unavailable(failure.error, (*calls, *failure.calls)) from failure
        except (DryheaveError, OSError) as error:
            raise _Unavailable(type(error).__name__, tuple(calls)) from error
        calls.append(call)
        if all(item.fact_id in verdicts for item in facts):
            judged = verdicts
            break
    if judged is None:
        raise _Unavailable("judge_unavailable", tuple(calls))
    unclear = tuple(item for item in facts if judged[item.fact_id].verdict == "unclear")
    if not unclear or request.cancelled.is_set() or time.monotonic() >= request.deadline:
        return tuple(calls), judged
    try:
        call, rejudged = _judge_call(request, private, unclear, snapshot, "rejudge")
    except _Unavailable as failure:
        raise _Unavailable(failure.error, (*calls, *failure.calls)) from failure
    except (DryheaveError, OSError) as error:
        raise _Unavailable(type(error).__name__, tuple(calls)) from error
    return (*calls, call), judged | {
        fact_id: judgment for fact_id, judgment in rejudged.items() if fact_id in judged
    }
