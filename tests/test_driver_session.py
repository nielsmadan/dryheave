import json
import threading
from pathlib import Path

import pytest

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.claude import ClaudeAdapter
from dryheave.drivers.fake import FakeEventSource, FakeTerminal
from dryheave.drivers.models import DriverLimits, NativeEvent, Screen
from dryheave.drivers.session import DriverSession
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
        assert session.deliver(session.prepare("Hello")).state == "partial_input"
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
