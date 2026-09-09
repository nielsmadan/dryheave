import psutil

from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import InputError
from dryheave.process_ownership import ProcessOwner
from dryheave.runner_models import AttemptState
from dryheave.runner_state import ExecutionJournal


def reconcile_grading(execution: ExecutionJournal, state: AttemptState) -> None:
    owner = ProcessOwner()
    errors = []

    def publish(identity: ProcessIdentity) -> None:
        current = execution.attempts[state.attempt_id]
        if identity not in current.owned:
            execution.own(current, identity)

    try:
        for identity in state.owned:
            try:
                process = psutil.Process(identity.pid)
                if process.create_time() == identity.created:
                    owner.add(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                errors.append("assessment_process_visibility_denied")
        owner.publish(publish)
    finally:
        cleanup = owner.stop(timeout=3, terminal_closed=True)
        owner.publish(publish)
    execution.evidence(state, "assessment-recovery-cleanup", cleanup)
    if errors or cleanup.errors or not cleanup.known_writers_stopped:
        raise InputError("Assessment recovery could not reconcile all recorded writers.")
