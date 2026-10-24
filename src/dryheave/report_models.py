from typing import Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from dryheave.cases import FactKind
from dryheave.models import Name, ObjectId, RunId, StrictModel
from dryheave.result_models import (
    Assessment,
    AttemptOutcome,
    InspectionVerdict,
    OutcomeReason,
    RoleMetrics,
)
from dryheave.runner_models import RunBudget


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


class FactRow(StrictModel):
    case_id: ObjectId
    fact_id: Name
    kind: FactKind
    phase: Name | None
    attempts: int = Field(ge=0)
    met: int = Field(ge=0)
    unmet: int = Field(ge=0)
    unclear: int = Field(ge=0)
    disputed: int = Field(ge=0)
    own: int = Field(ge=0)
    elicited: int = Field(ge=0)
    corrected: int = Field(ge=0)
    unreached: int = Field(ge=0)
    raised: int = Field(ge=0)
    raised_rate: float | None = Field(ge=0, le=1)
    first_inspection_met: int = Field(ge=0)
    first_inspected: int = Field(ge=0)
    first_inspection_met_rate: float | None = Field(ge=0, le=1)
    regressions: int = Field(ge=0)


class FactGroup(StrictModel):
    attempts: int = Field(ge=0)
    outcomes: dict[str, int] = Field(default_factory=dict)
    outcome_reasons: dict[str, int] = Field(default_factory=dict)
    pass_rate: float | None = Field(ge=0, le=1)
    independence: Distribution
    corrections: Distribution
    unreached_attempts: int = Field(default=0, ge=0)
    uncovered: int = Field(default=0, ge=0)
    facts: tuple[FactRow, ...] = ()


class AttemptTopline(StrictModel):
    latest: bool
    turns: int | None
    seconds: float | None
    tokens: int | None
    cost: float | None
    currency: str | None
    outcome: AttemptOutcome | None = None
    outcome_reason: OutcomeReason | None = None
    independence: float | None = None
    corrections: int | None = None
    first_inspection: InspectionVerdict | None = None
    workflow: str | None = None
    workflow_completed: bool | None = None
    eligible: bool = False


class GroupTopline(StrictModel):
    attempts: int = Field(ge=0)
    turns: float | None
    seconds: float | None
    tokens: float | None
    cost: float | None
    currency: str | None
    passes: int = 0
    graded: int = 0
    independence: float | None = None
    corrections: float | None = None
    first_inspection_passes: int = 0
    first_inspection_attempts: int = 0
    workflows_completed: int = 0
    workflow_attempts: int = 0


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
    metrics: tuple[MetricSummary, ...] = ()
    stand_in_replies: Distribution | None = None
    cost_by_role: dict[str, Distribution] = Field(default_factory=dict)
    price_basis: PriceBasis | None = None
    facts: FactGroup | None = None
    topline: GroupTopline | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        for name in ("criteria", "metrics"):
            if not getattr(self, name):
                value.pop(name, None)
        for name in ("stand_in_replies", "price_basis", "facts", "topline"):
            if getattr(self, name) is None:
                value.pop(name, None)
        return value


class FloorVariant(StrictModel):
    run_id: RunId
    label: str = Field(min_length=1, max_length=200)
    budget: RunBudget
    group: GroupMetrics


class RunReport(StrictModel):
    schema_version: Literal[2] = 2
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
    budget: RunBudget | None = None
    floor: FloorVariant | None = None
    portable: bool = False
    omissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "Quality distributions use eligible attempts; spending includes every retained attempt with observed costs.",
        "Native usage totals remain partial when descendant or category telemetry is missing.",
        "No significance claims are inferred from small samples.",
    )


class ModelSetup(StrictModel):
    status: Literal["configured", "none", "unavailable"]
    kind: str | None = None
    model: str | None = None
    effort: str | None = None
    identity: str | None = None


def _unavailable() -> ModelSetup:
    return ModelSetup(status="unavailable")


class RunSetup(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["run-setup"] = "run-setup"
    run_id: RunId
    case_ids: tuple[ObjectId, ...] | None = None
    variants: dict[str, ModelSetup] = Field(default_factory=dict)
    stand_in: ModelSetup = Field(default_factory=_unavailable)
    judge: ModelSetup = Field(default_factory=_unavailable)
    inspector: ModelSetup = Field(default_factory=_unavailable)
    price_versions: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


class PairChange(StrictModel):
    case_id: ObjectId
    repetition: int
    before_attempt: Name
    after_attempt: Name
    before_outcome: str
    after_outcome: str
    criterion_changes: dict[str, tuple[str, str]]
    fact_changes: dict[str, tuple[str, str]] = Field(default_factory=dict)


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
        "Pairs require identical case, fact, criterion, stand-in, scoring, repetition, seed and execution mode identities.",
        "The latest retained attempt per compatible trial is selected; retries never erase prior spend.",
    )
