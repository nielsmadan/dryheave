import json
import re
from pathlib import Path, PurePosixPath

from dryheave.errors import InputError, IntegrityError
from dryheave.models import ObjectKind
from dryheave.profile_capture import SelectedReader, validate_target
from dryheave.profile_models import (
    CaptureSpec,
    DeriveSpec,
    FrozenAsset,
    FrozenProfile,
    NativeRecipe,
    ProfileIssue,
)
from dryheave.profile_security import (
    inspect_config,
    reject_secret_bytes,
    reject_secret_fields,
    validate_arguments,
)
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore


def _recipe_issues(recipe: NativeRecipe) -> list[ProfileIssue]:
    validate_arguments(recipe.arguments)
    validate_arguments(recipe.launcher)
    reject_secret_fields(recipe.model_dump(mode="json"), location="profile recipe")
    issues: list[ProfileIssue] = []
    if recipe.launcher:
        issues.append(
            ProfileIssue(
                code="launcher-effects",
                message="Explicit argv launcher may change environment, executable, authentication or flags; its effects are unverified.",
            )
        )
    if recipe.version is None:
        issues.append(
            ProfileIssue(
                code="version-unpinned", message="Native executable version has not been pinned."
            )
        )
    if recipe.arguments:
        issues.append(
            ProfileIssue(
                code="native-arguments",
                message="Explicit native arguments are preserved; discovery, cwd and config effects require native validation.",
            )
        )
    if recipe.workflow:
        issues.append(
            ProfileIssue(
                code="workflow-semantics",
                message="Workflow is a frozen comparison label; its executable behavior must be supplied by selected instruction, skill, config or native arguments.",
            )
        )
    return issues


def profile_issues(profile: FrozenProfile, files: dict[str, bytes]) -> tuple[ProfileIssue, ...]:
    issues = _recipe_issues(profile.recipe)
    for asset in profile.assets:
        validate_target(profile.recipe.agent, asset.selection)
        validate_target(
            profile.recipe.agent, asset.selection.model_copy(update={"target": asset.target})
        )
        content = files[asset.blob]
        reject_secret_bytes(content, location=f"selected asset {asset.blob}")
        if asset.selection.kind == "config":
            config = inspect_config(content, asset.target)
            issues.extend(_config_issues(config, asset.blob))
        if asset.selection.kind in {"skill", "plugin"}:
            issues.append(
                ProfileIssue(
                    code="resource-dependencies",
                    message="Selected resource bytes are frozen; executable dependencies, network resources and plugin activation are not resolved automatically.",
                    asset=asset.blob,
                )
            )
        if re.search(
            rb"(?:^|[\s\"'=(:])(?:~/(?:[^\s]+)|/(?:Users|home|opt|usr|etc|var|tmp)/[^\s]+)", content
        ):
            issues.append(
                ProfileIssue(
                    code="absolute-reference",
                    message="Selected content contains a host path; bytes are preserved without rewriting external dependencies.",
                    asset=asset.blob,
                )
            )
        if asset.selection.kind == "instruction" and re.search(rb"(?<!\w)@[\w./~-]+", content):
            issues.append(
                ProfileIssue(
                    code="instruction-import",
                    message="Instruction imports require explicit dependency selection and native resolution review.",
                    asset=asset.blob,
                )
            )
    return tuple(issues)


def _config_issues(config: dict[str, object], blob: str) -> list[ProfileIssue]:
    issues = [
        ProfileIssue(
            code="config-schema-unverified",
            message="Config syntax and recognized secret fields were checked; the complete selected native version schema and effective permission/trust decisions remain unverified.",
            asset=blob,
        )
    ]
    if set(config) & {
        "mcp_servers",
        "mcpServers",
        "hooks",
        "plugins",
        "enabledPlugins",
        "extraKnownMarketplaces",
        "model_providers",
        "model_instructions_file",
        "projects",
    }:
        issues.append(
            ProfileIssue(
                code="external-config-dependencies",
                message="Selected configuration references runtime integrations, instructions, trust or plugin dependencies that require native review.",
                asset=blob,
            )
        )
    return issues


def capture_profile(store: ObjectStore, spec: CaptureSpec, *, base: Path | None = None) -> str:
    _recipe_issues(spec.recipe)
    reader = SelectedReader(spec, base or Path.cwd())
    assets, files = reader.capture()
    profile = FrozenProfile(recipe=spec.recipe, assets=assets, limits=spec.limits)
    profile = profile.model_copy(update={"issues": profile_issues(profile, files)})
    return store.put(ObjectKind.PROFILE, profile, files=files)


def _exceeds_depth(profile: FrozenProfile) -> bool:
    return any(
        len(PurePosixPath(asset.target).relative_to(asset.selection.target).parts)
        > profile.limits.max_depth
        for asset in profile.assets
    )


