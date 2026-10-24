import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

from pydantic import Field

from dryheave.cases import FrozenCase, subject_context
from dryheave.controller_models import (
    ControllerRecipe,
    RoleCall,
    SafePersona,
    StandInDecision,
    StandInInput,
)
from dryheave.controllers import CallContext, fit_inspection_to_budget, invoke_controller
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import CancelledError, InputError, NotFoundError
from dryheave.experiment_models import ScoringConfig, inspection_recipe
from dryheave.experiments import load_experiment, load_role
from dryheave.filesystem import atomic_write, read_bytes
from dryheave.journals import RunStore
from dryheave.judges import (
    FactJudgeInput,
    FactJudgeResponse,
    FactJudgment,
    fact_judge_recipe,
    fit_transcript,
    invoke_fact_judge,
    judge_fact,
    settled_judgment,
)
from dryheave.models import Name, ObjectId, ObjectKind, RunId, StrictModel
from dryheave.personas import Persona, subject_persona
from dryheave.runner import cancellation_signals
from dryheave.runner_models import AttemptState
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, parse_model
from dryheave.storage import ObjectStore

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CALL_FILE = re.compile(r"(\d{6})-(request|response|error)\.json")

type ReplayRole = Literal["stand_in", "inspector"]


class ReplayOutput(StrictModel):
    status: str
    error: str | None = None
    decision: StandInDecision | None = None
    judgments: tuple[FactJudgment, ...] = ()


class ReplayedCall(StrictModel):
    call_id: Name
    role: ReplayRole
    source: Literal["store", "run"]
    replay_call_id: Name
    old: ReplayOutput
    new: ReplayOutput
    directory: str


