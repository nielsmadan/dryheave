from typing import Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from dryheave.models import Name, ObjectId, RunId, StrictModel
from dryheave.result_models import Assessment, CheckResult, RoleMetrics


class Distribution(StrictModel):
    count: int = Field(ge=0)
    unknown: int = Field(ge=0)
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    stdev: float | None


Eligibility = Literal["eligible", "excluded", "ungraded", "unassessed"]


class PriceBasis(StrictModel):
    kinds: tuple[tuple[str, int], ...] = ()
    bases: tuple[tuple[str, int], ...] = ()
    currencies: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()


class FactDisclosure(StrictModel):
    fact_id: Name
    kind: Literal["proactive", "on request", "unknown"]
    count: int = Field(ge=1)


class HelpNeededSummary(StrictModel):
    measured: int = Field(ge=1)
    unmeasured: int = Field(ge=0)
    facts: tuple[FactDisclosure, ...] = ()


class AttemptTopline(StrictModel):
    latest: bool
    turns: int | None
    seconds: float | None
    tokens: int | None
    cost: float | None
    currency: str | None
    checks: tuple[CheckResult, ...]


class ToplineCheck(StrictModel):
    check_id: Name
    passes: int = Field(ge=0)
    answered: int = Field(ge=0)


class GroupTopline(StrictModel):
    attempts: int = Field(ge=0)
    turns: float | None
    seconds: float | None
    tokens: float | None
    cost: float | None
    currency: str | None
    checks: tuple[ToplineCheck, ...]


class AttemptReport(StrictModel):
    attempt_id: Name
    trial_id: Name
    variant: Name | None
    stage: str
    assessment_id: ObjectId | None
    capture_id: ObjectId | None = None
    quarantine_id: ObjectId | None = None
    pairing_id: ObjectId | None = None
    result: Assessment | None
    current_exclusions: tuple[str, ...] = ()
    raw_evidence: Literal["available", "invalid", "unavailable"]
    mode: Literal["native", "offline-fixture"] | None = None
    roles: tuple[RoleMetrics, ...] = ()
    eligibility: Eligibility | None = None
    topline: AttemptTopline | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        for name in ("eligibility", "topline"):
            if getattr(self, name) is None:
                value.pop(name, None)
        return value


class CriterionSummary(StrictModel):
    case_id: ObjectId
    criterion_id: Name
    passes: int = Field(ge=0)
    graded: int = Field(ge=0)
    scores: Distribution


class CheckSummary(StrictModel):
    case_id: ObjectId
    check_id: Name
    passes: int = Field(ge=0)
    fails: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    errors: int = Field(default=0, ge=0)
    agreement: float | None = Field(default=None, ge=0, le=1)


class MetricSummary(StrictModel):
    case_id: ObjectId
    metric_id: Name
    label: str
    unit: str | None
    values: Distribution


class GroupMetrics(StrictModel):
    variant: str
    mode: str
    scheduled_trials: int | None
    attempts: int
    completed: int
    scored: int
    eligible: int
    audit_excluded: int
    exclusion_reasons: dict[str, int]
    eligible_passes: int
    eligible_completion_rate: float | None
    subjects_reporting_completion: int
    durations: Distribution
    input_tokens: Distribution
    output_tokens: Distribution
    accepted_turns: Distribution
    known_spend_by_currency: dict[str, float]
    unknown_cost_records: int
    partial_cost_attempts: int
    costs: Distribution
    cache_read_tokens: Distribution | None = None
    cache_write_tokens: Distribution | None = None
    total_input_tokens: Distribution | None = None
    criteria: tuple[CriterionSummary, ...] = ()
    checks: tuple[CheckSummary, ...] = ()
    metrics: tuple[MetricSummary, ...] = ()
    simulator_replies: Distribution | None = None
    cost_by_role: dict[str, Distribution] = Field(default_factory=dict)
    price_basis: PriceBasis | None = None
    help_needed: HelpNeededSummary | None = None
    topline: GroupTopline | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        for name in ("criteria", "checks", "metrics"):
            if not getattr(self, name):
                value.pop(name, None)
        for name in ("simulator_replies", "price_basis", "help_needed", "topline"):
            if getattr(self, name) is None:
                value.pop(name, None)
        return value


class RunReport(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["run-report"] = "run-report"
    run_id: RunId
    experiment_id: ObjectId
    compatibility_id: ObjectId | None = None
    durable_sequence: int
    scheduled_trials: int | None
    unstarted_trials: tuple[Name, ...]
    input_error: str | None
    attempts: tuple[AttemptReport, ...]
    groups: tuple[GroupMetrics, ...]
    portable: bool = False
    omissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "Quality distributions use eligible attempts; spending includes every retained attempt with observed costs.",
        "Native usage totals remain partial when descendant or category telemetry is missing.",
        "No significance claims are inferred from small samples.",
    )


class PairChange(StrictModel):
    case_id: ObjectId
    repetition: int
    before_attempt: Name
    after_attempt: Name
    before_completion: str
    after_completion: str
    criterion_changes: dict[str, tuple[str, str]]


class Comparison(StrictModel):
    schema_version: Literal[1] = 1
    before: RunId
    after: RunId
    before_input: str
    after_input: str
    before_reference: str
    after_reference: str
    before_sequence: int
    after_sequence: int
    before_variant: str | None = None
    after_variant: str | None = None
    pairs: tuple[PairChange, ...]
    paired_count: int
    excluded_before: int
    excluded_after: int
    before_metrics: tuple[GroupMetrics, ...]
    after_metrics: tuple[GroupMetrics, ...]
    limitations: tuple[str, ...] = (
        "Pairs require identical case, criterion, simulator, scoring, repetition, seed and execution mode identities.",
        "The latest retained attempt per compatible trial is selected; retries never erase prior spend.",
    )
