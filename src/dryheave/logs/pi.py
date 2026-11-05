from pathlib import Path

from pydantic import JsonValue, ValidationError

from dryheave.logs.base import (
    HARNESS_EVENT,
    Diagnostic,
    ImportLimits,
    LogEvent,
    Session,
    UsageRecord,
    mapping,
    read_records,
    source_hash,
    string,
)
from dryheave.models import AgentKind, TokenUsage
from dryheave.serialization import digest

INTERRUPTED = frozenset({"aborted", "error", "length"})
SETTING_KEYS = {
    "model_change": ("type", "provider", "modelId"),
    "thinking_level_change": ("type", "thinkingLevel"),
}


def _blocks(content: JsonValue | None) -> list[dict[str, JsonValue]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [mapping(item) for item in content] if isinstance(content, list) else []


def _texts(content: JsonValue | None) -> str:
    return "\n".join(
        text
        for block in _blocks(content)
        if block.get("type") == "text" and (text := string(block.get("text")))
    )


def _event(
    line: int,
    record: dict[str, JsonValue],
    suffix: str,
    kind: str,
    text: str = "",
    **data: JsonValue,
) -> LogEvent:
    return LogEvent.model_validate(
        {
            "event_id": f"e{line:06d}{suffix}",
            "source_line": line,
            "kind": kind,
            "text": text,
            "timestamp": string(record.get("timestamp")),
            "native_id": string(record.get("id")),
            "data": {key: value for key, value in data.items() if value is not None},
        }
    )


def _message_events(line: int, record: dict[str, JsonValue]) -> list[LogEvent]:
    message = mapping(record.get("message"))
    role = string(message.get("role"))
    blocks = list(enumerate(_blocks(message.get("content"))))
    if role == "user":
        return [
            _event(line, record, f"-{index}", "user", text)
            for index, block in blocks
            if block.get("type") == "text" and (text := string(block.get("text")))
        ]
    if role == "custom":
        return [
            _event(
                line,
                record,
                "-0",
                "user",
                HARNESS_EVENT.format(_texts(message.get("content"))),
                customType=message.get("customType"),
                display=message.get("display"),
            )
        ]
    if role == "assistant":
        events = [
            _event(line, record, f"-{index}", "assistant", string(block.get("text")) or "")
            if block.get("type") == "text"
            else _event(
                line,
                record,
                f"-{index}",
                "tool_call",
                name=block.get("name"),
                id=block.get("id"),
                arguments=block.get("arguments"),
            )
            for index, block in blocks
            if block.get("type") in {"text", "toolCall"}
        ]
        stop = string(message.get("stopReason"))
        if stop in INTERRUPTED:
            events.append(
                _event(
                    line,
                    record,
                    "-stop",
                    "lifecycle",
                    stopReason=stop,
                    errorMessage=message.get("errorMessage"),
                )
            )
        return events
    if role == "toolResult":
        return [
            _event(
                line,
                record,
                "-0",
                "tool_result",
                _texts(message.get("content")),
                toolCallId=message.get("toolCallId"),
                toolName=message.get("toolName"),
                isError=message.get("isError"),
            )
        ]
    if role == "bashExecution":
        return [
            _event(
                line, record, "-0", "tool_call", initiator="user", command=message.get("command")
            ),
            _event(
                line,
                record,
                "-1",
                "tool_result",
                string(message.get("output")) or "",
                exitCode=message.get("exitCode"),
                cancelled=message.get("cancelled"),
                excludeFromContext=message.get("excludeFromContext"),
            ),
        ]
    return []


def _record_events(line: int, record: dict[str, JsonValue]) -> list[LogEvent]:
    kind = string(record.get("type")) or ""
    if kind == "message":
        return _message_events(line, record)
    if kind == "custom_message":
        return [
            _event(
                line,
                record,
                "",
                "user",
                HARNESS_EVENT.format(_texts(record.get("content"))),
                customType=record.get("customType"),
                display=record.get("display"),
            )
        ]
    if kind in SETTING_KEYS:
        settings = {key: record[key] for key in SETTING_KEYS[kind] if key in record}
        return [_event(line, record, "", "metadata").model_copy(update={"data": settings})]
    return []


def _usage(
    line: int, record: dict[str, JsonValue], warnings: list[Diagnostic]
) -> UsageRecord | None:
    message = mapping(record.get("message"))
    owner = message if record.get("type") == "message" else record
    raw = mapping(owner.get("usage"))
    if not raw:
        return None

    def number(key: str) -> int | None:
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    entry = string(record.get("id"))
    identity = string(message.get("responseId")) or entry
    try:
        usage = TokenUsage(
            uncached_input=number("input"),
            cache_read=number("cacheRead"),
            cache_write=number("cacheWrite"),
            cache_write_1h=number("cacheWrite1h"),
            output=number("output"),
            reasoning=number("reasoning"),
            provenance="pi.usage; reasoning is included in output",
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
    if identity is None:
        warnings.append(
            Diagnostic(
                code="usage_identity_missing",
                message="Usage without an entry or response id cannot be reliably deduplicated.",
                line=line,
            )
        )
    return UsageRecord(
        identity=identity or f"unknown-{line}",
        source_line=line,
        usage=usage,
        raw=raw,
        accounting="response" if identity else "superseded",
    )


def _header(
    line: int,
    record: dict[str, JsonValue],
    metadata: dict[str, JsonValue],
    warnings: list[Diagnostic],
) -> None:
    incoming = string(record.get("id"))
    known = string(metadata.get("id"))
    if incoming and known and known != incoming:
        warnings.append(
            Diagnostic(
                code="ambiguous_session", message="Conflicting session identities.", line=line
            )
        )
    cwd = record.get("cwd")
    if "cwd" in metadata and cwd is not None and metadata["cwd"] != cwd:
        warnings.append(
            Diagnostic(
                code="ambiguous_cwd", message="Observed working directory changed.", line=line
            )
        )
    metadata.update(
        {
            key: value
            for key, value in record.items()
            if key in {"id", "cwd", "version", "parentSession"} and value is not None
        }
    )


def _tree_warning(
    line: int, record: dict[str, JsonValue], previous: str | None
) -> Diagnostic | None:
    parent = record.get("parentId", previous)
    if previous is None or parent == previous:
        return None
    return Diagnostic(
        code="branched",
        message="Entry continues an earlier branch; events follow file order, not one path.",
        line=line,
    )


def parse(path: Path, limits: ImportLimits | None = None) -> Session:
    limits = limits or ImportLimits()
    content, records, warnings = read_records(path, limits)
    events: list[LogEvent] = []
    usage: dict[str, UsageRecord] = {}
    header: dict[str, JsonValue] = {}
    metadata: dict[str, JsonValue] = {}
    previous: str | None = None
    for line, record in records:
        kind = string(record.get("type"))
        if kind == "session":
            _header(line, record, header, warnings)
            continue
        branch = _tree_warning(line, record, previous)
        if branch is not None:
            warnings.append(branch)
        previous = string(record.get("id")) or previous
        if kind == "session_info" and string(record.get("name")):
            metadata["title"] = record["name"]
        elif kind == "compaction":
            warnings.append(
                Diagnostic(
                    code="compacted",
                    message="Compaction recorded; replacement summaries are not replayed as dialogue.",
                    line=line,
                )
            )
        events.extend(_record_events(line, record))
        if kind in {"message", "usage"}:
            record_usage = _usage(line, record, warnings)
            if record_usage is not None:
                usage[record_usage.identity] = record_usage
    source_id = string(header.get("id"))
    if source_id is None:
        warnings.append(Diagnostic(code="missing_metadata", message="No session header found."))
    warnings.append(
        Diagnostic(
            code="baseline_unknown",
            message="Pi logs record no Git metadata, so no historical baseline SHA is established.",
        )
    )
    version = header.get("version")
    return Session(
        agent=AgentKind.PI,
        source_id=source_id,
        source_version=str(version) if type(version) is int else string(version),
        parent_session_id=string(header.get("parentSession")),
        source_path_hash=source_hash(path),
        source_content_hash=digest(content),
        metadata=({"cwd": header["cwd"]} if "cwd" in header else {}) | metadata,
        events=tuple(events),
        usage=tuple(usage.values()),
        warnings=tuple(warnings),
        limits=limits,
    )
