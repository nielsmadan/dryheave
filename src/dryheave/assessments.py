import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue

from dryheave.assessment_inputs import (
    AssessmentInputs,
    load_assessment_inputs,
    role_metrics,
    saved_judge_calls,
)
from dryheave.assessment_recovery import reconcile_grading
from dryheave.case_metrics import run_metrics
from dryheave.cases import (
    CapturePolicy,
    CaseContent,
    FrozenCase,
    TaskFact,
    fact_verifier,
    judged_facts,
    load_frozen_case,
    needs_judge,
    verifier_facts,
)
from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.controllers import CallContext
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import ScoringConfig
from dryheave.experiments import load_experiment, load_role
from dryheave.fact_metrics import (
    AttemptFacts,
    Exchange,
    attempt_outcome,
    attempt_reached,
    fact_account,
)
from dryheave.final_capture import CaptureBuilder, IgnoreSelection
from dryheave.grading import GradingContext, grade_fact
from dryheave.inspection import snapshot_digest, verifier_judgment
from dryheave.integrity import (
    audit_capture,
    exclusions,
    invalid_audit,
    load_assessment,
)
from dryheave.journals import RunStore
from dryheave.judges import (
    FactInspection,
    FactJudgment,
    FinalJudgeInput,
    RaiseEvent,
    fact_judge_recipe,
    final_projection,
    invoke_final_judge,
    invoke_judge,
    judge_fact,
    judge_projection,
)
from dryheave.models import JournalEvent, ObjectKind, StrictModel, TrialStage
from dryheave.native_recovery import reconcile_store_ownership
from dryheave.result_models import (
    FACT_EVIDENCE_CHARACTERS,
    MAX_UNCOVERED,
    Assessment,
    AssessmentEvidence,
    AuditReview,
    CriterionResult,
    FactAccount,
    FactResult,
    FinalInspection,
    InspectionVerdict,
    MetricsResult,
    RegradeRecord,
    WorkflowOutcome,
    verifier_verdict,
)
from dryheave.rubrics import (
    DEFAULT_RUBRIC_SET,
    graded_criteria,
    grading_identity,
    uses_default_rubrics,
)
from dryheave.runner import cancellation_signals, load_capture
from dryheave.runner_models import AttemptState, CapturedAttempt, WorkflowProgress
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore
from dryheave.workflow import (
    InspectionRecord,
    inspection_records,
    inspection_verdict,
    raise_events,
)

REGRADE_STAGES = frozenset(
    {TrialStage.CAPTURED, TrialStage.AUDITED, TrialStage.GRADING, TrialStage.FINISHED}
)
FACT_JUDGE_INTENT_EVENT = "fact-judge-intent"
FACT_GRADE_EVENT = "fact-grade"
RUBRIC_INTENT_EVENT = "rubric-intent"


class FactGrade(StrictModel):
    facts: tuple[FactResult, ...] = ()
    uncovered: tuple[str, ...] = Field(default=(), max_length=MAX_UNCOVERED)


@dataclass(frozen=True)
class GradedCase:
    case_id: str
    case: FrozenCase


@dataclass(frozen=True)
class JournalFacts:
    inspections: tuple[FactInspection, ...]
    records: tuple[InspectionRecord, ...]
    raises: tuple[RaiseEvent, ...]


@dataclass(frozen=True)
class JudgeRuntime:
    execution: ExecutionJournal
    state: AttemptState
    root: Path
    cancelled: Event


def _has_judge(scoring: ScoringConfig | None) -> bool:
    judge = scoring.judge if scoring else None
    return judge is not None and judge.budget.max_calls > 0


def _judged(inputs: AssessmentInputs) -> bool:
    return _has_judge(inputs.scoring)


def _journal_facts(execution: ExecutionJournal, attempt_id: str) -> JournalFacts:
    events = execution.journal.events
    records = inspection_records(events, attempt_id)
    return JournalFacts(
        inspections=tuple(record.result for record in records if record.result is not None),
        records=records,
        raises=raise_events(events, attempt_id),
    )


def _workflow_assist_call_ids(progress: WorkflowProgress | None) -> frozenset[str]:
    if progress is None:
        return frozenset()
    return frozenset(
        call_id
        for event in progress.events
        if event.kind == "inspect" and event.outcome == "fix_requested"
        for call_id in event.call_ids
    )


def _workflow_enabled(execution: ExecutionJournal) -> bool:
    return execution.options is not None and execution.options.workflow


def _capture_case(store: ObjectStore, inputs: AssessmentInputs) -> FrozenCase | None:
    capture = inputs.capture
    if capture is None:
        return None
    if (
        inputs.case is not None
        and inputs.trial is not None
        and inputs.trial.case_id == capture.case_id
    ):
        return inputs.case
    try:
        return load_frozen_case(store, capture.case_id)
    except (DryheaveError, OSError):
        return None


