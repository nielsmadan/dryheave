import re
import textwrap
from dataclasses import dataclass
from itertools import takewhile

from pydantic import JsonValue

from dryheave.drivers.models import DialogRow, Observation, QuestionDialog, Screen
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
CLAUDE_STARTUP = (
    "Accessing workspace:",
    "Quick safety check:",
    "Allow external CLAUDE.md file imports?",
)
CONFIRM_FOOTER = re.compile(
    r"(?:press )?enter to (?:confirm|continue)(?:(?: or | \u00b7 )esc to (?:cancel|go back))?"
    r"|esc to cancel"
)
QUESTION_FOOTER = re.compile(r"\S+ to select(?: \u00b7 .+)? \u00b7 \S+ to cancel", re.IGNORECASE)
CHAT_ROW = re.compile(r"(?P<marker>\u276f)?\s*(?P<number>\d+)\. Chat about this")
DIALOG_RULE = re.compile("[\u2500\u254c]+")
ROW_MARKERS = "\u276f\u2191\u2193"
NUMBERED_ROW = re.compile(r"(?P<number>\d+)\.\s+(?P<rest>\S.*)")
CHECKBOX_ROW = re.compile(r"\[(?P<mark>.)\]\s+(?P<label>\S.*)")
CHECKED_MARKS = {"\u2714", "\u2713", "\u221a", "\u00d7"}
SUBMIT_LABELS = {"Next", "Submit"}
TAB_MARKS = ("\u2610", "\u2612", "\u2190")
GUTTER = "\u2502\u258e\u2503 "
COMMITTED = " \u2714"
REVIEW_TITLE = "Review your answers"
REVIEW_PROMPT = "Ready to submit your answers?"
REVIEW_ROW = re.compile(r"(?P<marker>\u276f)?\s*(?:\d+\.\s+)?(?P<label>Submit answers|Cancel)")
UNANSWERED = "You have not answered all questions"
REVIEW_ROWS = 2
QUESTION_TAIL = 3
MIN_ROWS = 2


@dataclass(frozen=True)
class BodyLine:
    kind: str
    text: str
    focused: bool = False


def dialog(agent: AgentKind, screen: Screen) -> Observation | None:
    text = screen.text
    lines = text.splitlines()
    special = claude_dialog(screen) if agent == AgentKind.CLAUDE else None
    markers = "\u203a»>" if agent == AgentKind.CODEX else "\u276f"
    positions = [i for i, line in enumerate(lines) if line.lstrip().startswith(tuple(markers))]
    if special is not None or not positions:
        return special
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
    confirmed = CONFIRM_FOOTER.fullmatch(footer) is not None
    if not numbered or not confirmed:
        listed = (numbered and selection_list(lines, position, screen.cursor)) or (
            agent == AgentKind.CLAUDE and confirmed
        )
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


def claude_dialog(screen: Screen) -> Observation | None:
    if question_dialog(screen) is not None:
        return Observation(
            state="structured_input",
            reason="Native question dialog awaits a selection.",
            text=screen.text,
        )
    visible = [line.strip() for line in screen.text.splitlines() if line.strip()]
    if (
        visible
        and CONFIRM_FOOTER.fullmatch(visible[-1].lower())
        and any(line.startswith(CLAUDE_STARTUP) for line in visible)
        and any(line.startswith("\u276f") for line in visible)
    ):
        return Observation(
            state="approval",
            reason="Native startup trust or import dialog requires explicit operator policy.",
            text=screen.text,
        )
    return None


def question_dialog(screen: Screen) -> QuestionDialog | None:
    lines = [line.rstrip() for line in screen.text.splitlines()]
    visible = [index for index, line in enumerate(lines) if line.strip()]
    if not visible:
        return None
    return _review(lines, visible) or _question(lines, visible)


def _review(lines: list[str], visible: list[int]) -> QuestionDialog | None:
    stripped = [line.strip() for line in lines]
    if REVIEW_TITLE not in stripped or REVIEW_PROMPT not in stripped:
        return None
    prompt = max(index for index, line in enumerate(stripped) if line == REVIEW_PROMPT)
    if stripped.index(REVIEW_TITLE) > prompt:
        return None
    matches = [REVIEW_ROW.fullmatch(stripped[index]) for index in visible if index > prompt]
    if len(matches) != REVIEW_ROWS or None in matches:
        return None
    rows = tuple(DialogRow(kind="option", label=match["label"]) for match in matches if match)
    focused = [index for index, match in enumerate(matches) if match and match["marker"]]
    if {row.label for row in rows} != {"Submit answers", "Cancel"} or len(focused) > 1:
        return None
    return QuestionDialog(
        kind="review",
        question=REVIEW_PROMPT,
        rows=rows,
        focused=focused[0] if focused else None,
        complete=not any(UNANSWERED in line for line in stripped[:prompt]),
    )


