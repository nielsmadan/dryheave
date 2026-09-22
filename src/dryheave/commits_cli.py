import argparse
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.commits import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_HISTORY_LIMIT,
    SessionMatch,
    SessionScan,
    resolve_commit,
    scan_sessions,
    survey_repository,
)
from dryheave.errors import InputError
from dryheave.logs.base import ImportLimits
from dryheave.models import AgentKind
from dryheave.storage import ObjectStore


def _scan(args: argparse.Namespace) -> SessionScan | None:
    if args.root is None:
        if args.agent is not None:
            raise InputError("--agent selects a parser for --root; supply the log root as well.")
        return None
    if args.agent is None:
        raise InputError("Scanning --root requires --agent codex or --agent claude.")
    return scan_sessions(
        args.root,
        AgentKind(args.agent),
        ImportLimits(max_bytes=args.max_bytes, max_events=args.max_events),
    )


def _survey(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    survey = survey_repository(
        args.repo,
        history_limit=args.max_commits,
        candidate_limit=args.max_candidates,
        scan=_scan(args),
    )
    return {
        "survey": survey.model_dump(mode="json"),
        "tier": survey.tier,
        "candidates": len(survey.candidates),
        "bounds": f"The newest {survey.history_limit} commits reachable from HEAD were read; at most {survey.candidate_limit} candidates are reported.",
        "next": "Resolve a remembered commit with collect commit SHA --repo PATH --root LOG_DIRECTORY --agent AGENT, then select its session and open a request with problem request.",
    }


def _commit(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    resolution = resolve_commit(args.repo, args.sha, scan=_scan(args))
    return {
        "resolution": resolution.model_dump(mode="json"),
        "commit": resolution.commit,
        "baseline": resolution.parent,
        "join": resolution.join,
        "next": _next(resolution.sessions, resolution.parent),
    }


def _next(sessions: tuple[SessionMatch, ...], parent: str | None) -> str:
    if not sessions:
        return "No session was joined to this commit; select the source log explicitly with collect select NAME FILE --agent AGENT before problem request, or record the missing session as a gap."
    return (
        "Select the matched session with collect select NAME SESSION_PATH --agent AGENT, then run "
        'problem request "DESCRIPTION" --selection NAME as usual. Verify the reported parent '
        f"{parent} against the read-only source repository before case draft --repo PATH --commit {parent}; "
        "today's HEAD is never a substitute."
    )


def _register_shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--root", type=Path, help="Log directory scanned for matching sessions.")
    parser.add_argument("--agent", choices=["codex", "claude"])
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--max-events", type=int, default=20000)


def register_commit_survey(
    registry: CommandRegistry, commands: "argparse._SubParsersAction[argparse.ArgumentParser]"
) -> None:
    survey = commands.add_parser(
        "survey",
        help="Survey a read-only repository's recent history for candidate benchmark commits.",
    )
    _register_shared(survey)
    survey.add_argument("--max-commits", type=int, default=DEFAULT_HISTORY_LIMIT)
    survey.add_argument("--max-candidates", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    registry.handler(survey, _survey)
    commit = commands.add_parser(
        "commit", help="Resolve one remembered commit to its baseline parent and session."
    )
    commit.add_argument("sha")
    _register_shared(commit)
    registry.handler(commit, _commit)
