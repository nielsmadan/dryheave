import argparse
import io
import os
import sys
from pathlib import Path
from typing import Literal, TextIO

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.init_prompts import choose_install, choose_mode
from dryheave.skills import refresh_skills
from dryheave.storage import ObjectStore
from dryheave.transport_setup import (
    InstallTarget,
    fetch_install_target,
    install_tui_test,
    missing_transport_error,
    path_to_record,
    usable_transport,
)
from dryheave.workspace_git import (
    GITIGNORE,
    UPDATE_MESSAGE,
    checkpoint,
    require_git,
    setup_repository,
    update_gitignore,
    workspace_commit,
)
from dryheave.workspace_summary import (
    InitOutcome,
    UpdateOutcome,
    render_init,
    render_update,
    repository_state,
)
from dryheave.workspaces import (
    CONFIG_FILE,
    GIT_COMMIT_MODES,
    Workspace,
    check_initialization,
    initialize_workspace,
    initialized_workspace,
    read_workspace,
    resolve_transport,
)

NO_WORKSPACE = "No dryheave.toml here or in a parent directory; run dryheave init first."

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
    cwd = Path.cwd()
    resolved = resolve_transport(None, existing)
    action = transport_action(
        explicit=args.tui_test is not None,
        usable=args.tui_test is None and resolved is not None and usable_transport(resolved, cwd),
        install=args.install_tui_test,
        interactive=interactive,
    )
    target = _install_target(action, stdin=stdin, root=cwd)
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
    skills = [refresh_skills(target) for target in workspace.skill_targets()]
    commit = workspace_commit(
        workspace,
        (
            workspace.root / CONFIG_FILE,
            workspace.root / GITIGNORE,
            *(path for item in skills for path in item.owned_paths),
        ),
        (path for item in skills for path in item.changed_paths),
    )
    transport = resolve_transport(None, workspace)
    outcome = InitOutcome(
        root=workspace.root,
        repository=repository_state(git["repository"]),
        commit=commit,
        skills=skills,
        tui_test=transport,
        installed_version=target.version if installed and target else None,
        ready=action in {"explicit", "keep"} or installed is not None,
    )
    if not args.json:
        return {"text": render_init(outcome, cwd)}
    return _init_json(workspace, git, outcome, installed, target) | commit


def _init_json(
    workspace: Workspace,
    git: dict[str, JsonValue],
    outcome: InitOutcome,
    installed: Path | None,
    target: InstallTarget | None,
) -> dict[str, JsonValue]:
    return {
        "path": str(workspace.root),
        "config": str(workspace.root / CONFIG_FILE),
        "authoring": str(workspace.path("authoring")),
        "store": str(workspace.path("store")),
        "runtime": str(workspace.path("runtime")),
        "skills": str(workspace.path("skills")),
        "skill_installs": [item.model_dump(mode="json") for item in outcome.skills],
        "git": git,
        "tui_test": str(outcome.tui_test) if outcome.tui_test else None,
        "installed_tui_test": (
            {"path": str(installed), "version": target.version, "lookup": target.lookup}
            if installed and target
            else None
        ),
        "next": outcome.next_text,
    }


def _update(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    workspace = initialized_workspace(missing=NO_WORKSPACE)
    added = update_gitignore(workspace)
    skills = tuple(refresh_skills(target) for target in workspace.skill_targets())
    paths = (
        *((workspace.root / GITIGNORE,) if added else ()),
        *(path for item in skills for path in item.changed_paths),
    )
    outcome = UpdateOutcome(
        path=workspace.root,
        gitignore_added=added,
        skill_installs=skills,
        commit=checkpoint(workspace, UPDATE_MESSAGE, paths),
    )
    return outcome.as_json() if args.json else {"text": render_update(outcome, Path.cwd())}


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
        "auto commits, off never commits. Init's first commit is made in ask and auto. "
        "Without it, a terminal asks.",
    )
    registry.handler(parser, _initialize)
    update = registry.add(
        "update",
        help_text="Bring this workspace's skills and .gitignore up to the installed dryheave, "
        "committing the changes as [git] commit says.",
    )
    registry.handler(update, _update)
