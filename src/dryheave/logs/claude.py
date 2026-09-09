from pathlib import Path

from pydantic import JsonValue, ValidationError

from dryheave.logs.base import (
    Diagnostic,
    ImportLimits,
    LogEvent,
    Session,
    UsageRecord,
    mapping,
    read_records,
    source_hash,
    string,
    text_content,
)
from dryheave.models import AgentKind, TokenUsage
from dryheave.serialization import canonical_json, digest


def _usage(raw: dict[str, JsonValue]) -> TokenUsage:
    def number(key: str) -> int | None:
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    reasoning = mapping(raw.get("output_tokens_details")).get("thinking_tokens")
    return TokenUsage(
        uncached_input=number("input_tokens"),
        cache_read=number("cache_read_input_tokens"),
        cache_write=number("cache_creation_input_tokens"),
        output=number("output_tokens"),
        reasoning=reasoning if type(reasoning) is int else None,
        provenance="claude.message.usage; latest snapshot per message.id",
    )


def _events(line: int, record: dict[str, JsonValue]) -> list[LogEvent]:
    message = mapping(record.get("message"))
    role = string(message.get("role"))
    if role not in {"user", "assistant"}:
        return []
    content = message.get("content")
    blocks: list[JsonValue] = (
        content if isinstance(content, list) else [{"type": "text", "text": content}]
    )
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
                    "text": text,
                    "timestamp": string(record.get("timestamp")),
                    "native_id": string(record.get("uuid")),
                    "data": {
                        key: value
                        for key, value in block.items()
                        if key in {"name", "input", "id", "tool_use_id", "is_error"}
                    },
                }
            )
        )
    return events


def parse(path: Path, limits: ImportLimits | None = None) -> Session:
    limits = limits or ImportLimits()
    content, records, warnings = read_records(path, limits)
    events: list[LogEvent] = []
    usage: dict[str, UsageRecord] = {}
    metadata: dict[str, JsonValue] = {}
    source_id = version = None
    seen: set[tuple[str, str]] = set()
    for line, record in records:
        incoming = string(record.get("sessionId"))
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
        version = string(record.get("version")) or version
        for key in ("cwd", "gitBranch", "isSidechain"):
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
        message = mapping(record.get("message"))
        message_id = string(message.get("id"))
        for event in _events(line, record):
            identity = (
                message_id or event.native_id or event.event_id,
                digest(
                    canonical_json({"kind": event.kind, "text": event.text, "data": event.data})
                ),
            )
            if identity not in seen:
                seen.add(identity)
                events.append(event)
        raw = mapping(message.get("usage"))
        if raw:
            usage_identity = message_id or f"unknown-{line}"
            try:
                usage[usage_identity] = UsageRecord(
                    identity=usage_identity,
                    source_line=line,
                    usage=_usage(raw),
                    raw=raw,
                    accounting="response" if message_id else "superseded",
                )
            except ValidationError:
                warnings.append(
                    Diagnostic(
                        code="invalid_usage",
                        message="Token categories are inconsistent.",
                        line=line,
                    )
                )
            if not message_id:
                warnings.append(
                    Diagnostic(
                        code="usage_identity_missing",
                        message="Usage without message.id cannot be reliably deduplicated.",
                        line=line,
                    )
                )
        if record.get("type") == "system":
            events.append(
                LogEvent(
                    event_id=f"e{line:06d}",
                    source_line=line,
                    kind="lifecycle",
                    timestamp=string(record.get("timestamp")),
                    data={
                        key: value
                        for key, value in record.items()
                        if key in {"subtype", "stopReason", "preventedContinuation"}
                    },
                )
            )
        if record.get("type") == "cost-state":
            metadata["cost_state"] = {
                key: value
                for key, value in record.items()
                if key in {"totalCostUSD", "modelUsage", "hasUnknownModelCost"}
            }
    if source_id is None:
        warnings.append(Diagnostic(code="missing_metadata", message="No session identity found."))
    warnings.append(
        Diagnostic(
            code="baseline_unknown",
            message="Branch labels do not establish a historical baseline SHA.",
        )
    )
    return Session(
        agent=AgentKind.CLAUDE,
        source_id=source_id,
        source_version=version,
        source_path_hash=source_hash(path),
        source_content_hash=digest(content),
        metadata=metadata,
        events=tuple(events),
        usage=tuple(usage.values()),
        warnings=tuple(warnings),
        limits=limits,
    )
