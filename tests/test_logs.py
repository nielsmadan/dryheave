import json
from pathlib import Path

import pytest

from dryheave.errors import InputError, LimitError
from dryheave.logs import claude, codex
from dryheave.logs.base import ImportLimits, candidates, discover
from dryheave.logs.service import import_session
from dryheave.models import AgentKind
from dryheave.storage import ObjectStore

FIXTURES = Path(__file__).parent / "fixtures"


def write_log(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_real_codex_paginated_shapes() -> None:
    session = codex.parse(FIXTURES / "codex-recorded.jsonl")
    assert session.source_version == "0.153.4"
    users = [event.text for event in session.events if event.kind == "user"]
    assert users == [
        "Add a greeting command.",
        "Use a comma after Hello.",
        "Change the command name to greet.",
    ]
    assert [(event.event_id, event.kind) for event in session.events] == [
        ("e000002", "lifecycle"),
        ("e000003", "user"),
        ("e000004", "assistant"),
        ("e000005", "tool_call"),
        ("e000007", "tool_call"),
        ("e000008", "tool_result"),
        ("e000011", "lifecycle"),
        ("e000012", "user"),
        ("e000013", "user"),
    ]
    assert [
        (event.native_id, event.text) for event in session.events if event.kind == "assistant"
    ] == [
        ("fixture-id-5", "Which punctuation should the greeting use?"),
    ]
    assert {"tool_call", "tool_result", "assistant", "lifecycle"} <= {
        event.kind for event in session.events
    }
    assert {"task_started", "task_complete"} <= {
        event.data.get("type") for event in session.events if event.kind == "lifecycle"
    }
    assert session.usage[0].usage.uncached_input == 14070
    assert session.usage[1].accounting == "superseded"
    assert {"baseline_unknown", "compacted"} <= {warning.code for warning in session.warnings}
    assert "curator" in candidates(session)[0].issues[0]


def test_paginated_assistant_text_without_response_fallback(tmp_path: Path) -> None:
    records = [
        json.loads(line) for line in (FIXTURES / "codex-recorded.jsonl").read_text().splitlines()
    ]
    records = [record for record in records if record["type"] != "response_item"]
    session = codex.parse(write_log(tmp_path / "paginated.jsonl", records))
    assert [
        (event.kind, event.text) for event in session.events if event.kind in {"user", "assistant"}
    ] == [
        ("user", "Add a greeting command."),
        ("assistant", "Which punctuation should the greeting use?"),
        ("user", "Use a comma after Hello."),
        ("user", "Change the command name to greet."),
    ]


def test_real_claude_shapes() -> None:
    session = claude.parse(FIXTURES / "claude-recorded.jsonl")
    assert session.source_version == "2.1.263"
    assert {"user", "assistant", "tool_call", "tool_result", "lifecycle"} <= {
        event.kind for event in session.events
    }
    assert session.usage[0].usage.cache_write is not None
    assert session.metadata["cost_state"]["totalCostUSD"] == 0.6220835
    assert "baseline_unknown" in {warning.code for warning in session.warnings}


def test_codex_duplicate_representations_and_distinct_turns(tmp_path: Path) -> None:
    def item(turn: str, native: str) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": turn,
                "item": {
                    "id": native,
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "Do it"}],
                },
            },
        }

    records = [
        item("t1", "n1"),
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "n1",
                "role": "user",
                "content": [{"type": "input_text", "text": "Do it"}],
            },
        },
        item("t2", "n2"),
    ]
    session = codex.parse(write_log(tmp_path / "log.jsonl", records))
    assert [event.turn_id for event in session.events] == ["t1", "t2"]


def test_legacy_codex_and_unknown_usage(tmp_path: Path) -> None:
    records = [
        {"type": "session_meta", "payload": {"id": "session", "git": {"commit_hash": "a" * 40}}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Question"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}},
            },
        },
    ]
    session = codex.parse(write_log(tmp_path / "log.jsonl", records))
    assert session.events[0].text == "Question"
    assert session.usage[0].usage.uncached_input is None
    assert session.usage[0].accounting == "cumulative"
    assert session.warnings == ()


def test_streaming_claude_usage_dedupes_by_message(tmp_path: Path) -> None:
    records = [
        {
            "type": "assistant",
            "uuid": f"u{count}",
            "message": {
                "id": "same-message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": count,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                },
            },
        }
        for count in (1, 9)
    ]
    session = claude.parse(write_log(tmp_path / "log.jsonl", records))
    assert len(session.events) == 1
    assert len(session.usage) == 1
    assert session.usage[0].usage.output == 9


def test_malformed_truncated_and_ambiguous_metadata(tmp_path: Path) -> None:
    path = write_log(
        tmp_path / "log.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "first"}},
            {"type": "session_meta", "payload": {"id": "second"}},
        ],
    )
    with path.open("ab") as target:
        target.write(b'{invalid}\n{"unfinished":')
    session = codex.parse(path)
    assert [warning.code for warning in session.warnings].count("malformed_line") == 2
    assert {"ambiguous_session", "truncated_tail"} <= {warning.code for warning in session.warnings}


def test_scan_and_import_limits(tmp_path: Path, store: ObjectStore) -> None:
    path = write_log(tmp_path / "log.jsonl", [{"type": "unknown"}] * 3)
    with pytest.raises(LimitError):
        codex.parse(path, ImportLimits(max_events=2))
    with pytest.raises(LimitError):
        codex.parse(path, ImportLimits(max_bytes=5))
    (tmp_path / "linked.jsonl").symlink_to(path)
    assert discover(tmp_path, ImportLimits()) == (path,)
    with pytest.raises(InputError, match="No supported"):
        import_session(store, path, AgentKind.CODEX)


def test_legacy_duplicate_with_missing_turn_id(tmp_path: Path) -> None:
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Do it"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "first"},
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Do it"}},
    ]
    session = codex.parse(write_log(tmp_path / "log.jsonl", records))
    assert [(event.kind, event.text) for event in session.events] == [("user", "Do it")]
