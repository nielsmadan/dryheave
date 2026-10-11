import json
from pathlib import Path

import pytest

from dryheave.commits import scan_sessions
from dryheave.errors import InputError, LimitError
from dryheave.logs import claude, codex, droid
from dryheave.logs.base import (
    ImportLimits,
    Session,
    candidates,
    discover,
    recorded_baseline,
    recorded_cwd,
)
from dryheave.logs.service import import_session
from dryheave.models import AgentKind, ObjectKind
from dryheave.storage import ObjectStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "preferred,responses,remaining",
    [
        (
            [("same", "one", "n1"), ("other", "two", "n2")],
            [("same", "one", "n2"), ("different", "three", "n1")],
            [1],
        ),
        (
            [("same", "one", None), ("same", "two", None)],
            [("same", "three", None), ("same", "two", None)],
            [0],
        ),
        (
            [("same", None, None), ("same", "one", None)],
            [("same", "two", None), ("same", None, None), ("same", "one", None)],
            [2],
        ),
        ([("same", "one", "native")], [("changed text", "other", "native")], []),
        ([("same", "one", "n1")], [("same", "one", "different")], []),
    ],
)
def test_deduplication_preserves_earliest_compatible_candidates(preferred, responses, remaining):
    from dryheave.logs.base import LogEvent

    events = []
    for source, items in (("event_msg", preferred), ("response_item", responses)):
        for index, (text, turn, native) in enumerate(items):
            events.append(
                (
                    source,
                    LogEvent(
                        event_id=f"{source}-{index}",
                        source_line=len(events) + 1,
                        kind="user",
                        text=text,
                        turn_id=turn,
                        native_id=native,
                    ),
                )
            )
    assert [event.event_id for event in codex._dedupe(events)] == [
        *(f"event_msg-{index}" for index in range(len(preferred))),
        *(f"response_item-{index}" for index in remaining),
    ]


def test_large_repeated_message_deduplication_has_bounded_candidate_work(monkeypatch):
    from dryheave.logs.base import LogEvent

    count = 4000
    events = [
        (
            source,
            LogEvent(
                event_id=f"{source}-{index}",
                source_line=index + 1,
                kind="user",
                text="Repeated message",
                turn_id="turn",
            ),
        )
        for source in ("event_msg", "response_item")
        for index in range(count)
    ]
    get = LogEvent.__getattribute__
    observations = 0

    def observe(event, name):
        nonlocal observations
        if name in {"event_id", "kind", "text", "native_id", "turn_id"}:
            observations += 1
        return get(event, name)

    with monkeypatch.context() as context:
        context.setattr(LogEvent, "__getattribute__", observe)
        result = codex._dedupe(events)
    assert [event.event_id for event in result] == [f"event_msg-{index}" for index in range(count)]
    assert observations <= 20 * len(events)


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


def test_real_droid_shapes() -> None:
    session = droid.parse(FIXTURES / "droid-recorded.jsonl")
    assert (session.agent, session.source_id, session.source_version) == (
        AgentKind.DROID,
        "fixture-session",
        "2",
    )
    assert session.metadata["cwd"] == "/fixture/repository"
    assert session.metadata["title"] == "Add a greeting command"
    assert [(event.event_id, event.kind) for event in session.events] == [
        ("e000002-0", "user"),
        ("e000003-0", "user"),
        ("e000004-0", "user"),
        ("e000005-1", "assistant"),
        ("e000005-2", "tool_call"),
        ("e000006-0", "tool_result"),
        ("e000007", "metadata"),
        ("e000008-0", "user"),
        ("e000009-0", "user"),
        ("e000010-0", "user"),
        ("e000011", "lifecycle"),
    ]
    assert [event.text for event in session.events if event.kind == "assistant"] == [
        "Which punctuation should the greeting use?"
    ]
    assert "Private reasoning" not in "".join(event.text for event in session.events)
    assert [event.data["name"] for event in session.events if event.kind == "tool_call"] == ["Read"]
    assert [event.text for event in session.events if event.kind == "tool_result"] == [
        "print('Hello')\n"
    ]
    assert [event.turn_id for event in session.events if event.kind == "lifecycle"] == [
        "fixture-turn-1"
    ]
    assert session.usage == ()


