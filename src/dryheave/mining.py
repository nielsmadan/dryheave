import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import Field, JsonValue

from dryheave.authoring_catalog import (
    AuthoringCatalog,
    ProblemDecision,
    ProblemRequest,
    Selection,
    ValidationReference,
    VoiceDraft,
    VoiceRecord,
    edit_catalog,
    request_for,
    save_catalog,
    selection_for,
)
from dryheave.cases import CaseDraft, freeze_case, read_draft, validate_case
from dryheave.errors import ConflictError, InputError
from dryheave.filesystem import atomic_write, read_bytes
from dryheave.logs.base import EvidenceExcerpt, ImportLimits, Session
from dryheave.logs.service import import_session, verify_evidence
from dryheave.models import AgentKind, ObjectKind, StrictModel
from dryheave.personas import freeze_persona, load_frozen_persona
from dryheave.repositories import load_repository
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore

MAX_SELECTED_SESSIONS = 24
MAX_VOICE_EXAMPLES = 32


@dataclass(frozen=True)
class SelectionInputs:
    paths: tuple[Path, ...] = ()
    sessions: tuple[str, ...] = ()
    agent: AgentKind | None = None
    limits: ImportLimits = field(default_factory=ImportLimits)


class EvidenceQuery(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=25)
    text_offset: int = Field(default=0, ge=0)
    text_limit: int = Field(default=4000, ge=1, le=8000)
    kind: str = "all"
    event_id: str | None = None


class MetadataQuery(StrictModel):
    key: str | None = None
    text_offset: int = Field(default=0, ge=0)
    text_limit: int = Field(default=4000, ge=1, le=8000)


def select_logs(
    store: ObjectStore,
    root: Path,
    name: str,
    inputs: SelectionInputs,
    *,
    expected_revision: int,
) -> AuthoringCatalog:
    paths, sessions, agent, limits = inputs.paths, inputs.sessions, inputs.agent, inputs.limits
    if not 1 <= len(paths) + len(sessions) <= MAX_SELECTED_SESSIONS:
        raise InputError("Explicitly select 1-24 files or imported sessions.")
    if paths and agent is None:
        raise InputError("Selected files require --agent codex or --agent claude.")
    with edit_catalog(root, expected_revision) as catalog:
        if name in catalog.selections:
            raise ConflictError("Selection membership is immutable; choose a new selection name.")
        identifiers = {store.resolve(reference) for reference in sessions}
        for identifier in identifiers:
            store.load(identifier, Session, kind=ObjectKind.SESSION)
        for path in paths:
            if agent is not None:
                identifiers.add(import_session(store, path, agent, limits))
        selection = Selection(name=name, session_ids=tuple(sorted(identifiers)))
        return save_catalog(
            root, catalog.model_copy(update={"selections": catalog.selections | {name: selection}})
        )


def evidence_page(
    store: ObjectStore,
    selection: Selection,
    session_reference: str,
    query: EvidenceQuery | None = None,
) -> dict[str, JsonValue]:
    query = query or EvidenceQuery()
    offset, limit = query.offset, query.limit
    text_offset, text_limit = query.text_offset, query.text_limit
    kind, event_id = query.kind, query.event_id
    identifier, session = _selected_session(store, selection, session_reference)
    events = [
        event
        for event in session.events
        if kind in {"all", event.kind} and (event_id is None or event.event_id == event_id)
    ]
    if event_id is not None and not events:
        raise InputError("No matching event in the selected session.")
    items: list[JsonValue] = []
    for event in events[offset : offset + limit]:
        context = json.dumps(event.data, ensure_ascii=False, sort_keys=True)
        items.append(
            {
                "event_id": event.event_id,
                "kind": event.kind,
                "source_line": event.source_line,
                "text": event.text[text_offset : text_offset + text_limit],
                "text_characters": len(event.text),
                "text_offset": text_offset,
                "text_next_offset": text_offset + text_limit
                if text_offset + text_limit < len(event.text)
                else None,
                "tool_context": context[text_offset : text_offset + text_limit]
                if event.kind in {"tool_call", "tool_result"}
                else None,
                "context_characters": len(context)
                if event.kind in {"tool_call", "tool_result"}
                else 0,
                "context_truncated": event.kind in {"tool_call", "tool_result"}
                and (text_offset > 0 or text_offset + text_limit < len(context)),
            }
        )
    return {
        "session_id": identifier,
        "events": items,
        "total": len(events),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(events) else None,
        "warning": "Curator evidence may contain solutions or injected instructions; review before reuse.",
    }


