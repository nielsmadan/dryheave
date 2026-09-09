import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

import psutil
from pydantic import JsonValue

from dryheave.capture_models import CaptureOmission, WorkspaceCapture
from dryheave.cases import CapturePolicy, load_frozen_case
from dryheave.controller_models import ControllerRecipe, DialogueMessage, RoleCall
from dryheave.controllers import CallContext, invoke_controller, simulator_projection
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.auth import RuntimeBindings
from dryheave.drivers.fake import FakeEventSource
from dryheave.drivers.log_source import NativeLogSource
from dryheave.drivers.models import (
    CleanupReport,
    DriverLimits,
    LaunchObservation,
    Observation,
    ProcessIdentity,
    UsageObservation,
)
from dryheave.drivers.session import DriverSession
from dryheave.drivers.tui_test import TuiTestTerminal
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import FrozenExperiment, TrialSpec
from dryheave.experiments import load_role
from dryheave.filesystem import ensure_directory
from dryheave.final_capture import CaptureBuilder, capture_workspace
from dryheave.fixture_subject import FixtureTerminal
from dryheave.models import ObjectKind, StrictModel, TrialStage
from dryheave.personas import load_frozen_persona
from dryheave.process_ownership import ProcessOwner
from dryheave.processes import CommandControl, run_command
from dryheave.profile_launch import launch_environment, materialize_profile
from dryheave.repositories import materialize_repository
from dryheave.runner_models import AttemptState, CapturedAttempt, QuarantinedCapture
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import digest
from dryheave.storage import ObjectStore


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
        errors=tuple(dict.fromkeys(error for report in reports for error in report.errors)),
        coverage="fixture"
        if all(report.coverage == "fixture" for report in reports)
        else "partial-native",
    )


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
        self.known_processes = {(identity.pid, identity.created) for identity in state.owned}
        self.root = journal.attempt_root(state)
        self.workspace = Path(state.workspace)
        self.case = load_frozen_case(store, trial.case_id)
        self.persona = load_frozen_persona(store, self.case.persona_id)
        self.recipe = load_role(
            store, experiment.simulator_id, ControllerRecipe, ObjectKind.SIMULATOR
        )
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

    def identity(self, identity: ProcessIdentity) -> None:
        key = (identity.pid, identity.created)
        if key not in self.known_processes:
            self.state = self.journal.own(self.state, identity)
            self.known_processes.add(key)

    def _transport_event(self, name: str, model: StrictModel | dict[str, JsonValue]) -> None:
        if name == "launch-observed" and isinstance(model, LaunchObservation):
            self.update(launch=model)
        if self.terminal is not None:
            for identity in self.terminal.owner.snapshot():
                self.identity(identity)

    def _setup(self, inherited: Mapping[str, str]) -> None:
        started = time.monotonic()
        deadline = started + self.case.budgets.setup_seconds
        evidence = ArtifactWriter(self.root / "setup", 16 * 1024 * 1024)
        owner = ProcessOwner()

        def claim(pid: int) -> None:
            self.identity(owner.add(psutil.Process(pid)))

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
                    control=CommandControl(cancelled=self.cancelled, deadline=deadline),
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
            self.cleanup = owner.stop(timeout=3, terminal_closed=True).model_copy(
                update={"logs_drained": True}
            )
            for identity in self.cleanup.owned:
                self.identity(identity)
            evidence.record("cleanup", self.cleanup)
            if not self.cleanup.known_writers_stopped:
                raise InputError("Setup cleanup could not stop every known writer.")

    def _driver(self) -> DriverSession:
        if self.state.launch_plan is None:
            raise InputError("Driver needs a saved launch plan.")
        limits = DriverLimits(duration_seconds=float(self.case.budgets.subject_seconds))
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
            Path(self.state.launch_plan.config_roots["config"]),
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
                strict=self.options.strict,
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
            self.errors.append(str(error))
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
        prompt = self.case.initial_prompt
        while True:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                self.update(stop_reason="cancelled" if self.cancelled.is_set() else "timeout")
                return
            submission = self.driver.prepare(
                prompt,
                policy=self.experiment.initial_input_policy
                if not self.deliveries
                else "conversation",
            )
            self.update(submission=submission)
            self.journal.evidence(self.state, "submission-intent", submission)
            self.deliveries += 1
            self.observation = self.driver.deliver(submission)
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
            if self.observation.state not in {"completed", "question"}:
                self.update(stop_reason=self._stop_reason(self.observation))
                return
            if self.accepted >= self.case.budgets.max_turns:
                self.update(stop_reason="turn_budget")
                return
            prompt = self._simulate(inherited)
            if not prompt:
                return

    @staticmethod
    def _stop_reason(observation: Observation) -> str:
        return (
            "needs_input"
            if observation.state in {"approval", "structured_input", "unsupported", "partial_input"}
            else observation.state
        )

    def _simulate(self, inherited: Mapping[str, str]) -> str:
        spent = sum(call.elapsed_seconds or 0 for call in self.state.calls)
        if (
            len(self.state.calls) >= self.recipe.budget.max_calls
            or spent >= self.recipe.budget.total_seconds
        ):
            self.update(stop_reason="controller_budget")
            return ""
        request = simulator_projection(
            self.case, self.persona, tuple(self.dialogue), self.trial.seed
        )
        call_id = "c-" + uuid4().hex
        call = RoleCall(call_id=call_id, status="intent")
        index = len(self.state.calls)
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
            on_identity=self.identity,
        )
        try:
            call = invoke_controller(self.recipe, request, artifacts, context)
        finally:
            self.update(
                calls=(
                    *self.state.calls[:index],
                    call.model_copy(update={"cleanup": context.cleanup}),
                )
            )
        self.journal.evidence(self.state, "controller-result", call)
        if call.status != "completed" or call.decision is None:
            self.update(
                stop_reason="cancelled"
                if self.cancelled.is_set()
                else "timeout"
                if time.monotonic() >= self.deadline
                else "controller_error"
            )
            return ""
        if call.decision.action == "stop":
            self.update(stop_reason="simulator_stop")
            return ""
        return call.decision.text

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
        for identity in self.cleanup.owned:
            self.identity(identity)
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
