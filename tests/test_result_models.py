import pytest

from dryheave.result_models import CheckExecution, verifier_verdict

MISSING = "expected_execution_evidence_missing"


def ran(returncode, process="exited", outcome="error", error=MISSING):
    return CheckExecution(
        outcome=outcome, returncode=returncode, process_outcome=process, error=error
    )


@pytest.mark.parametrize(
    "execution, verdict",
    [
        pytest.param(ran(0, outcome="pass", error=None), "met", id="met-marker"),
        pytest.param(ran(1, outcome="fail", error=None), "unmet", id="failure-marker"),
        pytest.param(ran(1), "unmet", id="crash"),
        pytest.param(ran(0), "unmet", id="exit-zero-without-marker"),
        pytest.param(ran(-11, error=None), "unmet", id="killed-by-signal"),
        pytest.param(ran(-9, "timeout"), "unmet", id="own-timeout"),
        pytest.param(ran(-9, "timeout", error=None), "unmet", id="own-timeout-after-marker"),
        pytest.param(ran(-9, "timeout", error="deadline_exhausted"), None, id="attempt-deadline"),
        pytest.param(ran(-9, "output_limit"), "unmet", id="output-limit"),
        pytest.param(ran(0, error="verifier_cleanup_failed"), None, id="cleanup-unconfirmed"),
        pytest.param(ran(1, error="hidden_verifier_changed"), None, id="hidden-inputs-changed"),
        pytest.param(CheckExecution(outcome="error", error="InputError"), None, id="not-launched"),
        pytest.param(CheckExecution(outcome="error", error="OSError"), None, id="restore-failed"),
        pytest.param(CheckExecution(outcome="not_run"), None, id="not-run"),
    ],
)
def test_verifier_verdict_reads_each_execution_state(execution, verdict):
    assert verifier_verdict(execution) == verdict
