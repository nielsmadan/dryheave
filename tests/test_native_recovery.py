import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress

import psutil
import pytest

from dryheave.assessments import assess_run
from dryheave.cases import load_frozen_case
from dryheave.drivers.models import CleanupReport, LaunchObservation, ProcessIdentity
from dryheave.errors import DryheaveError, InputError, LockBusyError
from dryheave.experiments import create_experiment, load_experiment
from dryheave.integrity import load_assessment
from dryheave.journals import RunStore
from dryheave.models import CommandSpec, ObjectKind, TrialStage
from dryheave.native_recovery import reconcile_execution
from dryheave.runner import run_experiment, run_status
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


def _wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("Owned subprocess did not reach the expected state within its bound.")


def _alive(identity):
    try:
        process = psutil.Process(identity.pid)
        return (
            process.create_time() == identity.created and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


@contextmanager
def _writer(root):
    marker = root / "heartbeat"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib,sys,time\np=pathlib.Path(sys.argv[1]); end=time.monotonic()+20\nwhile time.monotonic()<end:\n p.write_text(str(time.monotonic())); time.sleep(.02)\n",
            str(marker),
        ],
        cwd=root,
        start_new_session=True,
    )
    try:
        _wait_until(marker.exists)
        native = psutil.Process(process.pid)
        yield (
            process,
            ProcessIdentity(
                pid=process.pid, created=native.create_time(), parent_pid=native.ppid()
            ),
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def _reserve(store, experiment_id, *, mode="offline-fixture"):
    experiment = load_experiment(store, experiment_id)
    run_id = RunStore(store.root).create(experiment_id)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode=mode))
        state = execution.reserve(experiment.trials[0].trial_id)
        execution.save(state.model_copy(update={"stage": TrialStage.PREPARING}))
    return run_id, state.attempt_id


def _record_subject(execution, attempt_id, identity):
    state = execution.own(execution.attempts[attempt_id], identity)
    launch = LaunchObservation(
        transport="disposable-subprocess-fixture",
        transport_version="fixture",
        session="interrupted-subject-fixture",
        runtime=state.workspace,
        requested_profile_id="a" * 64,
        requested_argv=(sys.executable,),
        requested_cwd=state.workspace,
        daemon=identity,
    )
    for stage in (TrialStage.LAUNCHING, TrialStage.INTERACTING):
        state = state.model_copy(
            update={"stage": stage, "launch_attempted": True, "launch": launch}
        )
        execution.save(state)


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["resume", "assess"])
def test_corrupt_inputs_do_not_prevent_recorded_writer_cleanup(
    store, benchmark, tmp_path, monkeypatch, operation
):
    experiment_id = create_experiment(store, benchmark)
    run_id, attempt_id = _reserve(store, experiment_id, mode="native")
    with _writer(tmp_path) as (writer, identity):
        with RunStore(store.root).open(run_id) as journal:
            execution = ExecutionJournal(journal)
            _record_subject(execution, attempt_id, identity)
        case_id = store.resolve("task")
        manifest = store.get(case_id)
        blob = store.object_path(case_id) / "blobs" / next(iter(manifest.files.values()))
        blob.write_bytes(b"corrupted frozen verifier")
        monkeypatch.setattr(
            "dryheave.runner.TrialExecution.execute",
            lambda *_args: pytest.fail("Invalid inputs must not launch a subject."),
        )
        if operation == "resume":
            with pytest.raises(DryheaveError):
                run_experiment(store, resume=run_id)
            writer.wait(timeout=3)
            assert not _alive(identity)
            status = run_status(store, run_id)
            assert status.input_error
            assert status.attempts[0].stop_reason == "invalid_inputs"
            assert status.attempts[0].capture_error
            assert any(
                event.event == "input-integrity-failed"
                for event in RunStore(store.root).inspect(run_id).events
            )
        results = assess_run(store, run_id)
        result = load_assessment(store, results[0])
        writer.wait(timeout=3)
        assert result.audit.input_integrity == "invalid"
        assert result.eligible is False
        assert result.completion == "indeterminate"
        assert len(run_status(store, run_id).attempts) == 1


@pytest.mark.integration
def test_new_run_reconciles_older_recorded_writer_before_execution(
    store, benchmark, tmp_path, monkeypatch
):
    from dryheave.runner_attempt import TrialExecution

    experiment_id = create_experiment(store, benchmark)
    old_run, attempt_id = _reserve(store, experiment_id, mode="native")
    execute = TrialExecution.execute
    with _writer(tmp_path) as (writer, identity):
        with RunStore(store.root).open(old_run) as journal:
            execution = ExecutionJournal(journal)
            _record_subject(execution, attempt_id, identity)

        def verify_stopped(execution, inherited):
            assert not _alive(identity)
            old = RunStore(store.root).inspect(old_run)
            assert any(event.event == "ownership-reconciled" for event in old.events)
            return execute(execution, inherited)

        monkeypatch.setattr(TrialExecution, "execute", verify_stopped)
        result = run_experiment(store, experiment_id, options=RunOptions(mode="offline-fixture"))
        writer.wait(timeout=3)
        assert len(result.attempts) == 1
        assert result.attempts[0].capture_id
        assert run_status(store, old_run).attempts[0].stage == TrialStage.STOPPING


