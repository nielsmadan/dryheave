import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StringConstraints, model_validator

from dryheave.errors import ConflictError, InputError, LockBusyError
from dryheave.filesystem import atomic_write, directory_fd, read_bytes
from dryheave.logs.base import EvidenceExcerpt
from dryheave.models import Name, ObjectId, StrictModel
from dryheave.personas import Persona
from dryheave.serialization import canonical_json, digest, parse_model

CATALOG_LIMIT = 8 * 1024 * 1024
VOICE_PREVIEW_LIMIT = 160
MAX_VOICE_EVIDENCE_EVENTS = 64
MAX_TRIAGED_SESSIONS = 24
ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
RepositoryPath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
TriageKind = Literal["config_change", "feature", "bugfix", "extraneous"]
TriageGrade = Literal["small", "medium", "large", "easy", "hard"]
TriageCategory = Literal[
    "config_change",
    "feature_small",
    "feature_medium",
    "feature_large",
    "bugfix_easy",
    "bugfix_medium",
    "bugfix_hard",
    "extraneous",
]
TRIAGE_GRADES: dict[str, tuple[str, ...]] = {
    "feature": ("small", "medium", "large"),
    "bugfix": ("easy", "medium", "hard"),
}
InjectionReason = Literal[
    "agents_instructions",
    "environment_context",
    "skill_wrapper",
    "command_wrapper",
    "system_reminder",
    "unrecognized_wrapper",
]


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


class TriageDecision(StrictModel):
    session_id: ObjectId
    kind: TriageKind
    grade: TriageGrade | None = None
    chosen: bool
    reason: ReviewText

    @model_validator(mode="after")
    def graded_by_kind(self) -> Self:
        allowed = TRIAGE_GRADES.get(self.kind, ())
        if (self.grade is None) is bool(allowed):
            raise ValueError("feature and bugfix triage carries a grade; other kinds carry none")
        if self.grade is not None and self.grade not in allowed:
            raise ValueError("triage grade must belong to its kind's own scale")
        return self

    @property
    def category(self) -> TriageCategory:
        label = f"{self.kind}_{self.grade}" if self.grade is not None else self.kind
        return cast(TriageCategory, label)


class SelectionTriage(StrictModel):
    schema_version: Literal[1] = 1
    name: Name
    selection_id: ObjectId
    decisions: dict[ObjectId, TriageDecision] = Field(
        default_factory=dict, max_length=MAX_TRIAGED_SESSIONS
    )

    @model_validator(mode="after")
    def keyed_by_session(self) -> Self:
        if any(session_id != item.session_id for session_id, item in self.decisions.items()):
            raise ValueError("triage keys must match their session IDs")
        return self


class TriageEntry(TriageDecision):
    repository: RepositoryPath | None = None


class TriageVariety(StrictModel):
    sessions: int = Field(ge=0)
    triaged: int = Field(ge=0)
    untriaged: int = Field(ge=0)
    chosen: int = Field(ge=0)
    rejected: int = Field(ge=0)
    kinds: dict[TriageCategory, int] = Field(default_factory=dict, max_length=8)
    repositories: dict[RepositoryPath, int] = Field(
        default_factory=dict, max_length=MAX_TRIAGED_SESSIONS
    )
    unknown_repositories: int = Field(default=0, ge=0)
    varied: bool

    @model_validator(mode="after")
    def measured_variety(self) -> Self:
        if self.triaged + self.untriaged != self.sessions:
            raise ValueError("triaged and untriaged sessions must total the selected sessions")
        if self.chosen + self.rejected != self.triaged:
            raise ValueError("chosen and rejected decisions must total the triaged sessions")
        if sum(self.kinds.values()) != self.chosen:
            raise ValueError("the kind distribution must total the chosen sessions")
        if sum(self.repositories.values()) + self.unknown_repositories != self.chosen:
            raise ValueError("the repository spread must total the chosen sessions")
        spread = len(self.repositories) + bool(self.unknown_repositories)
        if self.varied != (len(self.kinds) > 1 and spread > 1):
            raise ValueError("variety must follow the chosen kind and repository spread")
        return self


class TriageEvidence(StrictModel):
    schema_version: Literal[1] = 1
    entries: tuple[TriageEntry, ...] = Field(default=(), max_length=MAX_TRIAGED_SESSIONS)
    untriaged: tuple[ObjectId, ...] = Field(default=(), max_length=MAX_TRIAGED_SESSIONS)
    variety: TriageVariety

    @model_validator(mode="after")
    def described_once(self) -> Self:
        triaged = [item.session_id for item in self.entries]
        if len(set(triaged)) != len(triaged) or set(triaged) & set(self.untriaged):
            raise ValueError("triage must describe each selected session at most once")
        if len(set(self.untriaged)) != len(self.untriaged):
            raise ValueError("untriaged sessions must be listed once")
        if (len(self.entries), len(self.untriaged)) != (
            self.variety.triaged,
            self.variety.untriaged,
        ):
            raise ValueError("listed triage must total the counted sessions")
        if sum(item.chosen for item in self.entries) != self.variety.chosen:
            raise ValueError("chosen entries must total the counted chosen sessions")
        return self


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


