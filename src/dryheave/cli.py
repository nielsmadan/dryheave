import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from pydantic import JsonValue, ValidationError

from dryheave.authoring_cli import register_authoring
from dryheave.commands import ArgumentParser, CommandHandler, CommandRegistrar, CommandRegistry
from dryheave.constants import VERSION
from dryheave.errors import DryheaveError, InputError
from dryheave.profile_cli import register_profiles
from dryheave.runner_cli import register_runner
from dryheave.serialization import validation_message
from dryheave.storage import ObjectStore, default_store_path


def _path(_args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return {"path": str(store.root)}


def _inspect(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    identifier = store.resolve(args.reference)
    return {"id": identifier, "manifest": store.get(identifier).model_dump(mode="json")}


def _verify(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    identifier = store.resolve(args.reference)
    manifest = store.verify(identifier)
    return {"id": identifier, "kind": manifest.kind.value, "verified": True}


def _aliases(_args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return {"aliases": dict(store.aliases())}


def _alias_set(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    store.set_alias(args.name, args.object_id, replace=args.replace)
    return {"name": args.name, "id": args.object_id}


def _alias_delete(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    store.delete_alias(args.name)
    return {"deleted": args.name}


def register_store(registry: CommandRegistry) -> None:
    parser = registry.add("store", help_text="Inspect and verify stored artifacts and aliases.")
    commands = parser.add_subparsers(dest="store_command", required=True)
    location = commands.add_parser("path", help="Show the selected store path.")
    registry.handler(location, _path)
    for name, handler, text in (
        ("inspect", _inspect, "Read a verified object manifest."),
        ("verify", _verify, "Check an object and all referenced content."),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("reference", help="Immutable object ID or alias.")
        registry.handler(command, handler)
    aliases = commands.add_parser("alias", help="Manage names pointing to immutable objects.")
    actions = aliases.add_subparsers(dest="alias_action", required=True)
    listing = actions.add_parser("list", help="List aliases.")
    registry.handler(listing, _aliases)
    setting = actions.add_parser("set", help="Create an alias, or explicitly replace its target.")
    setting.add_argument("name")
    setting.add_argument("object_id")
    setting.add_argument("--replace", action="store_true")
    registry.handler(setting, _alias_set)
    deleting = actions.add_parser("delete", help="Remove an alias, preserving its object.")
    deleting.add_argument("name")
    registry.handler(deleting, _alias_delete)


def build_parser(registrars: Sequence[CommandRegistrar] = ()) -> ArgumentParser:
    parser = ArgumentParser(
        prog="dryheave", description="Frozen inputs and evidence for coding-agent benchmarks."
    )
    parser.add_argument("--version", action="version", version=f"dryheave {VERSION}")
    parser.add_argument("--store", type=Path, help="Store directory (accepted anywhere).")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON response.")
    registry = CommandRegistry(parser)
    register_store(registry)
    register_authoring(registry)
    register_profiles(registry)
    register_runner(registry)
    for registrar in registrars:
        registrar(registry)
    return parser


def _global_arguments(arguments: Sequence[str]) -> list[str]:
    globals_: list[str] = []
    remaining: list[str] = []
    iterator = iter(arguments)
    for argument in iterator:
        if argument == "--":
            remaining.extend([argument, *iterator])
            break
        if argument in {"--json"} or argument.startswith("--store="):
            globals_.append(argument)
        elif argument == "--store":
            globals_.append(argument)
            try:
                value = next(iterator)
            except StopIteration as error:
                raise InputError("--store requires a path.") from error
            if value.startswith("--"):
                raise InputError("--store requires a path; use --store=PATH for a leading dash.")
            globals_.append(value)
        else:
            remaining.append(argument)
    return globals_ + remaining


def _emit(value: dict[str, JsonValue], *, stream: TextIO, structured: bool) -> None:
    if structured:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False), file=stream)
    else:
        data = value.get("data")
        if isinstance(data, dict) and set(data) == {"path"}:
            print(data["path"], file=stream)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def main(argv: Sequence[str] | None = None, *, registrars: Sequence[CommandRegistrar] = ()) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    structured = "--json" in arguments
    try:
        parser = build_parser(registrars)
        if not arguments:
            parser.print_help()
            return 0
        args = parser.parse_args(_global_arguments(arguments))
        store = ObjectStore(args.store if args.store is not None else default_store_path())
        handler: CommandHandler = args.handler
        result = handler(args, store)
        _emit({"ok": True, "data": result}, stream=sys.stdout, structured=args.json)
    except (DryheaveError, ValidationError, OSError) as error:
        if isinstance(error, DryheaveError):
            code, message, exit_code = error.code, str(error), error.exit_code
        elif isinstance(error, ValidationError):
            code, message, exit_code = "invalid_input", validation_message(error), 2
        else:
            code, message, exit_code = "io_error", str(error), 1
        if structured:
            print(
                json.dumps({"ok": False, "error": {"code": code, "message": message}}),
                file=sys.stderr,
            )
        else:
            print(f"dryheave: {message}", file=sys.stderr)
        return exit_code
    return 0
