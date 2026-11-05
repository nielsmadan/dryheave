import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from itertools import groupby, islice
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import JsonValue, ValidationError

from dryheave.errors import InputError, LimitError
from dryheave.logs.base import (
    HARNESS_EVENT,
    Diagnostic,
    ImportLimits,
    LogEvent,
    Session,
    UsageRecord,
    mapping,
    source_hash,
    string,
)
from dryheave.models import AgentKind, TokenUsage
from dryheave.serialization import canonical_json, digest, parse_json

DATABASE_NAME = re.compile(r"^opencode(?:-[A-Za-z0-9._-]+)?\.db$")
SEPARATOR = "#"
SESSION_COLUMNS = ("id", "parent_id", "directory", "title", "version")
FINISHED_TOOLS = frozenset({"completed", "error"})
LegacyRow = tuple[str, str, str | None, str | None]
CurrentRow = tuple[str, str, str]
Events = tuple[list[LogEvent], list[UsageRecord]]


class Storage(NamedTuple):
    tables: frozenset[str]
    sessions: str
    session: str
    bounds: str
    rows: str
    events: Callable[[list[Any], list[Diagnostic]], Events]


def source_path(database: Path, session_id: str) -> Path:
    return Path(f"{database}{SEPARATOR}{session_id}")


def split_source(path: Path) -> tuple[Path, str | None]:
    database, separator, session_id = str(path).rpartition(SEPARATOR)
    if not separator:
        return path, None
    if not database or not session_id:
        raise InputError(
            "An OpenCode source is DATABASE#SESSION_ID; collect scan --agent opencode lists them."
        )
    return Path(database), session_id


