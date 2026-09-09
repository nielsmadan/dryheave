import fcntl
import hashlib
import os
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from dryheave.constants import VERSION
from dryheave.errors import ConflictError, DryheaveError, InputError, LockBusyError, PathError
from dryheave.filesystem import atomic_write, directory_fd, directory_names, read_bytes
from dryheave.models import ObjectId, StrictModel
from dryheave.serialization import parse_model

SKILL_NAMES = ("dryheave-collect", "dryheave-case", "dryheave-results")
OWNER_FILE = ".dryheave-owned.json"
SKILL_LIMIT = 1024 * 1024


class SkillOwnership(StrictModel):
    schema_version: Literal[1]
    owner: Literal["dryheave"]
    name: Literal["dryheave-collect", "dryheave-case", "dryheave-results"]
    version: str = Field(min_length=1, max_length=100)
    files: dict[Literal["SKILL.md"], ObjectId] = Field(min_length=1, max_length=1)


def bundled_skill(name: str) -> bytes:
    if name not in SKILL_NAMES:
        raise InputError(f"Unknown bundled skill: {name}")
    return files("dryheave").joinpath("resources", "skills", name, "SKILL.md").read_bytes()


def _target(path: Path) -> Path:
    absolute = path.absolute()
    if absolute == Path(absolute.anchor) or ".." in absolute.parts:
        raise PathError("Select a skills directory without parent components or a filesystem root.")
    return absolute


def _owned(path: Path, name: str) -> SkillOwnership:
    entries = directory_names(path)
    if OWNER_FILE not in entries:
        raise ConflictError(
            f"Foreign skill directory; preserve it and select another target: {path}"
        )
    ownership = parse_model(read_bytes(path / OWNER_FILE, limit=SKILL_LIMIT), SkillOwnership)
    if ownership.name != name:
        raise ConflictError(f"Ownership name does not match skill directory: {path}")
    if entries != {OWNER_FILE, *ownership.files}:
        raise ConflictError(f"Skill has missing or foreign files; preserve and review them: {path}")
    for relative, expected in ownership.files.items():
        content = read_bytes(path / relative, limit=SKILL_LIMIT)
        if hashlib.sha256(content).hexdigest() != expected:
            raise ConflictError(f"Skill has user-edited bytes; preserve and review them: {path}")
    return ownership


def _status(target: Path, name: str, present: set[str]) -> dict[str, JsonValue]:
    expected = hashlib.sha256(bundled_skill(name)).hexdigest()
    result: dict[str, JsonValue] = {"name": name, "version": VERSION, "sha256": expected}
    if name not in present:
        return result | {"status": "missing", "action": "Run skills install with this target."}
    try:
        ownership = _owned(target / name, name)
    except (DryheaveError, OSError, ValueError) as error:
        return result | {"status": "conflict", "action": str(error)}
    current = ownership.files["SKILL.md"] == expected and ownership.version == VERSION
    return result | {
        "status": "current" if current else "outdated",
        "installed_version": ownership.version,
        "action": "No action needed." if current else "Run skills update with this target.",
    }


def list_skills(target: Path | None = None) -> dict[str, JsonValue]:
    if target is None:
        return {
            "version": VERSION,
            "skills": [{"name": name, "status": "bundled"} for name in SKILL_NAMES],
        }
    target = _target(target)
    try:
        present = directory_names(target)
    except FileNotFoundError:
        present = set()
    statuses: list[JsonValue] = [_status(target, name, present) for name in SKILL_NAMES]
    return {"target": str(target), "version": VERSION, "skills": statuses}


def _write_skill(path: Path, name: str, *, replace: bool) -> None:
    content = bundled_skill(name)
    ownership = SkillOwnership.model_validate(
        {
            "schema_version": 1,
            "owner": "dryheave",
            "name": name,
            "version": VERSION,
            "files": {"SKILL.md": hashlib.sha256(content).hexdigest()},
        }
    )
    atomic_write(path / "SKILL.md", content, replace=replace)
    atomic_write(path / OWNER_FILE, ownership.model_dump_json().encode() + b"\n", replace=replace)


def _remove_skill(target: Path, name: str, descriptor: int) -> None:
    with directory_fd(target / name) as skill:
        for relative in ("SKILL.md", OWNER_FILE):
            os.unlink(relative, dir_fd=skill)
        os.fsync(skill)
    os.rmdir(name, dir_fd=descriptor)
    os.fsync(descriptor)


def manage_skills(
    action: Literal["install", "update", "uninstall"], target: Path, names: tuple[str, ...] = ()
) -> dict[str, JsonValue]:
    selected = names or SKILL_NAMES
    if len(set(selected)) != len(selected) or any(name not in SKILL_NAMES for name in selected):
        raise InputError("Select distinct bundled skill names from skills list.")
    target = _target(target)
    with directory_fd(target, create=action == "install") as descriptor:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusyError(
                f"Skill maintenance is already running; retry after it finishes: {target}"
            ) from error
        try:
            present = set(os.listdir(descriptor))
            for name in selected:
                if action == "install":
                    if name in present:
                        raise ConflictError(
                            f"Skill already exists; inspect skills doctor: {target / name}"
                        )
                else:
                    _owned(target / name, name)
            for name in selected:
                if action == "uninstall":
                    _remove_skill(target, name, descriptor)
                else:
                    if action == "install":
                        os.mkdir(name, mode=0o700, dir_fd=descriptor)
                    _write_skill(target / name, name, replace=action == "update")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    return {"action": action, "target": str(target), "skills": list(selected), "version": VERSION}