def stand_in_exchanges(
    capture: CapturedAttempt, raises: tuple[RaiseEvent, ...]
) -> tuple[Exchange, ...]:
    assisted = _workflow_assist_call_ids(capture.workflow)
    raised = {event.call_id: event for event in raises}
    calls = tuple(call for call in capture.controller_calls if call.role == "stand_in")
    known = {call.call_id for call in calls}
    exchanges = tuple(
        Exchange(raised=True, fact_ids=raised[call.call_id].fact_ids)
        if call.call_id in raised
        else Exchange(raised=False, fact_ids=call.decision.fact_ids if call.decision else ())
        for call in calls
        if call.call_id in raised
        or (
            call.status == "completed"
            and call.decision is not None
            and call.decision.action == "reply"
            and call.decision.screen_kind is None
            and call.call_id not in assisted
        )
    )
    return (
        *exchanges,
        *(
            Exchange(raised=True, fact_ids=event.fact_ids)
            for event in raises
            if event.call_id not in known
        ),
    )


def _attempt_facts(
    case: FrozenCase,
    capture: CapturedAttempt,
    journal: JournalFacts,
    results: tuple[FactResult, ...],
    *,
    workflow: bool,
) -> AttemptFacts:
    return AttemptFacts(
        case=case,
        results={item.fact_id: item for item in results},
        inspections=journal.inspections,
        raises=journal.raises,
        exchanges=stand_in_exchanges(capture, journal.raises),
        workflow=workflow,
    )


def _fact_account(
    case: FrozenCase | None,
    capture: CapturedAttempt | None,
    journal: JournalFacts,
    results: tuple[FactResult, ...],
    *,
    workflow: bool,
) -> FactAccount | None:
    if case is None or capture is None:
        return None
    return fact_account(_attempt_facts(case, capture, journal, results, workflow=workflow))


def _inspection_verdict(
    case: CaseContent, record: InspectionRecord, position: int
) -> Literal["passed", "failed", "unavailable", "unknown"]:
    return "unknown" if record.result is None else inspection_verdict(case, position, record.result)


def workflow_outcome(
    case: CaseContent | None,
    progress: WorkflowProgress | None,
    stop_reason: str,
    journal: JournalFacts,
    *,
    enabled: bool,
) -> WorkflowOutcome | None:
    if not enabled or case is None or progress is None:
        return None
    sent = tuple(event.turn for event in progress.events if event.kind == "prompt")
    verdicts = tuple(
        _inspection_verdict(case, record, sum(turn < record.turn for turn in sent))
        for record in journal.records
    )
    settled = tuple(verdict for verdict in verdicts if verdict != "unknown")
    first: InspectionVerdict = settled[0] if settled else "none"
    final: FinalInspection = verdicts[-1] if verdicts else "none"
    return WorkflowOutcome(
        steps=len(progress.steps),
        completed_steps=progress.position,
        inspections=len(settled),
        fix_requests=sum(event.cause == "fix_request" for event in journal.raises),
        fallbacks=sum(event.cause == "fallback" for event in journal.raises),
        dialogs_answered=sum(
            event.kind == "dialog" and event.outcome == "answered" for event in progress.events
        ),
        first_inspection=first,
        final_inspection=final,
        completed=stop_reason == "workflow_complete",
        inspection_seconds=sum(
            event.seconds
            for event in progress.events
            if event.kind == "inspect" and event.seconds is not None
        ),
        events=progress.events,
    )


def _initial(
    store: ObjectStore,
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    graded: FrozenCase | None,
    journal: JournalFacts,
) -> Assessment:
    capture, trial, experiment = inputs.capture, inputs.trial, inputs.experiment
    captured = _capture_case(store, inputs)
    if inputs.error is None and capture is not None and inputs.case is not None:
        try:
            audit = audit_capture(store, capture, inputs.case, inputs.files, Path(state.workspace))
        except (DryheaveError, OSError, UnicodeError) as error:
            inputs.error = str(error)
            audit = invalid_audit(str(error), capture)
    else:
        audit = invalid_audit(inputs.error or "Capture unavailable.", capture)
    return Assessment(
        run_id=execution.journal.metadata.run_id,
        experiment_id=execution.journal.metadata.experiment_id,
        trial_id=state.trial_id,
        attempt_id=state.attempt_id,
        capture_id=state.capture_id,
        quarantine_id=state.quarantine_id,
        case_id=trial.case_id if trial else capture.case_id if capture else None,
        profile_id=trial.profile_id if trial else capture.profile_id if capture else None,
        stand_in_id=experiment.stand_in_id
        if experiment
        else capture.stand_in_id
        if capture
        else None,
        scoring_id=experiment.scoring_id if experiment else capture.scoring_id if capture else None,
        criteria_id=grading_identity(graded, judged=_judged(inputs)) if graded else None,
        rubric_set=DEFAULT_RUBRIC_SET
        if graded and _judged(inputs) and uses_default_rubrics(graded)
        else None,
        variant=trial.variant if trial else None,
        repetition=trial.repetition if trial else None,
        seed=trial.seed if trial else None,
        mode=execution.options.mode if execution.options else "native",
        created_at=datetime.now(UTC),
        phase="audited",
        audit=audit,
        exclusion_reasons=exclusions(audit),
        stop_reason=capture.stop_reason if capture else state.stop_reason or "interrupted",
        subject_observation=capture.last_observation.state
        if capture and capture.last_observation
        else None,
        accepted_turns=capture.accepted_turns if capture else None,
        subject_seconds=capture.subject_seconds if capture else None,
        setup_seconds=capture.setup_seconds if capture else None,
        workflow=workflow_outcome(
            captured,
            capture.workflow if capture else None,
            capture.stop_reason if capture else "",
            journal,
            enabled=_workflow_enabled(execution),
        ),
        roles=role_metrics(inputs, state, ()),
    )


