import re

import psutil

from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import InputError
from dryheave.filesystem import directory_names
from dryheave.journals import RunStore
from dryheave.models import TrialStage
from dryheave.process_ownership import ProcessOwner
from dryheave.runner_models import AttemptState
from dryheave.runner_state import ExecutionJournal
from dryheave.storage import ObjectStore

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
        return previous
    owner = ProcessOwner()
    errors = []

    def publish(identity: ProcessIdentity) -> None:
        current = execution.attempts[state.attempt_id]
        if identity not in current.owned:
            execution.own(current, identity)

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
        owner.publish(publish)
    finally:
        report = owner.stop(timeout=3, terminal_closed=False)
        owner.publish(publish)
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


def reconcile_store_ownership(store: ObjectStore, current: ExecutionJournal) -> None:
    reconcile_execution(current)
    runs = RunStore(store.root)
    for identifier in sorted(directory_names(store.root / "runs")):
        if identifier == current.journal.metadata.run_id or not re.fullmatch(
            r"[0-9a-f]{32}", identifier
        ):
            continue
        with runs.open(identifier) as journal:
            reconcile_execution(ExecutionJournal(journal))
