from typing import Literal

from pydantic import AwareDatetime, Field

from dryheave.capture_models import WorkspaceCapture
from dryheave.controller_models import DialogueMessage, RoleCall
from dryheave.drivers.models import (
    CleanupReport,
    LaunchObservation,
    Observation,
    ProcessIdentity,
    Submission,
    UsageObservation,
)
from dryheave.models import Name, ObjectId, RelativePath, RunId, StrictModel, TrialStage
from dryheave.profile_models import LaunchPlan


class RunOptions(StrictModel):
    mode: Literal["native", "offline-fixture"] = "native"
    strict: bool = False
    runtime_root: str | None = None
    transport: str | None = None


class AttemptState(StrictModel):
    attempt_id: Name
    trial_id: Name
    stage: TrialStage = TrialStage.RESERVED
    created_at: AwareDatetime
    workspace: str
    runtime: str | None = None
    terminal_session: str | None = None
    launch_plan: LaunchPlan | None = None
    launch_attempted: bool = False
    launch: LaunchObservation | None = None
    owned: tuple[ProcessIdentity, ...] = ()
    submission: Submission | None = None
    calls: tuple[RoleCall, ...] = ()
    capture_id: ObjectId | None = None
    assessment_id: ObjectId | None = None
    quarantine_id: ObjectId | None = None
    capture_error: str | None = None
    stop_reason: str | None = None


class CapturedAttempt(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    experiment_id: ObjectId
    trial_id: Name
    attempt_id: Name
    case_id: ObjectId
    profile_id: ObjectId
    persona_id: ObjectId
    simulator_id: ObjectId
    scoring_id: ObjectId
    mode: Literal["native", "offline-fixture"]
    isolation: Literal["repository-object"] = "repository-object"
    assessment: Literal["pending"] = "pending"
    subject_completion: Literal["unassessed"] = "unassessed"
    created_at: AwareDatetime
    captured_at: AwareDatetime
    setup_seconds: float | None = Field(default=None, ge=0)
    subject_seconds: float | None = Field(ge=0)
    interaction_coverage: Literal["observed", "partial"] = "observed"
    stop_reason: str
    last_observation: Observation | None
    accepted_turns: int = Field(ge=0)
    delivery_attempts: int = Field(ge=0)
    dialogue: tuple[DialogueMessage, ...]
    launch_plan: LaunchPlan | None
    launch: LaunchObservation | None
    cleanup: CleanupReport
    usage: UsageObservation
    controller_calls: tuple[RoleCall, ...]
    workspace: WorkspaceCapture
    evidence_files: dict[RelativePath, ObjectId]
    evidence_omissions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "Native execution isolates repository objects, not host access.",
        "Native telemetry and detached-process visibility are partial.",
        "Audit, eligibility, deterministic grading and price calculation are pending.",
    )


class QuarantinedCapture(StrictModel):
    schema_version: Literal[1] = 1
    input_integrity: Literal["unverified"] = "unverified"
    failure: str
    capture: CapturedAttempt


class RunSummary(StrictModel):
    run_id: RunId
    experiment_id: ObjectId
    options: RunOptions | None
    scheduled_trials: int | None
    input_error: str | None = None
    attempts: tuple[AttemptState, ...]
    unstarted_trials: tuple[Name, ...]
    pending_assessment: tuple[ObjectId, ...]