def _saved_results(
    execution: ExecutionJournal, state: AttemptState
) -> tuple[dict[str, CriterionResult], tuple[RoleCall, ...]]:
    results = {}
    for event in execution.events_for(state.attempt_id, "criterion-result"):
        result = parse_model(canonical_json(event.data), CriterionResult)
        results[result.criterion_id] = result
    return results, saved_judge_calls(execution, state)


def _measure(
    execution: ExecutionJournal, state: AttemptState, case: FrozenCase, context: GradingContext
) -> tuple[MetricsResult, ...]:
    saved = {
        result.metrics_id: result
        for result in (
            parse_model(canonical_json(event.data), MetricsResult)
            for event in execution.events_for(state.attempt_id, "metrics-result")
        )
    }
    intents = {
        event.data.get("metrics_id")
        for event in execution.events_for(state.attempt_id, "metrics-intent")
    }
    for command in case.metrics:
        if context.cancelled.is_set():
            raise KeyboardInterrupt
        if command.metrics_id in saved:
            continue
        if command.metrics_id in intents:
            result = MetricsResult(
                metrics_id=command.metrics_id, outcome="error", error="interrupted_metrics"
            )
        else:
            execution.record(state, "metrics-intent", {"metrics_id": command.metrics_id})
            result = run_metrics(context, command, os.environ)
        saved[command.metrics_id] = result
        execution.evidence(state, "metrics-result", result)
    return tuple(saved[command.metrics_id] for command in case.metrics)


def _save_criterion(
    execution: ExecutionJournal, state: AttemptState, result: CriterionResult
) -> None:
    execution.evidence(state, "criterion-result", result)


def _judge_call[T](
    runtime: JudgeRuntime,
    recipe: ControllerRecipe,
    index: int,
    invoke: Callable[[ArtifactWriter, CallContext], tuple[RoleCall, T]],
) -> tuple[RoleCall, T]:
    execution, state = runtime.execution, runtime.state
    call_id = "j-" + uuid4().hex
    execution.evidence(
        state, "judge-intent", RoleCall(call_id=call_id, role="judge", status="intent")
    )

    def own(identities: tuple[ProcessIdentity, ...]) -> None:
        execution.own(execution.attempts[state.attempt_id], identities)

    context = CallContext(
        call_id=call_id,
        index=index,
        deadline=time.monotonic() + min(recipe.budget.call_seconds, recipe.budget.total_seconds),
        cancelled=runtime.cancelled,
        inherited=os.environ,
        on_identities=own,
        runtime_root=execution.attempt_root(state) / "role-runtime" / call_id,
    )
    call, value = invoke(
        ArtifactWriter(
            runtime.root / "judge" / call_id,
            recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes,
        ),
        context,
    )
    execution.evidence(state, "judge-result", call)
    if runtime.cancelled.is_set():
        raise KeyboardInterrupt
    return call, value


def _roots(state: AttemptState) -> tuple[str, ...]:
    workspace = str(Path(state.workspace))
    return tuple(dict.fromkeys((workspace, os.path.realpath(workspace))))


def _judge(
    runtime: JudgeRuntime, inputs: AssessmentInputs, index: int
) -> tuple[RoleCall, tuple[CriterionResult, ...]]:
    if (
        inputs.case is None
        or inputs.capture is None
        or inputs.scoring is None
        or inputs.scoring.judge is None
    ):
        raise InputError("Judge requires verified case, capture and scoring inputs.")
    recipe = inputs.scoring.judge
    request = judge_projection(
        inputs.case,
        inputs.capture,
        inputs.files,
        roots=_roots(runtime.state),
        limit=recipe.budget.max_input_bytes,
    )

    def invoke(
        artifacts: ArtifactWriter, context: CallContext
    ) -> tuple[RoleCall, tuple[CriterionResult, ...]]:
        call, results, _ = invoke_judge(recipe, request, artifacts, context)
        return call, results

    return _judge_call(runtime, recipe, index, invoke)


type FinalAnswer = tuple[dict[str, FactJudgment], tuple[str, ...]]


def _ask_facts(
    runtime: JudgeRuntime,
    inputs: AssessmentInputs,
    facts: tuple[TaskFact, ...],
    index: int,
) -> tuple[RoleCall, FinalAnswer]:
    if inputs.case is None or inputs.capture is None or inputs.scoring is None:
        raise InputError("Fact grading requires verified case, capture and scoring inputs.")
    if inputs.scoring.judge is None:
        raise InputError("Fact grading requires a judge.")
    recipe = fact_judge_recipe(inputs.scoring.judge, len(facts))
    request: FinalJudgeInput = final_projection(
        inputs.case,
        inputs.capture,
        inputs.files,
        facts,
        roots=_roots(runtime.state),
        limit=recipe.budget.max_input_bytes,
    )

    def invoke(artifacts: ArtifactWriter, context: CallContext) -> tuple[RoleCall, FinalAnswer]:
        call, judgments, uncovered = invoke_final_judge(recipe, request, artifacts, context)
        settled = {item.fact_id: item for item in judgments} if call.status == "completed" else {}
        return call, (settled, uncovered)

    return _judge_call(runtime, recipe, index, invoke)


