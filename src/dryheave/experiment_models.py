from typing import Literal, Self

from pydantic import Field, model_validator

from dryheave.controller_models import ControllerRecipe
from dryheave.models import Name, ObjectId, RelativePath, StrictModel

MAX_TRIALS = 10000


class VariantSpec(StrictModel):
    name: Name
    profile: str = Field(min_length=1)
    model: str | None = None
    effort: str | None = None
    workflow: str | None = None


class FrozenVariant(StrictModel):
    name: Name
    profile_id: ObjectId


class PriceRate(StrictModel):
    model: str = Field(min_length=1)
    uncached_input: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    output: float = Field(ge=0)


class PriceTable(StrictModel):
    version: str = Field(min_length=1)
    effective_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: Literal["per-million-tokens"] = "per-million-tokens"
    rates: tuple[PriceRate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_models(self) -> Self:
        if len({rate.model for rate in self.rates}) != len(self.rates):
            raise ValueError("price models must be distinct")
        return self


class ScoringConfig(StrictModel):
    schema_version: Literal[1] = 1
    policy: Literal["required-criteria-v1"] = "required-criteria-v1"
    judge: ControllerRecipe | None = None
    prices: PriceTable | None = None


class FixtureTurn(StrictModel):
    prompt: str = Field(min_length=1, max_length=32000)
    assistant: str = Field(min_length=1, max_length=32000)
    files: dict[RelativePath, str] = Field(default_factory=dict)
    state: Literal["completed", "question", "approval", "unsupported", "cancelled"] = "completed"

    @model_validator(mode="after")
    def task_files(self) -> Self:
        if any(".git" in path.split("/") for path in self.files):
            raise ValueError("fixture writes are limited to task files")
        if sum(len(text.encode()) for text in self.files.values()) > 1024 * 1024:
            raise ValueError("fixture writes exceed their byte limit")
        return self


class ExperimentDraft(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    cases: tuple[str, ...] = Field(min_length=1, max_length=100)
    variants: tuple[VariantSpec, ...] = Field(min_length=1, max_length=100)
    repetitions: int = Field(default=1, gt=0, le=1000)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    simulator: ControllerRecipe = Field(default_factory=ControllerRecipe)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    fixture: tuple[FixtureTurn, ...] = Field(default=(), max_length=100)
    initial_input_policy: Literal["conversation", "curated-native-command"] = "conversation"

    @model_validator(mode="after")
    def bounded_matrix(self) -> Self:
        if len(self.cases) * len(self.variants) * self.repetitions > MAX_TRIALS:
            raise ValueError("experiment exceeds 10000 trials")
        if len(set(self.cases)) != len(self.cases):
            raise ValueError("case references must be distinct")
        if len({variant.name for variant in self.variants}) != len(self.variants):
            raise ValueError("variant names must be distinct")
        return self


class TrialSpec(StrictModel):
    trial_id: Name
    case_id: ObjectId
    profile_id: ObjectId
    variant: Name
    repetition: int = Field(ge=1)
    seed: int = Field(ge=0)


class FrozenExperiment(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    case_ids: tuple[ObjectId, ...] = Field(min_length=1, max_length=100)
    variants: tuple[FrozenVariant, ...] = Field(min_length=1, max_length=100)
    repetitions: int = Field(gt=0, le=1000)
    seed: int = Field(ge=0, le=2**63 - 1)
    simulator_id: ObjectId
    scoring_id: ObjectId
    trials: tuple[TrialSpec, ...] = Field(min_length=1, max_length=10000)
    fixture: tuple[FixtureTurn, ...] = Field(default=(), max_length=100)
    initial_input_policy: Literal["conversation", "curated-native-command"] = "conversation"
