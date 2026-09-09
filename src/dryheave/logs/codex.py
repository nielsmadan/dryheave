from collections import defaultdict, deque
from pathlib import Path

from pydantic import JsonValue

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
from dryheave.models import AgentKind
from dryheave.serialization import digest
from dryheave.token_usage import codex_usage


def _event(
    line: int, record: dict[str, JsonValue], item: dict[str, JsonValue], kind: str, turn: str | None
) -> LogEvent:
    role = {"UserMessage": "user", "AgentMessage": "assistant", "agent_message": "assistant"}.get(
        kind, string(item.get("role")) or kind
    )
    text = text_content(item.get("content")) or string(item.get("message")) or ""
    data: dict[str, JsonValue] = {}
    if role not in {"user", "assistant"}:
        role = "tool_result" if kind.endswith("_output") else "tool_call"
        data = {
            key: value
            for key, value in item.items()
            if key
            in {
                "type",
                "name",
                "namespace",
                "arguments",
                "input",
                "output",
                "call_id",
                "command",
                "cwd",
                "status",
                "stdout",
                "stderr",
                "exit_code",
                "aggregated_output",
                "server",
                "tool",
                "result",
                "changes",
                "agent_thread_id",
                "kind",
            }
        }
        text = (
            text_content(item.get("output"))
            or string(item.get("aggregated_output"))
            or string(item.get("stdout"))
            or ""
        )
    return LogEvent.model_validate(
        {
            "event_id": f"e{line:06d}",
            "source_line": line,
            "kind": role,
            "text": text,
            "timestamp": string(record.get("timestamp")),
            "native_id": string(item.get("id")) or string(item.get("call_id")),
            "turn_id": turn,
            "data": data,
        }
    )


def _record_event(line: int, record: dict[str, JsonValue]) -> LogEvent | None:
    payload = mapping(record.get("payload"))
    kind = string(payload.get("type")) or ""
    turn = string(payload.get("turn_id")) or string(
        mapping(payload.get("internal_chat_message_metadata_passthrough")).get("turn_id")
    )
    if record.get("type") == "event_msg":
        if kind == "item_completed":
            item = mapping(payload.get("item"))
            item_kind = string(item.get("type")) or ""
            if item_kind in {"Reasoning", "ContextCompaction"}:
                return None
            return _event(line, record, item, item_kind, turn)
        if kind in {"user_message", "agent_message"}:
            return _event(
                line,
                record,
                payload | {"role": "user" if kind == "user_message" else "assistant"},
                "message",
                turn,
            )
        if kind in {"task_started", "task_complete", "turn_aborted"}:
            return LogEvent(
                event_id=f"e{line:06d}",
                source_line=line,
                kind="lifecycle",
                timestamp=string(record.get("timestamp")),
                turn_id=turn,
                data={
                    key: value
                    for key, value in payload.items()
                    if key in {"type", "turn_id", "started_at", "completed_at", "duration_ms"}
                },
            )
    elif record.get("type") == "response_item":
        if kind in {"message", "agent_message"}:
            if kind == "message" and payload.get("role") not in {"user", "assistant"}:
                return None
            return _event(line, record, payload, kind, turn)
        if kind in {
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
        }:
            return _event(line, record, payload, kind, turn)
    return None


class _Candidates:
    def __init__(self, events: list[tuple[str, LogEvent]]) -> None:
        self.native: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        self.text: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        self.turn: dict[tuple[str, str, str | None], deque[int]] = defaultdict(deque)
        self.consumed: set[int] = set()
        for index, (source, event) in enumerate(events):
            if source != "event_msg" or event.kind not in {"user", "assistant"}:
                continue
            if event.native_id is not None:
                self.native[(event.kind, event.native_id)].append(index)
            self.text[(event.kind, event.text)].append(index)
            self.turn[(event.kind, event.text, event.turn_id)].append(index)

    def consume(self, event: LogEvent) -> bool:
        queues = []
        if event.native_id is not None:
            queues.append(self.native.get((event.kind, event.native_id)))
        if event.turn_id is None:
            queues.append(self.text.get((event.kind, event.text)))
        else:
            queues.extend(
                self.turn.get((event.kind, event.text, turn)) for turn in (event.turn_id, None)
            )
        candidates = []
        for queue in queues:
            while queue and queue[0] in self.consumed:
                queue.popleft()
            if queue:
                candidates.append(queue[0])
        if not candidates:
            return False
        self.consumed.add(min(candidates))
        return True