def _phase(fact: TaskFact) -> str:
    if fact.phase is None:
        raise InputError(f"Graded fact {fact.fact_id} has no phase.")
    return fact.phase


def _fact_result(
    fact: TaskFact, judgment: FactJudgment, source: Literal["verifier", "judge"]
) -> FactResult:
    return FactResult(
        fact_id=fact.fact_id,
        kind=fact.kind,
        phase=_phase(fact),
        verdict=judgment.verdict,
        observation=judgment.observation,
        evidence=judgment.evidence,
        source=source,
    )


def uncalibrated_verifiers(criteria: Iterable[CriterionResult]) -> tuple[str, ...]:
    return tuple(
        item.criterion_id
        for item in criteria
        if item.kind == "deterministic"
        and item.required
        and item.outcome == "error"
        and (
            item.calibration is None
            or item.calibration.reference is None
            or item.calibration.reference.outcome != "pass"
        )
    )


def _verifier_result(fact: TaskFact, result: CriterionResult) -> FactResult | None:
    if result.execution is None or verifier_verdict(result.execution) is None:
        return None
    return _fact_result(fact, verifier_judgment(fact, result.execution), "verifier")


def _last_inspection(inspections: tuple[FactInspection, ...]) -> FactInspection | None:
    return next(
        (item for item in reversed(inspections) if item.status == "completed"),
        None,
    )


def _comparable_inspection(
    inspection: FactInspection | None, original: FrozenCase | None, graded: FrozenCase | None
) -> FactInspection | None:
    if inspection is None or original is None or graded is None:
        return inspection
    before = {fact.fact_id: judge_fact(fact) for fact in original.allowed_facts}
    after = {fact.fact_id: judge_fact(fact) for fact in graded.allowed_facts}
    kept = tuple(
        item
        for item in inspection.judgments
        if item.fact_id in before and before[item.fact_id] == after.get(item.fact_id)
    )
    return inspection.model_copy(update={"judgments": kept})


def _disagreements(
    facts: tuple[TaskFact, ...],
    judged: Mapping[str, FactJudgment],
    inspected: Mapping[str, str],
) -> tuple[TaskFact, ...]:
    settled = {"met", "unmet"}
    return tuple(
        fact
        for fact in facts
        if fact.fact_id in judged
        and judged[fact.fact_id].verdict in settled
        and inspected.get(fact.fact_id) in settled
        and inspected[fact.fact_id] != judged[fact.fact_id].verdict
    )


def _disputed(
    fact: TaskFact, judgment: FactJudgment, inspected: str, *, rejudged: bool
) -> FactResult:
    skipped = "" if rejudged else "re-judge skipped, judge call budget spent; "
    evidence = (
        f"{skipped}final judge {judgment.verdict}, last inspection {inspected}: {judgment.evidence}"
    )
    return _fact_result(fact, judgment, "judge").model_copy(
        update={"verdict": "disputed", "evidence": evidence[:FACT_EVIDENCE_CHARACTERS]}
    )


def _judge_facts(
    runtime: JudgeRuntime,
    inputs: AssessmentInputs,
    facts: tuple[TaskFact, ...],
    inspection: FactInspection | None,
) -> tuple[FactGrade, tuple[RoleCall, ...]]:
    recipe = inputs.scoring.judge if inputs.scoring else None
    calls: list[RoleCall] = []

    def ask(subset: tuple[TaskFact, ...]) -> FinalAnswer:
        if recipe is None or len(calls) >= recipe.budget.max_calls:
            return {}, ()
        call, answer = _ask_facts(runtime, inputs, subset, len(calls))
        calls.append(call)
        return answer

    judged, uncovered = ask(facts)
    missing = tuple(fact for fact in facts if fact.fact_id not in judged)
    if missing:
        more, extra = ask(missing)
        judged, uncovered = judged | more, (*uncovered, *extra)
    inspected = (
        {item.fact_id: item.verdict for item in inspection.judgments}
        if inspection is not None
        and inputs.capture is not None
        and inspection.snapshot_digest == snapshot_digest(inputs.capture.workspace)
        else {}
    )
    contested = _disagreements(facts, judged, inspected)
    rejudged = recipe is not None and len(calls) < recipe.budget.max_calls
    again, extra = ask(contested) if contested else ({}, ())
    agreed = {
        fact.fact_id: again[fact.fact_id]
        for fact in contested
        if fact.fact_id in again and again[fact.fact_id].verdict == inspected[fact.fact_id]
    }
    results = tuple(
        _fact_result(fact, agreed.get(fact.fact_id, judged[fact.fact_id]), "judge")
        if fact not in contested or fact.fact_id in agreed
        else _disputed(
            fact,
            again.get(fact.fact_id, judged[fact.fact_id]),
            inspected[fact.fact_id],
            rejudged=rejudged,
        )
        for fact in facts
        if fact.fact_id in judged
    )
    return (
        FactGrade(
            facts=results,
            uncovered=tuple(dict.fromkeys((*uncovered, *extra)))[:MAX_UNCOVERED],
        ),
        tuple(calls),
    )


