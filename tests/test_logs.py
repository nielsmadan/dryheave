import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from conftest import write_opencode_current_database, write_opencode_database
from dryheave.commits import scan_sessions
from dryheave.errors import InputError, LimitError
from dryheave.logs import claude, codex, droid, opencode, pi
from dryheave.logs.base import (
    ImportLimits,
    Session,
    candidates,
    discover,
    recorded_baseline,
    recorded_cwd,
    source_hash,
)
from dryheave.logs.service import discover_sources, import_session
from dryheave.mining import user_message_profile
from dryheave.models import AgentKind, ObjectKind
from dryheave.storage import ObjectStore

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC_READ = (
    "<harness-event>Called the Read tool with the following input: "
    '{"filePath":"/fixture/repository/greet.py"}</harness-event>'
)
PI_SKILL = (
    '<skill name="code-review" location="/fixture/skills/code-review/SKILL.md">\n'
    "Review the change.\n</skill>"
)


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


def _claude_usage_record(tmp_path: Path, usage: dict, name: str = "log.jsonl") -> Session:
    records = [
        {
            "type": "assistant",
            "uuid": "u1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "usage": usage,
            },
        }
    ]
    return claude.parse(write_log(tmp_path / name, records))


def test_claude_log_import_extracts_the_1h_cache_write_split(tmp_path: Path) -> None:
    session = _claude_usage_record(
        tmp_path,
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 5,
            "output_tokens": 1,
            "cache_creation": {"ephemeral_1h_input_tokens": 5},
        },
    )
    assert session.usage[0].usage.cache_write_1h == 5
    assert session.parser_version == "2"


def test_claude_log_import_drops_only_the_split_when_1h_exceeds_cache_write(
    tmp_path: Path,
) -> None:
    session = _claude_usage_record(
        tmp_path,
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 5,
            "output_tokens": 1,
            "cache_creation": {"ephemeral_1h_input_tokens": 99},
        },
    )
    assert len(session.usage) == 1
    assert session.usage[0].usage.cache_write_1h is None
    assert session.usage[0].usage.cache_write == 5
    assert "invalid_usage" not in {warning.code for warning in session.warnings}


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
        (
            "<harness-event><system-reminder>\nCurrent date: 2026-09-20.\n</system-reminder>"
            "</harness-event>"
        ),
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


def test_real_pi_shapes() -> None:
    session = pi.parse(FIXTURES / "pi-recorded.jsonl")
    assert (session.agent, session.source_id, session.source_version) == (
        AgentKind.PI,
        "fixture-session",
        "3",
    )
    assert session.metadata == {"cwd": "/fixture/repository", "title": "Add a greeting command"}
    assert [(event.event_id, event.kind) for event in session.events] == [
        ("e000002", "metadata"),
        ("e000003", "metadata"),
        ("e000005-0", "user"),
        ("e000006-1", "assistant"),
        ("e000006-2", "tool_call"),
        ("e000007-0", "tool_result"),
        ("e000008", "user"),
        ("e000009-0", "user"),
        ("e000010-0", "user"),
        ("e000011-0", "tool_call"),
        ("e000011-1", "tool_result"),
        ("e000012-stop", "lifecycle"),
        ("e000015-0", "user"),
    ]
    events = {event.event_id: event for event in session.events}
    assert events["e000002"].data == {
        "type": "model_change",
        "provider": "fixture-provider",
        "modelId": "fixture-model",
    }
    assert events["e000006-1"].text == "Which punctuation should the greeting use?"
    assert events["e000006-2"].data == {
        "name": "read",
        "id": "fixture-call-1",
        "arguments": {"path": "greet.py"},
    }
    assert (events["e000007-0"].text, events["e000007-0"].data["toolCallId"]) == (
        "print('Hello')\n",
        "fixture-call-1",
    )
    assert events["e000011-0"].data == {"initiator": "user", "command": "git status --short"}
    assert (events["e000011-1"].text, events["e000011-1"].data["exitCode"]) == (" M greet.py\n", 0)
    assert events["e000012-stop"].data == {
        "stopReason": "aborted",
        "errorMessage": "Request was aborted",
    }
    assert events["e000005-0"].native_id == "a4"
    assert events["e000005-0"].timestamp == "2026-10-01T10:00:04.000Z"
    assert "Private reasoning" not in "".join(event.text for event in session.events)


