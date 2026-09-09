from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from dryheave.errors import InputError
from dryheave.filesystem import read_bytes
from dryheave.logs.base import EvidenceExcerpt, Session
from dryheave.logs.service import verify_evidence
from dryheave.models import CommandSpec, Name, ObjectId, ObjectKind, RelativePath, StrictModel
from dryheave.personas import Persona, freeze_persona, load_frozen_persona, validate_persona
from dryheave.repositories import load_repository
from dryheave.serialization import parse_model
from dryheave.storage import ObjectStore

MIN_DESCRIPTION_LENGTH = 10
MIN_RUBRIC_LENGTH = 20


class TaskFact(StrictModel):
    fact_id: Name
    text: str = Field(min_length=1, max_length=8000)
    disclosure: Literal["on_request", "proactive"] = "on_request"
    evidence: tuple[EvidenceExcerpt, ...] = ()
    curator_authored: bool = False

    @model_validator(mode="after")
    def has_source(self) -> Self:
        if not self.text.strip():
            raise ValueError("fact text must not be blank")
        if not self.evidence and not self.curator_authored:
            raise ValueError("facts require evidence or an explicit curator-authored declaration")
        if any(item.visibility != "subject" for item in self.evidence):
            raise ValueError("allowed facts may only cite subject-safe evidence")
        return self


class DeterministicCriterion(StrictModel):
    criterion_id: Name
    kind: Literal["deterministic"] = "deterministic"
    description: str = Field(min_length=10, max_length=4000)
    required: bool = True
    command: CommandSpec
    entrypoint: RelativePath
    expected_stdout: str = Field(min_length=1, max_length=1000)
    expected_failure_stdout: str | None = Field(default=None, min_length=1, max_length=1000)
    suite_id: Name | None = None
    calibration: Literal["uncalibrated"] = "uncalibrated"

    @model_validator(mode="after")
    def trusted_entrypoint(self) -> Self:
        if self.description.strip().lower().startswith(("todo", "placeholder", "fill in")):
            raise ValueError("criterion description cannot be a placeholder")
        if len(self.description.strip()) < MIN_DESCRIPTION_LENGTH:
            raise ValueError("criterion description must be substantive")
        if f"{{verifier}}/{self.entrypoint}" not in self.command.argv:
            raise ValueError(
                "command must invoke the supplied hidden entrypoint with {verifier}/PATH"
            )
        if not self.expected_stdout.strip() or (
            self.expected_failure_stdout is not None and not self.expected_failure_stdout.strip()
        ):
            raise ValueError("expected execution evidence must not be blank")
        return self


class RubricCriterion(StrictModel):
    criterion_id: Name
    kind: Literal["judge"] = "judge"
    required: bool = True
    rubric: str = Field(min_length=20, max_length=16000)

    @model_validator(mode="after")
    def substantive_rubric(self) -> Self:
        if len(self.rubric.strip()) < MIN_RUBRIC_LENGTH or self.rubric.strip().lower().startswith(
            ("todo", "placeholder", "fill in")
        ):
            raise ValueError("rubric requires a substantive curator-authored criterion")
        return self


class CaseBudgets(StrictModel):
    max_turns: int = Field(default=4, gt=0, le=100)
    subject_seconds: int = Field(default=180, gt=0, le=86400)
    setup_seconds: int = Field(default=120, gt=0, le=3600)


class CapturePolicy(StrictModel):
    max_files: int = Field(default=20000, gt=0, le=100000)
    max_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=256 * 1024 * 1024)
    max_depth: int = Field(default=32, gt=0, le=64)
    exclude: tuple[RelativePath, ...] = ()
    include_ignored: tuple[RelativePath, ...] = ()


class CaseContent(StrictModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=300)
    initial_prompt: str = Field(min_length=1, max_length=32000)
    allowed_facts: tuple[TaskFact, ...] = ()
    evidence: tuple[EvidenceExcerpt, ...] = ()
    setup: tuple[CommandSpec, ...] = ()
    tool_requirements: tuple[str, ...] = ()
    budgets: CaseBudgets = Field(default_factory=CaseBudgets)
    capture_policy: CapturePolicy = Field(default_factory=CapturePolicy)
    criteria: tuple[DeterministicCriterion | RubricCriterion, ...] = ()
    intent_confirmed: bool = False
    facts_reviewed: bool = False
    unresolved_issues: tuple[str, ...] = ()


