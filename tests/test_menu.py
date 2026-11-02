import fcntl
import io
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from conftest import Terminal
from dryheave.menu import (
    HINT,
    Choice,
    Question,
    apply_key,
    apply_keys,
    choose,
    decode_keys,
    numbered,
    render_menu,
    screen_rows,
)

CHOICES = (Choice("install", "Install it for me"), Choice("stop", "I will install it"))
MODES = Question(
    "Mode?",
    (Choice("ask", "ask: offer"), Choice("auto", "auto: commit"), Choice("off", "off: never")),
    title="git commits",
    default="ask",
)


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("\x1b[A", ["up"]),
        ("\x1bOB", ["down"]),
        ("\x1b", ["escape"]),
        ("\r", ["enter"]),
        ("\n", ["enter"]),
        ("\x03", ["ctrl-c"]),
        ("jk2q", ["j", "k", "2", "q"]),
        ("\x1b[1;5C", ["other"]),
        ("\x1b[B\x1b[B\r", ["down", "down", "enter"]),
    ],
)
def test_terminal_input_decodes_to_keys(text, keys):
    assert decode_keys(text) == keys


@pytest.mark.parametrize(
    ("selected", "key", "result"),
    [
        (0, "up", (2, "pending")),
        (2, "down", (0, "pending")),
        (1, "k", (0, "pending")),
        (1, "j", (2, "pending")),
        (0, "3", (2, "pending")),
        (0, "4", (0, "pending")),
        (0, "0", (0, "pending")),
        (1, "enter", (1, "chosen")),
        (1, "escape", (1, "cancelled")),
        (1, "q", (1, "cancelled")),
        (1, "ctrl-c", (1, "cancelled")),
        (1, "other", (1, "pending")),
    ],
)
def test_keys_move_choose_or_cancel(selected, key, result):
    assert apply_key(selected, key, count=3) == result


@pytest.mark.parametrize(
    ("keys", "result"),
    [
        (["down", "enter", "down"], (1, "chosen")),
        (["escape", "enter"], (0, "cancelled")),
        (["down", "down"], (0, "pending")),
    ],
)
def test_the_first_choice_or_cancel_in_a_read_ends_it(keys, result):
    assert apply_keys(0, keys, count=2) == result


def test_a_question_rejects_a_default_outside_its_choices():
    with pytest.raises(ValueError, match="Default 'later' is not one of the choices"):
        Question("Install?", CHOICES, title="tui-test", default="later")
    with pytest.raises(ValueError, match="at least one choice"):
        Question("Install?", (), title="tui-test")


def test_an_answer_is_its_summary_or_its_name():
    question = Question(
        "Install?",
        (Choice("install", "Install it for me", "install 0.1.1"), Choice("stop", "Later")),
        title="tui-test",
    )
    assert question.answered("install") == "\u2714 tui-test: install 0.1.1"
    assert question.answered("stop") == "\u2714 tui-test: stop"
    assert question.answered(None) == "  tui-test: cancelled"


@pytest.mark.parametrize(
    ("text", "width", "rows"),
    [
        ("short", 40, 1),
        ("", 40, 1),
        ("x" * 40, 40, 1),
        ("x" * 41, 40, 2),
        ("a\n" + "y" * 90, 40, 4),
    ],
)
def test_screen_rows_count_wrapped_lines(text, width, rows):
    assert screen_rows(text, width) == rows


def test_screen_rows_count_wide_characters_twice():
    assert screen_rows("\u754c" * 15, 20) == 2


def test_render_menu_points_at_the_selection_and_ends_with_the_hint():
    assert render_menu(["one", "two"], 1, width=80, color=False) == [
        "    one",
        "  \u276f two",
        f"  {HINT}",
    ]


def test_render_menu_styles_the_selection_and_fits_the_width():
    lines = render_menu(["x" * 50, "short"], 0, width=30, color=True)
    assert lines[0] == f"\x1b[1;36m  \u276f {'x' * 25}\x1b[0m"
    assert lines[1] == "    short"
    assert lines[2] == f"\x1b[2m{('  ' + HINT)[:29]}\x1b[0m"


def test_numbered_prompt_lists_choices_and_offers_the_default():
    output = io.StringIO()
    assert numbered(MODES, stdin=io.StringIO("\n"), output=output) == "ask"
    assert output.getvalue() == (
        "Mode?\n  1. ask: offer\n  2. auto: commit\n  3. off: never\nChoose 1-3 [1]: "
    )


@pytest.mark.parametrize(
    ("answers", "picked"), [("2\n", "auto"), ("off\n", "off"), ("", None), ("9\n\n", "ask")]
)
def test_numbered_prompt_takes_a_number_or_name_and_end_of_input_cancels(answers, picked):
    assert numbered(MODES, stdin=io.StringIO(answers), output=io.StringIO()) == picked


