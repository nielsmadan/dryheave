import fcntl
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError, field_validator

from dryheave.constants import MAX_RELATIVE_PATH_LENGTH, MAX_RUNTIME_PATH_BYTES
from dryheave.errors import ConflictError, InputError, LockBusyError, PathError
from dryheave.filesystem import (
    atomic_write,
    directory_fd,
    publish_directory,
    read_bytes,
)
from dryheave.models import RunId, StrictModel
from dryheave.serialization import parse_model, validation_message
from dryheave.storage import LEGACY_STORE_COMMIT, ensure_store_format

WORKSPACE_SCHEMA_VERSION = 2
CONFIG_FILE = "dryheave.toml"
WORKSPACE_OWNER_FILE = ".dryheave-workspace.json"
DIRECTORY_OWNER_FILE = ".dryheave-owned.json"
CONFIG_LIMIT = 64 * 1024


GitCommitMode = Literal["ask", "auto", "off"]
GIT_COMMIT_MODES: tuple[GitCommitMode, ...] = ("ask", "auto", "off")


class WorkspaceConfig(StrictModel):
    schema_version: Literal[2] = 2
    authoring: str = ".dryheave/authoring"
    store: str = ".dryheave/store"
    runtime: str = ".dryheave/rt"
    skills: str = ".agents/skills"
    tui_test: str | None = None
    git_commit: GitCommitMode = "ask"

    @field_validator("authoring", "store", "runtime", "skills", "tui_test")
    @classmethod
    def valid_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.strip()
            or len(value) > MAX_RELATIVE_PATH_LENGTH
            or re.search(r"[\x00-\x1f\x7f]", value)
            or ".." in Path(value).parts
            or value.startswith("~")
        ):
            raise ValueError("expected a path without traversal, control characters or tilde")
        return value


class WorkspaceOwnership(StrictModel):
    schema_version: Literal[2] = 2
    owner: Literal["dryheave"] = "dryheave"
    workspace_id: RunId
    config: WorkspaceConfig


class WorkspaceDirectoryOwnership(StrictModel):
    schema_version: Literal[1] = 1
    owner: Literal["dryheave"] = "dryheave"
    workspace_id: RunId
    role: Literal["authoring", "store", "runtime"]


@dataclass(frozen=True)
class Workspace:
    root: Path
    config: WorkspaceConfig

    def path(self, role: Literal["authoring", "store", "runtime", "skills"]) -> Path:
        configured: str = getattr(self.config, role)
        return (self.root / configured).absolute()

    @property
    def voices_dir(self) -> Path:
        return self.root / "voices"

    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def replays_dir(self) -> Path:
        return self.root / "replays"

    def transport(self) -> Path | None:
        return (self.root / self.config.tui_test).absolute() if self.config.tui_test else None


def resolve_transport(explicit: Path | None, workspace: Workspace | None) -> Path | None:
    if explicit is not None:
        return explicit.absolute()
    if workspace is not None and workspace.transport() is not None:
        return workspace.transport()
    installed = shutil.which("tui-test")
    return Path(installed).absolute() if installed else None


def _safe_directory(path: Path) -> Path:
    absolute = path.absolute()
    if absolute == Path(absolute.anchor) or ".." in absolute.parts:
        raise PathError("Select a directory other than a filesystem root, without traversal.")
    return absolute


def validate_runtime_root(path: Path) -> None:
    length = len(os.fsencode(path.absolute() / ("x" * 10)))
    if length > MAX_RUNTIME_PATH_BYTES:
        raise InputError(
            f"Runtime root {path.absolute()} needs {length} bytes including its 10-character "
            f"child; the native limit is {MAX_RUNTIME_PATH_BYTES} bytes. "
            "Use init --runtime-root /short/new-directory, edit runtime in dryheave.toml, "
            "or supply run --runtime-root /short/directory."
        )


