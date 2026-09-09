import json
from pathlib import Path

import pytest

from dryheave.drivers.dialogs import CODEX_APPROVALS, composer, composer_matches, dialog
from dryheave.drivers.models import Screen
from dryheave.models import AgentKind

CODEX_DIALOGS = json.loads(Path("tests/fixtures/codex-dialogs.json").read_text())


@pytest.mark.parametrize("name", ["trust", "exec", "patch", "network", "permissions", "hooks"])
@pytest.mark.parametrize("history", ["", "\u203a Prior prompt\n• Prior answer\n\n"])
def test_pinned_upstream_approval_layouts_require_operator_policy(name, history):
    text = history + CODEX_DIALOGS[name]
    assert dialog(AgentKind.CODEX, Screen(text=text, exited=None)).state == "approval"


@pytest.mark.parametrize("name", ["freeform", "options"])
@pytest.mark.parametrize("history", ["", "\u203a Prior prompt\n• Prior answer\n\n"])
def test_pinned_structured_input_layouts(name, history):
    result = dialog(AgentKind.CODEX, Screen(text=history + CODEX_DIALOGS[name], exited=None))
    assert result.state == "structured_input"


@pytest.mark.parametrize(
    "phrase",
    [
        *CODEX_APPROVALS,
        "Choose how you want to use Codex.",
        "Sign in with ChatGPT",
        "Select login method",
        "Submit with unanswered questions?",
        "Input disabled.",
        "Question 1/1",
        "enter to submit answer",
        "esc to cancel",
        "enter to confirm",
        "select an option",
        "permission required",
        "approval required",
        "elicitation",
    ],
)
@pytest.mark.parametrize("location", ["composer", "transcript"])
def test_dialog_phrases_in_conversation_are_ordinary_text(phrase, location):
    text = (
        f"\u203a Update the confirmation label to “{phrase}”"
        if location == "composer"
        else f"• The UI uses this label:\n  {phrase}\n\n\u203a\n\n? for shortcuts"
    )
    assert dialog(AgentKind.CODEX, Screen(text=text, exited=None)) is None


@pytest.mark.parametrize("name", CODEX_DIALOGS)
def test_historical_dialog_with_current_composer_is_not_active(name):
    text = CODEX_DIALOGS[name] + "\n\n\u203a\n\n? for shortcuts"
    assert dialog(AgentKind.CODEX, Screen(text=text, exited=None)) is None


@pytest.mark.parametrize("name", CODEX_DIALOGS)
def test_quoted_dialog_layout_in_composer_is_not_active(name):
    quoted = "\n".join("  " + line for line in CODEX_DIALOGS[name].splitlines())
    text = "\u203a Explain this dialog:\n" + quoted + "\n\n? for shortcuts"
    assert dialog(AgentKind.CODEX, Screen(text=text, exited=None)) is None


def test_claude_approval_requires_active_controls():
    text = "Do you want to proceed?\n\n\u276f 1. Yes\n  2. No\n\nEsc to cancel"
    assert dialog(AgentKind.CLAUDE, Screen(text=text, exited=None)).state == "approval"
    for ordinary in (f"\u276f Update the label: {text.splitlines()[0]}", text + "\n\n\u276f"):
        assert dialog(AgentKind.CLAUDE, Screen(text=ordinary, exited=None)) is None


def test_large_paste_placeholder_and_multiline_composer():
    screen = Screen(
        text="Old transcript\n\u203a [Pasted Content 1001 chars]\n\n? for shortcuts", exited=None
    )
    assert composer_matches(AgentKind.CODEX, screen, "x" * 1001)
    assert not composer_matches(AgentKind.CODEX, screen, "x" * 1002)
    assert (
        composer(
            AgentKind.CODEX,
            Screen(text="\u203a old\n\u203a first\n  second\n\n? for shortcuts", exited=None),
        )
        == "first\nsecond"
    )


def test_auth_and_unanswered_confirmation_are_unsupported():
    for text in (CODEX_DIALOGS["auth"], CODEX_DIALOGS["confirm"]):
        assert dialog(AgentKind.CODEX, Screen(text=text, exited=None)).state == "unsupported"


@pytest.mark.parametrize("cursor", [{}, {"x": 2, "y": 0}, {"x": 16, "y": 0}, {"x": 8, "y": 3}])
@pytest.mark.parametrize("history", ["", "\u203a Prior prompt\n• Prior answer\n\n"])
@pytest.mark.parametrize("footer", ["", "\n\n? for shortcuts", "\n─────────────\n"])
def test_disabled_input_layout_does_not_require_an_active_cursor(cursor, history, footer):
    text = history + "\u203a Input disabled." + footer
    result = dialog(AgentKind.CODEX, Screen(text=text, exited=None, cursor=cursor))
    assert (result.state, result.reason) == ("unsupported", "Native input is disabled.")


@pytest.mark.parametrize(
    "text",
    [
        "\u203a Explain this dialog:\n  \u203a Input disabled.",
        "\u203a Explain this dialog:\n  \u203a Input disabled.\n\n? for shortcuts",
        "• The UI uses this label:\n  \u203a Input disabled.\n\n\u203a\n\n? for shortcuts",
        "\u203a Input disabled.\n• That is the current label.",
        "\u203a Input disabled.\n  Use this text as the label.",
        "\u203a Input disabled.\n\n\u203a Ask Codex to do anything",
        "> Input disabled.",
        "\u00bb Input disabled.",
    ],
)
def test_quoted_disabled_layout_is_ordinary_text(text):
    assert dialog(AgentKind.CODEX, Screen(text=text, exited=None, cursor={"x": 16, "y": 0})) is None


def test_pinned_empty_placeholder_needs_empty_cursor_position():
    text = "Welcome\n\u00bb Ask Codex to do anything"
    assert composer(AgentKind.CODEX, Screen(text=text, exited=None, cursor={"x": 2, "y": 1})) == ""
    assert (
        composer(AgentKind.CODEX, Screen(text=text, exited=None, cursor={"x": 25, "y": 1}))
        == "Ask Codex to do anything"
    )


def test_blank_paragraph_and_word_wrapped_input_match_visible_projection():
    text = "Create a greeting command for every supported input.\n\nAsk first."
    rendered = "\u203a Create a greeting\n  command for every\n  supported input.\n\n  Ask first.\n\n? for shortcuts"
    screen = Screen(text=rendered, cols=20, exited=None, cursor={"x": 12, "y": 4})
    assert composer_matches(AgentKind.CODEX, screen, text)
    assert not composer_matches(AgentKind.CODEX, screen, text.replace("Ask first.", "Ask last."))


def test_hard_wrapping_and_nested_prompt_marker_preserve_content():
    text = "x" * 118 + "\n\n\u203a keep this marker"
    screen = Screen(
        text="\u203a " + "x" * 117 + "\n  x\n\n  \u203a keep this marker\n\n? for shortcuts",
        exited=None,
    )
    assert composer_matches(AgentKind.CODEX, screen, text)
