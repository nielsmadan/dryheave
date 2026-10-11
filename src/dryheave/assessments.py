import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

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
    DeterministicCriterion,
    FrozenCase,
    RubricCriterion,
    effective_workflow,
    load_frozen_case,
)
from dryheave.controller_models import RoleCall
from dryheave.controllers import CallContext
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import ScoringConfig
from dryheave.experiments import load_experiment, load_role
from dryheave.final_capture import CaptureBuilder, IgnoreSelection
from dryheave.grading import GradingContext, grade_criterion
from dryheave.integrity import (
    audit_capture,
    exclusions,
    invalid_audit,
    load_assessment,
)
from dryheave.journals import RunStore
from dryheave.judges import check_results, invoke_judge, judge_projection
from dryheave.models import JournalEvent, ObjectKind, TrialStage
from dryheave.native_recovery import reconcile_store_ownership
from dryheave.result_models import (
    Assessment,
    AssessmentEvidence,
    AuditReview,
    CheckResult,
    CriterionResult,
    HelpNeeded,
    MetricsResult,
    RegradeRecord,
    WorkflowOutcome,
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

REGRADE_STAGES = frozenset(
    {TrialStage.CAPTURED, TrialStage.AUDITED, TrialStage.GRADING, TrialStage.FINISHED}
)


@dataclass(frozen=True)
class GradedCase:
    case_id: str
    case: FrozenCase


def _has_judge(scoring: ScoringConfig | None) -> bool:
    judge = scoring.judge if scoring else None
    return judge is not None and judge.budget.max_calls > 0


def _judged(inputs: AssessmentInputs) -> bool:
    return _has_judge(inputs.scoring)


def _workflow_assist_call_ids(progress: WorkflowProgress | None) -> frozenset[str]:
    if progress is None:
        return frozenset()
    return frozenset(
        call_id
        for event in progress.events
        if (event.kind == "inspect" and event.outcome == "fix_requested")
        or (event.kind == "dialog" and event.outcome == "answered")
        for call_id in event.call_ids
    )


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


def _help_needed(capture: CapturedAttempt | None, case: FrozenCase | None) -> HelpNeeded | None:
    if capture is None or case is None:
        return None
    assisted = _workflow_assist_call_ids(capture.workflow)
    replies = tuple(
        call.decision
        for call in capture.controller_calls
        if call.role == "simulator"
        and call.decision is not None
        and call.decision.action == "reply"
        and call.call_id not in assisted
    )
    disclosed = tuple(dict.fromkeys(fact_id for reply in replies for fact_id in reply.fact_ids))
    kinds = {fact.fact_id: fact.disclosure for fact in case.allowed_facts}
    return HelpNeeded(
        simulator_replies=len(replies),
        facts_disclosed=disclosed,
        proactive_facts=tuple(
            fact_id for fact_id in disclosed if kinds.get(fact_id) == "proactive"
        ),
        on_request_facts=tuple(
            fact_id for fact_id in disclosed if kinds.get(fact_id) == "on_request"
        ),
    )


_INSPECTION_VERDICTS = frozenset({"passed", "failed", "unavailable", "reused"})


def _inspection_verdict(outcome: str) -> Literal["passed", "failed", "unavailable"]:
    if outcome == "passed":
        return "passed"
    if outcome in ("failed", "reused"):
        return "failed"
    if outcome == "unavailable":
        return "unavailable"
    raise ValueError(f"unexpected inspection outcome: {outcome}")


def _workflow_step_kinds(
    capture: CapturedAttempt | None, case: FrozenCase | None
) -> Mapping[str, Literal["prompt", "inspect"]]:
    if capture is None or case is None or capture.workflow is None or not capture.workflow.steps:
        return {}
    steps = effective_workflow(case, enabled=True, inspected=True)
    return {step.step_id: step.kind for step in steps}


def _workflow_outcome(
    progress: WorkflowProgress | None, step_kinds: Mapping[str, Literal["prompt", "inspect"]]
) -> WorkflowOutcome | None:
    if progress is None:
        return None
    inspections = [
        event
        for event in progress.events
        if event.kind == "inspect" and event.outcome in _INSPECTION_VERDICTS
    ]
    fix_requests = sum(
        1
        for event in progress.events
        if event.kind == "inspect" and event.outcome == "fix_requested"
    )
    dialogs_answered = sum(
        1 for event in progress.events if event.kind == "dialog" and event.outcome == "answered"
    )
    first_inspection: Literal["passed", "failed", "unavailable", "none"] = (
        _inspection_verdict(inspections[0].outcome) if inspections else "none"
    )
    last_inspect_step_id = next(
        (step_id for step_id in reversed(progress.steps) if step_kinds.get(step_id) == "inspect"),
        None,
    )
    final_inspection: Literal["passed", "failed", "unavailable", "not_reached", "none"]
    if last_inspect_step_id is None:
        final_inspection = "none"
    else:
        last_event = next(
            (event for event in reversed(inspections) if event.step_id == last_inspect_step_id),
            None,
        )
        final_inspection = _inspection_verdict(last_event.outcome) if last_event else "not_reached"
    inspection_seconds = sum(
        event.seconds
        for event in progress.events
        if event.kind == "inspect" and event.seconds is not None
    )
    return WorkflowOutcome(
        steps=len(progress.steps),
        completed_steps=progress.position,
        inspections=len(inspections),
        fix_requests=fix_requests,
        dialogs_answered=dialogs_answered,
        first_inspection=first_inspection,
        final_inspection=final_inspection,
        inspection_seconds=inspection_seconds,
        events=progress.events,
    )


def _initial(
    store: ObjectStore,
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    graded: FrozenCase | None,
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
        simulator_id=experiment.simulator_id
        if experiment
        else capture.simulator_id
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
        help_needed=_help_needed(capture, captured),
        workflow=_workflow_outcome(
            capture.workflow if capture else None, _workflow_step_kinds(capture, captured)
        ),
        roles=role_metrics(inputs, state, ()),
    )


def _completion(criteria: tuple[CriterionResult, ...]) -> str:
    required = [item.outcome for item in criteria if item.required]
    if not required or any(outcome in {"error", "not_run"} for outcome in required):
        return "indeterminate"
    return "fail" if "fail" in required else "pass"


def _saved_results(
    execution: ExecutionJournal, state: AttemptState
) -> tuple[dict[str, CriterionResult], bool, tuple[RoleCall, ...]]:
    results = {}
    for event in execution.events_for(state.attempt_id, "criterion-result"):
        result = parse_model(canonical_json(event.data), CriterionResult)
        results[result.criterion_id] = result
    calls = saved_judge_calls(execution, state)
    return results, bool(calls), calls


def _saved_checks(execution: ExecutionJournal, state: AttemptState) -> dict[str, CheckResult]:
    results = (
        parse_model(canonical_json(event.data), CheckResult)
        for event in execution.events_for(state.attempt_id, "check-result")
    )
    return {result.check_id: result for result in results}


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


def _judge(
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    root: Path,
    cancelled: Event,
) -> tuple[RoleCall, tuple[CriterionResult, ...], tuple[CheckResult, ...]]:
    if (
        inputs.case is None
        or inputs.capture is None
        or inputs.scoring is None
        or inputs.scoring.judge is None
    ):
        raise InputError("Judge requires verified case, capture and scoring inputs.")
    recipe = inputs.scoring.judge
    call_id = "j-" + uuid4().hex
    call = RoleCall(call_id=call_id, role="judge", status="intent")
    execution.evidence(state, "judge-intent", call)

    def own(identities: tuple[ProcessIdentity, ...]) -> None:
        execution.own(execution.attempts[state.attempt_id], identities)

    context = CallContext(
        call_id=call_id,
        index=0,
        deadline=time.monotonic() + min(recipe.budget.call_seconds, recipe.budget.total_seconds),
        cancelled=cancelled,
        inherited=os.environ,
        on_identities=own,
        runtime_root=execution.attempt_root(state) / "role-runtime" / call_id,
    )
    workspace = str(Path(state.workspace))
    call, results, answers, _ = invoke_judge(
        recipe,
        judge_projection(
            inputs.case,
            inputs.capture,
            inputs.files,
            roots=tuple(dict.fromkeys((workspace, os.path.realpath(workspace)))),
            limit=recipe.budget.max_input_bytes,
        ),
        ArtifactWriter(
            root / "judge" / call_id,
            recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes,
        ),
        context,
    )
    execution.evidence(state, "judge-result", call)
    if cancelled.is_set():
        raise KeyboardInterrupt
    return call, results, check_results(inputs.case.checks, answers)


def _grade(
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    store: ObjectStore,
    cancelled: Event,
    hidden_case_id: str | None,
) -> tuple[
    tuple[CriterionResult, ...],
    tuple[CheckResult, ...],
    tuple[MetricsResult, ...],
    tuple[RoleCall, ...],
]:
    if inputs.case is None or inputs.capture is None:
        return (), (), (), ()
    results, judge_intent, calls = _saved_results(execution, state)
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
    resumed_suites = {
        item.suite_id
        for item in inputs.case.criteria
        if isinstance(item, DeterministicCriterion)
        and item.suite_id
        and (item.criterion_id in results or item.criterion_id in intents)
    }
    for criterion in inputs.case.criteria:
        if cancelled.is_set():
            raise KeyboardInterrupt
        if criterion.criterion_id in results or not isinstance(criterion, DeterministicCriterion):
            continue
        if criterion.criterion_id in intents or criterion.suite_id in resumed_suites:
            result = CriterionResult(
                criterion_id=criterion.criterion_id,
                kind="deterministic",
                required=criterion.required,
                outcome="error",
                error="interrupted_verifier",
            )
        else:
            execution.record(state, "criterion-intent", {"criterion_id": criterion.criterion_id})
            result = grade_criterion(context, criterion)
        results[criterion.criterion_id] = result
        _save_criterion(execution, state, result)
    metrics = _measure(execution, state, inputs.case, context)
    criteria = graded_criteria(inputs.case, judged=_judged(inputs))
    judged, checks, calls = _judge_phase(
        execution, state, inputs, results, (judge_intent, calls), (root, cancelled)
    )
    graded = results | judged
    return tuple(graded[item.criterion_id] for item in criteria), checks, metrics, calls


def _judge_phase(
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    graded: Mapping[str, CriterionResult],
    saved: tuple[bool, tuple[RoleCall, ...]],
    runtime: tuple[Path, Event],
) -> tuple[dict[str, CriterionResult], tuple[CheckResult, ...], tuple[RoleCall, ...]]:
    judge_intent, calls = saved
    results: dict[str, CriterionResult] = {}
    if inputs.case is None:
        return results, (), calls
    criteria = graded_criteria(inputs.case, judged=_judged(inputs))
    case_checks = inputs.case.checks if _judged(inputs) else ()
    checks = _saved_checks(execution, state)
    rubrics = [
        item
        for item in criteria
        if isinstance(item, RubricCriterion) and item.criterion_id not in graded
    ]
    pending = [item for item in case_checks if item.check_id not in checks]
    recipe = inputs.scoring.judge if inputs.scoring else None
    delivered = inputs.capture is None or inputs.capture.accepted_turns > 0
    if (
        (rubrics or pending)
        and not judge_intent
        and delivered
        and recipe is not None
        and recipe.budget.max_calls
    ):
        call, judgments, answered = _judge(execution, state, inputs, runtime[0], runtime[1])
        calls = (*calls, call)
        for result in judgments:
            results[result.criterion_id] = result
            _save_criterion(execution, state, result)
        for check in answered:
            checks[check.check_id] = check
            execution.evidence(state, "check-result", check)
    for criterion in rubrics:
        if criterion.criterion_id not in results:
            result = CriterionResult(
                criterion_id=criterion.criterion_id,
                kind="judge",
                required=criterion.required,
                outcome="error" if judge_intent else "not_run",
                error="interrupted_judge"
                if judge_intent
                else "judge_not_available"
                if delivered
                else "subject_accepted_no_prompt",
            )
            results[criterion.criterion_id] = result
            _save_criterion(execution, state, result)
    for item in case_checks:
        if item.check_id not in checks:
            check = CheckResult(check_id=item.check_id, outcome="error")
            checks[item.check_id] = check
            execution.evidence(state, "check-result", check)
    return results, tuple(checks[item.check_id] for item in case_checks), calls


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
    initial = _initial(store, execution, state, inputs, case)
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
    criteria: tuple[CriterionResult, ...] = ()
    checks: tuple[CheckResult, ...] = ()
    metrics: tuple[MetricsResult, ...] = ()
    calls: tuple[RoleCall, ...] = ()
    if (
        initial.audit.input_integrity == "verified"
        and initial.audit.capture_integrity == "verified"
        and initial.audit.cleanup == "stopped"
        and initial.audit.capture_complete
    ):
        criteria, checks, metrics, calls = _grade(
            execution, state, grading, store, cancelled, graded.case_id if graded else None
        )
    elif case:
        criteria = tuple(
            CriterionResult(
                criterion_id=item.criterion_id,
                kind=item.kind,
                required=item.required,
                outcome="not_run",
                error="audit_exclusion",
            )
            for item in graded_criteria(case, judged=_judged(inputs))
        )
    _, _, saved_calls = _saved_results(execution, state)
    calls = calls or saved_calls
    if inputs.error is None:
        try:
            store.verify(initial.experiment_id)
        except DryheaveError as error:
            audit = invalid_audit(str(error), inputs.capture)
            initial = initial.model_copy(
                update={"audit": audit, "exclusion_reasons": exclusions(audit)}
            )
    reasons = initial.exclusion_reasons
    completion = _completion(criteria)
    if completion == "indeterminate":
        reasons = (*reasons, "unscorable")
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
            "criteria": criteria,
            "checks": checks,
            "metrics": metrics,
            "completion": completion,
            "deterministic_completion": _completion(
                tuple(item for item in criteria if item.kind == "deterministic")
            ),
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


def _needs_judge(case: FrozenCase) -> bool:
    return bool(case.checks) or any(isinstance(item, RubricCriterion) for item in case.criteria)


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
        if not _has_judge(scoring) and _needs_judge(graded.case if graded else original):
            raise InputError(
                "Regrade needs a judge for the case's rubric criteria or checks, "
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
