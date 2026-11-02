import io

import pytest

from conftest import Terminal
from dryheave.errors import CancelledError
from dryheave.init_prompts import MODE_QUESTION, choose_install, choose_mode, install_question
from dryheave.menu import numbered
from dryheave.transport_setup import InstallTarget

NEWEST = InstallTarget("0.1.3", "found")


def mode(flag=None, existing=None, *, interactive=True, answers="", output=None):
    return choose_mode(
        flag,
        existing,
        interactive=interactive,
        stdin=Terminal(answers),
        output=output if output is not None else io.StringIO(),
        environment={},
    )


def test_the_mode_question_numbers_the_modes_with_ask_as_default():
    output = io.StringIO()
    assert numbered(MODE_QUESTION, stdin=io.StringIO("2\n"), output=output) == "auto"
    assert output.getvalue().startswith(
        "Commit authored files (voices, cases, reports) to this workspace's git repository?\n"
        "  1. ask: offer a commit after each checkpoint and let you decide\n"
        "  2. auto: commit the files each checkpoint command wrote\n"
        "  3. off: never offer commits\n"
        "Choose 1-3 [1]: "
    )


@pytest.mark.parametrize(("answer", "picked"), [("\n", "ask"), ("3\n", "off"), ("auto\n", "auto")])
def test_the_mode_prompt_takes_the_default_a_number_or_a_name(answer, picked):
    assert mode(answers=answer) == picked


def test_mode_choice_prefers_flag_then_existing_then_asks():
    output = io.StringIO()
    assert mode("off", "auto", answers="2\n", output=output) == "off"
    assert mode(None, "auto", answers="3\n", output=output) == "auto"
    assert output.getvalue() == ""
    assert mode(answers="2\n", output=output) == "auto"
    assert output.getvalue().endswith("Choose 1-3 [1]: \u2714 git commits: auto\n")


def test_mode_choice_off_a_terminal_is_ask_without_a_prompt():
    output = io.StringIO()
    assert mode(interactive=False, answers="3\n", output=output) == "ask"
    assert output.getvalue() == ""


def test_a_cancelled_mode_prompt_stops_init():
    with pytest.raises(
        CancelledError, match=r"nothing was written\. .*--git-commit ask\|auto\|off"
    ):
        mode(answers="")


def test_the_install_question_shows_the_version_command_and_profile_promise():
    question = install_question(NEWEST)
    assert question.text.splitlines() == [
        "dryheave drives agent CLIs through tui-test, which is not installed.",
        f"{NEWEST.description} It installs with:",
        f"  {NEWEST.command}",
        "Installing it for you leaves your shell profile unchanged.",
    ]
    assert question.answered("install") == "\u2714 tui-test: install 0.1.3"
    assert question.answered("manual") == "\u2714 tui-test: install it myself"


@pytest.mark.parametrize(("answers", "choice"), [("1\n", "install"), ("2\n", "manual")])
def test_the_install_prompt_offers_exactly_two_choices(answers, choice):
    output = io.StringIO()
    assert (
        choose_install(NEWEST, stdin=io.StringIO(answers), output=output, environment={}) == choice
    )
    assert "  1. Install it for me\n  2. I will install it\n" in output.getvalue()


def test_a_cancelled_install_prompt_stops_init():
    with pytest.raises(CancelledError, match="--install-tui-test or --tui-test PATH"):
        choose_install(NEWEST, stdin=io.StringIO(""), output=io.StringIO(), environment={})
