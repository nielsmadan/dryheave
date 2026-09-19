import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field

from dryheave.controller_models import ControllerRecipe, ControllerRuntime
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.auth import RuntimeBindings
from dryheave.errors import InputError
from dryheave.filesystem import (
    atomic_write,
    directory_fd,
    ensure_directory,
    read_bytes,
)
from dryheave.models import StrictModel
from dryheave.profile_models import RuntimeFileReference
from dryheave.serialization import canonical_json, parse_model

RUNTIME_MARKER = "runtime-bindings.json"
MAX_RUNTIMES = 1000


class ControllerBindings(StrictModel):
    schema_version: Literal[1] = 1
    owner: Literal["dryheave-controller"] = "dryheave-controller"
    config_roots: dict[str, str]
    runtime_files: tuple[RuntimeFileReference, ...] = Field(default=(), max_length=1)
    identities: dict[str, tuple[int, int, int]] = Field(default_factory=dict, max_length=1)


def prepare_runtime(
    recipe: ControllerRecipe, root: Path, inherited: Mapping[str, str], artifacts: ArtifactWriter
) -> RuntimeBindings:
    if recipe.runtime is None:
        raise InputError("Controller has no owned runtime policy.")
    root = root.absolute()
    evidence = artifacts.root.absolute()
    if root.is_relative_to(evidence) or evidence.is_relative_to(root):
        raise InputError("Controller runtime must be separate from captured evidence.")
    ensure_directory(root.parent)
    with directory_fd(root.parent) as descriptor:
        os.mkdir(root.name, mode=0o700, dir_fd=descriptor)
    for name in ("config", "tmp", "work"):
        ensure_directory(root / name)
    plan = ControllerBindings(
        config_roots={"config": str(root / "config")},
        runtime_files=recipe.runtime.runtime_files
        if isinstance(recipe.runtime, ControllerRuntime)
        else (),
    )
    bindings = RuntimeBindings(plan, inherited)
    atomic_write(root / RUNTIME_MARKER, canonical_json(plan), replace=False)
    try:
        bindings.__enter__()
        atomic_write(
            root / RUNTIME_MARKER,
            canonical_json(plan.model_copy(update={"identities": bindings.owned})),
        )
        artifacts.record(
            "runtime",
            {
                "config_root": str(root / "config"),
                "temporary_root": str(root / "tmp"),
                "policy": recipe.runtime.model_dump(mode="json"),
                "requested_model": recipe.model,
                "requested_effort": recipe.effort,
                "limitations": [
                    "Built-in tools are disabled with --tools ''; MCP discovery is strict with an empty server set; hooks and plugins are disabled in explicit settings.",
                    "User/project/local settings are disabled; all CLAUDE.md discovery is excluded. Managed policy and native executable behavior are not independently isolated or inspected.",
                    "Native HOME is retained for OAuth; only the named token reference is inherited, never read into evidence. --bare is prohibited because it disables OAuth.",
                    "Requested model and effort are not observations; result modelUsage may identify multiple models and does not establish effective effort.",
                ]
                if recipe.kind == "claude"
                else [
                    "Trusted native controller can invoke tools and access the host and network.",
                    "Inherited HOME skills, project, system and cloud configuration remain unverified; fresh CODEX_HOME is not complete isolation.",
                    "All plugins and plugin startup sync are disabled; bundled skills are disabled; user config and user/project exec rules are ignored.",
                    "JSONL does not verify the effective model or effort; requested values are not observations.",
                ],
            },
        )
    except BaseException:
        bindings.close()
        raise
    return bindings


def reconcile_runtimes(root: Path) -> None:
    names: list[str] = []
    try:
        with directory_fd(root) as descriptor, os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= MAX_RUNTIMES:
                    raise InputError("Controller runtime directory exceeds its recovery bound.")
                names.append(entry.name)
    except FileNotFoundError:
        return
    for name in names:
        runtime = root / name
        plan = parse_model(read_bytes(runtime / RUNTIME_MARKER, limit=65536), ControllerBindings)
        expected = str(runtime.absolute() / "config")
        if plan.config_roots != {"config": expected} or set(plan.identities) - {"auth.json"}:
            raise InputError("Controller runtime ownership differs from its recorded directory.")
        if "auth.json" not in plan.identities and (runtime / "config/auth.json").is_symlink():
            raise InputError(
                "Controller auth binding has no durable ownership identity; preserve it for review."
            )
        bindings = RuntimeBindings(plan, {})
        bindings.owned.update(plan.identities)
        if bindings.close():
            raise InputError(
                "Controller runtime binding was replaced; preserve it for manual review."
            )