def _dedupe(events: list[tuple[str, LogEvent]]) -> tuple[LogEvent, ...]:
    candidates = _Candidates(events)
    return tuple(
        event
        for source, event in events
        if not (
            source == "response_item"
            and event.kind in {"user", "assistant"}
            and candidates.consume(event)
        )
    )


def parse(path: Path, limits: ImportLimits | None = None) -> Session:
    limits = limits or ImportLimits()
    content, records, warnings = read_records(path, limits)
    events: list[tuple[str, LogEvent]] = []
    usage: dict[str, UsageRecord] = {}
    metadata: dict[str, JsonValue] = {}
    source_id = version = parent = None
    for line, record in records:
        payload = mapping(record.get("payload"))
        if record.get("type") == "session_meta":
            incoming = string(payload.get("id")) or string(payload.get("session_id"))
            if source_id is not None and incoming != source_id:
                warnings.append(
                    Diagnostic(
                        code="ambiguous_session",
                        message="Conflicting session identities.",
                        line=line,
                    )
                )
            source_id, version = incoming, string(payload.get("cli_version"))
            parent = string(payload.get("parent_thread_id"))
            metadata.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key
                    in {"cwd", "git", "history_mode", "source", "model_provider", "thread_source"}
                }
            )
        elif record.get("type") == "turn_context":
            for key in ("cwd", "model", "effort"):
                if key in payload:
                    if key == "cwd" and key in metadata and metadata[key] != payload[key]:
                        warnings.append(
                            Diagnostic(
                                code="ambiguous_cwd",
                                message="Observed working directory changed.",
                                line=line,
                            )
                        )
                    metadata[key] = payload[key]
        elif record.get("type") == "compacted":
            warnings.append(
                Diagnostic(
                    code="compacted",
                    message="Compaction recorded; replacement summaries are not replayed as dialogue.",
                    line=line,
                )
            )
        event = _record_event(line, record)
        if event is not None:
            events.append((string(record.get("type")) or "", event))
        _record_usage(line, record, usage, warnings)
    if source_id is None:
        warnings.append(
            Diagnostic(code="missing_metadata", message="No session metadata identity found.")
        )
    if not mapping(metadata.get("git")).get("commit_hash"):
        warnings.append(
            Diagnostic(
                code="baseline_unknown",
                message="No explicit historical commit metadata; curator must supply and verify a SHA.",
            )
        )
    has_responses = any(item.accounting == "response" for item in usage.values())
    final_usage = tuple(
        item.model_copy(update={"accounting": "superseded"})
        if has_responses and item.accounting == "cumulative"
        else item
        for item in usage.values()
    )
    return Session(
        agent=AgentKind.CODEX,
        source_id=source_id,
        source_version=version,
        parent_session_id=parent,
        source_path_hash=source_hash(path),
        source_content_hash=digest(content),
        metadata=metadata,
        events=_dedupe(events),
        usage=final_usage,
        warnings=tuple(warnings),
        limits=limits,
    )


def _record_usage(
    line: int,
    record: dict[str, JsonValue],
    usage: dict[str, UsageRecord],
    warnings: list[Diagnostic],
) -> None:
    payload = mapping(record.get("payload"))
    keyed = record.get("type") == "token_usage_record"
    legacy = record.get("type") == "event_msg" and payload.get("type") == "token_count"
    if not (keyed or legacy):
        return
    raw = (
        mapping(payload.get("usage"))
        if keyed
        else mapping(mapping(payload.get("info")).get("total_token_usage"))
    )
    if not raw:
        return
    response = string(payload.get("response_id"))
    if keyed and response is None:
        warnings.append(
            Diagnostic(
                code="usage_identity_missing",
                message="Response usage has no identity; accounting remains cumulative/unknown.",
                line=line,
            )
        )
    identity = (
        f"{string(payload.get('thread_id')) or 'root'}:{response}"
        if response
        else f"cumulative-{line}"
    )
    parsed = codex_usage(
        raw,
        protocol="native-log",
        provenance="codex.token_usage_record" if keyed else "codex.token_count.total_token_usage",
    )
    if parsed is None:
        warnings.append(
            Diagnostic(
                code="invalid_usage",
                message="Token categories are inconsistent; record excluded from accounting.",
                line=line,
            )
        )
        return
    usage[identity] = UsageRecord(
        identity=identity,
        source_line=line,
        usage=parsed,
        raw=raw,
        accounting="response" if response else "cumulative",
    )
