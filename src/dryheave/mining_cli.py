import argparse
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from dryheave.authoring_catalog import (
    AuthoringCatalog,
    ProblemDecision,
    ProblemRequest,
    TriageEvidence,
    VoiceDraft,
    VoiceEvidence,
    read_catalog,
    request_for,
    selection_for,
    voice_for,
)
from dryheave.commands import CommandRegistry
from dryheave.filesystem import atomic_write, read_bytes
from dryheave.logs.base import EvidenceExcerpt, ImportLimits, Session
from dryheave.mining import (
    EvidenceQuery,
    MetadataQuery,
    SelectionInputs,
    create_request,
    create_voice,
    delete_voice,
    evidence_page,
    inspect_voice,
    metadata_page,
    record_decision,
    record_gap,
    record_triage,
    resolve_voice_name,
    select_logs,
    session_repository,
    triage_decision,
    triage_evidence,
    validate_problem,
    voice_evidence,
)
from dryheave.models import AgentKind, ObjectKind
from dryheave.personas import Persona
from dryheave.serialization import parse_model
from dryheave.storage import ObjectStore
from dryheave.workspaces import initialized_workspace


def _root() -> Path:
    return initialized_workspace().path("authoring")


def _expected(args: argparse.Namespace, root: Path) -> int:
    return (
        read_catalog(root).revision if args.expect_revision is None else int(args.expect_revision)
    )


