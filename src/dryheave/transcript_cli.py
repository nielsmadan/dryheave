import argparse
import os
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from dryheave.authoring_catalog import read_catalog, voice_for
from dryheave.cases import load_frozen_case
from dryheave.commands import CommandRegistry
from dryheave.errors import InputError
from dryheave.journals import RunStore
from dryheave.personas import load_frozen_persona
from dryheave.replay import ReplayOverrides, render_replay, replay_attempt
from dryheave.runner_state import ExecutionJournal
from dryheave.storage import ObjectStore
from dryheave.transcript import load_material, render_lanes, review_lanes, run_refs
from dryheave.transcript_models import RunTranscript
from dryheave.workspaces import Workspace


def _transcript(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    execution = ExecutionJournal(RunStore(store.root).inspect(args.run_id))
    attempts = (args.attempt,) if args.attempt else tuple(execution.attempts)
    events = execution.journal.events
    transcript = RunTranscript(
        run_id=execution.journal.metadata.run_id,
        attempts=tuple(
            review_lanes(load_material(store, run_refs(store, execution, attempt_id), events))
            for attempt_id in attempts
        ),
    )
    if args.json:
        return transcript.model_dump(mode="json")
    if not transcript.attempts:
        return {"text": f"Run {transcript.run_id} has no attempts yet."}
    return {"text": "\n\n".join(render_lanes(item) for item in transcript.attempts)}


def _voice(store: ObjectStore, workspace: Workspace | None, reference: str) -> str:
    if workspace is not None:
        catalog = read_catalog(workspace.path("authoring"))
        if any(reference in {item.name, item.persona_id} for item in catalog.voices.values()):
            return voice_for(catalog, reference).persona_id
    return store.resolve(reference)


def _overrides(args: argparse.Namespace, store: ObjectStore) -> ReplayOverrides:
    voice_id = _voice(store, args.benchmark_workspace, args.voice) if args.voice else None
    case_id = store.resolve(args.case) if args.case else None
    return ReplayOverrides(
        voice_id=voice_id,
        persona=load_frozen_persona(store, voice_id) if voice_id else None,
        case_id=case_id,
        case=load_frozen_case(store, case_id) if case_id else None,
    )


def _default_replay(workspace: Workspace | None) -> Path:
    identifier = uuid4().hex[:12]
    if workspace is None:
        return Path.cwd() / f"replay-{identifier}"
    return workspace.replays_dir / identifier


def _replay(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    if args.call is None and not args.all_stand_in:
        raise InputError("Choose --call ID or --all-stand-in.")
    workspace = args.benchmark_workspace
    out = args.out or _default_replay(workspace)
    report = replay_attempt(
        store,
        args.run_id,
        args.attempt,
        call_id=args.call,
        overrides=_overrides(args, store),
        out=out,
        inherited=os.environ,
    )
    if args.json:
        return report.model_dump(mode="json")
    return {"text": render_replay(report)}


def register_transcript(registry: CommandRegistry) -> None:
    transcript = registry.add(
        "transcript",
        help_text="Print an attempt's conversation, what happened behind the scenes, and the "
        "gaps left at the end; reads the run without taking its lock.",
    )
    transcript.add_argument("run_id", help="Run ID.")
    transcript.add_argument("--attempt", help="Only this attempt (default: every attempt).")
    registry.handler(transcript, _transcript)
    replay = registry.add(
        "replay",
        help_text="Re-send stored stand-in or inspector requests of an attempt, optionally with "
        "another voice or case, and show old output next to new. Writes only to --out.",
    )
    replay.add_argument("run_id", help="Run ID.")
    replay.add_argument("--attempt", required=True, help="Attempt whose calls are re-sent.")
    selection = replay.add_mutually_exclusive_group()
    selection.add_argument("--call", help="One stand-in (c-…) or inspector (j-…) call ID.")
    selection.add_argument(
        "--all-stand-in", action="store_true", help="Every stand-in call of the attempt."
    )
    replay.add_argument(
        "--voice", help="Voice name, persona ID or alias that replaces the stand-in's persona."
    )
    replay.add_argument(
        "--case",
        help="Case ID or alias whose facts replace the stored ones (inspector: fact checks).",
    )
    replay.add_argument(
        "--out",
        type=Path,
        help="New output directory (default: replays/<id> in a workspace, else ./replay-<id>); "
        "must not exist and must be outside the store.",
    )
    registry.handler(replay, _replay)
