import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from dryheave.models import (
    AgentKind,
    EnvironmentReference,
    Name,
    ObjectId,
    PositiveInt,
    RelativePath,
    StrictModel,
)

Text = Annotated[
    str, StringConstraints(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f\x7f]+$")
]
Layer = Literal["global", "project"]
TargetRoot = Literal["config", "home", "project"]
AssetKind = Literal["config", "instruction", "skill", "plugin", "resource"]


class CaptureLimits(StrictModel):
    max_files: PositiveInt = 2048
    max_file_bytes: PositiveInt = 4 * 1024 * 1024
    max_total_bytes: PositiveInt = 32 * 1024 * 1024
    max_depth: PositiveInt = 32
    max_symlinks: PositiveInt = 40


class AssetSelection(StrictModel):
    root: Name
    path: RelativePath
    kind: AssetKind
    layer: Layer
    target_root: TargetRoot
    target: RelativePath
    overlay: Literal["preserve", "replace"] = "preserve"

    @model_validator(mode="after")
    def consistent_layer(self) -> Self:
        if (self.layer == "project") != (self.target_root == "project"):
            raise ValueError("project and global destination roots must remain separate")
        if self.layer == "global" and self.overlay != "preserve":
            raise ValueError("replacement overlays apply only to project files")
        return self


class RuntimeFileReference(StrictModel):
    name: Name
    source_path_environment: EnvironmentReference
    target: Literal["auth.json"] = "auth.json"
    binding: Literal["symlink"] = "symlink"
    acknowledge_source_writes: Literal[True]


class NativeRecipe(StrictModel):
    agent: AgentKind
    executable: Text
    version: Text | None = None
    launcher: tuple[Text, ...] = ()
    arguments: tuple[Text, ...] = ()
    model: Text | None = None
    effort: Text | None = None
    workflow: Text | None = None
    home_policy: Literal["native", "isolated"] = "native"
    workspace_trust: Literal["prompt", "trusted", "untrusted"] = "prompt"
    disabled_skill_paths: tuple[Text, ...] = ()
    disabled_skill_names: tuple[Name, ...] = (
        "dryheave-collect",
        "dryheave-case",
        "dryheave-results",
    )
    environment: tuple[EnvironmentReference, ...] = ()
    runtime_files: tuple[RuntimeFileReference, ...] = ()

    @model_validator(mode="after")
    def environment_contract(self) -> Self:
        if self.agent != AgentKind.CODEX and self.workspace_trust != "prompt":
            raise ValueError("explicit workspace trust is currently verified only for Codex")
        names = [item.name for item in self.environment]
        reserved = {
            "HOME",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "TMPDIR",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        }
        if len(set(names)) != len(names) or set(names) & reserved:
            raise ValueError("environment names must be unique and cannot override owned roots")
        if len(self.runtime_files) > 1 or (self.runtime_files and self.agent != AgentKind.CODEX):
            raise ValueError("opaque auth-file binding is supported only for one Codex auth.json")
        return self


class CaptureSpec(StrictModel):
    schema_version: Literal[1] = 1
    recipe: NativeRecipe
    include_roots: dict[Name, Text] = Field(default_factory=dict)
    assets: tuple[AssetSelection, ...] = ()
    limits: CaptureLimits = Field(default_factory=CaptureLimits)


class FrozenAsset(StrictModel):
    selection: AssetSelection
    source_root: Text
    source_path: Text
    resolved_source: Text
    target: RelativePath
    blob: RelativePath
    sha256: ObjectId
    executable: bool = False


class ProfileIssue(StrictModel):
    code: Name
    message: Text
    asset: RelativePath | None = None


class FrozenProfile(StrictModel):
    schema_version: Literal[1] = 1
    recipe: NativeRecipe
    assets: tuple[FrozenAsset, ...]
    limits: CaptureLimits
    issues: tuple[ProfileIssue, ...] = ()
    parent_id: ObjectId | None = None

    @model_validator(mode="after")
    def distinct_assets(self) -> Self:
        names: set[str] = set()
        portable_names: set[str] = set()
        for asset in self.assets:
            if asset.target != asset.selection.target and not asset.target.startswith(
                asset.selection.target + "/"
            ):
                raise ValueError("expanded asset target must remain beneath its selected target")
            portable = unicodedata.normalize("NFC", asset.blob).casefold()
            if (
                portable in portable_names
                or any(str(parent) in portable_names for parent in PurePosixPath(portable).parents)
                or any(name.startswith(portable + "/") for name in portable_names)
            ):
                raise ValueError("asset paths collide on case-insensitive filesystems")
            portable_names.add(portable)
            expected = f"{asset.selection.layer}/{asset.selection.target_root}/{asset.target}"
            if asset.blob != expected or asset.blob in names:
                raise ValueError("asset blobs must identify distinct layer and target paths")
            if any(str(parent) in names for parent in PurePosixPath(asset.blob).parents):
                raise ValueError("asset files overlap as ancestors")
            if any(name.startswith(asset.blob + "/") for name in names):
                raise ValueError("asset files overlap as ancestors")
            names.add(asset.blob)
        return self


class DeriveSpec(StrictModel):
    schema_version: Literal[1] = 1
    recipe_changes: dict[str, object] = Field(default_factory=dict)
    additions: CaptureSpec | None = None
    remove: tuple[RelativePath, ...] = ()
    replace: tuple[RelativePath, ...] = ()


class DiscoveryRoot(StrictModel):
    kind: Literal["config", "instruction", "skill", "plugin", "auth", "environment"]
    path: Text
    status: Literal["frozen", "disabled", "historical", "generated", "unverified"]
    mechanism: Text


class OverlayMapping(StrictModel):
    blob: RelativePath
    destination: Text
    action: Literal["create", "preserve", "replace", "identical"]
    previous_sha256: ObjectId | None = None


class ExecutableResolution(StrictModel):
    requested: Text
    resolved: Text | None
    expected_version: Text | None
    observed_version: Text | None = None
    version_status: Literal["unverified", "matched", "mismatch"] = "unverified"


class LaunchPlan(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: ObjectId
    agent: AgentKind
    argv: tuple[str, ...]
    cwd: Text
    executable: ExecutableResolution
    launcher_executable: Text | None = None
    config_roots: dict[str, str]
    generated_environment: dict[str, str]
    environment_references: tuple[EnvironmentReference, ...]
    environment_policy: Literal["only-generated-and-explicit-references"] = (
        "only-generated-and-explicit-references"
    )
    runtime_files: tuple[RuntimeFileReference, ...]
    generated_files: dict[RelativePath, str]
    overlays: tuple[OverlayMapping, ...]
    discovery_roots: tuple[DiscoveryRoot, ...]
    issues: tuple[ProfileIssue, ...]
    fidelity: Literal["captured", "strict"]
    launchable: bool
    required_environment: tuple[str, ...] = ()
    workspace_trust: Literal["prompt", "trusted", "untrusted"] = "prompt"
    execution_mode: Literal["native"] = "native"
    isolation: Literal["repository-object"] = "repository-object"

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or not value[0] or any(re.search(r"[\x00\r\n]", arg) for arg in value):
            raise ValueError("argv must contain a valid executable and no control characters")
        return value
