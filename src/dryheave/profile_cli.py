import argparse
import json
from pathlib import Path

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.errors import InputError
from dryheave.filesystem import read_bytes
from dryheave.profile_launch import materialize_profile, preflight_profile
from dryheave.profile_models import CaptureSpec, DeriveSpec
from dryheave.profiles import capture_profile, derive_profile, diff_profiles, load_profile
from dryheave.serialization import parse_json, parse_model
from dryheave.setup_helpers import capture_arguments, selected_capture
from dryheave.skills import SKILL_NAMES
from dryheave.storage import ObjectStore


def _capture(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    spec = parse_model(read_bytes(args.spec, limit=4 * 1024 * 1024), CaptureSpec)
    if "disabled_skill_names" not in spec.recipe.model_fields_set:
        spec = spec.model_copy(
            update={"recipe": spec.recipe.model_copy(update={"disabled_skill_names": SKILL_NAMES})}
        )
    if args.agent and args.agent != spec.recipe.agent.value:
        raise InputError(
            "Selected agent does not match the capture recipe; translation is unsupported."
        )
    identifier = capture_profile(store, spec, base=args.spec.absolute().parent)
    if args.name:
        store.set_alias(args.name, identifier, replace=args.replace_alias)
    return {
        "id": identifier,
        "next": f"Inspect frozen inputs with profile inspect {identifier}; prepare an exact native launch with profile preflight.",
    }


def _inspect(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    profile = load_profile(store, args.reference)
    return {"id": store.resolve(args.reference), "profile": profile.model_dump(mode="json")}


def _create(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    identifier = capture_profile(store, selected_capture(args))
    store.set_alias(args.name, identifier, replace=args.replace_alias)
    profile = load_profile(store, identifier)
    return {
        "id": identifier,
        "name": args.name,
        "issues": [item.model_dump(mode="json") for item in profile.issues],
        "next": f"Derive a comparison with dryheave profile derive {args.name} --model MODEL --name VARIANT (add --effort only for supported models, or --clear-effort). Inspect with dryheave profile diff {args.name} VARIANT; freeze with dryheave experiment setup NAME --case CASE --profile base={args.name} --profile changed=VARIANT --simulator {profile.recipe.agent.value} --simulator-model MODEL.",
    }


def _diff(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return parse_json(json.dumps(diff_profiles(store, args.before, args.after)).encode())


def _derive(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    changes: dict[str, object] = {}
    if args.model is not None:
        changes["model"] = args.model
    if args.effort is not None or args.clear_effort:
        changes["effort"] = args.effort
    if args.spec and changes:
        raise InputError("Use either --spec or model/effort options, not both.")
    if not args.spec and not changes:
        raise InputError("Derivation needs --spec, --model, --effort or --clear-effort.")
    spec = (
        parse_model(read_bytes(args.spec, limit=4 * 1024 * 1024), DeriveSpec)
        if args.spec
        else DeriveSpec(recipe_changes=changes)
    )
    identifier = derive_profile(
        store, args.reference, spec, base=args.spec.absolute().parent if args.spec else None
    )
    if args.name:
        store.set_alias(args.name, identifier, replace=args.replace_alias)
    return {
        "id": identifier,
        "parent_id": store.resolve(args.reference),
        "next": f"dryheave profile diff {args.reference} {args.name or identifier}",
    }


def _launch(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    operation = materialize_profile if args.profile_command == "materialize" else preflight_profile
    plan = operation(
        store,
        args.reference,
        args.destination,
        args.workspace,
        strict=args.strict,
        probe_version=args.probe_version,
    )
    return {
        "launch_plan": plan.model_dump(mode="json"),
        "materialized": args.profile_command == "materialize",
    }


def register_profiles(registry: CommandRegistry) -> None:
    parser = registry.add(
        "profile",
        help_text="Freeze selected native agent settings and prepare explicit launch plans.",
    )
    commands = parser.add_subparsers(dest="profile_command", required=True)
    create = commands.add_parser(
        "create", help="Capture selected Codex or Claude inputs with named CLI options."
    )
    capture_arguments(create)
    registry.handler(create, _create)
    capture = commands.add_parser("capture")
    capture.add_argument("name", nargs="?")
    capture.add_argument("--spec", type=Path, required=True)
    capture.add_argument("--agent", choices=["codex", "claude"])
    capture.add_argument("--replace-alias", action="store_true")
    registry.handler(capture, _capture)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("reference")
    registry.handler(inspect, _inspect)
    diff = commands.add_parser("diff")
    diff.add_argument("before")
    diff.add_argument("after")
    registry.handler(diff, _diff)
    derive = commands.add_parser("derive")
    derive.add_argument("reference")
    derive.add_argument("--spec", type=Path)
    derive.add_argument(
        "--model", help="Change the requested model; clear unsupported inherited effort explicitly."
    )
    changes = derive.add_mutually_exclusive_group()
    changes.add_argument(
        "--effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        help="Change effort only, preserving every other frozen input.",
    )
    changes.add_argument(
        "--clear-effort",
        action="store_true",
        help="Remove the frozen effort override, including when deriving a Haiku profile.",
    )
    derive.add_argument("--name")
    derive.add_argument("--replace-alias", action="store_true")
    registry.handler(derive, _derive)
    for name in ("preflight", "materialize"):
        launch = commands.add_parser(name)
        launch.add_argument("reference")
        launch.add_argument("--destination", type=Path, required=True)
        launch.add_argument("--workspace", type=Path, required=True)
        launch.add_argument("--strict", action="store_true")
        launch.add_argument(
            "--probe-version",
            action="store_true",
            help="Run only the selected native executable's bounded --version command.",
        )
        registry.handler(launch, _launch)
