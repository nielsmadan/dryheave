import json
import threading
import time
from pathlib import Path

import pytest

from conftest import (
    CHANNEL,
    EDIT_PERMISSION,
    FIX_SCOPE,
    AskedQuestion,
    ClaudeQuestionUI,
    ScreenUI,
)
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.base import DeliveryUncertain
from dryheave.drivers.claude import ClaudeAdapter
from dryheave.drivers.dialogs import UNRECOGNIZED
from dryheave.drivers.fake import FakeEventSource, FakeTerminal
from dryheave.drivers.models import (
    DialogSelection,
    DriverLimits,
    NativeEvent,
    Screen,
    ScreenAction,
)
from dryheave.drivers.session import (
    COMPOSER_UNREADABLE,
    SCREEN_ACTION_REFUSED,
    SCREEN_WAIT_EXPIRED,
    DriverSession,
)
from dryheave.errors import InputError
from dryheave.models import AgentKind


def native(kind, *, text="", turn="turn", session="root", **changes):
    return NativeEvent(
        kind=kind, session_id=session, thread_id=session, turn_id=turn, text=text, **changes
    )


def turn_events(prompt="Hello", *, turn="turn", final="Done.", session="root"):
    return [
        native("started", turn=turn, session=session),
        native("accepted", text=prompt, turn=turn, session=session, native_id=turn + "-user"),
        native("assistant", text=final, turn=turn, session=session),
        native("completed", turn=turn, session=session, text=final),
    ]


def build(tmp_path, plan, *, batches=(), terminal=None, cancelled=None):
    terminal = terminal or FakeTerminal([])
    source = FakeEventSource(batches)
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CODEX,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
        cancelled=cancelled,
    )
    assert session.start(plan, {}).state == "ready"
    return session, terminal, source


def metadata(plan, session="root", parent=None):
    return native(
        "metadata",
        session=session,
        parent_session_id=parent,
        data={"cwd": plan.cwd, "cli_version": "0.153.4"},
    )


def test_fresh_acceptance_matching_completion_and_late_root_binding(tmp_path, driver_plan):
    session, terminal, source = build(tmp_path, driver_plan)
    with session:
        intent = session.prepare("  Hello\r\nthere  ")
        assert intent.root_session_id is None
        source.batches.extend(
            [[], [metadata(driver_plan), *turn_events("Hello\nthere", final="Which punctuation?")]]
        )
        result = session.deliver(intent)
        assert (result.state, result.turn_id, result.accepted_id) == (
            "question",
            "turn",
            "turn-user",
        )
        assert result.text == "Which punctuation?"
        assert session.accepted_count == 1
        assert terminal.pastes == ["Hello\nthere"]
        assert terminal.enters == 1
        assert session.launch.observed_session_id == "root"
        assert session.launch.observed_model is None
        assert session.launch.requested_argv == driver_plan.argv
    assert session.cleanup.terminal_closed
    assert session.cleanup.logs_drained
    assert terminal.closed
    intent_file = next(session.artifacts.root.glob("*-submission-intent.json"))
    enter_file = next(session.artifacts.root.glob("*-enter-intent.json"))
    assert intent_file.name < enter_file.name