def load_profile(store: ObjectStore, reference: str) -> FrozenProfile:
    profile = store.load(reference, FrozenProfile, kind=ObjectKind.PROFILE)
    manifest = store.get(reference, kind=ObjectKind.PROFILE)
    if manifest.references != (() if profile.parent_id is None else (profile.parent_id,)):
        raise IntegrityError("Profile parent references do not match its manifest.")
    if manifest.files != {item.blob: item.sha256 for item in profile.assets}:
        raise IntegrityError("Profile asset hashes do not match its manifest.")
    files = {name: store.read_blob(reference, name) for name in manifest.files}
    if (
        sum(map(len, files.values())) > profile.limits.max_total_bytes
        or len(files) > profile.limits.max_files
    ):
        raise IntegrityError("Profile exceeds its frozen capture bounds.")
    if any(len(content) > profile.limits.max_file_bytes for content in files.values()):
        raise IntegrityError("Profile asset exceeds its frozen capture bounds.")
    if _exceeds_depth(profile):
        raise IntegrityError("Profile asset depth exceeds its frozen capture bounds.")
    if profile_issues(profile, files) != profile.issues:
        raise IntegrityError("Profile fidelity diagnostics do not match its frozen inputs.")
    return profile


def _superseded_skills(parent: FrozenProfile, assets: dict[str, FrozenAsset]) -> tuple[str, ...]:
    paths = set(parent.superseded_skill_paths)
    for asset in parent.assets:
        if asset.selection.kind != "skill" or PurePosixPath(asset.target).name != "SKILL.md":
            continue
        replacement = assets.get(asset.blob)
        if (
            replacement is None
            or replacement.selection.kind != "skill"
            or replacement.resolved_source != asset.resolved_source
        ):
            paths.add(asset.resolved_source)
    return tuple(sorted(paths))


def derive_profile(
    store: ObjectStore, reference: str, spec: DeriveSpec, *, base: Path | None = None
) -> str:
    parent_id = store.resolve(reference)
    parent = load_profile(store, parent_id)
    changes = spec.recipe_changes
    if "agent" in changes and changes["agent"] != parent.recipe.agent.value:
        raise InputError("Cross-agent profile derivation is unsupported.")
    reject_secret_fields(changes, location="profile recipe changes")
    recipe_data = parent.recipe.model_dump(mode="json") | changes
    recipe = parse_model(json.dumps(recipe_data).encode(), NativeRecipe)
    assets = {item.blob: item for item in parent.assets}
    files = {name: store.read_blob(parent_id, name) for name in assets}
    if len(set(spec.remove)) != len(spec.remove) or set(spec.remove) & set(spec.replace):
        raise InputError("Removal and replacement selections must be distinct.")
    for name in spec.remove:
        if name not in assets:
            raise InputError("A removed asset does not exist in the parent profile.")
        del assets[name]
        del files[name]
    if spec.additions:
        if spec.additions.recipe != recipe:
            raise InputError("Addition capture recipe must equal the derived recipe.")
        added, contents = SelectedReader(spec.additions, base or Path.cwd()).capture()
        collisions = {item.blob for item in added} & assets.keys()
        if collisions != set(spec.replace):
            raise InputError("Replacement list must exactly name colliding additions.")
        assets.update({item.blob: item for item in added})
        files.update(contents)
    elif spec.replace:
        raise InputError("Replacement selections require captured additions.")
    profile = FrozenProfile(
        recipe=recipe,
        assets=tuple(sorted(assets.values(), key=lambda item: item.blob)),
        limits=parent.limits,
        parent_id=parent_id,
        superseded_skill_paths=_superseded_skills(parent, assets),
    )
    if (
        len(files) > parent.limits.max_files
        or sum(map(len, files.values())) > parent.limits.max_total_bytes
        or any(len(content) > parent.limits.max_file_bytes for content in files.values())
        or _exceeds_depth(profile)
    ):
        raise InputError("Derived profile exceeds the parent's capture bounds.")
    profile = profile.model_copy(update={"issues": profile_issues(profile, files)})
    if canonical_json(profile.model_copy(update={"parent_id": parent.parent_id})) == canonical_json(
        parent
    ):
        raise InputError("Derivation must change an immutable input.")
    return store.put(ObjectKind.PROFILE, profile, files=files, references=(parent_id,))


def diff_profiles(store: ObjectStore, before: str, after: str) -> dict[str, object]:
    left, right = load_profile(store, before), load_profile(store, after)
    a, b = left.recipe.model_dump(mode="json"), right.recipe.model_dump(mode="json")
    old, new = {item.blob: item for item in left.assets}, {item.blob: item for item in right.assets}
    return {
        "before": store.resolve(before),
        "after": store.resolve(after),
        "recipe_changes": {
            key: {"before": a[key], "after": b[key]} for key in a if a[key] != b[key]
        },
        "superseded_skill_paths": {
            "before": list(left.superseded_skill_paths),
            "after": list(right.superseded_skill_paths),
        },
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(
            name
            for name in old.keys() & new.keys()
            if digest(canonical_json(old[name])) != digest(canonical_json(new[name]))
        ),
    }
