import os
import posixpath
import shutil
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from dryheave.capture_models import CapturedFile, CaptureOmission, WorkspaceCapture
from dryheave.cases import CapturePolicy
from dryheave.errors import DryheaveError, InputError
from dryheave.filesystem import atomic_write, directory_fd, ensure_directory, read_bytes
from dryheave.models import validate_relative_path
from dryheave.repositories import Git, SnapshotLimits, load_repository, materialize_repository
from dryheave.serialization import digest
from dryheave.storage import ObjectStore

MAX_LINK_HOPS = 40


@dataclass
class IgnoreSelection:
    omitted: set[str] = field(default_factory=set)
    children: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class CaptureBuilder:
    policy: CapturePolicy
    blobs: dict[str, bytes] = field(default_factory=dict)
    files: list[CapturedFile] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    omissions: list[CaptureOmission] = field(default_factory=list)
    total_bytes: int = 0
    visited: int = 0

    def omit(self, path: str, reason: str, *, intentional: bool = False) -> None:
        self.omissions.append(CaptureOmission(path=path, reason=reason, intentional=intentional))

    def walk(
        self,
        root: Path,
        relative: str = "",
        *,
        ignored: Callable[[dict[str, int]], IgnoreSelection] | None = None,
        children: set[str] | None = None,
    ) -> dict[str, int]:
        found: dict[str, int] = {}
        with directory_fd(root) as descriptor, ExitStack() as stack:
            names = (
                iter(children)
                if children is not None
                else (entry.name for entry in stack.enter_context(os.scandir(descriptor)))
            )
            for name in names:
                path = f"{relative}/{name}" if relative else name
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    if children is None:
                        raise
                    continue
                self.visited += 1
                if self.visited > self.policy.max_files:
                    self.omit(relative or ".", "entry_limit")
                    break
                if name == ".git":
                    if relative:
                        self.omit(path, "nested_git_metadata")
                    continue
                try:
                    validate_relative_path(path)
                except ValueError:
                    self.omit(path, "unsafe_path")
                    continue
                if any(
                    path == excluded or path.startswith(excluded + "/")
                    for excluded in self.policy.exclude
                ):
                    self.omit(path, "frozen_exclusion", intentional=True)
                    continue
                if path.count("/") >= self.policy.max_depth:
                    self.omit(path, "depth_limit")
                    continue
                if (
                    stat.S_ISDIR(info.st_mode)
                    or stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                ):
                    found[path] = info.st_mode
                else:
                    self.omit(path, "special_file")
        selection = ignored(found) if ignored is not None else IgnoreSelection()
        for path, mode in tuple(found.items()):
            if path in selection.omitted:
                self.omit(path, "ignored", intentional=True)
                del found[path]
            elif stat.S_ISDIR(mode):
                child_names = selection.children.get(path)
                if child_names is not None:
                    self.omit(path, "ignored_children", intentional=True)
                found.update(
                    self.walk(root / Path(path).name, path, ignored=ignored, children=child_names)
                )
        return found

    def take(self, root: Path, path: str, mode: int, *, prefix: str) -> None:
        try:
            if stat.S_ISDIR(mode):
                self.directories.append(path)
                return
            if stat.S_ISLNK(mode):
                with directory_fd((root / path).parent) as parent:
                    content = os.readlink(Path(path).name, dir_fd=parent).encode()
                kind = "symlink"
            else:
                content = read_bytes(
                    root / path, limit=max(0, self.policy.max_bytes - self.total_bytes)
                )
                kind = "executable" if mode & stat.S_IXUSR else "file"
            if self.total_bytes + len(content) > self.policy.max_bytes:
                self.omit(path, "byte_limit")
                return
            self.total_bytes += len(content)
            blob = f"{prefix}/{path}"
            self.blobs[blob] = content
            self.files.append(
                CapturedFile.model_validate(
                    {
                        "path": path,
                        "blob": blob,
                        "mode": kind,
                        "sha256": digest(content),
                        "size": len(content),
                    }
                )
            )
        except (OSError, DryheaveError):
            self.omit(path, "unreadable_or_changed")


