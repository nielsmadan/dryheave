import re

from pydantic import JsonValue

from dryheave.drivers.models import NativeEvent
from dryheave.errors import InputError
from dryheave.logs.base import mapping, string, text_content

COMMAND_TAG = re.compile(r"<(command-message|command-name|command-args)>(.*?)</\1>", re.S)
LOCAL_OUTPUT_TAG = re.compile(r"<local-command-(stdout|stderr)>(.*)</local-command-\1>", re.S)


def command_line(content: JsonValue) -> tuple[str, str] | None:
    if not isinstance(content, str):
        return None
    tags = COMMAND_TAG.findall(content)
    fields = dict(tags)
    name = fields.get("command-name", "")
    if len(fields) != len(tags) or not name.startswith("/") or COMMAND_TAG.sub("", content).strip():
        return None
    arguments = fields.get("command-args", "")
    return (f"{name} {arguments}" if arguments else name), name


def local_command_output(content: JsonValue) -> str | None:
    if not isinstance(content, str):
        return None
    match = LOCAL_OUTPUT_TAG.fullmatch(content)
    return match.group(2) if match else None


class ClaudeAdapter:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.turn_id: str | None = None
        self.chain: dict[str, str] = {}
        self.pending_completion: str | None = None
        self.described = False

    def feed(self, record: dict[str, JsonValue]) -> tuple[NativeEvent, ...]:
        session = string(record.get("sessionId")) or string(record.get("session_id"))
        if session is None:
            return ()
        if self.session_id is not None and session != self.session_id:
            raise InputError("Native Claude log changed its session identity.")
        self.session_id = session
        describes = not self.described and "cwd" in record
        self.described = self.described or describes
        metadata = self._event(
            "metadata",
            record,
            data={
                key: record[key]
                for key in ("cwd", "version", "isSidechain", "agentId")
                if key in record
            },
        )
        events = [metadata] if describes else []
        message = mapping(record.get("message"))
        role = message.get("role")
        native_id, parent_id = string(record.get("uuid")), string(record.get("parentUuid"))
        parent_turn = self.chain.get(parent_id or "")
        local_output = self._local_output(record, message)
        if local_output is not None:
            if native_id is None:
                events.append(
                    self._event(
                        "unsupported", record, text="Local command output lacks UUID identity."
                    )
                )
            elif parent_turn is None:
                events.append(
                    self._event(
                        "unsupported",
                        record,
                        text="Local command output has no tracked parent turn.",
                    )
                )
            else:
                self.turn_id = parent_turn
                events.extend(
                    (
                        self._event("assistant", record, text=local_output),
                        self._event("completed", record, turn_id=parent_turn),
                    )
                )
        elif self._human(record, message):
            if native_id is None:
                events.append(
                    self._event("unsupported", record, text="Human input lacks UUID identity.")
                )
            else:
                self.turn_id = native_id
                self.chain[native_id] = native_id
                self.pending_completion = None
                command = command_line(message.get("content"))
                events.extend(
                    (
                        self._event(
                            "accepted",
                            record,
                            text=text_content(message.get("content"))
                            if command is None
                            else command[0],
                            data={} if command is None else {"command": command[1]},
                        ),
                        self._event("started", record),
                    )
                )
        elif role in {"assistant", "user"}:
            self.turn_id = parent_turn
            if native_id is not None and parent_turn is not None:
                self.chain[native_id] = parent_turn
            events.extend(self._response(record, message))
        elif record.get("type") == "system":
            events.extend(self._system(record))
        if native_id is not None and parent_turn is not None:
            self.chain.setdefault(native_id, parent_turn)
        return tuple(events)

    @staticmethod
    def _local_output(record: dict[str, JsonValue], message: dict[str, JsonValue]) -> str | None:
        if (
            message.get("role") != "user"
            or record.get("isMeta") is True
            or record.get("isSidechain") is True
        ):
            return None
        return local_command_output(message.get("content"))

    @staticmethod
    def _human(record: dict[str, JsonValue], message: dict[str, JsonValue]) -> bool:
        content = message.get("content")
        return (
            message.get("role") == "user"
            and record.get("isMeta") is not True
            and record.get("isSidechain") is not True
            and (
                isinstance(content, str)
                or (
                    isinstance(content, list)
                    and bool(content)
                    and all(mapping(item).get("type") == "text" for item in content)
                )
            )
        )

    def _response(
        self, record: dict[str, JsonValue], message: dict[str, JsonValue]
    ) -> list[NativeEvent]:
        events = []
        content = message.get("content")
        if message.get("role") == "assistant":
            if "model" in message or "effort" in record:
                events.append(
                    self._event(
                        "metadata",
                        record,
                        data={
                            "model": message.get("model"),
                            "effort": record.get("effort"),
                            "isSidechain": record.get("isSidechain"),
                        },
                    )
                )
            self.pending_completion = (
                self.turn_id
                if message.get("stop_reason") in {"end_turn", "stop_sequence"}
                else None
            )
            text = text_content(content)
            if text:
                events.append(
                    self._event(
                        "assistant",
                        record,
                        text=text,
                        data={
                            "stop_reason": message.get("stop_reason"),
                            "model": message.get("model"),
                            "effort": record.get("effort"),
                        },
                    )
                )
        if isinstance(content, list):
            events.extend(
                self._event("tool", record, data=mapping(block))
                for block in content
                if mapping(block).get("type") in {"tool_use", "tool_result"}
            )
        if isinstance(message.get("usage"), dict):
            events.append(
                self._event(
                    "usage",
                    record,
                    data={
                        "accounting": "response",
                        "usage": message["usage"],
                        "response_id": message.get("id"),
                        "model": message.get("model"),
                    },
                )
            )
        return events

    def _system(self, record: dict[str, JsonValue]) -> list[NativeEvent]:
        subtype = record.get("subtype")
        if subtype == "turn_duration" and self.pending_completion is not None:
            return [
                self._event(
                    "completed", record, turn_id=self.pending_completion, data={"subtype": subtype}
                )
            ]
        if subtype in {"stop_hook_summary", "stop_hook", "hook_started", "hook_response"}:
            if record.get("preventedContinuation") is True:
                self.pending_completion = None
            return [
                self._event(
                    "hook",
                    record,
                    data={
                        key: record[key]
                        for key in (
                            "subtype",
                            "stopReason",
                            "preventedContinuation",
                            "hookCount",
                            "hookErrors",
                        )
                        if key in record
                    },
                )
            ]
        if subtype in {"interrupted", "turn_aborted"}:
            self.pending_completion = None
            return [self._event("aborted", record)]
        return []

    def _event(self, kind: str, record: dict[str, JsonValue], **fields: object) -> NativeEvent:
        return NativeEvent.model_validate(
            {
                "kind": kind,
                "session_id": self.session_id,
                "parent_session_id": string(record.get("parentSessionId")),
                "thread_id": string(record.get("agentId")) or self.session_id,
                "turn_id": self.turn_id,
                "native_id": string(record.get("uuid")),
                "parent_id": string(record.get("parentUuid")),
                **fields,
            }
        )
