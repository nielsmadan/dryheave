from dryheave.drivers.models import NativeEvent
from dryheave.drivers.usage import UsageLedger
from dryheave.models import AgentKind


def usage(session="root", thread="root", response="r", accounting="response", **raw):
    return NativeEvent(
        kind="usage",
        session_id=session,
        thread_id=thread,
        turn_id="turn",
        root_turn_id="root-turn",
        source="fixture",
        line=1,
        data={"response_id": response, "accounting": accounting, "usage": raw},
    )


def test_response_dedupe_and_cumulative_supersession_are_per_thread():
    ledger = UsageLedger(AgentKind.CODEX)
    legacy = usage(
        response=None,
        accounting="cumulative",
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=4,
    )
    ledger.add(legacy)
    ledger.add(usage(thread="child", response=None, accounting="cumulative", input_tokens=30))
    keyed = usage(
        input_tokens=100,
        cached_input_tokens=20,
        cache_write_input_tokens=10,
        output_tokens=5,
        reasoning_output_tokens=2,
    )
    ledger.add(keyed)
    ledger.add(keyed)
    records = ledger.snapshot()
    assert [record.accounting for record in records] == ["superseded", "cumulative", "response"]
    assert records[-1].usage.uncached_input == 70
    assert records[-1].usage.reasoning == 2
    assert records[-1].root_turn_id == "root-turn"
    assert records[1].usage.uncached_input is None


def test_unknown_and_invalid_usage_remain_unknown():
    ledger = UsageLedger(AgentKind.CODEX)
    ledger.add(usage(response=None, input_tokens=2, cached_input_tokens=3))
    record = ledger.snapshot()[0]
    assert record.accounting == "unknown"
    assert record.usage is None
    assert record.raw == {"input_tokens": 2, "cached_input_tokens": 3}


def test_claude_input_is_already_uncached():
    ledger = UsageLedger(AgentKind.CLAUDE)
    ledger.add(
        usage(
            input_tokens=2,
            cache_read_input_tokens=50,
            cache_creation_input_tokens=10,
            output_tokens=20,
        )
    )
    assert ledger.snapshot()[0].usage.uncached_input == 2
