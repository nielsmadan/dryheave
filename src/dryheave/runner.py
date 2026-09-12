import os
import signal
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from dryheave.cases import load_frozen_case
from dryheave.constants import MAX_RUNTIME_PATH_BYTES
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import FrozenExperiment
from dryheave.experiments import inspect_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.models import ObjectKind, TrialStage
from dryheave.native_recovery import reconcile_store_ownership
from dryheave.runner_attempt import TrialExecution
from dryheave.runner_models import CapturedAttempt, RunOptions, RunSummary
from dryheave.runner_recovery import recover_attempt
from dryheave.runner_state import ExecutionJournal
from dryheave.storage import ObjectStore


def load_capture(store: ObjectStore, reference: str) -> CapturedAttempt:
    identifier = store.resolve(reference)
    captured = store.load(identifier, CapturedAttempt, kind=ObjectKind.CAPTURE)
    manifest = store.get(identifier, kind=ObjectKind.CAPTURE)
    if manifest.references != (captured.experiment_id,):
        raise InputError("Captured attempt must reference its exact frozen experiment.")
    experiment = load_experiment(store, captured.experiment_id)
    trial = next((item for item in experiment.trials if item.trial_id == captured.trial_id), None)
    if trial is None or (
        trial.case_id,
        trial.profile_id,
        experiment.simulator_id,
        experiment.scoring_id,
    ) != (captured.case_id, captured.profile_id, captured.simulator_id, captured.scoring_id):
        raise InputError("Captured attempt input identities differ from the experiment.")
    if load_frozen_case(store, captured.case_id).persona_id != captured.persona_id:
        raise InputError("Captured persona differs from the frozen case.")
    if captured.launch_plan is not None and captured.launch_plan.profile_id != captured.profile_id:
        raise InputError("Captured launch plan differs from the trial profile.")
    blobs = store.read_blobs(identifier)
    expected = dict(captured.evidence_files)
    for entry in (*captured.workspace.files, *captured.workspace.git_files):
        if entry.blob in expected:
            raise InputError("Capture evidence overlaps task or Git files.")
        expected[entry.blob] = entry.sha256
        if entry.blob not in blobs:
            raise InputError("Capture blob map omits a declared file.")
        if len(blobs[entry.blob]) != entry.size:
            raise InputError("Captured file size differs from its declared size.")
    if any(not name.startswith("evidence/") for name in captured.evidence_files):
        raise InputError("Captured evidence requires its dedicated blob namespace.")
    for name in (captured.workspace.patch_file, captured.workspace.git_inventory_file):
        if name is not None:
            if name not in {"final.patch", "git-inventory.txt"} or name in expected:
                raise InputError("Invalid reserved capture evidence path.")
            expected[name] = manifest.files.get(name, "")
    if manifest.files != expected:
        raise InputError("Capture blob map differs from its declared evidence.")
    return captured


def _summary(
    execution: ExecutionJournal, experiment: FrozenExperiment | None, input_error: str | None = None
) -> RunSummary:
    attempted = {state.trial_id for state in execution.attempts.values()}
    return RunSummary(
        run_id=execution.journal.metadata.run_id,
        experiment_id=execution.journal.metadata.experiment_id,
        options=execution.options,
        scheduled_trials=len(experiment.trials) if experiment is not None else None,
        input_error=input_error,
        attempts=tuple(execution.attempts.values()),
        unstarted_trials=tuple(
            trial.trial_id
            for trial in (experiment.trials if experiment else ())
            if trial.trial_id not in attempted
        ),
        pending_assessment=tuple(
            state.capture_id
            for state in execution.attempts.values()
            if state.capture_id is not None and state.stage != TrialStage.FINISHED
        ),
    )


def run_status(store: ObjectStore, run_id: str) -> RunSummary:
    journal = RunStore(store.root).inspect(run_id)
    execution = ExecutionJournal(journal)
    experiment, error = inspect_experiment(store, journal.metadata.experiment_id)
    return _summary(execution, experiment, error)


def _options(options: RunOptions) -> RunOptions:
    if options.mode == "native":
        if options.runtime_root is None or options.transport is None:
            raise InputError("Native run requires --runtime-root PATH and --tui-test PATH.")
        root = Path(options.runtime_root).absolute()
        if len(os.fsencode(root / ("x" * 10))) > MAX_RUNTIME_PATH_BYTES:
            raise InputError(
                "Runtime root must leave room for a unique 10-byte child within the 70-byte TUI_TEST_HOME limit."
            )
        return options.model_copy(
            update={"runtime_root": str(root), "transport": str(Path(options.transport).absolute())}
        )
    if options.runtime_root is not None or options.transport is not None:
        raise InputError("Offline fixture mode has no native terminal runtime.")
    return options


