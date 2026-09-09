import io
import os
import re
import shutil
import tarfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from dryheave.constants import MAX_BLOB_BYTES
from dryheave.errors import InputError, IntegrityError, LimitError
from dryheave.filesystem import atomic_write, ensure_directory, read_bytes
from dryheave.models import (
    CommandSpec,
    ObjectKind,
    RelativePath,
    StrictModel,
    validate_relative_path,
)
from dryheave.processes import CommandResult, run_command
from dryheave.storage import ObjectStore

MAX_TREE_DEPTH = 32
MAX_SYMLINK_DEPTH = 40
SHA1_LENGTH = 40

GitSha = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class SnapshotLimits(StrictModel):
    max_files: int = Field(default=20000, gt=0, le=100000)
    max_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=128 * 1024 * 1024)
    max_pack_bytes: int = Field(default=128 * 1024 * 1024, gt=0, le=MAX_BLOB_BYTES)
    max_objects: int = Field(default=100000, gt=0, le=1000000)
    max_disallowed_commits: int = Field(default=10000, gt=0, le=100000)
    max_seconds: int = Field(default=120, gt=0, le=600)


class TreeEntry(StrictModel):
    path: RelativePath
    mode: Literal["100644", "100755", "120000"]
    oid: GitSha
    size: int = Field(ge=0)


class RepositorySnapshot(StrictModel):
    schema_version: Literal[1] = 1
    commit: GitSha
    tree: GitSha
    object_format: Literal["sha1", "sha256"]
    history_mode: Literal["ancestry"] = "ancestry"
    archive_file: RelativePath = "tree.tar"
    pack_file: RelativePath = "ancestry.pack"
    patch_file: RelativePath | None = None
    entries: tuple[TreeEntry, ...]
    limits: SnapshotLimits = Field(default_factory=SnapshotLimits)
    source_path: str
    source_git_path: str
    known_disallowed_commits: tuple[GitSha, ...] = ()
    audit_coverage: Literal["complete_at_capture", "partial"] = "partial"
    audit_limit: int
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded_entries(self) -> Self:
        if (
            len(self.entries) > self.limits.max_files
            or sum(item.size for item in self.entries) > self.limits.max_bytes
        ):
            raise ValueError("snapshot entries exceed the declared bounds")
        if len({entry.path for entry in self.entries}) != len(self.entries):
            raise ValueError("snapshot entry paths must be unique")
        if self.commit in self.known_disallowed_commits:
            raise ValueError("baseline cannot be a disallowed commit")
        return self


class Git:
    def __init__(self, root: Path, limits: SnapshotLimits, *, objects: Path | None = None) -> None:
        self.root = root
        self.limits = limits
        self.deadline = time.monotonic() + limits.max_seconds
        self.environment = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        if objects is not None:
            self.environment["GIT_OBJECT_DIRECTORY"] = str(objects)

    def run(
        self, *argv: str, input_bytes: bytes = b"", limit: int | None = None, check: bool = True
    ) -> CommandResult:
        remaining = int(self.deadline - time.monotonic())
        if remaining <= 0:
            raise LimitError("Repository operation exceeded its total time budget.")
        result = run_command(
            CommandSpec(
                argv=(
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.attributesFile=/dev/null",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.logAllRefUpdates=false",
                    *argv,
                ),
                timeout_seconds=remaining,
                max_output_bytes=limit or self.limits.max_pack_bytes,
            ),
            self.root,
            input_bytes=input_bytes,
            environment=self.environment,
        )
        if result.outcome != "exited":
            raise LimitError(f"Git command stopped: {result.outcome}.")
        if check and result.returncode:
            raise InputError(
                f"Git {argv[0]} failed (exit {result.returncode}); verify the selected SHA and repository objects."
            )
        return result


def _source_git(repo: Path) -> tuple[Path, Path]:
    if repo.is_symlink() or not repo.is_dir():
        raise InputError("Source repository must be a real directory.")
    git_dir = repo / ".git"
    if git_dir.is_file():
        value = read_bytes(git_dir, limit=8192).decode("utf-8").strip()
        if not value.startswith("gitdir: "):
            raise InputError("Invalid source Git directory pointer.")
        git_dir = (repo / value.removeprefix("gitdir: ")).resolve()
    elif not git_dir.is_dir():
        raise InputError("Source must be a Git working tree with .git metadata.")
    if git_dir.is_symlink():
        raise InputError("Symlink Git metadata is unsupported.")
    common = git_dir
    if (git_dir / "commondir").is_file():
        common = (
            git_dir / read_bytes(git_dir / "commondir", limit=8192).decode().strip()
        ).resolve()
    objects = common / "objects"
    if objects.is_symlink() or not objects.is_dir():
        raise InputError("Source object directory is unsupported.")
    if (objects / "info" / "alternates").exists() or (common / "shallow").exists():
        raise InputError(
            "Alternate object stores and shallow history are unsupported; supply complete local ancestry."
        )
    if any((objects / "pack").glob("*.promisor")):
        raise InputError("Partial-clone/promisor object stores are unsupported.")
    return common.absolute(), objects.absolute()