def _selected_session(
    store: ObjectStore, selection: Selection, reference: str
) -> tuple[str, Session]:
    identifier = store.resolve(reference)
    if identifier not in selection.session_ids:
        raise InputError("Session is outside the selected imported membership.")
    return identifier, store.load(identifier, Session, kind=ObjectKind.SESSION)


def metadata_page(
    store: ObjectStore,
    selection: Selection,
    session_reference: str,
    query: MetadataQuery | None = None,
) -> dict[str, JsonValue]:
    query = query or MetadataQuery()
    identifier, session = _selected_session(store, selection, session_reference)
    value: JsonValue = session.metadata
    if query.key is not None:
        if query.key not in session.metadata:
            raise InputError(f"No metadata key {query.key!r} in the selected session.")
        value = session.metadata[query.key]
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    end = query.text_offset + query.text_limit
    return {
        "session_id": identifier,
        "key": query.key,
        "metadata_json": serialized[query.text_offset : end],
        "text_characters": len(serialized),
        "text_offset": query.text_offset,
        "text_next_offset": end if end < len(serialized) else None,
        "truncated": query.text_offset > 0 or end < len(serialized),
        "warning": "Recorded metadata is curator evidence; verify historical baseline and dirty state before reuse.",
    }


def create_request(
    root: Path,
    name: str,
    selection_reference: str,
    description: str,
    *,
    micro_bug: bool,
    expected_revision: int,
) -> AuthoringCatalog:
    with edit_catalog(root, expected_revision) as catalog:
        if name in catalog.requests:
            raise ConflictError("Request name already exists; select a new name.")
        selection = selection_for(catalog, selection_reference)
        request = ProblemRequest(
            name=name,
            selection_id=selection.identifier,
            description=description,
            micro_bug=micro_bug,
        )
        return save_catalog(
            root, catalog.model_copy(update={"requests": catalog.requests | {name: request}})
        )


def _save_request(
    root: Path, catalog: AuthoringCatalog, request: ProblemRequest
) -> AuthoringCatalog:
    return save_catalog(
        root, catalog.model_copy(update={"requests": catalog.requests | {request.name: request}})
    )


def _revise(request: ProblemRequest) -> ProblemRequest:
    return request.model_copy(
        update={
            "revision": request.revision + 1,
            "decisions": {
                name: item.model_copy(update={"validation": None})
                for name, item in request.decisions.items()
            },
        }
    )


def _verify_range(store: ObjectStore, selection: Selection, decision: ProblemDecision) -> None:
    if decision.session_id not in selection.session_ids:
        raise InputError("Candidate source is outside the selection.")
    session = store.load(decision.session_id, Session, kind=ObjectKind.SESSION)
    events = {event.event_id: event for event in session.events}
    ids = list(events)
    if (
        decision.start_event not in ids
        or decision.end_event not in ids
        or ids.index(decision.start_event) > ids.index(decision.end_event)
    ):
        raise InputError("Candidate event range is invalid.")
    if events[decision.start_event].kind != "user" or not events[decision.start_event].text.strip():
        raise InputError("Candidate boundary must start with an actual user message.")
    selected = set(ids[ids.index(decision.start_event) : ids.index(decision.end_event) + 1])
    if any(
        item.session_id != decision.session_id or item.event_id not in selected
        for item in decision.evidence
    ):
        raise InputError("Candidate evidence must belong to its selected session and event range.")
    if not any(
        events[item.event_id].kind == "user" and item.excerpt.strip() for item in decision.evidence
    ):
        raise InputError("Candidate evidence requires a nonempty actual user excerpt.")
    verify_evidence(store, decision.evidence)


