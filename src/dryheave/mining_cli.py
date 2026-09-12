import argparse
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from dryheave.authoring_catalog import (
    AuthoringCatalog,
    ProblemDecision,
    ProblemRequest,
    VoiceDraft,
    read_catalog,
    request_for,
    selection_for,
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
    evidence_page,
    inspect_voice,
    metadata_page,
    record_decision,
    record_gap,
    select_logs,
    validate_problem,
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
    sessions: list[JsonValue] = []
    for identifier in selection.session_ids:
        session = store.load(identifier, Session, kind=ObjectKind.SESSION)
        sessions.append(
            {
                "id": identifier,
                "agent": session.agent.value,
                "events": len(session.events),
                "source_id": session.source_id,
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


def _voice_draft(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    catalog = read_catalog(root)
    selection = selection_for(catalog, args.selection)
    draft = VoiceDraft(
        selection_id=selection.identifier,
        persona=Persona(
            name=args.name, instructions="", disclosure_policy="", unknown_answer_policy=""
        ),
    )
    path = args.out or root / f"voice-{uuid4().hex[:12]}.json"
    atomic_write(path.absolute(), draft.model_dump_json(indent=2).encode() + b"\n", replace=False)
    return {
        "path": str(path),
        "revision": catalog.revision,
        "selection_id": selection.identifier,
        "next": "Use dryheave-voice-profile to curate policies, exact user excerpts and safety_review, then voice create NAME PATH --expect-revision REVISION.",
    }


def _voice_create(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    root = _root()
    draft = parse_model(read_bytes(args.path, limit=1024 * 1024), VoiceDraft)
    catalog = create_voice(store, root, args.name, draft, expected_revision=args.expect_revision)
    return {
        "revision": catalog.revision,
        "voice": catalog.voices[args.name].model_dump(mode="json"),
        "id": catalog.voices[args.name].persona_id,
    }


def _voices(_args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    catalog = read_catalog(_root())
    return {
        "revision": catalog.revision,
        "voices": [item.model_dump(mode="json") for item in catalog.voices.values()],
    }


def _voice(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return inspect_voice(store, read_catalog(_root()), args.reference)


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
    draft.add_argument("--name", default="Curated user")
    draft.add_argument("--out", type=Path)
    registry.handler(draft, _voice_draft)
    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("path", type=Path)
    create.add_argument("--expect-revision", type=int, required=True)
    registry.handler(create, _voice_create)
    listing = commands.add_parser("list")
    registry.handler(listing, _voices)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _voice)
