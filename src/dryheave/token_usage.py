from typing import Literal

from pydantic import JsonValue, ValidationError

from dryheave.models import TokenUsage

CODEX_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def codex_usage(
    raw: dict[str, JsonValue],
    *,
    protocol: Literal["native-log", "exec-0.153.4"],
    provenance: str,
) -> TokenUsage | None:
    values = [raw.get(name) for name in CODEX_TOKEN_FIELDS]
    if any(value is not None and (type(value) is not int or value < 0) for value in values):
        return None
    if protocol == "exec-0.153.4":
        if not any(values):
            return None
        if "cache_write_input_tokens" not in raw:
            values[2] = 0
    total, cached, written, output, reasoning = (
        value if type(value) is int else None for value in values
    )
    if total is not None and total < sum(value for value in (cached, written) if value is not None):
        return None
    uncached = (
        total - cached - written
        if total is not None and cached is not None and written is not None
        else None
    )
    try:
        return TokenUsage(
            uncached_input=uncached,
            cache_read=cached,
            cache_write=written,
            output=output,
            reasoning=reasoning,
            provenance=provenance,
        )
    except ValidationError:
        return None