def _fact_phase(
    runtime: JudgeRuntime,
    inputs: AssessmentInputs,
    inspection: FactInspection | None,
) -> tuple[FactGrade, tuple[RoleCall, ...]]:
    execution, state = runtime.execution, runtime.state
    saved = execution.events_for(state.attempt_id, FACT_GRADE_EVENT)
    if saved:
        return parse_model(canonical_json(saved[-1].data), FactGrade), ()
    facts = judged_facts(inputs.case) if inputs.case else ()
    delivered = inputs.capture is not None and inputs.capture.accepted_turns > 0
    if (
        not facts
        or not delivered
        or not _judged(inputs)
        or execution.events_for(state.attempt_id, FACT_JUDGE_INTENT_EVENT)
    ):
        grade = FactGrade()
        calls: tuple[RoleCall, ...] = ()
    else:
        execution.record(
            state, FACT_JUDGE_INTENT_EVENT, {"facts": [fact.fact_id for fact in facts]}
        )
        grade, calls = _judge_facts(runtime, inputs, facts, inspection)
    execution.evidence(state, FACT_GRADE_EVENT, grade)
    return grade, calls


def _grade(  # noqa: PLR0913
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    store: ObjectStore,
    cancelled: Event,
    hidden_case_id: str | None,
    *,
    inspection: FactInspection | None,
) -> tuple[FactGrade, tuple[CriterionResult, ...], tuple[MetricsResult, ...], tuple[RoleCall, ...]]:
    if inputs.case is None or inputs.capture is None:
        return FactGrade(), (), (), ()
    results, calls = _saved_results(execution, state)
    root = execution.assessment_root(state) / uuid4().hex
    hidden = store.read_blobs(hidden_case_id or inputs.capture.case_id)

    def own(identities: tuple[ProcessIdentity, ...]) -> None:
        execution.own(execution.attempts[state.attempt_id], identities)

    context = GradingContext(
        store,
        inputs.case,
        inputs.files | hidden,
        root,
        Path(state.workspace),
        own,
        cancelled,
        inputs.capture,
    )
    intents = {
        event.data.get("criterion_id")
        for event in execution.events_for(state.attempt_id, "criterion-intent")
    }
    facts = verifier_facts(inputs.case)
    resumed_suites = {
        fact_verifier(fact).suite_id
        for fact in facts
        if fact_verifier(fact).suite_id and (fact.fact_id in results or fact.fact_id in intents)
    }
    for fact in facts:
        if cancelled.is_set():
            raise KeyboardInterrupt
        if fact.fact_id in results:
            continue
        if fact.fact_id in intents or fact_verifier(fact).suite_id in resumed_suites:
            result = CriterionResult(
                criterion_id=fact.fact_id,
                kind="deterministic",
                required=fact.kind == "requirement",
                outcome="error",
                error="interrupted_verifier",
            )
        else:
            execution.record(state, "criterion-intent", {"criterion_id": fact.fact_id})
            result = grade_fact(context, fact)
        results[fact.fact_id] = result
        _save_criterion(execution, state, result)
    metrics = _measure(execution, state, inputs.case, context)
    runtime = JudgeRuntime(execution, state, root, cancelled)
    judged, fact_calls = _fact_phase(runtime, inputs, inspection)
    verified = tuple(
        settled
        for fact in facts
        if (settled := _verifier_result(fact, results[fact.fact_id])) is not None
    )
    by_id = {item.fact_id: item for item in (*verified, *judged.facts)}
    grade = FactGrade(
        facts=tuple(
            by_id[fact.fact_id] for fact in inputs.case.allowed_facts if fact.fact_id in by_id
        ),
        uncovered=judged.uncovered,
    )
    rubrics, rubric_calls = _judge_phase(runtime, inputs, (*calls, *fact_calls))
    criteria = graded_criteria(inputs.case, judged=_judged(inputs))
    return (
        grade,
        (
            *(results[fact.fact_id] for fact in facts),
            *(rubrics[item.criterion_id] for item in criteria),
        ),
        metrics,
        rubric_calls,
    )


def _judge_phase(
    runtime: JudgeRuntime, inputs: AssessmentInputs, calls: tuple[RoleCall, ...]
) -> tuple[dict[str, CriterionResult], tuple[RoleCall, ...]]:
    execution, state = runtime.execution, runtime.state
    saved = {
        result.criterion_id: result
        for result in _saved_results(execution, state)[0].values()
        if result.kind == "judge"
    }
    results: dict[str, CriterionResult] = {}
    if inputs.case is None:
        return results, calls
    criteria = graded_criteria(inputs.case, judged=_judged(inputs))
    rubrics = [item for item in criteria if item.criterion_id not in saved]
    started = bool(execution.events_for(state.attempt_id, RUBRIC_INTENT_EVENT))
    recipe = inputs.scoring.judge if inputs.scoring else None
    delivered = inputs.capture is None or inputs.capture.accepted_turns > 0
    if (
        rubrics
        and not started
        and delivered
        and recipe is not None
        and len(calls) < recipe.budget.max_calls
    ):
        execution.record(
            state, RUBRIC_INTENT_EVENT, {"criteria": [item.criterion_id for item in rubrics]}
        )
        started = True
        call, judgments = _judge(runtime, inputs, len(calls))
        calls = (*calls, call)
        for result in judgments:
            results[result.criterion_id] = result
            _save_criterion(execution, state, result)
    for criterion in rubrics:
        if criterion.criterion_id not in results:
            result = CriterionResult(
                criterion_id=criterion.criterion_id,
                kind="judge",
                required=False,
                outcome="error" if started else "not_run",
                error="interrupted_judge"
                if started
                else "subject_accepted_no_prompt"
                if not delivered
                else "judge_budget_exhausted"
                if recipe is not None and len(calls) >= recipe.budget.max_calls
                else "judge_not_available",
            )
            results[criterion.criterion_id] = result
            _save_criterion(execution, state, result)
    return saved | results, calls