def _safe_link(
    path: str,
    entries: dict[str, CapturedFile],
    blobs: dict[str, bytes],
    directories: tuple[str, ...] = (),
) -> bool:
    target = blobs[entries[path].blob].decode(errors="replace")
    parts = list(Path(path).parent.parts) + target.split("/")
    if target.startswith("/"):
        return False
    resolved: list[str] = []
    hops = 0
    while parts:
        part = parts.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                return False
            resolved.pop()
            continue
        resolved.append(part)
        current = "/".join(resolved)
        entry = entries.get(current)
        if entry is not None and entry.mode == "symlink":
            hops += 1
            if hops > MAX_LINK_HOPS:
                return False
            link = blobs[entry.blob].decode(errors="replace")
            if link.startswith("/"):
                return False
            resolved.pop()
            parts = link.split("/") + parts
    result = "/".join(resolved)
    return (
        result in directories
        or result in entries
        or any(name.startswith(result + "/") for name in entries)
    )


def _filter_links(builder: CaptureBuilder) -> None:
    while True:
        entries = {entry.path: entry for entry in builder.files}
        rejected = [
            entry
            for entry in builder.files
            if entry.mode == "symlink"
            and not _safe_link(entry.path, entries, builder.blobs, tuple(builder.directories))
        ]
        if not rejected:
            return
        for entry in rejected:
            builder.files.remove(entry)
            del builder.blobs[entry.blob]
            builder.omit(entry.path, "symlink_escape_cycle_or_missing_target")


def _git_files(workspace: Path, policy: CapturePolicy) -> CaptureBuilder:
    builder = CaptureBuilder(policy)
    try:
        paths = builder.walk(workspace / ".git")
        for path, mode in sorted(paths.items()):
            if stat.S_ISLNK(mode):
                builder.omit(path, "git_symlink")
            else:
                builder.take(workspace / ".git", path, mode, prefix="git")
    except (OSError, DryheaveError):
        builder.omit(".", "git_directory_unavailable")
    if builder.blobs.get("git/objects/info/alternates", b"").strip():
        builder.omit("objects/info/alternates", "external_alternates_not_followed")
    return builder


def _ignored(
    workspace: Path, scratch: Path, git_builder: CaptureBuilder, policy: CapturePolicy
) -> Callable[[dict[str, int]], IgnoreSelection]:
    git = Git(scratch, SnapshotLimits())
    original_index = read_bytes(scratch / ".git/index", limit=32 * 1024 * 1024)
    if "git/index" in git_builder.blobs:
        atomic_write(scratch / ".git/index", git_builder.blobs["git/index"])
    try:
        tracked = set(
            git.run("ls-files", "--cached", "-z", limit=16 * 1024 * 1024)
            .stdout.decode()
            .split("\0")
        )
    finally:
        atomic_write(scratch / ".git/index", original_index)
    for previous in scratch.rglob(".gitignore"):
        if ".git" not in previous.relative_to(scratch).parts:
            previous.unlink()
    if "git/info/exclude" in git_builder.blobs:
        atomic_write(scratch / ".git/info/exclude", git_builder.blobs["git/info/exclude"])
    protected = tracked | set(policy.include_ignored)
    children: dict[str, set[str]] = {}
    for path in protected:
        child = Path(path)
        for parent in child.parents:
            children.setdefault(str(parent), set()).add(child.name)
            child = parent
    protected.update(children)

    def select(paths: dict[str, int]) -> IgnoreSelection:
        if not paths:
            return IgnoreSelection()
        for path, mode in paths.items():
            if Path(path).name == ".gitignore" and stat.S_ISREG(mode):
                atomic_write(scratch / path, read_bytes(workspace / path, limit=1024 * 1024))
        result = git.run(
            "check-ignore",
            "--no-index",
            "--stdin",
            "-z",
            input_bytes=b"".join(
                (path + ("/" if stat.S_ISDIR(mode) else "")).encode() + b"\0"
                for path, mode in paths.items()
            ),
            limit=16 * 1024 * 1024,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise InputError("Cannot establish frozen ignored-file policy.")
        ignored = {
            path
            for entry in result.stdout.decode().split("\0")
            if (path := entry.rstrip("/"))
            and not any(
                path == name or path.startswith(name + "/") for name in policy.include_ignored
            )
        }
        return IgnoreSelection(
            omitted=ignored - protected,
            children={
                path: children.get(path, set())
                for path in ignored & protected
                if stat.S_ISDIR(paths[path])
            },
        )

    return select


def materialize_files(
    root: Path,
    entries: tuple[CapturedFile, ...],
    blobs: dict[str, bytes],
    *,
    directories: tuple[str, ...] = (),
) -> None:
    WorkspaceCapture(files=entries, directories=directories, complete=False, git_complete=False)
    indexed = {entry.path: entry for entry in entries}
    if any(
        entry.mode == "symlink" and not _safe_link(entry.path, indexed, blobs, directories)
        for entry in entries
    ):
        raise InputError("Captured symlink cannot be safely reconstructed.")
    ensure_directory(root)
    for directory in directories:
        ensure_directory(root / directory)
    for entry in entries:
        path = root / entry.path
        content = blobs[entry.blob]
        if digest(content) != entry.sha256 or len(content) != entry.size:
            raise InputError("Captured file hash or size differs from its manifest.")
        if entry.mode == "symlink":
            ensure_directory(path.parent)
            with directory_fd(path.parent) as parent:
                os.symlink(content.decode(), path.name, dir_fd=parent)
        else:
            atomic_write(path, content, replace=False)
            os.chmod(path, 0o700 if entry.mode == "executable" else 0o600, follow_symlinks=False)


def _patch(scratch: Path, builder: CaptureBuilder, commit: str) -> bytes:
    for path in scratch.iterdir():
        if path.name != ".git":
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    materialize_files(
        scratch, tuple(builder.files), builder.blobs, directories=tuple(builder.directories)
    )
    git = Git(scratch, SnapshotLimits(max_pack_bytes=128 * 1024 * 1024))
    git.run("add", "--all", "--force", ".")
    return git.run(
        "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", commit, "--"
    ).stdout


def _inventory(root: Path, git_builder: CaptureBuilder, object_format: str) -> bytes:
    ensure_directory(root)
    git = Git(root, SnapshotLimits())
    git.run("init", "--quiet", "--template=", "--object-format=" + object_format, ".")
    for entry in git_builder.files:
        if entry.path.startswith("objects/") and entry.path != "objects/info/alternates":
            atomic_write(root / ".git" / entry.path, git_builder.blobs[entry.blob])
    return git.run(
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype)",
        limit=32 * 1024 * 1024,
    ).stdout