def test_numbered_prompt_without_a_default_asks_again_on_a_blank_answer():
    output = io.StringIO()
    question = Question("Install?", CHOICES, title="tui-test")
    assert numbered(question, stdin=io.StringIO("\nx\n2\n"), output=output) == "stop"
    assert output.getvalue().count("Enter a number from 1 to 2.") == 2
    assert "Choose 1-2: " in output.getvalue()


@pytest.mark.parametrize(
    ("stdin", "output", "environment"),
    [
        (Terminal("1\n"), io.StringIO(), {"TERM": "xterm"}),
        (io.StringIO("1\n"), Terminal(), {"TERM": "xterm"}),
        (Terminal("1\n"), Terminal(), {"TERM": "dumb"}),
        (Terminal("1\n"), Terminal(), {}),
    ],
)
def test_choose_falls_back_to_numbers_without_a_capable_terminal(stdin, output, environment):
    picked = choose(
        Question("Install?", CHOICES, title="tui-test"),
        stdin=stdin,
        output=output,
        environment=environment,
    )
    assert picked == "install"
    assert output.getvalue().endswith(
        "  1. Install it for me\n  2. I will install it\nChoose 1-2: \u2714 tui-test: install\n"
    )


CHILD = """
import json, sys, termios
from dryheave.menu import Choice, Question, choose
picked = choose(
    Question(
        "Install?\\n" + "x" * 50,
        (
            Choice("install", "Install it for me"),
            Choice("stop", "I will install it", "install it myself"),
        ),
        title="tui-test",
    ),
    stdin=sys.stdin,
    output=sys.stderr,
    environment=json.loads(sys.argv[1]),
)
canonical = bool(termios.tcgetattr(0)[3] & termios.ICANON)
print(f"RESULT={picked} CANONICAL={canonical}", flush=True)
"""
XTERM = {"TERM": "xterm-256color"}


def _read(descriptor, until, deadline):
    collected = b""
    while until not in collected:
        assert time.monotonic() < deadline, collected
        if select.select([descriptor], [], [], 0.1)[0]:
            try:
                chunk = os.read(descriptor, 4096)
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
    return collected.decode()


def drive_menu(*steps, columns=0, environment=XTERM):
    parent, child = pty.openpty()
    fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, json.dumps(environment)],
        stdin=child,
        stdout=child,
        stderr=child,
        start_new_session=True,
    )
    os.close(child)
    try:
        deadline = time.monotonic() + 15
        text = _read(parent, HINT.encode(), deadline)
        for keys, shown in steps:
            os.write(parent, keys)
            if shown:
                text += _read(parent, shown.encode(), deadline)
        return text + _read(parent, b"CANONICAL=", deadline) + _read(parent, b"\n", deadline)
    finally:
        os.close(parent)
        process.wait(timeout=15)


@pytest.mark.integration
def test_the_arrow_menu_moves_chooses_and_collapses_the_question_into_the_answer():
    text = drive_menu((b"\x1b[B", "\u276f I will install it"), (b"\r", None), columns=40)
    assert "\x1b[1;36m  \u276f Install it for me\x1b[0m" in text
    assert "\x1b[3F\x1b[J    Install it for me\r\n\x1b[1;36m  \u276f I will install it" in text
    assert text.endswith(
        "\x1b[?25h\x1b[6F\x1b[J\u2714 tui-test: install it myself\r\nRESULT=stop CANONICAL=True\r\n"
    )


@pytest.mark.integration
def test_without_a_known_width_escape_clears_only_the_options_and_cancels():
    text = drive_menu((b"\x1b", None))
    assert text.endswith(
        "\x1b[?25h\x1b[3F\x1b[J  tui-test: cancelled\r\nRESULT=None CANONICAL=True\r\n"
    )


@pytest.mark.integration
@pytest.mark.parametrize(("keys", "result"), [(b"\rj", "RESULT=install"), (b"\x03", "RESULT=None")])
def test_keys_after_enter_are_ignored_and_ctrl_c_cancels(keys, result):
    assert f"{result} CANONICAL=True" in drive_menu((keys, None))


@pytest.mark.integration
def test_no_color_draws_the_menu_unstyled():
    text = drive_menu((b"\r", None), environment=XTERM | {"NO_COLOR": "1"})
    assert "\x1b[?25l  \u276f Install it for me\r\n    I will install it\r\n  " + HINT in text
    assert "\u2714 tui-test: install\r\nRESULT=install" in text


@pytest.mark.integration
def test_an_arrow_split_across_writes_still_moves_the_selection():
    parent_steps = ((b"\x1b", None), (b"[B\r", None))
    assert "RESULT=stop CANONICAL=True" in drive_menu(*parent_steps)