def test_pi_harness_user_events_keep_a_recognizable_wrapper() -> None:
    session = pi.parse(FIXTURES / "pi-recorded.jsonl")
    assert [event.text for event in session.events if event.kind == "user"] == [
        "Add a greeting command.",
        "<harness-event>Subagent finished its review.</harness-event>",
        PI_SKILL,
        "Use a comma after Hello.",
        "Change the command name to greet.",
    ]
    profile = user_message_profile(session)
    assert (profile.genuine, profile.harness_injected, profile.unclassified) == (3, 2, 0)


def test_pi_usage_records_each_response() -> None:
    session = pi.parse(FIXTURES / "pi-recorded.jsonl")
    assert [(item.identity, item.accounting) for item in session.usage] == [
        ("fixture-response-1", "response"),
        ("a11", "response"),
    ]
    first = session.usage[0].usage
    assert (
        first.uncached_input,
        first.cache_read,
        first.cache_write,
        first.output,
        first.reasoning,
    ) == (12, 100, 50, 30, 10)


def test_pi_usage_keeps_the_last_record_per_response_and_marks_unidentified_usage(
    tmp_path: Path,
) -> None:
    session = pi.parse(
        write_log(
            tmp_path / "pi.jsonl",
            [
                {"type": "session", "version": 3, "id": "s", "cwd": "/r"},
                {
                    "type": "message",
                    "id": "m1",
                    "parentId": None,
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "responseId": "response-1",
                        "stopReason": "error",
                        "errorMessage": "Overloaded",
                        "usage": {"input": 1, "output": 2},
                    },
                },
                {
                    "type": "message",
                    "id": "m2",
                    "parentId": "m1",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "responseId": "response-1",
                        "stopReason": "length",
                        "usage": {"input": 3, "output": 4},
                    },
                },
                {"type": "usage", "id": "u1", "parentId": "m2", "usage": {"input": 5, "output": 6}},
                {
                    "type": "message",
                    "parentId": "u1",
                    "message": {"role": "assistant", "content": [], "usage": {"input": 7}},
                },
                {
                    "type": "message",
                    "id": "m4",
                    "message": {
                        "role": "custom",
                        "customType": "reminder",
                        "display": False,
                        "content": "Remember the tests.",
                    },
                },
            ],
        )
    )
    assert [
        (item.identity, item.source_line, item.accounting, item.usage.uncached_input)
        for item in session.usage
    ] == [
        ("response-1", 3, "response", 3),
        ("u1", 4, "response", 5),
        ("unknown-5", 5, "superseded", 7),
    ]
    assert [(warning.code, warning.line) for warning in session.warnings][:1] == [
        ("usage_identity_missing", 5)
    ]
    assert [(event.event_id, event.text, event.data) for event in session.events] == [
        ("e000002-stop", "", {"stopReason": "error", "errorMessage": "Overloaded"}),
        ("e000003-stop", "", {"stopReason": "length"}),
        (
            "e000006-0",
            "<harness-event>Remember the tests.</harness-event>",
            {"customType": "reminder", "display": False},
        ),
    ]


def test_pi_records_branches_compaction_and_no_git_baseline() -> None:
    session = pi.parse(FIXTURES / "pi-recorded.jsonl")
    assert [
        (warning.code, warning.line)
        for warning in session.warnings
        if warning.code in {"branched", "compacted"}
    ] == [("compacted", 14), ("branched", 15)]
    assert recorded_baseline(session) is None
    assert recorded_cwd(session) == "/fixture/repository"
    assert "baseline_unknown" in {warning.code for warning in session.warnings}


