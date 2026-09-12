import os
from dataclasses import dataclass, field
from pathlib import Path

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.claude import ClaudeAdapter
from dryheave.drivers.codex import CodexAdapter
from dryheave.drivers.models import DriverLimits, EventCursor, FileCursor, NativeEvent
from dryheave.errors import InputError, LimitError
from dryheave.filesystem import directory_fd, regular_fd
from dryheave.logs.base import ImportLimits, discover
from dryheave.models import AgentKind
from dryheave.serialization import digest, parse_json


def native_log_root(config_root: Path, agent: AgentKind) -> Path:
    return config_root / ("sessions" if agent == AgentKind.CODEX else "projects")


@dataclass
class _LogFile:
    cursor: FileCursor
    adapter: CodexAdapter | ClaudeAdapter
    tail: bytes = b""
    old: bool = False


@dataclass
class NativeLogSource:
    root: Path
    agent: AgentKind
    artifacts: ArtifactWriter
    limits: DriverLimits = field(default_factory=DriverLimits)
    include_existing: bool = False
    files: dict[str, _LogFile] = field(default_factory=dict, init=False)
    sequence: int = field(default=0, init=False)
    bytes_read: int = field(default=0, init=False)
    records_read: int = field(default=0, init=False)
    polls: int = field(default=0, init=False)
    failed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for path in self._paths():
            with regular_fd(path, os.O_RDONLY) as descriptor:
                info = os.fstat(descriptor)
                self.files[str(path.relative_to(self.root))] = _LogFile(
                    FileCursor(
                        device=info.st_dev,
                        inode=info.st_ino,
                        offset=0 if self.include_existing else info.st_size,
                        line=0,
                    ),
                    self._adapter(),
                    old=not self.include_existing,
                )

    def _adapter(self) -> CodexAdapter | ClaudeAdapter:
        return CodexAdapter() if self.agent == AgentKind.CODEX else ClaudeAdapter()

    def _paths(self) -> tuple[Path, ...]:
        if not self.root.exists():
            return ()
        with directory_fd(self.root):
            return discover(
                self.root,
                ImportLimits(max_files=self.limits.max_files, max_depth=self.limits.max_depth),
            )

    def cursor(self) -> EventCursor:
        return EventCursor(
            sequence=self.sequence, files={name: item.cursor for name, item in self.files.items()}
        )

    def poll(self) -> tuple[NativeEvent, ...]:
        if self.failed:
            raise InputError("Native log source cannot resume after a failed poll.")
        self.failed = True
        self.polls += 1
        if self.polls > self.limits.max_polls:
            raise LimitError("Native log polling limit exceeded.")
        events: list[NativeEvent] = []
        for path in self._paths():
            name = str(path.relative_to(self.root))
            with regular_fd(path, os.O_RDONLY) as descriptor:
                info = os.fstat(descriptor)
                current = self.files.setdefault(
                    name,
                    _LogFile(
                        FileCursor(device=info.st_dev, inode=info.st_ino, offset=0, line=0),
                        self._adapter(),
                    ),
                )
                if (info.st_dev, info.st_ino) != (
                    current.cursor.device,
                    current.cursor.inode,
                ) or info.st_size < current.cursor.offset:
                    raise InputError("Native log was replaced or truncated during the session.")
                os.lseek(descriptor, current.cursor.offset, os.SEEK_SET)
                remaining = info.st_size - current.cursor.offset
                while remaining > 0:
                    content = os.read(
                        descriptor,
                        min(65536, remaining, self.limits.max_log_bytes - self.bytes_read + 1),
                    )
                    if not content:
                        break
                    events.extend(self._consume(name, current, content))
                    remaining -= len(content)
        self.failed = False
        return tuple(events)

    def _consume(self, name: str, current: _LogFile, content: bytes) -> list[NativeEvent]:
        self.bytes_read += len(content)
        if self.bytes_read > self.limits.max_log_bytes:
            raise LimitError("Native logs exceeded their byte limit.")
        self.artifacts.append("raw-" + digest(name.encode()) + ".jsonl", content)
        lines = (current.tail + content).split(b"\n")
        current.tail = lines.pop()
        offset = current.cursor.offset + len(content)
        line_number = current.cursor.line
        events = []
        for line in lines:
            line_number += 1
            self.records_read += 1
            if self.records_read > self.limits.max_events:
                raise LimitError("Native logs exceeded their record limit.")
            if current.old:
                continue
            record = parse_json(line)
            for event in current.adapter.feed(record):
                self.sequence += 1
                if self.sequence > self.limits.max_events:
                    raise LimitError("Native logs exceeded their event limit.")
                events.append(
                    event.model_copy(
                        update={"sequence": self.sequence, "source": name, "line": line_number}
                    )
                )
        current.cursor = current.cursor.model_copy(update={"offset": offset, "line": line_number})
        return events

    def drained(self) -> bool:
        return not self.failed and all(not item.tail for item in self.files.values())
