from pydantic import JsonValue, ValidationError

from dryheave.drivers.models import NativeEvent, NativeUsage
from dryheave.logs.base import mapping, string
from dryheave.models import AgentKind, TokenUsage
from dryheave.token_usage import codex_usage


def usage_record(agent: AgentKind, event: NativeEvent) -> NativeUsage:
    raw = mapping(event.data.get("usage"))
    response = string(event.data.get("response_id"))
    accounting = (
        "response"
        if response
        else "cumulative"
        if event.data.get("accounting") == "cumulative"
        else "unknown"
    )

    def number(key: str) -> int | None:
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    provenance = f"{agent}.native-log:{event.source}:{event.line}"
    thinking: JsonValue | None = mapping(raw.get("output_tokens_details")).get("thinking_tokens")
    try:
        usage = (
            codex_usage(raw, protocol="native-log", provenance=provenance)
            if agent == AgentKind.CODEX
            else TokenUsage(
                uncached_input=number("input_tokens"),
                cache_read=number("cache_read_input_tokens"),
                cache_write=number("cache_creation_input_tokens"),
                output=number("output_tokens"),
                reasoning=thinking if type(thinking) is int else None,
                provenance=provenance,
            )
        )
    except ValidationError:
        usage = None
    identity = f"{event.session_id}:{event.thread_id}:{response or f'{event.source}:{event.line}'}"
    return NativeUsage.model_validate(
        {
            "identity": identity,
            "session_id": event.session_id,
            "thread_id": event.thread_id,
            "turn_id": event.turn_id,
            "root_turn_id": event.root_turn_id,
            "response_id": response,
            "source": event.source,
            "line": event.line,
            "accounting": accounting,
            "usage": usage,
            "raw": raw,
        }
    )


class UsageLedger:
    def __init__(self, agent: AgentKind) -> None:
        self.agent = agent
        self.records: dict[str, NativeUsage] = {}

    def add(self, event: NativeEvent) -> None:
        record = usage_record(self.agent, event)
        self.records[record.identity] = record

    def snapshot(self) -> tuple[NativeUsage, ...]:
        keyed = {
            (r.session_id, r.thread_id) for r in self.records.values() if r.accounting == "response"
        }
        return tuple(
            r.model_copy(update={"accounting": "superseded"})
            if r.accounting == "cumulative" and (r.session_id, r.thread_id) in keyed
            else r
            for r in self.records.values()
        )
