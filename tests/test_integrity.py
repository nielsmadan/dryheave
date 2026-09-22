from datetime import UTC, datetime

import pytest

from dryheave.assessments import assess_run, review_assessment
from dryheave.drivers.models import NativeEvent
from dryheave.errors import IntegrityError
from dryheave.experiments import create_experiment
from dryheave.fixture_subject import FixtureTerminal
from dryheave.integrity import _access_findings, load_assessment
from dryheave.result_models import AuditReview
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.serialization import digest


def test_owned_workspace_nested_in_source_and_mentions_are_not_reads():
    workspace = "/source/.dryheave/runs/owned/workspace"
    events = (
        NativeEvent(
            kind="tool", data={"name": "Read", "input": {"file_path": workspace + "/tests.py"}}
        ),
        NativeEvent(
            kind="tool",
            data={"name": "exec_command", "arguments": '{"cmd":"echo /source/solution.py"}'},
        ),
        NativeEvent(
            kind="metadata",
            data={"argv": ["--config", 'skills.config=[{path="/source/skills",enabled=false}]']},
        ),
        NativeEvent(
            kind="tool", data={"name": "Read", "input": {"file_path": "/source-sibling/file"}}
        ),
    )
    findings, mentions = _access_findings(events, ("/source",), workspace, "a" * 64)
    assert (findings, mentions) == ((), 1)
    observed = NativeEvent(
        kind="tool", data={"name": "Read", "input": {"file_path": "../../../../solution.py"}}
    )
    findings, mentions = _access_findings((observed,), ("/source",), workspace, "a" * 64)
    assert findings[0].rule_id == "forbidden-read-observed"
    assert findings[0].confidence == "suspected"
    transcript = NativeEvent(
        kind="tool", data={"name": "read_file", "arguments": {"path": "/transcripts/source.jsonl"}}
    )
    assert (
        _access_findings((transcript,), (), workspace, digest(b"/transcripts/source.jsonl"))[0][
            0
        ].confidence
        == "suspected"
    )


@pytest.mark.integration
def test_suspicion_review_retains_original_finding_and_cannot_override_corruption(
    store, graded_benchmark, historical_repo, monkeypatch
):
    enter = FixtureTerminal.enter

    def record_access(terminal):
        enter(terminal)
        if terminal.enters != 1:
            return
        terminal.source.batches[-1].insert(
            0,
            NativeEvent(
                kind="tool",
                session_id="fixture-root",
                data={"name": "Read", "input": {"file_path": str(historical_repo[0] / "greet.py")}},
            ),
        )

    monkeypatch.setattr(FixtureTerminal, "enter", record_access)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    original_id = assess_run(store, summary.run_id)[0]
    original = load_assessment(store, original_id)
    assert original.completion == "pass"
    assert original.eligible is False
    review = AuditReview(
        finding_id=original.audit.findings[0].finding_id,
        decision="dismiss",
        reviewer="curator",
        reason="The recorded path request was examined against the retained evidence.",
        reviewed_at=datetime.now(UTC),
    )
    revised_id = review_assessment(store, summary.run_id, original.attempt_id, review)
    revised = load_assessment(store, revised_id)
    assert revised.eligible
    assert revised.audit.findings == original.audit.findings
    assert revised.previous_id == original_id
    assert revised.reviews == (review,)
    manifest = store.get(original.case_id)
    (
        store.object_path(original.case_id) / "blobs" / manifest.files["verifiers/hidden.py"]
    ).write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        review_assessment(store, summary.run_id, original.attempt_id, review)
