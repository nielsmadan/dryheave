import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from dryheave.errors import LimitError, LockBusyError, PathError


@contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    absolute = path.absolute()
    search_flags = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY)) | os.O_DIRECTORY
    descriptor = os.open(absolute.anchor, search_flags)
    try:
        for part in absolute.parts[1:]:
            if part in {".", ".."}:
                raise PathError("Directory paths must not contain dot or parent components.")
            try:
                child = os.open(part, search_flags | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                with _readable_directory(descriptor) as readable:
                    os.fsync(readable)
                child = os.open(part, search_flags | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PathError(f"Expected a directory without symlinks: {path}") from error
                raise
            os.close(descriptor)
            descriptor = child
        with _readable_directory(descriptor) as readable:
            yield readable
    finally:
        os.close(descriptor)


@contextmanager
def _readable_directory(descriptor: int) -> Iterator[int]:
    readable = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        yield readable
    finally:
        os.close(readable)


def ensure_directory(path: Path) -> None:
    with directory_fd(path, create=True):
        pass


def _regular(descriptor: int, path: Path) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise PathError(f"Expected a regular file: {path}")


@contextmanager
def regular_fd(path: Path, flags: int, *, create_parent: bool = False) -> Iterator[int]:
    with directory_fd(path.parent, create=create_parent) as parent:
        try:
            descriptor = os.open(
                path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode=0o600, dir_fd=parent
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise PathError(f"Refusing a symlink: {path}") from error
            raise
        try:
            _regular(descriptor, path)
            yield descriptor
        finally:
            os.close(descriptor)


def read_bytes(path: Path, *, limit: int) -> bytes:
    with regular_fd(path, os.O_RDONLY) as descriptor:
        if os.fstat(descriptor).st_size > limit:
            raise LimitError(f"File exceeds the {limit}-byte limit: {path}")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise LimitError(f"File exceeds the {limit}-byte limit: {path}")
        return content


def write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        count = os.write(descriptor, remaining)
        if count == 0:
            raise OSError("File write made no progress.")
        remaining = remaining[count:]


def atomic_write(path: Path, content: bytes, *, replace: bool = True) -> None:
    with directory_fd(path.parent, create=True) as parent:
        temporary = f".{path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            try:
                write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if replace:
                try:
                    existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(existing.st_mode):
                        raise PathError(f"Expected a regular destination file: {path}")
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    with regular_fd(path, os.O_RDWR | os.O_CREAT, create_parent=True) as descriptor:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusyError(
                f"Operation already in progress; retry after it finishes: {path}"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def publish_directory(staged: Path, destination: Path) -> None:
    if staged.parent != destination.parent:
        raise PathError("Directory publication requires the same parent.")
    with directory_fd(staged.parent) as parent:
        try:
            os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(staged.name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        else:
            raise FileExistsError(destination)


def directory_names(path: Path) -> set[str]:
    with directory_fd(path) as descriptor:
        return set(os.listdir(descriptor))