def test_other_run_lock_contention_refuses_launch_without_deadlock(store, benchmark, monkeypatch):
    experiment_id = create_experiment(store, benchmark)
    older, _ = _reserve(store, experiment_id)
    monkeypatch.setattr(
        "dryheave.runner.TrialExecution.execute",
        lambda *_args: pytest.fail("Busy ownership must not permit a launch."),
    )
    started = time.monotonic()
    with RunStore(store.root).open(older), pytest.raises(LockBusyError):
        run_experiment(store, experiment_id, options=RunOptions(mode="offline-fixture"))
    assert time.monotonic() - started < 2
    assert run_status(store, older).attempts[0].stage == TrialStage.PREPARING


def test_other_unknown_native_launch_remains_a_blocker(store, benchmark, monkeypatch):
    experiment_id = create_experiment(store, benchmark)
    older, attempt_id = _reserve(store, experiment_id, mode="native")
    with RunStore(store.root).open(older) as journal:
        execution = ExecutionJournal(journal)
        state = execution.attempts[attempt_id]
        execution.save(
            state.model_copy(update={"stage": TrialStage.LAUNCHING, "launch_attempted": True})
        )
    monkeypatch.setattr(
        "dryheave.runner.TrialExecution.execute",
        lambda *_args: pytest.fail("Unknown launch ownership must remain blocked."),
    )
    with pytest.raises(InputError, match="reconciliation before another subject launch"):
        run_experiment(store, experiment_id, options=RunOptions(mode="offline-fixture"))
    journal = RunStore(store.root).inspect(older)
    cleanup = ExecutionJournal(journal).cleanup_reports[attempt_id]
    assert cleanup.errors == ("launch_ownership_unobserved",)
    assert cleanup.terminal_closed is False


@pytest.mark.integration
def test_new_owned_process_invalidates_successful_cleanup_in_memory_and_replay(
    store, benchmark, tmp_path
):
    experiment_id = create_experiment(store, benchmark)
    run_id, attempt_id = _reserve(store, experiment_id)
    with _writer(tmp_path) as (writer, identity):
        with RunStore(store.root).open(run_id) as journal:
            execution = ExecutionJournal(journal)
            state = execution.attempts[attempt_id]
            execution.evidence(
                state,
                "ownership-reconciled",
                CleanupReport(terminal_closed=True, known_writers_stopped=True),
            )
            execution.own(state, identity)
            assert attempt_id in execution.unreconciled
            restored = ExecutionJournal(journal)
            assert attempt_id in restored.unreconciled
            reconcile_execution(restored)
            assert restored.cleanup_reports[attempt_id].known_writers_stopped
            assert attempt_id not in restored.unreconciled
            assert ExecutionJournal(journal).cleanup_reports[attempt_id].owned == (identity,)
        writer.wait(timeout=3)


@pytest.mark.integration
def test_setup_descendant_is_durable_before_cleanup_and_survives_parent_exit(
    store, benchmark, tmp_path
):
    root = tmp_path / "setup-probe"
    root.mkdir()
    case_id = store.resolve("task")
    case = load_frozen_case(store, case_id)
    child = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1]); end=time.monotonic()+20\nwhile time.monotonic()<end:\n p.write_text(str(time.monotonic())); time.sleep(.02)\n"
    script = root / "setup.py"
    script.write_text(
        "import pathlib,subprocess,sys,time\n"
        f"root=pathlib.Path({str(root)!r})\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r},str(root/'heartbeat')],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "(root/'child-pid').write_text(str(child.pid))\n"
        "end=time.monotonic()+12\n"
        "while not (root/'release').exists() and time.monotonic()<end: time.sleep(.02)\n"
    )
    setup = (
        CommandSpec(argv=(sys.executable, str(script)), timeout_seconds=15),
        CommandSpec(
            argv=(
                sys.executable,
                "-c",
                f"import pathlib,time; pathlib.Path({str(root / 'second-command')!r}).touch(); time.sleep(12)",
            ),
            timeout_seconds=15,
        ),
    )
    frozen = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"setup": setup}),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    experiment_id = create_experiment(store, benchmark.model_copy(update={"cases": (frozen,)}))
    run_id = RunStore(store.root).create(experiment_id)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dryheave",
            "--store",
            str(store.root),
            "run",
            "--resume",
            run_id,
            "--json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    identities = ()
    try:
        _wait_until((root / "child-pid").exists)
        child_pid = int((root / "child-pid").read_text())
        _wait_until(
            lambda: (
                child_pid
                in {
                    item.pid for state in run_status(store, run_id).attempts for item in state.owned
                }
            )
        )
        state = run_status(store, run_id).attempts[0]
        identities = state.owned
        child_identity = next(item for item in identities if item.pid == child_pid)
        parent = next(item for item in identities if item.pid == child_identity.parent_pid)
        assert state.stage == TrialStage.PREPARING
        assert _alive(child_identity)
        (root / "release").touch()
        _wait_until((root / "second-command").exists)
        _wait_until(lambda: not _alive(parent))
        assert _alive(child_identity)
        identities = run_status(store, run_id).attempts[0].owned
        process.send_signal(signal.SIGKILL)
        process.communicate(timeout=3)
        assert process.returncode == -signal.SIGKILL
        result = run_experiment(store, resume=run_id)
        assert result.attempts[0].capture_id
        assert not _alive(child_identity)
        assert result.attempts[0].stop_reason == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        for identity in identities:
            if _alive(identity):
                with suppress(psutil.NoSuchProcess):
                    psutil.Process(identity.pid).kill()
