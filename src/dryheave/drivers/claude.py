import re
from typing import NamedTuple, NewType

from pydantic import JsonValue

from dryheave.drivers.models import NativeEvent
from dryheave.errors import InputError
from dryheave.logs.base import mapping, string, text_content

COMMAND_TAG = re.compile(r"<(command-message|command-name|command-args)>(.*?)</\1>", re.DOTALL)
LOCAL_OUTPUT_TAG = re.compile(r"<local-command-(stdout|stderr)>(.*)</local-command-\1>", re.DOTALL)
NOTIFICATION_TAG = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
TASK_ID_TAG = re.compile(r"<task-id>(.*?)</task-id>", re.DOTALL)
TOOL_USE_ID_TAG = re.compile(r"<tool-use-id>(.*?)</tool-use-id>", re.DOTALL)
STATUS_TAG = re.compile(r"<status>(.*?)</status>", re.DOTALL)
TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})

TaskId = NewType("TaskId", str)
TurnId = NewType("TurnId", str)
ToolUseId = NewType("ToolUseId", str)


class BackgroundTask(NamedTuple):
    task: TaskId
    agent: bool


class BackgroundOwner(NamedTuple):
    turn: TurnId | None
    agent: bool


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


def _tag_texts(pattern: re.Pattern[str], text: str) -> list[str]:
    return [found.strip() for found in pattern.findall(text) if found.strip()]


def _notified_tasks(text: str, launches: dict[ToolUseId, TaskId]) -> set[TaskId]:
    finished: set[TaskId] = set()
    for block in NOTIFICATION_TAG.findall(text):
        if not TERMINAL_STATUSES.intersection(_tag_texts(STATUS_TAG, block)):
            continue
        tasks = [TaskId(task) for task in _tag_texts(TASK_ID_TAG, block)]
        uses = [launches.get(ToolUseId(use)) for use in _tag_texts(TOOL_USE_ID_TAG, block)]
        finished.update(tasks or [task for task in uses if task is not None])
    return finished


def _launched_task(result: dict[str, JsonValue]) -> BackgroundTask | None:
    agent = string(result.get("resumedAgentId")) or (
        string(result.get("agentId")) if result.get("isAsync") is True else None
    )
    monitor = string(result.get("taskId")) if "timeoutMs" in result else None
    other = string(result.get("backgroundTaskId")) or monitor
    if agent is not None:
        return BackgroundTask(TaskId(agent), agent=True)
    return None if other is None else BackgroundTask(TaskId(other), agent=False)


