from contextlib import suppress
from typing import Literal

from pydantic import Field, JsonValue, ValidationError

from dryheave.controller_models import RoleObservation
from dryheave.errors import InputError
from dryheave.models import StrictModel, TokenUsage
from dryheave.serialization import canonical_json, parse_json, parse_model


class ClaudeResult(StrictModel):
    type: Literal["result"]
    subtype: str
    is_error: bool
    duration_ms: int = Field(ge=0)
    duration_api_ms: int = Field(ge=0)
    num_turns: int = Field(ge=0)
    session_id: str = Field(min_length=1)
    uuid: str | None = None
    result: str | None = None
    stop_reason: str | None = None
    total_cost_usd: float | None = Field(default=None, ge=0)
    usage: dict[str, JsonValue]
    modelUsage: dict[str, dict[str, JsonValue]]
    permission_denials: tuple[dict[str, JsonValue], ...]
    structured_output: dict[str, JsonValue] | None = None
    errors: tuple[str, ...] = ()
    fast_mode_state: str | None = None


def claude_payload(content: bytes) -> bytes:
    result = parse_model(content, ClaudeResult)
    if (
        result.subtype != "success"
        or result.is_error
        or result.errors
        or result.permission_denials
        or result.structured_output is None
        or result.stop_reason not in {None, "end_turn", "stop_sequence"}
    ):
        raise InputError("Claude controller did not return a successful structured result.")
    tools = result.usage.get("server_tool_use")
    if isinstance(tools, dict) and any(value != 0 for value in tools.values()):
        raise InputError(
            "Claude controller reported server tool use under its tool-disabled policy."
        )
    return canonical_json(result.structured_output)


def claude_observation(content: bytes) -> RoleObservation:
    raw = parse_json(content)
    usage = None
    values = raw.get("usage")
    if isinstance(values, dict):
        counters = {
            field: values.get(name)
            for field, name in (
                ("uncached_input", "input_tokens"),
                ("cache_read", "cache_read_input_tokens"),
                ("cache_write", "cache_creation_input_tokens"),
                ("output", "output_tokens"),
            )
        }
        if all(
            value is None or (type(value) is int and value >= 0) for value in counters.values()
        ) and any(counters.values()):
            with suppress(ValidationError):
                usage = TokenUsage.model_validate(
                    counters
                    | {
                        "provenance": "claude-print-2.1.278-result-cumulative; reasoning is not separately observed"
                    }
                )
    models = raw.get("modelUsage")
    observed = []
    if isinstance(models, dict):
        for name, detail in models.items():
            if isinstance(detail, dict) and any(
                type(value := detail.get(field)) is int and value > 0
                for field in (
                    "inputTokens",
                    "outputTokens",
                    "cacheReadInputTokens",
                    "cacheCreationInputTokens",
                )
            ):
                observed.append(name)
    try:
        return RoleObservation(
            usage=usage, observed_model=observed[0] if len(observed) == 1 else None
        )
    except ValidationError:
        return RoleObservation(usage=usage)
