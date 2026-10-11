from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from dryheave.models import Name, ObjectId, RunId, StrictModel, TokenUsage

Outcome = Literal["pass", "fail", "error", "not_run"]


class AuditFinding(StrictModel):
    finding_id: Name
    rule_id: Name
    confidence: Literal["confirmed", "suspected"]
    description: str
    evidence_hash: ObjectId


class AuditReview(StrictModel):
    finding_id: Name
    decision: Literal["dismiss", "uphold"]
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=10, max_length=2000)
    reviewed_at: AwareDatetime


class AuditReport(StrictModel):
    input_integrity: Literal["verified", "invalid", "unknown"]
    capture_integrity: Literal["verified", "invalid", "unavailable"]
    cleanup: Literal["stopped", "unresolved"]
    capture_complete: bool
    findings: tuple[AuditFinding, ...] = ()
    coverage: tuple[str, ...]


class CheckExecution(StrictModel):
    outcome: Outcome
    execution_observed: bool = False
    returncode: int | None = None
    process_outcome: str | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    stdout_sha256: ObjectId | None = None
    stderr_sha256: ObjectId | None = None
    error: str | None = None


class Calibration(StrictModel):
    status: Literal["demonstrated", "ineffective", "unavailable"] = "unavailable"
    baseline: CheckExecution | None = None
    reference: CheckExecution | None = None


class CriterionResult(StrictModel):
    criterion_id: Name
    kind: Literal["deterministic", "judge"]
    required: bool
    outcome: Outcome
    execution: CheckExecution | None = None
    calibration: Calibration | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    evidence_hash: ObjectId | None = None
    error: str | None = None


Answer = Literal["yes", "no", "unclear"]


class CheckResult(StrictModel):
    check_id: Name
    outcome: Literal["pass", "fail", "not_applicable", "error"]
    answer: Answer | None = None


class HelpNeeded(StrictModel):
    simulator_replies: int = Field(ge=0)
    facts_disclosed: tuple[Name, ...]
    proactive_facts: tuple[Name, ...]
    on_request_facts: tuple[Name, ...]


MAX_METRIC_VALUES = 64
MAX_METRIC_TEXT = 500
MAX_ERROR_LENGTH = 1000


class MetricValue(StrictModel):
    metric_id: Name
    label: str = Field(min_length=1, max_length=200)
    value: int | float | bool | str
    unit: str | None = Field(default=None, min_length=1, max_length=200)
    better: Literal["higher", "lower"] | None = None

    @field_validator("value", mode="before")
    @classmethod
    def scalar_value(cls, value: object) -> object:
        if not isinstance(value, int | float | bool | str):
            raise ValueError("metric value must be a number, boolean or string")
        if isinstance(value, str) and len(value) > MAX_METRIC_TEXT:
            raise ValueError(f"metric string value exceeds {MAX_METRIC_TEXT} characters")
        return value


class MetricsResult(StrictModel):
    metrics_id: Name
    outcome: Literal["completed", "error"]
    values: tuple[MetricValue, ...] = Field(default=(), max_length=MAX_METRIC_VALUES)
    execution: CheckExecution | None = None
    error: str | None = Field(default=None, max_length=MAX_ERROR_LENGTH)

    @model_validator(mode="after")
    def distinct_values(self) -> Self:
        if len({item.metric_id for item in self.values}) != len(self.values):
            raise ValueError("metric IDs must be unique")
        return self


class UsageCharge(StrictModel):
    identity: str
    usage: TokenUsage | None
    observed_model: str | None = None
    requested_model: str | None = None
    model_basis: Literal["observed", "requested-root", "unknown", "scripted"] = "unknown"
    cost: float | None = Field(default=None, ge=0)
    cost_kind: Literal["estimated", "provider", "fixture", "unknown"] = "unknown"
    currency: str | None = None
    price_version: str | None = None
    price_date: str | None = None
    raw_evidence_sha256: ObjectId
    reason: str


