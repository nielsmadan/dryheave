import argparse
import io
import os
import sys
from pathlib import Path
from typing import Literal, TextIO

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.init_prompts import choose_install, choose_mode
from dryheave.storage import ObjectStore
from dryheave.transport_setup import (
    InstallTarget,
    fetch_install_target,
    install_tui_test,
    missing_transport_error,
    path_to_record,
    usable_transport,
)
from dryheave.tui_test_release import TUI_TEST_ACTION
from dryheave.workspace_git import require_git, setup_repository
from dryheave.workspaces import (
    CONFIG_FILE,
    GIT_COMMIT_MODES,
    Workspace,
    check_initialization,
    initialize_workspace,
    read_workspace,
    resolve_transport,
)

type TransportAction = Literal["explicit", "keep", "install", "ask", "skip"]


def _existing(path: Path) -> Workspace | None:
    try:
        return read_workspace(path.absolute())
    except FileNotFoundError:
        return None


def transport_action(
    *, explicit: bool, usable: bool, install: bool, interactive: bool
) -> TransportAction:
    if explicit:
        return "explicit"
    if usable:
        return "keep"
    if install:
        return "install"
    return "ask" if interactive else "skip"


def _install_target(action: TransportAction, *, stdin: TextIO, root: Path) -> InstallTarget | None:
    if action not in {"install", "ask"}:
        return None
    print("Checking GitHub for the newest tui-test release…", file=sys.stderr, flush=True)
    target = fetch_install_target(root)
    if action == "ask":
        choice = choose_install(target, stdin=stdin, output=sys.stderr, environment=os.environ)
        if choice == "manual":
            raise missing_transport_error(target)
    return target


def _initialize(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    check_initialization(args.path, runtime_root=args.runtime_root, tui_test=args.tui_test)
    existing = _existing(args.path)
    stdin: TextIO = io.StringIO() if args.json else sys.stdin
    interactive = stdin.isatty() and sys.stdout.isatty()
    root = Path.cwd()
    resolved = resolve_transport(None, existing)
    action = transport_action(
        explicit=args.tui_test is not None,
        usable=args.tui_test is None and resolved is not None and usable_transport(resolved, root),
        install=args.install_tui_test,
        interactive=interactive,
    )
    target = _install_target(action, stdin=stdin, root=root)
    mode = choose_mode(
        args.git_commit,
        existing.config.git_commit if existing else None,
        interactive=interactive,
        stdin=stdin,
        output=sys.stderr,
        environment=os.environ,
    )
    require_git(mode)
    installed = (
        install_tui_test(target, environment=os.environ, output=sys.stderr) if target else None
    )
    tui_test = (
        path_to_record(
            installed,
            resolve_transport(None, None),
            existing.config.tui_test if existing else None,
        )
        if installed
        else args.tui_test
    )
    workspace = initialize_workspace(
        args.path, runtime_root=args.runtime_root, tui_test=tui_test, git_commit=mode
    )
    git = setup_repository(workspace)
    transport = resolve_transport(None, workspace)
    ready = action in {"explicit", "keep"} or installed is not None
    return {
        "path": str(workspace.root),
        "config": str(workspace.root / CONFIG_FILE),
        "authoring": str(workspace.path("authoring")),
        "store": str(workspace.path("store")),
        "runtime": str(workspace.path("runtime")),
        "skills": str(workspace.path("skills")),
        "git": git,
        "tui_test": str(transport) if transport else None,
        "installed_tui_test": (
            {"path": str(installed), "version": target.version, "lookup": target.lookup}
            if installed and target
            else None
        ),
        "next": "Enter the workspace, run dryheave skills install (Claude: --target .claude/skills), then collect select NAME FILE --agent codex, --agent claude or --agent droid. "
        + (
            "Check transport with dryheave doctor."
            if ready
            else f"Before native runs: {TUI_TEST_ACTION}"
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
        "--install-tui-test",
        action="store_true",
        help="Install tui-test without asking when it is not found. Without it, a terminal asks.",
    )
    parser.add_argument(
        "--git-commit",
        choices=GIT_COMMIT_MODES,
        help="Whether checkpoint commands commit the files they wrote: ask offers (default), "
        "auto commits, off never commits. Without it, a terminal asks.",
    )
    registry.handler(parser, _initialize)
