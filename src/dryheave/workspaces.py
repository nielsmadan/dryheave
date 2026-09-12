import fcntl
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
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

CONFIG_FILE = "dryheave.toml"
WORKSPACE_OWNER_FILE = ".dryheave-workspace.json"
DIRECTORY_OWNER_FILE = ".dryheave-owned.json"
CONFIG_LIMIT = 64 * 1024


class WorkspaceConfig(StrictModel):
    schema_version: Literal[1] = 1
    authoring: str = ".dryheave/authoring"
    store: str = ".dryheave/store"
    runtime: str = ".dryheave/rt"
    skills: str = ".agents/skills"

    @field_validator("authoring", "store", "runtime", "skills")
    @classmethod
    def valid_path(cls, value: str) -> str:
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
    schema_version: Literal[1] = 1
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
        config = WorkspaceConfig.model_validate(value)
    except (tomllib.TOMLDecodeError, UnicodeError) as error:
        raise InputError(f"Invalid workspace TOML: {path}") from error
    except ValidationError as error:
        raise InputError(f"Invalid workspace {path}: {validation_message(error)}") from error
    workspace = Workspace(root.absolute(), config)
    _validate_paths(workspace)
    return workspace


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
    return "".join(
        f"{name} = {json.dumps(value, ensure_ascii=False)}\n"
        for name, value in config.model_dump().items()
    ).encode("utf-8")


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


def _initialize_locked(root: Path, runtime_root: Path | None) -> Workspace:
    owned = _ownership(root)
    try:
        existing = read_workspace(root)
    except FileNotFoundError:
        existing = None
    if existing is not None and (owned is None or owned.config != existing.config):
        raise ConflictError(
            f"Workspace config is foreign or edited; preserve it: {root / CONFIG_FILE}"
        )
    config = owned.config if owned is not None else WorkspaceConfig()
    if runtime_root is not None:
        config = WorkspaceConfig.model_validate(
            config.model_dump() | {"runtime": str(runtime_root)}
        )
    if owned is not None and config != owned.config:
        raise ConflictError(
            "Init options differ from the owned workspace; preserve its existing configuration."
        )
    workspace = Workspace(root, config)
    _validate_paths(workspace)
    validate_runtime_root(workspace.path("runtime"))
    ownership = owned or WorkspaceOwnership(workspace_id=uuid4().hex, config=config)
    missing: list[tuple[Path, WorkspaceDirectoryOwnership]] = []
    for role in ("authoring", "store", "runtime"):
        marker = WorkspaceDirectoryOwnership.model_validate(
            {"workspace_id": ownership.workspace_id, "role": role}
        )
        path = workspace.path(marker.role)
        if not _check_directory(path, marker):
            missing.append((path, marker))
    if owned is None:
        atomic_write(
            root / WORKSPACE_OWNER_FILE, ownership.model_dump_json().encode() + b"\n", replace=False
        )
    for path, marker in missing:
        _create_directory(path, marker)
    if existing is None:
        atomic_write(root / CONFIG_FILE, _config_bytes(config), replace=False)
    return workspace


def initialize_workspace(path: Path, *, runtime_root: Path | None = None) -> Workspace:
    root = _safe_directory(path)
    with directory_fd(root, create=True) as descriptor:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusyError(
                f"Workspace initialization is running; retry when it finishes: {root}"
            ) from error
        try:
            return _initialize_locked(root, runtime_root)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
