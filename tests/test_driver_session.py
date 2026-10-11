import json
import threading
import time
from pathlib import Path

import pytest

from conftest import AskedQuestion, ClaudeQuestionUI
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.base import DeliveryUncertain
from dryheave.drivers.claude import ClaudeAdapter
from dryheave.drivers.fake import FakeEventSource, FakeTerminal
from dryheave.drivers.models import DialogSelection, DriverLimits, NativeEvent, Screen
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
