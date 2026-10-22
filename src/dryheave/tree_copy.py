import errno
import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import NamedTuple

from dryheave.errors import InputError
from dryheave.filesystem import directory_fd, write_all

CHUNK_BYTES = 1024 * 1024
_SEARCH = getattr(os, "O_SEARCH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_DANGLING = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})

type Identity = tuple[int, int]
type Screen = Callable[[str, bytes], object]
type Admit = Callable[[str, bool], bool]
type LinkTarget = Callable[[str, str], None]


class TreeLimits(NamedTuple):
    files: int
    directories: int
    bytes: int
    screened_bytes: int


class TreePolicy(NamedTuple):
    dereference: bool
    screen: Screen | None = None
    admit: Admit | None = None
    link_target: LinkTarget | None = None


class CopiedTree(NamedTuple):
    records: dict[str, str]
    bytes: int
    parsed: dict[str, object]


def _identity(status: os.stat_result) -> Identity:
    return status.st_dev, status.st_ino


def _bounds() -> InputError:
    return InputError("Live config exceeds the snapshot bounds.")


def _moved(relative: str) -> InputError:
    return InputError(f"Live config entry {relative} changed while it was copied.")


def _outside(relative: str) -> InputError:
    return InputError(f"Live config link {relative} points outside the snapshot entries.")


def _lstat(parent: int, name: str, relative: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError as error:
        raise _moved(relative) from error


def _reopen(parent: int, name: str, flags: int, status: os.stat_result, relative: str) -> int:
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        if error.errno in _DANGLING:
            raise _moved(relative) from error
        raise
    if _identity(os.fstat(descriptor)) != _identity(status):
        os.close(descriptor)
        raise _moved(relative)
    return descriptor


class _Place(NamedTuple):
    relative: str
    real: str
    target: int | None
    ancestors: frozenset[Identity]
    linked: bool = False

    @property
    def name(self) -> str:
        return self.relative.rpartition("/")[2]


class _Walk:
    def __init__(self, *, policy: TreePolicy, limits: TreeLimits, read: bool) -> None:
        self.policy = policy
        self.limits = limits
        self.read = read
        self.records: dict[str, str] = {}
        self.parsed: dict[str, object] = {}
        self.files = self.directories = self.total = 0
        self.root: Identity = (-1, -1)
        self.entries: frozenset[Identity] = frozenset()

    def tree(self, root: Path, entries: tuple[str, ...], destination: Path | None) -> CopiedTree:
        with ExitStack() as stack:
            source = stack.enter_context(directory_fd(root))
            target = None if destination is None else stack.enter_context(directory_fd(destination))
            self.root = _identity(os.fstat(source))
            self.entries = frozenset(_identity(_lstat(source, entry, entry)) for entry in entries)
            for entry in entries:
                self._visit(source, entry, _Place(entry, f"{root}/{entry}", target, frozenset()))
        return CopiedTree(self.records, self.total, self.parsed)

    def _visit(self, parent: int, name: str, place: _Place) -> None:
        status = _lstat(parent, name, place.relative)
        admit = self.policy.admit
        if admit is not None and not admit(place.relative, stat.S_ISDIR(status.st_mode)):
            return
        if place.linked and self.policy.link_target is not None:
            self.policy.link_target(place.relative, place.real)
        if not stat.S_ISLNK(status.st_mode):
            self._node(parent, name, status, place)
        elif not self.policy.dereference:
            self._link(parent, name, place)
        elif "/" not in place.relative:
            raise InputError(
                f"Live config entry {place.relative} is a symlink, refusing to snapshot it."
            )
        else:
            self._follow(parent, name, place)

    def _link(self, parent: int, name: str, place: _Place) -> None:
        try:
            text = os.readlink(name, dir_fd=parent)
        except OSError as error:
            raise _moved(place.relative) from error
        self.files += 1
        if self.files > self.limits.files:
            raise _bounds()
        self.records[place.relative] = "-> " + text
        if place.target is not None:
            os.symlink(text, place.name, dir_fd=place.target)

    def _follow(self, parent: int, name: str, place: _Place) -> None:
        link = os.readlink(name, dir_fd=parent)
        location = os.path.realpath(os.path.join(os.path.dirname(place.real), link))
        resolved = self._resolve(Path(location), place.relative)
        if resolved is None:
            return
        descriptor, status = resolved
        try:
            if self.policy.link_target is not None:
                self.policy.link_target(place.relative, location)
            self._node(
                descriptor,
                Path(location).name,
                status,
                place._replace(real=location, linked=True),
            )
        finally:
            os.close(descriptor)

    def _resolve(self, location: Path, relative: str) -> tuple[int, os.stat_result] | None:
        if location.parent == location:
            raise _outside(relative)
        descriptor = os.open(location.anchor, _SEARCH)
        chain = {_identity(os.fstat(descriptor))}
        try:
            for part in location.parent.parts[1:]:
                child = os.open(part, _SEARCH, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                chain.add(_identity(os.fstat(descriptor)))
            status = os.stat(location.name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            os.close(descriptor)
            if error.errno in _DANGLING:
                return None
            raise
        chain.add(_identity(status))
        if stat.S_ISLNK(status.st_mode):
            os.close(descriptor)
            return None
        if self.root in chain and not self.entries & chain:
            os.close(descriptor)
            raise _outside(relative)
        return descriptor, status

    def _node(self, parent: int, name: str, status: os.stat_result, place: _Place) -> None:
        if stat.S_ISDIR(status.st_mode):
            self._directory(parent, name, status, place)
        elif stat.S_ISREG(status.st_mode):
            self._file(parent, name, status, place)
        else:
            raise InputError(f"Live config entry {place.relative} is not a regular file.")

    def _directory(self, parent: int, name: str, status: os.stat_result, place: _Place) -> None:
        key = _identity(status)
        if key in place.ancestors:
            raise InputError(f"Live config link {place.relative} forms a cycle.")
        if self.policy.dereference and key == self.root:
            raise _outside(place.relative)
        self.directories += 1
        if self.directories > self.limits.directories:
            raise _bounds()
        with ExitStack() as stack:
            descriptor = _reopen(parent, name, _DIRECTORY, status, place.relative)
            stack.callback(os.close, descriptor)
            copy = None
            if place.target is not None:
                os.mkdir(place.name, mode=0o700, dir_fd=place.target)
                copy = os.open(place.name, _DIRECTORY, dir_fd=place.target)
                stack.callback(os.close, copy)
            ancestors = place.ancestors | {key}
            try:
                children = sorted(os.listdir(descriptor))
            except FileNotFoundError as error:
                raise _moved(place.relative) from error
            for child in children:
                self._visit(
                    descriptor,
                    child,
                    _Place(
                        f"{place.relative}/{child}",
                        f"{place.real}/{child}",
                        copy,
                        ancestors,
                        place.linked,
                    ),
                )
            if copy is not None:
                os.fchmod(copy, stat.S_IMODE(status.st_mode))

    def _file(self, parent: int, name: str, status: os.stat_result, place: _Place) -> None:
        self.files += 1
        if self.files > self.limits.files or self.total + status.st_size > self.limits.bytes:
            raise _bounds()
        if not self.read:
            self.total += status.st_size
            return
        with ExitStack() as stack:
            descriptor = _reopen(parent, name, _FILE, status, place.relative)
            stack.callback(os.close, descriptor)
            copy = None
            if place.target is not None:
                copy = os.open(place.name, _CREATE, 0o600, dir_fd=place.target)
                stack.callback(os.close, copy)
            content = self._read(descriptor, copy, place.relative)
            if copy is not None:
                os.fchmod(copy, stat.S_IMODE(status.st_mode))
                os.utime(copy, ns=(status.st_atime_ns, status.st_mtime_ns))
        if self.policy.screen is not None:
            parsed = self.policy.screen(place.relative, content)
            if parsed is not None:
                self.parsed[place.relative] = parsed

    def _read(self, descriptor: int, copy: int | None, relative: str) -> bytes:
        hashed = hashlib.sha256()
        kept = bytearray()
        remaining = self.limits.bytes - self.total
        while chunk := os.read(descriptor, min(CHUNK_BYTES, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise _bounds()
            hashed.update(chunk)
            if copy is not None:
                write_all(copy, chunk)
            if self.policy.screen is not None:
                if len(kept) + len(chunk) > self.limits.screened_bytes:
                    raise InputError(
                        f"Live config file {relative} exceeds the "
                        f"{self.limits.screened_bytes}-byte credential screening limit."
                    )
                kept += chunk
        self.total = self.limits.bytes - remaining
        self.records[relative] = hashed.hexdigest()
        return bytes(kept)


def copy_tree(
    root: Path,
    entries: tuple[str, ...],
    destination: Path | None,
    *,
    policy: TreePolicy,
    limits: TreeLimits,
    read: bool = True,
) -> CopiedTree:
    return _Walk(policy=policy, limits=limits, read=read).tree(root, entries, destination)