def test_pi_header_conflicts_and_malformed_records_warn(tmp_path: Path) -> None:
    path = write_log(
        tmp_path / "pi.jsonl",
        [
            {"type": "session", "version": 3, "id": "first", "cwd": "/one"},
            {"type": "session", "version": 3, "id": "second", "cwd": "/two"},
            {
                "type": "message",
                "id": "m1",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": [],
                    "usage": {"input": 1, "output": 2, "reasoning": 5},
                },
            },
        ],
    )
    with path.open("ab") as target:
        target.write(b'{invalid}\n{"type": "message"')
    session = pi.parse(path)
    assert [
        (warning.code, warning.line)
        for warning in session.warnings
        if warning.code != "baseline_unknown"
    ] == [
        ("malformed_line", 4),
        ("malformed_line", 5),
        ("truncated_tail", 5),
        ("ambiguous_session", 2),
        ("ambiguous_cwd", 2),
        ("invalid_usage", 3),
    ]
    assert session.usage == ()


def test_pi_log_without_header_reports_missing_metadata(tmp_path: Path) -> None:
    session = pi.parse(
        write_log(
            tmp_path / "pi.jsonl",
            [{"type": "message", "id": "m1", "message": {"role": "user", "content": "Do it."}}],
        )
    )
    assert session.source_id is None
    assert [(event.kind, event.text) for event in session.events] == [("user", "Do it.")]
    assert "missing_metadata" in {warning.code for warning in session.warnings}


def test_pi_scan_and_import(tmp_path: Path, store: ObjectStore) -> None:
    source = FIXTURES / "pi-recorded.jsonl"
    (tmp_path / "pi-recorded.jsonl").write_bytes(source.read_bytes())
    scan = scan_sessions(tmp_path, AgentKind.PI, ImportLimits())
    assert [(item.source_id, item.cwd, item.baseline_commit) for item in scan.sessions] == [
        ("fixture-session", "/fixture/repository", None)
    ]
    identifier = import_session(store, source, AgentKind.PI)
    assert store.load(identifier, Session, kind=ObjectKind.SESSION).agent == AgentKind.PI


@pytest.fixture
def opencode_db(tmp_path: Path) -> Path:
    return write_opencode_database(tmp_path / "opencode.db", FIXTURES / "opencode-recorded.json")


def test_real_opencode_shapes(opencode_db: Path) -> None:
    session = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    assert (session.agent, session.source_id, session.source_version) == (
        AgentKind.OPENCODE,
        "ses_fixture",
        "1.19.0",
    )
    assert session.parent_session_id is None
    assert session.source_path_hash == source_hash(opencode_db)
    assert session.metadata == {"cwd": "/fixture/repository", "title": "Add a greeting command"}
    assert [(event.event_id, event.kind) for event in session.events] == [
        ("e000001", "user"),
        ("e000002", "user"),
        ("e000003", "metadata"),
        ("e000006", "assistant"),
        ("e000007-0", "tool_call"),
        ("e000007-1", "tool_result"),
        ("e000009", "user"),
        ("e000010-0", "tool_call"),
        ("e000010-error", "lifecycle"),
        ("e000011", "lifecycle"),
        ("e000012", "user"),
    ]
    events = {event.event_id: event for event in session.events}
    assert events["e000006"].text == "Which punctuation should the greeting use?"
    assert events["e000007-0"].data == {
        "tool": "read",
        "callID": "fixture-call-1",
        "input": {"filePath": "/fixture/repository/greet.py"},
        "status": "completed",
    }
    assert events["e000007-1"].text == "print('Hello')\n"
    assert events["e000010-0"].data["status"] == "running"
    assert events["e000010-error"].data == {"type": "error", "name": "MessageAbortedError"}
    assert events["e000003"].data == {
        "type": "file",
        "mime": "text/x-python",
        "filename": "greet.py",
    }
    assert events["e000001"].native_id == "prt_001"
    assert events["e000006"].timestamp == "2026-10-01T10:00:02.300Z"
    assert "Private reasoning" not in "".join(event.text for event in session.events)
    assert [
        (warning.code, warning.line)
        for warning in session.warnings
        if warning.code != "baseline_unknown"
    ] == [("compacted", 11)]


