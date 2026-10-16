import json
from pathlib import Path

import pytest

from dryheave.drivers.claude import ClaudeAdapter


def human(uuid="human", **changes):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "root",
        "cwd": "/fixture",
        "message": {"role": "user", "content": "Hello"},
        **changes,
    }


def assistant(stop_reason="end_turn", **changes):
    return {
        "type": "assistant",
        "uuid": "assistant",
        "parentUuid": "human",
        "sessionId": "root",
        "message": {
            "id": "response",
            "role": "assistant",
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": stop_reason,
        },
        **changes,
    }


def test_actual_shape_usage_and_tool_results_are_separate_from_human_acceptance():
    adapter = ClaudeAdapter()
    records = [
        json.loads(line)
        for line in Path("tests/fixtures/claude-recorded.jsonl").read_text().splitlines()
    ]
    events = [event for record in records for event in adapter.feed(record)]
    accepted = [event for event in events if event.kind == "accepted"]
    assert [event.text for event in accepted] == [
        "Add a greeting command.",
        "Use a comma after Hello.",
        "Change the command name to greet.",
    ]
    assert accepted[0].native_id == accepted[0].turn_id
    assert any(event.kind == "tool" and event.data.get("type") == "tool_result" for event in events)
    usage = [event for event in events if event.kind == "usage"]
    assert len({event.data["response_id"] for event in usage}) < len(usage)
    response = next(event for event in events if event.kind == "assistant")
    assert response.data["effort"] == "xhigh"


def test_completion_needs_linked_end_turn_and_lifecycle():
    adapter = ClaudeAdapter()
    adapter.feed(human())
    events = adapter.feed(assistant())
    assert [event.kind for event in events] == ["assistant"]
    completed = adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"})
    assert completed[0].kind == "completed"
    assert completed[0].turn_id == "human"


def test_recorded_tool_only_response_emits_observed_metadata():
    adapter = ClaudeAdapter()
    records = [
        json.loads(line)
        for line in Path("tests/fixtures/claude-recorded.jsonl").read_text().splitlines()
    ]
    adapter.feed(records[0])
    adapter.feed(records[1])
    events = adapter.feed(records[6])
    assert [event.kind for event in events] == ["metadata", "tool", "usage"]
    assert events[0].data == {"model": "fixture-model", "effort": "xhigh", "isSidechain": False}
    assert events[0].session_id == "fixture-id-3"
    assert events[0].native_id == "fixture-id-24"
    assert events[1].data["type"] == "tool_use"
    assert events[2].data["response_id"] == "fixture-id-21"


def test_sidechain_tool_stop_and_hook_continuation_cannot_complete_human_turn():
    adapter = ClaudeAdapter()
    assert all(event.kind != "accepted" for event in adapter.feed(human(isSidechain=True)))
    adapter.feed(human())
    adapter.feed(assistant("tool_use"))
    assert adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"}) == ()
    adapter.feed(assistant())
    hook = adapter.feed(
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "sessionId": "root",
            "preventedContinuation": True,
        }
    )[0]
    assert hook.data["preventedContinuation"] is True
    assert adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"}) == ()
    adapter.feed(assistant(parentUuid="unrelated"))
    assert adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"}) == ()


def test_session_is_described_by_the_first_record_with_a_cwd():
    adapter = ClaudeAdapter()
    assert adapter.feed({"type": "last-prompt", "sessionId": "root"}) == ()
    events = adapter.feed(human(version="2.1.281"))
    assert [event.kind for event in events] == ["metadata", "accepted", "started"]
    assert events[0].data == {"cwd": "/fixture", "version": "2.1.281"}
    assert [event.kind for event in adapter.feed(human("later"))] == ["accepted", "started"]


def test_attachments_between_prompt_and_answer_keep_the_turn_linked():
    adapter = ClaudeAdapter()
    adapter.feed(human())
    adapter.feed(
        {"type": "attachment", "uuid": "context", "parentUuid": "human", "sessionId": "root"}
    )
    events = adapter.feed(assistant(parentUuid="context"))
    assert [(event.kind, event.turn_id) for event in events] == [("assistant", "human")]
    adapter.feed({"type": "system", "subtype": "stop_hook_summary", "sessionId": "root"})
    completed = adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"})
    assert [(event.kind, event.turn_id) for event in completed] == [("completed", "human")]


