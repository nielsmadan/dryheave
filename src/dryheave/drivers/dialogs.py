import re
import textwrap
from dataclasses import dataclass
from itertools import takewhile

from pydantic import JsonValue

from dryheave.drivers.models import Observation, Screen
from dryheave.models import AgentKind

CODEX_LARGE_PASTE_THRESHOLD = 1000
CODEX_EMPTY_PLACEHOLDERS = {"Ask Codex to do anything", "Ask a follow-up question"}

CODEX_APPROVALS = (
    "Do you trust the contents of this directory?",
    "Would you like to run the following command?",
    "Would you like to make the following edits?",
    "Do you want to approve network access",
    "Would you like to grant these permissions?",
    "Hooks need review",
)
RULE = re.compile("\u2500+")
MENU_HIGHLIGHT = re.compile(r"\s+\u276f (/\S+)(?:\s{2,}|$)")
MENU_ROW = re.compile(r"\s+(?:[/\u2026]\S|No commands match \")")


@dataclass(frozen=True)
class CommandMenu:
    highlighted: str | None


CLAUDE_APPROVALS = (
    "Do you trust the files in this folder?",
    "Do you want to proceed?",
    "Allow this command?",
    "Do you want to make this edit",
)


def dialog(agent: AgentKind, screen: Screen) -> Observation | None:
    text = screen.text
    lines = text.splitlines()
    markers = "\u203a»>" if agent == AgentKind.CODEX else "\u276f"
    positions = [i for i, line in enumerate(lines) if line.lstrip().startswith(tuple(markers))]
    if not positions:
        return None
    position = positions[-1]
    indentation = len(lines[position]) - len(lines[position].lstrip())
    selected = lines[position].lstrip()[1:].strip()
    visible = [line.strip() for line in lines if line.strip()]
    footer = visible[-1].lower()
    if (
        agent == AgentKind.CODEX
        and lines[position].strip() == "\u203a Input disabled."
        and indentation == min(len(lines[i]) - len(lines[i].lstrip()) for i in positions)
        and all(
            not line.strip()
            or line.strip() == "? for shortcuts"
            or re.fullmatch("─+", line.strip())
            for line in lines[position + 1 :]
        )
    ):
        return Observation(state="unsupported", reason="Native input is disabled.", text=text)
    titles = [line.strip() for line in lines[:position]]
    if (
        re.fullmatch(r"(?:tab to add notes \| )?enter to submit answer \| esc to interrupt", footer)
        and any(re.fullmatch(r"Question \d+/\d+(?: \(\d+ unanswered\))?", line) for line in titles)
        and (re.match(r"\d+\. ", selected) or selected == "Type your answer (optional)")
    ):
        return Observation(
            state="structured_input",
            reason="Native structured question requires a dialog-specific response adapter.",
            text=text,
        )
    numbered = re.match(r"\d+\. ", selected) is not None
    if not numbered or not re.fullmatch(
        r"(?:press )?enter to (?:confirm|continue)(?: or esc to (?:cancel|go back))?|esc to cancel",
        footer,
    ):
        listed = numbered and selection_list(lines, position, screen.cursor)
        return unrecognized(text) if listed else None
    anchors = CODEX_APPROVALS if agent == AgentKind.CODEX else CLAUDE_APPROVALS
    if any(line in anchors for line in titles) or (
        agent == AgentKind.CODEX
        and any(
            line.startswith("Do you trust the contents of this directory? Working with untrusted")
            or re.fullmatch(r"Do you want to approve network access to .+\?", line)
            for line in titles
        )
    ):
        return Observation(
            state="approval",
            reason="Native approval or trust dialog requires explicit operator policy.",
            text=text,
        )
    if (
        "Choose how you want to use Codex." in titles
        or "Sign in with ChatGPT to use Codex as part of your paid plan" in titles
        or "Select login method" in titles
    ):
        return Observation(
            state="unsupported", reason="Native authentication requires input.", text=text
        )
    if "Submit with unanswered questions?" in titles:
        return Observation(
            state="unsupported",
            reason="Unanswered-question confirmation requires a dialog-specific policy.",
            text=text,
        )
    return unrecognized(text)


def unrecognized(text: str) -> Observation:
    return Observation(
        state="unsupported", reason="Unrecognized native dialog requires input.", text=text
    )


def selection_list(lines: list[str], position: int, cursor: dict[str, JsonValue]) -> bool:
    column = len(lines[position]) - len(lines[position].lstrip()[1:].lstrip())

    def option(line: str) -> bool:
        return not line[:column].strip() and re.match(r"\d+\. ", line[column:]) is not None

    above = takewhile(str.strip, reversed(lines[:position]))
    below = takewhile(str.strip, lines[position + 1 :])
    if any(map(option, above)):
        return True
    x, y = cursor.get("x"), cursor.get("y")
    return (
        type(x) is int
        and type(y) is int
        and (y < position or (y == position and x < column))
        and any(map(option, below))
    )


