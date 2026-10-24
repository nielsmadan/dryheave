from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from dryheave.cases import CaseContent, FrozenCase
from dryheave.controller_models import RoleCall, StandInDecision
from dryheave.errors import DryheaveError, IntegrityError, NotFoundError
from dryheave.experiment_models import FrozenExperiment
from dryheave.judges import FactInspection, RaiseEvent
from dryheave.models import JournalEvent, ObjectKind
from dryheave.result_models import Assessment
from dryheave.runner_models import (
    CapturedAttempt,
    QuarantinedCapture,
    WorkflowEvent,
    WorkflowStepRecord,
)
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, parse_model
from dryheave.storage import ObjectStore
from dryheave.transcript_models import (
    Author,
    ConversationLine,
    GapFact,
    Gaps,
    InspectedFact,
    ReviewLanes,
    SceneEntry,
    StandInPurpose,
)
from dryheave.workflow import (
    FACT_INSPECTION_EVENT,
    INSPECTION_INTENT_EVENT,
    RAISE_EVENT,
    blocking_phase,
    raised_facts,
)

INTERACTION_EVENT = "interaction"
SUBMISSION_EVENT = "submission-intent"
CONTROLLER_INTENT_EVENT = "controller-intent"
CONTROLLER_RESULT_EVENT = "controller-result"
FALLBACK_EVENT = "stand-in-fallback"
WORKFLOW_STEP_EVENT = "workflow-step"
NO_JOURNAL_NOTE = (
    "No run journal is available here, so behind-the-scenes entries come from the capture: "
    "inspections carry no per-fact verdicts and stand-in calls carry no turn."
)


@dataclass(frozen=True)
class AttemptMaterial:
    run_id: str
    attempt_id: str
    events: tuple[JournalEvent, ...] | None
    case_id: str | None = None
    case: CaseContent | None = None
    graded_case: CaseContent | None = None
    capture: CapturedAttempt | None = None
    assessment: Assessment | None = None
    stop_reason: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttemptRefs:
    run_id: str
    attempt_id: str
    capture_id: str | None
    quarantine_id: str | None
    assessment_id: str | None
    case_id: str | None = None
    stop_reason: str | None = None


def _stand_in(
    call: RoleCall, turn: int | None, purpose: StandInPurpose | None, fix: tuple[str, ...]
) -> SceneEntry:
    decision = call.decision
    return SceneEntry(
        kind="stand_in",
        turn=turn,
        call_id=call.call_id,
        purpose=purpose,
        status=call.status,
        action=decision.action if decision else None,
        text=_decision_text(decision),
        reason=decision.reason if decision else None,
        cited=decision.fact_ids if decision else (),
        handed=fix if purpose == "fix_request" else (),
        rejection=call.error if call.status == "failed" else None,
    )


def _decision_text(decision: StandInDecision | None) -> str | None:
    if decision is None:
        return None
    if decision.choices:
        return ", ".join(decision.choices) + (f" ({decision.text})" if decision.text else "")
    if decision.screen_action is not None:
        action = decision.screen_action
        return action.text if action.text is not None else f"key {action.key}"
    return decision.text or None


def _inspection(inspection: FactInspection, turn: int) -> SceneEntry:
    return SceneEntry(
        kind="inspection",
        turn=turn,
        inspection_id=inspection.inspection_id,
        status=inspection.status,
        detail=inspection.error,
        facts=tuple(
            InspectedFact(
                fact_id=item.fact_id,
                verdict=item.verdict,
                observation=item.observation,
                evidence=item.evidence,
            )
            for item in inspection.judgments
        ),
    )


def _correction(event: RaiseEvent) -> SceneEntry:
    return SceneEntry(
        kind="correction",
        turn=event.turn,
        call_id=event.call_id,
        phase=event.phase,
        handed=event.fact_ids,
        cited=event.cited,
        detail=event.cause,
    )


