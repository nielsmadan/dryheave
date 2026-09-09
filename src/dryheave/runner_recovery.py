from pathlib import Path
from uuid import uuid4

import psutil

from dryheave.controller_models import DialogueMessage
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.fake import FakeTerminal
from dryheave.drivers.log_source import NativeLogSource
from dryheave.drivers.models import Observation
from dryheave.drivers.session import DriverSession
from dryheave.errors import DryheaveError
from dryheave.logs.base import mapping
from dryheave.models import TrialStage
from dryheave.process_ownership import ProcessOwner
from dryheave.runner_attempt import TrialExecution
from dryheave.serialization import canonical_json, parse_model


def recover_attempt(execution: TrialExecution) -> None:
    execution.subject_seconds = None
    execution.recovered = True
    execution.update(
        stage=TrialStage.STOPPING,
        stop_reason="interrupted",
        calls=tuple(
            call.model_copy(update={"status": "interrupted"}) if call.status == "intent" else call
            for call in execution.state.calls
        ),
    )
    _dialogue(execution)
    owner = ProcessOwner()
    errors = []
    try:
        for identity in execution.state.owned:
            try:
                process = psutil.Process(identity.pid)
                if process.create_time() == identity.created:
                    owner.add(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                errors.append("recovery_process_visibility_denied")
    finally:
        report = owner.stop(timeout=3, terminal_closed=False)
    identity_known = (
        not execution.state.launch_attempted
        or (execution.state.launch is not None and execution.state.launch.daemon is not None)
        or execution.options.mode == "offline-fixture"
    )
    if not identity_known:
        errors.append("launch_ownership_unobserved")
    execution.cleanup = report.model_copy(
        update={
            "terminal_closed": identity_known and report.known_writers_stopped,
            "known_writers_stopped": identity_known and report.known_writers_stopped and not errors,
            "errors": (*report.errors, *errors),
        }
    )
    _drain(execution)
    execution.journal.evidence(execution.state, "recovery-cleanup", execution.cleanup)
    execution.capture()


def _dialogue(execution: TrialExecution) -> None:
    for event in execution.journal.journal.events:
        if event.attempt_id != execution.state.attempt_id or event.event != "interaction":
            continue
        observation = parse_model(
            canonical_json(mapping(event.data.get("observation"))), Observation
        )
        accepted = event.data.get("accepted_turns")
        deliveries = event.data.get("delivery_attempts")
        prompt = event.data.get("prompt")
        if type(accepted) is int and accepted > execution.accepted and isinstance(prompt, str):
            execution.dialogue.append(DialogueMessage(role="user", text=prompt))
            execution.accepted = accepted
        if type(deliveries) is int:
            execution.deliveries = deliveries
        if observation.text:
            execution.dialogue.append(DialogueMessage(role="assistant", text=observation.text))
        execution.observation = observation


def _drain(execution: TrialExecution) -> None:
    plan = execution.state.launch_plan
    if plan is None or execution.options.mode == "offline-fixture":
        execution.cleanup = execution.cleanup.model_copy(
            update={
                "logs_drained": not execution.state.launch_attempted
                or execution.options.mode == "offline-fixture"
            }
        )
        return
    try:
        artifacts = ArtifactWriter(
            execution.root / "recovery-driver" / uuid4().hex, 64 * 1024 * 1024
        )
        source = NativeLogSource(
            Path(plan.config_roots["config"]), plan.agent, artifacts, include_existing=True
        )
        driver = DriverSession(FakeTerminal([]), source, artifacts, agent=plan.agent)
        driver.launch = execution.state.launch
        report = driver.close()
        execution.usage = driver.usage_snapshot()
        execution.cleanup = execution.cleanup.model_copy(
            update={
                "logs_drained": report.logs_drained,
                "errors": (*execution.cleanup.errors, *report.errors),
            }
        )
    except (OSError, DryheaveError):
        execution.cleanup = execution.cleanup.model_copy(
            update={
                "logs_drained": False,
                "errors": (*execution.cleanup.errors, "recovery_log_drain_failed"),
            }
        )
