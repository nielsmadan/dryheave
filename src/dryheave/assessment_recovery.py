from dryheave.errors import InputError
from dryheave.native_recovery import cleanup_resolved, reconcile_attempt
from dryheave.runner_models import AttemptState
from dryheave.runner_state import ExecutionJournal


def reconcile_grading(execution: ExecutionJournal, state: AttemptState) -> None:
    cleanup = reconcile_attempt(execution, state, event="assessment-recovery-cleanup", force=True)
    if not cleanup_resolved(cleanup):
        raise InputError("Assessment recovery could not reconcile all recorded writers.")