@dataclass
class _Lanes:
    case: CaseContent | None
    resolved: frozenset[str]
    answered: frozenset[str]
    lines: list[ConversationLine] = field(default_factory=list)
    scenes: list[SceneEntry] = field(default_factory=list)
    turn: int = 0
    delivering: bool = False
    fix: tuple[str, tuple[str, ...]] | None = None
    raises: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    sources: list[tuple[Author, str | None, str]] = field(default_factory=list)

    def submission(self, _event: JournalEvent) -> None:
        self.delivering = True

    def interaction(self, event: JournalEvent) -> None:
        accepted = event.data.get("accepted_turns")
        prompt = event.data.get("prompt")
        observation = event.data.get("observation")
        if type(accepted) is int and accepted > self.turn:
            self.turn = accepted
            if isinstance(prompt, str):
                author, origin = self._author(prompt)
                self.lines.append(
                    ConversationLine(
                        turn=self.turn, role="user", text=prompt, author=author, origin=origin
                    )
                )
        text = observation.get("text") if isinstance(observation, dict) else None
        if isinstance(text, str) and text:
            self.lines.append(ConversationLine(turn=self.turn, role="assistant", text=text))
        self.delivering = False
        self.fix = None
        self.sources = []

    def _author(self, prompt: str) -> tuple[Author, str | None]:
        if not any(line.role == "user" for line in self.lines):
            return "initial", None
        match = next((item for item in reversed(self.sources) if item[2] == prompt), None)
        return (match[0], match[1]) if match else ("unknown", None)

    def stand_in_intent(self, event: JournalEvent) -> None:
        identifier = event.data.get("call_id")
        if isinstance(identifier, str) and identifier not in self.answered:
            self.scenes.append(
                SceneEntry(
                    kind="stand_in",
                    turn=self.turn,
                    call_id=identifier,
                    status="unknown",
                    detail="The call started but recorded no result.",
                )
            )

    def controller(self, event: JournalEvent) -> None:
        call = parse_model(canonical_json(event.data), RoleCall)
        if call.role != "stand_in":
            return
        decision = call.decision
        purpose: StandInPurpose = (
            ("screen" if decision and decision.screen_kind else "dialog")
            if self.delivering
            else "fix_request"
            if self.fix is not None
            else "reply"
        )
        self.scenes.append(
            _stand_in(call, self.turn, purpose, self.fix[1] if self.fix is not None else ())
        )
        if (
            call.status == "completed"
            and decision is not None
            and decision.action == "reply"
            and purpose in {"reply", "fix_request"}
        ):
            self.sources.append(("stand_in", call.call_id, decision.text))

    def fallback(self, event: JournalEvent) -> None:
        decision = parse_model(canonical_json(event.data), StandInDecision)
        phase, handed = self.fix if self.fix is not None else (None, ())
        self.scenes.append(
            SceneEntry(
                kind="fallback",
                turn=self.turn,
                text=decision.text,
                reason=decision.reason,
                phase=phase,
                handed=handed,
            )
        )
        self.sources.append(("fallback", None, decision.text))

    def intent(self, event: JournalEvent) -> None:
        identifier = event.data.get("inspection_id")
        if isinstance(identifier, str) and identifier not in self.resolved:
            self.scenes.append(
                SceneEntry(
                    kind="inspection",
                    turn=self.turn,
                    inspection_id=identifier,
                    status="unknown",
                    detail="The inspection started but recorded no result.",
                )
            )

    def inspection(self, event: JournalEvent) -> None:
        inspection = parse_model(canonical_json(event.data), FactInspection)
        self.scenes.append(_inspection(inspection, self.turn))
        if self.case is None or inspection.status != "completed":
            return
        verdicts = {item.fact_id: item.verdict for item in inspection.judgments}
        phase = blocking_phase(self.case, verdicts)
        if phase is not None:
            self.raises[inspection.inspection_id] = (
                phase,
                tuple(fact.fact_id for fact in raised_facts(self.case, phase, verdicts)),
            )

    def step(self, event: JournalEvent) -> None:
        record = parse_model(canonical_json(event.data), WorkflowStepRecord).event
        if record.kind == "inspect" and record.outcome != "fix_requested" and record.call_ids:
            self._attach_calls(record)
        if record.kind == "inspect" and record.step_id in self.raises:
            self.fix = self.raises[record.step_id] if record.outcome == "failed" else None
        elif record.kind == "prompt" and record.outcome == "sent":
            self.scenes.append(_prompt_step(record, self.case, record.turn))
            self._attribute(record)

    def _attach_calls(self, record: WorkflowEvent) -> None:
        position = next(
            (
                index
                for index in range(len(self.scenes) - 1, -1, -1)
                if self.scenes[index].kind == "inspection"
                and self.scenes[index].inspection_id == record.step_id
            ),
            None,
        )
        if position is not None:
            self.scenes[position] = self.scenes[position].model_copy(
                update={"call_ids": record.call_ids}
            )

    def _attribute(self, record: WorkflowEvent) -> None:
        position = next(
            (
                index
                for index in range(len(self.lines) - 1, -1, -1)
                if self.lines[index].role == "user"
                and self.lines[index].turn == record.turn + 1
                and self.lines[index].author == "unknown"
            ),
            None,
        )
        if position is not None:
            self.lines[position] = self.lines[position].model_copy(
                update={"author": "workflow", "origin": record.step_id}
            )

    def correction(self, event: JournalEvent) -> None:
        self.scenes.append(_correction(parse_model(canonical_json(event.data), RaiseEvent)))