class RoleMetrics(StrictModel):
    role: Literal["subject", "simulator", "judge"]
    records: tuple[UsageCharge, ...]
    known_tokens: TokenUsage
    known_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    coverage: Literal["complete", "partial", "unknown", "fixture"]
    reasons: tuple[str, ...]


class Assessment(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["assessment"] = "assessment"
    run_id: RunId
    experiment_id: ObjectId
    trial_id: Name
    attempt_id: Name
    capture_id: ObjectId | None
    quarantine_id: ObjectId | None = None
    case_id: ObjectId | None
    profile_id: ObjectId | None
    simulator_id: ObjectId | None
    scoring_id: ObjectId | None
    criteria_id: ObjectId | None
    variant: Name | None
    repetition: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=0)
    mode: Literal["native", "offline-fixture"]
    created_at: AwareDatetime
    phase: Literal["audited", "finished"]
    audit: AuditReport
    reviews: tuple[AuditReview, ...] = ()
    criteria: tuple[CriterionResult, ...] = ()
    deterministic_completion: Literal["pass", "fail", "indeterminate"] = "indeterminate"
    completion: Literal["pass", "fail", "indeterminate"] = "indeterminate"
    eligible: bool = False
    exclusion_reasons: tuple[str, ...]
    stop_reason: str
    subject_observation: str | None
    accepted_turns: int | None = Field(default=None, ge=0)
    subject_seconds: float | None = Field(default=None, ge=0)
    setup_seconds: float | None = Field(default=None, ge=0)
    roles: tuple[RoleMetrics, ...]
    evidence_id: ObjectId | None = None
    evidence_complete: bool = True
    previous_id: ObjectId | None = None
    checks: tuple[CheckResult, ...] = ()
    metrics: tuple[MetricsResult, ...] = ()
    help_needed: HelpNeeded | None = None
    rubric_set: str | None = None
    graded_case_id: ObjectId | None = None
    regrade_id: Name | None = None
    limitations: tuple[str, ...] = (
        "Native repository-object isolation does not isolate host access.",
        "Native access and detached descendant telemetry remain partial.",
        "Input and capture IDs are provenance; audit/regrade requires their optional exact raw objects.",
        "Price estimates use frozen tables and are not current market quotes.",
    )

    @model_validator(mode="after")
    def eligibility_contract(self) -> Self:
        reviewed = {item.finding_id: item.decision for item in self.reviews}
        if self.eligible and any(
            item.confidence == "confirmed" or reviewed.get(item.finding_id) != "dismiss"
            for item in self.audit.findings
        ):
            raise ValueError("eligible assessments cannot retain unresolved audit findings")
        if self.eligible and (
            self.phase != "finished"
            or self.exclusion_reasons
            or self.completion == "indeterminate"
            or self.audit.input_integrity != "verified"
            or self.audit.capture_integrity != "verified"
            or self.audit.cleanup != "stopped"
            or not self.audit.capture_complete
            or not self.evidence_complete
        ):
            raise ValueError("eligible assessments require finished, scorable, unexcluded results")
        if len({item.criterion_id for item in self.criteria}) != len(self.criteria):
            raise ValueError("criterion results must be distinct")
        if len({item.check_id for item in self.checks}) != len(self.checks):
            raise ValueError("check results must be distinct")
        if len({item.metrics_id for item in self.metrics}) != len(self.metrics):
            raise ValueError("metrics results must be distinct")
        if tuple(item.role for item in self.roles) != ("subject", "simulator", "judge"):
            raise ValueError("role accounting must cover subject, simulator and judge")
        return self

    @model_serializer(mode="wrap")
    def serialized(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        for name in ("checks", "metrics"):
            if not getattr(self, name):
                value.pop(name, None)
        for name in ("rubric_set", "help_needed", "graded_case_id", "regrade_id"):
            if getattr(self, name) is None:
                value.pop(name, None)
        return value


class RegradeRecord(StrictModel):
    regrade_id: Name
    case_id: ObjectId | None = None
    assessment_id: ObjectId | None = None


class AssessmentEvidence(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    attempt_id: Name
    files: dict[str, ObjectId]
    omissions: tuple[str, ...] = ()