@pytest.mark.parametrize("tool_only", [False, True])
def test_claude_recorded_effort_updates_launch_observation(tmp_path, driver_plan, tool_only):
    adapter = ClaudeAdapter()
    records = [
        json.loads(line)
        for line in Path("tests/fixtures/claude-recorded.jsonl").read_text().splitlines()
    ]
    if tool_only:
        records = [records[index] for index in (0, 1, 6)]
    events = [event for record in records for event in adapter.feed(record)]
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE, "cwd": "/fixture/repository"})
    session = DriverSession(
        FakeTerminal([Screen(text="\u276f", exited=None)]),
        FakeEventSource([events]),
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        assert session.launch.observed_model == "fixture-model"
        assert session.launch.observed_effort == "xhigh"


@pytest.mark.parametrize(
    "phrase",
    [
        "Would you like to run the following command?",
        "Sign in with ChatGPT",
        "Input disabled.",
        "esc to cancel",
        "approval required",
    ],
)
def test_dialog_text_in_prompt_and_completed_transcript_does_not_block(
    tmp_path, driver_plan, phrase
):
    prompt = f"Update the confirmation label to “{phrase}”"
    final = f"Updated the label to “{phrase}”."
    session, terminal, source = build(tmp_path, driver_plan)
    with session:
        intent = session.prepare(prompt)
        terminal.screens.extend(
            [
                Screen(text="\u203a", exited=None),
                Screen(text=f"\u203a {prompt}", exited=None),
                Screen(text=f"• {final}\n\n\u203a\n\n? for shortcuts", exited=None),
            ]
        )
        source.batches.extend([[], [metadata(driver_plan), *turn_events(prompt, final=final)]])
        result = session.deliver(intent)
        assert (result.state, result.text) == ("completed", final)
        assert terminal.pastes == [prompt]
        assert terminal.enters == 1
        assert session.accepted_count == 1


def test_enter_timeout_reconciles_without_resending(tmp_path, driver_plan):
    terminal = FakeTerminal([], uncertain_enter=True)
    session, terminal, source = build(tmp_path, driver_plan, terminal=terminal)
    with session:
        intent = session.prepare("Hello")
        source.batches.extend([[], [metadata(driver_plan), *turn_events()]])
        assert session.deliver(intent).state == "completed"
        with pytest.raises(InputError, match="reconciliation"):
            session.deliver(intent)
        assert session.reconcile().state == "completed"
        assert terminal.enters == 1
        assert session.accepted_count == 1


def test_stale_echo_assistant_text_and_wrong_turn_do_not_complete(tmp_path, driver_plan):
    session, terminal, source = build(
        tmp_path, driver_plan, batches=[[metadata(driver_plan), *turn_events()]]
    )
    with session:
        intent = session.prepare("Hello")
        source.batches.extend(
            [[], [native("assistant", text="Hello\nDone."), native("completed", turn="other")]]
        )
        result = session.deliver(intent)
        assert result.state == "timeout"
        assert session.accepted_count == 0
        assert terminal.enters == 1


def test_queued_acceptance_waits_for_its_own_start_and_completion(tmp_path, driver_plan):
    session, _terminal, source = build(tmp_path, driver_plan, batches=[[metadata(driver_plan)]])
    with session:
        intent = session.prepare("Hello")
        source.batches.extend(
            [
                [],
                [native("accepted", text="Hello", turn="queued", native_id="accepted")],
                [native("completed", turn="previous")],
                [native("started", turn="queued")],
                [native("completed", turn="queued", text="Done")],
            ]
        )
        result = session.deliver(intent)
        assert (result.state, result.turn_id, session.accepted_count) == ("completed", "queued", 1)


def test_partial_paste_never_sends_enter(tmp_path, driver_plan):
    session, terminal, _source = build(
        tmp_path, driver_plan, terminal=FakeTerminal([], partial_paste=True)
    )
    with session:
        result = session.deliver(session.prepare("Hello"))
        assert (result.state, result.text) == ("partial_input", "\u203a H")
        assert terminal.enters == 0
    assert terminal.closed


def test_delayed_composer_render_waits_for_match(tmp_path, driver_plan):
    session, terminal, source = build(tmp_path, driver_plan)
    with session:
        intent = session.prepare("Hello")
        terminal.screens.extend(
            [
                Screen(text="\u203a", exited=None),
                Screen(text="\u203a", exited=None),
                Screen(text="\u203a Hello", exited=None),
            ]
        )
        source.batches.extend([[], [metadata(driver_plan), *turn_events()]])
        assert session.deliver(intent).state == "completed"
        assert terminal.enters == 1


@pytest.mark.parametrize(
    "text,state",
    [
        (
            json.loads(Path("tests/fixtures/codex-dialogs.json").read_text())["exec"],
            "approval",
        ),
        (
            json.loads(Path("tests/fixtures/codex-dialogs.json").read_text())["freeform"],
            "structured_input",
        ),
        (
            (
                "Unknown service: select an option\n\u203a 1. Proceed\n  2. Cancel\n\n"
                "Press enter to confirm or esc to cancel"
            ),
            "unsupported",
        ),
    ],
)
def test_native_dialogs_stop_before_simulator_text(tmp_path, driver_plan, text, state):
    terminal = FakeTerminal([Screen(text=text, exited=None)])
    source = FakeEventSource([])
    session = DriverSession(
        terminal, source, ArtifactWriter(tmp_path / "evidence", 100000), agent=AgentKind.CODEX
    )
    with session:
        assert session.start(driver_plan, {}).state == state
        assert terminal.pastes == []
        assert terminal.enters == 0


def test_disabled_input_with_retained_cursor_stops_before_submission(tmp_path, driver_plan):
    terminal = FakeTerminal(
        [Screen(text="\u203a Input disabled.", exited=None, cursor={"x": 16, "y": 0})]
    )
    session = DriverSession(
        terminal,
        FakeEventSource([]),
        ArtifactWriter(tmp_path / "evidence", 100000),
        agent=AgentKind.CODEX,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    with session:
        result = session.start(driver_plan, {})
        assert (result.state, result.reason) == ("unsupported", "Native input is disabled.")
        assert terminal.pastes == []
        assert terminal.enters == 0


def test_descendant_dialogue_cannot_accept_root_submission(tmp_path, driver_plan):
    session, _terminal, source = build(tmp_path, driver_plan, batches=[[metadata(driver_plan)]])
    with session:
        intent = session.prepare("Hello")
        source.batches.extend(
            [[], [metadata(driver_plan, "child", "root"), *turn_events(session="child")]]
        )
        assert session.deliver(intent).state == "timeout"
        assert session.graph == {"root": None, "child": "root"}
        assert session.accepted_count == 0


def test_cancellation_and_interrupt_cleanup(tmp_path, driver_plan):
    cancelled = threading.Event()
    session, terminal, _source = build(tmp_path, driver_plan, cancelled=cancelled)
    with pytest.raises(KeyboardInterrupt), session:
        cancelled.set()
        assert session.deliver(session.prepare("Hello")).state == "cancelled"
        raise KeyboardInterrupt
    assert terminal.closed
    assert session.cleanup.known_writers_stopped


@pytest.mark.parametrize(
    "text", ["/permissions", "!rm anything", "$some-skill", "Hello\x1b[A", "\x00"]
)
def test_generated_reply_cannot_invoke_native_controls(tmp_path, driver_plan, text):
    session, terminal, _source = build(tmp_path, driver_plan)
    with session, pytest.raises(InputError):
        session.prepare(text)
    assert terminal.pastes == []


def test_subject_exit_zero_without_completion_is_not_success(tmp_path, driver_plan):
    terminal = FakeTerminal([Screen(text="Goodbye", exited=0)])
    session = DriverSession(
        terminal,
        FakeEventSource([]),
        ArtifactWriter(tmp_path / "evidence", 100000),
        agent=AgentKind.CODEX,
    )
    with session:
        assert session.start(driver_plan, {}).state == "exited"


def test_ambiguous_duplicate_acceptances_and_conflicting_roots_stop(tmp_path, driver_plan):
    session, _terminal, source = build(tmp_path, driver_plan, batches=[[metadata(driver_plan)]])
    with session:
        intent = session.prepare("Hello")
        source.batches.extend([[], [*turn_events(turn="one"), *turn_events(turn="two")]])
        assert session.deliver(intent).state == "unsupported"
        source.batches.append([metadata(driver_plan, "other-root")])
        with pytest.raises(InputError, match="Multiple root"):
            session.reconcile()


def test_claude_theme_picker_stops_delivery_before_paste(tmp_path, driver_plan):
    picker = Screen.model_validate_json(
        Path("tests/fixtures/claude-2.1.281-theme-picker.json").read_text()
    )
    terminal = FakeTerminal([Screen(text="\u276f", exited=None)])
    session = DriverSession(
        terminal,
        FakeEventSource([]),
        ArtifactWriter(tmp_path / "evidence", 100000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    with session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare("Hello")
        terminal.screens.append(picker)
        result = session.deliver(intent)
        assert (result.state, result.reason, result.text) == (
            "unsupported",
            "Unrecognized native dialog requires input.",
            picker.text,
        )
        assert terminal.pastes == []
        assert terminal.enters == 0


@pytest.mark.parametrize(
    "text,state,reason",
    [
        ("Loading", "unsupported", "Native composer was not observed; delivery stopped."),
        ("\u203a leftover", "partial_input", "Composer already contains text; delivery stopped."),
    ],
)
def test_unusable_composer_stops_delivery_with_screen_evidence(
    tmp_path, driver_plan, text, state, reason
):
    session, terminal, _source = build(tmp_path, driver_plan)
    with session:
        intent = session.prepare("Hello")
        terminal.screens.append(Screen(text=text, exited=None))
        result = session.deliver(intent)
        assert (result.state, result.reason, result.text) == (state, reason, text)
        assert terminal.pastes == []
        assert terminal.enters == 0


@pytest.mark.parametrize(
    ("existing", "cursor", "enters"),
    [("go ahead", {"x": 2, "y": 0}, 1), ("leftover", {"x": 10, "y": 0}, 0)],
)
def test_claude_types_over_a_suggestion_but_never_sends_mixed_text(
    tmp_path, driver_plan, existing, cursor, enters
):
    class ClaudeTerminal(FakeTerminal):
        def paste(self, text):
            self.pastes.append(text)
            kept = "" if cursor["x"] == 2 else existing
            self.current = Screen(text="\u276f " + kept + text, exited=None)

        def enter(self):
            self.enters += 1
            self.current = Screen(text="Working\n\u276f", exited=None)

    terminal = ClaudeTerminal([])
    terminal.current = Screen(text="\u276f", exited=None)
    session = DriverSession(
        terminal,
        FakeEventSource([]),
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=1, max_polls=10, poll_seconds=0.01),
    )
    with session:
        plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
        assert session.start(plan, {}).state == "ready"
        terminal.current = Screen(text="\u276f " + existing, exited=None, cursor=cursor)
        result = session.deliver(session.prepare("Hello"))
        assert terminal.pastes == ["Hello"], (result.state, result.reason)
        assert terminal.enters == enters, (result.state, result.reason, result.text)


class MenuTerminal(FakeTerminal):
    def __init__(self, highlighted):
        super().__init__([])
        self.highlighted = highlighted
        self.current = Screen(text="\u276f", exited=None, cursor={"x": 2, "y": 0})

    def paste(self, text):
        self.pastes.append(text)
        rule = "\u2500" * 40
        menu = f"  \u276f {self.highlighted:<30}Review code.\n    /review-perf{' ' * 22}Perf."
        self.current = Screen(
            text=f"{menu}\n{rule}\n\u276f {text}\n{rule}",
            exited=None,
            cursor={"x": 2 + len(text), "y": 3},
        )

    def enter(self):
        self.enters += 1
        self.current = Screen(text="Working\n\u276f", exited=None)


def claude_command_turn(plan, name, arguments):
    adapter = ClaudeAdapter()
    content = f"<command-message>{name[1:]}</command-message>\n<command-name>{name}</command-name>"
    records = [
        {
            "type": "user",
            "uuid": "command",
            "sessionId": "root",
            "cwd": plan.cwd,
            "message": {"role": "user", "content": content + arguments},
        },
        {
            "type": "user",
            "uuid": "expansion",
            "parentUuid": "command",
            "sessionId": "root",
            "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "# Skill body"}]},
        },
        {
            "type": "assistant",
            "uuid": "answer",
            "parentUuid": "expansion",
            "sessionId": "root",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Reviewed."}],
                "stop_reason": "end_turn",
            },
        },
        {"type": "system", "subtype": "turn_duration", "sessionId": "root"},
    ]
    return [event for record in records for event in adapter.feed(record)]


@pytest.mark.parametrize(
    ("prompt", "arguments"),
    [("/code-review", ""), ("/code-review --quick", "\n<command-args>--quick</command-args>")],
)
def test_claude_slash_command_is_delivered_through_its_menu_as_one_turn(
    tmp_path, driver_plan, prompt, arguments
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = MenuTerminal("/code-review")
    source = FakeEventSource([])
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare(prompt, policy="curated-native-command")
        source.batches.extend([[], claude_command_turn(plan, "/code-review", arguments)])
        result = session.deliver(intent)
        assert (result.state, result.text, result.turn_id) == ("completed", "Reviewed.", "command")
        assert (terminal.pastes, terminal.enters, session.accepted_count) == ([prompt], 1, 1)


def claude_local_command_turn(plan, name, output):
    adapter = ClaudeAdapter()
    content = f"<command-message>{name[1:]}</command-message>\n<command-name>{name}</command-name>"
    records = [
        {
            "type": "user",
            "uuid": "command",
            "sessionId": "root",
            "cwd": plan.cwd,
            "message": {"role": "user", "content": content},
        },
        {
            "type": "user",
            "uuid": "stdout",
            "parentUuid": "command",
            "sessionId": "root",
            "message": {
                "role": "user",
                "content": f"<local-command-stdout>{output}</local-command-stdout>",
            },
        },
    ]
    return [event for record in records for event in adapter.feed(record)]


def test_claude_local_command_completes_its_own_turn_without_a_new_human_turn(
    tmp_path, driver_plan
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = MenuTerminal("/model")
    source = FakeEventSource([])
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare("/model", policy="curated-native-command")
        output = "Set model to `Fable 5` for this session only"
        source.batches.extend([[], claude_local_command_turn(plan, "/model", output)])
        result = session.deliver(intent)
        assert (result.state, result.text, result.turn_id) == ("completed", output, "command")
        assert (terminal.pastes, terminal.enters, session.accepted_count) == (["/model"], 1, 1)


class ClaudePromptTerminal(FakeTerminal):
    def __init__(self):
        super().__init__([])
        self.current = Screen(text="\u276f", exited=None)

    def paste(self, text):
        self.pastes.append(text)
        self.current = Screen(text=f"\u276f {text}", exited=None)

    def enter(self):
        self.enters += 1
        self.current = Screen(text="Working\n\u276f", exited=None)


def claude_record(uuid, parent, role, content, **changes):
    return {
        "type": role,
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "root",
        "message": {"role": role, "content": content},
        **changes,
    }


def claude_turn_end(uuid, parent, text):
    answer = claude_record(uuid, parent, "assistant", [{"type": "text", "text": text}])
    answer["message"]["stop_reason"] = "end_turn"
    duration = {
        "type": "system",
        "subtype": "turn_duration",
        "uuid": f"{uuid}-duration",
        "parentUuid": uuid,
        "sessionId": "root",
    }
    return [answer, duration]


def claude_background_turn(plan, prompt):
    adapter = ClaudeAdapter()
    launch = [
        claude_record("human", None, "user", prompt, cwd=plan.cwd),
        claude_record(
            "launch",
            "human",
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "use",
                    "name": "Agent",
                    "input": {"run_in_background": True},
                }
            ],
        ),
        claude_record(
            "launched",
            "launch",
            "user",
            [{"type": "tool_result", "tool_use_id": "use", "content": "Launched."}],
            toolUseResult={"isAsync": True, "status": "async_launched", "agentId": "agent-1"},
        ),
        *claude_turn_end("waiting", "launched", "Review agent running."),
    ]
    notice = (
        "<task-notification>\n<task-id>agent-1</task-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    continuation = [
        claude_record(
            "notified",
            "waiting-duration",
            "user",
            notice,
            origin={"kind": "task-notification"},
            turnOrigin="task_notification",
        ),
        *claude_turn_end("reported", "notified", "Review found one bug."),
    ]
    return (
        [event for record in launch for event in adapter.feed(record)],
        [event for record in continuation for event in adapter.feed(record)],
    )


def claude_session(tmp_path, plan, source, terminal):
    return DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )


def test_claude_turn_with_background_work_completes_after_the_automatic_continuation(
    tmp_path, driver_plan
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal, source = ClaudePromptTerminal(), FakeEventSource([])
    with claude_session(tmp_path, plan, source, terminal) as session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare("Review this.")
        launch, continuation = claude_background_turn(plan, "Review this.")
        source.batches.extend([[], launch, [], continuation])
        result = session.deliver(intent)
        assert (result.state, result.turn_id, result.text) == (
            "completed",
            "human",
            "Review agent running.\nReview found one bug.",
        )
        assert (terminal.enters, session.accepted_count) == (1, 1)
        assert [event.kind for event in session.events].count("completed") == 1


def test_claude_background_work_still_running_at_the_deadline_times_out(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal, source = ClaudePromptTerminal(), FakeEventSource([])
    with claude_session(tmp_path, plan, source, terminal) as session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare("Review this.")
        launch, _continuation = claude_background_turn(plan, "Review this.")
        source.batches.extend([[], launch])
        result = session.deliver(intent)
        assert (result.state, result.reason) == (
            "timeout",
            "Driver lifetime or polling limit reached.",
        )
        assert session.reconcile().state == "accepted"


def claude_compacted_turn(plan, prompt):
    adapter = ClaudeAdapter()
    records = [
        claude_record("human", None, "user", prompt, cwd=plan.cwd),
        claude_record(
            "working",
            "human",
            "assistant",
            [{"type": "tool_use", "id": "use", "name": "Read", "input": {}}],
        ),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "logicalParentUuid": "summary-reminder",
            "sessionId": "root",
        },
        claude_record(
            "summary",
            "boundary",
            "user",
            "This session is being continued from a previous conversation.",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
        *claude_turn_end("answer", "summary", "Finished after compacting."),
    ]
    return [event for record in records for event in adapter.feed(record)]


def test_claude_turn_compacted_midway_completes_before_the_deadline(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal, source = ClaudePromptTerminal(), FakeEventSource([])
    with claude_session(tmp_path, plan, source, terminal) as session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare("Fix the layout.")
        source.batches.extend([[], claude_compacted_turn(plan, "Fix the layout.")])
        result = session.deliver(intent)
        assert (result.state, result.turn_id, result.text) == (
            "completed",
            "human",
            "Finished after compacting.",
        )
        assert [event.kind for event in session.events].count("completed") == 1
        assert session.accepted_count == 1


class LostTypingTerminal(FakeTerminal):
    def __init__(self, typed):
        super().__init__([])
        self.typed = list(typed)
        self.value = ""
        self.render()

    def render(self):
        rule = "\u2500" * 40
        rows = self.value.split("\n")
        body = "\n".join(["\u276f " + rows[0], *("  " + row for row in rows[1:])])
        cursor = {"x": 2 + len(rows[-1]), "y": len(rows)}
        self.current = Screen(text=f"{rule}\n{body}\n{rule}", exited=None, cursor=cursor)

    def paste(self, text):
        self.pastes.append(text)
        kept = self.typed.pop(0)
        self.value += text if kept is None else text[:kept]
        self.render()
        if kept is not None or self.typed:
            raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")

    def clear_line(self):
        self.clears += 1
        cut = len(self.value) - 1 if self.value.endswith("\n") else self.value.rfind("\n") + 1
        self.value = self.value[: max(cut, 0)]
        self.render()

    def enter(self):
        self.enters += 1
        self.current = Screen(text="Working\n\u276f", exited=None)


PARAGRAPHS = "Fix the rows.\nKeep the padding."


def typing_session(tmp_path, plan, source, terminal):
    return DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(
            duration_seconds=5, command_seconds=0.2, max_polls=1000, poll_seconds=0.01
        ),
    )


@pytest.mark.parametrize(
    ("typed", "pastes", "clears"),
    [
        ([None, None], [PARAGRAPHS], 0),
        ([len("Fix the rows.\nKeep"), None], [PARAGRAPHS, PARAGRAPHS], 3),
    ],
)
def test_a_lost_typing_acknowledgement_is_reconciled_on_screen_before_enter(
    tmp_path, driver_plan, typed, pastes, clears
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal, source = LostTypingTerminal(typed), FakeEventSource([])
    with typing_session(tmp_path, plan, source, terminal) as session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare(PARAGRAPHS)
        adapter = ClaudeAdapter()
        records = [
            claude_record("human", None, "user", PARAGRAPHS, cwd=plan.cwd),
            *claude_turn_end("answer", "human", "Fixed."),
        ]
        source.batches.extend([[], [event for record in records for event in adapter.feed(record)]])
        result = session.deliver(intent)
        assert (result.state, result.text) == ("completed", "Fixed.")
        assert (terminal.pastes, terminal.clears, terminal.enters) == (pastes, clears, 1)
        assert session.accepted_count == 1


class BusyTypingTerminal(LostTypingTerminal):
    def __init__(self, typed):
        super().__init__(typed)
        self.busy = 0

    def paste(self, text):
        self.busy = 2
        super().paste(text)

    def state(self):
        if self.busy:
            self.busy -= 1
            raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")
        return super().state()


def test_screen_reads_lost_while_typing_settles_do_not_end_the_delivery(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal, source = BusyTypingTerminal([None, None]), FakeEventSource([])
    with typing_session(tmp_path, plan, source, terminal) as session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare(PARAGRAPHS)
        adapter = ClaudeAdapter()
        records = [
            claude_record("human", None, "user", PARAGRAPHS, cwd=plan.cwd),
            *claude_turn_end("answer", "human", "Fixed."),
        ]
        source.batches.extend([[], [event for record in records for event in adapter.feed(record)]])
        assert session.deliver(intent).state == "completed"
        assert (terminal.pastes, terminal.enters) == ([PARAGRAPHS], 1)


def test_a_retyped_prompt_that_still_cannot_be_confirmed_stops_without_enter(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = LostTypingTerminal([len("Fix"), len("Fix the")])
    with typing_session(tmp_path, plan, FakeEventSource([]), terminal) as session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare(PARAGRAPHS))
        assert (result.state, result.reason) == (
            "partial_input",
            "Exact composer content was not observed within bounds; Enter was not sent.",
        )
        assert (terminal.pastes, terminal.clears, terminal.enters) == (
            [PARAGRAPHS, PARAGRAPHS],
            1,
            0,
        )


class PrefilledComposerTerminal(LostTypingTerminal):
    def __init__(self, typed, existing):
        super().__init__(typed)
        self.value = existing
        self.render()


def test_a_lost_typing_acknowledgement_never_clears_preexisting_composer_text(
    tmp_path, driver_plan
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = PrefilledComposerTerminal([len("Fix"), None], "leftover ")
    with typing_session(tmp_path, plan, FakeEventSource([]), terminal) as session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare(PARAGRAPHS))
        assert (result.state, result.reason) == (
            "partial_input",
            "Exact composer content was not observed within bounds; Enter was not sent.",
        )
        assert (terminal.pastes, terminal.clears, terminal.enters) == ([PARAGRAPHS], 0, 0)


class CancelMidClearTerminal(LostTypingTerminal):
    def __init__(self, typed, cancelled):
        super().__init__(typed)
        self.cancel_event = cancelled

    def clear_line(self):
        super().clear_line()
        self.cancel_event.set()


def test_cancellation_mid_clear_stops_before_the_retype(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    cancelled = threading.Event()
    terminal = CancelMidClearTerminal([len("Fix"), None], cancelled)
    session = DriverSession(
        terminal,
        FakeEventSource([]),
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(
            duration_seconds=5, command_seconds=0.2, max_polls=1000, poll_seconds=0.01
        ),
        cancelled=cancelled,
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare(PARAGRAPHS))
        assert result.state == "cancelled"
        assert (terminal.pastes, terminal.clears, terminal.enters) == ([PARAGRAPHS], 1, 0)


class LostClearAckTerminal(LostTypingTerminal):
    def clear_line(self):
        self.clears += 1
        raise DeliveryUncertain("Terminal command ended without a clear acknowledgement.")


def test_a_lost_clear_acknowledgement_abandons_the_retype_without_enter(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = LostClearAckTerminal([len("Fix"), None])
    with typing_session(tmp_path, plan, FakeEventSource([]), terminal) as session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare(PARAGRAPHS))
        assert (result.state, result.reason) == (
            "partial_input",
            "Exact composer content was not observed within bounds; Enter was not sent.",
        )
        assert (terminal.pastes, terminal.clears, terminal.enters) == ([PARAGRAPHS], 1, 0)


class StuckClearTerminal(LostTypingTerminal):
    def clear_line(self):
        self.clears += 1
        self.value = self.value[:-1]
        self.render()


def test_a_composer_that_never_empties_within_the_press_cap_stops_without_enter(
    tmp_path, driver_plan
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = StuckClearTerminal([len("Fix the rows.\nKeep"), None])
    with typing_session(tmp_path, plan, FakeEventSource([]), terminal) as session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare(PARAGRAPHS))
        assert (result.state, result.reason) == (
            "partial_input",
            "Exact composer content was not observed within bounds; Enter was not sent.",
        )
        assert (terminal.pastes, terminal.clears, terminal.enters) == ([PARAGRAPHS], 6, 0)


class UnreadableComposerTerminal(LostTypingTerminal):
    def __init__(self, typed):
        super().__init__(typed)
        self.unreadable = False

    def paste(self, text):
        self.unreadable = True
        super().paste(text)

    def state(self):
        if self.unreadable:
            raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")
        return super().state()


def test_an_unreadable_composer_stops_distinctly_from_a_content_mismatch(tmp_path, driver_plan):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = UnreadableComposerTerminal([len("Fix")])
    with typing_session(tmp_path, plan, FakeEventSource([]), terminal) as session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare(PARAGRAPHS))
        assert (result.state, result.reason) == ("delivery_uncertain", COMPOSER_UNREADABLE)
        assert (terminal.pastes, terminal.clears, terminal.enters) == ([PARAGRAPHS], 0, 0)


def test_claude_slash_command_is_not_entered_while_the_menu_highlights_another(
    tmp_path, driver_plan
):
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = MenuTerminal("/review-plan")
    session = DriverSession(
        terminal,
        FakeEventSource([]),
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        result = session.deliver(session.prepare("/review", policy="curated-native-command"))
        assert (result.state, result.reason) == (
            "partial_input",
            "The command menu did not highlight the typed command; Enter was not sent.",
        )
        assert (terminal.pastes, terminal.enters) == (["/review"], 0)


class QuestionTerminal(FakeTerminal):
    def __init__(self, ui, *, ignored=()):
        super().__init__([])
        self.current = Screen(text="\u276f", exited=None)
        self.ui, self.ignored = ui, set(ignored)
        self.shown = False
        self.typed = []
        self.on_close = None

    def state(self):
        if self.shown and self.ui.open:
            return Screen(text=self.ui.render(), exited=None)
        return self.current

    def paste(self, text):
        if self.shown and self.ui.open:
            self.typed.append(text)
            self.ui.type(text)
            return
        self.pastes.append(text)
        self.current = Screen(text="\u276f " + text, exited=None)

    def clear_line(self):
        super().clear_line()
        if self.shown and self.ui.open:
            self.ui.typed[self.ui.index] = ""

    def enter(self):
        self.enters += 1
        self.shown = True
        self.current = Screen(text="Done.\n\n\u276f", exited=None)

    def press(self, key):
        super().press(key)
        if key in self.ignored or not self.ui.open:
            return
        self.ui.press(key)
        if not self.ui.open and self.on_close is not None:
            self.on_close()


AUTH = AskedQuestion(
    "Auth",
    "Which auth method should we use?",
    (("OAuth", "Use the provider flow"), ("API key", "Static key in config")),
)
STORE = AskedQuestion("Store", "Where do sessions live?", (("Redis", "Fast"), ("SQL", "Durable")))
FEATURES = AskedQuestion(
    "Features",
    "Which features ship first?",
    (("Search", "Full text"), ("Export", "CSV"), ("Sharing", "Links")),
    multi=True,
)


def asking_session(tmp_path, plan, ui, *, ignored=(), cancelled=None, terminal=None):
    plan = plan.model_copy(update={"agent": AgentKind.CLAUDE})
    terminal = terminal or QuestionTerminal(ui, ignored=ignored)
    source = FakeEventSource([])
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(
            duration_seconds=5, max_polls=400, poll_seconds=0.01, command_seconds=0.3
        ),
        cancelled=cancelled,
    )
    session.__enter__()
    assert session.start(plan, {}).state == "ready"
    intent = session.prepare("Add login")
    source.batches.append(
        [
            metadata(plan),
            native("started"),
            native("accepted", text="Add login", native_id="user"),
        ]
    )
    terminal.on_close = lambda: source.batches.append(
        [native("assistant", text="Added."), native("completed", text="Added.")]
    )
    assert session.deliver(intent).state == "structured_input"
    return session, terminal


def test_single_choice_moves_the_highlight_verifies_it_then_continues_the_turn(
    tmp_path, driver_plan
):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        asked = session.question()
        assert (asked.question, asked.options) == (AUTH.question, ("OAuth", "API key"))
        result = session.answer(asked, DialogSelection(choices=("API key",)))
        assert (result.state, result.text) == ("completed", "Added.")
        assert (terminal.keys, ui.answers, session.answered) == (
            ["Down", "Enter"],
            {0: "API key"},
            1,
        )
        assert (terminal.enters, session.accepted_count) == (1, 1)


def test_preview_question_is_answered_by_verified_arrow_navigation(tmp_path, driver_plan):
    scope = AskedQuestion(
        "Fix scope",
        "Which findings should I fix?",
        (("Fix everything (Recommended)", ""), ("Fix Critical only", ""), ("Skip", "")),
        previews=("1. Not an option\n2. Also not", "", ""),
    )
    ui = ClaudeQuestionUI((scope,))
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        asked = session.question()
        assert asked.options == ("Fix everything (Recommended)", "Fix Critical only", "Skip")
        result = session.answer(asked, DialogSelection(choices=("Skip",)))
        assert (result.state, terminal.keys, ui.answers) == (
            "completed",
            ["Down", "Down", "Enter"],
            {0: "Skip"},
        )


def test_multi_select_toggles_each_choice_submits_and_confirms_the_review(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((FEATURES,))
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        result = session.answer(session.question(), DialogSelection(choices=("Search", "Sharing")))
        assert result.state == "completed"
        assert terminal.keys == ["Space", "Down", "Down", "Space", "Down", "Down", "Enter", "Enter"]
        assert (ui.answers, ui.result) == ({0: "Search, Sharing"}, "submitted")


def test_free_text_is_typed_into_the_other_option(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        result = session.answer(session.question(), DialogSelection(text="Use mutual TLS"))
        assert result.state == "completed"
        assert (terminal.keys, terminal.typed) == (["Down", "Down", "Enter"], ["Use mutual TLS"])
        assert ui.answers == {0: "Use mutual TLS"}


def test_free_text_wrapped_across_rows_is_still_confirmed(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,), wrap_width=20)
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        text = "Use mutual TLS with a hardware token"
        result = session.answer(session.question(), DialogSelection(text=text))
        assert result.state == "completed"
        assert (terminal.keys, terminal.typed) == (["Down", "Down", "Enter"], [text])
        assert ui.answers == {0: text}


class DroppedFirstPasteTerminal(QuestionTerminal):
    def __init__(self, ui):
        super().__init__(ui)
        self.dialog_paste_calls = 0

    def paste(self, text):
        if self.shown and self.ui.open:
            self.dialog_paste_calls += 1
            if self.dialog_paste_calls == 1:
                self.typed.append(text)
                return
        super().paste(text)


def test_an_unconfirmable_free_text_answer_is_retyped_once_before_giving_up(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    dropping = DroppedFirstPasteTerminal(ui)
    session, terminal = asking_session(tmp_path, driver_plan, ui, terminal=dropping)
    with session:
        result = session.answer(session.question(), DialogSelection(text="Use mutual TLS"))
        assert result.state == "completed"
        assert (terminal.dialog_paste_calls, terminal.clears, terminal.typed) == (
            2,
            1,
            ["Use mutual TLS", "Use mutual TLS"],
        )
        assert ui.answers == {0: "Use mutual TLS"}


class LostDialogKeyTerminal(QuestionTerminal):
    def press(self, key):
        raise DeliveryUncertain("Fixture dialog key acknowledgement lost.")


def test_a_lost_dialog_key_acknowledgement_reaches_the_dialog_stop_handling(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(
        tmp_path, driver_plan, ui, terminal=LostDialogKeyTerminal(ui)
    )
    with session:
        result = session.answer(session.question(), DialogSelection(choices=("API key",)))
        assert (result.state, result.reason) == (
            "unsupported",
            "Dialog key delivery was not acknowledged; the dialog state is unknown.",
        )
        assert terminal.keys == []


def test_dialog_answer_past_the_deadline_stops_as_a_timeout(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui, ignored=("Down",))
    with session:
        asked = session.question()
        session.deadline = time.monotonic() - 1
        result = session.answer(asked, DialogSelection(choices=("API key",)))
        assert result.state == "timeout"
        assert terminal.keys == ["Down"]


def test_questions_are_answered_one_at_a_time_then_the_review_is_submitted(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH, STORE))
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        first = session.answer(session.question(), DialogSelection(choices=("OAuth",)))
        assert first.state == "structured_input"
        second = session.question()
        assert second.question == STORE.question
        assert session.answer(second, DialogSelection(choices=("SQL",))).state == "completed"
        assert terminal.keys == ["Enter", "Down", "Enter", "Enter"]
        assert (ui.answers, ui.result, session.answered) == (
            {0: "OAuth", 1: "SQL"},
            "submitted",
            2,
        )


def test_unverified_navigation_never_confirms_a_choice(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui, ignored=("Down",))
    with session:
        result = session.answer(session.question(), DialogSelection(choices=("API key",)))
        assert (result.state, result.reason) == (
            "unsupported",
            "Dialog selection could not be verified on screen.",
        )
        assert (terminal.keys, ui.answers, session.answered) == (["Down"], {}, 0)


def test_a_dialog_that_stays_open_after_enter_is_reported(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui, ignored=("Enter",))
    with session:
        result = session.answer(session.question(), DialogSelection(choices=("OAuth",)))
        assert (result.state, result.reason) == (
            "unsupported",
            "The question dialog did not advance after its answer.",
        )
        assert (terminal.keys, session.answered) == (["Enter"], 0)


def test_a_changed_dialog_is_not_answered(tmp_path, driver_plan):
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui)
    with session:
        stale = session.question().model_copy(update={"question": "Which database?"})
        result = session.answer(stale, DialogSelection(choices=("OAuth",)))
        assert (result.state, result.reason) == (
            "unsupported",
            "The question dialog changed before it was answered.",
        )
        assert terminal.keys == []


def test_cancellation_while_answering_reports_cancelled(tmp_path, driver_plan):
    cancelled = threading.Event()
    ui = ClaudeQuestionUI((AUTH,))
    session, terminal = asking_session(tmp_path, driver_plan, ui, cancelled=cancelled)
    with session:
        asked = session.question()
        cancelled.set()
        assert session.answer(asked, DialogSelection(choices=("API key",))).state == "cancelled"
        assert terminal.keys == []


def test_question_dialogs_are_only_read_for_claude(tmp_path, driver_plan):
    session, terminal, _ = build(tmp_path, driver_plan)
    with session:
        terminal.current = Screen(text=ClaudeQuestionUI((AUTH,)).render(), exited=None)
        assert session.question() is None


class ScreenTerminal(QuestionTerminal):
    def __init__(self, ui, *, ignored=(), closes_after=None):
        super().__init__(ui, ignored=ignored)
        self.closes_after = closes_after

    def state(self):
        if self.shown and self.ui.open and self.closes_after is not None:
            self.closes_after -= 1
            if self.closes_after < 0:
                self.ui.answer = "closed by itself"
                self.on_close()
        if self.shown and self.ui.open:
            return self.ui.screen()
        return self.current


def screen_session(tmp_path, plan, terminal):
    plan = plan.model_copy(update={"agent": AgentKind.CLAUDE})
    source = FakeEventSource([])
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(
            duration_seconds=5, max_polls=400, poll_seconds=0.01, command_seconds=0.3
        ),
    )
    session.__enter__()
    assert session.start(plan, {}).state == "ready"
    intent = session.prepare("Review the change")
    source.batches.append(
        [
            metadata(plan),
            native("started"),
            native("accepted", text="Review the change", native_id="user"),
        ]
    )
    terminal.on_close = lambda: source.batches.append(
        [native("assistant", text="Fixed."), native("completed", text="Fixed.")]
    )
    blocked = session.deliver(intent)
    assert (blocked.state, blocked.reason) == ("unsupported", UNRECOGNIZED)
    return session


def act(session, target=None, **action):
    return session.act(ScreenAction(**action), session.terminal.state().text, target)


def artifacts(tmp_path, name):
    return [
        json.loads(path.read_text())
        for path in sorted((tmp_path / "evidence").glob(f"*-{name}.json"))
    ]


def test_screen_keys_move_the_highlight_then_enter_continues_the_turn(tmp_path, driver_plan):
    ui = ScreenUI(FIX_SCOPE)
    terminal = ScreenTerminal(ui)
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        assert session.screen().text == FIX_SCOPE.rstrip("\n")
        moved = act(session, key="Down")
        assert (moved.state, moved.reason) == ("unsupported", UNRECOGNIZED)
        assert "\u276f 2. Don't fix anything" in moved.text
        assert "\u276f 1." in act(session, key="Up").text
        result = act(session, key="Enter")
        assert (result.state, result.text) == ("completed", "Fixed.")
        assert (terminal.keys, ui.answer, session.accepted_count) == (
            ["Down", "Up", "Enter"],
            "Fix all Should Fix",
            1,
        )
        assert session.screen() is None
    intents = artifacts(tmp_path, "screen-intent")
    assert [item["action"] for item in intents] == [
        {"key": "Down"},
        {"key": "Up"},
        {"key": "Enter"},
    ]
    assert intents[0]["screen"] == FIX_SCOPE.rstrip("\n")
    assert [item["changed"] for item in artifacts(tmp_path, "screen-result")] == [True] * 3


def test_screen_text_is_typed_into_the_focused_text_row(tmp_path, driver_plan):
    ui = ScreenUI(FIX_SCOPE)
    terminal = ScreenTerminal(ui)
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        act(session, key="3")
        typed = act(session, text="Only the prefix bug")
        assert "\u276f 3. Only the prefix bug" in typed.text
        assert act(session, key="Enter").state == "completed"
        assert (terminal.keys, terminal.typed, ui.answer) == (
            ["3", "Enter"],
            ["Only the prefix bug"],
            "Only the prefix bug",
        )


def test_a_digit_key_picks_its_numbered_option(tmp_path, driver_plan):
    ui = ScreenUI(FIX_SCOPE)
    session = screen_session(tmp_path, driver_plan, ScreenTerminal(ui))
    with session:
        assert act(session, key="2").state == "completed"
        assert ui.answer == "Don't fix anything"


def test_a_key_without_a_visible_effect_is_recorded_as_unchanged(tmp_path, driver_plan):
    ui = ScreenUI(FIX_SCOPE)
    terminal = ScreenTerminal(ui, ignored=("Down",))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        result = act(session, key="Down")
        assert (result.state, result.text) == ("unsupported", FIX_SCOPE.rstrip("\n"))
        assert artifacts(tmp_path, "screen-result")[0]["changed"] is False


def test_no_key_reaches_a_screen_that_became_a_permission_prompt(tmp_path, driver_plan):
    ui = ScreenUI(FIX_SCOPE)
    terminal = ScreenTerminal(ui)
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        ui.lines = EDIT_PERMISSION.split("\n")
        assert session.screen() is None
        result = act(session, key="1")
        assert (result.state, terminal.keys, artifacts(tmp_path, "screen-intent")) == (
            "unsupported",
            [],
            [],
        )


def test_permission_prompts_and_parsed_questions_are_never_answerable_screens(
    tmp_path, driver_plan
):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        for text in (EDIT_PERMISSION, ClaudeQuestionUI((AUTH,)).render()):
            terminal.ui = ScreenUI(text)
            assert session.screen() is None


class LostScreenKeyTerminal(ScreenTerminal):
    def press(self, key):
        raise DeliveryUncertain("Fixture screen key acknowledgement lost.")


def test_a_lost_screen_key_acknowledgement_stops_with_an_unknown_screen(tmp_path, driver_plan):
    session = screen_session(tmp_path, driver_plan, LostScreenKeyTerminal(ScreenUI(FIX_SCOPE)))
    with session:
        result = act(session, key="Enter")
        assert (result.state, result.reason) == (
            "unsupported",
            "Screen key delivery was not acknowledged; the screen state is unknown.",
        )


def test_waiting_on_a_busy_screen_returns_once_the_turn_moves_on(tmp_path, driver_plan):
    ui = ScreenUI(FIX_SCOPE)
    terminal = ScreenTerminal(ui, closes_after=5)
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        result = session.await_change(session.screen().text, time.monotonic() + 5)
        assert (result.state, result.text, terminal.keys) == ("completed", "Fixed.", [])


def test_closing_a_screen_returns_without_waiting_out_the_turn(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE))
    session = screen_session(tmp_path, driver_plan, terminal)
    finish = terminal.on_close
    terminal.on_close = lambda: None
    with session:
        assert act(session, key="1").state == "accepted"
        finish()
        assert session.reconcile(wait=True).text == "Fixed."


ESCALATING_CHOICE = (
    " Next step?\n\n\u276f 1. Keep reviewing\n  2. Always allow edits here\n"
    "  3. Type something.\n  4. Chat about this\n\n"
    "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel\n"
)


def test_keys_that_would_choose_a_mode_or_permission_option_are_never_sent(tmp_path, driver_plan):
    ui = ScreenUI(ESCALATING_CHOICE)
    terminal = ScreenTerminal(ui)
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        refused = act(session, key="2")
        assert (refused.state, refused.reason) == ("approval", SCREEN_ACTION_REFUSED)
        act(session, key="Down")
        assert act(session, key="Enter").reason == SCREEN_ACTION_REFUSED
        act(session, key="Up")
        assert act(session, key="Enter", target="Keep reviewing").state == "completed"
        assert (terminal.keys, ui.answer) == (["Down", "Up", "Enter"], "Keep reviewing")
    assert [item["option"] for item in artifacts(tmp_path, "screen-refused")] == [
        "Always allow edits here",
        "Always allow edits here",
    ]


def test_a_target_naming_a_mode_option_refuses_the_key(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        shown = session.screen().text
        result = session.act(ScreenAction(key="Enter"), shown, "Yes, and use auto mode")
        assert (result.state, result.reason, terminal.keys) == (
            "approval",
            SCREEN_ACTION_REFUSED,
            [],
        )


def test_an_action_judged_on_one_screen_is_not_applied_to_its_replacement(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        judged = session.screen().text
        terminal.ui = ScreenUI(CHANNEL)
        result = session.act(ScreenAction(key="2"), judged, "Don't fix anything")
        assert (result.state, result.reason, result.text) == (
            "unsupported",
            UNRECOGNIZED,
            CHANNEL.rstrip("\n"),
        )
        assert (terminal.keys, terminal.ui.answer) == ([], None)
        assert session.screen().text == CHANNEL.rstrip("\n")
    assert artifacts(tmp_path, "screen-intent") == []
    assert [item["action"] for item in artifacts(tmp_path, "screen-stale")] == [{"key": "2"}]


def test_a_busy_wait_on_an_unchanging_screen_ends_at_its_bound(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        started = time.monotonic()
        result = session.await_change(session.screen().text, started + 0.2)
        assert (result.state, result.reason) == ("unsupported", SCREEN_WAIT_EXPIRED)
        assert time.monotonic() - started < 2


class ExitedScreenUI(ScreenUI):
    def screen(self):
        return super().screen().model_copy(update={"exited": 1})


def test_no_key_reaches_the_screen_of_an_exited_subject(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        terminal.ui = ExitedScreenUI(FIX_SCOPE)
        result = act(session, key="Down")
        assert (result.state, terminal.keys, terminal.ui.focus) == ("exited", [], 0)
        assert session.exit_observation(terminal.state()).state == "exited"


STATUS = "  main \u00b7 Opus"
COMPOSER = (
    "\u25cf Fixed the prefix bug.\n\n\u2500\u2500\u2500\u2500\n\u276f \n"
    "\u2500\u2500\u2500\u2500\n" + STATUS
)


def test_the_composer_is_never_an_open_screen_even_with_the_dialog_footer(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE + STATUS))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        assert session.screen() is not None
        terminal.ui.answer = "closed"
        terminal.current = Screen(text=COMPOSER, exited=None)
        assert session.screen() is None


def test_a_new_turn_forgets_the_previous_screen_footer(tmp_path, driver_plan):
    terminal = ScreenTerminal(ScreenUI(FIX_SCOPE + STATUS))
    session = screen_session(tmp_path, driver_plan, terminal)
    with session:
        assert session.screen() is not None
        terminal.ui.answer = "closed"
        terminal.on_close()
        assert session.reconcile(wait=True).state == "completed"
        session.prepare("Now add a test")
        terminal.current = Screen(text="\u273b Thinking\u2026\n\n" + STATUS, exited=None)
        assert session.screen() is None
