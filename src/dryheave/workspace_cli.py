import argparse
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.storage import ObjectStore
from dryheave.workspaces import CONFIG_FILE, initialize_workspace, resolve_transport


def _initialize(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    workspace = initialize_workspace(
        args.path, runtime_root=args.runtime_root, tui_test=args.tui_test
    )
    transport = resolve_transport(None, workspace)
    return {
        "path": str(workspace.root),
        "config": str(workspace.root / CONFIG_FILE),
        "authoring": str(workspace.path("authoring")),
        "store": str(workspace.path("store")),
        "runtime": str(workspace.path("runtime")),
        "skills": str(workspace.path("skills")),
        "tui_test": str(transport) if transport else None,
        "next": "Enter the workspace, run dryheave skills install (Claude: --target .claude/skills), then collect select NAME FILE --agent codex or --agent claude. "
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
    registry.handler(parser, _initialize)