def _evidence(
    store: ObjectStore, execution: ExecutionJournal, state: AttemptState
) -> tuple[str | None, tuple[str, ...]]:
    root = execution.assessment_root(state)
    if not root.exists():
        return None, ()
    builder = CaptureBuilder(
        CapturePolicy(max_bytes=128 * 1024 * 1024, max_files=30000, max_depth=32)
    )

    def select(paths: dict[str, int]) -> IgnoreSelection:
        return IgnoreSelection(
            omitted={path for path in paths if Path(path).name in {"workspace", "hidden"}}
        )

    for path, mode in builder.walk(root, ignored=select).items():
        if "-evidence/" in path or "/evidence/" in path or "/judge/" in path:
            builder.take(root, path, mode, prefix="assessment")
    omissions = tuple(item.reason for item in builder.omissions if not item.intentional)
    model = AssessmentEvidence(
        run_id=execution.journal.metadata.run_id,
        attempt_id=state.attempt_id,
        files={name: digest(content) for name, content in builder.blobs.items()},
        omissions=omissions,
    )
    return store.put(ObjectKind.ASSESSMENT_EVIDENCE, model, files=builder.blobs), omissions


def _carried_reviews(
    store: ObjectStore, previous_id: str | None, initial: Assessment
) -> Assessment:
    if previous_id is None:
        return initial
    findings = {item.finding_id for item in initial.audit.findings}
    reviews = tuple(
        item for item in load_assessment(store, previous_id).reviews if item.finding_id in findings
    )
    return initial.model_copy(
        update={"reviews": reviews, "exclusion_reasons": exclusions(initial.audit, reviews)}
    )


def _assess_one(
    store: ObjectStore,
    execution: ExecutionJournal,
    state: AttemptState,
    cancelled: Event,
    graded: GradedCase | None = None,
) -> str:
    inputs = load_assessment_inputs(store, execution, state)
    case = graded.case if graded and inputs.case else inputs.case
    journal = _journal_facts(execution, state.attempt_id)
    initial = _initial(store, execution, state, inputs, case, journal)
    if execution.regrade_id is not None:
        initial = _carried_reviews(store, state.assessment_id, initial)
    if graded is not None:
        initial = initial.model_copy(update={"graded_case_id": graded.case_id})
    grading = replace(inputs, case=case)
    if state.stage == TrialStage.STOPPING:
        execution.save(
            state.model_copy(
                update={
                    "stage": TrialStage.CAPTURED,
                    "capture_error": inputs.error or "capture_unavailable",
                }
            )
        )
        state = execution.attempts[state.attempt_id]
    if state.stage == TrialStage.CAPTURED:
        audit_id = store.put(ObjectKind.RESULT, initial)
        execution.save(
            state.model_copy(update={"stage": TrialStage.AUDITED, "assessment_id": audit_id})
        )
        state = execution.attempts[state.attempt_id]
    if state.stage == TrialStage.AUDITED:
        execution.save(state.model_copy(update={"stage": TrialStage.GRADING}))
        state = execution.attempts[state.attempt_id]
    grade = FactGrade()
    criteria: tuple[CriterionResult, ...] = ()
    metrics: tuple[MetricsResult, ...] = ()
    calls: tuple[RoleCall, ...] = ()
    if (
        initial.audit.input_integrity == "verified"
        and initial.audit.capture_integrity == "verified"
        and initial.audit.cleanup == "stopped"
        and initial.audit.capture_complete
    ):
        grade, criteria, metrics, calls = _grade(
            execution,
            state,
            grading,
            store,
            cancelled,
            graded.case_id if graded else None,
            inspection=_comparable_inspection(
                _last_inspection(journal.inspections), inputs.case, case
            ),
        )
    elif case:
        criteria = (
            *(
                CriterionResult(
                    criterion_id=fact.fact_id,
                    kind="deterministic",
                    required=fact.kind == "requirement",
                    outcome="not_run",
                    error="audit_exclusion",
                )
                for fact in verifier_facts(case)
            ),
            *(
                CriterionResult(
                    criterion_id=item.criterion_id,
                    kind="judge",
                    required=False,
                    outcome="not_run",
                    error="audit_exclusion",
                )
                for item in graded_criteria(case, judged=_judged(inputs))
            ),
        )
    _, saved_calls = _saved_results(execution, state)
    calls = calls or saved_calls
    if inputs.error is None:
        try:
            store.verify(initial.experiment_id)
        except DryheaveError as error:
            audit = invalid_audit(str(error), inputs.capture)
            initial = initial.model_copy(
                update={"audit": audit, "exclusion_reasons": exclusions(audit)}
            )
    workflow = _workflow_enabled(execution)
    account = _fact_account(case, inputs.capture, journal, grade.facts, workflow=workflow)
    outcome, reason = (
        attempt_outcome(
            case,
            {item.fact_id: item for item in grade.facts},
            attempt_reached(
                _attempt_facts(case, inputs.capture, journal, grade.facts, workflow=workflow)
            ),
            initial.stop_reason,
        )
        if case is not None and inputs.capture is not None
        else ("indeterminate", "grading_error")
    )
    reasons = initial.exclusion_reasons
    if outcome == "indeterminate":
        reasons = (*reasons, "unscorable")
    if uncalibrated_verifiers(criteria):
        reasons = (*reasons, "verifier_uncalibrated")
    if any(
        call.cleanup and (not call.cleanup.known_writers_stopped or call.cleanup.errors)
        for call in calls
    ):
        reasons = (*reasons, "judge_cleanup_unresolved")
    reconcile_grading(execution, execution.attempts[state.attempt_id])
    evidence_id, evidence_omissions = _evidence(store, execution, state)
    if evidence_omissions:
        reasons = (*reasons, "assessment_evidence_incomplete")
    result = initial.model_copy(
        update={
            "phase": "finished",
            "facts": grade.facts,
            "uncovered": grade.uncovered,
            "criteria": criteria,
            "metrics": metrics,
            "outcome": outcome,
            "outcome_reason": reason,
            "fact_account": account,
            "exclusion_reasons": tuple(dict.fromkeys(reasons)),
            "eligible": not reasons,
            "roles": role_metrics(inputs, state, calls),
            "evidence_id": evidence_id,
            "evidence_complete": not evidence_omissions,
            "previous_id": state.assessment_id,
            "regrade_id": execution.regrade_id,
        }
    )
    identifier = store.put(ObjectKind.RESULT, result)
    load_assessment(store, identifier)
    execution.save(
        execution.attempts[state.attempt_id].model_copy(
            update={"stage": TrialStage.FINISHED, "assessment_id": identifier}
        )
    )
    return identifier


