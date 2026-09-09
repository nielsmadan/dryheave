import argparse
import os
from importlib.resources import files
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.doctor import runtime_doctor
from dryheave.errors import ConflictError, PathError
from dryheave.filesystem import atomic_write, directory_fd
from dryheave.skills import SKILL_NAMES, list_skills, manage_skills
from dryheave.storage import ObjectStore


def _list(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    return list_skills(args.target)


def _manage(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    return manage_skills(args.skills_command, args.target, tuple(args.names))


def _doctor(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    return runtime_doctor(args.tui_test, agent=args.agent, runtime_root=args.runtime_root)


def _example(args: argparse.Namespace, _store: ObjectStore) -> dict[str, JsonValue]:
    destination = args.target.absolute()
    if ".." in destination.parts:
        raise PathError("Select a fresh example directory without parent traversal.")
    with directory_fd(destination.parent, create=True) as parent:
        try:
            os.mkdir(destination.name, mode=0o700, dir_fd=parent)
        except FileExistsError as error:
            raise ConflictError(f"Select a fresh example directory: {destination}") from error
    for name in ("offline_demo.py", "real-workflow.md"):
        content = files("dryheave").joinpath("resources", "examples", name).read_bytes()
        atomic_write(destination / name, content, replace=False)
    return {"path": str(destination), "files": ["offline_demo.py", "real-workflow.md"]}


def register_operators(registry: CommandRegistry) -> None:
    parser = registry.add(
        "skills", help_text="Manage packaged operator skills in an explicit target."
    )
    commands = parser.add_subparsers(dest="skills_command", required=True)
    for action in ("list", "doctor", "install", "update", "uninstall"):
        command = commands.add_parser(action)
        command.add_argument("--target", type=Path, required=action != "list")
        if action in {"list", "doctor"}:
            registry.handler(command, _list)
        else:
            command.add_argument("names", nargs="*", choices=SKILL_NAMES)
            registry.handler(command, _manage)
    doctor = registry.add(
        "doctor", help_text="Check runtime tools without model calls or state discovery."
    )
    doctor.add_argument("--tui-test", default="tui-test")
    doctor.add_argument("--agent", choices=["codex", "claude"])
    doctor.add_argument("--runtime-root", type=Path)
    registry.handler(doctor, _doctor)
    example = registry.add(
        "example", help_text="Copy installed public CLI examples to a fresh directory."
    )
    actions = example.add_subparsers(dest="example_command", required=True)
    write = actions.add_parser("write")
    write.add_argument("--target", type=Path, required=True)
    registry.handler(write, _example)