@contextmanager
def _scratch(store: ObjectStore) -> Iterator[Path]:
    parent = store.root / "scratch"
    ensure_directory(parent)
    with TemporaryDirectory(prefix="repository-", dir=parent) as temporary:
        yield Path(temporary)


def _initialize(git: Git, object_format: str) -> None:
    git.run(
        "init",
        "--quiet",
        "--template=",
        f"--object-format={object_format}",
        "--initial-branch=baseline",
        ".",
    )
    atomic_write(
        git.root / ".git" / "config",
        b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tlogAllRefUpdates = false\n\thooksPath = /dev/null\n\tautocrlf = false\n"
        if object_format == "sha1"
        else b"[core]\n\trepositoryformatversion = 1\n\tbare = false\n\tlogAllRefUpdates = false\n\thooksPath = /dev/null\n\tautocrlf = false\n[extensions]\n\tobjectFormat = sha256\n",
    )


def _safe_tree_path(path: str) -> None:
    validate_relative_path(path)
    if len(path.split("/")) > MAX_TREE_DEPTH or any(
        part.casefold().rstrip(" .") == ".git" or part.lower().startswith("git~")
        for part in path.split("/")
    ):
        raise InputError("Tracked path conflicts with Git metadata or exceeds the depth limit.")


def _tree(git: Git, commit: str) -> tuple[list[tuple[str, str, str]], dict[str, bytes]]:
    raw = git.run("ls-tree", "-rz", "--full-tree", commit, limit=git.limits.max_files * 2048).stdout
    rows = raw.rstrip(b"\0").split(b"\0") if raw else []
    if len(rows) > git.limits.max_files:
        raise LimitError("Repository exceeds the tracked file limit.")
    entries: list[tuple[str, str, str]] = []
    for row in rows:
        header, name = row.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        try:
            path = name.decode("utf-8")
            _safe_tree_path(path)
        except (UnicodeError, ValueError) as error:
            raise InputError("Tracked path is not a safe UTF-8 relative path.") from error
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise InputError("Submodules and unsupported tracked file types cannot be snapshotted.")
        entries.append((mode, oid, path))
    identifiers = list(dict.fromkeys(oid for _, oid, _ in entries))
    output = git.run(
        "cat-file",
        "--batch",
        input_bytes="".join(f"{oid}\n" for oid in identifiers).encode(),
        limit=git.limits.max_bytes + len(identifiers) * 128,
    ).stdout
    blobs: dict[str, bytes] = {}
    position = 0
    for oid in identifiers:
        end = output.index(b"\n", position)
        found, kind, length = output[position:end].decode().split()
        if found != oid or kind != "blob":
            raise IntegrityError("Unexpected Git blob response.")
        size = int(length)
        blobs[oid] = output[end + 1 : end + 1 + size]
        position = end + size + 2
    if sum(len(blobs[oid]) for _, oid, _ in entries) > git.limits.max_bytes:
        raise LimitError("Repository exceeds the tracked byte limit.")
    _validate_content(entries, blobs)
    return entries, blobs


def _validate_content(entries: list[tuple[str, str, str]], blobs: dict[str, bytes]) -> None:
    paths = {path for _, _, path in entries}
    folded: set[str] = set()
    links: dict[str, str] = {}
    for mode, oid, path in entries:
        if path.casefold() in folded:
            raise InputError("Case-colliding tracked paths are unsupported.")
        folded.add(path.casefold())
        content = blobs[oid]
        if content.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise InputError("Git LFS pointers require an unsupported external dependency.")
        if mode == "120000":
            try:
                target = content.decode("utf-8")
            except UnicodeError as error:
                raise InputError("Symlink target is not UTF-8.") from error
            links[path] = target
    _validate_symlinks(paths, links)