def assess_run(
    store: ObjectStore, run_id: str, *, cancelled: Event | None = None
) -> tuple[str, ...]:
    identifiers = []
    cancelled = cancelled or Event()
    with (
        RunStore(store.root).open(run_id) as journal,
        store.native_lock(),
        cancellation_signals(cancelled),
    ):
        unfinished = _open_regrade(journal.events)
        if unfinished is not None:
            raise InputError(
                f"Regrade {unfinished.regrade_id} is unfinished; resume it before assessing with: "
                + _regrade_command(run_id, unfinished.case_id)
            )
        execution = ExecutionJournal(journal)
        reconcile_store_ownership(store, execution)
        for state in tuple(execution.attempts.values()):
            if cancelled.is_set():
                raise KeyboardInterrupt
            if state.stage == TrialStage.FINISHED:
                if state.assessment_id:
                    identifiers.append(state.assessment_id)
                continue
            if state.stage not in {
                TrialStage.CAPTURED,
                TrialStage.AUDITED,
                TrialStage.GRADING,
                TrialStage.STOPPING,
            }:
                raise InputError(
                    "Resume execution to reconcile active subject ownership before assessment."
                )
            if state.stage == TrialStage.GRADING:
                reconcile_grading(execution, state)
            try:
                identifiers.append(_assess_one(store, execution, state, cancelled))
            finally:
                current = execution.attempts[state.attempt_id]
                if current.stage == TrialStage.GRADING:
                    reconcile_grading(execution, current)
        result_ids: list[JsonValue] = list(identifiers)
        journal.write_result({"assessments": result_ids})
    return tuple(identifiers)


def _open_regrade(events: tuple[JournalEvent, ...]) -> RegradeRecord | None:
    started: dict[str, RegradeRecord] = {}
    for event in events:
        if event.event in {"regrade-start", "regrade-finished"}:
            record = parse_model(canonical_json(event.data), RegradeRecord)
            if event.event == "regrade-start":
                started[record.regrade_id] = record
            else:
                started.pop(record.regrade_id, None)
    return next(reversed(started.values()), None)


def _regraded(events: tuple[JournalEvent, ...], regrade_id: str) -> set[str]:
    return {
        event.attempt_id
        for event in events
        if event.event == "regrade-assessed"
        and event.attempt_id
        and event.data.get("regrade_id") == regrade_id
    }


def _regrade_command(run_id: str, case_id: str | None) -> str:
    return f"dryheave assess {run_id} --regrade" + (f" --case {case_id}" if case_id else "")