def _prompt_step(record: WorkflowEvent, case: CaseContent | None, turn: int | None) -> SceneEntry:
    declared = next(
        (step.prompt for step in (case.workflow if case else ()) if step.step_id == record.step_id),
        None,
    )
    return SceneEntry(
        kind="prompt_step",
        turn=turn,
        text=record.delivered_text or declared,
        detail=record.step_id,
    )


def _journal_lanes(
    case: CaseContent | None, events: tuple[JournalEvent, ...]
) -> tuple[tuple[ConversationLine, ...], tuple[SceneEntry, ...]]:
    resolved = frozenset(
        identifier
        for event in events
        if event.event == FACT_INSPECTION_EVENT
        and isinstance(identifier := event.data.get("inspection_id"), str)
    )
    answered = frozenset(
        identifier
        for event in events
        if event.event == CONTROLLER_RESULT_EVENT
        and isinstance(identifier := event.data.get("call_id"), str)
    )
    lanes = _Lanes(case=case, resolved=resolved, answered=answered)
    handlers: Mapping[str, Callable[[JournalEvent], None]] = {
        SUBMISSION_EVENT: lanes.submission,
        INTERACTION_EVENT: lanes.interaction,
        CONTROLLER_INTENT_EVENT: lanes.stand_in_intent,
        CONTROLLER_RESULT_EVENT: lanes.controller,
        FALLBACK_EVENT: lanes.fallback,
        INSPECTION_INTENT_EVENT: lanes.intent,
        FACT_INSPECTION_EVENT: lanes.inspection,
        WORKFLOW_STEP_EVENT: lanes.step,
        RAISE_EVENT: lanes.correction,
    }
    for event in events:
        handler = handlers.get(event.event)
        if handler is not None:
            handler(event)
    return tuple(lanes.lines), tuple(lanes.scenes)


def _capture_conversation(capture: CapturedAttempt) -> tuple[ConversationLine, ...]:
    replies = {
        call.decision.text: call.call_id
        for call in capture.controller_calls
        if call.role == "stand_in" and call.decision is not None and call.decision.text
    }
    lines: list[ConversationLine] = []
    turn = 0
    for message in capture.dialogue:
        if message.role == "assistant":
            lines.append(ConversationLine(turn=turn, role="assistant", text=message.text))
            continue
        author: Author = (
            "initial" if turn == 0 else "stand_in" if message.text in replies else "unknown"
        )
        turn += 1
        lines.append(
            ConversationLine(
                turn=turn,
                role="user",
                text=message.text,
                author=author,
                origin=replies.get(message.text) if author == "stand_in" else None,
            )
        )
    return tuple(lines)