def test_droid_harness_user_events_keep_a_recognizable_wrapper() -> None:
    session = droid.parse(FIXTURES / "droid-recorded.jsonl")
    assert [
        event.text for event in session.events if event.kind == "user" and event.source_line < 5
    ] == [
        "<harness-event>SessionStart</harness-event>",
        "<harness-event><system-reminder>\nCurrent date: 2026-09-20.\n</system-reminder>"
        "</harness-event>",
        "Add a greeting command.",
    ]
    hook = session.events[0]
    assert hook.data["hookEventName"] == "SessionStart"
    assert hook.data["visibility"] == "user_only"
    assert session.events[7].text == (
        '<harness-event>Skill "code-review" activated: --quick</harness-event>'
    )


def test_droid_records_no_git_baseline(tmp_path: Path, store: ObjectStore) -> None:
    source = FIXTURES / "droid-recorded.jsonl"
    session = droid.parse(source)
    assert recorded_baseline(session) is None
    assert recorded_cwd(session) == "/fixture/repository"
    assert "git" not in session.metadata
    assert "baseline_unknown" in {warning.code for warning in session.warnings}
    (tmp_path / "droid-recorded.jsonl").write_bytes(source.read_bytes())
    scan = scan_sessions(tmp_path, AgentKind.DROID, ImportLimits())
    assert [(item.source_id, item.cwd, item.baseline_commit) for item in scan.sessions] == [
        ("fixture-session", "/fixture/repository", None)
    ]
    assert scan.sessions_with_baseline == 0
    identifier = import_session(store, source, AgentKind.DROID)
    assert store.load(identifier, Session, kind=ObjectKind.SESSION).agent == AgentKind.DROID


def test_droid_malformed_and_truncated_records_warn(tmp_path: Path) -> None:
    session = droid.parse(FIXTURES / "droid-recorded.jsonl")
    assert [
        (warning.code, warning.line)
        for warning in session.warnings
        if warning.code in {"malformed_line", "truncated_tail"}
    ] == [("malformed_line", 12), ("truncated_tail", 12)]
    path = write_log(
        tmp_path / "droid.jsonl",
        [
            {"type": "session_start", "id": "first", "cwd": "/one"},
            {"type": "session_start", "id": "second", "cwd": "/two"},
            {"type": "compaction_state", "summaryText": "earlier work", "removedCount": 4},
        ],
    )
    session = droid.parse(path)
    assert {"ambiguous_session", "ambiguous_cwd", "compacted"} <= {
        warning.code for warning in session.warnings
    }


def test_droid_log_without_session_start_reports_missing_metadata(tmp_path: Path) -> None:
    session = droid.parse(write_log(tmp_path / "droid.jsonl", [{"type": "todo_state", "id": "t"}]))
    assert session.source_id is None
    assert "missing_metadata" in {warning.code for warning in session.warnings}


def test_droid_ignores_unknown_records_and_keeps_a_string_version(tmp_path: Path) -> None:
    session = droid.parse(
        write_log(
            tmp_path / "droid.jsonl",
            [
                {"type": "session_start", "id": "fixture", "version": "3.1.0"},
                {"type": "unknown_record", "payload": {"ignored": True}},
                {"type": "message", "id": "m1", "message": {"role": "system", "content": "setup"}},
                {
                    "type": "message",
                    "id": "m2",
                    "message": {"role": "user", "content": "Do the work."},
                },
            ],
        )
    )
    assert session.source_version == "3.1.0"
    assert [(event.kind, event.text) for event in session.events] == [("user", "Do the work.")]