def _validate_symlinks(paths: set[str], links: dict[str, str]) -> None:
    directories = {""} | {
        path[:index] for path in paths for index, character in enumerate(path) if character == "/"
    }

    def resolve(parent: list[str], target: str, active: set[str]) -> list[str]:
        if target.startswith("/") or "\\" in target or "\x00" in target:
            raise InputError("External symlinks are unsupported.")
        if not target:
            raise InputError("Dangling symlinks are unsupported.")
        current = parent.copy()
        for component in target.split("/"):
            if "/".join(current) not in directories:
                raise InputError("Symlink target traverses a non-directory component.")
            if component in {"", "."}:
                continue
            if component == "..":
                if not current:
                    raise InputError("External symlinks are unsupported.")
                current.pop()
                continue
            candidate = "/".join((*current, component))
            if candidate in links:
                if candidate in active:
                    raise InputError("Cyclic symlinks are unsupported.")
                if len(active) >= MAX_SYMLINK_DEPTH:
                    raise LimitError("Symlink resolution exceeds the depth limit.")
                current = resolve(current, links[candidate], active | {candidate})
            elif candidate in paths or candidate in directories:
                current.append(component)
            else:
                raise InputError("Dangling symlinks are unsupported.")
        return current

    for path, target in links.items():
        parent = path.split("/")[:-1]
        resolve(parent, target, {path})


def _archive(
    entries: list[tuple[str, str, str]], blobs: dict[str, bytes]
) -> tuple[bytes, tuple[TreeEntry, ...]]:
    output = io.BytesIO()
    tracked: list[TreeEntry] = []
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for mode, oid, path in entries:
            content = blobs[oid]
            info = tarfile.TarInfo(path)
            info.mode = 0o755 if mode == "100755" else 0o644
            info.size = len(content)
            if mode == "120000":
                info.type, info.linkname, info.size = tarfile.SYMTYPE, content.decode(), 0
            archive.addfile(info, io.BytesIO(content) if info.isfile() else None)
            tracked.append(
                TreeEntry.model_validate(
                    {"path": path, "mode": mode, "oid": oid, "size": len(content)}
                )
            )
    return output.getvalue(), tuple(tracked)


def _audit(git: Git, commit: str) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    ancestry = set(
        git.run("rev-list", commit, limit=git.limits.max_objects * 65).stdout.decode().splitlines()
    )
    try:
        result = git.run(
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname) %(objecttype)",
            "--unordered",
            limit=git.limits.max_objects * 80,
        )
    except LimitError:
        return (), "partial", ("Known future-object inventory exceeded the capture bound.",)
    commits = sorted(
        row.split()[0]
        for row in result.stdout.decode().splitlines()
        if row.endswith(" commit") and row.split()[0] not in ancestry
    )
    maximum = git.limits.max_disallowed_commits
    if len(commits) > maximum:
        return (
            tuple(commits[:maximum]),
            "partial",
            ("Known future-commit list was truncated at its recorded limit.",),
        )
    return (
        tuple(commits),
        "complete_at_capture",
        (
            "Audit inventory covers locally present commits at capture only; later source history remains unknown.",
        ),
    )


def capture_repository(
    store: ObjectStore,
    repo: Path,
    commit: str,
    *,
    initial_patch: bytes | None = None,
    limits: SnapshotLimits | None = None,
) -> str:
    limits = limits or SnapshotLimits()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise InputError(
            "An explicit full lowercase starting commit SHA is required; branches and HEAD are not accepted."
        )
    if initial_patch is not None and (
        not initial_patch.strip() or len(initial_patch) > limits.max_bytes
    ):
        raise InputError("Initial patch must be nonempty and within the snapshot byte limit.")
    common, objects = _source_git(repo)
    object_format = "sha1" if len(commit) == SHA1_LENGTH else "sha256"
    with _scratch(store) as temporary:
        git = Git(temporary, limits)
        _initialize(git, object_format)
        git.environment["GIT_OBJECT_DIRECTORY"] = str(objects)
        actual = git.run("rev-parse", "--verify", f"{commit}^{{commit}}").stdout.decode().strip()
        if actual != commit:
            raise InputError("Starting SHA must identify a commit object directly.")
        tree = git.run("rev-parse", f"{commit}^{{tree}}").stdout.decode().strip()
        reachable = git.run(
            "rev-list", "--objects", "--no-object-names", commit, limit=limits.max_objects * 65
        ).stdout
        if len(reachable.splitlines()) > limits.max_objects:
            raise LimitError("Baseline ancestry exceeds the object limit.")
        entries, blobs = _tree(git, commit)
        archive, tracked = _archive(entries, blobs)
        pack = git.run(
            "pack-objects",
            "--stdout",
            "--revs",
            "--no-reuse-delta",
            "--no-reuse-object",
            input_bytes=f"{commit}\n".encode(),
        ).stdout
        disallowed, coverage, warnings = _audit(git, commit)
    snapshot = RepositorySnapshot.model_validate(
        {
            "commit": commit,
            "tree": tree,
            "object_format": object_format,
            "entries": tracked,
            "source_path": str(repo.absolute()),
            "source_git_path": str(common),
            "known_disallowed_commits": disallowed,
            "audit_coverage": coverage,
            "audit_limit": limits.max_disallowed_commits,
            "warnings": warnings,
            "limits": limits,
            "patch_file": "initial.patch" if initial_patch is not None else None,
        }
    )
    files = {"tree.tar": archive, "ancestry.pack": pack}
    if initial_patch is not None:
        files["initial.patch"] = initial_patch
    with _scratch(store) as temporary:
        _materialize(snapshot, files, temporary / "verify")
    return store.put(ObjectKind.REPOSITORY, snapshot, files=files)


