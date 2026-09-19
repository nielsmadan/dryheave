import argparse
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.controller_models import (
    ClaudeControllerRuntime,
    ControllerRecipe,
    ControllerRuntime,
    RoleBudget,
)
from dryheave.errors import InputError
from dryheave.experiment_models import ExperimentDraft, VariantSpec
from dryheave.experiments import create_experiment, load_experiment
from dryheave.models import CommandSpec, EnvironmentReference
from dryheave.setup_helpers import runtime_references
from dryheave.storage import ObjectStore


def _simulator(args: argparse.Namespace) -> ControllerRecipe:
    if args.simulator == "none":
        if (
            args.simulator_auth_file_env
            or args.simulator_acknowledge_auth_source_writes
            or args.simulator_environment
            or args.design_approval
            or args.simulator_sandbox is not None
            or args.simulator_model
            or args.simulator_executable
            or args.simulator_effort
            or args.simulator_max_calls is not None
            or args.simulator_call_seconds is not None
            or args.simulator_total_seconds is not None
        ):
            raise InputError("Simulator options require --simulator codex or claude.")
        return ControllerRecipe(budget=RoleBudget(max_calls=0))
    claude = args.simulator == "claude"
    if claude and (
        args.simulator_auth_file_env
        or args.simulator_acknowledge_auth_source_writes
        or args.simulator_sandbox is not None
    ):
        raise InputError("Claude simulator does not accept Codex sandbox or auth-file options.")
    if claude and not args.simulator_model:
        raise InputError("Claude simulator requires an explicit --simulator-model.")
    environment = list(args.simulator_environment)
    if claude and "CLAUDE_CODE_OAUTH_TOKEN" not in environment:
        environment.append("CLAUDE_CODE_OAUTH_TOKEN")
    executable = args.simulator_executable or args.simulator
    if "/" in executable:
        executable = str(Path(executable).absolute())
    return ControllerRecipe(
        kind=args.simulator,
        version="2.1.278" if claude else "0.154.0",
        model=args.simulator_model or "gpt-5.6-terra",
        effort=args.simulator_effort if claude else args.simulator_effort or "low",
        isolation="trusted-native",
        conversation_policy="design-approval" if args.design_approval else "facts-only",
        command=CommandSpec(
            argv=(executable,),
            timeout_seconds=args.simulator_call_seconds or 30,
            environment=tuple(EnvironmentReference(name=name) for name in environment),
        ),
        runtime=ClaudeControllerRuntime()
        if claude
        else ControllerRuntime(
            sandbox=args.simulator_sandbox or "read-only",
            runtime_files=runtime_references(
                args.simulator_auth_file_env,
                acknowledge=args.simulator_acknowledge_auth_source_writes,
            ),
        ),
        budget=RoleBudget(
            max_calls=args.simulator_max_calls if args.simulator_max_calls is not None else 3,
            call_seconds=args.simulator_call_seconds
            if args.simulator_call_seconds is not None
            else 30,
            total_seconds=args.simulator_total_seconds
            if args.simulator_total_seconds is not None
            else 90,
        ),
    )


def _setup(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    variants = []
    for selected in args.profile:
        name, separator, reference = selected.partition("=")
        if not separator or not name or not reference:
            raise InputError("Each --profile must be VARIANT=PROFILE_NAME_OR_ID.")
        variants.append(VariantSpec(name=name, profile=reference))
    draft = ExperimentDraft(
        name=args.name,
        cases=tuple(args.case),
        variants=tuple(variants),
        repetitions=args.repetitions,
        seed=args.seed,
        simulator=_simulator(args),
    )
    identifier = create_experiment(store, draft)
    store.set_alias(args.name, identifier, replace=args.replace_alias)
    return {
        "id": identifier,
        "name": args.name,
        "experiment": load_experiment(store, identifier).model_dump(mode="json"),
        "simulator": draft.simulator.model_dump(mode="json"),
        "next": f"Calibrate each frozen case with dryheave case calibrate CASE, then dryheave run {args.name} --assess. Retain the returned run ID for dryheave assess RUN_ID or dryheave run --resume RUN_ID.",
    }


def register_experiment_setup(
    commands: "argparse._SubParsersAction[argparse.ArgumentParser]", registry: CommandRegistry
) -> None:
    parser = commands.add_parser(
        "setup",
        help="Freeze a named experiment using profiles and a bounded simulator, without a JSON draft.",
    )
    parser.add_argument("name")
    parser.add_argument("--case", action="append", required=True, metavar="CASE")
    parser.add_argument("--profile", action="append", required=True, metavar="VARIANT=PROFILE")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--simulator",
        choices=["none", "codex", "claude"],
        required=True,
        help="Explicitly select no replies, Codex 0.154.0 or tool-disabled Claude 2.1.278.",
    )
    parser.add_argument("--simulator-executable")
    parser.add_argument("--simulator-model")
    parser.add_argument(
        "--simulator-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"]
    )
    parser.add_argument("--simulator-max-calls", type=int)
    parser.add_argument("--simulator-call-seconds", type=int)
    parser.add_argument("--simulator-total-seconds", type=int)
    parser.add_argument(
        "--simulator-sandbox", choices=["read-only", "workspace-write", "danger-full-access"]
    )
    parser.add_argument("--simulator-environment", action="append", default=[], metavar="NAME")
    parser.add_argument("--simulator-auth-file-env", metavar="NAME")
    parser.add_argument("--simulator-acknowledge-auth-source-writes", action="store_true")
    parser.add_argument(
        "--design-approval",
        action="store_true",
        help="Freeze authority for ordinary in-scope conversational design approval; never native permission/auth/trust approval.",
    )
    parser.add_argument("--replace-alias", action="store_true")
    registry.handler(parser, _setup)