def test_stop_hooks_that_let_the_turn_end_do_not_block_completion():
    adapter = ClaudeAdapter()
    adapter.feed(human())
    adapter.feed(assistant())
    summary = {
        "type": "system",
        "subtype": "stop_hook_summary",
        "sessionId": "root",
        "hookCount": 2,
        "hookErrors": [],
        "preventedContinuation": False,
        "stopReason": "",
    }
    assert [event.kind for event in adapter.feed(summary)] == ["hook"]
    completed = adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"})
    assert [(event.kind, event.turn_id) for event in completed] == [("completed", "human")]


@pytest.mark.parametrize("partial,hook", [(False, False), (True, False), (False, True)])
def test_synthetic_278_composer_paste_and_linked_lifecycle(tmp_path, driver_plan, partial, hook):
    from dryheave.drivers.artifacts import ArtifactWriter
    from dryheave.drivers.fake import FakeEventSource, FakeTerminal
    from dryheave.drivers.models import DriverLimits, Screen
    from dryheave.drivers.session import DriverSession
    from dryheave.models import AgentKind

    class SyntheticClaudeTerminal(FakeTerminal):
        def paste(self, text):
            self.pastes.append(text)
            self.current = Screen(
                text="\u276f " + (text[:1] if partial else text.replace("\n", "\n  ")), exited=None
            )

        def enter(self):
            self.enters += 1
            self.current = Screen(text="Working\n\u276f", exited=None)

    prompt = "Fix greeting.\nAsk which punctuation first."
    adapter = ClaudeAdapter()
    accepted = human(
        version="2.1.278", cwd=driver_plan.cwd, message={"role": "user", "content": prompt}
    )
    response = assistant(
        message={
            "id": "synthetic-response",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "Which punctuation?"}],
            "stop_reason": "end_turn",
        }
    )
    records = [accepted, response]
    if hook:
        records.append({"type": "system", "subtype": "hook_started", "sessionId": "root"})
    records.append({"type": "system", "subtype": "turn_duration", "sessionId": "root"})
    events = [event for record in records for event in adapter.feed(record)]
    source = FakeEventSource([])
    terminal = SyntheticClaudeTerminal([Screen(text="\u276f", exited=None)])
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare(prompt)
        source.batches.extend([[], events])
        result = session.deliver(intent)
        assert result.state == ("partial_input" if partial else "question")
        assert terminal.pastes == [prompt]
        assert terminal.enters == (0 if partial else 1)
        if not partial:
            assert session.launch.observed_model == "claude-haiku-4-5-20251001"
            assert session.launch.observed_effort is None
    assert terminal.closed


def command(name, arguments=None, *, order=("message", "name", "args")):
    tags = {
        "message": f"<command-message>{name[1:]}</command-message>",
        "name": f"<command-name>{name}</command-name>",
        "args": None if arguments is None else f"<command-args>{arguments}</command-args>",
    }
    content = "\n".join(tags[tag] for tag in order if tags[tag] is not None)
    return human("command", message={"role": "user", "content": content}, promptId="prompt")


def expansion(parent="command"):
    return {
        "type": "user",
        "uuid": "expansion",
        "parentUuid": parent,
        "sessionId": "root",
        "isMeta": True,
        "promptId": "prompt",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Base directory for this skill: /x\n\n# Review"}],
        },
    }


@pytest.mark.parametrize(
    ("record", "accepted"),
    [
        (command("/code-review", "--quick"), "/code-review --quick"),
        (command("/code-review", "--staged\nonly src"), "/code-review --staged\nonly src"),
        (command("/simplify"), "/simplify"),
        (command("/simplify", ""), "/simplify"),
        (command("/model", "", order=("name", "message", "args")), "/model"),
    ],
)
def test_slash_command_record_is_accepted_as_the_typed_command(record, accepted):
    events = ClaudeAdapter().feed(record)
    assert [(event.kind, event.text) for event in events[1:]] == [
        ("accepted", accepted),
        ("started", ""),
    ]
    assert events[1].data == {"command": accepted.split()[0]}


