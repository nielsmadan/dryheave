from pathlib import Path

from pydantic import JsonValue

from dryheave.logs.base import (
    Diagnostic,
    ImportLimits,
    LogEvent,
    Session,
    mapping,
    read_records,
    source_hash,
    string,
    text_content,
)
from dryheave.models import AgentKind
from dryheave.serialization import digest

HARNESS_EVENT = "<harness-event>{}</harness-event>"
MESSAGE_KEYS = frozenset(
    {
        "visibility",
        "interactionMode",
        "hookEventName",
        "hookStatus",
        "hookMatcher",
        "hookToolCallId",
        "compactionSummaryId",
    }
)
BLOCK_KEYS = frozenset({"name", "input", "id", "tool_use_id", "is_error"})
INJECTED_VISIBILITY = frozenset({"user_only", "llm_only"})
SESSION_KEYS = ("cwd", "title", "owner", "hostId")


def _version(value: JsonValue | None) -> str | None:
    if isinstance(value, str):
        return value or None
    return str(value) if type(value) is int else None


def _events(line: int, record: dict[str, JsonValue]) -> list[LogEvent]:
    message = mapping(record.get("message"))
    role = string(message.get("role"))
    if role not in {"user", "assistant"}:
        return []
    context: dict[str, JsonValue] = {
        key: value for key, value in message.items() if key in MESSAGE_KEYS
    }
    injected = role == "user" and string(message.get("visibility")) in INJECTED_VISIBILITY
    timestamp, native = string(record.get("timestamp")), string(record.get("id"))
    content = message.get("content")
    blocks: list[JsonValue] = (
        content if isinstance(content, list) else [{"type": "text", "text": content}]
    )
    if role == "user" and not blocks:
        return [
            LogEvent(
                event_id=f"e{line:06d}-0",
                source_line=line,
                kind="user",
                text=HARNESS_EVENT.format(string(message.get("hookEventName")) or ""),
                timestamp=timestamp,
                native_id=native,
                data=context,
            )
        ]
    events: list[LogEvent] = []
    for index, block_value in enumerate(blocks):
        block = mapping(block_value)
        kind = block.get("type")
        if kind not in {"text", "tool_use", "tool_result"}:
            continue
        event_kind = {"tool_use": "tool_call", "tool_result": "tool_result"}.get(str(kind), role)
        text = string(block.get("text")) or text_content(block.get("content"))
        events.append(
            LogEvent.model_validate(
                {
                    "event_id": f"e{line:06d}-{index}",
                    "source_line": line,
                    "kind": event_kind,
                    "text": HARNESS_EVENT.format(text)
                    if injected and event_kind == "user"
                    else text,
                    "timestamp": timestamp,
                    "native_id": native,
                    "data": context
                    | {key: value for key, value in block.items() if key in BLOCK_KEYS},
                }
            )
        )
    return events


def _record_event(line: int, record: dict[str, JsonValue]) -> LogEvent | None:
    kind = string(record.get("type"))
    if kind == "agent_turn_outcome":
        return LogEvent(
            event_id=f"e{line:06d}",
            source_line=line,
            kind="lifecycle",
            timestamp=string(record.get("timestamp")),
            native_id=string(record.get("id")),
            turn_id=string(record.get("turnId")),
            data={
                key: value
                for key, value in record.items()
                if key in {"type", "reason", "resultKind", "turnId"}
            },
        )
    if kind == "todo_state":
        return LogEvent(
            event_id=f"e{line:06d}",
            source_line=line,
            kind="metadata",
            timestamp=string(record.get("timestamp")),
            native_id=string(record.get("id")),
            data={
                key: value
                for key, value in record.items()
                if key in {"type", "todos", "messageIndex"}
            },
        )
    return None


def parse(path: Path, limits: ImportLimits | None = None) -> Session:
    limits = limits or ImportLimits()
    content, records, warnings = read_records(path, limits)
    events: list[LogEvent] = []
    metadata: dict[str, JsonValue] = {}
    source_id = version = None
    for line, record in records:
        kind = string(record.get("type"))
        if kind == "session_start":
            incoming = string(record.get("id"))
            if incoming:
                if source_id and source_id != incoming:
                    warnings.append(
                        Diagnostic(
                            code="ambiguous_session",
                            message="Conflicting session identities.",
                            line=line,
                        )
                    )
                source_id = incoming
            version = _version(record.get("version")) or version
            for key in SESSION_KEYS:
                if key in record:
                    if key == "cwd" and key in metadata and metadata[key] != record[key]:
                        warnings.append(
                            Diagnostic(
                                code="ambiguous_cwd",
                                message="Observed working directory changed.",
                                line=line,
                            )
                        )
                    metadata[key] = record[key]
        elif kind == "message":
            events.extend(_events(line, record))
        elif kind == "compaction_state":
            warnings.append(
                Diagnostic(
                    code="compacted",
                    message="Compaction recorded; replacement summaries are not replayed as dialogue.",
                    line=line,
                )
            )
        else:
            event = _record_event(line, record)
            if event is not None:
                events.append(event)
    if source_id is None:
        warnings.append(Diagnostic(code="missing_metadata", message="No session identity found."))
    warnings.append(
        Diagnostic(
            code="baseline_unknown",
            message="Droid logs record no Git metadata, so no historical baseline SHA is established.",
        )
    )
    return Session(
        agent=AgentKind.DROID,
        source_id=source_id,
        source_version=version,
        source_path_hash=source_hash(path),
        source_content_hash=digest(content),
        metadata=metadata,
        events=tuple(events),
        warnings=tuple(warnings),
        limits=limits,
    )