def _select(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    catalog = select_logs(
        store,
        root,
        args.name,
        SelectionInputs(
            paths=tuple(args.files),
            sessions=tuple(args.session),
            agent=AgentKind(args.agent) if args.agent else None,
            limits=ImportLimits(max_bytes=args.max_bytes, max_events=args.max_events),
        ),
        expected_revision=_expected(args, root),
    )
    selection = catalog.selections[args.name]
    return {
        "selection": selection.model_dump(mode="json"),
        "id": selection.identifier,
        "revision": catalog.revision,
        "next": f"Inspect collect selection {args.name}, then curate with dryheave-voice-profile and dryheave-generate-problem.",
    }


def _selections(_args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    return {
        "revision": catalog.revision,
        "selections": [
            {"name": item.name, "id": item.identifier, "sessions": len(item.session_ids)}
            for item in catalog.selections.values()
        ],
    }


def _selection(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    selection = selection_for(catalog, args.reference)
    triage = triage_evidence(store, catalog, selection)
    decided = {item.session_id: item for item in triage.entries}
    sessions: list[JsonValue] = []
    for identifier in selection.session_ids:
        session = store.load(identifier, Session, kind=ObjectKind.SESSION)
        decision = decided.get(identifier)
        sessions.append(
            {
                "id": identifier,
                "agent": session.agent.value,
                "events": len(session.events),
                "source_id": session.source_id,
                "repository": session_repository(session),
                "triage": decision.model_dump(mode="json") if decision is not None else None,
                "warnings": [item.model_dump(mode="json") for item in session.warnings[:20]],
                "warning_count": len(session.warnings),
                "metadata_command": f"dryheave collect metadata {selection.identifier} --session {identifier} --json",
            }
        )
    return {
        "revision": catalog.revision,
        "id": selection.identifier,
        "selection": selection.model_dump(mode="json"),
        "sessions": sessions,
        "triage": triage.model_dump(mode="json"),
        "warnings": _merged_warnings(None, triage),
    }


def _triage(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    catalog = record_triage(
        store,
        root,
        args.reference,
        triage_decision(
            store.resolve(args.session),
            args.kind,
            args.grade,
            chosen=args.decision == "chosen",
            reason=args.reason,
        ),
        expected_revision=args.expect_revision,
    )
    selection = selection_for(catalog, args.reference)
    triage = triage_evidence(store, catalog, selection)
    return {
        "revision": catalog.revision,
        "id": selection.identifier,
        "name": selection.name,
        "triage": triage.model_dump(mode="json"),
        "warnings": _merged_warnings(None, triage),
        "next": "Triage every examined session, then derive the voice from the chosen ones with dryheave-voice-profile.",
    }


def _evidence(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    selection = selection_for(catalog, args.reference)
    return evidence_page(
        store,
        selection,
        args.session,
        EvidenceQuery(
            offset=args.offset,
            limit=args.limit,
            text_offset=args.text_offset,
            text_limit=args.text_limit,
            kind=args.kind,
            event_id=args.event,
        ),
    )


def _metadata(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    selection = selection_for(read_catalog(_root()), args.reference)
    return metadata_page(
        store,
        selection,
        args.session,
        MetadataQuery(key=args.key, text_offset=args.text_offset, text_limit=args.text_limit),
    )


def _request_summary(request: ProblemRequest) -> dict[str, JsonValue]:
    return {
        "name": request.name,
        "id": request.identifier,
        "selection_id": request.selection_id,
        "description": request.description,
        "micro_bug": request.micro_bug,
        "target_maximum": request.target_maximum,
        "request_revision": request.revision,
        "decisions": {name: item.state for name, item in request.decisions.items()},
        "gaps": list(request.gaps),
    }


def _request_result(catalog: AuthoringCatalog, reference: str) -> dict[str, JsonValue]:
    request = request_for(catalog, reference)
    return {
        "revision": catalog.revision,
        "request": request.model_dump(mode="json"),
        "id": request.identifier,
        "target_maximum": request.target_maximum,
        "next": "Inspect bounded selection evidence, curate supported candidates and record rejections or gaps; never fill a quota.",
    }


def _request(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    name = args.name or f"request-{uuid4().hex[:12]}"
    catalog = create_request(
        root,
        name,
        args.selection,
        args.description,
        micro_bug=args.micro_bug,
        expected_revision=_expected(args, root),
    )
    return _request_result(catalog, name)


def _problems(_args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    return {
        "revision": catalog.revision,
        "requests": [_request_summary(item) for item in catalog.requests.values()],
    }


def _problem(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    return _request_result(read_catalog(_root()), args.reference)


def _record(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    session_id = store.resolve(args.session)
    decision = ProblemDecision(
        name=args.name,
        title=args.title,
        category=args.category,
        state=args.state,
        session_id=session_id,
        start_event=args.start,
        end_event=args.end,
        evidence=tuple(
            EvidenceExcerpt(
                session_id=session_id, event_id=event, excerpt=excerpt, visibility="curator"
            )
            for event, excerpt in args.evidence
        ),
        rationale=args.rationale,
        boundary_rationale=args.boundary_rationale,
        baseline_rationale=args.baseline_rationale,
        dirty_state=args.dirty_state,
        dirty_state_rationale=args.dirty_state_rationale,
        micro_bug_rationale=args.micro_bug_rationale,
        draft_path=_draft_reference(args.draft, root),
    )
    return _request_result(
        record_decision(
            store, root, args.reference, decision, expected_revision=args.expect_revision
        ),
        args.reference,
    )


def _draft_reference(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    absolute = path.absolute()
    return str(absolute.relative_to(root) if absolute.is_relative_to(root) else absolute)


def _gap(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    return _request_result(
        record_gap(_root(), args.reference, args.reason, expected_revision=args.expect_revision),
        args.reference,
    )


def _validate_problem(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    catalog = validate_problem(
        store,
        _root(),
        args.reference,
        args.name,
        expected_revision=args.expect_revision,
        freeze=args.problem_command == "freeze",
    )
    request = request_for(catalog, args.reference)
    decision = request.decisions[args.name]
    return {
        "revision": catalog.revision,
        "request_revision": request.revision,
        "decision": decision.model_dump(mode="json"),
        "valid": True,
        "id": decision.case_id,
        "next": "Calibrate the frozen case before subject spend."
        if decision.case_id
        else f"Freeze with problem freeze {request.name} {decision.name} --expect-revision {catalog.revision}.",
    }


def _evidence_warnings(evidence: VoiceEvidence | None) -> list[JsonValue]:
    if evidence is None or evidence.sufficient:
        return []
    coverage = "; ".join(f"{item.session_id}: {item.genuine}" for item in evidence.sessions)
    return [
        (
            f"Evidence below threshold: {evidence.genuine} genuine user messages from "
            f"{evidence.user_events} user-role events, against a threshold of {evidence.threshold}. "
            f"Genuine messages per selected session ({len(evidence.sessions)} selected): {coverage}. "
            "Name the sessions this voice was drawn from and add more varied sessions with "
            "collect select NAME FILE [FILE ...] --agent AGENT. "
            "Report the shortfall and do not assert voice traits the retained excerpts do not show."
        )
    ]


def _sampling_warnings(triage: TriageEvidence) -> list[JsonValue]:
    variety = triage.variety
    kinds = "; ".join(f"{name}: {count}" for name, count in sorted(variety.kinds.items())) or "none"
    spread = (
        "; ".join(f"{name}: {count}" for name, count in sorted(variety.repositories.items()))
        or "none"
    )
    if variety.unknown_repositories:
        spread = f"{spread}; no recorded cwd: {variety.unknown_repositories}"
    messages: list[JsonValue] = []
    if variety.untriaged:
        messages.append(
            f"Untriaged sessions: {variety.untriaged} of {variety.sessions} selected sessions "
            f"carry no triage decision ({', '.join(triage.untriaged)}). Judge each examined "
            "session with collect triage SELECTION --session ID --kind KIND [--grade GRADE] "
            "--decision chosen|rejected --reason REASON --expect-revision N. "
            "Until then the sampling frame is unrecorded and this voice's bias stays invisible."
        )
    if variety.triaged and not variety.chosen:
        messages.append(
            f"No chosen sessions: all {variety.triaged} triaged sessions were rejected, so no "
            "chosen set backs this voice. Triage the sessions its excerpts come from, or build "
            "the voice from a selection whose sessions were chosen."
        )
    elif variety.chosen and not variety.varied:
        messages.append(
            f"Chosen sessions lack variety: {variety.chosen} chosen, kinds ({kinds}) across "
            f"repositories ({spread}). A voice drawn from one kind of work or one repository "
            "describes that work, not the user. Triage further sessions of other kinds and from "
            "other repositories, then build the voice from a wider selection."
        )
    return messages


def _merged_warnings(
    evidence: VoiceEvidence | None, triage: TriageEvidence | None
) -> list[JsonValue]:
    return _evidence_warnings(evidence) + (_sampling_warnings(triage) if triage else [])


def _voice_draft(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    catalog = read_catalog(root)
    selection = selection_for(catalog, args.selection)
    name = resolve_voice_name(catalog, args.name)
    draft = VoiceDraft(
        selection_id=selection.identifier,
        persona=Persona(name=name, instructions="", disclosure_policy="", unknown_answer_policy=""),
    )
    path = args.out or root / f"voice-{uuid4().hex[:12]}.json"
    atomic_write(path.absolute(), draft.model_dump_json(indent=2).encode() + b"\n", replace=False)
    evidence = voice_evidence(store, selection)
    triage = triage_evidence(store, catalog, selection)
    return {
        "path": str(path),
        "name": name,
        "revision": catalog.revision,
        "selection_id": selection.identifier,
        "evidence": evidence.model_dump(mode="json"),
        "warnings": _merged_warnings(evidence, triage),
        "triage": triage.model_dump(mode="json"),
        "next": "Use dryheave-voice-profile to curate policies, exact user excerpts and safety_review, then voice create [NAME] PATH --expect-revision REVISION; omit NAME for the default voice.",
    }


def _voice_create(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    draft = parse_model(read_bytes(args.path, limit=1024 * 1024), VoiceDraft)
    name = resolve_voice_name(read_catalog(root), args.name)
    catalog = create_voice(store, root, name, draft, expected_revision=args.expect_revision)
    record = catalog.voices[name]
    return {
        "revision": catalog.revision,
        "voice": record.model_dump(mode="json"),
        "name": name,
        "id": record.persona_id,
        "evidence": record.evidence.model_dump(mode="json") if record.evidence else None,
        "warnings": _merged_warnings(record.evidence, record.triage),
        "triage": record.triage.model_dump(mode="json") if record.triage else None,
    }


def _voice_delete(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    catalog, record = delete_voice(_root(), args.name, expected_revision=args.expect_revision)
    return {
        "revision": catalog.revision,
        "name": record.name,
        "id": record.persona_id,
        "deleted": True,
        "next": f"Only the catalog entry was removed. The frozen persona object {record.persona_id} stays in the store, so every case and experiment that froze it keeps its original behavior. Curate a replacement with voice draft --selection NAME_OR_ID --name NEW_NAME.",
    }


def _voices(_args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    return {
        "revision": catalog.revision,
        "voices": [item.model_dump(mode="json") for item in catalog.voices.values()],
    }


def _voice(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    result = inspect_voice(store, catalog, args.reference)
    record = voice_for(catalog, args.reference)
    result["warnings"] = _merged_warnings(record.evidence, record.triage)
    return result


def register_mining_collection(
    registry: CommandRegistry, commands: "argparse._SubParsersAction[argparse.ArgumentParser]"
) -> None:
    select = commands.add_parser(
        "select", help="Pin explicitly selected files and imported sessions."
    )
    select.add_argument("name")
    select.add_argument("files", type=Path, nargs="*")
    select.add_argument("--session", action="append", default=[])
    select.add_argument("--agent", choices=["codex", "claude"])
    select.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    select.add_argument("--max-events", type=int, default=20000)
    select.add_argument("--expect-revision", type=int)
    registry.handler(select, _select)
    selections = commands.add_parser("selections")
    registry.handler(selections, _selections)
    selection = commands.add_parser("selection")
    selection.add_argument("reference")
    registry.handler(selection, _selection)
    evidence = commands.add_parser(
        "evidence",
        help="Read bounded curator evidence, including actual user messages and tool context.",
    )
    evidence.add_argument("reference")
    evidence.add_argument("--session", required=True)
    evidence.add_argument("--offset", type=int, default=0)
    evidence.add_argument("--limit", type=int, default=20)
    evidence.add_argument("--text-offset", type=int, default=0)
    evidence.add_argument("--text-limit", type=int, default=4000)
    evidence.add_argument("--event")
    evidence.add_argument(
        "--kind",
        choices=["all", "user", "assistant", "tool_call", "tool_result", "metadata", "lifecycle"],
        default="all",
    )
    registry.handler(evidence, _evidence)
    metadata = commands.add_parser(
        "metadata", help="Page selected session metadata, including recorded cwd and git baseline."
    )
    metadata.add_argument("reference")
    metadata.add_argument("--session", required=True)
    metadata.add_argument("--key")
    metadata.add_argument("--text-offset", type=int, default=0)
    metadata.add_argument("--text-limit", type=int, default=4000)
    registry.handler(metadata, _metadata)
    triage = commands.add_parser(
        "triage",
        help="Record one selected session's judged kind and a chosen/rejected sampling decision.",
    )
    triage.add_argument("reference")
    triage.add_argument("--session", required=True)
    triage.add_argument(
        "--kind", choices=["config_change", "feature", "bugfix", "extraneous"], required=True
    )
    triage.add_argument("--grade", choices=["small", "medium", "large", "easy", "hard"])
    triage.add_argument("--decision", choices=["chosen", "rejected"], required=True)
    triage.add_argument("--reason", required=True)
    triage.add_argument("--expect-revision", type=int, required=True)
    registry.handler(triage, _triage)


def register_mining(registry: CommandRegistry) -> None:
    problem = registry.add(
        "problem", help_text="Request and curate problems from selected imported evidence."
    )
    commands = problem.add_subparsers(dest="problem_command", required=True)
    request = commands.add_parser("request")
    request.add_argument("description", nargs="?", default="")
    request.add_argument("--selection", required=True)
    request.add_argument("--name")
    request.add_argument("--micro-bug", action="store_true")
    request.add_argument("--expect-revision", type=int)
    registry.handler(request, _request)
    listing = commands.add_parser("list")
    registry.handler(listing, _problems)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _problem)
    record = commands.add_parser("record")
    record.add_argument("reference")
    record.add_argument("name")
    for flag in (
        "title",
        "category",
        "session",
        "start",
        "end",
        "rationale",
        "boundary-rationale",
        "baseline-rationale",
        "dirty-state-rationale",
    ):
        record.add_argument(f"--{flag}", required=True)
    record.add_argument("--state", choices=["rejected", "unresolved", "drafted"], required=True)
    record.add_argument("--dirty-state", choices=["unknown", "clean", "patch"], required=True)
    record.add_argument("--micro-bug-rationale")
    record.add_argument(
        "--evidence", nargs=2, metavar=("EVENT", "EXCERPT"), action="append", required=True
    )
    record.add_argument("--draft", type=Path)
    record.add_argument("--expect-revision", type=int, required=True)
    registry.handler(record, _record)
    gap = commands.add_parser("gap")
    gap.add_argument("reference")
    gap.add_argument("reason")
    gap.add_argument("--expect-revision", type=int, required=True)
    registry.handler(gap, _gap)
    for name in ("validate", "freeze"):
        parser = commands.add_parser(name)
        parser.add_argument("reference")
        parser.add_argument("name")
        parser.add_argument("--expect-revision", type=int, required=True)
        registry.handler(parser, _validate_problem)
    _register_voices(registry)


def _register_voices(registry: CommandRegistry) -> None:
    voice = registry.add(
        "voice", help_text="Curate a reusable voice from selected actual user messages."
    )
    commands = voice.add_subparsers(dest="voice_command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("--selection", required=True)
    draft.add_argument("--name")
    draft.add_argument("--out", type=Path)
    registry.handler(draft, _voice_draft)
    create = commands.add_parser("create")
    create.add_argument("name", nargs="?")
    create.add_argument("path", type=Path)
    create.add_argument("--expect-revision", type=int, required=True)
    registry.handler(create, _voice_create)
    delete = commands.add_parser(
        "delete", help="Discard a catalog voice entry; its frozen persona object is retained."
    )
    delete.add_argument("name")
    delete.add_argument("--expect-revision", type=int, required=True)
    registry.handler(delete, _voice_delete)
    listing = commands.add_parser("list")
    registry.handler(listing, _voices)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _voice)
