import os
import stat
import threading
from pathlib import Path

from dryheave.errors import DryheaveError
from dryheave.filesystem import directory_fd

MAX_CONTROLLER_DEPTH = 16


class ControllerWatch:
    def __init__(
        self,
        root: Path,
        parent_cancelled: threading.Event,
        *,
        max_bytes: int,
        max_files: int = 256,
        output_bytes: int = 65536,
    ) -> None:
        self.root, self.parent_cancelled = root, parent_cancelled
        self.max_bytes, self.max_files = max_bytes, max_files
        self.output_bytes = output_bytes
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _measure(self, root: Path, depth: int = 0) -> tuple[int, int]:
        if depth > MAX_CONTROLLER_DEPTH:
            raise ValueError("controller_file_depth")
        size = count = 0
        with directory_fd(root) as descriptor, os.scandir(descriptor) as entries:
            for entry in entries:
                count += 1
                if count > self.max_files:
                    raise ValueError("controller_file_limit")
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child_size, child_count = self._measure(root / entry.name, depth + 1)
                    except FileNotFoundError:
                        continue
                    size += child_size
                    count += child_count
                elif stat.S_ISREG(info.st_mode):
                    size += info.st_size
                    if (
                        root == self.root
                        and entry.name == "response.json"
                        and info.st_size > self.output_bytes
                    ):
                        raise ValueError("controller_response_file_limit")
                else:
                    raise ValueError("controller_special_file")
                if size > self.max_bytes or count > self.max_files:
                    raise ValueError("controller_file_limit")
        return size, count

    def _check(self) -> None:
        if self.parent_cancelled.is_set():
            self.cancelled.set()
        try:
            self._measure(self.root)
        except ValueError as error:
            self.error = str(error)
            self.cancelled.set()
        except (OSError, DryheaveError):
            self.error = "controller_file_observation_failed"
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
