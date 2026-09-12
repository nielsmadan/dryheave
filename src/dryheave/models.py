import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from dryheave.constants import MAX_RELATIVE_PATH_LENGTH

ObjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RunId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonnegativeInt = Annotated[int, Field(ge=0)]


def validate_relative_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or len(value) > MAX_RELATIVE_PATH_LENGTH
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in value
        or ":" in value
        or re.search(r"[\x00-\x1f\x7f]", value)
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("expected a normalized relative POSIX file path")
    return value


RelativePath = Annotated[str, AfterValidator(validate_relative_path)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value


class ObjectKind(StrEnum):
    SESSION = "session"
    REPOSITORY = "repository"
    PERSONA = "persona"
    CASE = "case"
    PROFILE = "profile"
    EXPERIMENT = "experiment"
    SIMULATOR = "simulator"
    SCORING = "scoring"
    RESULT = "result"
    CAPTURE = "capture"
    QUARANTINE = "quarantine"
    ASSESSMENT_EVIDENCE = "assessment-evidence"
    CALIBRATION = "calibration"
    PORTABLE_REPORT = "portable-report"


class AgentKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class TrialStage(StrEnum):
    RESERVED = "reserved"
    PREPARING = "preparing"
    LAUNCHING = "launching"
    INTERACTING = "interacting"
    STOPPING = "stopping"
    CAPTURED = "captured"
    AUDITED = "audited"
    GRADING = "grading"
    FINISHED = "finished"


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    kind: ObjectKind
    payload: dict[str, JsonValue]
    files: dict[RelativePath, ObjectId] = Field(default_factory=dict)
    references: tuple[ObjectId, ...] = ()

    @field_validator("files")
    @classmethod
    def no_file_ancestors(cls, value: dict[str, str]) -> dict[str, str]:
        names = set(value)
        for name in names:
            if any(str(parent) in names for parent in PurePosixPath(name).parents):
                raise ValueError("file paths overlap as ancestors")
        return value

    @field_validator("references")
    @classmethod
    def ordered_unique_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("references must be unique and sorted")
        return value


class AliasIndex(StrictModel):
    schema_version: Literal[1] = 1
    aliases: dict[Name, ObjectId] = Field(default_factory=dict)


class EnvironmentReference(StrictModel):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    source: Literal["inherited"] = "inherited"


class CommandSpec(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: RelativePath | Literal["."] = "."
    timeout_seconds: PositiveInt = 60
    max_output_bytes: PositiveInt = 1024 * 1024
    environment: tuple[EnvironmentReference, ...] = ()

    @field_validator("argv")
    @classmethod
    def valid_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value[0].strip() or any("\x00" in argument for argument in value):
            raise ValueError("argv needs an executable and cannot contain NUL bytes")
        return value

    @field_validator("environment")
    @classmethod
    def distinct_environment(
        cls, value: tuple[EnvironmentReference, ...]
    ) -> tuple[EnvironmentReference, ...]:
        if len({item.name for item in value}) != len(value):
            raise ValueError("environment reference names must be unique")
        return value


class EvidenceReference(StrictModel):
    session_id: ObjectId
    event_id: Name
    visibility: Literal["subject", "curator", "judge"]


class TokenUsage(StrictModel):
    uncached_input: NonnegativeInt | None = None
    cache_read: NonnegativeInt | None = None
    cache_write: NonnegativeInt | None = None
    output: NonnegativeInt | None = None
    reasoning: NonnegativeInt | None = None
    provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def reasoning_is_subset(self) -> Self:
        if self.reasoning is not None and self.output is not None and self.reasoning > self.output:
            raise ValueError("reasoning tokens exceed output tokens")
        return self


class RunMetadata(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    experiment_id: ObjectId
    created_at: AwareDatetime


class JournalEvent(StrictModel):
    schema_version: Literal[1] = 1
    sequence: PositiveInt
    previous_hash: ObjectId | None
    timestamp: AwareDatetime
    event: Name
    trial_id: Name | None = None
    attempt_id: Name | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class RunCheckpoint(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    experiment_id: ObjectId
    sequence: NonnegativeInt
    event_hash: ObjectId | None
    state: dict[str, JsonValue]

    @model_validator(mode="after")
    def sequence_has_hash(self) -> Self:
        if (self.sequence == 0) != (self.event_hash is None):
            raise ValueError("a nonzero sequence requires an event hash")
        return self


def object_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected a lowercase SHA-256 object ID")
    return value
