import re
import threading
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Self
from uuid import uuid4

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.base import DeliveryUncertain, EventSource, Terminal, TransportError
from dryheave.drivers.dialogs import composer, composer_matches, dialog
from dryheave.drivers.models import (
    CleanupReport,
    DriverLimits,
    LaunchObservation,
    NativeEvent,
    Observation,
    Screen,
    Submission,
    UsageObservation,
)
from dryheave.drivers.usage import UsageLedger
from dryheave.errors import InputError, LimitError
from dryheave.logs.base import string
from dryheave.models import AgentKind
from dryheave.profile_models import LaunchPlan
from dryheave.serialization import digest


def normalize_input(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


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
            if composer(self.agent, screen) != "":
                return self._observation(
                    "partial_input", "Composer already contains text; delivery stopped."
                )
            self.terminal.paste(self.prompt)
            verification = self._verify_composer(self.prompt)
            if verification is not None:
                return verification
            self.enter_attempted = True
            self.artifacts.record("enter-intent", {"submission_id": submission.submission_id})
            self.terminal.enter()
        except DeliveryUncertain:
            return self.reconcile(wait=True)
        return self.reconcile(wait=True)

    def _verify_composer(self, prompt: str) -> Observation | None:
        deadline = min(self.deadline, time.monotonic() + self.limits.command_seconds)
        while time.monotonic() < deadline and self.polls < self.limits.max_polls:
            self.polls += 1
            screen = self.terminal.state()
            blocked = self._blocked(screen)
            if blocked is not None:
                return blocked
            if composer_matches(self.agent, screen, prompt):
                return None
            self.cancelled.wait(min(self.limits.poll_seconds, max(0, deadline - time.monotonic())))
        return self._observation(
            "partial_input",
            "Exact composer content was not observed within bounds; Enter was not sent.",
        )

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

    def _observation(self, state: str, reason: str) -> Observation:
        return Observation.model_validate(
            {
                "state": state,
                "reason": reason,
                "session_id": self.root_session_id,
                "submission_id": self.submission.submission_id if self.submission else None,
            }
        )

    def _blocked(self, screen: Screen) -> Observation | None:
        if self.cancelled.is_set():
            return self._observation("cancelled", "Controller cancellation requested.")
        found = dialog(self.agent, screen)
        if found is not None:
            return found
        if screen.exited is not None:
            completed = self.observe_turn()
            return (
                completed
                if completed.state in {"completed", "question", "cancelled"}
                else self._observation(
                    "exited", "Subject exited without matching completion evidence."
                )
            )
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
