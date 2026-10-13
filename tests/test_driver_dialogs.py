import json
from pathlib import Path

import pytest

from conftest import AskedQuestion, ClaudeQuestionUI
from dryheave.drivers.dialogs import (
    CODEX_APPROVALS,
    command_menu,
    command_ready,
    composer,
    composer_matches,
    dialog,
    question_dialog,
    submittable,
)
from dryheave.drivers.models import DialogRow, Screen
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


def test_claude_composer_wrapped_narrower_than_the_terminal_still_matches():
    reply = (
        "looks good. one thing \u2014 when folder path is hidden but branch is still shown, "
        "does the branch move up into the folder line's position or does it leave an empty gap?"
    )
    screen = Screen(
        text="\u2500"
        * 120
        + "\n\u276f looks good. one thing \u2014 when folder path is hidden but branch is still"
        " shown, does the branch move up into the\n  folder line's position or does it leave an"
        " empty gap?\n" + "\u2500" * 120,
        exited=None,
        cols=120,
    )
    assert composer_matches(AgentKind.CLAUDE, screen, reply)
    assert not composer_matches(AgentKind.CLAUDE, screen, reply.replace("empty gap", "gap"))


def test_claude_suggestion_placeholder_is_an_empty_composer():
    text = "\u2500" * 20 + '\n\u276f\xa0Try "how does <filepath> work?"\n' + "\u2500" * 20
    placeholder = Screen(text=text, exited=None, cursor={"x": 2, "y": 1})
    typed = Screen(text=text, exited=None, cursor={"x": 34, "y": 1})
    assert composer(AgentKind.CLAUDE, placeholder) == ""
    assert composer(AgentKind.CLAUDE, typed) == 'Try "how does <filepath> work?"'


@pytest.mark.parametrize("suggestion", ["go ahead", "commit this"])
def test_claude_suggested_next_prompt_is_an_empty_composer(suggestion):
    text = "\u2500" * 20 + f"\n\u276f\xa0{suggestion}\n" + "\u2500" * 20
    assert composer(AgentKind.CLAUDE, Screen(text=text, exited=None, cursor={"x": 2, "y": 1})) == ""
    typed = Screen(text=text, exited=None, cursor={"x": 2 + len(suggestion), "y": 1})
    assert composer(AgentKind.CLAUDE, typed) == suggestion


def test_claude_collapsed_multiline_paste_matches_its_line_count():
    prompt = "Can you add some settings:\n\nshow folder path\nshow branch\nreplace home path with ~"
    screen = Screen(
        text="\u2500" * 20 + "\n\u276f [Pasted text #1 +4 lines]\n" + "\u2500" * 20,
        exited=None,
    )
    assert composer_matches(AgentKind.CLAUDE, screen, prompt)
    assert not composer_matches(AgentKind.CLAUDE, screen, prompt + "\nextra line")
    assert not composer_matches(AgentKind.CODEX, screen, prompt)


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


def test_claude_theme_picker_is_an_unrecognized_dialog():
    screen = Screen.model_validate_json(
        Path("tests/fixtures/claude-2.1.281-theme-picker.json").read_text()
    )
    result = dialog(AgentKind.CLAUDE, screen)
    assert (result.state, result.reason, result.text) == (
        "unsupported",
        "Unrecognized native dialog requires input.",
        screen.text,
    )


def test_first_option_selected_with_cursor_on_marker_is_an_unrecognized_dialog():
    text = "Pick a mode\n\n \u276f 1. Fast\n   2. Slow\n\n Syntax theme: Monokai"
    result = dialog(AgentKind.CLAUDE, Screen(text=text, exited=None, cursor={"x": 1, "y": 2}))
    assert (result.state, result.reason) == (
        "unsupported",
        "Unrecognized native dialog requires input.",
    )


@pytest.mark.parametrize("cursor", [{}, {"x": 11, "y": 3}])
def test_numbered_prompt_in_claude_composer_remains_composer_text(cursor):
    text = "────\n\n\u276f 1. Fix it\n  2. Test it\n\n────\n? for shortcuts"
    screen = Screen(text=text, exited=None, cursor=cursor)
    assert dialog(AgentKind.CLAUDE, screen) is None
    assert composer(AgentKind.CLAUDE, screen) == "1. Fix it\n2. Test it"
    assert composer_matches(AgentKind.CLAUDE, screen, "1. Fix it\n2. Test it")