def test_expanded_skill_content_stays_in_the_command_turn_and_completes_once():
    adapter = ClaudeAdapter()
    records = [
        command("/code-review", "--quick"),
        expansion(),
        {"type": "attachment", "uuid": "context", "parentUuid": "expansion", "sessionId": "root"},
        assistant(parentUuid="context"),
        {"type": "system", "subtype": "turn_duration", "sessionId": "root"},
    ]
    events = [event for record in records for event in adapter.feed(record)]
    assert [(event.kind, event.turn_id) for event in events if event.kind != "metadata"] == [
        ("accepted", "command"),
        ("started", "command"),
        ("assistant", "command"),
        ("completed", "command"),
    ]


@pytest.mark.parametrize("tag", ["stdout", "stderr"])
def test_local_command_output_completes_the_command_turn_without_a_new_human_turn(tag):
    adapter = ClaudeAdapter()
    adapter.feed(command("/model", ""))
    text = "Set model to `Fable 5` for this session only"
    events = adapter.feed(
        {
            "type": "user",
            "uuid": "output",
            "parentUuid": "command",
            "sessionId": "root",
            "message": {
                "role": "user",
                "content": f"<local-command-{tag}>{text}</local-command-{tag}>",
            },
        }
    )
    assert [(event.kind, event.turn_id, event.text) for event in events] == [
        ("assistant", "command", text),
        ("completed", "command", ""),
    ]


def test_local_command_output_without_a_tracked_parent_turn_is_unsupported():
    events = ClaudeAdapter().feed(
        {
            "type": "user",
            "uuid": "stray",
            "parentUuid": "unknown",
            "sessionId": "root",
            "message": {
                "role": "user",
                "content": "<local-command-stdout>Hi</local-command-stdout>",
            },
        }
    )
    assert [event.kind for event in events] == ["unsupported"]


@pytest.mark.parametrize(
    "content",
    [
        "Explain <command-name>/code-review</command-name> please",
        "<command-name>code-review</command-name>",
        "<command-name>/a</command-name><command-name>/b</command-name>",
    ],
)
def test_text_that_only_mentions_command_tags_is_accepted_verbatim(content):
    events = ClaudeAdapter().feed(human(message={"role": "user", "content": content}))
    assert (events[1].kind, events[1].text, events[1].data) == ("accepted", content, {})


LAUNCH_RESULTS = {
    "Agent": lambda task: {"isAsync": True, "status": "async_launched", "agentId": task},
    "Bash": lambda task: {
        "stdout": "",
        "stderr": "",
        "interrupted": False,
        "backgroundTaskId": task,
    },
    "Monitor": lambda task: {"taskId": task, "timeoutMs": 300000, "persistent": False},
}


def background_launch(tool, task, *, parent="human", uuid="launch"):
    return [
        {
            "type": "assistant",
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "root",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"use-{task}",
                        "name": tool,
                        "input": {"run_in_background": True},
                    }
                ],
                "stop_reason": "tool_use",
            },
        },
        {
            "type": "user",
            "uuid": f"{uuid}-result",
            "parentUuid": uuid,
            "sessionId": "root",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"use-{task}", "content": "Started."}
                ],
            },
            "toolUseResult": LAUNCH_RESULTS[tool](task),
        },
    ]


def turn_end(parent, text, *, uuid, pending=None):
    duration = {"type": "system", "subtype": "turn_duration", "uuid": f"{uuid}-duration"}
    if pending is not None:
        duration["pendingBackgroundAgentCount"] = pending
    return [
        assistant(
            uuid=uuid,
            parentUuid=parent,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
            },
        ),
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "uuid": f"{uuid}-hooks",
            "parentUuid": uuid,
            "sessionId": "root",
            "preventedContinuation": False,
        },
        {**duration, "parentUuid": f"{uuid}-hooks", "sessionId": "root"},
    ]


