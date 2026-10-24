from typing import Literal

from pydantic import AwareDatetime

from dryheave.calibration_models import CalibrationStatus
from dryheave.cases import CaseBudgets, FactKind
from dryheave.models import (
    CommandSpec,
    Name,
    NonnegativeInt,
    ObjectId,
    PositiveInt,
    RelativePath,
    RunId,
    StrictModel,
)
from dryheave.transcript_models import ReviewLanes

DetailStatus = Literal["ok", "unavailable", "invalid", "omitted", "refused"]
ContentEncoding = Literal["utf-8", "base64"]
Verification = Literal["verified", "failed", "unverified"]


class ViewerTarget(StrictModel):
    kind: Literal["run", "report"]
    id: str


class SessionView(StrictModel):
    target: ViewerTarget | None


class LabelIndex(StrictModel):
    labels: dict[ObjectId, tuple[Name, ...]]


class RunRow(StrictModel):
    run_id: RunId
    error: str | None = None
    experiment_id: ObjectId | None = None
    created_at: AwareDatetime | None = None
    durable_sequence: NonnegativeInt | None = None
    observed_events: NonnegativeInt | None = None
    truncated: bool = False
    counted_through: NonnegativeInt | None = None
    mode: str | None = None
    attempts: NonnegativeInt | None = None
    finished: NonnegativeInt | None = None


class RunListing(StrictModel):
    runs: tuple[RunRow, ...]
    offset: NonnegativeInt
    limit: PositiveInt
    total: NonnegativeInt


class AttemptProgress(StrictModel):
    attempt_id: Name
    trial_id: Name
    stage: str
    capture_id: ObjectId | None
    quarantine_id: ObjectId | None
    assessment_id: ObjectId | None


class RunProgress(StrictModel):
    run_id: RunId
    experiment_id: ObjectId
    created_at: AwareDatetime
    durable_sequence: NonnegativeInt
    mode: str | None
    attempts: tuple[AttemptProgress, ...]


class CaptureOmissionView(StrictModel):
    path: str
    reason: str
    intentional: bool


class DialogueLine(StrictModel):
    role: Literal["user", "assistant"]
    text: str


class DialogueView(StrictModel):
    attempt_id: Name
    capture_id: ObjectId | None
    quarantine_id: ObjectId | None
    quarantined: bool
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    interaction_coverage: Literal["observed", "partial"] | None = None
    capture_errors: tuple[str, ...] = ()
    evidence_omissions: tuple[str, ...] = ()
    messages: tuple[DialogueLine, ...] = ()


class PatchView(StrictModel):
    attempt_id: Name
    capture_id: ObjectId | None
    quarantine_id: ObjectId | None
    quarantined: bool
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    name: RelativePath | None = None
    workspace_complete: bool | None = None
    omissions: tuple[CaptureOmissionView, ...] = ()
    limit_bytes: NonnegativeInt | None = None
    bytes: NonnegativeInt | None = None
    sha256: ObjectId | None = None
    encoding: ContentEncoding | None = None
    content: str | None = None


class EvidenceListing(StrictModel):
    attempt_id: Name
    assessment_id: ObjectId | None
    evidence_id: ObjectId | None
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    files: tuple[str, ...] = ()
    omissions: tuple[str, ...] = ()
    complete: bool | None = None


class EvidenceFileView(StrictModel):
    attempt_id: Name
    assessment_id: ObjectId | None
    evidence_id: ObjectId | None
    name: RelativePath
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    limit_bytes: NonnegativeInt | None = None
    bytes: NonnegativeInt | None = None
    sha256: ObjectId | None = None
    encoding: ContentEncoding | None = None
    content: str | None = None


class CalibrationEntry(StrictModel):
    calibration_id: ObjectId
    case_id: ObjectId
    verification: Verification
    reason: str | None = None
    reason_code: str | None = None
    status: CalibrationStatus | None = None
    created_at: AwareDatetime | None = None
    evidence_complete: bool | None = None
    omissions: tuple[str, ...] = ()


class CalibrationScan(StrictModel):
    complete: bool
    scanned: NonnegativeInt
    unreadable: NonnegativeInt
    attempted: NonnegativeInt
    verified: NonnegativeInt
    next_cursor: ObjectId | None = None


class CalibrationView(StrictModel):
    run_id: RunId
    experiment_id: ObjectId
    input_error: str | None
    calibrations: dict[ObjectId, tuple[CalibrationEntry, ...]]
    scan: CalibrationScan


class CaseCalibrationView(StrictModel):
    case_id: ObjectId
    calibrations: tuple[CalibrationEntry, ...]
    scan: CalibrationScan


class CriterionReasoning(StrictModel):
    criterion_id: Name
    rationale: str | None = None


class JudgeReasoningView(StrictModel):
    attempt_id: Name
    assessment_id: ObjectId | None
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    criteria: tuple[CriterionReasoning, ...] = ()


class CaseFactView(StrictModel):
    fact_id: Name
    kind: FactKind
    phase: Name | None
    disclosure: Literal["on_request", "proactive"]
    text: str
    check: str | None = None
    verifier: CommandSpec | None = None


class CasePromptStepView(StrictModel):
    step_id: Name
    after_phase: Name
    prompt: str
    slash: bool = False


class CaseRubricView(StrictModel):
    criterion_id: Name
    rubric: str
    origin: Literal["case", "default", "overridden"]


class CaseMetricsView(StrictModel):
    metrics_id: Name
    description: str
    command: CommandSpec


class CaseView(StrictModel):
    case_id: ObjectId
    title: str
    initial_prompt: str
    phases: tuple[Name, ...]
    facts: tuple[CaseFactView, ...]
    workflow: tuple[CasePromptStepView, ...]
    rubrics: tuple[CaseRubricView, ...]
    disabled_rubrics: tuple[Name, ...]
    metrics: tuple[CaseMetricsView, ...]
    budgets: CaseBudgets
    tool_requirements: tuple[str, ...]
    repository_id: ObjectId
    persona_id: ObjectId
    baseline_commit: str | None
    reference_patch: bool


class ReviewView(StrictModel):
    attempt_id: Name
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    lanes: ReviewLanes | None = None