@contextmanager
def _connect(database: Path) -> Iterator[tuple[sqlite3.Connection, Storage]]:
    if database.is_symlink() or not database.is_file():
        raise InputError(f"OpenCode database must be a regular file: {database}")
    journal = Path(f"{database}-wal")
    if journal.is_file() and journal.stat().st_size:
        raise InputError(
            f"{database} has changes OpenCode has not yet written into it; "
            "quit OpenCode so it saves them, then retry."
        )
    try:
        with closing(
            sqlite3.connect(f"{database.absolute().as_uri()}?mode=ro&immutable=1", uri=True)
        ) as connection:
            tables = {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            storage = next((item for item in STORAGES if tables >= item.tables), None)
            if storage is None:
                raise InputError(
                    f"{database} has no OpenCode session tables; "
                    "this OpenCode storage version is not supported."
                )
            yield connection, storage
    except sqlite3.Error as error:
        raise InputError(f"OpenCode database could not be read: {error}") from error


def _database_sources(database: Path, limit: int) -> list[Path]:
    try:
        with _connect(database) as (connection, storage):
            return [
                source_path(database, session_id)
                for (session_id,) in connection.execute(storage.sessions, (limit,))
            ]
    except InputError:
        return [database]


def discover(root: Path, limits: ImportLimits) -> tuple[Path, ...]:
    if root.is_symlink() or not root.is_dir():
        raise InputError("Log root must be a real directory.")
    with os.scandir(root) as entries:
        listed = list(islice(entries, limits.max_files + 1))
    if len(listed) > limits.max_files:
        raise LimitError("Log scan exceeds the entry limit; choose a narrower root.")
    databases = sorted(
        root / entry.name
        for entry in listed
        if DATABASE_NAME.match(entry.name) and entry.is_file(follow_symlinks=False)
    )
    found: list[Path] = []
    for database in databases:
        found.extend(_database_sources(database, limits.max_files + 1 - len(found)))
        if len(found) > limits.max_files:
            raise LimitError("OpenCode scan exceeds the session limit; choose a narrower root.")
    return tuple(found)


def _bounded_rows(
    connection: sqlite3.Connection, storage: Storage, session_id: str, limits: ImportLimits
) -> list[Any]:
    count, size = connection.execute(storage.bounds, (session_id,)).fetchone()
    if count > limits.max_events:
        raise LimitError("Session exceeds the event limit; select a smaller source.")
    if size > limits.max_bytes:
        raise LimitError(f"Session exceeds the {limits.max_bytes}-byte limit.")
    return list(connection.execute(storage.rows, (session_id,)))


def _decode(value: str | None, line: int, warnings: list[Diagnostic]) -> dict[str, JsonValue]:
    if value is None:
        return {}
    try:
        return parse_json(value.encode())
    except InputError:
        warnings.append(
            Diagnostic(code="malformed_line", message="Invalid JSON record was skipped.", line=line)
        )
        return {}


def _event(
    line: int,
    suffix: str,
    kind: str,
    native: str | None,
    timestamp: JsonValue | None,
    text: str = "",
    **data: JsonValue,
) -> LogEvent:
    return LogEvent.model_validate(
        {
            "event_id": f"e{line:06d}{suffix}",
            "source_line": line,
            "kind": kind,
            "text": text,
            "timestamp": _iso(timestamp),
            "native_id": native,
            "data": {key: value for key, value in data.items() if value is not None},
        }
    )


def _iso(value: JsonValue | None) -> str | None:
    if type(value) is not int:
        return None
    return (
        datetime.fromtimestamp(value / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _compacted(line: int) -> Diagnostic:
    return Diagnostic(
        code="compacted",
        message="Compaction recorded; replacement summaries are not replayed as dialogue.",
        line=line,
    )


def _part_events(
    line: int, role: str | None, part_id: str, part: dict[str, JsonValue]
) -> list[LogEvent]:
    kind = string(part.get("type"))
    started = mapping(part.get("time")).get("start")
    text = string(part.get("text")) or ""
    if kind == "text" and role in {"user", "assistant"} and text:
        injected = role == "user" and (part.get("synthetic") is True or part.get("ignored") is True)
        return [
            _event(
                line,
                "",
                role,
                part_id,
                started,
                HARNESS_EVENT.format(text) if injected else text,
                synthetic=part.get("synthetic"),
                ignored=part.get("ignored"),
            )
        ]
    if kind == "tool":
        state = mapping(part.get("state"))
        timing = mapping(state.get("time"))
        status = string(state.get("status"))
        call = _event(
            line,
            "-0",
            "tool_call",
            part_id,
            timing.get("start"),
            tool=part.get("tool"),
            callID=part.get("callID"),
            input=state.get("input"),
            status=status,
        )
        if status not in FINISHED_TOOLS:
            return [call]
        return [
            call,
            _event(
                line,
                "-1",
                "tool_result",
                part_id,
                timing.get("end"),
                string(state.get("output")) or string(state.get("error")) or "",
                tool=part.get("tool"),
                callID=part.get("callID"),
                status=status,
                title=state.get("title"),
            ),
        ]
    if kind in {"file", "agent", "subtask"}:
        return [
            _event(
                line,
                "",
                "metadata",
                part_id,
                started,
                type=kind,
                mime=part.get("mime"),
                filename=part.get("filename"),
                name=part.get("name"),
                agent=part.get("agent"),
                description=part.get("description"),
            )
        ]
    if kind == "compaction":
        return [_event(line, "", "lifecycle", part_id, started, type=kind, auto=part.get("auto"))]
    return []


def _usage(
    line: int, message_id: str, message: dict[str, JsonValue], warnings: list[Diagnostic]
) -> UsageRecord | None:
    raw = mapping(message.get("tokens"))
    if not raw:
        return None
    cache = mapping(raw.get("cache"))

    def number(value: JsonValue | None) -> int | None:
        return value if type(value) is int and value >= 0 else None

    output, reasoning = number(raw.get("output")), number(raw.get("reasoning"))
    try:
        usage = TokenUsage(
            uncached_input=number(raw.get("input")),
            cache_read=number(cache.get("read")),
            cache_write=number(cache.get("write")),
            output=output + reasoning if output is not None and reasoning is not None else output,
            reasoning=reasoning,
            provenance="opencode.message.tokens; output adds the separately recorded reasoning",
        )
    except ValidationError:
        warnings.append(
            Diagnostic(
                code="invalid_usage",
                message="Token categories are inconsistent; record excluded from accounting.",
                line=line,
            )
        )
        return None
    return UsageRecord(identity=message_id, source_line=line, usage=usage, raw=raw)


def _error_event(
    line: int, message_id: str, message: dict[str, JsonValue], name: str
) -> list[LogEvent]:
    error = mapping(message.get("error"))
    if not error:
        return []
    time = mapping(message.get("time"))
    return [
        _event(
            line,
            "-error",
            "lifecycle",
            message_id,
            time.get("completed", time.get("created")),
            type="error",
            name=error.get(name),
        )
    ]


def _legacy_events(rows: list[LegacyRow], warnings: list[Diagnostic]) -> Events:
    events: list[LogEvent] = []
    usage: list[UsageRecord] = []
    for message_id, group in groupby(enumerate(rows, 1), key=lambda item: item[1][0]):
        members = list(group)
        first, last = members[0][0], members[-1][0]
        message = _decode(members[0][1][1], first, warnings)
        role = string(message.get("role"))
        record = _usage(first, message_id, message, warnings)
        if record is not None:
            usage.append(record)
        for line, (_, _, part_id, part_data) in members:
            if part_id is None:
                continue
            part = _decode(part_data, line, warnings)
            if part.get("type") == "compaction":
                warnings.append(_compacted(line))
            events.extend(_part_events(line, role, part_id, part))
        events.extend(_error_event(last, message_id, message, "name"))
    return events, usage


def _content_text(content: JsonValue | None) -> str:
    items = content if isinstance(content, list) else []
    return "\n".join(
        text
        for item in map(mapping, items)
        if item.get("type") == "text" and (text := string(item.get("text")))
    )


def _assistant_events(line: int, message: dict[str, JsonValue]) -> list[LogEvent]:
    content = message.get("content")
    events: list[LogEvent] = []
    for index, item in enumerate(map(mapping, content if isinstance(content, list) else [])):
        kind = item.get("type")
        if kind == "text" and (text := string(item.get("text"))):
            events.append(
                _event(
                    line,
                    f"-{index}",
                    "assistant",
                    None,
                    mapping(message.get("time")).get("created"),
                    text,
                )
            )
        elif kind == "tool":
            state = mapping(item.get("state"))
            timing = mapping(item.get("time"))
            status = string(state.get("status"))
            identity = string(item.get("id"))
            events.append(
                _event(
                    line,
                    f"-{index}-0",
                    "tool_call",
                    identity,
                    timing.get("created"),
                    tool=item.get("name"),
                    callID=identity,
                    input=state.get("input"),
                    status=status,
                )
            )
            if status in FINISHED_TOOLS:
                error = mapping(state.get("error"))
                events.append(
                    _event(
                        line,
                        f"-{index}-1",
                        "tool_result",
                        identity,
                        timing.get("completed"),
                        _content_text(state.get("content")) or string(error.get("message")) or "",
                        tool=item.get("name"),
                        callID=identity,
                        status=status,
                        error=error.get("type"),
                    )
                )
    return events


def _current_message_events(
    line: int, message_id: str, kind: str, message: dict[str, JsonValue]
) -> list[LogEvent]:
    created = mapping(message.get("time")).get("created")
    text = string(message.get("text")) or ""
    description = message.get("description")
    if kind == "user" and text:
        return [_event(line, "", "user", message_id, created, text)]
    if kind == "synthetic" and text:
        return [
            _event(
                line,
                "",
                "user",
                message_id,
                created,
                HARNESS_EVENT.format(text),
                description=description,
            )
        ]
    if kind == "system" and text:
        return [
            _event(
                line, "", "metadata", message_id, created, text, type=kind, description=description
            )
        ]
    if kind == "agent-switched":
        return [
            _event(
                line,
                "",
                "metadata",
                message_id,
                created,
                type=kind,
                agent=message.get("agent"),
                previous=message.get("previous"),
            )
        ]
    if kind == "idle":
        return [
            _event(
                line,
                "",
                "lifecycle",
                message_id,
                created,
                type=kind,
                outcome=message.get("outcome"),
                error=mapping(message.get("error")).get("type"),
            )
        ]
    if kind == "assistant":
        return _assistant_events(line, message) + _error_event(line, message_id, message, "type")
    return []


def _current_events(rows: list[CurrentRow], warnings: list[Diagnostic]) -> Events:
    events: list[LogEvent] = []
    usage: list[UsageRecord] = []
    for line, (message_id, kind, data) in enumerate(rows, 1):
        message = _decode(data, line, warnings)
        if kind == "assistant" and (record := _usage(line, message_id, message, warnings)):
            usage.append(record)
        events.extend(_current_message_events(line, message_id, kind, message))
    return events, usage


LEGACY = Storage(
    tables=frozenset({"session", "message", "part"}),
    sessions="SELECT id FROM session ORDER BY time_created, id LIMIT ?",
    session="SELECT id, parent_id, directory, title, version FROM session WHERE id = ?",
    bounds=(
        "SELECT"
        " (SELECT count(*) FROM message AS m LEFT JOIN part AS p ON p.message_id = m.id"
        "  WHERE m.session_id = ?1),"
        " (SELECT coalesce(sum(length(CAST(data AS BLOB))), 0) FROM message"
        "  WHERE session_id = ?1)"
        " + (SELECT coalesce(sum(length(CAST(p.data AS BLOB))), 0)"
        "  FROM part AS p JOIN message AS m ON p.message_id = m.id WHERE m.session_id = ?1)"
    ),
    rows=(
        "SELECT m.id, m.data, p.id, p.data"
        " FROM message AS m LEFT JOIN part AS p ON p.message_id = m.id"
        " WHERE m.session_id = ? ORDER BY m.time_created, m.id, p.id"
    ),
    events=_legacy_events,
)
CURRENT = Storage(
    tables=frozenset({"session_v2", "session_message"}),
    sessions="SELECT id FROM session_v2 ORDER BY time_created, id LIMIT ?",
    session="SELECT id, parent_id, directory, title, version FROM session_v2 WHERE id = ?",
    bounds=(
        "SELECT coalesce(sum(1 + CASE WHEN json_valid(data)"
        " THEN 2 * coalesce(json_array_length(data, '$.content'), 0) ELSE 0 END), 0),"
        " coalesce(sum(length(CAST(data AS BLOB))), 0)"
        " FROM session_message WHERE session_id = ?"
    ),
    rows="SELECT id, type, data FROM session_message WHERE session_id = ? ORDER BY seq",
    events=_current_events,
)
STORAGES = (CURRENT, LEGACY)


def parse(path: Path, limits: ImportLimits | None = None) -> Session:
    limits = limits or ImportLimits()
    database, session_id = split_source(path)
    with _connect(database) as (connection, storage):
        if session_id is None:
            raise InputError(
                f"{database} is an OpenCode database; pass DATABASE#SESSION_ID, "
                "which collect scan --agent opencode --root DIRECTORY lists."
            )
        found = connection.execute(storage.session, (session_id,)).fetchone()
        if found is None:
            raise InputError(f"OpenCode session {session_id} is not in {database}.")
        rows = _bounded_rows(connection, storage, session_id, limits)
    session = dict(zip(SESSION_COLUMNS, found, strict=True))
    warnings: list[Diagnostic] = []
    events, usage = storage.events(rows, warnings)
    warnings.append(
        Diagnostic(
            code="baseline_unknown",
            message="OpenCode records only its own snapshot hashes, not a repository commit, so no historical baseline SHA is established.",
        )
    )
    metadata: dict[str, JsonValue] = {
        key: value
        for key, value in (("cwd", session["directory"]), ("title", session["title"]))
        if value
    }
    return Session(
        agent=AgentKind.OPENCODE,
        source_id=session_id,
        source_version=session["version"] or None,
        parent_session_id=session["parent_id"],
        source_path_hash=source_hash(database),
        source_content_hash=digest(
            canonical_json({"session": session, "rows": [list(row) for row in rows]})
        ),
        metadata=metadata,
        events=tuple(events),
        usage=tuple(usage),
        warnings=tuple(warnings),
        limits=limits,
    )