def notification_text(*tasks, tool_use=True, status="completed"):
    ids = "".join(f"<task-id>{task}</task-id>\n" for task in tasks)
    use = f"<tool-use-id>use-{tasks[0]}</tool-use-id>\n" if tool_use else ""
    state = f"<status>{status}</status>\n" if status else ""
    return (
        f"<task-notification>\n{ids}{use}{state}<summary>Task finished</summary>\n"
        "</task-notification>"
    )


def notification(task, parent, *, uuid="notified", content=None):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "root",
        "message": {"role": "user", "content": content or notification_text(task)},
        "origin": {"kind": "task-notification", "producer": "session-task"},
        "promptSource": "system",
        "turnOrigin": "task_notification",
    }


def queued_notification(task, parent, *, uuid="queued", prompt=None):
    return {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "root",
        "attachment": {
            "type": "queued_command",
            "prompt": prompt or notification_text(task),
            "commandMode": "task-notification",
            "origin": {"kind": "task-notification", "producer": "session-task"},
        },
    }


def lifecycle(adapter, records):
    return [
        (event.kind, event.turn_id, event.text)
        for record in records
        for event in adapter.feed(record)
        if event.kind in {"accepted", "started", "assistant", "completed"}
    ]


def fed(adapter, records):
    for record in records:
        adapter.feed(record)
    return adapter


@pytest.mark.parametrize("pending", [1, None])
def test_a_background_agent_keeps_the_turn_open_until_claude_continues_and_ends(pending):
    adapter = fed(ClaudeAdapter(), [human()])
    waiting = lifecycle(
        adapter,
        [
            *background_launch("Agent", "agent-1"),
            *turn_end("launch-result", "Review running.", uuid="first", pending=pending),
        ],
    )
    assert waiting == [("assistant", "human", "Review running.")]
    continued = lifecycle(
        adapter,
        [
            notification("agent-1", "first-duration"),
            *turn_end("notified", "Review found nothing.", uuid="second"),
        ],
    )
    assert continued == [
        ("assistant", "human", "Review found nothing."),
        ("completed", "human", ""),
    ]


@pytest.mark.parametrize(("tool", "pending"), [("Bash", 0), ("Bash", None), ("Monitor", None)])
def test_background_shells_and_monitors_never_hold_the_turn(tool, pending):
    adapter = fed(ClaudeAdapter(), [human()])
    records = [
        *background_launch(tool, "server-1"),
        *turn_end("launch-result", "Dev server running.", uuid="first", pending=pending),
    ]
    assert lifecycle(adapter, records)[-1] == ("completed", "human", "")
    second = lifecycle(
        adapter,
        [
            human(uuid="again", message={"role": "user", "content": "Next"}),
            *turn_end("again", "Next done.", uuid="second"),
        ],
    )
    assert second[-1] == ("completed", "again", "")


def test_a_late_shell_notification_continues_its_own_turn_without_human_acceptance():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Bash", "shell-1"),
            *turn_end("launch-result", "Running.", uuid="first"),
        ],
    )
    events = adapter.feed(notification("shell-1", "first-duration"))
    assert (events, adapter.turn_id, adapter.background) == ((), "human", {})
    continued = lifecycle(adapter, turn_end("notified", "Shell finished.", uuid="second"))
    assert continued == [("assistant", "human", "Shell finished."), ("completed", "human", "")]


def test_one_notification_finishes_every_task_it_lists():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Agent", "agent-1"),
            *background_launch("Agent", "agent-2", parent="launch-result", uuid="again"),
            *turn_end("again-result", "Both running.", uuid="first"),
        ],
    )
    content = notification_text("agent-1", "agent-2", tool_use=False, status="stopped")
    finished = lifecycle(
        adapter,
        [
            notification("agent-1", "first-duration", content=content),
            *turn_end("notified", "Both stopped.", uuid="second"),
        ],
    )
    assert finished == [("assistant", "human", "Both stopped."), ("completed", "human", "")]