def test_opencode_synthetic_user_text_is_harness_injected(opencode_db: Path) -> None:
    session = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    assert [event.text for event in session.events if event.kind == "user"] == [
        "Add a greeting command.",
        SYNTHETIC_READ,
        "Use a comma after Hello.",
        "Change the command name to greet.",
    ]
    profile = user_message_profile(session)
    assert (profile.genuine, profile.harness_injected, profile.unclassified) == (3, 1, 0)


def test_opencode_usage_adds_reasoning_to_output(opencode_db: Path) -> None:
    session = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    assert [(item.identity, item.accounting) for item in session.usage] == [("msg_002", "response")]
    usage = session.usage[0].usage
    assert (
        usage.uncached_input,
        usage.cache_read,
        usage.cache_write,
        usage.output,
        usage.reasoning,
    ) == (12, 100, 50, 30, 10)


def test_opencode_discovery_lists_sessions_of_databases_directly_in_the_root(
    opencode_db: Path, tmp_path: Path
) -> None:
    with pytest.raises(LimitError, match="session limit"):
        opencode.discover(tmp_path, ImportLimits(max_files=1))
    channel = write_opencode_database(
        tmp_path / "opencode-beta.db", FIXTURES / "opencode-recorded.json"
    )
    (tmp_path / "snapshot").mkdir()
    write_opencode_database(
        tmp_path / "snapshot" / "opencode.db", FIXTURES / "opencode-recorded.json"
    )
    (tmp_path / "other.db").write_bytes(b"")
    (tmp_path / "opencode-linked.db").symlink_to(opencode_db)
    assert discover_sources(AgentKind.OPENCODE, tmp_path, ImportLimits()) == (
        opencode.source_path(channel, "ses_fixture"),
        opencode.source_path(channel, "ses_child"),
        opencode.source_path(opencode_db, "ses_fixture"),
        opencode.source_path(opencode_db, "ses_child"),
    )
    with pytest.raises(LimitError, match="entry limit"):
        opencode.discover(tmp_path, ImportLimits(max_files=4))


def test_opencode_scan_skips_a_database_it_cannot_read(opencode_db: Path, tmp_path: Path) -> None:
    unreadable = tmp_path / "opencode-old.db"
    unreadable.write_bytes(b"not a database" * 100)
    scan = scan_sessions(tmp_path, AgentKind.OPENCODE, ImportLimits())
    assert [item.source_id for item in scan.sessions] == ["ses_fixture", "ses_child"]
    assert [(item.path, item.reason) for item in scan.skipped] == [
        (str(unreadable), "OpenCode database could not be read: file is not a database")
    ]


def test_opencode_scan_reports_the_recorded_parent(opencode_db: Path) -> None:
    scan = scan_sessions(opencode_db.parent, AgentKind.OPENCODE, ImportLimits())
    assert [(item.source_id, item.parent_session_id, item.cwd) for item in scan.sessions] == [
        ("ses_fixture", None, "/fixture/repository"),
        ("ses_child", "ses_fixture", "/fixture/repository"),
    ]
    assert scan.sessions_with_baseline == 0


def test_opencode_reads_the_database_without_changing_it(opencode_db: Path) -> None:
    before = hashlib.sha256(opencode_db.read_bytes()).hexdigest()
    first = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    second = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    assert hashlib.sha256(opencode_db.read_bytes()).hexdigest() == before
    assert first.source_content_hash == second.source_content_hash
    child = opencode.parse(opencode.source_path(opencode_db, "ses_child"))
    assert child.source_content_hash != first.source_content_hash


