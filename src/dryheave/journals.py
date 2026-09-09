import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from dryheave.constants import MAX_EVENT_BYTES, MAX_JOURNAL_BYTES, MAX_MANIFEST_BYTES
from dryheave.errors import ConflictError, InputError, IntegrityError, LimitError, NotFoundError
from dryheave.filesystem import (
    atomic_write,
    ensure_directory,
    file_lock,
    publish_directory,
    read_bytes,
    regular_fd,
    write_all,
)
from dryheave.models import JournalEvent, RunCheckpoint, RunMetadata
from dryheave.serialization import canonical_json, digest, parse_model, validation_message


def _run_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise InputError("Expected a 32-character lowercase run ID.")
    return value


def _read_events(path: Path) -> tuple[list[JournalEvent], bytes]:
    content = read_bytes(path, limit=MAX_JOURNAL_BYTES)
    lines = content.split(b"\n")
    tail = lines.pop()
    events: list[JournalEvent] = []
    previous_hash: str | None = None
    for sequence, line in enumerate(lines, start=1):
        if len(line) + 1 > MAX_EVENT_BYTES:
            raise IntegrityError(f"Journal event {sequence} exceeds its byte limit.")
        try:
            event = parse_model(line, JournalEvent)
        except InputError as error:
            raise IntegrityError(
                f"Invalid journal event at sequence {sequence}. {error}"
            ) from error
        if event.sequence != sequence or event.previous_hash != previous_hash:
            raise IntegrityError(f"Broken journal sequence or hash chain at event {sequence}.")
        if canonical_json(event) != line:
            raise IntegrityError(f"Journal event {sequence} is not canonical.")
        events.append(event)
        previous_hash = digest(line)
    return events, tail