RULE = "\u2500" * 120
FOOTER = "  \u23f5\u23f5 auto mode on (shift+tab to cycle)"


def menu_screen(typed, entries, *, below=False, history=""):
    rows = [
        f"  {'\u276f' if index == 0 else ' '} {name:<48}{summary}"
        for index, (name, summary) in enumerate(entries)
    ]
    box = [RULE, f"\u276f {typed}", RULE]
    lines = [*box, *rows] if below else [*rows, *box, FOOTER]
    text = history + "\n".join(lines)
    row = text.splitlines().index(f"\u276f {typed}")
    return Screen(text=text, exited=None, cursor={"x": 2 + len(typed), "y": row})


REVIEW_ENTRIES = (
    ("/review-plan", "Multi-agent review of implementation plans."),
    ("/review-perf", "Performance analysis for algorithmic complexity."),
)


@pytest.mark.parametrize("below", [False, True])
def test_claude_composer_matches_through_an_open_command_menu(below):
    entries = (
        ("/code-review", "Clean up code comments, then review code."),
        ("/superpowers:receiving-code-review", "(superpowers) Use when receiving review."),
    )
    screen = menu_screen("/code-review", entries, below=below, history="\u276f Earlier ask\n\n")
    assert composer(AgentKind.CLAUDE, screen) == "/code-review"
    assert dialog(AgentKind.CLAUDE, screen) is None
    assert command_menu(screen).highlighted == "/code-review"
    assert submittable(AgentKind.CLAUDE, screen, "/code-review")


@pytest.mark.parametrize("below", [False, True])
def test_claude_command_menu_highlighting_another_command_blocks_submission(below):
    screen = menu_screen("/review", REVIEW_ENTRIES, below=below)
    assert composer_matches(AgentKind.CLAUDE, screen, "/review")
    assert command_menu(screen).highlighted == "/review-plan"
    assert not command_ready(screen, "/review")
    assert not submittable(AgentKind.CLAUDE, screen, "/review")


def test_claude_command_menu_without_a_match_blocks_submission():
    screen = Screen(
        text=f'  No commands match "/zzqq"\n{RULE}\n\u276f /zzqq\n{RULE}\n{FOOTER}',
        exited=None,
        cursor={"x": 7, "y": 2},
    )
    assert command_menu(screen).highlighted is None
    assert not command_ready(screen, "/zzqq")


def test_claude_closed_command_menu_submits_only_a_command_with_arguments():
    typed = "/code-review --quick"
    screen = Screen(
        text="\n".join([RULE, f"\u276f {typed}", RULE, FOOTER]),
        exited=None,
        cursor={"x": 2 + len(typed), "y": 1},
    )
    bare = Screen(
        text=f"{RULE}\n\u276f /code-review\n{RULE}\n{FOOTER}",
        exited=None,
        cursor={"x": 14, "y": 1},
    )
    assert command_menu(screen) is None
    assert submittable(AgentKind.CLAUDE, screen, typed)
    assert not submittable(AgentKind.CLAUDE, bare, "/code-review")


def test_command_like_transcript_rows_away_from_the_composer_are_not_a_menu():
    history = "  \u276f /usr/bin/env  python\n    /etc/hosts  entries\n\n"
    screen = Screen(
        text=history + f"{RULE}\n\u276f /code-review --quick\n{RULE}\n{FOOTER}",
        exited=None,
        cursor={"x": 22, "y": 4},
    )
    assert command_menu(screen) is None
    assert submittable(AgentKind.CLAUDE, screen, "/code-review --quick")


def test_plain_claude_prompt_ignores_the_command_menu():
    screen = menu_screen("review", REVIEW_ENTRIES)
    assert submittable(AgentKind.CLAUDE, screen, "review")


