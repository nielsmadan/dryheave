import argparse
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.errors import InputError
from dryheave.experiments import (
    create_experiment,
    load_experiment,
    read_experiment,
    validate_experiment,
)
from dryheave.runner import load_capture, run_experiment, run_status
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore


def _validate(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    pinned = validate_experiment(store, read_experiment(args.path))
    return {
        "valid": True,
        "resolved": pinned.model_dump(mode="json"),
        "trial_count": len(pinned.cases) * len(pinned.variants) * pinned.repetitions,
    }


def _create(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    identifier = create_experiment(store, read_experiment(args.path))
    if args.name:
        store.set_alias(args.name, identifier)
    return {
        "id": identifier,
        "experiment": load_experiment(store, identifier).model_dump(mode="json"),
    }


def _inspect(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return {
        "id": store.resolve(args.reference),
        "experiment": load_experiment(store, args.reference).model_dump(mode="json"),
    }


def _run(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    if args.status:
        if (
            args.reference
            or args.resume
            or args.retry
            or args.mode
            or args.runtime_root
            or args.tui_test
            or args.strict
        ):
            raise InputError("Run status cannot be combined with execution options.")
        return run_status(store, args.status).model_dump(mode="json")
    options = None
    if not args.resume or args.mode or args.runtime_root or args.tui_test or args.strict:
        options = RunOptions.model_validate(
            {
                "mode": args.mode or "native",
                "strict": args.strict,
                "runtime_root": str(args.runtime_root) if args.runtime_root else None,
                "transport": str(args.tui_test) if args.tui_test else None,
            }
        )
    return run_experiment(
        store, args.reference, resume=args.resume, retry=tuple(args.retry), options=options
    ).model_dump(mode="json")


def _capture(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return {
        "id": store.resolve(args.reference),
        "capture": load_capture(store, args.reference).model_dump(mode="json"),
    }


def register_runner(registry: CommandRegistry) -> None:
    parser = registry.add(
        "experiment", help_text="Validate and freeze a repeatable experiment matrix."
    )
    commands = parser.add_subparsers(dest="experiment_command", required=True)
    for name, handler in (("validate", _validate), ("create", _create)):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
        if name == "create":
            command.add_argument("--name")
        registry.handler(command, handler)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _inspect)
    run = registry.add(
        "run",
        help_text="Execute native or explicit offline trials, resume, retry or inspect status.",
    )
    run.add_argument("reference", nargs="?")
    run.add_argument("--resume")
    run.add_argument("--retry", action="append", default=[], metavar="TRIAL_ID")
    run.add_argument("--status", metavar="RUN_ID")
    run.add_argument("--mode", choices=["native", "offline-fixture"])
    run.add_argument("--strict", action="store_true")
    run.add_argument("--runtime-root", type=Path)
    run.add_argument("--tui-test", type=Path)
    registry.handler(run, _run)
    capture = registry.add(
        "capture", help_text="Inspect immutable final files and pending-assessment evidence."
    )
    capture.add_argument("reference")
    registry.handler(capture, _capture)
