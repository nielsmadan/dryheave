import os
import time
import traceback
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

import psutil
from pydantic import JsonValue

from dryheave.capture_models import CaptureOmission, WorkspaceCapture
from dryheave.cases import (
    CapturePolicy,
    InspectStep,
    PromptStep,
    WorkflowStep,
    effective_workflow,
    load_frozen_case,
)
from dryheave.controller_models import (
    ControllerRecipe,
    DialogPrompt,
    DialogueMessage,
    InspectionFinding,
    RoleCall,
    SimulatorDecision,
)
from dryheave.controllers import (
    CallContext,
    dialog_projection,
    dialog_selection,
    invoke_controller,
    is_controller_limit_error,
    screen_answer,
    screen_projection,
    simulator_projection,
)
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.auth import RuntimeBindings
from dryheave.drivers.dialogs import awaits_screen, screen_identity
from dryheave.drivers.fake import FakeEventSource
from dryheave.drivers.log_source import NativeLogSource, native_log_root
from dryheave.drivers.models import (
    CleanupReport,
    DriverLimits,
    LaunchObservation,
    Observation,
    ProcessIdentity,
    QuestionDialog,
    Screen,
    UsageObservation,
)
from dryheave.drivers.session import (
    SCREEN_ACTION_REFUSED,
    SCREEN_KEY_UNCERTAIN,
    SCREEN_WAIT_EXPIRED,
    DriverSession,
)
from dryheave.drivers.tui_test import TuiTestTerminal
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import (
    FrozenExperiment,
    ScoringConfig,
    TrialSpec,
    inspection_recipe,
)
from dryheave.experiments import load_role
from dryheave.filesystem import ensure_directory
from dryheave.final_capture import CaptureBuilder, capture_workspace
from dryheave.fixture_subject import FixtureTerminal
from dryheave.inspection import InspectionRequest, InspectionResult, inspect_workspace
from dryheave.judges import TranscriptEntry, judge_transcript_from_events
from dryheave.live_config import LIVE_CONFIG_DIRECTORY
from dryheave.models import JournalEvent, ObjectKind, StrictModel, TrialStage
from dryheave.personas import load_frozen_persona
from dryheave.process_ownership import ProcessOwner
from dryheave.processes import CommandControl, run_command
from dryheave.profile_launch import LaunchOptions, launch_environment, materialize_profile
from dryheave.repositories import materialize_repository
from dryheave.runner_models import (
    AttemptState,
    CapturedAttempt,
    QuarantinedCapture,
    WorkflowEvent,
    WorkflowProgress,
    WorkflowStepRecord,
)
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore
from dryheave.workflow import dialog_source, next_workflow_action

type SubmissionPolicy = Literal["conversation", "curated-native-command"]

INSPECTION_ATTEMPTS = 2
WORKFLOW_STEP_EVENT = "workflow-step"
INVALID_DIALOG_ANSWER = "invalid_dialog_answer"
INVALID_SCREEN_ANSWER = "invalid_screen_answer"
UNANSWERED_REJECTIONS = frozenset({INVALID_DIALOG_ANSWER, INVALID_SCREEN_ANSWER})
RETRIED_REJECTIONS = frozenset({"stop_with_findings", "empty_reply", *UNANSWERED_REJECTIONS})
SCREEN_ACTIONS = 8
SCREEN_WAITS = 4
SCREEN_STEPS = 32
SCREEN_WAIT_SECONDS = 60.0
TURN_PENDING = frozenset({"working", "accepted", "delivery_uncertain"})
SCREEN_UNSETTLED = frozenset({"cancelled", "timeout", "exited", "error"})


@dataclass(frozen=True)
class ScreenTally:
    identity: tuple[str, ...]
    before: frozenset[str]
    acted: int = 0
    waited: int = 0
    wait_until: float | None = None
    pressed: bool = False


@dataclass(frozen=True)
class DialogAsk:
    prompt: DialogPrompt | None
    answer_hint: str | None
    dialogue: tuple[DialogueMessage, ...]
    screen: str | None = None


def _combine_cleanup(*reports: CleanupReport) -> CleanupReport:
    owned = {(item.pid, item.created): item for report in reports for item in report.owned}
    survivors = {(item.pid, item.created): item for report in reports for item in report.survivors}
    return CleanupReport(
        terminal_closed=all(report.terminal_closed for report in reports),
        known_writers_stopped=not survivors
        and all(report.known_writers_stopped for report in reports),
        logs_drained=all(report.logs_drained for report in reports),
        owned=tuple(owned.values()),
        survivors=tuple(survivors.values()),
        errors=tuple(
            dict.fromkeys(
                error
                for report in reports
                for error in report.errors
                if not is_controller_limit_error(error)
            )
        ),
        coverage="fixture"
        if all(report.coverage == "fixture" for report in reports)
        else "partial-native",
    )