DIALOG_FOOTER = "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel"
SINGLE_QUESTION = f"""
\u276f Add login to the app

\u25cf I need one decision first.

 {RULE}
 \u2610 Auth

 Which auth method should we use?

   1. OAuth
      Use the provider flow
 \u276f 2. API key
      Static key in config
   3. Type something.
 {RULE}
   4. Chat about this

 {DIALOG_FOOTER}
"""
MULTI_SELECT = f"""
 {RULE}
 \u2190 \u2610 Features  \u2714 Submit \u2192

 Which features ship first?

   1. [\u2714] Search
           Full text search
 \u276f 2. [ ] Export
           CSV export
   3. [ ] Type something
       Submit
 {RULE}
   4. Chat about this

 {DIALOG_FOOTER}
"""


def parse(text):
    return question_dialog(Screen(text=text, exited=None))


def test_single_select_question_is_parsed_from_the_screen():
    found = parse(SINGLE_QUESTION)
    assert (found.kind, found.question, found.options, found.multi_select) == (
        "question",
        "Which auth method should we use?",
        ("OAuth", "API key"),
        False,
    )
    assert (found.allows_text, found.focused) == (True, 1)
    assert found.rows[2] == DialogRow(kind="text", label="Type something.")
    assert dialog(AgentKind.CLAUDE, Screen(text=SINGLE_QUESTION, exited=None)).state == (
        "structured_input"
    )


def test_multi_select_question_reports_checkboxes_and_its_submit_row():
    found = parse(MULTI_SELECT)
    assert (found.question, found.options, found.multi_select, found.focused) == (
        "Which features ship first?",
        ("Search", "Export"),
        True,
        1,
    )
    assert [(row.kind, row.label, row.checked) for row in found.rows] == [
        ("option", "Search", True),
        ("option", "Export", False),
        ("text", "Type something", False),
        ("submit", "Submit", None),
    ]


def test_typed_free_text_and_a_committed_tick_are_read_as_labels():
    text = (
        SINGLE_QUESTION.replace("1. OAuth", "1. OAuth \u2714")
        .replace("\u276f 2. API key", "  2. API key")
        .replace("   3. Type something.", " \u276f 3. Use mutual TLS")
    )
    found = parse(text)
    assert (found.options, found.focused, found.rows[2].label) == (
        ("OAuth", "API key"),
        2,
        "Use mutual TLS",
    )


def test_later_question_of_several_is_parsed_below_its_tab_bar():
    ui = ClaudeQuestionUI(
        (
            AskedQuestion("Auth", "Which auth method?", (("OAuth", "Flow"), ("Key", "Static"))),
            AskedQuestion(
                "Store", "Where do sessions live?", (("Redis", "Fast"), ("SQL", "Durable"))
            ),
        )
    )
    ui.press("Enter")
    found = parse(ui.render())
    assert "\u2612 Auth" in ui.render()
    assert (found.question, found.options, found.focused) == (
        "Where do sessions live?",
        ("Redis", "SQL"),
        0,
    )


def test_unnumbered_options_are_parsed_by_column():
    question = AskedQuestion("Auth", "Which auth method?", (("OAuth", "Flow"), ("Key", "Static")))
    found = parse(ClaudeQuestionUI((question,), numbered=False).render())
    assert (found.options, found.allows_text, found.focused) == (("OAuth", "Key"), True, 0)


def test_review_screen_is_parsed_and_reports_unanswered_questions():
    ui = ClaudeQuestionUI(
        (
            AskedQuestion("Auth", "Which auth method?", (("OAuth", "Flow"), ("Key", "Static"))),
            AskedQuestion("Store", "Where?", (("Redis", "Fast"), ("SQL", "Durable"))),
        )
    )
    ui.press("Enter")
    ui.press("Enter")
    found = parse(ui.render())
    assert (found.kind, found.options, found.focused, found.complete) == (
        "review",
        ("Submit answers", "Cancel"),
        0,
        True,
    )
    warned = ui.render().replace(
        " Review your answers",
        " Review your answers\n\n ⚠ You have not answered all questions",
    )
    assert parse(warned).complete is False


