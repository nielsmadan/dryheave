import argparse
from pathlib import Path
from threading import Event

from pydantic import JsonValue

from dryheave.assessments import assess_run
from dryheave.commands import CommandRegistry
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_setup import register_experiment_setup
from dryheave.experiments import (
    create_experiment,
    load_experiment,
    read_experiment,
    validate_experiment,
)
from dryheave.native_recovery import cleanup_resolved
from dryheave.runner import cancellation_signals, load_capture, run_experiment, run_status
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore
from dryheave.workspaces import Workspace, resolve_transport, validate_runtime_root


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
            or args.assess
        ):
            raise InputError("Run status cannot be combined with execution options.")
        return run_status(store, args.status).model_dump(mode="json")
    runtime_root = args.runtime_root
    workspace: Workspace | None = args.benchmark_workspace
    transport = args.tui_test
    if not args.resume and args.mode != "offline-fixture":
        if runtime_root is None and workspace is not None:
            runtime_root = workspace.path("runtime")
        if runtime_root is not None:
            validate_runtime_root(runtime_root)
        transport = resolve_transport(transport, workspace)
    options = None
    if not args.resume or args.mode or args.runtime_root or args.tui_test or args.strict:
        options = RunOptions.model_validate(
            {
                "mode": args.mode or "native",
                "strict": args.strict,
                "runtime_root": str(runtime_root) if runtime_root else None,
                "transport": str(transport) if transport else None,
            }
        )
    cancelled = Event()
    with cancellation_signals(cancelled):
        summary = run_experiment(
            store,
            args.reference,
            resume=args.resume,
            retry=tuple(args.retry),
            options=options,
            cancelled=cancelled,
        )
        if not args.assess:
            return summary.model_dump(mode="json")
        run_id = summary.run_id
        try:
            if cancelled.is_set():
                raise InputError("Capture was interrupted; assessment was not started.")
            if (
                summary.input_error
                or summary.unstarted_trials
                or any(
                    state.capture_id is None or state.capture_error for state in summary.attempts
                )
            ):
                raise InputError("Capture is incomplete; reconcile execution before assessment.")
            for state in summary.attempts:
                if state.capture_id is not None and not cleanup_resolved(
                    load_capture(store, state.capture_id).cleanup
                ):
                    raise InputError("Owned cleanup is unresolved; assessment was not started.")
            assessments = assess_run(store, run_id, cancelled=cancelled)
        except (DryheaveError, OSError, KeyboardInterrupt) as error:
            raise InputError(
                f"Run {run_id} retains its evidence. {str(error) or 'Assessment interrupted.'} "
                f"Use dryheave run --resume {run_id} to reconcile execution, or dryheave assess {run_id} for separate assessment."
            ) from error
        return run_status(store, run_id).model_dump(mode="json") | {
            "assessments": list(assessments)
        }


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
    register_experiment_setup(commands, registry)
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
    run.add_argument(
        "--assess",
        action="store_true",
        help="After capture and owned cleanup, run frozen assessment; may invoke configured judges.",
    )
    registry.handler(run, _run)
    capture = registry.add(
        "capture", help_text="Inspect immutable final files and pending-assessment evidence."
    )
    capture.add_argument("reference")
    registry.handler(capture, _capture)
