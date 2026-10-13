import os
import stat
import threading
from collections.abc import Mapping
from pathlib import Path

from dryheave.errors import DryheaveError
from dryheave.filesystem import directory_fd

MAX_CONTROLLER_DEPTH = 16
MAX_CONTROLLER_FILES = 256
MAX_RUNTIME_FILES = 4096
MAX_RUNTIME_BYTES = 64 * 1024 * 1024
FILE_LIMIT = "controller_file_limit"
DEPTH_LIMIT = "controller_file_depth"
RESPONSE_FILE_LIMIT = "controller_response_file_limit"
SPECIAL_FILE = "controller_special_file"
OBSERVATION_FAILED = "controller_file_observation_failed"
CONTROLLER_LIMIT_ERRORS = frozenset({FILE_LIMIT, DEPTH_LIMIT, RESPONSE_FILE_LIMIT})
CHILD_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ControllerWatch:
    def __init__(
        self,
        root: Path | tuple[Path, ...],
        parent_cancelled: threading.Event,
        *,
        max_bytes: int,
        max_files: int = MAX_CONTROLLER_FILES,
        output_bytes: int = 65536,
        owned_symlinks: Mapping[Path, tuple[int, int, int]] | None = None,
    ) -> None:
        self.roots = (root,) if isinstance(root, Path) else root
        self.root, self.parent_cancelled = self.roots[0], parent_cancelled
        self.owned_symlinks = dict(owned_symlinks or {})
        self.max_bytes, self.max_files = max_bytes, max_files
        self.output_bytes = output_bytes
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _measure(self, root: Path) -> tuple[int, int]:
        with directory_fd(root) as descriptor:
            return self._measure_directory(descriptor, root, 0)

    def _measure_directory(self, descriptor: int, path: Path, depth: int) -> tuple[int, int]:
        if depth > MAX_CONTROLLER_DEPTH:
            raise ValueError(DEPTH_LIMIT)
        size = count = 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                count += 1
                if count > self.max_files:
                    raise ValueError(FILE_LIMIT)
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child_size, child_count = self._measure_child(
                            descriptor, path, entry.name, depth + 1
                        )
                    except FileNotFoundError:
                        continue
                    size += child_size
                    count += child_count
                elif stat.S_ISREG(info.st_mode):
                    size += info.st_size
                    if (
                        path == self.root
                        and entry.name == "response.json"
                        and info.st_size > self.output_bytes
                    ):
                        raise ValueError(RESPONSE_FILE_LIMIT)
                elif stat.S_ISLNK(info.st_mode) and self.owned_symlinks.get(path / entry.name) == (
                    info.st_dev,
                    info.st_ino,
                    info.st_ctime_ns,
                ):
                    pass
                else:
                    raise ValueError(SPECIAL_FILE)
                if size > self.max_bytes or count > self.max_files:
                    raise ValueError(FILE_LIMIT)
        return size, count

    def _measure_child(self, parent: int, path: Path, name: str, depth: int) -> tuple[int, int]:
        child = os.open(name, CHILD_DIRECTORY_FLAGS, dir_fd=parent)
        try:
            return self._measure_directory(child, path / name, depth)
        finally:
            os.close(child)

    def _check(self) -> None:
        if self.parent_cancelled.is_set():
            self.cancelled.set()
        try:
            size = count = 0
            for root in self.roots:
                root_size, root_count = self._measure(root)
                size += root_size
                count += root_count
                if size > self.max_bytes or count > self.max_files:
                    raise ValueError(FILE_LIMIT)
        except ValueError as error:
            self.error = str(error)
            self.cancelled.set()
        except (OSError, DryheaveError):
            self.error = OBSERVATION_FAILED
            self.cancelled.set()

    def _watch(self) -> None:
        while not self.done.wait(0.02):
            self._check()
            if self.error is not None:
                return

    def close(self) -> None:
        self.done.set()
        self.thread.join(timeout=1)
        self._check()