def composer_row(agent: AgentKind, lines: list[str]) -> int | None:
    markers = ("\u203a", "\u00bb") if agent == AgentKind.CODEX else ("\u276f",)
    positions = [i for i, line in enumerate(lines) if line.lstrip().startswith(markers)]
    if not positions:
        return None
    indentation = min(len(lines[i]) - len(lines[i].lstrip()) for i in positions)
    return next(
        i for i in reversed(positions) if len(lines[i]) - len(lines[i].lstrip()) == indentation
    )


def command_menu(screen: Screen) -> CommandMenu | None:
    lines = screen.text.splitlines()
    position = composer_row(AgentKind.CLAUDE, lines)
    if position is None:
        return None
    indentation = len(lines[position]) - len(lines[position].lstrip())
    rules = [i for i, line in enumerate(lines) if RULE.fullmatch(line.strip())]
    top = max((i for i in rules if i < position), default=position)
    bottom = min((i for i in rules if i > position), default=len(lines))
    nearby = [
        *takewhile(str.strip, reversed(lines[:top])),
        *takewhile(str.strip, lines[bottom + 1 :]),
    ]
    rows = [line for line in nearby if len(line) - len(line.lstrip()) > indentation]
    highlighted = [match[1] for line in rows if (match := MENU_HIGHLIGHT.match(line))]
    if not highlighted and not any(map(MENU_ROW.match, rows)):
        return None
    return CommandMenu(highlighted=highlighted[0] if len(highlighted) == 1 else None)


def command_ready(screen: Screen, prompt: str) -> bool:
    name, *arguments = prompt.split(maxsplit=1)
    menu = command_menu(screen)
    if menu is None:
        return bool(arguments)
    return menu.highlighted == name


def submittable(agent: AgentKind, screen: Screen, text: str) -> bool:
    return composer_matches(agent, screen, text) and (
        agent != AgentKind.CLAUDE or not text.startswith("/") or command_ready(screen, text)
    )


def composer(agent: AgentKind, screen: Screen) -> str | None:
    lines = screen.text.splitlines()
    position = composer_row(agent, lines)
    if position is None:
        return None
    first = lines[position].lstrip()[1:].strip()
    prefix_width = len(lines[position]) - len(lines[position].lstrip()) + 2
    placeholder = agent == AgentKind.CLAUDE or first in CODEX_EMPTY_PLACEHOLDERS
    if (
        placeholder
        and screen.cursor.get("x") == prefix_width
        and screen.cursor.get("y") == position
    ):
        return ""
    content = [first]
    cursor_y = screen.cursor.get("y")
    cursor_bounded = False
    visible = lines[position + 1 :]
    if type(cursor_y) is int and position <= cursor_y < len(lines):
        cursor_bounded = True
        visible = lines[position + 1 : cursor_y + 1]
    for line in visible:
        stripped = line.strip()
        if not cursor_bounded and (
            stripped.startswith(("─", "╰", "? for", "ctrl+", "esc "))
            or "context left" in stripped
            or "shortcuts" in stripped
        ):
            break
        content.append(
            line[prefix_width:].rstrip() if line.startswith(" " * prefix_width) else line.rstrip()
        )
    return "\n".join(content).strip()


def composer_matches(agent: AgentKind, screen: Screen, text: str) -> bool:
    value = composer(agent, screen)
    if value is None:
        return False
    if len(text) > CODEX_LARGE_PASTE_THRESHOLD and agent == AgentKind.CODEX:
        return (
            re.fullmatch(rf"\[Pasted Content {len(text)} chars\](?: \+\d+ lines)?", value)
            is not None
        )
    if value == text.strip() or value.split() == text.split():
        return True
    pasted = re.fullmatch(r"\[Pasted text #\d+ \+(\d+) lines\]", value)
    if agent == AgentKind.CLAUDE and pasted is not None:
        return int(pasted[1]) == text.strip().count("\n")
    width = screen.cols - 3
    if width <= 0:
        return False
    word_wrapped = []
    hard_wrapped = []
    for line in text.strip().split("\n"):
        word_wrapped.extend(
            textwrap.wrap(
                line,
                width=width,
                expand_tabs=False,
                replace_whitespace=False,
                break_on_hyphens=False,
            )
            or [""]
        )
        hard_wrapped.extend(
            [line[start : start + width] for start in range(0, len(line), width)] or [""]
        )
    return value in {"\n".join(word_wrapped).strip(), "\n".join(hard_wrapped).strip()}