def test_completion_waits_for_every_outstanding_background_agent():
    adapter = fed(ClaudeAdapter(), [human()])
    records = [
        *background_launch("Agent", "agent-1"),
        *background_launch("Agent", "agent-2", parent="launch-result", uuid="again"),
        *turn_end("again-result", "Both running.", uuid="first"),
        notification("agent-1", "first-duration"),
        *turn_end("notified", "One agent done.", uuid="second"),
    ]
    assert [kind for kind, _, _ in lifecycle(adapter, records)] == ["assistant", "assistant"]
    finished = lifecycle(
        adapter,
        [
            notification("agent-2", "second-duration", uuid="notified-again"),
            *turn_end("notified-again", "All done.", uuid="third"),
        ],
    )
    assert finished == [("assistant", "human", "All done."), ("completed", "human", "")]


def test_a_new_human_turn_clears_tasks_left_from_earlier_turns():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Agent", "agent-1"),
            *turn_end("launch-result", "Agent left running.", uuid="first", pending=0),
        ],
    )
    second = lifecycle(
        adapter,
        [
            human(uuid="again", message={"role": "user", "content": "Next"}),
            *turn_end("again", "Next done.", uuid="second"),
        ],
    )
    assert second[-1] == ("completed", "again", "")


def test_claude_reported_pending_count_overrides_tracked_agents():
    adapter = fed(ClaudeAdapter(), [human()])
    records = [
        *background_launch("Agent", "agent-1"),
        *turn_end("launch-result", "Agent finished silently.", uuid="first", pending=0),
    ]
    assert lifecycle(adapter, records)[-1] == ("completed", "human", "")


def test_claude_reported_pending_agents_hold_completion_without_a_tracked_launch():
    adapter = fed(ClaudeAdapter(), [human()])
    waiting = lifecycle(adapter, turn_end("human", "Waiting on agents.", uuid="first", pending=2))
    assert waiting == [("assistant", "human", "Waiting on agents.")]


def test_a_notification_delivered_mid_turn_lets_that_turn_complete():
    adapter = fed(ClaudeAdapter(), [human()])
    records = [
        *background_launch("Agent", "agent-1"),
        queued_notification("agent-1", "launch-result"),
        *turn_end("queued", "Agent reported back.", uuid="first"),
    ]
    assert lifecycle(adapter, records)[-1] == ("completed", "human", "")


def test_a_notification_without_a_status_finishes_nothing():
    adapter = fed(ClaudeAdapter(), [human()])
    event = notification_text("agent-1", status=None)
    records = [
        *background_launch("Agent", "agent-1"),
        queued_notification("agent-1", "launch-result", prompt=event),
        *turn_end("queued", "Agent still running.", uuid="first"),
    ]
    assert lifecycle(adapter, records) == [("assistant", "human", "Agent still running.")]


def test_a_notification_for_an_untracked_task_is_ignored():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Agent", "agent-1"),
            *turn_end("launch-result", "Agent running.", uuid="first"),
        ],
    )
    content = notification_text("stranger", tool_use=False)
    assert adapter.feed(notification("stranger", "unknown-parent", content=content)) == ()
    assert (adapter.turn_id, list(adapter.background)) == (None, ["agent-1"])
    stray = lifecycle(adapter, turn_end("notified", "Stray continuation.", uuid="second"))
    assert stray == [("assistant", None, "Stray continuation.")]


def test_a_notification_without_a_task_id_is_matched_by_its_tool_use():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Agent", "agent-1"),
            *turn_end("launch-result", "Running.", uuid="first"),
        ],
    )
    content = notification_text("agent-1").replace("<task-id>agent-1</task-id>\n", "")
    adapter.feed(notification("agent-1", "first-duration", content=content))
    finished = lifecycle(adapter, turn_end("notified", "Done.", uuid="second"))
    assert (finished[-1], adapter.launches) == (("completed", "human", ""), {})