def _question(lines: list[str], visible: list[int]) -> QuestionDialog | None:
    if len(visible) < QUESTION_TAIL or not QUESTION_FOOTER.fullmatch(lines[visible[-1]].strip()):
        return None
    chat_line = lines[visible[-2]]
    chat = CHAT_ROW.fullmatch(chat_line.strip())
    rule = visible[-3]
    if chat is None or not DIALOG_RULE.fullmatch(lines[rule].strip()):
        return None
    start = len(chat_line) - len(chat_line.lstrip())
    lead = start if chat["marker"] else start - 2
    top = next(
        (
            index
            for index in reversed(visible)
            if index < rule and DIALOG_RULE.fullmatch(lines[index].strip())
        ),
        None,
    )
    if lead < 0 or top is None:
        return None
    body = _body_lines([lines[index] for index in visible if top < index < rule], lead)
    if body is None:
        return None
    headers = list(takewhile(lambda line: line.kind == "title", body))
    entries = body[len(headers) :]
    if not entries or any(line.kind == "title" for line in entries):
        return None
    titles = [line.text for line in headers]
    if titles and titles[0].startswith(TAB_MARKS):
        titles = titles[1:]
    question = " ".join(title.lstrip(GUTTER) for title in titles).strip()
    rows = _rows(entries)
    if not question or rows is None or len(rows) < MIN_ROWS:
        return None
    multi = rows[0][0].checked is not None
    count = sum(row.kind != "submit" for row, _ in rows)
    if (
        int(chat["number"]) != count + 1
        or any((row.checked is not None) != multi for row, _ in rows if row.kind != "submit")
        or any(row.kind == "submit" for row, _ in rows) != multi
    ):
        return None
    focused = [index for index, (_, focus) in enumerate(rows) if focus]
    return QuestionDialog(
        kind="question",
        question=question,
        rows=tuple(row for row, _ in rows),
        focused=focused[0] if len(focused) == 1 else None,
        multi_select=multi,
    )


def _body_lines(lines: list[str], lead: int) -> list[BodyLine] | None:
    parsed = []
    for line in lines:
        head, spacer = line[lead : lead + 1], line[lead + 1 : lead + 2]
        if line[:lead].strip():
            return None
        if head not in ROW_MARKERS + " ":
            parsed.append(BodyLine("title", line[lead:].strip()))
        elif spacer.strip():
            return None
        else:
            parsed.append(BodyLine("body", line[lead + 2 :], head == "\u276f"))
    return parsed


def _rows(entries: list[BodyLine]) -> list[tuple[DialogRow, bool]] | None:
    numbered = re.match(r"1\.\s", entries[0].text) is not None
    rows: list[tuple[DialogRow, bool]] = []
    continuations: list[list[str]] = []
    for position, entry in enumerate(entries):
        if entry.text[:1] == " ":
            if entry.text.strip() in SUBMIT_LABELS and position == len(entries) - 1 and rows:
                rows.append((DialogRow(kind="submit", label=entry.text.strip()), entry.focused))
                continuations.append([])
            elif not rows or entry.focused:
                return None
            else:
                continuations[-1].append(entry.text.strip())
            continue
        match = NUMBERED_ROW.fullmatch(entry.text) if numbered else None
        if numbered and (match is None or int(match["number"]) != len(rows) + 1):
            return None
        row = _row(match["rest"] if match else entry.text.strip())
        if row is None:
            return None
        rows.append((row, entry.focused))
        continuations.append([])
    texts = [index for index, (row, _) in enumerate(rows) if row.kind != "submit"]
    if not texts:
        return None
    last = texts[-1]
    wrapped = " ".join([rows[last][0].label, *continuations[last]])
    rows[last] = (
        rows[last][0].model_copy(update={"kind": "text", "label": wrapped}),
        rows[last][1],
    )
    return rows


def _row(text: str) -> DialogRow | None:
    box = CHECKBOX_ROW.fullmatch(text)
    if box is None:
        label = text.removesuffix(COMMITTED).rstrip()
        return DialogRow(kind="option", label=label) if label else None
    mark = box["mark"]
    if mark != " " and mark not in CHECKED_MARKS:
        return None
    return DialogRow(kind="option", label=box["label"].strip(), checked=mark != " ")


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