class RunJournal:
    def __init__(self, path: Path, metadata: RunMetadata, *, read_only: bool = False) -> None:
        self.path = path
        self.metadata = metadata
        self._events, self.recovered_tail = _read_events(path / "events.jsonl")
        self._active = not read_only
        if read_only:
            return
        self.read_checkpoint()
        if self.recovered_tail:
            atomic_write(path / f"torn-{uuid4().hex}.bin", self.recovered_tail, replace=False)
            with regular_fd(path / "events.jsonl", os.O_WRONLY) as descriptor:
                size = os.fstat(descriptor).st_size - len(self.recovered_tail)
                os.ftruncate(descriptor, size)
                os.fsync(descriptor)

    def _require_active(self) -> None:
        if not self._active:
            raise ConflictError("The run journal is closed; acquire the run lock again.")

    @property
    def events(self) -> tuple[JournalEvent, ...]:
        return self.events_since(0)

    @property
    def sequence(self) -> int:
        return len(self._events)

    def events_since(self, sequence: int) -> tuple[JournalEvent, ...]:
        if not 0 <= sequence <= self.sequence:
            raise InputError("Journal cursor is outside the durable event prefix.")
        return tuple(event.model_copy(deep=True) for event in self._events[sequence:])

    def close(self) -> None:
        self._active = False

    def append(
        self,
        event: str,
        data: dict[str, JsonValue] | None = None,
        *,
        trial_id: str | None = None,
        attempt_id: str | None = None,
    ) -> JournalEvent:
        self._require_active()
        previous_hash = digest(canonical_json(self._events[-1])) if self._events else None
        try:
            record = JournalEvent(
                sequence=len(self._events) + 1,
                previous_hash=previous_hash,
                timestamp=datetime.now(UTC),
                event=event,
                trial_id=trial_id,
                attempt_id=attempt_id,
                data=data or {},
            )
        except ValidationError as error:
            raise InputError(validation_message(error)) from error
        encoded = canonical_json(record) + b"\n"
        if len(encoded) > MAX_EVENT_BYTES:
            raise LimitError(f"Journal event exceeds the {MAX_EVENT_BYTES}-byte limit.")
        with regular_fd(self.path / "events.jsonl", os.O_WRONLY | os.O_APPEND) as descriptor:
            if os.fstat(descriptor).st_size + len(encoded) > MAX_JOURNAL_BYTES:
                raise LimitError(f"Journal exceeds the {MAX_JOURNAL_BYTES}-byte limit.")
            try:
                write_all(descriptor, encoded)
                os.fsync(descriptor)
            except OSError:
                self.close()
                raise
        self._events.append(record.model_copy(deep=True))
        return record

    def _snapshot(self, state: dict[str, JsonValue]) -> RunCheckpoint:
        self._require_active()
        return RunCheckpoint(
            run_id=self.metadata.run_id,
            experiment_id=self.metadata.experiment_id,
            sequence=len(self._events),
            event_hash=digest(canonical_json(self._events[-1])) if self._events else None,
            state=state,
        )

    def checkpoint(self, state: dict[str, JsonValue]) -> RunCheckpoint:
        return self._write_snapshot("checkpoint.json", state)

    def write_result(self, state: dict[str, JsonValue]) -> RunCheckpoint:
        return self._write_snapshot("result.json", state)

    def _write_snapshot(self, name: str, state: dict[str, JsonValue]) -> RunCheckpoint:
        snapshot = self._snapshot(state)
        content = canonical_json(snapshot)
        if len(content) > MAX_MANIFEST_BYTES:
            raise LimitError(f"Run snapshot exceeds the {MAX_MANIFEST_BYTES}-byte limit.")
        atomic_write(self.path / name, content)
        return snapshot

    def _read_snapshot(self, name: str) -> RunCheckpoint | None:
        self._require_active()
        try:
            content = read_bytes(self.path / name, limit=MAX_MANIFEST_BYTES)
        except FileNotFoundError:
            return None
        try:
            checkpoint = parse_model(content, RunCheckpoint)
        except InputError as error:
            raise IntegrityError(f"Invalid {name}. {error}") from error
        if (
            checkpoint.run_id != self.metadata.run_id
            or checkpoint.experiment_id != self.metadata.experiment_id
            or checkpoint.sequence > len(self._events)
        ):
            raise IntegrityError(f"{name} does not match this run's durable journal.")
        expected = (
            digest(canonical_json(self._events[checkpoint.sequence - 1]))
            if checkpoint.sequence
            else None
        )
        if checkpoint.event_hash != expected:
            raise IntegrityError(f"{name} event hash does not match the journal.")
        return checkpoint

    def read_checkpoint(self) -> RunCheckpoint | None:
        return self._read_snapshot("checkpoint.json")

    def read_result(self) -> RunCheckpoint | None:
        return self._read_snapshot("result.json")


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()

    def create(self, experiment_id: str) -> str:
        identifier = uuid4().hex
        try:
            metadata = RunMetadata(
                run_id=identifier, experiment_id=experiment_id, created_at=datetime.now(UTC)
            )
        except ValidationError as error:
            raise InputError(validation_message(error)) from error
        parent = self.root / "runs"
        staged = parent / f".pending-{uuid4().hex}"
        ensure_directory(staged)
        try:
            atomic_write(staged / "metadata.json", canonical_json(metadata), replace=False)
            atomic_write(staged / "events.jsonl", b"", replace=False)
            publish_directory(staged, parent / identifier)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return identifier

    def inspect(self, identifier: str) -> RunJournal:
        identifier = _run_id(identifier)
        path = self.root / "runs" / identifier
        try:
            content = read_bytes(path / "metadata.json", limit=MAX_MANIFEST_BYTES)
        except FileNotFoundError as error:
            raise NotFoundError(f"Run does not exist: {identifier}") from error
        try:
            metadata = parse_model(content, RunMetadata)
        except InputError as error:
            raise IntegrityError(f"Run metadata is invalid. {error}") from error
        if metadata.run_id != identifier:
            raise IntegrityError("Run metadata has a mismatched run ID.")
        return RunJournal(path, metadata, read_only=True)

    @contextmanager
    def open(self, identifier: str, *, experiment_id: str | None = None) -> Iterator[RunJournal]:
        identifier = _run_id(identifier)
        with file_lock(self.root / "locks" / f"run-{identifier}.lock"):
            path = self.root / "runs" / identifier
            try:
                content = read_bytes(path / "metadata.json", limit=MAX_MANIFEST_BYTES)
            except FileNotFoundError as error:
                raise NotFoundError(f"Run does not exist: {identifier}") from error
            try:
                metadata = parse_model(content, RunMetadata)
            except InputError as error:
                raise IntegrityError(f"Run metadata is invalid. {error}") from error
            if metadata.run_id != identifier:
                raise IntegrityError("Run metadata has a mismatched run ID.")
            if experiment_id is not None and metadata.experiment_id != experiment_id:
                raise ConflictError(
                    "Resume experiment differs from the run's pinned experiment ID."
                )
            journal = RunJournal(path, metadata)
            try:
                yield journal
            finally:
                journal.close()