def read_workspace(root: Path) -> Workspace:
    path = root / CONFIG_FILE
    try:
        value = tomllib.loads(read_bytes(path, limit=CONFIG_LIMIT).decode("utf-8"))
        version = value.get("schema_version", WORKSPACE_SCHEMA_VERSION)
        if version != WORKSPACE_SCHEMA_VERSION:
            raise InputError(
                f"Workspace {path} uses schema_version {version}, the pre-redesign format; "
                "this Dryheave reads only schema_version 2 and has no migration. Use the "
                f"Dryheave CLI from commit {LEGACY_STORE_COMMIT} for the old workspace, "
                "or create a new workspace with dryheave init."
            )
        config = WorkspaceConfig.model_validate(_flatten_git(value))
    except (tomllib.TOMLDecodeError, UnicodeError) as error:
        raise InputError(f"Invalid workspace TOML: {path}") from error
    except ValidationError as error:
        raise InputError(f"Invalid workspace {path}: {validation_message(error)}") from error
    workspace = Workspace(root.absolute(), config)
    _validate_paths(workspace)
    return workspace


def _flatten_git(value: dict[str, Any]) -> dict[str, Any]:
    table = value.get("git", {})
    if "git_commit" in value or not isinstance(table, dict) or set(table) - {"commit"}:
        raise InputError("Workspace git settings belong in a [git] table with only 'commit'.")
    rest = {name: item for name, item in value.items() if name != "git"}
    return rest | ({"git_commit": table["commit"]} if "commit" in table else {})


def discover_workspace(start: Path | None = None) -> Workspace | None:
    root = (start or Path.cwd()).absolute()
    for directory in (root, *root.parents):
        try:
            (directory / CONFIG_FILE).lstat()
        except FileNotFoundError:
            continue
        return read_workspace(directory)
    return None


def initialized_workspace(start: Path | None = None) -> Workspace:
    workspace = discover_workspace(start)
    if workspace is None:
        raise InputError("Run dryheave init first, or provide an explicit --target PATH.")
    try:
        parse_model(
            read_bytes(workspace.root / WORKSPACE_OWNER_FILE, limit=CONFIG_LIMIT),
            WorkspaceOwnership,
        )
    except FileNotFoundError as error:
        raise ConflictError(f"Workspace is not initialized: {workspace.root}") from error
    return workspace


def _validate_paths(workspace: Workspace) -> None:
    paths = [workspace.path(role) for role in ("authoring", "store", "runtime", "skills")]
    for path in paths:
        _safe_directory(path)
        if path == workspace.root or workspace.root.is_relative_to(path):
            raise PathError("Workspace data directories cannot contain the workspace root.")
        if any(path != other and path.is_relative_to(other) for other in paths):
            raise PathError("Workspace data directories must not overlap.")
    if len(set(paths)) != len(paths):
        raise PathError("Workspace data directories must be distinct.")


def _config_bytes(config: WorkspaceConfig) -> bytes:
    values = config.model_dump(exclude_none=True, exclude={"git_commit"})
    lines = [
        f"{name} = {json.dumps(value, ensure_ascii=False)}\n" for name, value in values.items()
    ]
    lines += ["\n[git]\n", f"commit = {json.dumps(config.git_commit)}\n"]
    return "".join(lines).encode("utf-8")


def _ownership(root: Path) -> WorkspaceOwnership | None:
    try:
        return parse_model(
            read_bytes(root / WORKSPACE_OWNER_FILE, limit=CONFIG_LIMIT), WorkspaceOwnership
        )
    except FileNotFoundError:
        return None


def _check_directory(path: Path, expected: WorkspaceDirectoryOwnership) -> bool:
    try:
        with directory_fd(path):
            pass
    except FileNotFoundError:
        return False
    try:
        owned = parse_model(
            read_bytes(path / DIRECTORY_OWNER_FILE, limit=CONFIG_LIMIT), WorkspaceDirectoryOwnership
        )
    except FileNotFoundError as error:
        raise ConflictError(f"Workspace directory is foreign; preserve it: {path}") from error
    if owned != expected:
        raise ConflictError(f"Workspace directory ownership differs; preserve it: {path}")
    return True