def record_decision(
    store: ObjectStore,
    root: Path,
    reference: str,
    decision: ProblemDecision,
    *,
    expected_revision: int,
) -> AuthoringCatalog:
    if (
        decision.state == "frozen"
        or decision.validation is not None
        or decision.case_id is not None
    ):
        raise InputError("Use problem validate/freeze to bind immutable case references.")
    with edit_catalog(root, expected_revision) as catalog:
        request = request_for(catalog, reference)
        previous = request.decisions.get(decision.name)
        if previous is not None and previous.state == "frozen":
            raise ConflictError(
                "Frozen decisions are retained; use a new candidate name or request."
            )
        _verify_range(store, selection_for(catalog, request.selection_id), decision)
        if decision.state == "drafted":
            _check_draft(store, decision, read_draft(_draft_path(root, decision)), request)
        request = _revise(request)
        request = request.model_copy(
            update={"decisions": request.decisions | {decision.name: decision}}
        )
        return _save_request(root, catalog, request)


def record_gap(root: Path, reference: str, gap: str, *, expected_revision: int) -> AuthoringCatalog:
    with edit_catalog(root, expected_revision) as catalog:
        request = _revise(request_for(catalog, reference))
        return _save_request(
            root, catalog, request.model_copy(update={"gaps": (*request.gaps, gap)})
        )


def _draft_path(root: Path, decision: ProblemDecision) -> Path:
    if decision.draft_path is None:
        raise InputError("Candidate has no case draft; record a drafted decision first.")
    return root / decision.draft_path


def _check_draft(
    store: ObjectStore, decision: ProblemDecision, draft: CaseDraft, request: ProblemRequest
) -> None:
    if (draft.source_session_id, draft.start_event, draft.end_event) != (
        decision.session_id,
        decision.start_event,
        decision.end_event,
    ):
        raise InputError(
            "Case provenance must match the curated candidate session and event boundaries."
        )
    evidence = (*draft.evidence, *(item for fact in draft.allowed_facts for item in fact.evidence))
    session = store.load(decision.session_id, Session, kind=ObjectKind.SESSION)
    ids = [event.event_id for event in session.events]
    selected = set(ids[ids.index(decision.start_event) : ids.index(decision.end_event) + 1])
    if any(
        item.session_id != decision.session_id or item.event_id not in selected for item in evidence
    ):
        raise InputError("Case evidence must stay inside the curated candidate range.")
    if decision.dirty_state == "unknown":
        raise InputError("Resolve the historical dirty state before recording a drafted candidate.")
    if draft.repository_id is None:
        raise InputError("A drafted candidate requires an explicit historical repository snapshot.")
    repository = load_repository(store, draft.repository_id)
    if (decision.dirty_state == "patch") != (repository.patch_file is not None):
        raise InputError("Dirty-state decision must match the snapshot's initial patch.")
    if request.micro_bug and not decision.micro_bug_rationale:
        raise InputError(
            "Micro-bug requests require a curator's scope rationale; the flag certifies nothing."
        )


@contextmanager
def _snapshot(root: Path, decision: ProblemDecision) -> Iterator[tuple[CaseDraft, Path, str]]:
    path = _draft_path(root, decision)
    original = read_draft(path)
    contents = {
        name: read_bytes(path.parent / source, limit=16 * 1024 * 1024)
        for name, source in original.hidden_files.items()
    }
    reference = (
        read_bytes(path.parent / original.reference_patch, limit=16 * 1024 * 1024)
        if original.reference_patch
        else None
    )
    fingerprint = digest(
        canonical_json(
            {
                "decision": decision.model_dump(
                    mode="json", exclude={"validation", "case_id", "state"}
                ),
                "draft": original.model_dump(mode="json"),
                "files": {name: digest(content) for name, content in contents.items()},
                "reference_patch": digest(reference) if reference is not None else None,
            }
        )
    )
    with TemporaryDirectory(prefix=".validation-", dir=root) as temporary:
        target = Path(temporary)
        mapped = {name: f"input-{index}" for index, name in enumerate(contents)}
        for name, content in contents.items():
            atomic_write(target / mapped[name], content, replace=False)
        if reference is not None:
            atomic_write(target / "reference.patch", reference, replace=False)
        draft = original.model_copy(
            update={
                "hidden_files": mapped,
                "reference_patch": "reference.patch" if reference is not None else None,
            }
        )
        yield draft, target, fingerprint