class ClaudeAdapter:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.turn_id: TurnId | None = None
        self.chain: dict[str, TurnId] = {}
        self.pending_completion: TurnId | None = None
        self.held: TurnId | None = None
        self.held_agents: int | None = None
        self.described = False
        self.background: dict[TaskId, BackgroundOwner] = {}
        self.launches: dict[ToolUseId, TaskId] = {}

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
        native_id, parent_id = string(record.get("uuid")), string(record.get("parentUuid"))
        parent_turn = self._parent_turn(record, parent_id)
        local_output = self._local_output(record, message)
        notification = self._notification(record, message)
        if local_output is not None:
            events.extend(self._handle_local_output(record, local_output, native_id, parent_turn))
        elif notification is not None:
            events.extend(self._handle_notification(record, notification, native_id, parent_turn))
        elif self._human(record, message):
            events.extend(self._handle_human(record, message, native_id))
        elif message.get("role") in {"assistant", "user"}:
            events.extend(self._handle_response(record, message, native_id, parent_turn))
        elif record.get("type") == "system":
            events.extend(self._system(record))
        elif record.get("type") == "attachment":
            events.extend(self._attachment(record))
        if native_id is not None and parent_turn is not None:
            self.chain.setdefault(native_id, parent_turn)
        return tuple(events)

    def _parent_turn(self, record: dict[str, JsonValue], parent_id: str | None) -> TurnId | None:
        if parent_id is None and record.get("subtype") == "compact_boundary":
            logical = string(record.get("logicalParentUuid"))
            return self.chain.get(logical or "") or self.turn_id
        return self.chain.get(parent_id or "")

    def _handle_local_output(
        self,
        record: dict[str, JsonValue],
        output: str,
        native_id: str | None,
        parent_turn: TurnId | None,
    ) -> list[NativeEvent]:
        if native_id is None:
            return [
                self._event("unsupported", record, text="Local command output lacks UUID identity.")
            ]
        if parent_turn is None:
            return [
                self._event(
                    "unsupported", record, text="Local command output has no tracked parent turn."
                )
            ]
        self.turn_id = parent_turn
        return [
            self._event("assistant", record, text=output),
            self._event("completed", record, turn_id=parent_turn),
        ]

    def _handle_notification(
        self,
        record: dict[str, JsonValue],
        text: str,
        native_id: str | None,
        parent_turn: TurnId | None,
    ) -> list[NativeEvent]:
        owners = self._finish_background_tasks(text)
        self.turn_id = parent_turn or next(
            (owner.turn for owner in owners if owner.turn is not None), None
        )
        self.pending_completion = None
        if native_id is not None and self.turn_id is not None:
            self.chain[native_id] = self.turn_id
        if record.get("queueTranscriptOnly") is not True or self.held is None:
            return []
        # Claude records this notification without answering it, so no turn_duration follows.
        if self.held_agents is not None:
            self.held_agents -= sum(owner.agent for owner in owners)
        if (self.held_agents or 0) > 0 or any(owner.agent for owner in self.background.values()):
            return []
        held, self.held, self.held_agents = self.held, None, None
        self.turn_id = held
        return [self._event("completed", record, turn_id=held, data={"subtype": "notification"})]

    def _handle_human(
        self, record: dict[str, JsonValue], message: dict[str, JsonValue], native_id: str | None
    ) -> list[NativeEvent]:
        if native_id is None:
            return [self._event("unsupported", record, text="Human input lacks UUID identity.")]
        self.turn_id = TurnId(native_id)
        self.chain[native_id] = self.turn_id
        self.pending_completion = None
        self.held = self.held_agents = None
        self.background.clear()
        self.launches.clear()
        command = command_line(message.get("content"))
        return [
            self._event(
                "accepted",
                record,
                text=text_content(message.get("content")) if command is None else command[0],
                data={} if command is None else {"command": command[1]},
            ),
            self._event("started", record),
        ]

    def _handle_response(
        self,
        record: dict[str, JsonValue],
        message: dict[str, JsonValue],
        native_id: str | None,
        parent_turn: TurnId | None,
    ) -> list[NativeEvent]:
        self.turn_id = parent_turn
        if native_id is not None and parent_turn is not None:
            self.chain[native_id] = parent_turn
        self._track(record, message)
        return self._response(record, message)

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
    def _notification(record: dict[str, JsonValue], message: dict[str, JsonValue]) -> str | None:
        if message.get("role") != "user" or record.get("isSidechain") is True:
            return None
        text = text_content(message.get("content"))
        notifies = mapping(record.get("origin")).get("kind") == "task-notification"
        return text if notifies or text.lstrip().startswith("<task-notification>") else None

    def _attachment(self, record: dict[str, JsonValue]) -> list[NativeEvent]:
        attachment = mapping(record.get("attachment"))
        if attachment.get("commandMode") == "task-notification":
            prompt = attachment.get("prompt")
            self._finish_background_tasks(
                prompt if isinstance(prompt, str) else text_content(prompt)
            )
        return []

    def _finish_background_tasks(self, text: str) -> list[BackgroundOwner]:
        owners = []
        for task in sorted(_notified_tasks(text, self.launches)):
            owner = self._forget(task)
            if owner is not None:
                owners.append(owner)
        return owners

    def _forget(self, task: TaskId) -> BackgroundOwner | None:
        self.launches = {use: known for use, known in self.launches.items() if known != task}
        return self.background.pop(task, None)

    def _track(self, record: dict[str, JsonValue], message: dict[str, JsonValue]) -> None:
        result = mapping(record.get("toolUseResult"))
        content = message.get("content")
        uses = [
            ToolUseId(use)
            for block in (content if isinstance(content, list) else [])
            if mapping(block).get("type") == "tool_result"
            and (use := string(mapping(block).get("tool_use_id"))) is not None
        ]
        launched = _launched_task(result)
        if launched is not None:
            self.background[launched.task] = BackgroundOwner(self.turn_id, agent=launched.agent)
            self.launches.update(dict.fromkeys(uses, launched.task))
        stopped = string(result.get("task_id"))
        if stopped is not None and "task_type" in result:
            self._forget(TaskId(stopped))

    @staticmethod
    def _human(record: dict[str, JsonValue], message: dict[str, JsonValue]) -> bool:
        content = message.get("content")
        return (
            message.get("role") == "user"
            and record.get("isMeta") is not True
            and record.get("isSidechain") is not True
            and record.get("isCompactSummary") is not True
            and record.get("isVisibleInTranscriptOnly") is not True
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
            reported = record.get("pendingBackgroundAgentCount")
            if self._agents_pending(reported):
                self.held = self.pending_completion
                self.held_agents = (
                    reported
                    if isinstance(reported, int) and not isinstance(reported, bool)
                    else None
                )
                self.pending_completion = None
                return []
            self.held = self.held_agents = None
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

    def _agents_pending(self, reported: JsonValue) -> bool:
        if isinstance(reported, int) and not isinstance(reported, bool):
            return reported > 0
        return any(owner.agent for owner in self.background.values())

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