@contextmanager
def cancellation_signals(cancelled: threading.Event) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {kind: signal.getsignal(kind) for kind in (signal.SIGTERM, signal.SIGINT)}

    def cancel(_signal: int, _frame: object) -> None:
        cancelled.set()

    try:
        for kind in previous:
            signal.signal(kind, cancel)
        yield
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def run_experiment(
    store: ObjectStore,
    reference: str | None = None,
    *,
    resume: str | None = None,
    retry: tuple[str, ...] = (),
    options: RunOptions | None = None,
    cancelled: threading.Event | None = None,
) -> RunSummary:
    identifier = store.resolve(reference) if reference is not None else None
    if resume is None:
        if identifier is None:
            raise InputError("A new run requires a frozen experiment reference.")
        experiment = load_experiment(store, identifier)
        selected = _options(options or RunOptions())
        if selected.mode == "offline-fixture" and not experiment.fixture:
            raise InputError("Offline fixture mode requires a frozen subject fixture.")
        if retry:
            raise InputError("Explicit retry requires --resume RUN_ID.")
        resume = RunStore(store.root).create(identifier)
        print(f"dryheave run reserved: {resume}", file=sys.stderr, flush=True)
    cancelled = cancelled or threading.Event()
    with RunStore(store.root).open(resume, experiment_id=identifier) as journal:
        execution = ExecutionJournal(journal)
        if execution.options is None:
            execution.configure(_options(options or RunOptions()))
        elif options is not None and execution.options != _options(options):
            raise InputError("Resume cannot change frozen run execution options.")
        with store.native_lock(), cancellation_signals(cancelled):
            reconcile_store_ownership(store, execution)
            try:
                experiment = load_experiment(store, journal.metadata.experiment_id)
            except DryheaveError as error:
                journal.append(
                    "input-integrity-failed", {"code": error.code, "message": str(error)}
                )
                for state in tuple(execution.attempts.values()):
                    if state.stage == TrialStage.STOPPING and state.capture_id is None:
                        execution.save(
                            state.model_copy(
                                update={
                                    "stop_reason": "invalid_inputs",
                                    "capture_error": str(error),
                                }
                            )
                        )
                raise
            _run_trials(store, execution, experiment, retry, cancelled, os.environ)
        result = _summary(execution, experiment)
        journal.write_result(result.model_dump(mode="json"))
        return result


def _run_trials(
    store: ObjectStore,
    execution: ExecutionJournal,
    experiment: FrozenExperiment,
    retry: tuple[str, ...],
    cancelled: threading.Event,
    inherited: Mapping[str, str],
) -> None:
    trials = {trial.trial_id: trial for trial in experiment.trials}
    attempted = {state.trial_id for state in execution.attempts.values()}
    if len(set(retry)) != len(retry) or any(identifier not in attempted for identifier in retry):
        raise InputError("Retry must name distinct previously attempted trials in this run.")
    if any(state.capture_error for state in execution.attempts.values()):
        raise InputError("Frozen input integrity failed; further subject launches are blocked.")
    for state in tuple(execution.attempts.values()):
        if state.trial_id not in trials:
            raise InputError("Journal names a trial outside its experiment.")
        if state.stage in {
            TrialStage.RESERVED,
            TrialStage.PREPARING,
            TrialStage.LAUNCHING,
            TrialStage.INTERACTING,
            TrialStage.STOPPING,
        }:
            recover_attempt(
                TrialExecution(
                    store, execution, experiment, trials[state.trial_id], state, cancelled
                )
            )
    if any(state.capture_error for state in execution.attempts.values()):
        raise InputError("Frozen input integrity failed; further subject launches are blocked.")
    for trial in experiment.trials:
        if cancelled.is_set():
            break
        if trial.trial_id in attempted and trial.trial_id not in retry:
            continue
        state = execution.reserve(trial.trial_id)
        runner = TrialExecution(store, execution, experiment, trial, state, cancelled)
        runner.execute(inherited)
        if (
            runner.state.capture_error
            or not runner.cleanup.terminal_closed
            or not runner.cleanup.known_writers_stopped
        ):
            break
