import re
import shutil
from pathlib import Path

import psutil

from dryheave.calibration_recovery import reconcile_calibrations
from dryheave.controller_runtime import reconcile_runtimes
from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import InputError
from dryheave.filesystem import directory_names
from dryheave.inspection import writers_confirmed_stopped
from dryheave.journals import RunStore
from dryheave.models import TrialStage
from dryheave.process_ownership import ProcessOwner
from dryheave.runner_models import AttemptState
from dryheave.runner_state import ExecutionJournal
from dryheave.storage import ObjectStore

INSPECTION_INTENT_EVENT = "inspection-intent"

ACTIVE_STAGES = {
    TrialStage.RESERVED,
    TrialStage.PREPARING,
    TrialStage.LAUNCHING,
    TrialStage.INTERACTING,
    TrialStage.STOPPING,
}


def cleanup_resolved(report: CleanupReport | None) -> bool:
    return bool(
        report and report.terminal_closed and report.known_writers_stopped and not report.errors
    )


def _inspection_scratch_paths(execution: ExecutionJournal, attempt_id: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for event in execution.journal.events:
        if event.attempt_id != attempt_id or event.event != INSPECTION_INTENT_EVENT:
            continue
        value = event.data.get("private", ())
        if isinstance(value, list):
            paths.extend(Path(item) for item in value if isinstance(item, str))
    return tuple(paths)


def reconcile_inspection_scratch(
    execution: ExecutionJournal, state: AttemptState
) -> tuple[str, ...]:
    errors: list[str] = []
    for private in _inspection_scratch_paths(execution, state.attempt_id):
        if not private.exists():
            continue
        reconcile_runtimes(private / "runtime")
        if writers_confirmed_stopped(private):
            try:
                shutil.rmtree(private)
            except OSError:
                errors.append("inspection_scratch_cleanup_failed")
        else:
            errors.append("inspection_scratch_unresolved")
    return tuple(errors)


def reconcile_attempt(
    execution: ExecutionJournal,
    state: AttemptState,
    *,
    event: str = "ownership-reconciled",
    force: bool = False,
) -> CleanupReport:
    previous = execution.cleanup_reports.get(state.attempt_id)
    if (
        not force
        and state.attempt_id not in execution.unreconciled
        and previous is not None
        and cleanup_resolved(previous)
    ):
        reconcile_runtimes(execution.attempt_root(state) / "role-runtime")
        scratch_errors = reconcile_inspection_scratch(execution, state)
        if not scratch_errors:
            return previous
        updated = previous.model_copy(
            update={"errors": tuple(dict.fromkeys((*previous.errors, *scratch_errors)))}
        )
        execution.evidence(state, event, updated)
        return updated
    owner = ProcessOwner()
    errors = []

    def publish(identities: tuple[ProcessIdentity, ...]) -> None:
        execution.own(execution.attempts[state.attempt_id], identities)

    identities = (*state.owned, *(previous.owned if previous else ()))
    if state.launch is not None and state.launch.daemon is not None:
        identities = (*identities, state.launch.daemon)
    try:
        for identity in identities:
            try:
                process = psutil.Process(identity.pid)
                if process.create_time() == identity.created:
                    owner.add(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                errors.append("recovery_process_visibility_denied")
        owner.publish(publish, force=True)
    finally:
        report = owner.stop(timeout=3, terminal_closed=False)
        owner.publish(publish, force=True)
    identity_known = (
        not state.launch_attempted
        or (state.launch is not None and state.launch.daemon is not None)
        or (execution.options is not None and execution.options.mode == "offline-fixture")
        or (previous is not None and previous.terminal_closed)
    )
    if not identity_known:
        errors.append("launch_ownership_unobserved")
    cleanup = report.model_copy(
        update={
            "terminal_closed": identity_known and report.known_writers_stopped,
            "known_writers_stopped": identity_known and report.known_writers_stopped and not errors,
            "errors": tuple(dict.fromkeys((*report.errors, *errors))),
        }
    )
    if cleanup_resolved(cleanup):
        reconcile_runtimes(execution.attempt_root(state) / "role-runtime")
        scratch_errors = reconcile_inspection_scratch(execution, state)
        if scratch_errors:
            cleanup = cleanup.model_copy(
                update={"errors": tuple(dict.fromkeys((*cleanup.errors, *scratch_errors)))}
            )
    execution.evidence(state, event, cleanup)
    return cleanup


def reconcile_execution(execution: ExecutionJournal) -> None:
    unresolved = False
    for state in tuple(execution.attempts.values()):
        cleanup = reconcile_attempt(execution, state)
        current = execution.attempts[state.attempt_id]
        if current.stage in ACTIVE_STAGES:
            execution.save(
                current.model_copy(
                    update={
                        "stage": TrialStage.STOPPING,
                        "stop_reason": current.stop_reason or "interrupted",
                        "calls": tuple(
                            call.model_copy(update={"status": "interrupted"})
                            if call.status == "intent"
                            else call
                            for call in current.calls
                        ),
                    }
                )
            )
        unresolved = unresolved or not cleanup_resolved(cleanup)
    if unresolved:
        raise InputError(
            f"Run {execution.journal.metadata.run_id} has unresolved recorded writers or terminal ownership; reconciliation before another subject launch is required."
        )


def reconcile_store_ownership(store: ObjectStore, current: ExecutionJournal | None = None) -> None:
    reconcile_calibrations(store)
    if current is not None:
        reconcile_execution(current)
    runs = RunStore(store.root)
    try:
        names = directory_names(store.root / "runs")
    except FileNotFoundError:
        return
    for identifier in sorted(names):
        if (
            current is not None and identifier == current.journal.metadata.run_id
        ) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            continue
        with runs.open(identifier) as journal:
            reconcile_execution(ExecutionJournal(journal))
