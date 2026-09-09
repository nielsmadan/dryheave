from typing import Literal

from pydantic import Field

from dryheave.models import Name, ObjectId, RunId, StrictModel
from dryheave.result_models import Assessment, RoleMetrics


class Distribution(StrictModel):
    count: int = Field(ge=0)
    unknown: int = Field(ge=0)
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    stdev: float | None


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
