import subprocess
import sys
import time

import psutil

from dryheave.cases import load_frozen_case
from dryheave.drivers.models import ProcessIdentity
from dryheave.experiments import create_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.models import TrialStage
from dryheave.repositories import materialize_repository
from dryheave.runner import load_capture, run_experiment
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


def test_recovery_stops_only_recorded_process_identity_before_capture(store, benchmark):
    identifier = create_experiment(store, benchmark)
    experiment = load_experiment(store, identifier)
    run_id = RunStore(store.root).create(identifier)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
        state = execution.reserve(experiment.trials[0].trial_id)
        root = execution.attempt_root(state) / "workspace"
        materialize_repository(store, load_frozen_case(store, "task").repository_id, root)
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,time; p=pathlib.Path('writer.txt');\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.02)",
            ],
            cwd=root,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (root / "writer.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (root / "writer.txt").exists()
            state = execution.own(
                state,
                ProcessIdentity(pid=writer.pid, created=psutil.Process(writer.pid).create_time()),
            )
            execution.save(state.model_copy(update={"stage": TrialStage.PREPARING}))
        except BaseException:
            writer.kill()
            writer.wait()
            raise
    try:
        result = run_experiment(store, resume=run_id)
        writer.wait(timeout=3)
        capture = load_capture(store, result.pending_assessment[0])
        assert capture.cleanup.known_writers_stopped
        assert capture.stop_reason == "interrupted"
        assert (
            store.read_blob(result.pending_assessment[0], "workspace/writer.txt")
            == (root / "writer.txt").read_bytes()
        )
        assert capture.subject_seconds is None
        assert capture.interaction_coverage == "partial"
    finally:
        if writer.poll() is None:
            writer.kill()
        writer.wait()
