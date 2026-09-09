import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from dryheave.errors import InputError, PathError
from dryheave.filesystem import atomic_write, ensure_directory, read_bytes
from dryheave.models import AgentKind, CommandSpec, EnvironmentReference
from dryheave.processes import run_command
from dryheave.profile_models import (
    DiscoveryRoot,
    ExecutableResolution,
    FrozenProfile,
    LaunchPlan,
    NativeRecipe,
    OverlayMapping,
    ProfileIssue,
)
from dryheave.profiles import load_profile
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore

BASE_ENVIRONMENT = ("PATH", "TERM", "LANG", "LC_ALL", "LC_CTYPE")


def _issue(code: str, message: str) -> ProfileIssue:
    return ProfileIssue(code=code, message=message)


def _roots(destination: Path, workspace: Path, recipe: NativeRecipe) -> dict[str, str]:
    return {
        "config": str(destination / "config"),
        "home": str(destination / "home") if recipe.home_policy == "isolated" else str(Path.home()),
        "project": str(workspace),
        "runtime": str(destination),
    }


def _environment(
    recipe: NativeRecipe, roots: dict[str, str]
) -> tuple[dict[str, str], tuple[EnvironmentReference, ...]]:
    runtime = Path(roots["runtime"])
    generated = {
        "CODEX_HOME" if recipe.agent == AgentKind.CODEX else "CLAUDE_CONFIG_DIR": roots["config"],
        "TMPDIR": str(runtime / "tmp"),
    }
    names = set(BASE_ENVIRONMENT) | {item.name for item in recipe.environment}
    if recipe.home_policy == "isolated":
        generated["HOME"] = roots["home"]
        generated.update(
            {
                f"XDG_{name}_HOME": str(runtime / "xdg" / name.lower())
                for name in ("CONFIG", "CACHE", "DATA", "STATE")
            }
        )
    else:
        names.add("HOME")
        names.update({"XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"})
    if recipe.agent == AgentKind.CLAUDE:
        generated["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return generated, tuple(EnvironmentReference(name=name) for name in sorted(names))


def _disabled_paths(profile: FrozenProfile, roots: dict[str, str]) -> tuple[str, ...]:
    paths = set(profile.recipe.disabled_skill_paths) | set(profile.superseded_skill_paths)
    for asset in profile.assets:
        destination = Path(roots[asset.selection.target_root]) / asset.target
        if (
            asset.selection.kind == "skill"
            and Path(asset.target).name == "SKILL.md"
            and str(destination) != asset.resolved_source
        ):
            paths.add(asset.resolved_source)
    return tuple(sorted(paths))


def _codex_args(profile: FrozenProfile, roots: dict[str, str]) -> list[str]:
    recipe = profile.recipe
    arguments: list[str] = []
    if recipe.workspace_trust != "prompt":
        arguments.extend(
            (
                "--config",
                "projects={"
                + json.dumps(roots["project"], ensure_ascii=False)
                + "={trust_level="
                + json.dumps(recipe.workspace_trust, ensure_ascii=False)
                + "}}",
            )
        )
    if recipe.model:
        arguments.extend(("--model", recipe.model))
    if recipe.effort:
        arguments.extend(
            ("--config", "model_reasoning_effort=" + json.dumps(recipe.effort, ensure_ascii=False))
        )
    rules = [
        "{name=" + json.dumps(name, ensure_ascii=False) + ",enabled=false}"
        for name in recipe.disabled_skill_names
    ]
    for path in _disabled_paths(profile, roots):
        if not Path(path).is_absolute():
            raise InputError("Disabled skill paths must name absolute SKILL.md paths.")
        rules.append("{path=" + json.dumps(path, ensure_ascii=False) + ",enabled=false}")
    if rules:
        arguments.extend(("--config", "skills.config=[" + ",".join(rules) + "]"))
    return arguments


def _claude_args(profile: FrozenProfile, roots: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    recipe = profile.recipe
    arguments = ["--setting-sources", "user,project,local"]
    if recipe.model:
        arguments.extend(("--model", recipe.model))
    if recipe.effort:
        arguments.extend(("--effort", recipe.effort))
    ancestors = Path(roots["project"]).parents
    excludes = [
        str(parent / name)
        for parent in ancestors
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/**")
    ]
    settings = json.dumps(
        {
            "claudeMdExcludes": excludes,
            "skillOverrides": dict.fromkeys(recipe.disabled_skill_names, "off"),
        },
        sort_keys=True,
    )
    arguments.extend(("--settings", str(Path(roots["runtime"]) / "overrides.json")))
    plugins = sorted(
        {
            str(Path(roots[item.selection.target_root]) / item.selection.target)
            for item in profile.assets
            if item.selection.kind == "plugin"
        }
    )
    for plugin in plugins:
        arguments.extend(("--plugin-dir", plugin))
    return arguments, {"overrides.json": settings}


def _resolve_executable(executable: str) -> str | None:
    resolved = shutil.which(executable)
    return str(Path(resolved).absolute()) if resolved is not None else None


def _resolution(
    recipe: NativeRecipe, workspace: Path, *, probe_version: bool
) -> ExecutableResolution:
    resolved = _resolve_executable(recipe.executable)
    observed = None
    status: Literal["unverified", "matched", "mismatch"] = "unverified"
    if resolved and probe_version:
        command = CommandSpec(
            argv=(resolved, "--version"), timeout_seconds=10, max_output_bytes=4096
        )
        result = run_command(command, workspace)
        if result.outcome == "exited" and result.returncode == 0:
            match = re.fullmatch(
                rb"(?:codex-cli )?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)(?: \(Claude Code\))?\s*",
                result.stdout,
            )
            if match:
                observed = match[1].decode("ascii")
                if recipe.version:
                    status = "matched" if observed == recipe.version else "mismatch"
    return ExecutableResolution(
        requested=recipe.executable,
        resolved=resolved,
        expected_version=recipe.version,
        observed_version=observed,
        version_status=status,
    )


def _overlays(profile: FrozenProfile, roots: dict[str, str]) -> tuple[OverlayMapping, ...]:
    mappings: list[OverlayMapping] = []
    for asset in profile.assets:
        destination = Path(roots[asset.selection.target_root]) / asset.target
        action: Literal["create", "preserve", "replace", "identical"] = "create"
        previous = None
        if asset.selection.target_root == "home" and profile.recipe.home_policy == "native":
            raise InputError(
                "Selected home assets require explicit isolated home policy; native home is read-only."
            )
        if asset.selection.layer == "project":
            try:
                old = read_bytes(destination, limit=profile.limits.max_file_bytes)
            except FileNotFoundError:
                pass
            else:
                previous = digest(old)
                executable = bool(destination.stat(follow_symlinks=False).st_mode & 0o111)
                action = (
                    "identical"
                    if previous == asset.sha256 and executable == asset.executable
                    else asset.selection.overlay
                )
        mappings.append(
            OverlayMapping(
                blob=asset.blob,
                destination=str(destination),
                action=action,
                previous_sha256=previous,
            )
        )
    return tuple(mappings)


def _discovery(profile: FrozenProfile, roots: dict[str, str]) -> list[DiscoveryRoot]:
    recipe = profile.recipe
    config, home, project = (Path(roots[name]) for name in ("config", "home", "project"))
    native = ".codex" if recipe.agent == AgentKind.CODEX else ".claude"
    instruction = "AGENTS.md" if recipe.agent == AgentKind.CODEX else "CLAUDE.md"
    found = [
        DiscoveryRoot(
            kind="config",
            path=str(config),
            status="frozen",
            mechanism="Fresh native config root containing only selected bytes and generated overrides.",
        ),
        DiscoveryRoot(
            kind="instruction",
            path=str(config / instruction),
            status="frozen",
            mechanism="Separate global instruction layer in fresh native config root.",
        ),
        DiscoveryRoot(
            kind="instruction",
            path=str(project),
            status="historical",
            mechanism="Historical project instructions remain; recorded explicit overlays determine collisions.",
        ),
        DiscoveryRoot(
            kind="skill",
            path=str(config / "skills"),
            status="frozen",
            mechanism="Fresh native user skill root populated only from explicit selected bytes.",
        ),
        DiscoveryRoot(
            kind="skill",
            path=str(project / native / "skills"),
            status="historical",
            mechanism="Native project skill discovery uses the materialized repository.",
        ),
        DiscoveryRoot(
            kind="plugin",
            path=str(config / "plugins"),
            status="unverified",
            mechanism="Selected plugin bytes are frozen; activation, fetched components and integration availability require native verification.",
        ),
    ]
    if recipe.agent == AgentKind.CODEX:
        found.extend(_codex_discovery(profile, roots))
    else:
        found.extend(
            [
                DiscoveryRoot(
                    kind="instruction",
                    path=str(project.parent),
                    status="disabled",
                    mechanism="Generated claudeMdExcludes matches ancestor CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md and .claude/rules paths; managed instructions remain unverified.",
                ),
                DiscoveryRoot(
                    kind="skill",
                    path=str(home / ".claude" / "skills"),
                    status="unverified" if recipe.home_policy == "native" else "disabled",
                    mechanism="CLAUDE_CONFIG_DIR redirects documented user resources to the fresh root; alternate home and managed sources require native-version validation.",
                ),
                DiscoveryRoot(
                    kind="skill",
                    path=str(project.parent / ".claude" / "skills"),
                    status="unverified",
                    mechanism="Claude discovers skills from cwd through repository root; generated skillOverrides hides named operator skills, while other ancestor and plugin skills remain unverified.",
                ),
                DiscoveryRoot(
                    kind="config",
                    path="managed Claude settings and policy",
                    status="unverified",
                    mechanism="Managed sources are not inspected or overridden.",
                ),
            ]
        )
    return found


def _codex_discovery(profile: FrozenProfile, roots: dict[str, str]) -> list[DiscoveryRoot]:
    recipe = profile.recipe
    home, project, config = (Path(roots[name]) for name in ("home", "project", "config"))
    found = [
        DiscoveryRoot(
            kind="skill",
            path=str(home / ".agents/skills"),
            status="unverified" if recipe.home_policy == "native" else "frozen",
            mechanism="Codex 0.153.4 discovers HOME/.agents/skills independently of CODEX_HOME; native home is not inventoried.",
        ),
        DiscoveryRoot(
            kind="skill",
            path=str(project / ".agents/skills"),
            status="historical",
            mechanism="Codex searches project root through cwd for .agents/skills; repository root markers and trust may change scope.",
        ),
        DiscoveryRoot(
            kind="skill",
            path=str(config / "skills/.system"),
            status="unverified",
            mechanism="Bundled system skills depend on the native executable and can be installed by the harness.",
        ),
        DiscoveryRoot(
            kind="config",
            path="/etc/codex and managed/cloud/MDM config",
            status="unverified",
            mechanism="System configuration, requirements and skill roots are not inventoried or overridden.",
        ),
        DiscoveryRoot(
            kind="config",
            path=str(project.parent),
            status="unverified",
            mechanism="Ancestor .codex discovery depends on effective project_root_markers and trust; CODEX_HOME alone does not bound it.",
        ),
    ]
    found.extend(
        DiscoveryRoot(
            kind="skill",
            path=path,
            status="disabled",
            mechanism="Session skills.config path selector with enabled=false; explicit paths and copied skill source documents are disabled without disabling their frozen copies.",
        )
        for path in _disabled_paths(profile, roots)
    )
    found.extend(
        DiscoveryRoot(
            kind="skill",
            path=f"skill-name:{name}",
            status="disabled",
            mechanism="Session skills.config name selector with enabled=false; ordered after selected arguments.",
        )
        for name in recipe.disabled_skill_names
    )
    return found


def _launch_issues(
    profile: FrozenProfile, resolution: ExecutableResolution, discovery: list[DiscoveryRoot]
) -> list[ProfileIssue]:
    issues = list(profile.issues)
    if resolution.resolved is None:
        issues.append(
            _issue(
                "executable-missing",
                "Selected native executable is unavailable on PATH or at its explicit path.",
            )
        )
    if resolution.version_status != "matched":
        issues.append(
            _issue(
                "version-" + resolution.version_status,
                "Native executable version has not been verified to match the frozen version.",
            )
        )
    for root in discovery:
        if root.status == "unverified":
            issues.append(
                _issue(
                    "ambient-" + root.kind,
                    f"Unverified discovery source: {root.path}. {root.mechanism}",
                )
            )
    issues.extend(
        [
            _issue(
                "native-environment",
                "Only required OS basics and explicitly named runtime variables are inherited; omitted variables may affect native tools. Repository isolation does not isolate host access.",
            ),
            _issue(
                "authentication-unverified",
                "Authentication is a runtime reference; availability and billing identity are not inspected. A fresh Codex home also changes file and keyring authentication identity.",
            ),
            _issue(
                "operator-skills",
                "Known Dryheave skill names are disabled through Codex skills.config or Claude skillOverrides; Claude plugin skills are unaffected, and other operator names require discovery review.",
            ),
            _issue(
                "permissions-unverified",
                "Native trust, sandbox and approval prompts remain controlled by the selected configuration and host; no bypass flags are generated.",
            ),
        ]
    )
    if profile.recipe.home_policy == "isolated":
        issues.append(
            _issue(
                "home-relocated",
                "Explicit isolated home policy changes native tool configuration, browser/device state and authentication lookup; tool equivalence is unverified.",
            )
        )
    if profile.recipe.runtime_files:
        issues.append(
            _issue(
                "auth-source-writes",
                "Runtime auth.json symlink binding is deferred; native token refresh can modify its source. The frozen recipe explicitly acknowledges these writes.",
            )
        )
    if (
        any(item.name == "CODEX_API_KEY" for item in profile.recipe.environment)
        and profile.recipe.agent == AgentKind.CODEX
    ):
        issues.append(
            _issue(
                "codex-tui-auth",
                "CODEX_API_KEY is supported by exec/review, not the ordinary interactive TUI; it does not establish this launch's authentication.",
            )
        )
    return issues


def preflight_profile(
    store: ObjectStore,
    reference: str,
    destination: Path,
    workspace: Path,
    *,
    strict: bool = False,
    probe_version: bool = False,
) -> LaunchPlan:
    profile_id = store.resolve(reference)
    profile = load_profile(store, profile_id)
    workspace = workspace.absolute()
    destination = destination.absolute()
    if not workspace.is_dir() or workspace.is_symlink():
        raise PathError("Launch workspace must be an existing real directory.")
    if destination.exists() or destination.is_symlink():
        raise PathError("Profile materialization requires a fresh destination.")
    if destination.is_relative_to(workspace) or workspace.is_relative_to(destination):
        raise PathError("Native config and workspace roots must be separate.")
    roots = _roots(destination, workspace, profile.recipe)
    generated, references = _environment(profile.recipe, roots)
    resolution = _resolution(profile.recipe, workspace, probe_version=probe_version)
    discovery = _discovery(profile, roots)
    issues = _launch_issues(profile, resolution, discovery)
    launcher = _resolve_executable(profile.recipe.launcher[0]) if profile.recipe.launcher else None
    if profile.recipe.launcher and launcher is None:
        issues.append(_issue("launcher-missing", "Explicit launcher executable is unavailable."))
    prefix = (
        (launcher or profile.recipe.launcher[0], *profile.recipe.launcher[1:])
        if profile.recipe.launcher
        else (resolution.resolved or profile.recipe.executable,)
    )
    generated_files: dict[str, str]
    if profile.recipe.agent == AgentKind.CODEX:
        arguments, generated_files = _codex_args(profile, roots), {}
    else:
        arguments, generated_files = _claude_args(profile, roots)
    if strict and issues:
        raise InputError(
            "Strict faithful launch refused unresolved issues: "
            + ", ".join(sorted({item.code for item in issues}))
        )
    return LaunchPlan(
        profile_id=profile_id,
        agent=profile.recipe.agent,
        argv=(*prefix, *profile.recipe.arguments, *arguments),
        cwd=str(workspace),
        executable=resolution,
        launcher_executable=launcher,
        config_roots=roots,
        generated_environment=generated,
        environment_references=references,
        runtime_files=profile.recipe.runtime_files,
        generated_files=generated_files,
        overlays=_overlays(profile, roots),
        discovery_roots=tuple(discovery),
        issues=tuple(issues),
        fidelity="strict" if strict else "captured",
        launchable=not any(
            item.code in {"executable-missing", "launcher-missing", "version-mismatch"}
            for item in issues
        ),
        required_environment=tuple(item.name for item in profile.recipe.environment),
        workspace_trust=profile.recipe.workspace_trust,
    )


def materialize_profile(
    store: ObjectStore,
    reference: str,
    destination: Path,
    workspace: Path,
    *,
    strict: bool = False,
    probe_version: bool = False,
) -> LaunchPlan:
    plan = preflight_profile(
        store, reference, destination, workspace, strict=strict, probe_version=probe_version
    )
    profile = load_profile(store, plan.profile_id)
    files = store.read_blobs(plan.profile_id)
    root = Path(plan.config_roots["runtime"])
    ensure_directory(root.parent)
    os.mkdir(root, mode=0o700)
    for name in ("config", "tmp"):
        ensure_directory(root / name)
    if profile.recipe.home_policy == "isolated":
        ensure_directory(root / "home")
    for overlay in plan.overlays:
        if overlay.action in {"preserve", "identical"}:
            continue
        target = Path(overlay.destination)
        if (
            overlay.previous_sha256 is not None
            and digest(read_bytes(target, limit=profile.limits.max_file_bytes))
            != overlay.previous_sha256
        ):
            raise InputError("Project file changed after preflight; replacement refused.")
        atomic_write(
            target,
            files[overlay.blob],
            replace=overlay.action == "replace",
        )
        asset = next(item for item in profile.assets if item.blob == overlay.blob)
        os.chmod(target, 0o700 if asset.executable else 0o600, follow_symlinks=False)
    for name, content in plan.generated_files.items():
        atomic_write(root / name, content.encode(), replace=False)
    atomic_write(root / "launch-plan.json", canonical_json(plan), replace=False)
    return plan


def launch_environment(plan: LaunchPlan, inherited: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in plan.required_environment if name not in inherited]
    if missing:
        raise InputError(
            "Required runtime environment references are unavailable: " + ", ".join(missing)
        )
    values = {
        item.name: inherited[item.name]
        for item in plan.environment_references
        if item.name in inherited
    }
    values.update(plan.generated_environment)
    return values
