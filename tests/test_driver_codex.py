import json
from pathlib import Path

import pytest

from dryheave.drivers.codex import CodexAdapter
from dryheave.errors import InputError


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
