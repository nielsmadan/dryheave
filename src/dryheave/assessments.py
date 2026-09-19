import os
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import JsonValue

from dryheave.assessment_inputs import (
    AssessmentInputs,
    load_assessment_inputs,
    role_metrics,
    saved_judge_calls,
)
from dryheave.assessment_recovery import reconcile_grading
from dryheave.cases import (
    CapturePolicy,
    DeterministicCriterion,
    RubricCriterion,
)
from dryheave.controller_models import RoleCall
from dryheave.controllers import CallContext
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import DryheaveError, InputError
from dryheave.final_capture import CaptureBuilder, IgnoreSelection
from dryheave.grading import GradingContext, grade_criterion
from dryheave.integrity import (
    audit_capture,
    exclusions,
    invalid_audit,
    load_assessment,
)
from dryheave.journals import RunStore
from dryheave.judges import invoke_judge, judge_projection
from dryheave.models import ObjectKind, TrialStage
from dryheave.native_recovery import reconcile_store_ownership
from dryheave.result_models import (
    Assessment,
    AssessmentEvidence,
    AuditReview,
    CriterionResult,
)
from dryheave.runner import cancellation_signals, load_capture
from dryheave.runner_models import AttemptState
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore


def _initial(
    store: ObjectStore, execution: ExecutionJournal, state: AttemptState, inputs: AssessmentInputs
) -> Assessment:
    capture, trial, experiment = inputs.capture, inputs.trial, inputs.experiment
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
        criteria_id=digest(
            canonical_json(
                {"criteria": [item.model_dump(mode="json") for item in inputs.case.criteria]}
            )
        )
        if inputs.case
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
) -> tuple[RoleCall, tuple[CriterionResult, ...]]:
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

    def own(identity: ProcessIdentity) -> None:
        execution.own(execution.attempts[state.attempt_id], identity)

    context = CallContext(
        call_id=call_id,
        index=0,
        deadline=time.monotonic() + min(recipe.budget.call_seconds, recipe.budget.total_seconds),
        cancelled=cancelled,
        inherited=os.environ,
        on_identity=own,
        runtime_root=execution.attempt_root(state) / "role-runtime" / call_id,
    )
    call, results = invoke_judge(
        recipe,
        judge_projection(inputs.case, inputs.capture, inputs.files),
        ArtifactWriter(
            root / "judge" / call_id,
            recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes,
        ),
        context,
    )
    execution.evidence(state, "judge-result", call)
    if cancelled.is_set():
        raise KeyboardInterrupt
    return call, results


def _grade(
    execution: ExecutionJournal,
    state: AttemptState,
    inputs: AssessmentInputs,
    store: ObjectStore,
    cancelled: Event,
) -> tuple[tuple[CriterionResult, ...], tuple[RoleCall, ...]]:
    if inputs.case is None or inputs.capture is None:
        return (), ()
    results, judge_intent, calls = _saved_results(execution, state)
    root = execution.attempt_root(state) / "assessment" / uuid4().hex
    hidden = store.read_blobs(inputs.capture.case_id)

    def own(identity: ProcessIdentity) -> None:
        execution.own(execution.attempts[state.attempt_id], identity)

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
            execution.journal.append(
                "criterion-intent",
                {"criterion_id": criterion.criterion_id},
                trial_id=state.trial_id,
                attempt_id=state.attempt_id,
            )
            result = grade_criterion(context, criterion)
        results[criterion.criterion_id] = result
        _save_criterion(execution, state, result)
    rubrics = [
        item
        for item in inputs.case.criteria
        if isinstance(item, RubricCriterion) and item.criterion_id not in results
    ]
    recipe = inputs.scoring.judge if inputs.scoring else None
    if rubrics and not judge_intent and recipe is not None and recipe.budget.max_calls:
        call, judgments = _judge(execution, state, inputs, root, cancelled)
        calls = (*calls, call)
        for result in judgments:
            results[result.criterion_id] = result
            _save_criterion(execution, state, result)
    for criterion in rubrics:
        if criterion.criterion_id not in results:
            result = CriterionResult(
                criterion_id=criterion.criterion_id,
                kind="judge",
                required=criterion.required,
                outcome="error" if judge_intent else "not_run",
                error="interrupted_judge" if judge_intent else "judge_not_available",
            )
            results[criterion.criterion_id] = result
            _save_criterion(execution, state, result)
    return tuple(results[item.criterion_id] for item in inputs.case.criteria), calls


def _evidence(
    store: ObjectStore, execution: ExecutionJournal, state: AttemptState
) -> tuple[str | None, tuple[str, ...]]:
    root = execution.attempt_root(state) / "assessment"
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


def _assess_one(
    store: ObjectStore, execution: ExecutionJournal, state: AttemptState, cancelled: Event
) -> str:
    inputs = load_assessment_inputs(store, execution, state)
    initial = _initial(store, execution, state, inputs)
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
    calls: tuple[RoleCall, ...] = ()
    if (
        initial.audit.input_integrity == "verified"
        and initial.audit.capture_integrity == "verified"
        and initial.audit.cleanup == "stopped"
        and initial.audit.capture_complete
    ):
        criteria, calls = _grade(execution, state, inputs, store, cancelled)
    elif inputs.case:
        criteria = tuple(
            CriterionResult(
                criterion_id=item.criterion_id,
                kind=item.kind,
                required=item.required,
                outcome="not_run",
                error="audit_exclusion",
            )
            for item in inputs.case.criteria
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