class VoiceCandidate(StrictModel):
    session_id: ObjectId
    event_id: Name
    characters: int = Field(ge=0)
    preview: str = Field(default="", max_length=VOICE_PREVIEW_LIMIT)


class ExcludedUserEvent(StrictModel):
    session_id: ObjectId
    event_id: Name
    classification: Literal["harness_injected", "unclassified"]
    reasons: tuple[InjectionReason, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def explained_exclusion(self) -> Self:
        if self.classification == "harness_injected" and not self.reasons:
            raise ValueError("harness-injected user events must record a recognized wrapper")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("exclusion reasons must be unique")
        return self


class UserMessageProfile(StrictModel):
    user_events: int = Field(default=0, ge=0)
    genuine: int = Field(default=0, ge=0)
    harness_injected: int = Field(default=0, ge=0)
    unclassified: int = Field(default=0, ge=0)
    genuine_characters: int = Field(default=0, ge=0)
    median_genuine_characters: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def measured_user_messages(self) -> Self:
        if self.genuine + self.harness_injected + self.unclassified != self.user_events:
            raise ValueError("classified user events must total the user-role event count")
        if not self.genuine and (self.genuine_characters or self.median_genuine_characters):
            raise ValueError("genuine characters require at least one genuine user message")
        if self.median_genuine_characters > self.genuine_characters:
            raise ValueError("median genuine characters cannot exceed their total")
        return self


class SessionEvidence(UserMessageProfile):
    session_id: ObjectId


class VoiceEvidence(StrictModel):
    schema_version: Literal[1] = 1
    threshold: int = Field(ge=1)
    user_events: int = Field(ge=0)
    genuine: int = Field(ge=0)
    harness_injected: int = Field(ge=0)
    unclassified: int = Field(ge=0)
    sufficient: bool
    candidates: tuple[VoiceCandidate, ...] = Field(default=(), max_length=MAX_VOICE_EVIDENCE_EVENTS)
    excluded: tuple[ExcludedUserEvent, ...] = Field(
        default=(), max_length=MAX_VOICE_EVIDENCE_EVENTS
    )
    sessions: tuple[SessionEvidence, ...] = Field(default=(), max_length=24)
    truncated: bool = False

    @model_validator(mode="after")
    def measured_classification(self) -> Self:
        if self.genuine + self.harness_injected + self.unclassified != self.user_events:
            raise ValueError("classified user events must total the user-role event count")
        if self.sessions:
            if len({item.session_id for item in self.sessions}) != len(self.sessions):
                raise ValueError("per-session evidence must describe each session once")
            if any(
                sum(getattr(item, counted) for item in self.sessions) != getattr(self, counted)
                for counted in ("user_events", "genuine", "harness_injected", "unclassified")
            ):
                raise ValueError("per-session evidence must total the selection counts")
        if self.sufficient != (self.genuine >= self.threshold):
            raise ValueError("sufficiency must follow the genuine count against the threshold")
        excluded = self.harness_injected + self.unclassified
        if len(self.candidates) > self.genuine or len(self.excluded) > excluded:
            raise ValueError("listed user events exceed their counted classification")
        if self.truncated != (len(self.candidates) < self.genuine or len(self.excluded) < excluded):
            raise ValueError("truncation must follow the listed user events")
        return self


class VoiceRecord(StrictModel):
    name: Name
    selection_id: ObjectId
    persona_id: ObjectId
    safety_review: ReviewText
    evidence: VoiceEvidence | None = None
    triage: TriageEvidence | None = None


class AuthoringCatalog(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    selections: dict[Name, Selection] = Field(default_factory=dict, max_length=200)
    requests: dict[Name, ProblemRequest] = Field(default_factory=dict, max_length=200)
    voices: dict[Name, VoiceRecord] = Field(default_factory=dict, max_length=200)
    triage: dict[Name, SelectionTriage] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        for records in (self.selections, self.requests, self.voices, self.triage):
            if any(name != item.name for name, item in records.items()):
                raise ValueError("catalog keys must match record names")
        selections = {item.identifier for item in self.selections.values()}
        if any(
            item.selection_id not in selections
            for records in (self.requests, self.voices, self.triage)
            for item in records.values()
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


def voice_for(catalog: AuthoringCatalog, reference: str) -> VoiceRecord:
    item = next(
        (item for item in catalog.voices.values() if reference in {item.name, item.persona_id}),
        None,
    )
    if item is None:
        raise InputError("Unknown voice; inspect voice list.")
    return item


def request_for(catalog: AuthoringCatalog, reference: str) -> ProblemRequest:
    item = next(
        (item for item in catalog.requests.values() if reference in {item.name, item.identifier}),
        None,
    )
    if item is None:
        raise InputError("Unknown request; inspect problem list.")
    return item