def test_opencode_source_errors(opencode_db: Path, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="DATABASE#SESSION_ID"):
        opencode.parse(opencode_db)
    with pytest.raises(InputError, match="ses_missing is not in"):
        opencode.parse(opencode.source_path(opencode_db, "ses_missing"))
    with pytest.raises(InputError, match="regular file"):
        opencode.parse(opencode.source_path(tmp_path / "absent.db", "ses_fixture"))
    empty = tmp_path / "opencode-empty.db"
    with closing(sqlite3.connect(empty)) as connection:
        connection.execute("CREATE TABLE session (id text)")
    with pytest.raises(InputError, match="storage version is not supported"):
        opencode.parse(opencode.source_path(empty, "ses_fixture"))
    corrupt = tmp_path / "opencode-corrupt.db"
    corrupt.write_bytes(b"not a database" * 100)
    with pytest.raises(InputError, match="could not be read"):
        opencode.parse(opencode.source_path(corrupt, "ses_fixture"))


def test_opencode_limits_bound_the_session_rows(opencode_db: Path) -> None:
    source = opencode.source_path(opencode_db, "ses_fixture")
    with pytest.raises(LimitError, match="event limit"):
        opencode.parse(source, ImportLimits(max_events=11))
    with pytest.raises(LimitError, match="byte limit"):
        opencode.parse(source, ImportLimits(max_bytes=100))
    assert len(opencode.parse(source, ImportLimits(max_events=12)).events) == 11
    with closing(sqlite3.connect(opencode_db)) as connection:
        (stored,) = connection.execute(
            "SELECT (SELECT sum(length(data)) FROM message WHERE session_id = 'ses_fixture')"
            " + (SELECT sum(length(data)) FROM part WHERE session_id = 'ses_fixture')"
        ).fetchone()
    assert opencode.parse(source, ImportLimits(max_bytes=stored)).source_id == "ses_fixture"
    with pytest.raises(LimitError, match="byte limit"):
        opencode.parse(source, ImportLimits(max_bytes=stored - 1))


def test_opencode_malformed_rows_warn(opencode_db: Path) -> None:
    with closing(sqlite3.connect(opencode_db)) as connection, connection:
        connection.execute("UPDATE part SET data = '{invalid' WHERE id = 'prt_009'")
        connection.execute("UPDATE message SET data = '{invalid' WHERE id = 'msg_006'")
    session = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    assert [
        (warning.code, warning.line)
        for warning in session.warnings
        if warning.code == "malformed_line"
    ] == [("malformed_line", 9), ("malformed_line", 12)]
    assert [event.text for event in session.events if event.kind == "user"] == [
        "Add a greeting command.",
        SYNTHETIC_READ,
    ]


def test_opencode_import_stores_the_session(opencode_db: Path, store: ObjectStore) -> None:
    identifier = import_session(
        store, opencode.source_path(opencode_db, "ses_child"), AgentKind.OPENCODE
    )
    session = store.load(identifier, Session, kind=ObjectKind.SESSION)
    assert (session.agent, session.parent_session_id) == (AgentKind.OPENCODE, "ses_fixture")
    assert [event.text for event in session.events] == ["Review the greeting change."]