def _write_tree(root: Path, content: bytes, snapshot: RepositorySnapshot) -> None:
    expected = {item.path: item for item in snapshot.entries}
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        for member in archive:
            _safe_tree_path(member.name)
            if member.name in seen or member.name not in expected:
                raise IntegrityError("Archive has duplicate or unexpected entries.")
            seen.add(member.name)
            entry = expected[member.name]
            if member.size > snapshot.limits.max_bytes or member.size < 0:
                raise LimitError("Archive entry exceeds the byte limit.")
            target = root / member.name
            ensure_directory(target.parent)
            if entry.mode == "120000" and member.issym():
                if len(member.linkname.encode()) != entry.size:
                    raise IntegrityError("Symlink archive size mismatch.")
                os.symlink(member.linkname, target)
            elif entry.mode != "120000" and member.isfile() and member.size == entry.size:
                reader = archive.extractfile(member)
                if reader is None:
                    raise IntegrityError("Archive file content missing.")
                atomic_write(target, reader.read(snapshot.limits.max_bytes + 1), replace=False)
                target.chmod(0o755 if entry.mode == "100755" else 0o644)
            else:
                raise IntegrityError("Archive entry does not match its declared tree type.")
    if seen != set(expected):
        raise IntegrityError("Archive omitted tracked entries.")


def materialize_repository(
    store: ObjectStore, reference: str, destination: Path
) -> RepositorySnapshot:
    snapshot = load_repository(store, reference)
    files = {name: store.read_blob(reference, name) for name in store.get(reference).files}
    _materialize(snapshot, files, destination)
    return snapshot


def _materialize(snapshot: RepositorySnapshot, files: dict[str, bytes], destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise InputError("Repository destination must not already exist.")
    ensure_directory(destination.parent)
    destination.mkdir(mode=0o700)
    try:
        git = Git(destination, snapshot.limits)
        _initialize(git, snapshot.object_format)
        git.run(
            "index-pack",
            "--stdin",
            "--strict",
            input_bytes=files[snapshot.pack_file],
        )
        _verify_pack_closure(git, snapshot.commit)
        if (
            git.run("rev-parse", f"{snapshot.commit}^{{tree}}").stdout.decode().strip()
            != snapshot.tree
        ):
            raise IntegrityError("Snapshot tree identity does not match its baseline commit.")
        entries, blobs = _tree(git, snapshot.commit)
        archive, tracked = _archive(entries, blobs)
        if tracked != snapshot.entries or archive != files[snapshot.archive_file]:
            raise IntegrityError("Repository archive differs from the selected Git tree.")
        git.run("read-tree", snapshot.commit)
        if snapshot.patch_file:
            git.run(
                "apply",
                "--cached",
                "--check",
                "--binary",
                "-",
                input_bytes=files[snapshot.patch_file],
            )
            git.run(
                "apply",
                "--cached",
                "--binary",
                "-",
                input_bytes=files[snapshot.patch_file],
            )
            tree = git.run("write-tree").stdout.decode().strip()
            entries, blobs = _tree(git, tree)
            archive, tracked = _archive(entries, blobs)
            git.run("read-tree", snapshot.commit)
        _write_tree(destination, archive, snapshot.model_copy(update={"entries": tracked}))
        git.run("update-ref", "refs/heads/baseline", snapshot.commit)
    except BaseException:
        shutil.rmtree(destination)
        raise


def _verify_pack_closure(git: Git, commit: str) -> None:
    reachable = set(
        git.run(
            "rev-list", "--objects", "--no-object-names", commit, limit=git.limits.max_objects * 65
        ).stdout.splitlines()
    )
    present = set(
        git.run(
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
            limit=git.limits.max_objects * 65,
        ).stdout.splitlines()
    )
    if present != reachable:
        raise IntegrityError(
            "Repository pack contains objects outside the exact baseline ancestry."
        )


def load_repository(store: ObjectStore, reference: str) -> RepositorySnapshot:
    snapshot = store.load(reference, RepositorySnapshot, kind=ObjectKind.REPOSITORY)
    manifest = store.get(reference, kind=ObjectKind.REPOSITORY)
    expected = {snapshot.archive_file, snapshot.pack_file}
    if snapshot.patch_file:
        expected.add(snapshot.patch_file)
    if manifest.references or set(manifest.files) != expected:
        raise InputError("Repository manifest does not match its self-contained snapshot inputs.")
    return snapshot
