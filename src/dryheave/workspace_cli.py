import argparse
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.storage import ObjectStore
from dryheave.workspaces import CONFIG_FILE, initialize_workspace


def _initialize(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    workspace = initialize_workspace(args.path, runtime_root=args.runtime_root)
    return {
        "path": str(workspace.root),
        "config": str(workspace.root / CONFIG_FILE),
        "authoring": str(workspace.path("authoring")),
        "store": str(workspace.path("store")),
        "runtime": str(workspace.path("runtime")),
        "skills": str(workspace.path("skills")),
    }


def register_workspace(registry: CommandRegistry) -> None:
    parser = registry.add("init", help_text="Initialize a private benchmark workspace.")
    parser.add_argument("path", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--runtime-root", type=Path, help="Short owned runtime directory; relative to PATH."
    )
    registry.handler(parser, _initialize)