def capture_workspace(
    store: ObjectStore,
    repository_id: str,
    workspace: Path,
    policy: CapturePolicy,
    scratch_root: Path,
) -> tuple[WorkspaceCapture, dict[str, bytes]]:
    repository = load_repository(store, repository_id)
    builder = CaptureBuilder(policy)
    git_builder = _git_files(
        workspace,
        CapturePolicy(
            max_files=repository.limits.max_files,
            max_bytes=repository.limits.max_pack_bytes,
            max_depth=64,
        ),
    )
    ensure_directory(scratch_root)
    with TemporaryDirectory(prefix="capture-", dir=scratch_root) as temporary:
        scratch = Path(temporary) / "workspace"
        materialize_repository(store, repository_id, scratch)
        try:
            paths = builder.walk(
                workspace, ignored=_ignored(workspace, scratch, git_builder, policy)
            )
            for path, mode in sorted(paths.items()):
                builder.take(workspace, path, mode, prefix="workspace")
        except (OSError, DryheaveError, UnicodeError):
            builder.omit(".", "capture_enumeration_failed")
        _filter_links(builder)
        patch_file = inventory_file = None
        try:
            builder.blobs["final.patch"] = _patch(scratch, builder, repository.commit)
            patch_file = "final.patch"
        except (OSError, DryheaveError):
            builder.omit(".", "patch_failed")
        try:
            builder.blobs["git-inventory.txt"] = _inventory(
                Path(temporary) / "audit", git_builder, repository.object_format
            )
            inventory_file = "git-inventory.txt"
        except (OSError, DryheaveError):
            git_builder.omit(".", "git_inventory_failed")
    omissions = tuple(builder.omissions) + tuple(
        item.model_copy(update={"path": posixpath.join(".git", item.path)})
        for item in git_builder.omissions
    )
    result = WorkspaceCapture(
        directories=tuple(builder.directories),
        git_directories=tuple(git_builder.directories),
        files=tuple(builder.files),
        git_files=tuple(git_builder.files),
        omissions=omissions,
        patch_file=patch_file,
        git_inventory_file=inventory_file,
        complete=not any(not item.intentional for item in builder.omissions),
        git_complete=not git_builder.omissions,
    )
    return result, builder.blobs | git_builder.blobs
