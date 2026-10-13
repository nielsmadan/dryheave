import re
import threading
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Self
from uuid import uuid4

from pydantic import JsonValue

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.base import DeliveryUncertain, EventSource, Terminal, TransportError
from dryheave.drivers.dialogs import (
    UNRECOGNIZED,
    answerable_screen,
    composer,
    composer_matches,
    composer_ready,
    dialog,
    escalation,
    footer,
    permission_like,
    question_dialog,
    screen_lines,
    submittable,
)
from dryheave.drivers.models import (
    CleanupReport,
    DialogSelection,
    DriverLimits,
    LaunchObservation,
    NativeEvent,
    Observation,
    QuestionDialog,
    Screen,
    ScreenAction,
    Submission,
    UsageObservation,
)
from dryheave.drivers.usage import UsageLedger
from dryheave.errors import InputError, LimitError
from dryheave.logs.base import string
from dryheave.models import AgentKind
from dryheave.profile_models import LaunchPlan
from dryheave.serialization import digest

CLOSED_POLLS = 2
SCREEN_KEY_UNCERTAIN = "Screen key delivery was not acknowledged; the screen state is unknown."
SCREEN_ACTION_REFUSED = (
    "The screen action would choose a mode or permission option; no key was sent."
)
SCREEN_WAIT_EXPIRED = "The screen stayed unchanged past its busy wait bound."
SUBJECT_EXITED = "Subject exited without matching completion evidence."
COMPOSER_UNREADABLE = "Native composer could not be read while clearing; delivery stopped."
SCREEN_EVIDENCE_CHARS = 8000