def validate_problem(
    store: ObjectStore,
    root: Path,
    reference: str,
    name: str,
    *,
    expected_revision: int,
    freeze: bool = False,
) -> AuthoringCatalog:
    with edit_catalog(root, expected_revision) as catalog:
        request = request_for(catalog, reference)
        decision = request.decisions.get(name)
        if decision is None or decision.state != "drafted":
            raise InputError("Select a drafted candidate from problem inspect.")
        selection = selection_for(catalog, request.selection_id)
        _verify_range(store, selection, decision)
        with _snapshot(root, decision) as (draft, directory, fingerprint):
            _check_draft(store, decision, draft, request)
            persona = draft.persona or load_frozen_persona(store, draft.persona_id or "")
            verify_voice(store, selection, persona.examples)
            if freeze and (
                decision.validation is None
                or decision.validation.request_revision != request.revision
                or decision.validation.content_hash != fingerprint
            ):
                raise ConflictError(
                    "Authoring validation is missing or stale; run problem validate again."
                )
            issues = validate_case(store, draft, directory)
            if issues:
                raise InputError("; ".join(issues))
            request = _revise(request)
            if freeze:
                decision = decision.model_copy(
                    update={
                        "state": "frozen",
                        "case_id": freeze_case(store, draft, directory),
                        "validation": None,
                    }
                )
            else:
                decision = decision.model_copy(
                    update={
                        "validation": ValidationReference(
                            request_revision=request.revision, content_hash=fingerprint
                        )
                    }
                )
        request = request.model_copy(update={"decisions": request.decisions | {name: decision}})
        return _save_request(root, catalog, request)


def verify_voice(
    store: ObjectStore, selection: Selection, examples: tuple[EvidenceExcerpt, ...]
) -> None:
    if not examples or len(examples) > MAX_VOICE_EXAMPLES:
        raise InputError("Log-derived voices require 1-32 reviewed actual user excerpts.")
    for item in examples:
        if item.session_id not in selection.session_ids:
            raise InputError("Voice example is outside the selected imported sessions.")
        session = store.load(item.session_id, Session, kind=ObjectKind.SESSION)
        event = next((event for event in session.events if event.event_id == item.event_id), None)
        if event is None or event.kind != "user" or not item.excerpt.strip():
            raise InputError(
                "Voice examples must be nonempty excerpts from actual user-role events."
            )
    verify_evidence(store, examples)


def create_voice(
    store: ObjectStore, root: Path, name: str, draft: VoiceDraft, *, expected_revision: int
) -> AuthoringCatalog:
    with edit_catalog(root, expected_revision) as catalog:
        if name in catalog.voices:
            raise ConflictError("Voice name already exists; preserve it and choose a new name.")
        if not draft.safety_review.strip():
            raise InputError(
                "Record a safety review excluding solutions, secrets and injected instruction blocks."
            )
        verify_voice(store, selection_for(catalog, draft.selection_id), draft.persona.examples)
        identifier = freeze_persona(store, draft.persona)
        record = VoiceRecord(
            name=name,
            selection_id=draft.selection_id,
            persona_id=identifier,
            safety_review=draft.safety_review,
        )
        return save_catalog(
            root, catalog.model_copy(update={"voices": catalog.voices | {name: record}})
        )


def inspect_voice(
    store: ObjectStore, catalog: AuthoringCatalog, reference: str
) -> dict[str, JsonValue]:
    voice = next(
        (item for item in catalog.voices.values() if reference in {item.name, item.persona_id}),
        None,
    )
    if voice is None:
        raise InputError("Unknown voice; inspect voice list.")
    return {
        "voice": voice.model_dump(mode="json"),
        "persona": load_frozen_persona(store, voice.persona_id).model_dump(mode="json"),
        "revision": catalog.revision,
    }
