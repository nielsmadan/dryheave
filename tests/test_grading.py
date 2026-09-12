import signal
import sys
import threading
from types import SimpleNamespace

import pytest

from dryheave.cases import DeterministicCriterion
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.grading import execute_check
from dryheave.models import CommandSpec


def test_subject_owned_executable_is_rejected_before_execution(tmp_path):
    import json

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
        on_identity=lambda _identity: None,
    )
    evidence = ArtifactWriter(tmp_path / "evidence", 1000000)
    execution = execute_check(criterion, workspace, hidden, evidence, context)
    assert execution.outcome == "error"
    assert json.loads(next(evidence.root.glob("*-error.json")).read_text()) == {
        "type": "InputError",
        "message": "Verifier executable resolves into subject-owned content.",
    }
    assert not canary.exists()


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
        on_identity=identities.append,
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
        cancelled=threading.Event(), original_workspace=None, on_identity=lambda _identity: None
    )
    evidence = ArtifactWriter(tmp_path / "evidence", 65536)
    execution = execute_check(criterion, workspace, hidden, evidence, context)
    assert execution.outcome == "error"
    assert execution.process_outcome == "output_limit"
    assert (evidence.root / (stream + ".bin")).read_bytes() == b"P"
