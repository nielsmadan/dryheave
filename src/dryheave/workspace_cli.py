import argparse
import io
import sys
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.storage import ObjectStore
from dryheave.workspace_git import choose_mode, require_git, setup_repository
from dryheave.workspaces import (
    CONFIG_FILE,
    GIT_COMMIT_MODES,
    GitCommitMode,
    initialize_workspace,
    read_workspace,
    resolve_transport,
)


def _existing_mode(path: Path) -> GitCommitMode | None:
    try:
        return read_workspace(path.absolute()).config.git_commit
    except FileNotFoundError:
        return None


def _mode(args: argparse.Namespace) -> GitCommitMode:
    stdin = io.StringIO() if args.json else sys.stdin
    return choose_mode(args.git_commit, _existing_mode(args.path), stdin, sys.stdout, sys.stderr)


def _initialize(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    mode = _mode(args)
    require_git(mode)
    workspace = initialize_workspace(
        args.path, runtime_root=args.runtime_root, tui_test=args.tui_test, git_commit=mode
    )
    git = setup_repository(workspace)
    transport = resolve_transport(None, workspace)
    return {
        "path": str(workspace.root),
        "config": str(workspace.root / CONFIG_FILE),
        "authoring": str(workspace.path("authoring")),
        "store": str(workspace.path("store")),
        "runtime": str(workspace.path("runtime")),
        "skills": str(workspace.path("skills")),
        "git": git,
        "tui_test": str(transport) if transport else None,
        "next": "Enter the workspace, run dryheave skills install (Claude: --target .claude/skills), then collect select NAME FILE --agent codex, --agent claude or --agent droid. "
        + (
            "Check transport with dryheave doctor."
            if transport
            else "Install tui-test 0.1.0-beta.3 on PATH or set tui_test in dryheave.toml before native runs."
        ),
    }


def register_workspace(registry: CommandRegistry) -> None:
    parser = registry.add("init", help_text="Initialize a private benchmark workspace.")
    parser.add_argument("path", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--runtime-root", type=Path, help="Short owned runtime directory; relative to PATH."
    )
    parser.add_argument(
        "--tui-test", type=Path, help="Native transport executable; relative to PATH."
    )
    parser.add_argument(
        "--git-commit",
        choices=GIT_COMMIT_MODES,
        help="Whether checkpoint commands commit the files they wrote: ask offers (default), "
        "auto commits, off never commits. Without it, a terminal shows a numbered prompt.",
    )
    registry.handler(parser, _initialize)
