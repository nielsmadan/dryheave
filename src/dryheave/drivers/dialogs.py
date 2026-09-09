import re
import textwrap

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
    if not re.match(r"\d+\. ", selected) or not re.fullmatch(
        r"(?:press )?enter to (?:confirm|continue)(?: or esc to (?:cancel|go back))?|esc to cancel",
        footer,
    ):
        return None
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
    return Observation(
        state="unsupported", reason="Unrecognized native dialog requires input.", text=text
    )


def composer(agent: AgentKind, screen: Screen) -> str | None:
    markers = ("\u203a", "\u00bb") if agent == AgentKind.CODEX else ("\u276f",)
    lines = screen.text.splitlines()
    positions = [i for i, line in enumerate(lines) if line.lstrip().startswith(markers)]
    if not positions:
        return None
    indentation = min(len(lines[i]) - len(lines[i].lstrip()) for i in positions)
    position = next(
        i for i in reversed(positions) if len(lines[i]) - len(lines[i].lstrip()) == indentation
    )
    first = lines[position].lstrip()[1:].strip()
    prefix_width = len(lines[position]) - len(lines[position].lstrip()) + 2
    if (
        agent == AgentKind.CODEX
        and first in CODEX_EMPTY_PLACEHOLDERS
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
    if value == text.strip():
        return True
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
