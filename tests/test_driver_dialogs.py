import json
from pathlib import Path

import pytest

from conftest import EDIT_PERMISSION, AskedQuestion, ClaudeQuestionUI, ScreenUI
from conftest import FIX_SCOPE as FIX_SCOPE_SCREEN
from dryheave.drivers.dialogs import (
    CODEX_APPROVALS,
    UNRECOGNIZED,
    answerable_screen,
    command_menu,
    command_ready,
    composer,
    composer_matches,
    composer_ready,
    dialog,
    escalation,
    permission_like,
    question_chrome,
    question_dialog,
    screen_identity,
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


def test_claude_command_menu_above_a_status_widget_and_blank_line_is_read():
    lines = [
        f"  \u276f {'/code-review':<48}Clean up comments and mechanical loose ends, then review",
        f"    {'':<48}comprehensive and quick modes.",
        f"    {'/plan':<48}Lightweight planning workflow.",
        "\u25cf no phase yet" + " " * 90 + "[-]",
        "",
        RULE,
        "\u276f /code-review",
        RULE,
        "  \U0001f969 \u2502 \U0001f916 sonnet5 low \u2502 \U0001f9e0 0%",
        FOOTER,
    ]
    screen = Screen(text="\n".join(lines), exited=None, cursor={"x": 14, "y": 6})
    assert command_menu(screen).highlighted == "/code-review"
    assert submittable(AgentKind.CLAUDE, screen, "/code-review")


def test_claude_reply_text_above_a_closed_composer_is_not_a_command_menu():
    lines = [
        "\u25cf Ran the review.",
        "  /code-review found nothing to fix.",
        "",
        RULE,
        "\u276f /code-review",
        RULE,
        FOOTER,
    ]
    screen = Screen(text="\n".join(lines), exited=None, cursor={"x": 14, "y": 4})
    assert command_menu(screen) is None
    assert not submittable(AgentKind.CLAUDE, screen, "/code-review")


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


def test_the_claude_fix_scope_question_is_an_unparsed_answerable_screen():
    screen = ScreenUI(FIX_SCOPE_SCREEN).screen()
    assert question_dialog(screen) is None
    assert (
        dialog(AgentKind.CLAUDE, screen).reason,
        answerable_screen(AgentKind.CLAUDE, screen),
    ) == (
        UNRECOGNIZED,
        True,
    )


@pytest.mark.parametrize(
    ("agent", "text", "answerable"),
    [
        (AgentKind.CODEX, CODEX_DIALOGS["freeform"], True),
        (AgentKind.CODEX, CODEX_DIALOGS["options"], True),
        (AgentKind.CODEX, CODEX_DIALOGS["exec"], False),
        (AgentKind.CODEX, CODEX_DIALOGS["trust"], False),
        (AgentKind.CODEX, CODEX_DIALOGS["auth"], False),
        (AgentKind.CODEX, CODEX_DIALOGS["confirm"], False),
        (AgentKind.CLAUDE, EDIT_PERMISSION, False),
        (
            AgentKind.CLAUDE,
            ClaudeQuestionUI(
                (
                    AskedQuestion(
                        "A",
                        "Which?",
                        (("X", ""), ("Y", "")),
                    ),
                )
            ).render(),
            False,
        ),
        (AgentKind.CLAUDE, "Done.\n\n\u276f", False),
    ],
)
def test_only_unclassified_input_screens_are_answerable(agent, text, answerable):
    assert answerable_screen(agent, Screen(text=text, exited=None, cursor={"x": 0, "y": 0})) is (
        answerable
    )


def test_an_unclassified_permission_prompt_is_recognised_by_its_wording():
    screen = Screen(text=EDIT_PERMISSION, exited=None, cursor={"x": 0, "y": 0})
    assert (dialog(AgentKind.CLAUDE, screen).reason, permission_like(EDIT_PERMISSION)) == (
        UNRECOGNIZED,
        True,
    )
    assert not permission_like(FIX_SCOPE_SCREEN)


@pytest.mark.parametrize(
    "line",
    [
        "Ready to code?",
        "Claude has written up a plan and is ready to execute. Would you like to proceed?",
        "\u276f 1. Yes, and use auto mode",
        "  2. Yes, auto-accept edits",
        "Yes, clear context and bypass permissions",
        "Yes, and switch to auto mode",
        "Exit plan mode?",
        "Enter plan mode?",
        'Use skill "code-review"?',
        "Use this skill?",
        "Run a dynamic workflow?",
        "  2. Yes, and don\u2019t ask again for: npm run build:*",
        "Yes, and don\u2019t ask again for any Bash command",
        "Do not ask again.",
        "  3. No, and tell Claude what to do differently (esc)",
        "  2. Yes, allow all edits during this session (shift+tab)",
        "Do you trust the files in this folder?",
        "Trust this plugin directory?",
        "Do you want to proceed?",
        "Do you want to make this edit to greet.py?",
        "Do you want to allow Claude to fetch this content?",
        "Do you want to use this API key?",
        "Allow external CLAUDE.md file imports?",
        "Select login method",
        "Sign in with Google",
        "Approve app tool call?",
        "\u203a 2. Allow for this session  Run the tool and remember this choice for this session.",
        "  3. Allow and don't ask me again  Run the tool and remember this choice for future tool calls.",
        "  3. Yes, grant these permissions for this session (a)",
        'Do you want to approve network access to "example.com"?',
        "Do you want to download and install dependencies for this project?",
        "Sign in with ChatGPT",
    ],
)
def test_shipped_permission_and_mode_wording_is_recognised(line):
    assert permission_like(f"Earlier transcript\n\n{line}\n")


CONFIRM = "\n\n Enter to confirm \u00b7 Esc to cancel\n"
SHIPPED_PROMPTS = {
    "plan-exit": (
        AgentKind.CLAUDE,
        " Ready to code?\n\n Here is Claude's plan:\n Add a greet helper with a test.\n\n"
        " Claude has written up a plan and is ready to execute. Would you like to proceed?\n\n"
        " \u276f 1. Yes, and use auto mode\n   2. Yes, manually approve edits\n"
        "   3. No, keep planning" + CONFIRM,
    ),
    "skill": (
        AgentKind.CLAUDE,
        ' Use skill "code-review"?\n'
        " Claude may use instructions, code, or files from this Skill.\n\n"
        " \u276f 1. Yes\n   2. No" + CONFIRM,
    ),
    "workflow": (
        AgentKind.CLAUDE,
        " Run a dynamic workflow?\n\n \u276f 1. Yes, run it\n   2. Yes, and switch to auto mode\n"
        "   3. No" + CONFIRM,
    ),
    "bash-rule": (
        AgentKind.CLAUDE,
        " Bash command\n\n   npm run build\n\n \u276f 1. Yes\n"
        "   2. Yes, and don\u2019t ask again for: npm run build:*\n   3. No" + CONFIRM,
    ),
    "codex-app-tool": (
        AgentKind.CODEX,
        (
            "  Approve app tool call?\n  github wants to run create_issue.\n\n"
            "\u203a 1. Allow  Run the tool and continue.\n"
            "  2. Allow for this session  Run the tool and remember this choice for this session.\n"
            "  3. Allow and don't ask me again  Run the tool and remember this choice for future"
            " tool calls.\n  4. Cancel  Cancel this tool call.\n\n"
            "  Press enter to confirm or esc to cancel\n"
        ),
    ),
}


@pytest.mark.parametrize("name", sorted(SHIPPED_PROMPTS))
def test_shipped_permission_and_mode_prompts_never_reach_the_simulated_user(name):
    agent, text = SHIPPED_PROMPTS[name]
    screen = Screen(text=text, exited=None, cursor={"x": 0, "y": 0})
    assert (dialog(agent, screen).reason, answerable_screen(agent, screen)) == (
        UNRECOGNIZED,
        False,
    )


CODEX_CONFIRM = "\n\n  Press enter to confirm or esc to cancel\n"
UNCHROMED_PROMPTS = {
    "codex-mcp-tool-wrapped": (
        AgentKind.CODEX,
        "  Allow the acme-internal-deployment-orchestrator MCP server to run tool\n"
        '  "create_release_candidate_with_changelog"?\n\n'
        "\u203a 1. Allow  Run the tool and continue.\n  2. Cancel  Cancel this tool call."
        + CODEX_CONFIRM,
    ),
    "codex-send-input": (
        AgentKind.CODEX,
        "  Would you like to send input to the existing terminal?\n\n  npm run dev\n\n"
        "\u203a 1. Yes, proceed\n  2. No, continue without running it" + CODEX_CONFIRM,
    ),
    "codex-send-input-named": (
        AgentKind.CODEX,
        "  Would you like to send input to terminal 3?\n\n"
        "\u203a 1. Yes, just this once\n  2. No, continue without running it" + CODEX_CONFIRM,
    ),
    "codex-session-grant": (
        AgentKind.CODEX,
        "  github wants to run create_issue.\n\n\u203a 1. Allow  Run the tool and continue.\n"
        "  2. Allow for this session  Run the tool and remember this choice for this session.\n"
        "  3. Cancel  Cancel this tool call." + CODEX_CONFIRM,
    ),
    "codex-plain-allow": (
        AgentKind.CODEX,
        "  github wants to run create_issue.\n\n\u203a 1. Allow  Run the tool and continue.\n"
        "  2. Cancel  Cancel this tool call." + CODEX_CONFIRM,
    ),
    "claude-switch-to-auto": (
        AgentKind.CLAUDE,
        " Bash command\n\n   npm test\n   Run the test suite\n\n \u276f 1. Yes\n"
        "   2. Yes, and switch to auto (no routine prompts; a reviewer model screens actions)"
        " for this session\n   3. No" + CONFIRM,
    ),
    "claude-manual-edits": (
        AgentKind.CLAUDE,
        " Here is Claude's plan:\n Add a greet helper with a test.\n\n"
        " \u276f 1. Yes, manually approve edits\n   2. No, keep planning" + CONFIRM,
    ),
    "claude-keep-allowing-reads": (
        AgentKind.CLAUDE,
        " Read file\n\n   /etc/hosts\n\n \u276f 1. Yes\n"
        "   2. Yes, and keep allowing any reads outside the working directories\n   3. No"
        + CONFIRM,
    ),
    "claude-session-read": (
        AgentKind.CLAUDE,
        " Read file\n\n   /tmp/notes/todo.txt\n\n \u276f 1. Yes\n"
        "   2. Yes, allow reading from notes/ during this session\n   3. No" + CONFIRM,
    ),
    "claude-plain-yes": (
        AgentKind.CLAUDE,
        " Bash command\n\n   rm -rf build\n   Remove build output\n\n \u276f 1. Yes\n   2. No"
        + CONFIRM,
    ),
    "claude-chrome-in-transcript": (
        AgentKind.CLAUDE,
        "\u23fa The question offers:\n  3. Type something.\n  4. Chat about this\n\n"
        " Bash command\n\n   rm -rf build\n   Remove build output\n\n \u276f 1. Yes\n   2. No"
        + CONFIRM,
    ),
    "codex-chrome-in-transcript": (
        AgentKind.CODEX,
        "\u2022 The tool shows:\n  Question 2/3 (1 unanswered)\n"
        "  enter to submit answer | esc to interrupt\n\n"
        "  Would you like to send input to terminal 3?\n\n"
        "\u203a 1. Yes, just this once\n  2. No, continue without running it" + CODEX_CONFIRM,
    ),
}


@pytest.mark.parametrize("name", sorted(UNCHROMED_PROMPTS))
def test_prompts_without_question_tool_chrome_never_reach_the_simulated_user(name):
    agent, text = UNCHROMED_PROMPTS[name]
    screen = Screen(text=text, exited=None, cursor={"x": 0, "y": 0})
    assert (dialog(agent, screen).reason, answerable_screen(agent, screen)) == (
        UNRECOGNIZED,
        False,
    )


def shifted(text, columns):
    return "\n".join(" " * columns + line if line else line for line in text.splitlines())


CODEX_MULTI_FOOTER = (
    "tab to add notes | enter to submit answer | \u2190/\u2192 to navigate questions"
    " | esc to interrupt"
)


def review_screen():
    ui = ClaudeQuestionUI(
        (
            AskedQuestion("Scope", "Which findings?", (("All", ""), ("None", ""))),
            AskedQuestion("Tests", "Add tests?", (("Yes", ""), ("No", ""))),
        )
    )
    ui.press("Enter")
    ui.press("Enter")
    return ui.render()


REVIEW_SCREEN = review_screen()
QUESTION_SCREENS = {
    "fix-scope": (AgentKind.CLAUDE, FIX_SCOPE_SCREEN),
    "fix-scope-shifted": (AgentKind.CLAUDE, shifted(FIX_SCOPE_SCREEN, 3)),
    "fix-scope-chat-offset": (
        AgentKind.CLAUDE,
        FIX_SCOPE_SCREEN.replace("  4. Chat about this", "       4.  Chat about this"),
    ),
    "fix-scope-typed": (
        AgentKind.CLAUDE,
        FIX_SCOPE_SCREEN.replace("3. Type something.", "3. Only the prefix bug"),
    ),
    "multi-select": (
        AgentKind.CLAUDE,
        (
            " Which checks?\n\n\u276f 1. [ ] Lint\n  2. [ ] Tests\n  3. [ ] Type something\n"
            "     Submit\n\n"
            "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel\n"
        ),
    ),
    "review-with-footer": (
        AgentKind.CLAUDE,
        REVIEW_SCREEN
        + "\n\n Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel",
    ),
    "codex-multi-question": (
        AgentKind.CODEX,
        shifted(
            CODEX_DIALOGS["options"]
            .replace("Question 1/1 (1 unanswered)", "Question 1/2 (2 unanswered)")
            .replace("tab to add notes | enter to submit answer | esc to interrupt", ""),
            2,
        )
        + "\n  "
        + CODEX_MULTI_FOOTER,
    ),
}


@pytest.mark.parametrize("name", sorted(QUESTION_SCREENS))
def test_unparsed_question_tool_screens_remain_answerable(name):
    agent, text = QUESTION_SCREENS[name]
    screen = Screen(text=text, exited=None, cursor={"x": 0, "y": 0})
    assert (dialog(agent, screen).reason, answerable_screen(agent, screen)) == (
        UNRECOGNIZED,
        True,
    )


def test_a_parsed_review_screen_is_answerable():
    screen = Screen(text=REVIEW_SCREEN, exited=None, cursor={"x": 0, "y": 0})
    assert (question_dialog(screen).kind, answerable_screen(AgentKind.CLAUDE, screen)) == (
        "review",
        True,
    )


def test_codex_question_chrome_needs_both_the_question_header_and_the_submit_footer():
    header, *rest, footer = CODEX_DIALOGS["options"].splitlines()
    body = "\n".join(rest)
    assert [
        question_chrome(AgentKind.CODEX, text)
        for text in (f"{header}\n{body}\n{footer}", f"{body}\n{footer}", f"{header}\n{body}")
    ] == [True, False, False]


def test_droid_screens_never_reach_the_simulated_user():
    screen = Screen(text=FIX_SCOPE_SCREEN, exited=None, cursor={"x": 0, "y": 0})
    assert (dialog(AgentKind.DROID, screen).reason, answerable_screen(AgentKind.DROID, screen)) == (
        UNRECOGNIZED,
        False,
    )


@pytest.mark.parametrize("invisible", ["\u00ad", "\u200b", "\u200d", "\u2060", "\ufeff"])
def test_zero_width_characters_do_not_hide_permission_wording(invisible):
    row = f"Yes, and don{invisible}\u2019t ask again for: npm test"
    screen = Screen(
        text=FIX_SCOPE_SCREEN.replace("Don't fix anything", row), exited=None, cursor={}
    )
    assert permission_like(f"  2. {row}\n")
    assert permission_like(f'Use{invisible} skill "code-review"?')
    assert answerable_screen(AgentKind.CLAUDE, screen) is False


ESCALATING = (
    "auto mode",
    "auto-accept",
    "accept edits",
    "bypass",
    "don't ask again",
    "don\u2019t ask again",
    "always allow",
    "allow all",
    "allow and",
    "trust",
    "dangerously",
    "skip permissions",
    "yolo",
)


def offered(label):
    return Screen(
        text=FIX_SCOPE_SCREEN.replace("Fix all Should Fix", label), exited=None, cursor={}
    )


@pytest.mark.parametrize("phrase", ESCALATING)
@pytest.mark.parametrize("key", ["Enter", "Space", "1"])
def test_a_key_choosing_a_mode_or_permission_option_is_refused(phrase, key):
    label = f"Go ahead, {phrase.upper()} for the rest"
    assert escalation(AgentKind.CLAUDE, offered(label), key, None).startswith("Go ahead, ")
    assert escalation(AgentKind.CLAUDE, offered("Fix all Should Fix"), key, label) == label


def test_ordinary_options_and_moves_pass_the_option_guard():
    screen = offered("Fix all Should Fix")
    assert [
        escalation(AgentKind.CLAUDE, screen, key, target)
        for key, target in (
            ("Enter", "Fix all Should Fix"),
            ("1", "Fix all Should Fix"),
            ("2", "Don't fix anything"),
            ("Space", None),
        )
    ] == [None] * 4
    assert escalation(AgentKind.CLAUDE, offered("Always allow"), "Down", "Always allow") is None
    assert escalation(AgentKind.CLAUDE, offered("Always allow"), "2", None) is None


def test_a_digit_is_checked_against_the_option_it_picks():
    screen = Screen(
        text=FIX_SCOPE_SCREEN.replace("Don't fix anything", "Trust the agent from now on"),
        exited=None,
        cursor={},
    )
    assert escalation(AgentKind.CLAUDE, screen, "2", None).startswith("Trust the agent")
    assert escalation(AgentKind.CLAUDE, screen, "Enter", None) is None


def test_screen_identity_ignores_the_highlight_and_typed_answer_but_not_the_question():
    from conftest import CHANNEL

    ui = ScreenUI(FIX_SCOPE_SCREEN)
    shown = screen_identity(ui.render())
    ui.press("Down")
    ui.press("Down")
    ui.type("Only the prefix bug")
    assert screen_identity(ui.render()) == shown
    assert screen_identity(ScreenUI(CHANNEL).render()) != shown


@pytest.mark.parametrize(
    ("agent", "text", "ready"),
    [
        (
            AgentKind.CLAUDE,
            "Fixed it.\n\n\u2500\u2500\u2500\u2500\n\u276f \n\u2500\u2500\u2500\u2500\n  main",
            True,
        ),
        (
            AgentKind.CODEX,
            "\u2022 Fixed it.\n\n\u203a Ask Codex to do anything\n\n? for shortcuts",
            True,
        ),
        (AgentKind.CLAUDE, FIX_SCOPE_SCREEN, False),
        (AgentKind.CODEX, CODEX_DIALOGS["freeform"], False),
    ],
)
def test_the_composer_is_told_apart_from_an_input_screen(agent, text, ready):
    assert composer_ready(agent, Screen(text=text, exited=None, cursor={})) is ready


CHROME_PERMISSION = (
    "⏺ I'll open a tab for the docs.\n"
    "  {marker}\n"
    "\n"
    "⏺ Claude in Chrome[tabs_create_mcp]\n"
    "\n" + "─" * 120 + "\n"
    " Claude in Chrome wants to create a new tab\n"
    "\n"
    " \u276f 1. Allow\n"
    "   2. Deny (esc)\n"
)


@pytest.mark.parametrize("marker", ["Type something.", "Chat about this"])
def test_agent_printed_question_rows_above_a_short_permission_prompt_are_not_chrome(marker):
    screen = Screen(
        text=CHROME_PERMISSION.format(marker=marker), exited=None, cursor={"x": 1, "y": 8}
    )
    assert question_chrome(AgentKind.CLAUDE, screen.text) is False
    assert answerable_screen(AgentKind.CLAUDE, screen) is False


@pytest.mark.parametrize("key", ["Enter", "1", "Space"])
def test_a_bare_allow_option_is_never_pressed(key):
    screen = Screen(
        text=CHROME_PERMISSION.format(marker="⏺ done"), exited=None, cursor={"x": 1, "y": 8}
    )
    assert escalation(AgentKind.CLAUDE, screen, key, None) == "Allow"