def test_a_stopped_background_agent_is_no_longer_outstanding():
    adapter = fed(ClaudeAdapter(), [human()])
    stop = {
        "type": "user",
        "uuid": "stopped",
        "parentUuid": "launch-result",
        "sessionId": "root",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "stop", "content": "Stopped."}],
        },
        "toolUseResult": {"message": "Stopped", "task_id": "agent-1", "task_type": "local_agent"},
    }
    records = [
        *background_launch("Agent", "agent-1"),
        stop,
        *turn_end("stopped", "Ok.", uuid="end"),
    ]
    assert lifecycle(adapter, records)[-1] == ("completed", "human", "")


def compaction(logical_parent, *, parent=None):
    return [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": parent,
            "logicalParentUuid": logical_parent,
            "sessionId": "root",
            "isSidechain": False,
            "compactMetadata": {"trigger": "auto", "preTokens": 1000, "postTokens": 100},
        },
        {
            "type": "attachment",
            "uuid": "compact-context",
            "parentUuid": "boundary",
            "sessionId": "root",
            "attachment": {"type": "session_context"},
        },
        human(
            "summary",
            parentUuid="compact-context",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
            message={
                "role": "user",
                "content": "This session is being continued from a previous conversation.",
            },
        ),
        {
            "type": "attachment",
            "uuid": "compact-reminder",
            "parentUuid": "summary",
            "sessionId": "root",
            "attachment": {"type": "total_tokens_reminder"},
        },
    ]


def working(parent="human"):
    return {
        "type": "assistant",
        "uuid": "working",
        "parentUuid": parent,
        "sessionId": "root",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "use-read", "name": "Read", "input": {}}],
            "stop_reason": "tool_use",
        },
    }


@pytest.mark.parametrize("logical_parent", ["working", "compact-reminder", None])
def test_a_turn_compacted_midway_completes_once_in_the_same_turn(logical_parent):
    adapter = fed(ClaudeAdapter(), [human(), working()])
    events = lifecycle(
        adapter,
        [
            *compaction(logical_parent),
            *turn_end("compact-reminder", "Done after compacting.", uuid="end"),
        ],
    )
    assert events == [
        ("assistant", "human", "Done after compacting."),
        ("completed", "human", ""),
    ]


def test_a_compact_summary_is_never_human_input():
    adapter = ClaudeAdapter()
    records = [human(), working(), *compaction("working")]
    accepted = [
        (event.native_id, event.text)
        for record in records
        for event in adapter.feed(record)
        if event.kind == "accepted"
    ]
    assert (accepted, adapter.chain["summary"]) == ([("human", "Hello")], "human")


def hand_back(task, parent, *, uuid="handback"):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "root",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": f'Another Claude session sent a message:\n<agent-message from="{task}">\n'
            "[Subagent hand-back] Review found nothing.\n</agent-message>",
        },
        "origin": {"kind": "peer", "from": task, "senderTaskId": task},
    }


def transcript_only(record):
    return {**record, "queueTranscriptOnly": True, "queueSkipAttachments": True}


def test_a_transcript_only_notification_completes_a_turn_held_for_its_agent():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Agent", "agent-1"),
            *turn_end("launch-result", "Review running.", uuid="first", pending=1),
        ],
    )
    answered = lifecycle(
        adapter,
        [
            hand_back("agent-1", "first-duration"),
            *turn_end("handback", "Review found nothing.", uuid="second", pending=1),
        ],
    )
    assert answered == [("assistant", "human", "Review found nothing.")]
    finished = lifecycle(adapter, [transcript_only(notification("agent-1", "second-duration"))])
    assert finished == [("completed", "human", "")]


def test_a_transcript_only_notification_keeps_holding_while_another_agent_runs():
    adapter = fed(
        ClaudeAdapter(),
        [
            human(),
            *background_launch("Agent", "agent-1"),
            *background_launch("Agent", "agent-2", parent="launch-result", uuid="again"),
            *turn_end("again-result", "Reviews running.", uuid="first", pending=2),
        ],
    )
    first = lifecycle(adapter, [transcript_only(notification("agent-1", "first-duration"))])
    assert first == []
    second = lifecycle(
        adapter,
        [transcript_only(notification("agent-2", "notified", uuid="notified-2"))],
    )
    assert second == [("completed", "human", "")]