@pytest.mark.parametrize(
    "text",
    [
        SINGLE_QUESTION + "\n\u276f",
        SINGLE_QUESTION.replace("4. Chat about this", "4. Something else"),
        SINGLE_QUESTION.replace(f" {RULE}\n   4.", "   4."),
        SINGLE_QUESTION.replace("   2. API key", "   7. API key").replace("\u276f 2.", "\u276f 7."),
        SINGLE_QUESTION.replace("   4. Chat about this", " \u276f Chat about this"),
        f"\u2022 Claude Code shows:\n  Chat about this\n  {DIALOG_FOOTER}\n\n\u276f\n\n? for shortcuts",
    ],
)
def test_question_like_text_that_is_not_an_active_dialog_is_not_parsed(text):
    assert parse(text) is None


STARTUP_TRUST = f"""
 {RULE}
 Accessing workspace:

 /tmp/project

 Quick safety check: Is this a project you created or one you trust?

 \u276f No, exit
   Yes, I trust this folder

 Enter to confirm \u00b7 Esc to cancel
"""
STARTUP_IMPORTS = f"""
 {RULE}
 Allow external CLAUDE.md file imports?

 This project's CLAUDE.md or .claude/rules imports files outside the current working directory.

 \u276f Yes, allow external imports
   No, disable external imports

 Enter to confirm \u00b7 Esc to cancel
"""


@pytest.mark.parametrize("text", [STARTUP_TRUST, STARTUP_IMPORTS])
def test_unnumbered_startup_prompts_need_operator_policy_and_are_not_questions(text):
    screen = Screen(text=text, exited=None)
    assert parse(text) is None
    assert dialog(AgentKind.CLAUDE, screen).state == "approval"


def test_unknown_unnumbered_claude_selection_is_unrecognized():
    text = STARTUP_TRUST.replace("Accessing workspace:", "Pick a color").replace(
        "Quick safety check: Is this a project you created or one you trust?", "Any color works."
    )
    assert dialog(AgentKind.CLAUDE, Screen(text=text, exited=None)).state == "unsupported"


def test_claude_permission_prompt_remains_an_approval():
    text = "Bash command\n\n  rm -rf build\n\nDo you want to proceed?\n\u276f 1. Yes\n  2. No\n\nEsc to cancel"
    assert parse(text) is None
    assert dialog(AgentKind.CLAUDE, Screen(text=text, exited=None)).state == "approval"


FIX_SCOPE = AskedQuestion(
    "Fix scope",
    "Which findings should I fix?",
    (("Fix everything (Recommended)", ""), ("Fix Critical only", ""), ("Don't fix anything", "")),
    previews=(
        "codex.py:144  strip one leading '='\n\u276f 2. Not an option\n3. Chat about this",
        "",
        "",
    ),
)


def preview_screen(*presses):
    ui = ClaudeQuestionUI((FIX_SCOPE,))
    for key in presses:
        ui.press(key)
    return ui.render()


def test_side_by_side_preview_question_reads_options_from_the_left_column():
    text = preview_screen()
    found = parse(text)
    assert (found.question, found.options, found.focused, found.multi_select) == (
        "Which findings should I fix?",
        ("Fix everything (Recommended)", "Fix Critical only", "Don't fix anything"),
        0,
        False,
    )
    assert dialog(AgentKind.CLAUDE, Screen(text=text, exited=None)).state == "structured_input"
    assert (
        parse(preview_screen("Down")).focused,
        parse(preview_screen("Down", "Down")).focused,
    ) == (
        1,
        2,
    )


def test_focus_on_the_unnumbered_chat_row_of_a_preview_question_is_no_option():
    assert parse(preview_screen("Down", "Down", "Down")).focused is None


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace(" · n to add notes", ""),
        lambda text: "\n".join(line[:31].rstrip() for line in text.splitlines()),
        lambda text: text.replace("Fix everything" + " " * 15, "Fix everything" + " " * 12 + "gap"),
        lambda text: text.replace("Notes: press n to add notes", ""),
    ],
    ids=["footer", "no-pane", "gap", "no-notes"],
)
def test_preview_like_layouts_that_do_not_fit_stay_unrecognized(change):
    text = change(preview_screen("Down"))
    assert parse(text) is None
    assert dialog(AgentKind.CLAUDE, Screen(text=text, exited=None)).state == "unsupported"