class ReplayReport(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    attempt_id: Name
    voice_id: ObjectId | None = None
    case_id: ObjectId | None = None
    out: str
    calls: tuple[ReplayedCall, ...] = Field(min_length=1)


@dataclass(frozen=True)
class ReplayOverrides:
    voice_id: str | None = None
    persona: Persona | None = None
    case_id: str | None = None
    case: FrozenCase | None = None


@dataclass(frozen=True)
class StoredCall:
    call: RoleCall
    role: ReplayRole
    index: int
    source: Literal["store", "run"]
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class ReplayRoles:
    stand_in: ControllerRecipe
    inspector: ControllerRecipe | None


def _role(call: RoleCall) -> ReplayRole:
    return "stand_in" if call.role == "stand_in" else "inspector"


def _call_prefix(role: ReplayRole, call_id: str) -> re.Pattern[str]:
    middle = "controller" if role == "stand_in" else r"inspection/[^/]+/judge"
    return re.compile(rf"evidence/{middle}/{re.escape(call_id)}/(?P<file>[^/]+)")


def _capture_files(
    store: ObjectStore, state: AttemptState, role: ReplayRole, call_id: str
) -> dict[str, bytes]:
    identifier = state.capture_id or state.quarantine_id
    if identifier is None:
        return {}
    pattern = _call_prefix(role, call_id)
    names = {
        match.group("file"): name
        for name in store.read_envelope(identifier).files
        if (match := pattern.fullmatch(name)) and CALL_FILE.fullmatch(match.group("file"))
    }
    return {
        file: store.read_blob_bounded(identifier, name, limit=MAX_RESPONSE_BYTES)
        for file, name in names.items()
    }


def _run_directories(run: Path, state: AttemptState, role: ReplayRole, call_id: str) -> list[Path]:
    if role == "stand_in":
        return [run / "attempts" / state.attempt_id / "controller" / call_id]
    root = run / "inspections" / state.attempt_id
    return sorted(root.glob(f"*/judge/{call_id}")) if root.is_dir() else []


def _run_files(run: Path, state: AttemptState, role: ReplayRole, call_id: str) -> dict[str, bytes]:
    for directory in _run_directories(run, state, role, call_id):
        if directory.is_dir():
            return {
                path.name: read_bytes(path, limit=MAX_RESPONSE_BYTES)
                for path in directory.iterdir()
                if CALL_FILE.fullmatch(path.name) and path.is_file() and not path.is_symlink()
            }
    return {}


def _latest(files: Mapping[str, bytes], kind: str) -> bytes | None:
    names = sorted(
        name for name in files if (match := CALL_FILE.fullmatch(name)) and match.group(2) == kind
    )
    if not names:
        return None
    return files[names[0] if kind == "request" else names[-1]]


def stored_call(
    store: ObjectStore, execution: ExecutionJournal, state: AttemptState, call: RoleCall
) -> StoredCall:
    role = _role(call)
    index = [item.call_id for item in state.calls if item.role == call.role].index(call.call_id)
    try:
        files = _capture_files(store, state, role, call.call_id)
    except NotFoundError:
        files = {}
    source: Literal["store", "run"] = "store"
    if _latest(files, "request") is None:
        files = _run_files(execution.journal.path, state, role, call.call_id)
        source = "run"
    if _latest(files, "request") is None:
        raise InputError(
            f"No stored request for call {call.call_id}: neither the attempt's capture nor its "
            "run directory retains it, so it cannot be replayed."
        )
    return StoredCall(call=call, role=role, index=index, source=source, files=files)


def _stored_request(stored: StoredCall) -> bytes:
    request = _latest(stored.files, "request")
    if request is None:
        raise InputError(f"No stored request for call {stored.call.call_id}.")
    return request


def stand_in_request(
    stored: StandInInput, overrides: ReplayOverrides, max_input_bytes: int
) -> StandInInput:
    request = stored
    if overrides.persona is not None:
        request = request.model_copy(
            update={
                "persona": parse_model(
                    json.dumps(subject_persona(overrides.persona)).encode(), SafePersona
                )
            }
        )
    case = overrides.case
    if case is not None:
        facts = {fact.fact_id: fact for fact in case.allowed_facts}
        missing = [item.fact_id for item in request.inspection if item.fact_id not in facts]
        if missing:
            raise InputError(f"Replacement case has no fact {', '.join(missing)}.")
        request = request.model_copy(
            update={
                **subject_context(case),
                "inspection": tuple(
                    item.model_copy(update={"text": facts[item.fact_id].text})
                    for item in request.inspection
                ),
            }
        )
    request = parse_model(canonical_json(request), StandInInput)
    return fit_inspection_to_budget(request, max_input_bytes) if request.inspection else request


def inspector_request(
    stored: FactJudgeInput, overrides: ReplayOverrides, max_input_bytes: int
) -> FactJudgeInput:
    request = stored
    case = overrides.case
    if case is not None:
        facts = {fact.fact_id: fact for fact in case.allowed_facts}
        missing = [item.fact_id for item in request.facts if item.fact_id not in facts]
        if missing:
            raise InputError(f"Replacement case has no fact {', '.join(missing)}.")
        request = request.model_copy(
            update={"facts": tuple(judge_fact(facts[item.fact_id]) for item in request.facts)}
        )
    return fit_transcript(parse_model(canonical_json(request), FactJudgeInput), max_input_bytes)


def _old_output(stored: StoredCall) -> ReplayOutput:
    call = stored.call
    if stored.role == "stand_in":
        return ReplayOutput(status=call.status, error=call.error, decision=call.decision)
    response = _latest(stored.files, "response")
    if response is not None:
        return ReplayOutput(
            status=call.status,
            error=call.error,
            judgments=tuple(
                settled_judgment(item)
                for item in parse_model(response, FactJudgeResponse).judgments
            ),
        )
    return ReplayOutput(status=call.status, error=call.error)


def _ignore(_identities: tuple[ProcessIdentity, ...]) -> None:
    return None


def _context(
    recipe: ControllerRecipe,
    index: int,
    runtime: Path,
    inherited: Mapping[str, str],
    cancelled: Event,
) -> CallContext:
    return CallContext(
        call_id="r-" + uuid4().hex,
        index=index,
        deadline=time.monotonic() + recipe.budget.call_seconds,
        cancelled=cancelled,
        inherited=inherited,
        on_identities=_ignore,
        runtime_root=runtime,
    )


@dataclass(frozen=True)
class PreparedCall:
    stored: StoredCall
    recipe: ControllerRecipe
    request: StandInInput | FactJudgeInput


def _prepare(stored: StoredCall, roles: ReplayRoles, overrides: ReplayOverrides) -> PreparedCall:
    content = _stored_request(stored)
    if stored.role == "stand_in":
        recipe = roles.stand_in
        return PreparedCall(
            stored,
            recipe,
            stand_in_request(
                parse_model(content, StandInInput), overrides, recipe.budget.max_input_bytes
            ),
        )
    if roles.inspector is None:
        raise InputError("The experiment has no inspector to replay this call with.")
    judged = parse_model(content, FactJudgeInput)
    recipe = fact_judge_recipe(roles.inspector, len(judged.facts))
    return PreparedCall(
        stored, recipe, inspector_request(judged, overrides, recipe.budget.max_input_bytes)
    )


def _send(
    prepared: PreparedCall, root: Path, inherited: Mapping[str, str], cancelled: Event
) -> tuple[RoleCall, ReplayOutput]:
    recipe, request = prepared.recipe, prepared.request
    budget = recipe.budget
    if isinstance(request, StandInInput):
        artifacts = ArtifactWriter(
            root / "controller", 8 * budget.max_output_bytes + budget.max_input_bytes
        )
        context = _context(recipe, prepared.stored.index, root / "runtime", inherited, cancelled)
        call = invoke_controller(recipe, request, artifacts, context)
        return call, ReplayOutput(status=call.status, error=call.error, decision=call.decision)
    artifacts = ArtifactWriter(root / "judge", budget.max_input_bytes + 8 * budget.max_output_bytes)
    call, judgments = invoke_fact_judge(
        recipe, request, artifacts, _context(recipe, 0, root / "runtime", inherited, cancelled)
    )
    return call, ReplayOutput(status=call.status, error=call.error, judgments=judgments)


def _selected(
    state: AttemptState, call_id: str | None, overrides: ReplayOverrides
) -> tuple[RoleCall, ...]:
    if call_id is None:
        calls = tuple(call for call in state.calls if call.role == "stand_in")
        if not calls:
            raise InputError(f"Attempt {state.attempt_id} made no stand-in calls.")
        return calls
    call = next((item for item in state.calls if item.call_id == call_id), None)
    if call is None:
        raise NotFoundError(
            f"Attempt {state.attempt_id} has no stand-in or inspector call {call_id}."
        )
    if call.role == "judge" and overrides.persona is not None:
        raise InputError("--voice applies to stand-in calls; this is an inspector call.")
    return (call,)


def _output_directory(store: ObjectStore, out: Path) -> Path:
    resolved = out.expanduser().resolve()
    if resolved.is_relative_to(store.root.resolve()):
        raise InputError("Replay output must be outside the store; choose another --out.")
    if resolved.exists():
        raise InputError(f"Replay output directory already exists: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.mkdir(mode=0o700)
    return resolved


def _roles(store: ObjectStore, execution: ExecutionJournal) -> ReplayRoles:
    experiment = load_experiment(store, execution.journal.metadata.experiment_id)
    return ReplayRoles(
        stand_in=load_role(store, experiment.stand_in_id, ControllerRecipe, ObjectKind.STAND_IN),
        inspector=inspection_recipe(
            load_role(store, experiment.scoring_id, ScoringConfig, ObjectKind.SCORING)
        ),
    )


def replay_attempt(  # noqa: PLR0913
    store: ObjectStore,
    run_id: str,
    attempt_id: str,
    *,
    call_id: str | None,
    overrides: ReplayOverrides,
    out: Path,
    inherited: Mapping[str, str],
) -> ReplayReport:
    execution = ExecutionJournal(RunStore(store.root).inspect(run_id))
    state = execution.attempts.get(attempt_id)
    if state is None:
        raise NotFoundError(f"Run has no attempt {attempt_id}.")
    roles = _roles(store, execution)
    prepared = tuple(
        _prepare(stored_call(store, execution, state, call), roles, overrides)
        for call in _selected(state, call_id, overrides)
    )
    cancelled = Event()
    replayed = []
    with store.native_lock(), cancellation_signals(cancelled):
        directory = _output_directory(store, out)
        for item in prepared:
            root = directory / item.stored.call.call_id
            old = _old_output(item.stored)
            call, output = _send(item, root, inherited, cancelled)
            if cancelled.is_set():
                raise CancelledError(f"Replay cancelled; partial output is in {directory}.")
            replayed.append(
                ReplayedCall(
                    call_id=item.stored.call.call_id,
                    role=item.stored.role,
                    source=item.stored.source,
                    replay_call_id=call.call_id,
                    old=old,
                    new=output,
                    directory=str(root),
                )
            )
    report = ReplayReport(
        run_id=execution.journal.metadata.run_id,
        attempt_id=attempt_id,
        voice_id=overrides.voice_id,
        case_id=overrides.case_id,
        out=str(directory),
        calls=tuple(replayed),
    )
    atomic_write(directory / "replay.json", canonical_json(report), replace=False)
    return report


def _decision_lines(output: ReplayOutput) -> list[str]:
    decision = output.decision
    if decision is None:
        return [f"      {output.status}" + (f" ({output.error})" if output.error else "")]
    lines = [f"      {decision.action}" + (f" ({output.error})" if output.error else "")]
    lines.extend(f"        {line}" for line in decision.text.splitlines())
    if decision.choices:
        lines.append(f"        choices: {', '.join(decision.choices)}")
    lines.append(f"        reason: {decision.reason}")
    if decision.fact_ids:
        lines.append(f"        cites: {', '.join(decision.fact_ids)}")
    return lines


def _judgment_lines(output: ReplayOutput) -> list[str]:
    head = f"      {output.status}" + (f" ({output.error})" if output.error else "")
    return [
        head,
        *(
            f"        {item.fact_id}: {item.verdict} — {item.observation}"
            for item in output.judgments
        ),
    ]


def render_replay(report: ReplayReport) -> str:
    lines = [f"Replay of attempt {report.attempt_id} in run {report.run_id}"]
    if report.voice_id:
        lines.append(f"Voice: {report.voice_id}")
    if report.case_id:
        lines.append(f"Case: {report.case_id}")
    lines.append(f"Output: {report.out}")
    for item in report.calls:
        render = _decision_lines if item.role == "stand_in" else _judgment_lines
        label = "stand-in" if item.role == "stand_in" else "inspector"
        lines.extend(
            [
                "",
                f"{label} call {item.call_id} (request from {item.source})",
                "    old:",
                *render(item.old),
                "    new:",
                *render(item.new),
            ]
        )
    return "\n".join(lines)