class CaseDraft(CaseContent):
    source_session_id: ObjectId
    start_event: Name
    end_event: Name
    repository_id: ObjectId | None = None
    persona: Persona | None = None
    persona_id: ObjectId | None = None
    hidden_files: dict[RelativePath, str] = Field(default_factory=dict)
    reference_patch: str | None = None


class FrozenCase(CaseContent):
    repository_id: ObjectId
    persona_id: ObjectId
    source_session_id: ObjectId
    source_path_hash: ObjectId
    source_start_event: Name
    source_end_event: Name
    hidden_files: dict[RelativePath, RelativePath] = Field(default_factory=dict)
    reference_patch_file: RelativePath | None = None

    @model_validator(mode="after")
    def replay_invariants(self) -> Self:
        issues = _content_issues(self)
        if issues:
            raise ValueError("; ".join(issues))
        for criterion in self.criteria:
            if (
                isinstance(criterion, DeterministicCriterion)
                and criterion.entrypoint not in self.hidden_files
            ):
                raise ValueError("hidden verifier entrypoint is missing")
        if len(set(self.hidden_files.values())) != len(self.hidden_files):
            raise ValueError("hidden file blob paths must be unique")
        return self


def _content_issues(case: CaseContent) -> list[str]:
    issues = list(case.unresolved_issues)
    if not case.intent_confirmed:
        issues.append("Curator must confirm task intent and selected event boundaries.")
    if not case.facts_reviewed:
        issues.append("Curator must review allowed facts and mark facts_reviewed.")
    if not case.initial_prompt.strip() or not case.title.strip():
        issues.append("Title and initial prompt cannot be blank.")
    if not case.evidence:
        issues.append("Case requires curated source evidence.")
    if not case.criteria or not any(item.required for item in case.criteria):
        issues.append("At least one usable required hidden verifier or judge rubric is required.")
    if len({criterion.criterion_id for criterion in case.criteria}) != len(case.criteria):
        issues.append("Grading criterion IDs must be unique.")
    if len({fact.fact_id for fact in case.allowed_facts}) != len(case.allowed_facts):
        issues.append("Allowed fact IDs must be unique.")
    return issues


def load_frozen_case(store: ObjectStore, reference: str) -> FrozenCase:
    case = store.load(reference, FrozenCase, kind=ObjectKind.CASE)
    manifest = store.get(reference, kind=ObjectKind.CASE)
    if manifest.references != tuple(sorted({case.repository_id, case.persona_id})):
        raise InputError("Frozen case reference closure does not match its runtime inputs.")
    expected = set(case.hidden_files.values())
    if case.reference_patch_file:
        expected.add(case.reference_patch_file)
    if set(manifest.files) != expected:
        raise InputError("Frozen case blob map does not match its declared hidden inputs.")
    for content in store.read_blobs(reference, tuple(expected)).values():
        if not content.strip():
            raise InputError("Frozen hidden inputs must be nonempty.")
    load_repository(store, case.repository_id)
    load_frozen_persona(store, case.persona_id)
    return case


