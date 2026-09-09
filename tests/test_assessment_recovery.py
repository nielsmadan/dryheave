import json
import signal
import subprocess
import sys
import time
from contextlib import suppress

import psutil
import pytest

from dryheave.assessments import assess_run
from dryheave.cases import load_frozen_case
from dryheave.errors import InputError
from dryheave.experiments import create_experiment
from dryheave.integrity import load_assessment
from dryheave.journals import RunStore
from dryheave.models import ObjectKind, TrialStage
from dryheave.runner import run_experiment, run_status
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


def _wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("Synthetic grader did not reach its expected state within the bound.")


def _alive(identity):
    try:
        process = psutil.Process(identity.pid)
        return (
            process.create_time() == identity.created and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def _writer_case(store, benchmark, root):
    case_id = benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    files = store.read_blobs(case_id)
    child = (
        "import pathlib,sys,time\n"
        "marker=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+15\n"
        "while time.monotonic()<deadline:\n"
        " with marker.open('a') as stream: stream.write('beat\\n')\n"
        " time.sleep(.02)\n"
    )
    program = (
        "import os,pathlib,subprocess,sys,time\n"
        f"root=pathlib.Path({str(root)!r})\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r},str(root/'heartbeat')],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "(root/'child-pid').write_text(str(child.pid))\n"
        "os.close(1); os.close(2)\n"
        "deadline=time.monotonic()+8\n"
        "while not (root/'release').exists() and time.monotonic()<deadline: time.sleep(.02)\n"
    )
    files["verifiers/hidden.py"] = program.encode()
    return store.put(
        ObjectKind.CASE, case, files=files, references=(case.persona_id, case.repository_id)
    )


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
def test_assessor_persists_descendants_and_recovers_after_parent_disappears(
    store, graded_benchmark, tmp_path, monkeypatch, interrupt
):
    case_id = _writer_case(store, graded_benchmark, tmp_path)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"cases": (case_id,)})),
        options=RunOptions(mode="offline-fixture"),
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dryheave",
            "--store",
            str(store.root),
            "--json",
            "assess",
            summary.run_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    owned = ()
    try:
        pid_file = tmp_path / "child-pid"
        _wait_until(pid_file.exists)
        child_pid = int(pid_file.read_text())
        _wait_until(
            lambda: (
                child_pid
                in {
                    identity.pid for identity in run_status(store, summary.run_id).attempts[0].owned
                }
            )
        )
        owned = run_status(store, summary.run_id).attempts[0].owned
        child = next(identity for identity in owned if identity.pid == child_pid)
        parent = next(identity for identity in owned if identity.pid == child.parent_pid)
        assert _alive(child)
        process.send_signal(interrupt)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == (-signal.SIGKILL if interrupt == signal.SIGKILL else 130), (
            stdout,
            stderr,
        )
        if interrupt == signal.SIGKILL:
            (tmp_path / "release").touch()
            _wait_until(lambda: not _alive(parent))
            assert _alive(child)
        else:
            assert json.loads(stderr)["error"]["code"] == "cancelled"
            assert f"assess {summary.run_id}" in json.loads(stderr)["error"]["message"]
            assert stdout == b""
            assert not _alive(child)
        monkeypatch.setattr(
            "dryheave.assessments.grade_criterion",
            lambda *_args: pytest.fail("Interrupted verifier calls must not be repeated."),
        )
        result = load_assessment(store, assess_run(store, summary.run_id)[0])
        assert result.criteria[0].error == "interrupted_verifier"
        assert result.completion == "indeterminate"
        assert not _alive(child)
        assert (
            run_status(store, summary.run_id).attempts[0].capture_id
            == summary.attempts[0].capture_id
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)
        for identity in owned:
            if _alive(identity):
                with suppress(psutil.NoSuchProcess):
                    psutil.Process(identity.pid).kill()


@pytest.mark.parametrize("finished", [False, True])
def test_resume_refuses_subject_launch_when_grading_cleanup_is_unresolved(
    store, graded_benchmark, monkeypatch, finished
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    if finished:
        assess_run(store, summary.run_id)
    else:
        with RunStore(store.root).open(summary.run_id) as journal:
            execution = ExecutionJournal(journal)
            state = execution.attempts[summary.attempts[0].attempt_id]
            for stage in (TrialStage.AUDITED, TrialStage.GRADING):
                state = state.model_copy(update={"stage": stage})
                execution.save(state)

    def unresolved(*_args):
        raise InputError("Assessment recovery could not reconcile all recorded writers.")

    monkeypatch.setattr("dryheave.native_recovery.reconcile_attempt", unresolved)
    monkeypatch.setattr(
        "dryheave.runner.TrialExecution.execute",
        lambda *_args: pytest.fail("Unresolved grader ownership must block another subject."),
    )
    with pytest.raises(InputError, match="recorded writers"):
        run_experiment(store, resume=summary.run_id, retry=(summary.attempts[0].trial_id,))
    assert len(run_status(store, summary.run_id).attempts) == 1


def test_grading_recovery_preserves_reused_process_identity(store):
    from dryheave.assessment_recovery import reconcile_grading
    from dryheave.drivers.models import ProcessIdentity

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        observed = psutil.Process(process.pid)
        stale = ProcessIdentity(
            pid=process.pid, created=observed.create_time() + 1, parent_pid=observed.ppid()
        )
        run_id = RunStore(store.root).create("a" * 64)
        with RunStore(store.root).open(run_id) as journal:
            execution = ExecutionJournal(journal)
            state = execution.reserve("trial")
            state = execution.own(state, stale)
            reconcile_grading(execution, state)
            assert journal.events[-1].data["owned"] == []
            assert journal.events[-1].data["known_writers_stopped"] is True
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=2)
