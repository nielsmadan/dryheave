import json

import pytest

from dryheave.controllers import _codex_usage
from dryheave.drivers.models import NativeEvent
from dryheave.drivers.usage import usage_record
from dryheave.logs import codex
from dryheave.models import AgentKind
from dryheave.token_usage import CODEX_TOKEN_FIELDS, codex_usage


@pytest.mark.parametrize("field", CODEX_TOKEN_FIELDS)
@pytest.mark.parametrize("change", ["absent", None, 0, -1, True, "0", 1.5])
def test_codex_token_policy_preserves_unknowns_and_rejects_invalid_counts(tmp_path, field, change):
    raw = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 10,
        "output_tokens": 30,
        "reasoning_output_tokens": 0,
    }
    if change == "absent":
        del raw[field]
    else:
        raw[field] = change
    native = usage_record(
        AgentKind.CODEX, NativeEvent(kind="usage", data={"response_id": "response", "usage": raw})
    ).usage
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps(
            {"type": "token_usage_record", "payload": {"response_id": "response", "usage": raw}}
        )
        + "\n"
    )
    imported = codex.parse(path)
    recorded = imported.usage[0].usage if imported.usage else None
    executed = _codex_usage(json.dumps({"type": "turn.completed", "usage": raw}).encode())

    def categories(usage):
        return usage.model_dump(exclude={"provenance"}) if usage else None

    assert categories(native) == categories(recorded)
    if field == "cache_write_input_tokens" and change == "absent":
        assert native.cache_write is None
        assert native.uncached_input is None
        assert executed.cache_write == 0
        assert executed.uncached_input == 80
    else:
        assert categories(executed) == categories(native)
    if change is None and field == "cache_write_input_tokens":
        assert executed.cache_write is None
        assert executed.uncached_input is None
    if native:
        assert native.provenance.startswith("codex.native-log:")
        assert recorded.provenance == "codex.token_usage_record"
        assert "codex-exec-0.153.4" in executed.provenance


@pytest.mark.parametrize("changes", [{"input_tokens": 5}, {"reasoning_output_tokens": 31}])
def test_codex_inconsistent_categories_are_not_accepted(changes):
    raw = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 10,
        "output_tokens": 30,
        "reasoning_output_tokens": 0,
    } | changes
    for protocol in ("native-log", "exec-0.153.4"):
        assert codex_usage(raw, protocol=protocol, provenance="fixture") is None


def test_exec_zero_fallback_remains_unknown_while_explicit_log_zero_is_observed():
    raw = dict.fromkeys(CODEX_TOKEN_FIELDS, 0)
    assert codex_usage(raw, protocol="exec-0.153.4", provenance="exec") is None
    assert codex_usage(raw, protocol="native-log", provenance="log").uncached_input == 0