def draft_case(
    store: ObjectStore,
    session_reference: str,
    *,
    repository_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> CaseDraft:
    session_id = store.resolve(session_reference)
    session = store.load(session_id, Session, kind=ObjectKind.SESSION)
    user = next(
        (
            event
            for event in session.events
            if event.kind == "user"
            and event.text.strip()
            and (start is None or event.event_id == start)
        ),
        None,
    )
    if user is None:
        raise InputError("Select a starting event containing a user message.")
    finish = (
        next((event for event in session.events if event.event_id == end), None)
        if end
        else session.events[-1]
    )
    if finish is None or session.events.index(finish) < session.events.index(user):
        raise InputError("End event must exist at or after the starting event.")
    excerpt = EvidenceExcerpt(
        session_id=session_id,
        event_id=user.event_id,
        visibility="subject",
        excerpt=user.text[:16000],
    )
    return CaseDraft(
        title=user.text.strip().splitlines()[0][:120] or "Untitled task",
        initial_prompt=user.text[:32000],
        source_session_id=session_id,
        start_event=user.event_id,
        end_event=finish.event_id,
        repository_id=repository_id,
        evidence=(excerpt,),
        persona=Persona(
            name="Curated user",
            instructions="",
            disclosure_policy="",
            unknown_answer_policy="",
            examples=(excerpt,),
        ),
        unresolved_issues=(
            "Confirm task boundaries and intent; curate facts, persona and grading.",
        ),
    )


def _evidence(draft: CaseDraft) -> tuple[EvidenceExcerpt, ...]:
    return (*draft.evidence, *(item for fact in draft.allowed_facts for item in fact.evidence))


def _source_file(root: Path, name: str) -> bytes:
    path = Path(name)
    if not path.is_absolute():
        path = root / path
    return read_bytes(path, limit=16 * 1024 * 1024)


def validate_case(store: ObjectStore, draft: CaseDraft, root: Path) -> tuple[str, ...]:
    issues = _content_issues(draft)
    if draft.repository_id is None:
        issues.append("Explicit verified starting commit snapshot is required.")
    else:
        load_repository(store, draft.repository_id)
    if (draft.persona is None) == (draft.persona_id is None):
        issues.append("Select exactly one inline persona or immutable persona ID.")
    elif draft.persona is not None:
        issues.extend(validate_persona(draft.persona))
        verify_evidence(store, draft.persona.examples)
    else:
        load_frozen_persona(store, draft.persona_id or "")
    issues.extend(_validate_grading(draft, root))
    _validate_range(store, draft)
    verify_evidence(store, _evidence(draft))
    return tuple(issues)


def _validate_range(store: ObjectStore, draft: CaseDraft) -> None:
    session = store.load(draft.source_session_id, Session, kind=ObjectKind.SESSION)
    ids = [event.event_id for event in session.events]
    if (
        draft.start_event not in ids
        or draft.end_event not in ids
        or ids.index(draft.start_event) > ids.index(draft.end_event)
    ):
        raise InputError("Selected source event range is invalid.")
    selected = set(ids[ids.index(draft.start_event) : ids.index(draft.end_event) + 1])
    if any(
        item.session_id == draft.source_session_id and item.event_id not in selected
        for item in _evidence(draft)
    ):
        raise InputError("Case evidence lies outside its selected source event range.")
    if not draft.evidence:
        raise InputError("Case requires curated source evidence.")


def freeze_case(store: ObjectStore, draft: CaseDraft, root: Path) -> str:
    issues = validate_case(store, draft, root)
    if issues:
        raise InputError("; ".join(issues))
    persona_id = (
        freeze_persona(store, draft.persona) if draft.persona is not None else draft.persona_id
    )
    session = store.load(draft.source_session_id, Session, kind=ObjectKind.SESSION)
    files = {
        f"verifiers/{name}": _source_file(root, source)
        for name, source in draft.hidden_files.items()
    }
    if draft.reference_patch:
        files["reference.patch"] = _source_file(root, draft.reference_patch)
    frozen = FrozenCase.model_validate(
        draft.model_dump(
            exclude={
                "persona",
                "persona_id",
                "hidden_files",
                "reference_patch",
                "start_event",
                "end_event",
            }
        )
        | {
            "persona_id": persona_id,
            "source_path_hash": session.source_path_hash,
            "source_start_event": draft.start_event,
            "source_end_event": draft.end_event,
            "hidden_files": {name: f"verifiers/{name}" for name in draft.hidden_files},
            "reference_patch_file": "reference.patch" if draft.reference_patch else None,
        }
    )
    return store.put(
        ObjectKind.CASE, frozen, files=files, references=(frozen.repository_id, frozen.persona_id)
    )


def read_draft(path: Path) -> CaseDraft:
    return parse_model(read_bytes(path, limit=16 * 1024 * 1024), CaseDraft)


def subject_context(case: FrozenCase) -> dict[str, object]:
    return {
        "initial_prompt": case.initial_prompt,
        "allowed_facts": [
            {"fact_id": fact.fact_id, "text": fact.text, "disclosure": fact.disclosure}
            for fact in case.allowed_facts
        ],
    }


def _validate_grading(draft: CaseDraft, root: Path) -> tuple[str, ...]:
    issues: list[str] = []
    for criterion in draft.criteria:
        if (
            isinstance(criterion, DeterministicCriterion)
            and criterion.entrypoint not in draft.hidden_files
        ):
            issues.append(f"Hidden verifier entrypoint is missing: {criterion.entrypoint}.")
        text = (
            criterion.description
            if isinstance(criterion, DeterministicCriterion)
            else criterion.rubric
        )
        if text.strip().lower() in {"todo", "placeholder", "fill in grading here"}:
            issues.append(f"Criterion {criterion.criterion_id} is a placeholder.")
    for name, path in draft.hidden_files.items():
        if not _source_file(root, path).strip():
            issues.append(f"Hidden verifier file is empty: {name}.")
    if draft.reference_patch and not _source_file(root, draft.reference_patch).strip():
        issues.append("Reference patch must be nonempty when supplied.")
    return tuple(issues)
