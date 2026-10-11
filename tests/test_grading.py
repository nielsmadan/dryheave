import json
import signal
import sys
import threading
import time
from contextlib import suppress
from types import SimpleNamespace

import psutil
import pytest

from dryheave.cases import DeterministicCriterion
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.errors import LimitError
from dryheave.grading import execute_check
from dryheave.models import CommandSpec


def test_subject_owned_executable_is_rejected_before_execution(tmp_path):
    workspace, hidden = tmp_path / "workspace", tmp_path / "hidden"
    workspace.mkdir()
    hidden.mkdir()
    (hidden / "check.py").write_text('print("CHECKS_PASSED")\n')
    canary = workspace / "executed"
    executable = workspace / "wrapper"
    executable.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(canary)!r}).write_text('ran')\nprint('CHECKS_PASSED')\n"
    )
    executable.chmod(0o700)
    criterion = DeterministicCriterion(
        criterion_id="check",
        description="Reject a subject-owned executable before it can forge a result.",
        command=CommandSpec(argv=(str(executable), "{verifier}/check.py")),
        entrypoint="check.py",
        expected_stdout="CHECKS_PASSED",
    )
    context = SimpleNamespace(
        cancelled=threading.Event(),
        original_workspace=tmp_path / "original",
        on_identities=lambda _identities: None,
    )
    evidence = ArtifactWriter(tmp_path / "evidence", 1000000)
    execution = execute_check(criterion, workspace, hidden, evidence, context)
    assert execution.outcome == "error"
    assert json.loads(next(evidence.root.glob("*-error.json")).read_text()) == {
        "type": "InputError",
        "message": "Verifier executable resolves into subject-owned content.",
    }
    assert not canary.exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    "program, failure_marker, outcome, observed",
    [
        ('print("CHECKS_PASSED")', "ASSERTION_FAILED", "pass", True),
        ('print("ASSERTION_FAILED"); raise SystemExit(1)', "ASSERTION_FAILED", "fail", True),
        ('print("CHECKS_PASSED"); raise SystemExit(1)', "ASSERTION_FAILED", "error", False),
        ('print("CHECKS_PASSED"); raise SystemExit(1)', None, "fail", True),
        ("raise SystemExit(1)", "ASSERTION_FAILED", "error", False),
        ("invalid syntax here", "ASSERTION_FAILED", "error", False),
        ("import absent_verifier_dependency", "ASSERTION_FAILED", "error", False),
        ('raise RuntimeError("setup failed")', "ASSERTION_FAILED", "error", False),
        ('print("zero checks")', "ASSERTION_FAILED", "error", False),
        (
            'import os,signal; print("CHECKS_PASSED", flush=True); os.kill(os.getpid(), signal.SIGTERM)',
            "ASSERTION_FAILED",
            "error",
            True,
        ),
    ],
)
def test_exit_codes_require_truthful_execution_evidence(
    tmp_path, program, failure_marker, outcome, observed
):
    workspace = tmp_path / "workspace"
    hidden = tmp_path / "hidden"
    workspace.mkdir()
    hidden.mkdir()
    (hidden / "check.py").write_text(program + "\n")
    criterion = DeterministicCriterion(
        criterion_id="check",
        description="Check the trusted execution evidence contract.",
        command=CommandSpec(argv=(sys.executable, "{verifier}/check.py")),
        entrypoint="check.py",
        expected_stdout="CHECKS_PASSED",
        expected_failure_stdout=failure_marker,
    )
    identities = []
    context = SimpleNamespace(
        cancelled=threading.Event(),
        original_workspace=tmp_path / "original",
        on_identities=identities.extend,
    )
    execution = execute_check(
        criterion, workspace, hidden, ArtifactWriter(tmp_path / "evidence", 1000000), context
    )
    assert (execution.outcome, execution.execution_observed) == (outcome, observed)
    if not observed:
        assert execution.error == "expected_execution_evidence_missing"
    if "SIGTERM" in program:
        assert execution.returncode == -signal.SIGTERM
    assert identities


@pytest.mark.integration
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_one_byte_stream_limit_remains_independent_of_evidence_budget(tmp_path, stream):
    workspace = tmp_path / "workspace"
    hidden = tmp_path / "hidden"
    workspace.mkdir()
    hidden.mkdir()
    (hidden / "check.py").write_text(f"import sys\nsys.{stream}.write('PP')\n")
    criterion = DeterministicCriterion(
        criterion_id="check",
        description="Bound the actual verifier output separately from its retained metadata.",
        command=CommandSpec(argv=(sys.executable, "{verifier}/check.py"), max_output_bytes=1),
        entrypoint="check.py",
        expected_stdout="P",
    )
    context = SimpleNamespace(
        cancelled=threading.Event(), original_workspace=None, on_identities=lambda _identities: None
    )
    evidence = ArtifactWriter(tmp_path / "evidence", 65536)
    execution = execute_check(criterion, workspace, hidden, evidence, context)
    assert execution.outcome == "error"
    assert execution.process_outcome == "output_limit"
    assert (evidence.root / (stream + ".bin")).read_bytes() == b"P"


@pytest.mark.integration
def test_failed_pre_stop_publish_still_stops_the_owned_child(tmp_path):
    workspace, hidden = tmp_path / "workspace", tmp_path / "hidden"
    workspace.mkdir()
    hidden.mkdir()
    marker = tmp_path / "heartbeat"
    child_code = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nend=time.monotonic()+10\nwhile time.monotonic()<end:\n with p.open('a') as f: f.write('beat\\n')\n time.sleep(.02)\n"
    (hidden / "check.py").write_text(
        "import pathlib,subprocess,sys,time\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r},{str(marker)!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"while not pathlib.Path({str(marker)!r}).exists(): time.sleep(.01)\n"
        "time.sleep(.3)\n"
        "print(child.pid, 'CHECKS_PASSED')\n"
    )
    criterion = DeterministicCriterion(
        criterion_id="check",
        description="Stop owned processes even when the pre-stop identity flush fails.",
        command=CommandSpec(argv=(sys.executable, "{verifier}/check.py")),
        entrypoint="check.py",
        expected_stdout="CHECKS_PASSED",
    )
    parent: list[psutil.Process] = []
    failed: list[tuple[int, ...]] = []
    identities = []

    def on_identities(batch):
        if not parent:
            parent.append(psutil.Process(batch[0].pid))
        if not failed and not parent[0].is_running():
            failed.append(tuple(item.pid for item in batch))
            raise LimitError("Journal exceeds the limit.")
        identities.extend(batch)

    context = SimpleNamespace(
        cancelled=threading.Event(), original_workspace=None, on_identities=on_identities
    )
    evidence = ArtifactWriter(tmp_path / "evidence", 1000000)
    execution = execute_check(criterion, workspace, hidden, evidence, context)
    child = int((evidence.root / "stdout.bin").read_text().split()[0])
    try:
        assert child in failed[0]
        assert (execution.outcome, execution.error) == ("error", "verifier_cleanup_failed")
        cleanup = json.loads(next(evidence.root.glob("*-cleanup.json")).read_text())
        assert cleanup["errors"] == ["process_identity_publish_failed"]
        assert cleanup["known_writers_stopped"] is True
        assert child in {item["pid"] for item in cleanup["owned"]}
        assert child in {item.pid for item in identities}
        assert (
            not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        )
        at_close = marker.read_bytes()
        time.sleep(0.08)
        assert marker.read_bytes() == at_close
    finally:
        with suppress(psutil.NoSuchProcess):
            psutil.Process(child).kill()
