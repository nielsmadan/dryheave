import argparse
from pathlib import Path

from dryheave.errors import InputError
from dryheave.models import AgentKind, EnvironmentReference
from dryheave.profile_models import AssetSelection, CaptureSpec, NativeRecipe, RuntimeFileReference
from dryheave.skills import SKILL_NAMES


def runtime_references(
    source_environment: str | None, *, acknowledge: bool
) -> tuple[RuntimeFileReference, ...]:
    if bool(source_environment) != acknowledge:
        raise InputError(
            "Auth-file binding requires both --auth-file-env and --acknowledge-auth-source-writes (use simulator-prefixed flags for an experiment)."
        )
    if source_environment is None:
        return ()
    return (
        RuntimeFileReference(
            name="codex-auth",
            source_path_environment=EnvironmentReference(name=source_environment),
            acknowledge_source_writes=True,
        ),
    )


def selected_capture(args: argparse.Namespace) -> CaptureSpec:
    claude = args.agent == "claude"
    if claude and (
        args.plugins is not None
        or args.auth_file_env
        or args.acknowledge_auth_source_writes
        or args.workspace_trust is not None
    ):
        raise InputError("Claude profiles do not accept Codex plugin, trust or auth-file options.")
    if claude and not args.model:
        raise InputError(
            "Claude profile creation requires an explicit --model; Haiku must omit --effort."
        )
    version = args.native_version or ("2.1.278" if claude else "0.154.0")
    if version != ("2.1.278" if claude else "0.154.0"):
        raise InputError(
            "Friendly setup supports Codex 0.154.0 or Claude 2.1.278; use profile capture for legacy recipes."
        )
    environment = list(args.environment)
    if claude and "CLAUDE_CODE_OAUTH_TOKEN" not in environment:
        environment.append("CLAUDE_CODE_OAUTH_TOKEN")
    recipe = NativeRecipe(
        agent=AgentKind(args.agent),
        executable=args.executable or args.agent,
        version=version,
        model=args.model or "gpt-5.6-terra",
        effort=args.effort if claude else args.effort or "low",
        arguments=tuple(args.native_arg),
        workspace_trust=args.workspace_trust or "prompt",
        disabled_skill_names=SKILL_NAMES,
        codex_discovery=None if claude else args.plugins or "disabled-plugins",
        claude_discovery="selected-2.1.278" if claude else None,
        environment=tuple(EnvironmentReference(name=name) for name in environment),
        runtime_files=runtime_references(
            args.auth_file_env, acknowledge=args.acknowledge_auth_source_writes
        ),
    )
    roots = {}
    assets: list[AssetSelection] = []
    selected = [
        ("config", args.config, "settings.json" if claude else "config.toml"),
        ("instruction", args.instruction, "CLAUDE.md" if claude else "AGENTS.md"),
        *(("skill", path, f"skills/{path.name}") for path in args.skill),
        *(("plugin", path, f"plugins/{path.name}") for path in args.plugin),
    ]
    for kind, source, target in selected:
        if source is None:
            continue
        path = source.absolute()
        name = f"selected-{len(assets)}"
        roots[name] = str(path.parent)
        assets.append(
            AssetSelection.model_validate(
                {
                    "root": name,
                    "path": path.name,
                    "kind": kind,
                    "layer": "global",
                    "target_root": "config",
                    "target": target,
                }
            )
        )
    return CaptureSpec(recipe=recipe, include_roots=roots, assets=tuple(assets))


def capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--agent", choices=["codex", "claude"], default="codex")
    parser.add_argument("--executable")
    parser.add_argument("--native-version", choices=["0.154.0", "2.1.278"])
    parser.add_argument(
        "--model",
        help="Explicit provider model; required for Claude, defaults to gpt-5.6-terra for Codex.",
    )
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    parser.add_argument(
        "--config",
        type=Path,
        help="One selected safe config.toml (Codex) or settings.json (Claude).",
    )
    parser.add_argument(
        "--instruction",
        type=Path,
        help="One selected global AGENTS.md (Codex) or CLAUDE.md (Claude).",
    )
    parser.add_argument(
        "--skill",
        type=Path,
        action="append",
        default=[],
        help="Selected skill directory; repeat for multiple skills.",
    )
    parser.add_argument(
        "--plugin",
        type=Path,
        action="append",
        default=[],
        help="Selected plugin directory; requires --plugins selected-plugins.",
    )
    parser.add_argument("--plugins", choices=["disabled-plugins", "selected-plugins"])
    parser.add_argument(
        "--native-arg",
        action="append",
        default=[],
        help="Explicit native argv entry; use --native-arg=--flag for flags.",
    )
    parser.add_argument("--workspace-trust", choices=["prompt", "trusted", "untrusted"])
    parser.add_argument("--environment", action="append", default=[], metavar="NAME")
    parser.add_argument(
        "--auth-file-env",
        metavar="NAME",
        help="Variable holding an opaque auth.json source path, resolved only at execution.",
    )
    parser.add_argument("--acknowledge-auth-source-writes", action="store_true")
    parser.add_argument("--replace-alias", action="store_true")