def _create_directory(path: Path, ownership: WorkspaceDirectoryOwnership) -> None:
    staged = path.with_name(f".dryheave-pending-{uuid4().hex}")
    with directory_fd(path.parent, create=True) as parent:
        os.mkdir(staged.name, mode=0o700, dir_fd=parent)
    try:
        atomic_write(
            staged / DIRECTORY_OWNER_FILE,
            ownership.model_dump_json().encode() + b"\n",
            replace=False,
        )
        publish_directory(staged, path)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def _with_settings(config: WorkspaceConfig, settings: WorkspaceConfig) -> WorkspaceConfig:
    return config.model_copy(
        update={"git_commit": settings.git_commit, "tui_test": settings.tui_test}
    )


def _planned_config(
    root: Path,
    runtime_root: Path | None,
    tui_test: Path | None,
    git_commit: GitCommitMode | None,
) -> tuple[WorkspaceOwnership | None, Workspace | None, Workspace]:
    owned = _ownership(root)
    try:
        existing = read_workspace(root)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        owned is None or _with_settings(owned.config, existing.config) != existing.config
    ):
        raise ConflictError(
            f"Workspace config is foreign or edited; preserve it: {root / CONFIG_FILE}"
        )
    config = owned.config if owned is not None else WorkspaceConfig()
    if existing is not None:
        config = _with_settings(config, existing.config)
    mode = git_commit or config.git_commit
    if runtime_root is not None:
        config = WorkspaceConfig.model_validate(
            config.model_dump() | {"runtime": str(runtime_root)}
        )
    if tui_test is not None:
        config = WorkspaceConfig.model_validate(config.model_dump() | {"tui_test": str(tui_test)})
    if owned is not None and _with_settings(config, owned.config) != owned.config:
        raise ConflictError(
            "Init options differ from the owned workspace; preserve its existing configuration."
        )
    workspace = Workspace(root, config.model_copy(update={"git_commit": mode}))
    _validate_paths(workspace)
    validate_runtime_root(workspace.path("runtime"))
    return owned, existing, workspace


def check_initialization(path: Path, *, runtime_root: Path | None, tui_test: Path | None) -> None:
    _planned_config(_safe_directory(path), runtime_root, tui_test, None)


def _initialize_locked(
    root: Path,
    runtime_root: Path | None,
    tui_test: Path | None,
    git_commit: GitCommitMode | None,
) -> Workspace:
    owned, existing, workspace = _planned_config(root, runtime_root, tui_test, git_commit)
    config = workspace.config
    ownership = (
        owned.model_copy(update={"config": config})
        if owned is not None
        else WorkspaceOwnership(workspace_id=uuid4().hex, config=config)
    )
    missing: list[tuple[Path, WorkspaceDirectoryOwnership]] = []
    for role in ("authoring", "store", "runtime"):
        marker = WorkspaceDirectoryOwnership.model_validate(
            {"workspace_id": ownership.workspace_id, "role": role}
        )
        path = workspace.path(marker.role)
        if not _check_directory(path, marker):
            missing.append((path, marker))
    if owned is None or owned.config != config:
        atomic_write(
            root / WORKSPACE_OWNER_FILE,
            ownership.model_dump_json().encode() + b"\n",
            replace=owned is not None,
        )
    for path, marker in missing:
        _create_directory(path, marker)
    ensure_store_format(workspace.path("store"))
    if existing is None or existing.config != config:
        atomic_write(root / CONFIG_FILE, _config_bytes(config), replace=existing is not None)
    return workspace


def initialize_workspace(
    path: Path,
    *,
    runtime_root: Path | None = None,
    tui_test: Path | None = None,
    git_commit: GitCommitMode | None = None,
) -> Workspace:
    root = _safe_directory(path)
    with directory_fd(root, create=True) as descriptor:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusyError(
                f"Workspace initialization is running; retry when it finishes: {root}"
            ) from error
        try:
            return _initialize_locked(root, runtime_root, tui_test, git_commit)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
