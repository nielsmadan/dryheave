from contextlib import suppress
from dataclasses import dataclass, field

from dryheave.cases import FrozenCase, load_frozen_case
from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.drivers.models import NativeEvent
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import FrozenExperiment, ScoringConfig, TrialSpec
from dryheave.experiments import inspect_experiment, load_experiment, load_role
from dryheave.integrity import native_events
from dryheave.metrics import call_metrics, subject_metrics
from dryheave.models import ObjectKind
from dryheave.profiles import load_profile
from dryheave.result_models import RoleMetrics
from dryheave.runner import load_capture
from dryheave.runner_models import AttemptState, CapturedAttempt, QuarantinedCapture
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, parse_model
from dryheave.storage import ObjectStore


@dataclass
class AssessmentInputs:
    experiment: FrozenExperiment | None = None
    trial: TrialSpec | None = None
    case: FrozenCase | None = None
    capture: CapturedAttempt | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    scoring: ScoringConfig | None = None
    simulator: ControllerRecipe | None = None
    requested_model: str | None = None
    error: str | None = None


def load_assessment_inputs(
    store: ObjectStore, execution: ExecutionJournal, state: AttemptState
) -> AssessmentInputs:
    inputs = AssessmentInputs()
    try:
        identifier = state.capture_id or state.quarantine_id
        if identifier:
            evidence = store.read_evidence(identifier)
            inputs.files = evidence.files
            if evidence.manifest.kind == ObjectKind.QUARANTINE:
                quarantine = parse_model(
                    canonical_json(evidence.manifest.payload), QuarantinedCapture
                )
                inputs.capture = quarantine.capture
                inputs.error = quarantine.failure
            elif evidence.manifest.kind == ObjectKind.CAPTURE:
                inputs.capture = parse_model(
                    canonical_json(evidence.manifest.payload), CapturedAttempt
                )
                inputs.error = evidence.dependency_error
            else:
                raise InputError("Attempt evidence has an unexpected object kind.")
            if (
                inputs.capture.run_id,
                inputs.capture.attempt_id,
                inputs.capture.trial_id,
                inputs.capture.experiment_id,
            ) != (
                execution.journal.metadata.run_id,
                state.attempt_id,
                state.trial_id,
                execution.journal.metadata.experiment_id,
            ):
                inputs.capture = None
                raise InputError("Captured evidence identity differs from the durable attempt.")
        inputs.experiment = load_experiment(store, execution.journal.metadata.experiment_id)
        inputs.trial = next(
            item for item in inputs.experiment.trials if item.trial_id == state.trial_id
        )
        inputs.case = load_frozen_case(store, inputs.trial.case_id)
        inputs.scoring = load_role(
            store, inputs.experiment.scoring_id, ScoringConfig, ObjectKind.SCORING
        )
        inputs.simulator = load_role(
            store, inputs.experiment.simulator_id, ControllerRecipe, ObjectKind.SIMULATOR
        )
        inputs.requested_model = load_profile(store, inputs.trial.profile_id).recipe.model
        if state.capture_id:
            inputs.capture = load_capture(store, state.capture_id)
        elif inputs.error is None:
            inputs.error = "Stopped attempt has no validated final capture."
    except (DryheaveError, OSError, StopIteration) as error:
        inputs.error = str(error) or type(error).__name__
    if inputs.error:
        inputs.experiment, _ = inspect_experiment(store, execution.journal.metadata.experiment_id)
        if inputs.experiment:
            inputs.trial = next(
                (item for item in inputs.experiment.trials if item.trial_id == state.trial_id), None
            )
            with suppress(DryheaveError):
                inputs.scoring = load_role(
                    store, inputs.experiment.scoring_id, ScoringConfig, ObjectKind.SCORING
                )
            with suppress(DryheaveError):
                inputs.simulator = load_role(
                    store, inputs.experiment.simulator_id, ControllerRecipe, ObjectKind.SIMULATOR
                )
            if inputs.trial:
                with suppress(DryheaveError):
                    inputs.requested_model = load_profile(
                        store, inputs.trial.profile_id
                    ).recipe.model
    return inputs


def role_metrics(
    inputs: AssessmentInputs, state: AttemptState, judge_calls: tuple[RoleCall, ...]
) -> tuple[RoleMetrics, ...]:
    table = inputs.scoring.prices if inputs.scoring else None
    events: tuple[NativeEvent, ...] = ()
    with suppress(DryheaveError):
        events = native_events(inputs.files)
    return (
        subject_metrics(inputs.capture, table, events, inputs.requested_model),
        call_metrics(
            "simulator",
            inputs.capture.controller_calls if inputs.capture else state.calls,
            inputs.simulator,
            table,
        ),
        call_metrics("judge", judge_calls, inputs.scoring.judge if inputs.scoring else None, table),
    )


def saved_judge_calls(execution: ExecutionJournal, state: AttemptState) -> tuple[RoleCall, ...]:
    calls: dict[str, RoleCall] = {}
    for event in execution.journal.events:
        if event.attempt_id == state.attempt_id and event.event in {"judge-intent", "judge-result"}:
            call = parse_model(canonical_json(event.data), RoleCall)
            calls[call.call_id] = (
                call.model_copy(update={"status": "interrupted"})
                if call.status == "intent"
                else call
            )
    return tuple(calls.values())
