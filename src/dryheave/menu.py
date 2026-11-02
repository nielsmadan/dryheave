import math
import os
import select
import termios
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, TextIO

__all__ = ["Choice", "Question", "choose"]

type Outcome = Literal["pending", "chosen", "cancelled"]
type NamedKey = Literal["up", "down", "enter", "escape", "ctrl-c", "other"]
type Key = NamedKey | str

HINT = "↑/↓ move · Enter choose · Esc cancel"
POINTER = "\u276f"
CHECK = "\u2714"
ARROWS: dict[str, NamedKey] = {"A": "up", "B": "down"}
CONTROL_KEYS: dict[str, NamedKey] = {
    "\x1b": "escape",
    "\r": "enter",
    "\n": "enter",
    "\x03": "ctrl-c",
}
ESCAPE_WAIT_SECONDS = 0.05
DEFAULT_WIDTH = 80
SELECTED_STYLE = "\x1b[1;36m"
HINT_STYLE = "\x1b[2m"
RESET = "\x1b[0m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


@dataclass(frozen=True)
class Choice[T: str]:
    name: T
    label: str
    summary: str | None = None


@dataclass(frozen=True)
class Question[T: str]:
    text: str
    choices: Sequence[Choice[T]]
    title: str
    default: T | None = None

    def __post_init__(self) -> None:
        if not self.choices:
            raise ValueError("A question needs at least one choice.")
        if self.default is not None and self.default not in self.names:
            raise ValueError(f"Default {self.default!r} is not one of the choices.")

    @property
    def names(self) -> list[T]:
        return [choice.name for choice in self.choices]

    def answered(self, picked: T | None) -> str:
        if picked is None:
            return f"  {self.title}: cancelled"
        choice = next(choice for choice in self.choices if choice.name == picked)
        return f"{CHECK} {self.title}: {choice.summary or choice.name}"


def choose[T: str](
    question: Question[T], *, stdin: TextIO, output: TextIO, environment: Mapping[str, str]
) -> T | None:
    if stdin.isatty() and output.isatty() and environment.get("TERM", "dumb") != "dumb":
        picked = _arrow_menu(question, stdin, output, color="NO_COLOR" not in environment)
    else:
        picked = numbered(question, stdin=stdin, output=output)
    print(question.answered(picked), file=output)
    return picked


def numbered[T: str](question: Question[T], *, stdin: TextIO, output: TextIO) -> T | None:
    names = question.names
    print(question.text, file=output)
    for number, choice in enumerate(question.choices, start=1):
        print(f"  {number}. {choice.label}", file=output)
    default = question.default
    suffix = f" [{names.index(default) + 1}]" if default is not None else ""
    while True:
        print(f"Choose 1-{len(names)}{suffix}: ", end="", file=output, flush=True)
        answer = stdin.readline()
        if answer == "":
            return None
        picked = numbered_answer(answer.strip(), names, default)
        if picked is not None:
            return picked
        print(f"Enter a number from 1 to {len(names)}.", file=output)


def numbered_answer[T: str](answer: str, names: Sequence[T], default: T | None) -> T | None:
    if answer == "":
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        return names[int(answer) - 1]
    return next((name for name in names if name == answer), None)


def decode_keys(text: str) -> list[Key]:
    keys: list[Key] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\x1b" and text[index + 1 : index + 2] in {"[", "O"}:
            end = index + 2
            while end < len(text) and text[end] in "0123456789;":
                end += 1
            keys.append(ARROWS.get(text[end : end + 1], "other"))
            index = end + 1
            continue
        keys.append(CONTROL_KEYS.get(character, character))
        index += 1
    return keys


def apply_key(selected: int, key: Key, *, count: int) -> tuple[int, Outcome]:
    if key in {"up", "k"}:
        return (selected - 1) % count, "pending"
    if key in {"down", "j"}:
        return (selected + 1) % count, "pending"
    if key.isdigit() and 1 <= int(key) <= count:
        return int(key) - 1, "pending"
    if key == "enter":
        return selected, "chosen"
    if key in {"escape", "q", "ctrl-c"}:
        return selected, "cancelled"
    return selected, "pending"


def apply_keys(selected: int, keys: Iterable[Key], *, count: int) -> tuple[int, Outcome]:
    for key in keys:
        selected, outcome = apply_key(selected, key, count=count)
        if outcome != "pending":
            return selected, outcome
    return selected, "pending"


def render_menu(labels: Sequence[str], selected: int, *, width: int, color: bool) -> list[str]:
    room = max(width - 5, 10)
    rows = [
        _styled(f"  {POINTER} {label[:room]}", SELECTED_STYLE, color)
        if index == selected
        else f"    {label[:room]}"
        for index, label in enumerate(labels)
    ]
    return [*rows, _styled(f"  {HINT}"[: width - 1], HINT_STYLE, color)]


def screen_rows(text: str, width: int) -> int:
    return sum(
        max(1, math.ceil(sum(_cells(character) for character in line) / width))
        for line in text.split("\n")
    )


def _cells(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _styled(text: str, style: str, color: bool) -> str:
    return f"{style}{text}{RESET}" if color else text


def _erase(rows: int) -> str:
    return f"\x1b[{rows}F\x1b[J" if rows else ""


def _columns(output: TextIO) -> int | None:
    try:
        return os.get_terminal_size(output.fileno()).columns or None
    except OSError:
        return None


def _read_text(descriptor: int) -> str:
    data = os.read(descriptor, 64)
    if data == b"\x1b" and select.select([descriptor], [], [], ESCAPE_WAIT_SECONDS)[0]:
        data += os.read(descriptor, 64)
    return data.decode("utf-8", errors="replace")


@contextmanager
def _keypresses(descriptor: int, output: TextIO) -> Iterator[None]:
    saved = termios.tcgetattr(descriptor)
    try:
        keyed = termios.tcgetattr(descriptor)
        keyed[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        keyed[6][termios.VMIN] = 1
        keyed[6][termios.VTIME] = 0
        termios.tcsetattr(descriptor, termios.TCSANOW, keyed)
        output.write(HIDE_CURSOR)
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        output.write(SHOW_CURSOR)
        output.flush()


def _arrow_menu[T: str](
    question: Question[T], stdin: TextIO, output: TextIO, *, color: bool
) -> T | None:
    names = question.names
    labels = [choice.label for choice in question.choices]
    selected = names.index(question.default) if question.default is not None else 0
    outcome: Outcome = "pending"
    columns = _columns(output)
    asked = screen_rows(question.text, columns) if columns else 0
    print(question.text, file=output)
    drawn = 0
    try:
        with _keypresses(stdin.fileno(), output):
            while outcome == "pending":
                lines = render_menu(labels, selected, width=columns or DEFAULT_WIDTH, color=color)
                output.write(_erase(drawn) + "".join(f"{line}\n" for line in lines))
                output.flush()
                drawn = len(lines)
                selected, outcome = apply_keys(
                    selected, decode_keys(_read_text(stdin.fileno())), count=len(names)
                )
    finally:
        output.write(_erase(drawn + asked))
        output.flush()
    return names[selected] if outcome == "chosen" else None
