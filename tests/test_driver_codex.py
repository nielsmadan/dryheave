import json
from collections import Counter
from pathlib import Path

import pytest

from dryheave.drivers.codex import CodexAdapter
from dryheave.drivers.usage import UsageLedger
from dryheave.errors import InputError
from dryheave.models import AgentKind


def test_actual_shape_retains_native_turn_and_response_identity():
    adapter = CodexAdapter()
    events = [
        event
        for line in Path("tests/fixtures/codex-recorded.jsonl").read_text().splitlines()
        for event in adapter.feed(json.loads(line))
    ]
    accepted = next(event for event in events if event.kind == "accepted")
    assert (accepted.session_id, accepted.turn_id, accepted.native_id, accepted.text) == (
        "fixture-id-1",
        "fixture-id-3",
        "fixture-id-4",
        "Add a greeting command.",
    )
    usage = next(
        event for event in events if event.kind == "usage" and event.data.get("response_id")
    )
    assert (usage.thread_id, usage.root_turn_id, usage.data["response_id"]) == (
        "fixture-id-1",
        "fixture-id-3",
        "fixture-id-8",
    )
    completed = next(event for event in events if event.kind == "completed")
    assert completed.turn_id == accepted.turn_id
    assert next(event for event in events if event.kind == "assistant").data == {
        "phase": "commentary"
    }


def test_legacy_events_bind_to_active_native_turn_but_response_echo_is_ignored():
    adapter = CodexAdapter()
    adapter.feed({"type": "session_meta", "payload": {"id": "root"}})
    adapter.feed({"type": "event_msg", "payload": {"type": "turn_started", "turn_id": "turn"}})
    accepted = adapter.feed(
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}}
    )[0]
    assert (accepted.kind, accepted.turn_id) == ("accepted", "turn")
    assert (
        adapter.feed(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
            }
        )
        == ()
    )
    assert (
        adapter.feed({"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "turn"}})[
            0
        ].kind
        == "aborted"
    )


def test_0_162_1_probe_session_matches_turn_lifecycle_and_metadata():
    adapter = CodexAdapter()
    lines = Path("tests/fixtures/codex-0.162.1-probe.jsonl").read_text().splitlines()
    events = [
        event.model_copy(update={"source": "rollout.jsonl", "line": number})
        for number, line in enumerate(lines, 1)
        for event in adapter.feed(json.loads(line))
    ]
    assert Counter(event.kind for event in events) == {
        "usage": 4,
        "metadata": 2,
        "tool": 2,
        "started": 1,
        "accepted": 1,
        "assistant": 1,
        "completed": 1,
    }
    session_metadata = next(
        event for event in events if event.kind == "metadata" and event.turn_id is None
    )
    assert session_metadata.data["cli_version"] == "0.162.1"
    turn_metadata = next(event for event in events if event.kind == "metadata" and event.turn_id)
    assert (turn_metadata.data["model"], turn_metadata.data["effort"]) == ("gpt-5.6-luna", "low")
    started = next(event for event in events if event.kind == "started")
    accepted = next(event for event in events if event.kind == "accepted")
    assert accepted.turn_id == started.turn_id
    assert accepted.text == (
        "Run exactly this shell command and nothing else: mkdir -p "
        "/home/user/Library/Caches/dryheave-approval-probe && date > "
        "/home/user/Library/Caches/dryheave-approval-probe/ok. Then reply with the single word DONE."
    )
    usage = next(
        event for event in events if event.kind == "usage" and event.data.get("response_id")
    )
    assert usage.data["response_id"] == "resp_synthetic0005"
    assert usage.data["usage"]["total_tokens"] == 31874
    completed = next(event for event in events if event.kind == "completed")
    assert (completed.turn_id, completed.text) == (started.turn_id, "DONE")
    ledger = UsageLedger(AgentKind.CODEX)
    for event in events:
        if event.kind == "usage":
            ledger.add(event)
    records = ledger.snapshot()
    assert [record.accounting for record in records] == [
        "response",
        "superseded",
        "response",
        "superseded",
    ]
    first = records[0].usage
    assert first is not None
    assert (first.uncached_input, first.cache_read, first.cache_write, first.output) == (
        20759,
        11008,
        0,
        107,
    )


def test_child_metadata_and_identity_conflict():
    adapter = CodexAdapter()
    event = adapter.feed(
        {
            "type": "session_meta",
            "payload": {"id": "child", "parent_thread_id": "root", "cwd": "/fixture"},
        }
    )[0]
    assert event.parent_session_id == "root"
    with pytest.raises(InputError, match="identity"):
        adapter.feed({"type": "session_meta", "payload": {"id": "different"}})


def test_forked_child_skips_the_copied_parent_history():
    adapter = CodexAdapter()
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "forked_from_id": "root",
                "parent_thread_id": "root",
                "subagent_history_start_ordinal": 4,
            },
        },
        {"type": "session_meta", "payload": {"id": "root", "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "root-turn"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "root-turn"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "child-turn"}},
    ]
    events = []
    for record in records:
        if adapter.skips_next():
            adapter.skip()
            continue
        events.extend(adapter.feed(record))
    assert [(event.kind, event.session_id, event.turn_id) for event in events] == [
        ("metadata", "child", None),
        ("started", "child", "child-turn"),
    ]
    assert adapter.parent_session_id == "root"


def test_later_session_meta_does_not_start_a_history_skip():
    adapter = CodexAdapter()
    records = [
        {"type": "session_meta", "payload": {"id": "root"}},
        {
            "type": "session_meta",
            "payload": {"id": "root", "subagent_history_start_ordinal": 4},
        },
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
    ]
    events = [event for record in records for event in adapter.feed(record)]
    assert [event.kind for event in events] == ["metadata", "metadata", "started"]
    assert adapter.skips_next() is False