def workflow_progress(
    steps: tuple[WorkflowStep, ...], events: tuple[JournalEvent, ...], attempt_id: str
) -> WorkflowProgress | None:
    records = tuple(
        parse_model(canonical_json(event.data), WorkflowStepRecord)
        for event in events
        if event.attempt_id == attempt_id and event.event == WORKFLOW_STEP_EVENT
    )
    if not steps and not records:
        return None
    return WorkflowProgress(
        steps=tuple(step.step_id for step in steps),
        position=records[-1].position if records else 0,
        events=tuple(record.event for record in records),
    )


def describe_error(error: BaseException) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    origin = f" ({Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    return f"{type(error).__name__}: {error}{origin}"


class TrialExecution:
    def __init__(
        self,
        store: ObjectStore,
        journal: ExecutionJournal,
        experiment: FrozenExperiment,
        trial: TrialSpec,
        state: AttemptState,
        cancelled: Event,
    ) -> None:
        if journal.options is None:
            raise InputError("Run execution options are missing.")
        self.store, self.journal, self.experiment, self.trial = store, journal, experiment, trial
        self.state, self.cancelled, self.options = state, cancelled, journal.options
        self.root = journal.attempt_root(state)
        self.workspace = Path(state.workspace)
        self.case = load_frozen_case(store, trial.case_id)
        self.persona = load_frozen_persona(store, self.case.persona_id)
        self.recipe = load_role(
            store, experiment.simulator_id, ControllerRecipe, ObjectKind.SIMULATOR
        )
        self.inspector = (
            inspection_recipe(
                load_role(store, experiment.scoring_id, ScoringConfig, ObjectKind.SCORING)
            )
            if self.options.workflow
            else None
        )
        self.steps = effective_workflow(
            self.case, enabled=self.options.workflow, inspected=self.inspector is not None
        )
        self.position = state.workflow_position or 0
        self.inspections = 0
        self.last_inspection: InspectionResult | None = None
        self.pending: WorkflowEvent | None = None
        self.inspection_root = journal.journal.path / "inspections" / state.attempt_id
        self.driver: DriverSession | None = None
        self.terminal: TuiTestTerminal | None = None
        self.dialogue: list[DialogueMessage] = []
        self.observation: Observation | None = None
        self.accepted = self.deliveries = 0
        self.subject_seconds: float | None = 0.0
        self.setup_seconds: float | None = None
        self.recovered = False
        self.started: float | None = None
        self.deadline = 0.0
        self.errors: list[str] = []
        self.cleanup = CleanupReport(
            terminal_closed=True,
            known_writers_stopped=True,
            logs_drained=True,
            coverage="fixture" if self.options.mode == "offline-fixture" else "partial-native",
        )
        self.usage = UsageObservation(
            records=(), unattributed=(), session_graph={}, missing_session_usage=()
        )

    def update(self, **changes: object) -> None:
        self.state = self.state.model_copy(update=changes)
        self.journal.save(self.state)

    def own(self, identities: tuple[ProcessIdentity, ...]) -> None:
        self.state = self.journal.own(self.state, identities)

    def _transport_event(self, name: str, model: StrictModel | dict[str, JsonValue]) -> None:
        if name == "launch-observed" and isinstance(model, LaunchObservation):
            self.update(launch=model)
        if self.terminal is not None:
            self.terminal.owner.publish(self.own, force=name == "launch-observed")

    def _setup(self, inherited: Mapping[str, str]) -> None:
        started = time.monotonic()
        deadline = started + self.case.budgets.setup_seconds
        evidence = ArtifactWriter(self.root / "setup", 16 * 1024 * 1024)
        owner = ProcessOwner()

        def claim(pid: int) -> None:
            owner.add(psutil.Process(pid))
            publish(force=True)

        def publish(*, force: bool = False) -> None:
            owner.publish(self.own, force=force)

        try:
            for command in self.case.setup:
                if self.cancelled.is_set() or time.monotonic() >= deadline:
                    raise InputError("Setup cancelled or exhausted its overall deadline.")
                environment = {
                    name: inherited[name]
                    for name in ("PATH", "HOME", "LANG", "LC_ALL")
                    if name in inherited
                }
                for reference in command.environment:
                    if reference.name not in inherited:
                        raise InputError(f"Missing setup environment reference: {reference.name}.")
                    environment[reference.name] = inherited[reference.name]
                evidence.record("intent", command)
                self.journal.evidence(self.state, "setup-intent", command)
                result = run_command(
                    command,
                    self.workspace,
                    environment=environment,
                    on_start=claim,
                    control=CommandControl(
                        cancelled=self.cancelled, deadline=deadline, on_poll=publish
                    ),
                )
                evidence.record(
                    "result",
                    {
                        "returncode": result.returncode,
                        "outcome": result.outcome,
                        "stdout": result.stdout.decode(errors="replace"),
                        "stderr": result.stderr.decode(errors="replace"),
                    },
                )
                if result.returncode or result.outcome != "exited":
                    raise InputError(
                        "Setup command failed; retained evidence describes the outcome."
                    )
        finally:
            self.setup_seconds = time.monotonic() - started
            owner.flush(self.own)
            self.cleanup = owner.stop(timeout=3, terminal_closed=True).model_copy(
                update={"logs_drained": True}
            )
            publish(force=True)
            evidence.record("cleanup", self.cleanup)
            if not self.cleanup.known_writers_stopped:
                raise InputError("Setup cleanup could not stop every known writer.")

    def _driver(self) -> DriverSession:
        if self.state.launch_plan is None:
            raise InputError("Driver needs a saved launch plan.")
        limits = DriverLimits.for_duration(float(self.case.budgets.subject_seconds))
        artifacts = ArtifactWriter(self.root / "driver", limits.max_artifact_bytes)
        if self.options.mode == "offline-fixture":
            source = FakeEventSource([])
            return DriverSession(
                FixtureTerminal(self.experiment.fixture, source, self.state.launch_plan.agent),
                source,
                artifacts,
                agent=self.state.launch_plan.agent,
                limits=limits,
                cancelled=self.cancelled,
            )
        if self.options.runtime_root is None or self.options.transport is None:
            raise InputError(
                "Native mode requires an explicit short runtime root and tui-test executable."
            )
        runtime = Path(self.options.runtime_root) / uuid4().hex[:10]
        terminal = TuiTestTerminal(Path(self.options.transport), runtime, limits=limits)
        self.terminal = terminal
        terminal.artifacts.on_record = self._transport_event
        self.update(runtime=str(runtime), terminal_session=terminal.session)
        source_native = NativeLogSource(
            native_log_root(
                Path(self.state.launch_plan.config_roots["config"]),
                self.state.launch_plan.agent,
            ),
            self.state.launch_plan.agent,
            artifacts,
            limits=limits,
        )
        return DriverSession(
            terminal,
            source_native,
            artifacts,
            agent=self.state.launch_plan.agent,
            limits=limits,
            cancelled=self.cancelled,
        )

    def execute(self, inherited: Mapping[str, str]) -> None:
        try:
            self.update(stage=TrialStage.PREPARING)
            ensure_directory(self.root)
            materialize_repository(self.store, self.case.repository_id, self.workspace)
            self._setup(inherited)
            plan = materialize_profile(
                self.store,
                self.trial.profile_id,
                self.root / "profile",
                self.workspace,
                LaunchOptions(
                    strict=self.options.strict,
                    probe_version=True,
                    live_config=self.journal.journal.path / LIVE_CONFIG_DIRECTORY,
                ),
            )
            self.update(launch_plan=plan)
            if not plan.launchable:
                raise InputError("Frozen profile preflight is not launchable.")
            if self.cancelled.is_set():
                raise InputError("Subject cancelled before launch.")
            self.started = time.monotonic()
            self.deadline = self.started + self.case.budgets.subject_seconds
            self.driver = self._driver()
            with RuntimeBindings(plan, inherited), self.driver:
                self.update(stage=TrialStage.LAUNCHING, launch_attempted=True)
                observation = self.driver.start(plan, launch_environment(plan, inherited))
                self.update(launch=self.driver.launch, stage=TrialStage.INTERACTING)
                self.observation = observation
                self._interact(inherited)
        except KeyboardInterrupt:
            self.cancelled.set()
            self.update(stop_reason="cancelled")
        except (DryheaveError, OSError, psutil.Error) as error:
            self.errors.append(describe_error(error))
            self.update(stop_reason="cancelled" if self.cancelled.is_set() else "error")
        finally:
            self.stop()
        self.capture()

    def _interact(self, inherited: Mapping[str, str]) -> None:
        if self.driver is None or self.observation is None:
            raise InputError("Interaction needs a started driver.")
        if self.observation.state != "ready":
            self.update(stop_reason=self._stop_reason(self.observation))
            return
        prompt: str = self.case.initial_prompt
        policy: SubmissionPolicy = self.experiment.initial_input_policy
        while True:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                self.update(stop_reason="cancelled" if self.cancelled.is_set() else "timeout")
                return
            submission = self.driver.prepare(prompt, policy=policy)
            self.update(submission=submission)
            self.journal.evidence(self.state, "submission-intent", submission)
            self.deliveries += 1
            delivered = self.accepted
            self.observation = self.driver.deliver(submission)
            if awaits_screen(self.observation) and self.driver.enter_attempted:
                self.observation = self._dialogs(inherited, prompt, delivered)
            previous = self.accepted
            self.accepted = self.driver.accepted_count
            if self.accepted > previous:
                self.dialogue.append(DialogueMessage(role="user", text=prompt))
            if self.observation.text:
                self.dialogue.append(DialogueMessage(role="assistant", text=self.observation.text))
            self.journal.journal.append(
                "interaction",
                {
                    "prompt": prompt,
                    "observation": self.observation.model_dump(mode="json"),
                    "accepted_turns": self.accepted,
                    "delivery_attempts": self.deliveries,
                },
                trial_id=self.state.trial_id,
                attempt_id=self.state.attempt_id,
            )
            if self.pending is not None and self.accepted > previous:
                self._advance(self.pending, self.position + 1)
            self.pending = None
            if self.observation.state not in {"completed", "question"}:
                self.update(
                    stop_reason=self.state.stop_reason or self._stop_reason(self.observation)
                )
                return
            if self.accepted >= self.case.budgets.max_turns:
                self.update(stop_reason="turn_budget")
                return
            following = self._follow(inherited)
            if following is None:
                return
            prompt, policy = following

    def _dialogs(self, inherited: Mapping[str, str], prompt: str, delivered: int) -> Observation:
        observation = self.observation
        if self.driver is None or observation is None:
            raise InputError("Dialog answers need a started driver.")
        self.driver.observe_turn()
        self.accepted = self.driver.accepted_count
        if self.accepted > delivered:
            self.dialogue.append(DialogueMessage(role="user", text=prompt))
            if self.pending is not None:
                self._advance(self.pending, self.position + 1)
                self.pending = None
        refreshed = False
        while awaits_screen(observation):
            interruption = self._interruption()
            if interruption is not None:
                self.update(stop_reason=interruption)
                return observation
            asked = self.driver.question()
            screen = None if asked is not None else self.driver.screen()
            if asked is not None:
                answered = self._answer_dialog(inherited, asked, prompt, delivered)
            elif screen is not None:
                answered = self._answer_screen(inherited, screen, prompt, delivered)
            elif refreshed:
                return observation
            else:
                refreshed = True
                observation = self.driver.reconcile(wait=True)
                continue
            if answered is None:
                return observation
            observation, refreshed = answered, False
            if self.state.stop_reason is not None:
                return observation
        return observation

    def _ask(
        self, prompt: str, delivered: int, *, dialog: DialogPrompt | None, screen: str | None
    ) -> tuple[DialogAsk, str]:
        source = dialog_source(
            self.steps, self.position + 1 if self.pending is not None else self.position
        )
        return DialogAsk(
            prompt=dialog,
            screen=screen,
            answer_hint=source.answer_hint if source is not None else None,
            dialogue=tuple(self.dialogue)
            if self.accepted > delivered
            else (*self.dialogue, DialogueMessage(role="user", text=prompt)),
        ), source.step_id if source is not None else "dialog"

    def _answer_dialog(
        self, inherited: Mapping[str, str], asked: QuestionDialog, prompt: str, delivered: int
    ) -> Observation | None:
        if self.driver is None:
            raise InputError("Dialog answers need a started driver.")
        dialog, step_id = self._ask(
            prompt,
            delivered,
            dialog=DialogPrompt(
                question=asked.question,
                options=asked.options,
                multi_select=asked.multi_select,
                allows_text=asked.allows_text,
            ),
            screen=None,
        )
        before = {call.call_id for call in self.state.calls}
        decision = self._simulate(inherited, dialog=dialog)
        selection = (
            None
            if decision is None or dialog.prompt is None
            else dialog_selection(dialog.prompt, decision)
        )
        if selection is None:
            return None
        answered = self.driver.answered
        observation = self.driver.answer(asked, selection)
        if self.driver.answered > answered:
            self._answered(step_id, before)
        return observation

    def _answer_screen(
        self, inherited: Mapping[str, str], screen: Screen, prompt: str, delivered: int
    ) -> Observation:
        driver = self.driver
        if driver is None:
            raise InputError("Screen answers need a started driver.")
        tally = ScreenTally(screen_identity(screen.text), self._call_ids())
        steps = 0
        current = screen
        while True:
            if current.exited is not None:
                return driver.exit_observation(current)
            interruption = self._interruption()
            if interruption is not None:
                return self._screen_stop(current, interruption, "Screen answering was interrupted.")
            if (
                steps >= SCREEN_STEPS
                or tally.acted >= SCREEN_ACTIONS
                or tally.waited >= SCREEN_WAITS
            ):
                return self._screen_stop(
                    current, "needs_input", "The screen stayed open after its bounded answer steps."
                )
            dialog, step_id = self._ask(prompt, delivered, dialog=None, screen=current.text)
            decision = self._simulate(inherited, dialog=dialog)
            if decision is None:
                return self._screen_stop(current, None, "The simulator did not answer the screen.")
            if decision.screen_kind == "approval":
                return self._screen_stop(
                    current,
                    "needs_input",
                    "The simulator judged the screen a permission prompt; it was not answered.",
                    state="approval",
                )
            steps += 1
            sent = driver.screen_actions
            if decision.screen_kind == "busy" or decision.screen_action is None:
                until = tally.wait_until or time.monotonic() + SCREEN_WAIT_SECONDS
                tally = replace(tally, waited=tally.waited + 1, wait_until=until)
                observation = driver.await_change(current.text, until)
            else:
                tally = replace(tally, acted=tally.acted + 1)
                observation = driver.act(
                    decision.screen_action, current.text, decision.screen_target
                )
            tally = replace(tally, pressed=tally.pressed or driver.screen_actions > sent)
            self.journal.record(
                self.state,
                "screen-step",
                {
                    "call_id": self.state.calls[-1].call_id,
                    "screen_kind": decision.screen_kind,
                    "action": None
                    if decision.screen_action is None
                    else decision.screen_action.model_dump(mode="json"),
                    "target": decision.screen_target,
                    "state": observation.state,
                    "reason": observation.reason,
                },
            )
            if observation.reason in {SCREEN_ACTION_REFUSED, SCREEN_WAIT_EXPIRED}:
                return self._screen_stop(
                    current, "needs_input", observation.reason, state=observation.state
                )
            following = driver.screen() if awaits_screen(observation) else None
            if following is None or screen_identity(following.text) != tally.identity:
                if (
                    tally.pressed
                    and observation.state not in SCREEN_UNSETTLED
                    and observation.reason != SCREEN_KEY_UNCERTAIN
                ):
                    self._answered(step_id, tally.before)
                tally = ScreenTally(
                    () if following is None else screen_identity(following.text),
                    self._call_ids(),
                )
            if following is None:
                return (
                    driver.reconcile(wait=True)
                    if observation.state in TURN_PENDING
                    else observation
                )
            current = following

    def _call_ids(self) -> frozenset[str]:
        return frozenset(call.call_id for call in self.state.calls)

    def _screen_stop(
        self, screen: Screen, stop_reason: str | None, reason: str, *, state: str = "unsupported"
    ) -> Observation:
        if stop_reason is not None:
            self.update(stop_reason=stop_reason)
        driver = self.driver
        return Observation.model_validate(
            {
                "state": state,
                "reason": reason,
                "submission_id": driver.submission.submission_id
                if driver is not None and driver.submission is not None
                else None,
                "session_id": driver.root_session_id if driver is not None else None,
                "text": screen.text,
            }
        )

    def _answered(self, step_id: str, before: AbstractSet[str]) -> None:
        self._record(
            WorkflowEvent(
                step_id=step_id,
                kind="dialog",
                outcome="answered",
                turn=self.driver.accepted_count if self.driver is not None else self.accepted,
                call_ids=tuple(
                    call.call_id
                    for call in self.state.calls
                    if call.call_id not in before and call.role == "simulator"
                ),
            )
        )

    def _follow(self, inherited: Mapping[str, str]) -> tuple[str, SubmissionPolicy] | None:
        decision = self._simulate(inherited)
        if decision is None:
            return None
        if decision.action == "reply":
            return decision.text, "conversation"
        if not self.steps or decision.stop_kind != "done":
            self.update(stop_reason="simulator_stop")
            return None
        return self._done_point(inherited)

    def _done_point(self, inherited: Mapping[str, str]) -> tuple[str, SubmissionPolicy] | None:
        while True:
            if next_workflow_action(self.steps, self.position) == "complete":
                self.update(stop_reason="workflow_complete")
                return None
            step = self.steps[self.position]
            if isinstance(step, PromptStep):
                self.pending = WorkflowEvent(
                    step_id=step.step_id, kind="prompt", outcome="sent", turn=self.accepted
                )
                return step.prompt, ("curated-native-command" if step.slash else "conversation")
            if self.inspections >= self.case.budgets.max_turns:
                self.update(stop_reason="turn_budget")
                return None
            if not self._inspect_step(step, inherited):
                return None
            if self.last_inspection is None or self.last_inspection.status == "passed":
                continue
            return self._request_fix(step, self.last_inspection.findings, inherited)

    def _inspect_step(self, step: InspectStep, inherited: Mapping[str, str]) -> bool:
        previous = self.last_inspection
        result = self._inspect(step, inherited)
        reused = (
            previous is not None
            and previous.step_id == result.step_id
            and previous.snapshot_digest == result.snapshot_digest
            and result.status == "failed"
            and not result.calls
        )
        event = WorkflowEvent(
            step_id=step.step_id,
            kind="inspect",
            outcome="reused" if reused else result.status,
            turn=self.accepted,
            failed_criteria=tuple(item.criterion_id for item in result.findings),
            call_ids=tuple(call.call_id for call in result.calls),
            seconds=result.seconds,
        )
        if result.status == "unavailable":
            self._advance(event, self.position)
            self.update(stop_reason=self._interruption() or "inspection_unavailable")
            return False
        self.last_inspection = result
        self._advance(event, self.position + (result.status == "passed"))
        return True

    def _request_fix(
        self,
        step: InspectStep,
        findings: tuple[InspectionFinding, ...],
        inherited: Mapping[str, str],
    ) -> tuple[str, SubmissionPolicy] | None:
        before = {call.call_id for call in self.state.calls}
        decision = self._simulate(inherited, inspection=findings)
        if decision is None or decision.action != "reply":
            return None
        self._advance(
            WorkflowEvent(
                step_id=step.step_id,
                kind="inspect",
                outcome="fix_requested",
                turn=self.accepted,
                failed_criteria=tuple(item.criterion_id for item in findings),
                call_ids=tuple(
                    call.call_id for call in self.state.calls if call.call_id not in before
                ),
            ),
            self.position,
        )
        return decision.text, "conversation"

    def _advance(self, event: WorkflowEvent, position: int) -> None:
        self.journal.record(
            self.state,
            WORKFLOW_STEP_EVENT,
            WorkflowStepRecord(event=event, position=position).model_dump(mode="json"),
        )
        self.position = position
        self.update(workflow_position=position)

    def _record(self, event: WorkflowEvent) -> None:
        self.journal.record(
            self.state,
            WORKFLOW_STEP_EVENT,
            WorkflowStepRecord(event=event, position=self.position).model_dump(mode="json"),
        )

    def _interruption(self) -> str | None:
        if self.cancelled.is_set():
            return "cancelled"
        return "timeout" if time.monotonic() >= self.deadline else None

    def _inspect(self, step: InspectStep, inherited: Mapping[str, str]) -> InspectionResult:
        self.inspections += 1
        scratch = self.store.root / "scratch"
        privates = tuple(scratch / f"inspection-{uuid4().hex}" for _ in range(INSPECTION_ATTEMPTS))
        self.journal.record(
            self.state,
            "inspection-intent",
            {
                "step_id": step.step_id,
                "position": self.position,
                "turn": self.accepted,
                "private": [str(path) for path in privates],
            },
        )
        calls: list[RoleCall] = []
        judged = 0
        seconds = 0.0
        result: InspectionResult | None = None
        for index in range(INSPECTION_ATTEMPTS):
            observed = self._observed_events()
            attempt, made = self._inspect_once(step, inherited, privates[index], judged)
            judged += made
            calls.extend(attempt.calls)
            seconds += attempt.seconds
            if self._observed_events() == observed:
                result = attempt
                break
        if result is None:
            result = InspectionResult(
                step_id=step.step_id,
                status="unavailable",
                findings=(),
                calls=(),
                snapshot_digest=None,
                error="events_arrived",
                seconds=0,
            )
        result = result.model_copy(update={"calls": tuple(calls), "seconds": seconds})
        self.journal.record(
            self.state,
            "inspection-result",
            {"position": self.position, "result": result.model_dump(mode="json")},
        )
        return result

    def _inspect_once(
        self, step: InspectStep, inherited: Mapping[str, str], private: Path, judged: int
    ) -> tuple[InspectionResult, int]:
        intents: list[str] = []

        def context(deadline: float, runtime_root: Path) -> CallContext:
            call_id = "j-" + uuid4().hex
            intents.append(call_id)
            self.update(
                calls=(
                    *self.state.calls,
                    RoleCall(call_id=call_id, role="judge", status="intent"),
                )
            )
            return CallContext(
                call_id=call_id,
                index=judged + len(intents) - 1,
                deadline=deadline,
                cancelled=self.cancelled,
                inherited=inherited,
                on_identities=self.own,
                runtime_root=runtime_root,
            )

        request = InspectionRequest(
            store=self.store,
            case_id=self.trial.case_id,
            case=self.case,
            step=step,
            workspace=self.workspace,
            transcript=self._transcript(),
            recipe=self.inspector,
            scratch=self.store.root / "scratch",
            retained=self.inspection_root,
            call_context=context,
            deadline=self.deadline,
            cancelled=self.cancelled,
            on_identities=self.own,
            previous=self.last_inspection,
            private=private,
        )
        self._publish()
        try:
            result = inspect_workspace(request)
        except (DryheaveError, OSError) as error:
            result = InspectionResult(
                step_id=step.step_id,
                status="unavailable",
                findings=(),
                calls=(),
                snapshot_digest=None,
                error=type(error).__name__,
                seconds=0,
            )
        finally:
            self._publish()
        settled = {call.call_id: call for call in result.calls}
        self.update(
            calls=tuple(
                settled.get(
                    call.call_id,
                    call.model_copy(update={"status": "interrupted"})
                    if call.call_id in intents and call.status == "intent"
                    else call,
                )
                for call in self.state.calls
            )
        )
        return result, len(intents)

    def _transcript(self) -> tuple[TranscriptEntry, ...]:
        workspace = str(self.workspace)
        roots = tuple(dict.fromkeys((workspace, os.path.realpath(workspace))))
        events = tuple(self.driver.events) if self.driver is not None else ()
        return judge_transcript_from_events(events, tuple(self.dialogue), roots)

    def _observed_events(self) -> int:
        if self.driver is None or self.driver.submission is None:
            return 0
        self.driver.reconcile()
        return len(self.driver.events)

    def _publish(self) -> None:
        if self.terminal is not None:
            self.terminal.owner.publish(self.own, force=True)

    @staticmethod
    def _stop_reason(observation: Observation) -> str:
        return (
            "needs_input"
            if observation.state in {"approval", "structured_input", "unsupported", "partial_input"}
            else observation.state
        )

    def _simulate(
        self,
        inherited: Mapping[str, str],
        *,
        retry: bool = True,
        inspection: tuple[InspectionFinding, ...] = (),
        dialog: DialogAsk | None = None,
    ) -> SimulatorDecision | None:
        made = tuple(call for call in self.state.calls if call.role == "simulator")
        spent = sum(call.elapsed_seconds or 0 for call in made)
        if len(made) >= self.recipe.budget.max_calls or spent >= self.recipe.budget.total_seconds:
            self.update(stop_reason="controller_budget")
            return None
        request = simulator_projection(
            self.case,
            self.persona,
            tuple(self.dialogue) if dialog is None else dialog.dialogue,
            self.trial.seed,
            recipe=self.recipe,
            inspection=inspection,
        )
        if dialog is not None and dialog.screen is not None:
            request = screen_projection(
                request, dialog.screen, dialog.answer_hint, self.recipe.budget.max_input_bytes
            )
        elif dialog is not None and dialog.prompt is not None:
            request = dialog_projection(
                request, dialog.prompt, dialog.answer_hint, self.recipe.budget.max_input_bytes
            )
        call_id = "c-" + uuid4().hex
        call = RoleCall(call_id=call_id, status="intent")
        index = len(made)
        self.update(calls=(*self.state.calls, call))
        self.journal.evidence(self.state, "controller-intent", call)
        artifacts = ArtifactWriter(
            self.root / "controller" / call_id,
            8 * self.recipe.budget.max_output_bytes + self.recipe.budget.max_input_bytes,
        )
        deadline = min(
            self.deadline,
            time.monotonic() + self.recipe.budget.total_seconds - spent,
            time.monotonic() + self.recipe.budget.call_seconds,
        )
        context = CallContext(
            call_id=call_id,
            index=index,
            deadline=deadline,
            cancelled=self.cancelled,
            inherited=inherited,
            on_identities=self.own,
            runtime_root=self.root / "role-runtime" / call_id,
        )
        self._publish()
        try:
            call = invoke_controller(self.recipe, request, artifacts, context)
        finally:
            self._replace_call(call.model_copy(update={"cleanup": context.cleanup}))
        rejection = self._rejection(call.decision, inspection, dialog)
        if rejection is not None:
            call = call.model_copy(update={"status": "failed", "error": rejection})
            self._replace_call(call.model_copy(update={"cleanup": context.cleanup}))
        self.journal.evidence(self.state, "controller-result", call)
        if call.status != "completed" or call.decision is None:
            stopped = (
                context.cleanup.known_writers_stopped
                if context.cleanup is not None
                else call.error in RETRIED_REJECTIONS
            )
            if retry and stopped and self._interruption() is None:
                return self._simulate(inherited, retry=False, inspection=inspection, dialog=dialog)
            self.update(
                stop_reason=self._interruption()
                or ("needs_input" if call.error in UNANSWERED_REJECTIONS else "controller_error")
            )
            return None
        return call.decision

    @staticmethod
    def _rejection(
        decision: SimulatorDecision | None,
        inspection: tuple[InspectionFinding, ...],
        dialog: DialogAsk | None,
    ) -> str | None:
        if decision is None:
            return None
        if inspection and decision.action == "stop":
            return "stop_with_findings"
        if dialog is not None and dialog.screen is not None:
            return None if screen_answer(decision) else INVALID_SCREEN_ANSWER
        if dialog is not None and dialog.prompt is not None:
            return (
                INVALID_DIALOG_ANSWER if dialog_selection(dialog.prompt, decision) is None else None
            )
        return "empty_reply" if decision.action == "reply" and not decision.text.strip() else None

    def _replace_call(self, call: RoleCall) -> None:
        self.update(
            calls=tuple(call if item.call_id == call.call_id else item for item in self.state.calls)
        )

    def stop(self) -> None:
        if self.started is not None:
            self.subject_seconds = time.monotonic() - self.started
        previous = self.cleanup
        try:
            self.update(
                stage=TrialStage.STOPPING,
                calls=tuple(
                    call.model_copy(update={"status": "interrupted"})
                    if call.status == "intent"
                    else call
                    for call in self.state.calls
                ),
            )
        finally:
            self._close_driver()
            self.cleanup = _combine_cleanup(
                previous,
                self.cleanup,
                *(call.cleanup for call in self.state.calls if call.cleanup is not None),
            )
        self.own(self.cleanup.owned)
        self.journal.evidence(self.state, "cleanup", self.cleanup)
        self.update(stop_reason=self.state.stop_reason or "interrupted")

    def _close_driver(self) -> None:
        if self.driver is not None:
            try:
                self.cleanup = self.driver.close()
                self.usage = self.driver.usage_snapshot()
            except (OSError, DryheaveError, psutil.Error) as error:
                self.errors.append(str(error))
                self.cleanup = CleanupReport(
                    terminal_closed=False,
                    known_writers_stopped=False,
                    errors=("driver_cleanup_failed",),
                )
            self.update(launch=self.driver.launch or self.state.launch)
        elif self.terminal is not None:
            self.cleanup = self.terminal.close()

    def capture(self) -> None:
        blobs: dict[str, bytes] = {}
        quiescent = self.cleanup.terminal_closed and self.cleanup.known_writers_stopped
        try:
            if not quiescent:
                raise InputError("Capture requires reconciled terminal and writer cleanup.")
            workspace, blobs = capture_workspace(
                self.store,
                self.case.repository_id,
                self.workspace,
                self.case.capture_policy,
                self.root / "scratch",
            )
        except (OSError, DryheaveError):
            workspace = WorkspaceCapture(
                complete=False,
                git_complete=False,
                omissions=(
                    CaptureOmission(
                        path=".",
                        reason="capture_unavailable" if quiescent else "cleanup_unresolved",
                    ),
                ),
            )
        evidence, omissions = self._evidence() if quiescent else ({}, ("cleanup_unresolved",))
        blobs.update(evidence)
        captured = CapturedAttempt(
            run_id=self.journal.journal.metadata.run_id,
            experiment_id=self.journal.journal.metadata.experiment_id,
            trial_id=self.trial.trial_id,
            attempt_id=self.state.attempt_id,
            case_id=self.trial.case_id,
            profile_id=self.trial.profile_id,
            persona_id=self.case.persona_id,
            simulator_id=self.experiment.simulator_id,
            scoring_id=self.experiment.scoring_id,
            mode=self.options.mode,
            created_at=self.state.created_at,
            captured_at=datetime.now(UTC),
            subject_seconds=self.subject_seconds,
            setup_seconds=self.setup_seconds,
            interaction_coverage="partial" if self.recovered else "observed",
            stop_reason=self.state.stop_reason or "interrupted",
            last_observation=self.observation,
            accepted_turns=self.accepted,
            delivery_attempts=self.deliveries,
            dialogue=tuple(self.dialogue),
            launch_plan=self.state.launch_plan,
            launch=self.state.launch,
            cleanup=self.cleanup,
            usage=self.usage,
            controller_calls=self.state.calls,
            workflow=workflow_progress(
                self.steps, self.journal.journal.events, self.state.attempt_id
            ),
            workspace=workspace,
            evidence_files={name: digest(content) for name, content in evidence.items()},
            evidence_omissions=omissions,
            errors=tuple(self.errors),
        )
        try:
            identifier = self.store.put(
                ObjectKind.CAPTURE, captured, files=blobs, references=(captured.experiment_id,)
            )
        except DryheaveError as error:
            identifier = self.store.put(
                ObjectKind.QUARANTINE,
                QuarantinedCapture(failure=str(error), capture=captured),
                files=blobs,
            )
            self.update(
                stage=TrialStage.CAPTURED, quarantine_id=identifier, capture_error=str(error)
            )
            return
        self.update(stage=TrialStage.CAPTURED, capture_id=identifier)

    def _evidence(self) -> tuple[dict[str, bytes], tuple[str, ...]]:
        builder = CaptureBuilder(
            CapturePolicy(max_bytes=128 * 1024 * 1024, max_files=30000, max_depth=32)
        )
        sources = [
            (name, self.root / name)
            for name in ("setup", "driver", "controller", "recovery-driver")
        ]
        if self.state.runtime is not None:
            sources.append(("transport", Path(self.state.runtime)))
        sources.append(("inspection", self.inspection_root))
        for name, root in sources:
            if not root.exists():
                continue
            try:
                paths = builder.walk(root)
                for path, mode in sorted(paths.items()):
                    builder.take(root, path, mode, prefix=f"evidence/{name}")
            except (OSError, DryheaveError):
                builder.omit(name, "evidence_unavailable")
        return builder.blobs, tuple(f"{item.path}:{item.reason}" for item in builder.omissions)
