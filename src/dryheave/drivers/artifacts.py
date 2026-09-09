import os
from pathlib import Path

from pydantic import JsonValue

from dryheave.errors import LimitError
from dryheave.filesystem import atomic_write, directory_fd, regular_fd, write_all
from dryheave.models import StrictModel
from dryheave.serialization import canonical_json


class ArtifactWriter:
    def __init__(self, root: Path, max_bytes: int) -> None:
        with directory_fd(root.parent, create=True) as descriptor:
            os.mkdir(root.name, mode=0o700, dir_fd=descriptor)
            os.fsync(descriptor)
        self.root = root
        self.max_bytes = max_bytes
        self.written = 0
        self.sequence = 0

    def record(self, name: str, value: StrictModel | dict[str, JsonValue]) -> Path:
        self.sequence += 1
        path = self.root / f"{self.sequence:06d}-{name}.json"
        self._reserve(len(content := canonical_json(value)))
        atomic_write(path, content, replace=False)
        return path

    def append(self, name: str, content: bytes) -> None:
        self._reserve(len(content))
        path = self.root / name
        with regular_fd(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT) as descriptor:
            write_all(descriptor, content)
            os.fsync(descriptor)

    def _reserve(self, size: int) -> None:
        if self.written + size > self.max_bytes:
            raise LimitError("Driver evidence exceeds its artifact byte limit.")
        self.written += size