def _validate_regrade(
    store: ObjectStore,
    experiment_id: str,
    states: tuple[AttemptState, ...],
    graded: GradedCase | None,
) -> None:
    experiment = load_experiment(store, experiment_id)
    scoring = load_role(store, experiment.scoring_id, ScoringConfig, ObjectKind.SCORING)
    trials = {item.trial_id: item.case_id for item in experiment.trials}
    for case_id in sorted({trials[state.trial_id] for state in states if state.trial_id in trials}):
        original = load_frozen_case(store, case_id)
        if graded is not None and (graded.case.repository_id, graded.case.initial_prompt) != (
            original.repository_id,
            original.initial_prompt,
        ):
            raise InputError(
                "A replacement case must share the original case's repository_id and initial_prompt."
            )
        if not _has_judge(scoring) and needs_judge(graded.case if graded else original):
            raise InputError(
                "Regrade needs a judge for the case's rubric criteria or judged facts, "
                "but the experiment's scoring has none."
            )


def _regrade_one(
    store: ObjectStore,
    execution: ExecutionJournal,
    state: AttemptState,
    cancelled: Event,
    graded: GradedCase | None,
) -> str:
    if state.stage == TrialStage.GRADING:
        reconcile_grading(execution, state)
    try:
        return _assess_one(store, execution, state, cancelled, graded)
    except BaseException:
        reconcile_grading(execution, execution.attempts[state.attempt_id])
        raise


def regrade_run(
    store: ObjectStore,
    run_id: str,
    *,
    case: str | None = None,
    cancelled: Event | None = None,
) -> tuple[str, tuple[str, ...]]:
    cancelled = cancelled or Event()
    case_id = store.resolve(case) if case else None
    identifiers: list[str] = []
    with (
        RunStore(store.root).open(run_id) as journal,
        store.native_lock(),
        cancellation_signals(cancelled),
    ):
        unfinished = _open_regrade(journal.events)
        if unfinished is not None and unfinished.case_id != case_id:
            raise InputError(
                f"Regrade {unfinished.regrade_id} is unfinished; resume it with: "
                + _regrade_command(run_id, unfinished.case_id)
            )
        regrade_id = unfinished.regrade_id if unfinished else "r-" + uuid4().hex
        execution = ExecutionJournal(journal, regrade_id=regrade_id)
        reconcile_store_ownership(store, execution)
        graded = GradedCase(case_id, load_frozen_case(store, case_id)) if case_id else None
        states = tuple(
            state
            for state in execution.attempts.values()
            if state.capture_id and state.stage in REGRADE_STAGES
        )
        _validate_regrade(store, journal.metadata.experiment_id, states, graded)
        if unfinished is None:
            record = RegradeRecord(regrade_id=regrade_id, case_id=case_id)
            journal.append("regrade-start", record.model_dump(mode="json"))
        done = _regraded(journal.events, regrade_id)

        def mark_regraded(state: AttemptState, assessment_id: str) -> None:
            journal.append(
                "regrade-assessed",
                RegradeRecord(regrade_id=regrade_id, assessment_id=assessment_id).model_dump(
                    mode="json"
                ),
                trial_id=state.trial_id,
                attempt_id=state.attempt_id,
            )

        for state in states:
            if cancelled.is_set():
                raise KeyboardInterrupt
            if state.attempt_id in done and state.assessment_id:
                identifiers.append(state.assessment_id)
                continue
            if (
                state.assessment_id
                and load_assessment(store, state.assessment_id).regrade_id == regrade_id
            ):
                mark_regraded(state, state.assessment_id)
                identifiers.append(state.assessment_id)
                continue
            identifier = _regrade_one(store, execution, state, cancelled, graded)
            mark_regraded(state, identifier)
            identifiers.append(identifier)
        journal.append(
            "regrade-finished", RegradeRecord(regrade_id=regrade_id).model_dump(mode="json")
        )
        result_ids: list[JsonValue] = list(identifiers)
        journal.write_result({"assessments": result_ids, "regrade_id": regrade_id})
    return regrade_id, tuple(identifiers)


def review_assessment(store: ObjectStore, run_id: str, attempt_id: str, review: AuditReview) -> str:
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        state = execution.attempts.get(attempt_id)
        if state is None or state.assessment_id is None or state.stage != TrialStage.FINISHED:
            raise InputError("Review requires a finished retained assessment.")
        previous = load_assessment(store, state.assessment_id)
        selected = next(
            (item for item in previous.audit.findings if item.finding_id == review.finding_id), None
        )
        if selected is None or selected.confidence != "suspected":
            raise InputError(
                "Only suspected access findings can receive a review; confirmed corruption cannot be overridden."
            )
        store.verify(previous.experiment_id)
        if previous.capture_id is None:
            raise InputError("Review requires the exact verified raw capture.")
        load_capture(store, previous.capture_id)
        reviews = (*previous.reviews, review)
        audit_reasons = set(exclusions(previous.audit, previous.reviews))
        retained = tuple(
            reason for reason in previous.exclusion_reasons if reason not in audit_reasons
        )
        reasons = (*exclusions(previous.audit, reviews), *retained)
        result = previous.model_copy(
            update={
                "reviews": reviews,
                "exclusion_reasons": reasons,
                "eligible": not reasons,
                "previous_id": state.assessment_id,
            }
        )
        identifier = store.put(ObjectKind.RESULT, result)
        execution.evidence(state, "audit-review", review)
        execution.save(state.model_copy(update={"assessment_id": identifier}))
        return identifier