def _workflow_scene(
    record: WorkflowEvent, case: CaseContent | None, calls: Mapping[str, RoleCall]
) -> SceneEntry | None:
    if record.kind == "prompt" and record.outcome == "sent":
        return _prompt_step(record, case, record.turn)
    if record.kind == "inspect" and record.outcome == "fix_requested":
        return SceneEntry(
            kind="correction",
            turn=record.turn,
            call_id=next((item for item in reversed(record.call_ids) if item in calls), None),
            phase=record.step_id,
            handed=record.failed_criteria,
        )
    if record.kind == "inspect":
        return SceneEntry(
            kind="inspection",
            turn=record.turn,
            inspection_id=record.step_id,
            call_ids=record.call_ids,
            status=record.outcome,
            handed=record.failed_criteria,
        )
    return None


def _capture_scenes(capture: CapturedAttempt, case: CaseContent | None) -> tuple[SceneEntry, ...]:
    calls = {call.call_id: call for call in capture.controller_calls if call.role == "stand_in"}
    stand_ins = tuple(_stand_in(call, None, None, ()) for call in calls.values())
    workflow = capture.workflow.events if capture.workflow is not None else ()
    return (
        *stand_ins,
        *(
            scene
            for record in workflow
            if (scene := _workflow_scene(record, case, calls)) is not None
        ),
    )


def _gaps(assessment: Assessment | None, case: CaseContent | None) -> Gaps:
    if assessment is None:
        return Gaps(assessed=False)
    texts = {fact.fact_id: fact.text for fact in case.allowed_facts} if case else {}
    return Gaps(
        assessed=True,
        facts=tuple(
            GapFact(
                fact_id=item.fact_id,
                kind=item.kind,
                phase=item.phase,
                verdict=item.verdict,
                text=texts.get(item.fact_id),
                observation=item.observation,
                evidence=item.evidence,
                source=item.source,
            )
            for item in assessment.facts
            if item.verdict != "met"
        ),
        uncovered=assessment.uncovered,
        unreached_phases=assessment.fact_account.unreached_phases
        if assessment.fact_account is not None
        else (),
    )


def review_lanes(material: AttemptMaterial) -> ReviewLanes:
    capture, assessment = material.capture, material.assessment
    notes = list(material.notes)
    if material.events is not None:
        events = tuple(
            event for event in material.events if event.attempt_id == material.attempt_id
        )
        conversation, scenes = _journal_lanes(material.case, events)
    elif capture is not None:
        notes.append(NO_JOURNAL_NOTE)
        conversation, scenes = (
            _capture_conversation(capture),
            _capture_scenes(capture, material.case),
        )
    else:
        notes.append("Neither a run journal nor a capture is available for this attempt.")
        conversation, scenes = (), ()
    stop_reason = material.stop_reason or (capture.stop_reason if capture else None)
    if stop_reason is not None:
        scenes = (*scenes, SceneEntry(kind="stop", detail=stop_reason))
    return ReviewLanes(
        run_id=material.run_id,
        attempt_id=material.attempt_id,
        case_id=material.case_id,
        outcome=assessment.outcome if assessment else None,
        outcome_reason=assessment.outcome_reason if assessment else None,
        stop_reason=stop_reason,
        conversation=conversation,
        behind_the_scenes=scenes,
        gaps=_gaps(assessment, material.graded_case or material.case),
        notes=tuple(notes),
    )


def _indented(text: str, depth: int = 6) -> list[str]:
    pad = " " * depth
    return [pad + line if line else "" for line in text.splitlines() or [""]]


