import argparse
import json
import shlex
from pathlib import Path

from pydantic import JsonValue

from dryheave.calibrations import calibrate_case, load_calibration
from dryheave.cases import draft_case, freeze_case, load_frozen_case, read_draft, validate_case
from dryheave.commands import CommandRegistry
from dryheave.errors import CancelledError, InputError
from dryheave.filesystem import atomic_write, read_bytes
from dryheave.logs import claude, codex
from dryheave.logs.base import ImportLimits, Session, candidates, discover
from dryheave.logs.service import import_session
from dryheave.mining_cli import register_mining, register_mining_collection
from dryheave.models import AgentKind, ObjectKind, StrictModel
from dryheave.personas import Persona, freeze_persona, load_frozen_persona
from dryheave.repositories import capture_repository
from dryheave.serialization import parse_model
from dryheave.storage import ObjectStore

MAX_CALIBRATION_EVIDENCE_BYTES = 65536


def _write(path: Path, model: StrictModel) -> None:
    atomic_write(
        path.absolute(),
        (json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n").encode(),
        replace=False,
    )


def _limits(args: argparse.Namespace) -> ImportLimits:
    return ImportLimits(max_bytes=args.max_bytes, max_events=args.max_events)


def _scan(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    limits = _limits(args)
    parser = codex.parse if args.agent == "codex" else claude.parse
    found: list[JsonValue] = []
    for path in discover(args.root, limits):
        session = parser(path, limits)
        found.append(
            {
                "path": str(path),
                "source_id": session.source_id,
                "source_version": session.source_version,
                "events": len(session.events),
                "warnings": [item.model_dump(mode="json") for item in session.warnings],
                "candidates": [item.model_dump(mode="json") for item in candidates(session)],
            }
        )
    return {
        "sessions": found,
        "next": "Import a selected log with collect import PATH --agent AGENT.",
    }


def _import(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    identifier = import_session(store, args.path, AgentKind(args.agent), _limits(args))
    return {
        "id": identifier,
        "next": f"Create a case draft with case draft {identifier} --repo PATH --commit FULL_SHA --out case.json.",
    }


def _show(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    session = store.load(args.reference, Session, kind=ObjectKind.SESSION)
    return {
        "id": store.resolve(args.reference),
        "session": session.model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in candidates(session)],
    }


def _draft(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    if bool(args.repo) != bool(args.commit):
        raise InputError(
            "Supply --repo and --commit together; an unspecified baseline stays unresolved."
        )
    if args.initial_patch and not args.repo:
        raise InputError("--initial-patch requires --repo and --commit.")
    patch = read_bytes(args.initial_patch, limit=16 * 1024 * 1024) if args.initial_patch else None
    repository_id = (
        capture_repository(store, args.repo, args.commit, initial_patch=patch)
        if args.repo
        else None
    )
    draft = draft_case(
        store, args.reference, repository_id=repository_id, start=args.start, end=args.end
    )
    _write(args.out, draft)
    return {
        "path": str(args.out),
        "repository_id": repository_id,
        "next": "Curate intent, facts, persona and grading; clear resolved issues, then case validate PATH.",
    }


def _validate(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    issues = validate_case(store, read_draft(args.path), args.path.absolute().parent)
    if issues:
        raise InputError("; ".join(issues))
    return {"valid": True, "next": f"Freeze reviewed inputs with case freeze {args.path}."}


def _freeze(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    identifier = freeze_case(store, read_draft(args.path), args.path.absolute().parent)
    return {
        "id": identifier,
        "next": f"Calibrate before subject spend with case calibrate {identifier}.",
    }


def _case_inspect(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    case = load_frozen_case(store, args.reference)
    return {"id": store.resolve(args.reference), "case": case.model_dump(mode="json")}


def _calibrate(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    try:
        identifier = calibrate_case(store, args.reference)
    except KeyboardInterrupt as error:
        retry = shlex.join(
            ("dryheave", "--store", str(store.root), "case", "calibrate", args.reference)
        )
        raise CancelledError(
            f"Calibration interrupted; retained work is in {store.root / 'calibrations'}. Reconcile owned processes and start a fresh calibration with: {retry}"
        ) from error
    record = load_calibration(store, identifier)
    return {
        "id": identifier,
        "calibration": record.model_dump(mode="json"),
        "next": f"Inspect retained evidence with case calibration {identifier} --evidence PATH; review status before subject spend.",
    }


def _calibration(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    record = load_calibration(store, args.reference)
    result: dict[str, JsonValue] = {
        "id": store.resolve(args.reference),
        "calibration": record.model_dump(mode="json"),
    }
    if args.evidence:
        content = store.read_blob(args.reference, args.evidence)
        limit = args.limit
        if not 1 <= limit <= MAX_CALIBRATION_EVIDENCE_BYTES or args.offset < 0:
            raise InputError("Evidence byte offset must be nonnegative and limit must be 1-65536.")
        end = min(len(content), args.offset + limit)
        result["evidence"] = {
            "path": args.evidence,
            "sha256": record.files[args.evidence],
            "bytes": len(content),
            "offset": args.offset,
            "text": content[args.offset : end].decode("utf-8", errors="replace"),
            "next_offset": end if end < len(content) else None,
            "truncated": args.offset > 0 or end < len(content),
        }
    return result


def _persona_draft(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    draft = draft_case(store, args.reference)
    if draft.persona is None:
        raise InputError("No persona suggestion could be created.")
    _write(args.out, draft.persona)
    return {
        "path": str(args.out),
        "next": "Curate communication, disclosure, unknown-answer policy and safe examples; then persona create PATH.",
    }


def _persona_create(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    persona = parse_model(read_bytes(args.path, limit=1024 * 1024), Persona)
    return {"id": freeze_persona(store, persona)}


def _persona_inspect(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    persona = load_frozen_persona(store, args.reference)
    return {"id": store.resolve(args.reference), "persona": persona.model_dump(mode="json")}


def register_authoring(registry: CommandRegistry) -> None:
    collection = registry.add(
        "collect", help_text="Discover and import bounded coding-agent log evidence."
    )
    commands = collection.add_subparsers(dest="collect_command", required=True)
    for name, handler in (("scan", _scan), ("import", _import)):
        parser = commands.add_parser(name)
        parser.add_argument("--agent", choices=["codex", "claude"], required=True)
        parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
        parser.add_argument("--max-events", type=int, default=20000)
        if name == "scan":
            parser.add_argument("--root", type=Path, required=True)
        else:
            parser.add_argument("path", type=Path)
        registry.handler(parser, handler)
    show = commands.add_parser("show")
    show.add_argument("reference")
    registry.handler(show, _show)
    register_mining_collection(registry, commands)
    register_mining(registry)
    _register_cases(registry)
    _register_personas(registry)


def _register_cases(registry: CommandRegistry) -> None:
    cases = registry.add("case", help_text="Curate, validate and freeze reusable benchmark cases.")
    commands = cases.add_subparsers(dest="case_command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("reference")
    draft.add_argument("--repo", type=Path)
    draft.add_argument("--commit")
    draft.add_argument("--initial-patch", type=Path)
    draft.add_argument("--start", help="Starting user event ID.")
    draft.add_argument("--end", help="Last included event ID.")
    draft.add_argument("--out", type=Path, required=True)
    registry.handler(draft, _draft)
    for name, handler in (("validate", _validate), ("freeze", _freeze)):
        parser = commands.add_parser(name)
        parser.add_argument("path", type=Path)
        registry.handler(parser, handler)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _case_inspect)
    calibrate = commands.add_parser(
        "calibrate", help="Run bounded baseline/reference verifiers before subject spend."
    )
    calibrate.add_argument("reference", metavar="CASE_REF")
    registry.handler(calibrate, _calibrate)
    calibration = commands.add_parser(
        "calibration", help="Validate a standalone calibration and inspect its retained evidence."
    )
    calibration.add_argument("reference", metavar="CALIBRATION_REF")
    calibration.add_argument(
        "--evidence", metavar="PATH", help="Read a path from the calibration files map."
    )
    calibration.add_argument("--offset", type=int, default=0, help="Evidence byte offset.")
    calibration.add_argument(
        "--limit", type=int, default=4000, help="Evidence byte limit (maximum 65536)."
    )
    registry.handler(calibration, _calibration)


def _register_personas(registry: CommandRegistry) -> None:
    personas = registry.add(
        "persona", help_text="Author and inspect a separate evidence-backed simulated user."
    )
    commands = personas.add_subparsers(dest="persona_command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("reference")
    draft.add_argument("--out", type=Path, required=True)
    registry.handler(draft, _persona_draft)
    create = commands.add_parser("create")
    create.add_argument("path", type=Path)
    registry.handler(create, _persona_create)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _persona_inspect)
