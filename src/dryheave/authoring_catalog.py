import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from dryheave.errors import ConflictError, InputError, LockBusyError
from dryheave.filesystem import atomic_write, directory_fd, read_bytes
from dryheave.logs.base import EvidenceExcerpt
from dryheave.models import Name, ObjectId, StrictModel
from dryheave.personas import Persona
from dryheave.serialization import canonical_json, digest, parse_model

CATALOG_LIMIT = 8 * 1024 * 1024
ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class Selection(StrictModel):
    name: Name
    session_ids: tuple[ObjectId, ...] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def unique_sessions(self) -> Self:
        if tuple(sorted(set(self.session_ids))) != self.session_ids:
            raise ValueError("selection sessions must be sorted and unique")
        return self

    @property
    def identifier(self) -> str:
        return digest(canonical_json(self))


class ValidationReference(StrictModel):
    request_revision: int = Field(ge=1)
    content_hash: ObjectId


class ProblemDecision(StrictModel):
    name: Name
    title: ReviewText
    category: ReviewText
    state: Literal["rejected", "unresolved", "drafted", "frozen"]
    session_id: ObjectId
    start_event: Name
    end_event: Name
    evidence: tuple[EvidenceExcerpt, ...] = Field(min_length=1, max_length=32)
    rationale: ReviewText
    boundary_rationale: ReviewText
    baseline_rationale: ReviewText
    dirty_state: Literal["unknown", "clean", "patch"]
    dirty_state_rationale: ReviewText
    micro_bug_rationale: ReviewText | None = None
    draft_path: str | None = Field(default=None, min_length=1, max_length=4096)
    validation: ValidationReference | None = None
    case_id: ObjectId | None = None

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if (self.state in {"drafted", "frozen"}) != (self.draft_path is not None):
            raise ValueError("drafted/frozen decisions require a draft path")
        if (self.state == "frozen") != (self.case_id is not None):
            raise ValueError("only frozen decisions have a case ID")
        if self.validation is not None and self.state != "drafted":
            raise ValueError("only drafted decisions have a validation reference")
        return self


class ProblemRequest(StrictModel):
    name: Name
    selection_id: ObjectId
    description: str = Field(default="", max_length=8000)
    micro_bug: bool = False
    revision: int = Field(default=1, ge=1)
    decisions: dict[Name, ProblemDecision] = Field(default_factory=dict, max_length=100)
    gaps: tuple[ReviewText, ...] = Field(default=(), max_length=32)

    @property
    def identifier(self) -> str:
        return digest(
            canonical_json(self.model_dump(mode="json", exclude={"revision", "decisions", "gaps"}))
        )

    @property
    def target_maximum(self) -> int:
        return 1 if self.micro_bug else 6

    @model_validator(mode="after")
    def bounded_candidates(self) -> Self:
        if any(name != item.name for name, item in self.decisions.items()):
            raise ValueError("decision keys must match their names")
        if (
            sum(item.state in {"drafted", "frozen"} for item in self.decisions.values())
            > self.target_maximum
        ):
            raise ValueError("request exceeds its supported candidate maximum")
        return self


class VoiceDraft(StrictModel):
    schema_version: Literal[1] = 1
    selection_id: ObjectId
    persona: Persona
    safety_review: str = Field(default="", max_length=8000)


class VoiceRecord(StrictModel):
    name: Name
    selection_id: ObjectId
    persona_id: ObjectId
    safety_review: ReviewText


class AuthoringCatalog(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    selections: dict[Name, Selection] = Field(default_factory=dict, max_length=200)
    requests: dict[Name, ProblemRequest] = Field(default_factory=dict, max_length=200)
    voices: dict[Name, VoiceRecord] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        for records in (self.selections, self.requests, self.voices):
            if any(name != item.name for name, item in records.items()):
                raise ValueError("catalog keys must match record names")
        selections = {item.identifier for item in self.selections.values()}
        if any(item.selection_id not in selections for item in self.requests.values()) or any(
            item.selection_id not in selections for item in self.voices.values()
        ):
            raise ValueError("catalog references an unknown selection")
        return self


def read_catalog(root: Path) -> AuthoringCatalog:
    try:
        return parse_model(read_bytes(root / "catalog.json", limit=CATALOG_LIMIT), AuthoringCatalog)
    except FileNotFoundError:
        return AuthoringCatalog()


@contextmanager
def edit_catalog(root: Path, expected_revision: int) -> Iterator[AuthoringCatalog]:
    with directory_fd(root) as descriptor:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusyError(
                "Authoring catalog is being edited; retry after inspecting its revision."
            ) from error
        try:
            catalog = read_catalog(root)
            if catalog.revision != expected_revision:
                raise ConflictError(
                    f"Catalog revision is {catalog.revision}, expected {expected_revision}; inspect and reconcile your edit."
                )
            yield catalog
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def save_catalog(root: Path, catalog: AuthoringCatalog) -> AuthoringCatalog:
    updated = parse_model(
        canonical_json(catalog.model_copy(update={"revision": catalog.revision + 1})),
        AuthoringCatalog,
    )
    content = canonical_json(updated) + b"\n"
    if len(content) > CATALOG_LIMIT:
        raise InputError("Catalog exceeds its bounded size; use a new benchmark workspace.")
    atomic_write(root / "catalog.json", content)
    return updated


def selection_for(catalog: AuthoringCatalog, reference: str) -> Selection:
    item = next(
        (item for item in catalog.selections.values() if reference in {item.name, item.identifier}),
        None,
    )
    if item is None:
        raise InputError("Unknown selection; inspect collect selections.")
    return item


def request_for(catalog: AuthoringCatalog, reference: str) -> ProblemRequest:
    item = next(
        (item for item in catalog.requests.values() if reference in {item.name, item.identifier}),
        None,
    )
    if item is None:
        raise InputError("Unknown request; inspect problem list.")
    return item