def _ids(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


def _when(turn: int | None) -> str:
    return "  " if turn is None else f"  turn {turn} · "


def _conversation_lines(lanes: ReviewLanes) -> list[str]:
    if not lanes.conversation:
        return ["  (no conversation retained)"]
    rendered: list[str] = []
    for line in lanes.conversation:
        who = "agent" if line.role == "assistant" else "user"
        if line.author not in {None, "unknown"}:
            source = (line.author or "").replace("_", "-")
            who += f" ({source}{f' {line.origin}' if line.origin else ''})"
        rendered.append(f"  turn {line.turn} · {who}:")
        rendered.extend(_indented(line.text))
    return rendered


def _stand_in_lines(entry: SceneEntry) -> list[str]:
    purpose = (entry.purpose or "call").replace("_", " ")
    status = (
        f"rejected ({entry.rejection})"
        if entry.status == "failed"
        else entry.action or entry.status or "unknown"
    )
    head = f"{_when(entry.turn)}stand-in {purpose} {entry.call_id}: {status}"
    rendered = [head + (f" ({entry.detail})" if entry.detail else "")]
    if entry.handed:
        rendered.append(f"      handed: {_ids(entry.handed)}")
    if entry.text:
        rendered.append("      said:")
        rendered.extend(_indented(entry.text, 8))
    if entry.reason:
        rendered.append(f"      reason: {entry.reason}")
    if entry.cited:
        rendered.append(f"      cites: {_ids(entry.cited)}")
    return rendered


def _inspection_lines(entry: SceneEntry) -> list[str]:
    head = f"{_when(entry.turn)}inspection {entry.inspection_id}: {entry.status}"
    rendered = [head + (f" ({entry.detail})" if entry.detail else "")]
    if entry.call_ids:
        rendered.append(f"      calls: {_ids(entry.call_ids)}")
    if entry.handed:
        rendered.append(f"      unmet: {_ids(entry.handed)}")
    for fact in entry.facts:
        rendered.append(f"      {fact.fact_id}: {fact.verdict} — {fact.observation}")
        if fact.evidence:
            rendered.append(f"        evidence: {fact.evidence}")
    return rendered


def _scene_lines(entry: SceneEntry) -> list[str]:
    if entry.kind == "stand_in":
        return _stand_in_lines(entry)
    if entry.kind == "inspection":
        return _inspection_lines(entry)
    if entry.kind == "correction":
        cause = f"; cause {entry.detail}" if entry.detail else ""
        cited = f"; stand-in cited {_ids(entry.cited)}" if entry.cited else ""
        return [
            (
                f"{_when(entry.turn)}correction in phase {entry.phase} via {entry.call_id}: "
                f"raised {_ids(entry.handed)}{cause}{cited}"
            )
        ]
    if entry.kind == "fallback":
        return [
            (
                f"{_when(entry.turn)}fallback fix reply for phase {entry.phase}: "
                f"raised {_ids(entry.handed)}"
            ),
            *_indented(entry.text or "", 8),
        ]
    if entry.kind == "prompt_step":
        return [f"{_when(entry.turn)}workflow prompt {entry.detail}:", *_indented(entry.text or "")]
    return [f"  stopped: {entry.detail}"]


def _gap_lines(gaps: Gaps) -> list[str]:
    if not gaps.assessed:
        return ["  (not assessed yet)"]
    rendered: list[str] = []
    for fact in gaps.facts:
        rendered.append(
            f"  {fact.fact_id} ({fact.kind}, phase {fact.phase}): {fact.verdict} [{fact.source}]"
        )
        if fact.text:
            rendered.append(f"      wanted: {fact.text}")
        rendered.append(f"      saw: {fact.observation or '(no observation)'}")
        if fact.evidence:
            rendered.append(f"      evidence: {fact.evidence}")
    if gaps.uncovered:
        rendered.append("  Problems no fact covers:")
        rendered.extend(f"    - {item}" for item in gaps.uncovered)
    if gaps.unreached_phases:
        rendered.append(f"  Unreached phases: {_ids(gaps.unreached_phases)}")
    return rendered or ["  (no gaps: every fact was met and nothing uncovered)"]


def render_lanes(lanes: ReviewLanes) -> str:
    outcome = (
        "not assessed"
        if lanes.outcome is None
        else lanes.outcome + (f" ({lanes.outcome_reason})" if lanes.outcome_reason else "")
    )
    rendered = [
        f"Attempt {lanes.attempt_id} of run {lanes.run_id}",
        f"Case: {lanes.case_id or 'unknown'}",
        f"Outcome: {outcome} · stop: {lanes.stop_reason or 'unknown'}",
        "",
        "== Conversation (what the agent saw) ==",
        *_conversation_lines(lanes),
        "",
        "== Behind the scenes ==",
        *(line for entry in lanes.behind_the_scenes for line in _scene_lines(entry)),
        "",
        "== Gaps ==",
        *_gap_lines(lanes.gaps),
    ]
    if lanes.notes:
        rendered.extend(["", "Notes:", *(f"  - {note}" for note in lanes.notes)])
    return "\n".join(rendered)


def run_refs(store: ObjectStore, execution: ExecutionJournal, attempt_id: str) -> AttemptRefs:
    state = execution.attempts.get(attempt_id)
    if state is None:
        raise NotFoundError(f"Run has no attempt {attempt_id}.")
    try:
        experiment = store.read_payload(
            execution.journal.metadata.experiment_id, FrozenExperiment, kind=ObjectKind.EXPERIMENT
        )
        case_id = next(
            (trial.case_id for trial in experiment.trials if trial.trial_id == state.trial_id),
            None,
        )
    except DryheaveError:
        case_id = None
    return AttemptRefs(
        run_id=execution.journal.metadata.run_id,
        attempt_id=attempt_id,
        capture_id=state.capture_id,
        quarantine_id=state.quarantine_id,
        assessment_id=state.assessment_id,
        case_id=case_id,
        stop_reason=state.stop_reason,
    )


def _capture(store: ObjectStore, refs: AttemptRefs) -> CapturedAttempt | None:
    identifier = refs.capture_id or refs.quarantine_id
    if identifier is None:
        return None
    envelope = store.read_envelope(identifier)
    if envelope.kind == ObjectKind.QUARANTINE:
        capture = parse_model(canonical_json(envelope.payload), QuarantinedCapture).capture
    elif envelope.kind == ObjectKind.CAPTURE:
        capture = parse_model(canonical_json(envelope.payload), CapturedAttempt)
    else:
        raise IntegrityError("Retained attempt evidence has an unexpected object kind.")
    if (capture.run_id, capture.attempt_id) != (refs.run_id, refs.attempt_id):
        raise IntegrityError("Captured payload identity differs from the retained attempt.")
    return capture


def _assessment(store: ObjectStore, refs: AttemptRefs) -> Assessment | None:
    if refs.assessment_id is None:
        return None
    assessment = store.read_payload(refs.assessment_id, Assessment, kind=ObjectKind.RESULT)
    if (assessment.run_id, assessment.attempt_id) != (refs.run_id, refs.attempt_id):
        raise IntegrityError("Assessment identity differs from the retained attempt.")
    return assessment


def _case(store: ObjectStore, case_id: str) -> FrozenCase:
    return store.read_payload(case_id, FrozenCase, kind=ObjectKind.CASE)


def _loaded[T](load: Callable[[], T | None], label: str, notes: list[str]) -> T | None:
    try:
        return load()
    except DryheaveError as error:
        notes.append(f"The {label} could not be read ({error.code}).")
        return None


def load_material(
    store: ObjectStore,
    refs: AttemptRefs,
    events: tuple[JournalEvent, ...] | None,
    *,
    assessment: Assessment | None = None,
) -> AttemptMaterial:
    notes: list[str] = []
    capture = _loaded(lambda: _capture(store, refs), "capture", notes)
    assessment = assessment or _loaded(lambda: _assessment(store, refs), "assessment", notes)
    case_id = refs.case_id or (capture.case_id if capture else None)
    case_id = case_id or (assessment.case_id if assessment else None)
    case = _loaded(lambda: _case(store, case_id), "case", notes) if case_id else None
    graded_id = assessment.graded_case_id if assessment else None
    graded = (
        _loaded(lambda: _case(store, graded_id), "graded case", notes)
        if graded_id is not None and graded_id != case_id
        else None
    )
    return AttemptMaterial(
        run_id=refs.run_id,
        attempt_id=refs.attempt_id,
        events=events,
        case_id=case_id,
        case=case,
        graded_case=graded,
        capture=capture,
        assessment=assessment,
        stop_reason=refs.stop_reason,
        notes=tuple(notes),
    )