def test_opencode_reads_a_wal_database_without_writing_beside_it(tmp_path: Path) -> None:
    database = write_opencode_database(
        tmp_path / "opencode.db", FIXTURES / "opencode-recorded.json"
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert [path.name for path in tmp_path.iterdir()] == ["opencode.db"]
    assert opencode.discover(tmp_path, ImportLimits())
    assert opencode.parse(opencode.source_path(database, "ses_fixture")).source_id == "ses_fixture"
    assert [path.name for path in tmp_path.iterdir()] == ["opencode.db"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("UPDATE session SET title = 'Unsaved title'")
        writer.commit()
        with pytest.raises(InputError, match="quit OpenCode"):
            opencode.parse(opencode.source_path(database, "ses_fixture"))


def test_opencode_harness_parts_stay_inside_the_prompt_candidate(opencode_db: Path) -> None:
    session = opencode.parse(opencode.source_path(opencode_db, "ses_fixture"))
    first = candidates(session)[0]
    assert (first.start_event, first.end_event, first.prompt) == (
        "e000001",
        "e000007-1",
        "Add a greeting command.",
    )
    assert first.signals == (
        "Nonempty user message starts a possible task boundary.",
        "Tool activity follows this message.",
        "Assistant question suggests a clarification exchange.",
    )
    assert [item.start_event for item in candidates(session)] == ["e000001", "e000009", "e000012"]


def test_opencode_legacy_parts_cover_ignored_text_failed_tools_and_bare_messages(
    tmp_path: Path,
) -> None:
    created = 1790848800000
    recorded = {
        "sessions": [
            {
                "id": "ses_edge",
                "parent_id": None,
                "directory": "/fixture",
                "title": "Edges",
                "version": "1.19.0",
                "time_created": created,
            }
        ],
        "messages": [
            {
                "id": "msg_a",
                "session_id": "ses_edge",
                "time_created": created + 2000,
                "data": {"role": "user", "time": {"created": created + 2000}},
                "parts": [
                    {"id": "prt_a1", "data": {"type": "text", "text": "Fix it."}},
                    {"id": "prt_a2", "data": {"type": "text", "ignored": True, "text": "Skipped."}},
                ],
            },
            {
                "id": "msg_b",
                "session_id": "ses_edge",
                "time_created": created + 3000,
                "data": {"role": "assistant", "time": {"created": created + 3000}},
                "parts": [
                    {
                        "id": "prt_b1",
                        "data": {
                            "type": "tool",
                            "tool": "edit",
                            "callID": "call-b",
                            "state": {
                                "status": "error",
                                "input": {"filePath": "a.py"},
                                "error": "File not found.",
                                "time": {"start": created + 3100, "end": created + 3200},
                            },
                        },
                    }
                ],
            },
            {
                "id": "msg_z",
                "session_id": "ses_edge",
                "time_created": created + 1000,
                "data": {
                    "role": "assistant",
                    "time": {"created": created + 1000},
                    "tokens": {"input": 4, "output": 6, "cache": {"read": 0, "write": 0}},
                    "error": {"name": "ProviderAuthError"},
                },
                "parts": [],
            },
        ],
    }
    fixture = tmp_path / "edge.json"
    fixture.write_text(json.dumps(recorded))
    database = write_opencode_database(tmp_path / "opencode.db", fixture)
    source = opencode.source_path(database, "ses_edge")
    session = opencode.parse(source, ImportLimits(max_events=5))
    assert [(event.event_id, event.kind, event.text, event.data) for event in session.events] == [
        ("e000001-error", "lifecycle", "", {"type": "error", "name": "ProviderAuthError"}),
        ("e000002", "user", "Fix it.", {}),
        ("e000003", "user", "<harness-event>Skipped.</harness-event>", {"ignored": True}),
        (
            "e000004-0",
            "tool_call",
            "",
            {"tool": "edit", "callID": "call-b", "input": {"filePath": "a.py"}, "status": "error"},
        ),
        (
            "e000004-1",
            "tool_result",
            "File not found.",
            {"tool": "edit", "callID": "call-b", "status": "error"},
        ),
    ]
    assert session.events[-1].timestamp == "2026-10-01T10:00:03.200Z"
    usage = session.usage[0]
    assert (usage.identity, usage.usage.output, usage.usage.reasoning) == ("msg_z", 6, None)
    with pytest.raises(LimitError, match="event limit"):
        opencode.parse(source, ImportLimits(max_events=3))


@pytest.fixture
def opencode_current_db(tmp_path: Path) -> Path:
    return write_opencode_current_database(
        tmp_path / "opencode.db", FIXTURES / "opencode-current-recorded.json"
    )


def test_current_opencode_storage_is_read_instead_of_the_legacy_tables(
    opencode_current_db: Path,
) -> None:
    assert opencode.discover(opencode_current_db.parent, ImportLimits()) == (
        opencode.source_path(opencode_current_db, "ses_current"),
        opencode.source_path(opencode_current_db, "ses_current_child"),
    )
    session = opencode.parse(opencode.source_path(opencode_current_db, "ses_current"))
    assert (session.source_id, session.source_version, session.parent_session_id) == (
        "ses_current",
        "2.2.5",
        None,
    )
    assert session.metadata == {"cwd": "/fixture/repository", "title": "Rename the greeting"}
    assert [(event.event_id, event.kind, event.text, event.data) for event in session.events] == [
        (
            "e000001",
            "metadata",
            "",
            {"type": "agent-switched", "agent": "build", "previous": "plan"},
        ),
        ("e000002", "user", "Rename the greeting to welcome.", {}),
        (
            "e000003",
            "user",
            "<harness-event><system-reminder>\nYou are in build mode.\n</system-reminder></harness-event>",
            {"description": "Loaded fixture/AGENTS.md"},
        ),
        ("e000004-1", "assistant", "Should the old name stay as an alias?", {}),
        (
            "e000004-2-0",
            "tool_call",
            "",
            {
                "tool": "read",
                "callID": "call-1",
                "input": {"path": "greet.py"},
                "status": "completed",
            },
        ),
        (
            "e000004-2-1",
            "tool_result",
            "print('Hello')\n",
            {"tool": "read", "callID": "call-1", "status": "completed"},
        ),
        ("e000005", "metadata", "The available tools have changed.", {"type": "system"}),
        ("e000006", "user", "No alias.", {}),
        (
            "e000007-0-0",
            "tool_call",
            "",
            {"tool": "edit", "callID": "call-2", "input": {"path": "greet.py"}, "status": "error"},
        ),
        (
            "e000007-0-1",
            "tool_result",
            "File changed on disk.",
            {"tool": "edit", "callID": "call-2", "status": "error", "error": "tool.error"},
        ),
        (
            "e000007-1-0",
            "tool_call",
            "",
            {
                "tool": "bash",
                "callID": "call-3",
                "input": {"command": "pytest"},
                "status": "running",
            },
        ),
        ("e000007-error", "lifecycle", "", {"type": "error", "name": "aborted"}),
        ("e000008", "lifecycle", "", {"type": "idle", "outcome": "failed", "error": "aborted"}),
    ]
    events = {event.event_id: event for event in session.events}
    assert events["e000004-2-1"].timestamp == "2026-10-26T07:33:26.000Z"
    assert "Private reasoning" not in "".join(event.text for event in session.events)
    assert [(item.identity, item.usage.output, item.usage.reasoning) for item in session.usage] == [
        ("msg_c04", 30, 10),
        ("msg_c07", 7, None),
    ]
    profile = user_message_profile(session)
    assert (profile.genuine, profile.harness_injected) == (2, 1)
    assert [item.start_event for item in candidates(session)] == ["e000002", "e000006"]


def test_current_opencode_storage_limits_and_child_sessions(opencode_current_db: Path) -> None:
    source = opencode.source_path(opencode_current_db, "ses_current")
    assert len(opencode.parse(source, ImportLimits(max_events=18)).events) == 13
    with pytest.raises(LimitError, match="event limit"):
        opencode.parse(source, ImportLimits(max_events=17))
    child = opencode.parse(opencode.source_path(opencode_current_db, "ses_current_child"))
    assert (child.parent_session_id, [event.text for event in child.events]) == (
        "ses_current",
        ["Review the rename."],
    )
    stored = (
        opencode_current_db.read_bytes().count(b"not-a-token"),
        child.model_dump_json().count("not-a-token"),
    )
    assert stored == (1, 0)