def normalize_input(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def same_question(found: QuestionDialog, asked: QuestionDialog) -> bool:
    return (found.kind, found.question, found.options, found.multi_select) == (
        asked.kind,
        asked.question,
        asked.options,
        asked.multi_select,
    )


def focused_on(asked: QuestionDialog, index: int) -> Callable[[QuestionDialog], bool]:
    return lambda found: same_question(found, asked) and found.focused == index


def toggled(asked: QuestionDialog, index: int, wanted: bool) -> Callable[[QuestionDialog], bool]:
    return lambda found: same_question(found, asked) and found.rows[index].checked is wanted


def row_index(asked: QuestionDialog, kind: str, label: str | None = None) -> int | None:
    return next(
        (
            index
            for index, row in enumerate(asked.rows)
            if row.kind == kind and (label is None or row.label == label)
        ),
        None,
    )


class DriverSession:
    def __init__(
        self,
        terminal: Terminal,
        source: EventSource,
        artifacts: ArtifactWriter,
        *,
        agent: AgentKind,
        limits: DriverLimits | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        self.terminal, self.source, self.artifacts = terminal, source, artifacts
        self.agent, self.limits = agent, limits or DriverLimits()
        self.cancelled = cancelled or threading.Event()
        self.deadline = time.monotonic() + self.limits.duration_seconds
        self.polls = 0
        self.events: list[NativeEvent] = []
        self.root_session_id: str | None = None
        self.graph: dict[str, str | None] = {}
        self.usage = UsageLedger(agent)
        self.launch: LaunchObservation | None = None
        self.submission: Submission | None = None
        self.prompt: str | None = None
        self.delivery_attempted = False
        self.enter_attempted = False
        self.accepted_count = 0
        self.answered = 0
        self.screen_actions = 0
        self.screen_footer: str | None = None
        self.counted: set[str] = set()
        self.cleanup: CleanupReport | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def start(self, plan: LaunchPlan, environment: Mapping[str, str]) -> Observation:
        if self.launch is not None:
            raise InputError("Driver session already launched.")
        try:
            self.launch = self.terminal.start(plan, environment)
            return self._wait(self._startup)
        except BaseException:
            self.close()
            raise

    def _ingest(self) -> None:
        for event in self.source.poll():
            if len(self.events) >= self.limits.max_events:
                raise LimitError("Driver event limit exceeded.")
            self.events.append(event)
            self.artifacts.record("native-event", event)
            if event.kind == "metadata" or (event.kind == "assistant" and "model" in event.data):
                self._metadata(event)
            elif event.kind == "usage":
                self.usage.add(event)

    def _metadata(self, event: NativeEvent) -> None:
        if event.session_id is None:
            return
        thread = event.thread_id or event.session_id
        self.graph[thread] = event.parent_session_id or (
            event.session_id if thread != event.session_id else None
        )
        root = (
            event.parent_session_id is None
            and event.thread_id == event.session_id
            and event.data.get("isSidechain") is not True
        )
        if not root or self.launch is None:
            return
        cwd = string(event.data.get("cwd"))
        if cwd is not None and cwd != self.launch.requested_cwd:
            raise InputError("Native root session cwd differs from the launch workspace.")
        if self.root_session_id is None and cwd is None:
            return
        if self.root_session_id is not None and event.session_id != self.root_session_id:
            raise InputError("Multiple root native sessions appeared in the owned log root.")
        self.root_session_id = event.session_id
        observed = {
            "observed_session_id": event.session_id,
            "observed_cwd": cwd or self.launch.observed_cwd,
            "observed_version": string(event.data.get("cli_version"))
            or string(event.data.get("version"))
            or self.launch.observed_version,
            "observed_model": string(event.data.get("model")) or self.launch.observed_model,
            "observed_effort": string(event.data.get("effort")) or self.launch.observed_effort,
        }
        self.launch = self.launch.model_copy(update=observed)

    def _startup(self, screen: Screen) -> Observation:
        state = "ready" if composer(self.agent, screen) is not None else "starting"
        return self._observation(
            state,
            "Native composer observed; root identity may await the first submission."
            if state == "ready"
            else "Waiting for native composer.",
        )

    def prepare(self, text: str, *, policy: str = "conversation") -> Submission:
        if self.cleanup is not None or self.launch is None:
            raise InputError("Submission needs a running native session.")
        if self.submission is not None and self.observe_turn().state not in {
            "completed",
            "question",
        }:
            raise InputError(
                "Previous submission is unresolved; reconcile it before another attempt."
            )
        normalized = normalize_input(text)
        if not normalized or len(normalized.encode()) > self.limits.max_input_bytes:
            raise InputError("Submission text is empty or exceeds its byte limit.")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", normalized):
            raise InputError("Submission contains native control characters.")
        if policy == "conversation" and normalized.startswith(("/", "!", "$")):
            raise InputError("Conversation text cannot invoke native commands or skills.")
        self._ingest()
        self.submission = Submission.model_validate(
            {
                "submission_id": uuid4().hex,
                "prompt_sha256": digest(normalized.encode()),
                "cursor": self.source.cursor(),
                "root_session_id": self.root_session_id,
                "policy": policy,
            }
        )
        self.prompt = normalized
        self.screen_footer = None
        self.delivery_attempted = self.enter_attempted = False
        self.artifacts.record("submission-intent", self.submission)
        return self.submission

    def deliver(self, submission: Submission) -> Observation:
        if self.submission != submission or self.prompt is None or self.delivery_attempted:
            raise InputError("Submission is unknown or already attempted; use reconciliation.")
        self.delivery_attempted = True
        self.artifacts.record("delivery-intent", {"submission_id": submission.submission_id})
        try:
            self._ingest()
            screen = self.terminal.state()
            blocked = self._blocked(screen)
            if blocked is not None:
                return blocked
            current = composer(self.agent, screen)
            if current is None:
                return self._observation(
                    "unsupported",
                    "Native composer was not observed; delivery stopped.",
                    screen.text,
                )
            if current and self.agent != AgentKind.CLAUDE:
                return self._observation(
                    "partial_input",
                    "Composer already contains text; delivery stopped.",
                    screen.text,
                )
            verification = self._type_prompt(
                submission, self.prompt, had_existing_text=bool(current)
            )
            if verification is not None:
                return verification
            self.enter_attempted = True
            self.artifacts.record("enter-intent", {"submission_id": submission.submission_id})
            self.terminal.enter()
        except DeliveryUncertain:
            return self.reconcile(wait=True)
        return self.reconcile(wait=True)

    def _type_prompt(
        self, submission: Submission, prompt: str, *, had_existing_text: bool
    ) -> Observation | None:
        evidence: dict[str, JsonValue] = {"submission_id": submission.submission_id}
        try:
            self.terminal.paste(prompt)
        except DeliveryUncertain:
            self.artifacts.record("typing-uncertain", evidence | {"retyped": False})
            verification = self._verify_composer(prompt)
            if had_existing_text or verification is None or verification.state != "partial_input":
                return verification
            cleared = self._clear_composer()
            if isinstance(cleared, Observation):
                return cleared
            if not cleared:
                return verification
            self.artifacts.record("typing-retry", evidence)
            try:
                self.terminal.paste(prompt)
            except DeliveryUncertain:
                self.artifacts.record("typing-uncertain", evidence | {"retyped": True})
        return self._verify_composer(prompt)

    def _clear_composer(self) -> Observation | bool:
        screen = self._read_until_present()
        if screen is None:
            return self._observation("delivery_uncertain", COMPOSER_UNREADABLE)
        current = composer(self.agent, screen)
        presses = 0 if current is None else 2 * (current.count("\n") + 1) + 2
        for _ in range(presses):
            blocked = self._blocked(screen)
            if blocked is not None:
                return blocked
            current = composer(self.agent, screen)
            if not current:
                return current == ""
            try:
                self.terminal.clear_line()
            except DeliveryUncertain:
                return False
            changed = self._screen_change(screen)
            if changed is None:
                return False
            screen = changed
        blocked = self._blocked(screen)
        return blocked if blocked is not None else composer(self.agent, screen) == ""

    def _read_until_present(self) -> Screen | None:
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        while time.monotonic() < deadline and self.polls < self.limits.max_polls:
            self.polls += 1
            screen = self._read()
            if screen is not None:
                return screen
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return None

    def _screen_change(self, before: Screen | None) -> Screen | None:
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        while time.monotonic() < deadline and self.polls < self.limits.max_polls:
            self.polls += 1
            screen = self._read()
            if screen is not None and (
                before is None or (screen.text, screen.cursor) != (before.text, before.cursor)
            ):
                return screen
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return None

    def _read(self) -> Screen | None:
        try:
            return self.terminal.state()
        except DeliveryUncertain:
            return None

    def _verify_composer(self, prompt: str) -> Observation | None:
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        screen = None
        while time.monotonic() < deadline and self.polls < self.limits.max_polls:
            self.polls += 1
            read = self._read()
            if read is None:
                self.cancelled.wait(
                    min(self.limits.poll_seconds, max(0, deadline - time.monotonic()))
                )
                continue
            screen = read
            blocked = self._blocked(screen)
            if blocked is not None:
                return blocked
            if submittable(self.agent, screen, prompt):
                return None
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return self._observation(
            "partial_input",
            "The command menu did not highlight the typed command; Enter was not sent."
            if screen is not None and composer_matches(self.agent, screen, prompt)
            else "Exact composer content was not observed within bounds; Enter was not sent.",
            "" if screen is None else screen.text,
        )

    def question(self) -> QuestionDialog | None:
        if self.agent != AgentKind.CLAUDE or self.launch is None or self.cleanup is not None:
            return None
        found = question_dialog(self.terminal.state())
        return found if found is not None and found.kind == "question" else None

    def answer(self, asked: QuestionDialog, selection: DialogSelection) -> Observation:
        if self.submission is None or self.cleanup is not None:
            raise InputError("Dialog answers need an active native submission.")
        if selection.text is not None and (
            re.search(r"[\x00-\x1f\x7f]", selection.text)
            or len(selection.text.encode()) > self.limits.max_input_bytes
        ):
            raise InputError("Dialog text must be one bounded line without control characters.")
        self.artifacts.record(
            "dialog-intent",
            {"question": asked.question, "selection": selection.model_dump(mode="json")},
        )
        try:
            failure = self._apply(asked, selection)
            if failure is None:
                return self._after_answer(asked)
        except DeliveryUncertain:
            failure = "Dialog key delivery was not acknowledged; the dialog state is unknown."
        return self._dialog_stop(failure)

    def screen(self) -> Screen | None:
        if self.submission is None or self.cleanup is not None:
            return None
        current = self.terminal.state()
        self.screen_footer = self._open_footer(current)
        return current if self.screen_footer is not None else None

    def _open_footer(self, screen: Screen) -> str | None:
        if composer_ready(self.agent, screen):
            return None
        if answerable_screen(self.agent, screen):
            return footer(screen)
        same = (
            self.screen_footer is not None
            and screen.exited is None
            and footer(screen) == self.screen_footer
            and dialog(self.agent, screen) is None
            and not permission_like(screen.text)
        )
        return self.screen_footer if same else None

    def act(self, action: ScreenAction, shown: str, target: str | None = None) -> Observation:
        if self.submission is None or self.cleanup is not None:
            raise InputError("Screen answers need an active native submission.")
        before = self.terminal.state()
        self.screen_footer = self._open_footer(before)
        if self.cancelled.is_set() or before.exited is not None or self.screen_footer is None:
            return self._screen_outcome(before)
        evidence: dict[str, JsonValue] = {
            "screen": before.text[-SCREEN_EVIDENCE_CHARS:],
            "action": action.model_dump(mode="json"),
        }
        if screen_lines(before.text) != screen_lines(shown):
            self.artifacts.record("screen-stale", evidence)
            return self._screen_outcome(before)
        chosen = escalation(self.agent, before, action.key, target)
        if chosen is not None:
            self.artifacts.record(
                "screen-refused", {**evidence, "target": target, "option": chosen}
            )
            return self._observation("approval", SCREEN_ACTION_REFUSED, before.text)
        self.artifacts.record("screen-intent", evidence)
        self.screen_actions += 1
        try:
            if action.key is not None:
                self._press(action.key)
            elif action.text is not None:
                self.terminal.paste(action.text)
        except DeliveryUncertain:
            return self._dialog_stop(SCREEN_KEY_UNCERTAIN)
        after = self._settle(before.text)
        current = after or self.terminal.state()
        self.artifacts.record(
            "screen-result",
            {"changed": after is not None, "screen": current.text[-SCREEN_EVIDENCE_CHARS:]},
        )
        return self._screen_outcome(current)

    def await_change(self, shown: str, until: float) -> Observation:
        if self.submission is None or self.cleanup is not None:
            raise InputError("Screen waits need an active native submission.")
        while True:
            self.polls += 1
            if time.monotonic() >= self.deadline or self.polls > self.limits.max_polls:
                return self._observation("timeout", "Driver lifetime or polling limit reached.")
            self._ingest()
            screen = self.terminal.state()
            if self.cancelled.is_set() or screen.text != shown or screen.exited is not None:
                return self._screen_outcome(screen)
            turn = self.observe_turn()
            if turn.state in {"completed", "question", "cancelled"}:
                return turn
            if time.monotonic() >= until:
                return self._observation("unsupported", SCREEN_WAIT_EXPIRED, screen.text)
            self.cancelled.wait(
                min(self.limits.poll_seconds, max(0, min(self.deadline, until) - time.monotonic()))
            )

    def exit_observation(self, screen: Screen) -> Observation:
        self._ingest()
        completed = self.observe_turn()
        return (
            completed
            if completed.state in {"completed", "question", "cancelled"}
            else self._observation("exited", SUBJECT_EXITED, screen.text)
        )

    def _screen_outcome(self, screen: Screen) -> Observation:
        if self.cancelled.is_set():
            return self._observation("cancelled", "Controller cancellation requested.", screen.text)
        if screen.exited is not None:
            return self.exit_observation(screen)
        found = dialog(self.agent, screen)
        if found is not None:
            return found
        self.screen_footer = self._open_footer(screen)
        if self.screen_footer is not None:
            return self._observation("unsupported", UNRECOGNIZED, screen.text)
        return self.reconcile()

    def _settle(self, before: str) -> Screen | None:
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        changed: Screen | None = None
        while not self.cancelled.is_set() and self.polls < self.limits.max_polls:
            self.polls += 1
            screen = self.terminal.state()
            if changed is not None and screen.text == changed.text:
                return screen
            changed = screen if screen.text != before else None
            if time.monotonic() >= deadline:
                return changed
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return changed

    def _dialog_stop(self, reason: str) -> Observation:
        text = self.terminal.state().text
        if self.cancelled.is_set():
            return self._observation("cancelled", "Controller cancellation requested.", text)
        if time.monotonic() >= self.deadline:
            return self._observation("timeout", "Driver lifetime or polling limit reached.", text)
        return self._observation("unsupported", reason, text)

    def _apply(self, asked: QuestionDialog, selection: DialogSelection) -> str | None:
        current = self._await(lambda found: same_question(found, asked))
        if current is None:
            return "The question dialog changed before it was answered."
        if asked.multi_select:
            for index, row in enumerate(asked.rows):
                wanted = row.label in selection.choices
                if current is None or row.kind != "option" or current.rows[index].checked == wanted:
                    continue
                current = self._focus(asked, current, index)
                current = current and self._key("Space", toggled(asked, index, wanted))
            if current is not None and selection.text is not None:
                current = self._type(asked, current, selection.text)
            submit = row_index(asked, "submit")
            if current is None or submit is None:
                return "Dialog selection could not be verified on screen."
            current = self._focus(asked, current, submit)
        else:
            target = (
                row_index(asked, "text")
                if selection.text is not None
                else row_index(asked, "option", selection.choices[0])
            )
            if target is None:
                return "Dialog option could not be located."
            current = self._focus(asked, current, target)
            if current is not None and selection.text is not None:
                current = self._type(asked, current, selection.text)
        if current is None:
            return "Dialog selection could not be verified on screen."
        self._press("Enter")
        return None

    def _type(
        self, asked: QuestionDialog, current: QuestionDialog, text: str
    ) -> QuestionDialog | None:
        index = row_index(asked, "text")
        focused = None if index is None else self._focus(asked, current, index)
        if focused is None or index is None:
            return None
        target = " ".join(text.split())

        def settled(found: QuestionDialog) -> bool:
            return (
                same_question(found, asked)
                and found.focused == index
                and " ".join(found.rows[index].label.split()) == target
                and found.rows[index].checked in {None, True}
            )

        for attempt in range(2):
            if attempt:
                self.terminal.clear_line()
            self.artifacts.record("dialog-text", {"question": asked.question})
            self.terminal.paste(text)
            found = self._await(settled)
            if found is not None:
                return found
        return None

    def _focus(
        self, asked: QuestionDialog, current: QuestionDialog, target: int
    ) -> QuestionDialog | None:
        for _ in asked.rows:
            if current.focused == target:
                return current
            if current.focused is None:
                return None
            step = 1 if current.focused < target else -1
            expected = current.focused + step
            found = self._key("Down" if step > 0 else "Up", focused_on(asked, expected))
            if found is None:
                return None
            current = found
        return current if current.focused == target else None

    def _key(self, key: str, settled: Callable[[QuestionDialog], bool]) -> QuestionDialog | None:
        self._press(key)
        return self._await(settled)

    def _press(self, key: str) -> None:
        self.artifacts.record("dialog-key", {"key": key})
        self.terminal.press(key)

    def _await(self, settled: Callable[[QuestionDialog], bool]) -> QuestionDialog | None:
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        while not self.cancelled.is_set() and self.polls < self.limits.max_polls:
            self.polls += 1
            found = question_dialog(self.terminal.state())
            if found is not None and settled(found):
                return found
            if time.monotonic() >= deadline:
                return None
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return None

    def _after_answer(self, asked: QuestionDialog) -> Observation:
        advanced, outcome = self._advance(asked)
        if advanced and outcome is not None and outcome.kind == "review":
            submit = row_index(outcome, "option", "Submit answers")
            focused = (
                None
                if submit is None or not outcome.complete
                else self._focus(outcome, outcome, submit)
            )
            if focused is None:
                return self._dialog_stop(
                    "The answer review could not be submitted with every question answered."
                )
            self._press("Enter")
            advanced, outcome = self._advance(outcome)
            advanced = advanced and outcome is None
        if not advanced:
            return self._dialog_stop("The question dialog did not advance after its answer.")
        self.answered += 1
        self.artifacts.record("dialog-answered", {"question": asked.question})
        if outcome is not None:
            return self._observation(
                "structured_input",
                "Native question dialog awaits a selection.",
                self.terminal.state().text,
            )
        return self.reconcile(wait=True)

    def _advance(self, asked: QuestionDialog) -> tuple[bool, QuestionDialog | None]:
        closed = 0
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        while not self.cancelled.is_set() and self.polls < self.limits.max_polls:
            self.polls += 1
            found = question_dialog(self.terminal.state())
            closed = closed + 1 if found is None else 0
            if found is not None and not same_question(found, asked):
                return True, found
            if closed >= CLOSED_POLLS:
                return True, None
            if time.monotonic() >= deadline:
                break
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return False, None

    def reconcile(self, *, wait: bool = False) -> Observation:
        if self.submission is None:
            raise InputError("No submission to reconcile.")
        if wait:
            return self._wait(lambda _screen: self.observe_turn())
        self._ingest()
        blocked = self._blocked(self.terminal.state())
        return blocked or self.observe_turn()

    def observe_turn(self) -> Observation:
        if self.submission is None:
            return self._observation("working", "No active submission.")
        root = self.submission.root_session_id or self.root_session_id
        relevant = [
            event
            for event in self.events
            if root is not None
            and event.sequence > self.submission.cursor.sequence
            and event.session_id == root
            and event.thread_id == root
        ]
        accepted = [
            event
            for event in relevant
            if event.kind == "accepted"
            and normalize_input(event.text) == self.prompt
            and event.turn_id is not None
        ]
        identities = {(event.turn_id, event.native_id or event.turn_id) for event in accepted}
        if len(identities) > 1:
            return self._observation(
                "unsupported", "Multiple fresh user acceptances match this submission."
            )
        if not accepted:
            return self._observation(
                "delivery_uncertain" if self.delivery_attempted else "working",
                "Waiting for fresh native human acceptance.",
            )
        accepted_event = accepted[0]
        if self.submission.submission_id not in self.counted:
            self.counted.add(self.submission.submission_id)
            self.accepted_count += 1
            self.artifacts.record("submission-accepted", accepted_event)
        turn = [event for event in relevant if event.turn_id == accepted_event.turn_id]
        started = next((event for event in turn if event.kind == "started"), None)
        final = next(
            (
                event
                for event in turn
                if event.kind in {"completed", "aborted"}
                and event.sequence > accepted_event.sequence
                and (started is None or event.sequence > started.sequence)
            ),
            None,
        )
        state, reason = (
            "accepted",
            "Fresh native human acceptance observed; waiting for matching lifecycle completion.",
        )
        text = "\n".join(event.text for event in turn if event.kind == "assistant")
        evidence = [accepted_event.sequence]
        if started is not None and final is not None:
            evidence.extend((started.sequence, final.sequence))
            text = final.text or text
            state = (
                "cancelled"
                if final.kind == "aborted"
                else "question"
                if text.rstrip().endswith("?")
                else "completed"
            )
            reason = "Matching native root turn lifecycle observed."
        return Observation.model_validate(
            {
                "state": state,
                "reason": reason,
                "submission_id": self.submission.submission_id,
                "session_id": self.root_session_id,
                "turn_id": accepted_event.turn_id,
                "accepted_id": accepted_event.native_id or accepted_event.turn_id,
                "text": text,
                "evidence": tuple(evidence),
            }
        )

    def _observation(self, state: str, reason: str, text: str = "") -> Observation:
        return Observation.model_validate(
            {
                "state": state,
                "reason": reason,
                "session_id": self.root_session_id,
                "submission_id": self.submission.submission_id if self.submission else None,
                "text": text,
            }
        )

    def _blocked(self, screen: Screen) -> Observation | None:
        if self.cancelled.is_set():
            return self._observation("cancelled", "Controller cancellation requested.", screen.text)
        found = dialog(self.agent, screen)
        if found is not None:
            return found
        if screen.exited is not None:
            return self.exit_observation(screen)
        return None

    def _wait(self, classify: Callable[[Screen], Observation]) -> Observation:
        while True:
            self.polls += 1
            if time.monotonic() >= self.deadline or self.polls > self.limits.max_polls:
                return self._observation("timeout", "Driver lifetime or polling limit reached.")
            self._ingest()
            screen = self.terminal.state()
            observation = self._blocked(screen) or classify(screen)
            if observation.state not in {"starting", "working", "accepted", "delivery_uncertain"}:
                self.artifacts.record("observation", observation)
                return observation
            self.cancelled.wait(
                min(self.limits.poll_seconds, max(0, self.deadline - time.monotonic()))
            )

    def close(self) -> CleanupReport:
        if self.cleanup is not None:
            return self.cleanup
        report = self.terminal.close()
        errors = list(report.errors)
        drained = False
        try:
            self._ingest()
            drained_method = getattr(self.source, "drained", None)
            drained = drained_method() if callable(drained_method) else True
        except (InputError, OSError, TransportError):
            errors.append("native_log_drain_failed")
        self.cleanup = report.model_copy(update={"logs_drained": drained, "errors": tuple(errors)})
        return self.cleanup

    def usage_snapshot(self) -> UsageObservation:
        known = {self.root_session_id} if self.root_session_id is not None else set()
        for _ in self.graph:
            added = {child for child, parent in self.graph.items() if parent in known}
            if added <= known:
                break
            known.update(added)
        records = self.usage.snapshot()
        attributable = tuple(
            record for record in records if record.thread_id in known and record.session_id in known
        )
        attributed_ids = {record.identity for record in attributable}
        observed_usage = {record.thread_id for record in attributable if record.usage is not None}
        return UsageObservation(
            records=attributable,
            unattributed=tuple(
                record for record in records if record.identity not in attributed_ids
            ),
            session_graph=dict(self.graph),
            missing_session_usage=tuple(sorted(known - observed_usage)),
        )
