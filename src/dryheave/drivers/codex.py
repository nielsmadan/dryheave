from pydantic import JsonValue

from dryheave.drivers.models import NativeEvent
from dryheave.errors import InputError
from dryheave.logs.base import mapping, string, text_content


class CodexAdapter:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.parent_session_id: str | None = None
        self.turn_id: str | None = None

    def feed(self, record: dict[str, JsonValue]) -> tuple[NativeEvent, ...]:
        payload = mapping(record.get("payload"))
        source = record.get("type")
        if source == "session_meta":
            incoming = string(payload.get("id")) or string(payload.get("session_id"))
            if self.session_id is not None and incoming != self.session_id:
                raise InputError("Native log changed its session identity.")
            self.session_id = incoming
            self.parent_session_id = string(payload.get("parent_thread_id"))
            if self.parent_session_id is None:
                fork = mapping(
                    mapping(mapping(payload.get("source")).get("subagent")).get("thread_spawn")
                )
                self.parent_session_id = string(fork.get("parent_thread_id"))
            return (
                self._event(
                    "metadata",
                    payload,
                    data={
                        key: payload[key]
                        for key in ("cwd", "cli_version", "history_mode", "source")
                        if key in payload
                    },
                ),
            )
        if source == "turn_context":
            return (
                self._event(
                    "metadata",
                    payload,
                    data={
                        key: payload[key] for key in ("model", "effort", "cwd") if key in payload
                    },
                ),
            )
        if source == "token_usage_record":
            return (
                self._event(
                    "usage",
                    payload,
                    data={
                        "accounting": "response",
                        "usage": payload.get("usage"),
                        "response_id": payload.get("response_id"),
                    },
                ),
            )
        if source == "event_msg":
            return self._message(payload)
        if source == "response_item" and payload.get("type") in {
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
        }:
            return (self._event("tool", payload, data=payload),)
        return ()

    def _message(self, payload: dict[str, JsonValue]) -> tuple[NativeEvent, ...]:
        kind = payload.get("type")
        if kind in {"task_started", "turn_started"}:
            self.turn_id = string(payload.get("turn_id"))
            return (self._event("started", payload),)
        if kind in {"task_complete", "turn_complete", "turn_aborted"}:
            event = self._event(
                "aborted" if kind == "turn_aborted" else "completed",
                payload,
                text=string(payload.get("last_agent_message")) or "",
            )
            self.turn_id = None
            return (event,)
        if kind == "token_count":
            return (
                self._event(
                    "usage",
                    payload,
                    data={
                        "accounting": "cumulative",
                        "usage": mapping(payload.get("info")).get("total_token_usage"),
                    },
                ),
            )
        if kind == "item_completed":
            item = mapping(payload.get("item"))
            if item.get("type") in {"UserMessage", "AgentMessage"}:
                return (
                    self._event(
                        "accepted" if item.get("type") == "UserMessage" else "assistant",
                        payload,
                        native_id=string(item.get("id")),
                        text=text_content(item.get("content")),
                        data={"phase": item.get("phase")},
                    ),
                )
            if item.get("type") in {
                "CommandExecution",
                "McpToolCall",
                "DynamicToolCall",
                "CollabAgentSpawn",
                "CollabAgentToolCall",
                "FileChange",
            }:
                return (self._event("tool", payload, data=item),)
        if kind in {"user_message", "agent_message"}:
            return (
                self._event(
                    "accepted" if kind == "user_message" else "assistant",
                    payload,
                    text=string(payload.get("message")) or "",
                ),
            )
        return ()

    def _event(self, kind: str, payload: dict[str, JsonValue], **fields: object) -> NativeEvent:
        return NativeEvent.model_validate(
            {
                "kind": kind,
                "session_id": string(payload.get("session_id")) or self.session_id,
                "parent_session_id": self.parent_session_id,
                "thread_id": string(payload.get("thread_id")) or self.session_id,
                "turn_id": string(payload.get("turn_id")) or self.turn_id,
                "root_turn_id": string(payload.get("root_turn_id")),
                **fields,
            }
        )
