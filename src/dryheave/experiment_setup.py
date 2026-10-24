import argparse
from pathlib import Path

from pydantic import JsonValue

from dryheave.claude_policy import CLAUDE_EFFORT_TEXT
from dryheave.codex_policy import CODEX_CONTROLLER_VERSION
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
from dryheave.models import EFFORTS, CommandSpec, EnvironmentReference
from dryheave.setup_helpers import runtime_references
from dryheave.storage import ObjectStore


def _stand_in(args: argparse.Namespace) -> ControllerRecipe:
    if args.stand_in == "none":
        if (
            args.stand_in_auth_file_env
            or args.stand_in_acknowledge_auth_source_writes
            or args.stand_in_environment
            or args.design_approval
            or args.stand_in_sandbox is not None
            or args.stand_in_model
            or args.stand_in_executable
            or args.stand_in_effort
            or args.stand_in_max_calls is not None
            or args.stand_in_call_seconds is not None
            or args.stand_in_total_seconds is not None
        ):
            raise InputError("Stand-in options require --stand-in codex or claude.")
        return ControllerRecipe(budget=RoleBudget(max_calls=0))
    claude = args.stand_in == "claude"
    if claude and (
        args.stand_in_auth_file_env
        or args.stand_in_acknowledge_auth_source_writes
        or args.stand_in_sandbox is not None
    ):
        raise InputError("Claude stand-in does not accept Codex sandbox or auth-file options.")
    if claude and not args.stand_in_model:
        raise InputError("Claude stand-in requires an explicit --stand-in-model.")
    environment = list(args.stand_in_environment)
    if claude and "CLAUDE_CODE_OAUTH_TOKEN" not in environment:
        environment.append("CLAUDE_CODE_OAUTH_TOKEN")
    executable = args.stand_in_executable or args.stand_in
    if "/" in executable:
        executable = str(Path(executable).absolute())
    return ControllerRecipe(
        kind=args.stand_in,
        version=None if claude else CODEX_CONTROLLER_VERSION,
        model=args.stand_in_model or "gpt-5.6-terra",
        effort=args.stand_in_effort if claude else args.stand_in_effort or "low",
        isolation="trusted-native",
        conversation_policy="design-approval" if args.design_approval else "facts-only",
        command=CommandSpec(
            argv=(executable,),
            timeout_seconds=args.stand_in_call_seconds or 30,
            environment=tuple(EnvironmentReference(name=name) for name in environment),
        ),
        runtime=ClaudeControllerRuntime()
        if claude
        else ControllerRuntime(
            sandbox=args.stand_in_sandbox or "read-only",
            runtime_files=runtime_references(
                args.stand_in_auth_file_env,
                acknowledge=args.stand_in_acknowledge_auth_source_writes,
            ),
        ),
        budget=RoleBudget(
            max_calls=args.stand_in_max_calls if args.stand_in_max_calls is not None else 3,
            call_seconds=args.stand_in_call_seconds
            if args.stand_in_call_seconds is not None
            else 30,
            total_seconds=args.stand_in_total_seconds
            if args.stand_in_total_seconds is not None
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
        stand_in=_stand_in(args),
    )
    identifier = create_experiment(store, draft)
    store.set_alias(args.name, identifier, replace=args.replace_alias)
    return {
        "id": identifier,
        "name": args.name,
        "experiment": load_experiment(store, identifier).model_dump(mode="json"),
        "stand_in": draft.stand_in.model_dump(mode="json"),
        "next": f"Calibrate each frozen case with dryheave case calibrate CASE, then dryheave run {args.name} --workflow --assess (without --workflow the run skips the inspection loop). Retain the returned run ID for dryheave assess RUN_ID or dryheave run --resume RUN_ID.",
    }


def register_experiment_setup(
    commands: "argparse._SubParsersAction[argparse.ArgumentParser]", registry: CommandRegistry
) -> None:
    parser = commands.add_parser(
        "setup",
        help="Freeze a named experiment using profiles and a bounded stand-in, without a JSON draft.",
    )
    parser.add_argument("name")
    parser.add_argument("--case", action="append", required=True, metavar="CASE")
    parser.add_argument("--profile", action="append", required=True, metavar="VARIANT=PROFILE")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--stand-in",
        choices=["none", "codex", "claude"],
        required=True,
        help=f"Explicitly select no replies, Codex {CODEX_CONTROLLER_VERSION} or tool-disabled Claude.",
    )
    parser.add_argument("--stand-in-executable")
    parser.add_argument("--stand-in-model")
    parser.add_argument(
        "--stand-in-effort",
        choices=EFFORTS,
        help=f"Stand-in effort; Claude accepts {CLAUDE_EFFORT_TEXT}.",
    )
    parser.add_argument("--stand-in-max-calls", type=int)
    parser.add_argument("--stand-in-call-seconds", type=int)
    parser.add_argument("--stand-in-total-seconds", type=int)
    parser.add_argument(
        "--stand-in-sandbox", choices=["read-only", "workspace-write", "danger-full-access"]
    )
    parser.add_argument("--stand-in-environment", action="append", default=[], metavar="NAME")
    parser.add_argument("--stand-in-auth-file-env", metavar="NAME")
    parser.add_argument("--stand-in-acknowledge-auth-source-writes", action="store_true")
    parser.add_argument(
        "--design-approval",
        action="store_true",
        help="Freeze authority for ordinary in-scope conversational design approval; never native permission/auth/trust approval.",
    )
    parser.add_argument("--replace-alias", action="store_true")
    registry.handler(parser, _setup)
