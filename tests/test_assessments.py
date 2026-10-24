import os
from pathlib import Path

import pytest

from conftest import HOST
from dryheave.assessments import assess_run, uncalibrated_verifiers
from dryheave.cases import FactVerifier, TaskFact, load_frozen_case
from dryheave.errors import InputError, IntegrityError
from dryheave.experiments import create_experiment
from dryheave.integrity import load_assessment
from dryheave.journals import RunStore
from dryheave.models import ObjectKind, TrialStage
from dryheave.reports import report_run
from dryheave.runner import load_capture, run_experiment, run_status
from dryheave.runner_models import RunOptions, WorkflowEvent, WorkflowProgress


def with_verifier(fact, **changes):
    return fact.model_copy(update={"verifier": fact.verifier.model_copy(update=changes)})


def as_context(fact):
    return fact.model_copy(update={"kind": "context", "phase": None, "verifier": None})


def phased_case():
    from dryheave.cases import CaseContent, PromptStep

    return CaseContent(
        schema_version=2,
        title="Greeting",
        initial_prompt="Fix the greeting.",
        phases=("fix", "polish"),
        allowed_facts=(
            TaskFact(
                fact_id="comma",
                phase="fix",
                text="Use a comma after Hello.",
                check="greet returns Hello, NAME.",
                curator_authored=True,
            ),
            TaskFact(
                fact_id="tests",
                phase="polish",
                text="Add a test.",
                check="A test covers greet.",
                curator_authored=True,
            ),
        ),
        workflow=(PromptStep(step_id="review", prompt="Review your work.", after_phase="fix"),),
    )


def inspected(identifier, turn, digest="d1", **verdicts):
    from dryheave.judges import FactInspection, FactJudgment
    from dryheave.workflow import InspectionRecord

    result = FactInspection(
        inspection_id=identifier,
        turn=turn,
        snapshot_digest=digest,
        input_digest=digest + identifier,
        status="completed",
        judgments=tuple(
            FactJudgment(fact_id=fact_id, verdict=verdict, observation="seen", evidence="greet.py")
            for fact_id, verdict in verdicts.items()
        ),
    )
    return InspectionRecord(identifier, turn, result)


def journal_of(*records, raises=()):
    from dryheave.assessments import JournalFacts

    return JournalFacts(
        inspections=tuple(item.result for item in records if item.result is not None),
        records=records,
        raises=raises,
    )


def test_workflow_outcome_is_none_without_a_workflow() -> None:
    from dryheave.assessments import workflow_outcome

    progress = WorkflowProgress(
        steps=(),
        position=0,
        events=(WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=1),),
    )
    assert (
        workflow_outcome(phased_case(), None, "stand_in_stop", journal_of(), enabled=True) is None
    )
    assert (
        workflow_outcome(phased_case(), progress, "stand_in_stop", journal_of(), enabled=False)
        is None
    )


def test_workflow_outcome_passes_an_inspection_whose_only_unmet_facts_are_in_a_later_phase():
    from dryheave.assessments import workflow_outcome

    progress = WorkflowProgress(
        steps=("review",),
        position=1,
        events=(
            WorkflowEvent(step_id="i-1", kind="inspect", outcome="passed", turn=1, seconds=1.5),
            WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=1),
        ),
    )
    outcome = workflow_outcome(
        phased_case(),
        progress,
        "workflow_complete",
        journal_of(
            inspected("i-1", 1, comma="met", tests="unmet"),
            inspected("i-2", 3, digest="d2", comma="met", tests="met"),
        ),
        enabled=True,
    )
    assert outcome is not None
    assert (outcome.steps, outcome.completed_steps, outcome.inspections) == (1, 1, 2)
    assert (outcome.first_inspection, outcome.final_inspection) == ("passed", "passed")
    assert (outcome.completed, outcome.inspection_seconds) == (True, 1.5)


def test_workflow_outcome_fails_an_inspection_with_a_blocking_phase_and_counts_raises():
    from dryheave.assessments import workflow_outcome
    from dryheave.judges import RaiseEvent

    raises = (
        RaiseEvent(call_id="s-1", phase="fix", fact_ids=("comma",), cause="fix_request", turn=2),
        RaiseEvent(call_id="s-2", phase="fix", fact_ids=("comma",), cause="fallback", turn=3),
    )
    outcome = workflow_outcome(
        phased_case(),
        WorkflowProgress(steps=("review",), position=0),
        "turn_budget",
        journal_of(
            inspected("i-1", 1, comma="unmet", tests="unmet"),
            inspected("i-2", 2, comma="unmet", tests="unmet"),
            raises=raises,
        ),
        enabled=True,
    )
    assert outcome is not None
    assert (outcome.fix_requests, outcome.fallbacks, outcome.completed) == (1, 1, False)
    assert (outcome.first_inspection, outcome.final_inspection) == ("failed", "failed")


def test_an_inspection_with_an_unchecked_verifier_requirement_is_not_a_first_pass():
    from dryheave.assessments import workflow_outcome
    from dryheave.models import CommandSpec

    case = phased_case()
    comma, tests = case.allowed_facts
    verifier = FactVerifier(
        command=CommandSpec(argv=("python3", "{verifier}/comma.py")),
        entrypoint="comma.py",
        expected_stdout="CHECK_RAN",
    )
    case = case.model_copy(
        update={"allowed_facts": (comma.model_copy(update={"verifier": verifier}), tests)}
    )
    outcome = workflow_outcome(
        case,
        WorkflowProgress(steps=("review",), position=1),
        "workflow_complete",
        journal_of(
            inspected("i-1", 1, comma="unclear", tests="unmet"),
            inspected("i-2", 3, digest="d2", comma="met", tests="met"),
        ),
        enabled=True,
    )
    assert outcome is not None
    assert (outcome.inspections, outcome.first_inspection, outcome.final_inspection) == (
        2,
        "unavailable",
        "passed",
    )


def test_workflow_outcome_reports_an_inspection_intent_without_a_result_as_unknown():
    from dryheave.assessments import workflow_outcome
    from dryheave.workflow import InspectionRecord

    outcome = workflow_outcome(
        phased_case(),
        WorkflowProgress(steps=("review",), position=0),
        "interrupted",
        journal_of(
            inspected("i-1", 1, comma="unmet", tests="unmet"), InspectionRecord("i-2", 2, None)
        ),
        enabled=True,
    )
    assert outcome is not None
    assert (outcome.inspections, outcome.first_inspection, outcome.final_inspection) == (
        1,
        "failed",
        "unknown",
    )


def test_workflow_outcome_without_inspections_has_no_verdicts() -> None:
    from dryheave.assessments import workflow_outcome

    progress = WorkflowProgress(
        steps=(),
        position=0,
        events=(WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=1),),
    )
    outcome = workflow_outcome(phased_case(), progress, "stand_in_stop", journal_of(), enabled=True)
    assert outcome is not None
    assert (outcome.inspections, outcome.dialogs_answered) == (0, 1)
    assert (outcome.first_inspection, outcome.final_inspection) == ("none", "none")


@pytest.mark.integration
def test_real_hidden_check_calibrates_and_resume_never_relaunches(
    store, graded_benchmark, monkeypatch
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    original = load_capture(store, summary.pending_assessment[0])
    identifiers = assess_run(store, summary.run_id)
    result = load_assessment(store, identifiers[0])
    assert (result.audit.input_integrity, result.outcome, result.eligible) == (
        "verified",
        "pass",
        True,
    )
    check = result.criteria[0]
    assert check.execution.execution_observed
    assert check.calibration.status == "demonstrated"
    assert (check.calibration.baseline.outcome, check.calibration.reference.outcome) == (
        "fail",
        "pass",
    )
    assert result.roles[0].known_cost == 0
    assert load_capture(store, summary.pending_assessment[0]) == original
    assert run_status(store, summary.run_id).attempts[0].stage == TrialStage.FINISHED
    monkeypatch.setattr(
        "dryheave.assessments.grade_fact",
        lambda *_args: pytest.fail("Finished checks must not rerun."),
    )
    assert assess_run(store, summary.run_id) == identifiers
    assert run_experiment(store, resume=summary.run_id, host=HOST).pending_assessment == ()


@pytest.mark.integration
def test_verifier_blob_mutation_retains_invalid_report_without_loading_corrupt_inputs(
    store, graded_benchmark, monkeypatch
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    case_id = store.resolve(graded_benchmark.cases[0])
    manifest = store.get(case_id)
    verifier_hash = manifest.files["verifiers/hidden.py"]
    (store.object_path(case_id) / "blobs" / verifier_hash).write_bytes(
        b'print("PRIVATE_VERIFIER_SENTINEL")\n'
    )
    with pytest.raises(IntegrityError, match="hash mismatch"):
        load_capture(store, summary.pending_assessment[0])
    monkeypatch.setattr(
        "dryheave.assessments.grade_fact",
        lambda *_args: pytest.fail("Tampered inputs cannot be graded."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.audit.input_integrity == "invalid"
    assert result.outcome == "indeterminate"
    assert result.roles[0].known_cost == 0
    assert result.capture_id == summary.pending_assessment[0]
    report = report_run(store, summary.run_id)
    assert report.input_error
    assert report.groups[0].eligible == 0
    assert report.groups[0].audit_excluded == 1
    assert run_status(store, summary.run_id).input_error
    assert assess_run(store, summary.run_id) == (report.attempts[0].assessment_id,)


@pytest.mark.integration
def test_mutation_before_capture_publication_quarantines_and_blocks_next_subject(
    store, graded_benchmark, monkeypatch
):
    from dryheave.fixture_subject import FixtureTerminal

    case_id = store.resolve(graded_benchmark.cases[0])
    blob = store.object_path(case_id) / "blobs" / store.get(case_id).files["verifiers/hidden.py"]
    close = FixtureTerminal.close

    def tamper(terminal):
        result = close(terminal)
        blob.write_text('print("PRIVATE_VERIFIER_SENTINEL")\n')
        return result

    monkeypatch.setattr(FixtureTerminal, "close", tamper)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"repetitions": 2})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    assert len(summary.attempts) == len(summary.unstarted_trials) == 1
    state = summary.attempts[0]
    assert state.capture_id is None
    assert state.quarantine_id and state.capture_error
    assert store.get(state.quarantine_id).kind == ObjectKind.QUARANTINE
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert (result.audit.input_integrity, result.eligible, result.quarantine_id) == (
        "invalid",
        False,
        state.quarantine_id,
    )
    with pytest.raises(IntegrityError):
        run_experiment(store, resume=summary.run_id, host=HOST)


@pytest.mark.integration
@pytest.mark.parametrize(
    "program",
    [
        'print("not the marker")\n',
        "raise SystemExit(1)\n",
    ],
)
def test_a_verifier_exiting_without_a_marker_is_unmet_but_uncalibrated(
    store, graded_benchmark, program
):
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    blobs = store.read_blobs(case_id)
    blobs["verifiers/hidden.py"] = program.encode()
    changed = store.put(
        ObjectKind.CASE, case, files=blobs, references=(case.persona_id, case.repository_id)
    )
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"cases": (changed,)})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].outcome == "error"
    assert result.criteria[0].calibration.status == "unavailable"
    assert verdicts(result) == {"punctuation": ("unmet", "verifier")}
    assert (result.outcome, result.outcome_reason) == ("fail", "unmet")
    assert (result.eligible, result.exclusion_reasons) == (False, ("verifier_uncalibrated",))


def test_uncalibrated_verifiers_name_crashed_required_verifiers_whose_reference_did_not_pass():
    from dryheave.result_models import Calibration, CheckExecution, CriterionResult

    passed = CheckExecution(outcome="pass")
    errored = CheckExecution(outcome="error", error="expected_execution_evidence_missing")

    def criterion(name, reference, *, required=True, kind="deterministic", outcome="error"):
        return CriterionResult(
            criterion_id=name,
            kind=kind,
            required=required,
            outcome=outcome,
            calibration=Calibration(reference=reference),
        )

    criteria = (
        criterion("calibrated", passed),
        criterion("broken", errored),
        criterion("no-reference", None),
        criterion("optional", errored, required=False),
        criterion("rubric", errored, kind="judge", required=False),
        criterion("audit-excluded", errored, outcome="not_run"),
        criterion("marker-fail-without-reference", None, outcome="fail"),
    )
    assert uncalibrated_verifiers(criteria) == ("broken", "no-reference")


@pytest.mark.integration
def test_known_future_git_object_is_retained_but_excluded(
    store, graded_benchmark, historical_repo, monkeypatch
):
    from dryheave.fixture_subject import FixtureTerminal
    from dryheave.repositories import Git, SnapshotLimits

    source, _, future, _ = historical_repo
    close = FixtureTerminal.close

    def import_future(terminal):
        packed = (
            Git(source, SnapshotLimits())
            .run("pack-objects", "--stdout", "--revs", input_bytes=(future + "\n").encode())
            .stdout
        )
        Git(Path(terminal.plan.cwd), SnapshotLimits()).run(
            "index-pack", "--stdin", input_bytes=packed
        )
        return close(terminal)

    monkeypatch.setattr(FixtureTerminal, "close", import_future)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.outcome == "pass"
    assert result.audit.findings[0].rule_id == "forbidden-git-object"
    assert result.audit.findings[0].confidence == "confirmed"
    assert result.eligible is False


@pytest.mark.integration
def test_relative_verifier_executable_requires_an_explicit_trusted_path(store, graded_benchmark):
    import json

    from dryheave.models import CommandSpec

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    fact = with_verifier(
        case.allowed_facts[0], command=CommandSpec(argv=("./wrapper", "{verifier}/hidden.py"))
    )
    case = case.model_copy(update={"allowed_facts": (fact,)})
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"cases": (changed,)})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].outcome == "error"
    assert result.facts == ()
    assert (result.outcome, result.outcome_reason) == ("indeterminate", "grading_error")
    errors = [
        json.loads(content)
        for name, content in store.read_blobs(result.evidence_id).items()
        if name.endswith("-error.json")
    ]
    assert errors
    assert all(
        error
        == {
            "type": "InputError",
            "message": "Verifier executable must be an absolute trusted path or a system executable name.",
        }
        for error in errors
    )


@pytest.mark.integration
def test_interrupted_grading_resume_retains_error_without_reexecuting_command(
    store, graded_benchmark, monkeypatch
):
    from dryheave.grading import grade_fact

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )

    def interrupt(context, fact):
        grade_fact(context, fact)
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.assessments.grade_fact", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assess_run(store, summary.run_id)
    assert run_status(store, summary.run_id).attempts[0].stage == TrialStage.GRADING
    monkeypatch.setattr(
        "dryheave.assessments.grade_fact",
        lambda *_args: pytest.fail("Ambiguous verifier execution must not repeat."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].error == "interrupted_verifier"
    assert result.outcome == "indeterminate"
    assert result.evidence_id


@pytest.mark.integration
@pytest.mark.parametrize("layout", ["independent", "shared", "colliding-names"])
def test_independent_checks_and_explicit_ordered_suites(store, graded_benchmark, layout):
    import sys

    from dryheave.models import CommandSpec

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    files = store.read_blobs(case_id)
    files["verifiers/first.py"] = (
        b'from pathlib import Path\nPath("marker").write_text("created")\nprint("CHECK_RAN")\n'
    )
    shared = layout == "shared"
    files["verifiers/second.py"] = (
        'from pathlib import Path\nassert Path("marker").exists() == '
        + repr(shared)
        + '\nprint("CHECK_RAN")\n'
    ).encode()
    suite = "ordered" if shared else None
    facts = tuple(
        TaskFact(
            fact_id="suite-shared" if layout == "colliding-names" and name == "first" else name,
            phase="fix",
            text="Confirm independent or explicitly shared workspace state.",
            curator_authored=True,
            verifier=FactVerifier(
                command=CommandSpec(argv=(sys.executable, "{verifier}/" + name + ".py")),
                entrypoint=name + ".py",
                expected_stdout="CHECK_RAN",
                suite_id="shared" if layout == "colliding-names" and name == "second" else suite,
            ),
        )
        for name in ("first", "second")
    )
    case = case.model_copy(
        update={
            "allowed_facts": (as_context(case.allowed_facts[0]), *facts),
            "hidden_files": {
                **case.hidden_files,
                "first.py": "verifiers/first.py",
                "second.py": "verifiers/second.py",
            },
        }
    )
    changed = store.put(
        ObjectKind.CASE, case, files=files, references=(case.persona_id, case.repository_id)
    )
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"cases": (changed,)})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [item.outcome for item in result.criteria] == ["pass", "pass"]
    assert result.eligible


@pytest.mark.integration
@pytest.mark.parametrize("saved_first", [False, True])
def test_interrupted_shared_suite_keeps_remaining_members_indeterminate(
    store, graded_benchmark, monkeypatch, saved_first
):
    from dryheave.journals import RunStore
    from dryheave.result_models import CriterionResult
    from dryheave.runner_state import ExecutionJournal

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    facts = tuple(
        with_verifier(case.allowed_facts[0], suite_id="ordered").model_copy(
            update={"fact_id": name}
        )
        for name in ("first", "second")
    )
    case = case.model_copy(update={"allowed_facts": (as_context(case.allowed_facts[0]), *facts)})
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"cases": (changed,)})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    with RunStore(store.root).open(summary.run_id) as journal:
        execution = ExecutionJournal(journal)
        state = execution.attempts[summary.attempts[0].attempt_id]
        for stage in (TrialStage.AUDITED, TrialStage.GRADING):
            state = state.model_copy(update={"stage": stage})
            execution.save(state)
        journal.append(
            "criterion-intent",
            {"criterion_id": "first"},
            trial_id=state.trial_id,
            attempt_id=state.attempt_id,
        )
        if saved_first:
            execution.evidence(
                state,
                "criterion-result",
                CriterionResult(
                    criterion_id="first", kind="deterministic", required=True, outcome="pass"
                ),
            )
    monkeypatch.setattr(
        "dryheave.assessments.grade_fact",
        lambda *_args: pytest.fail("Interrupted suites cannot continue in fresh workspaces."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].outcome == ("pass" if saved_first else "error")
    assert result.criteria[1].error == "interrupted_verifier"
    assert result.outcome == "indeterminate"


@pytest.mark.integration
def test_unjudged_assessment_keeps_legacy_shape_without_default_rubrics(store, assessed):
    from dryheave.result_models import Assessment
    from dryheave.serialization import canonical_json, parse_model

    identifier = run_status(store, assessed.run_id).attempts[0].assessment_id
    payload = store.get(identifier).payload
    result = parse_model(canonical_json(payload), Assessment)
    assert [item.criterion_id for item in result.criteria] == ["punctuation"]
    assert [(item.fact_id, item.verdict, item.source) for item in result.facts] == [
        ("punctuation", "met", "verifier")
    ]
    assert (result.metrics, result.rubric_set, result.uncovered) == ((), None, ())
    assert not {"metrics", "rubric_set", "uncovered"} & set(payload)
    assert canonical_json(result) == canonical_json(payload)


MEASURE = b"""import json, os
from pathlib import Path
print("running 2 measurements; free-form log line", flush=True)
baseline = Path(os.environ["DRYHEAVE_BASELINE_DIR"], "greet.py").read_text()
final = Path("greet.py").read_text()
try:
    Path(os.environ["DRYHEAVE_BASELINE_DIR"], "probe").write_text("x")
    writable = True
except PermissionError:
    writable = False
Path(os.environ["DRYHEAVE_METRICS_OUTPUT"]).write_text(json.dumps({"metrics": [
    {"metric_id": "changed_lines", "label": "Changed lines", "unit": "lines", "better": "lower",
     "value": sum(a != b for a, b in zip(baseline.splitlines(), final.splitlines()))},
    {"metric_id": "baseline_unfixed", "label": "Baseline lacks the comma",
     "value": "Hello, " not in baseline},
    {"metric_id": "baseline_writable", "label": "Baseline writable", "value": writable},
]}))
"""


def measured_benchmark(store, graded_benchmark, program: bytes):
    import sys

    from dryheave.cases import MetricsCommand
    from dryheave.models import CommandSpec

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    files = store.read_blobs(case_id) | {"verifiers/measure.py": program}
    command = MetricsCommand(
        metrics_id="greeting-metrics",
        description="Measure how the final greeting differs from the baseline.",
        command=CommandSpec(argv=(sys.executable, "{verifier}/measure.py")),
        entrypoint="measure.py",
    )
    case = case.model_copy(
        update={
            "metrics": (command,),
            "hidden_files": {**case.hidden_files, "measure.py": "verifiers/measure.py"},
        }
    )
    changed = store.put(
        ObjectKind.CASE, case, files=files, references=(case.persona_id, case.repository_id)
    )
    return graded_benchmark.model_copy(update={"cases": (changed,)})


@pytest.mark.integration
def test_case_metrics_command_measures_final_against_baseline(store, graded_benchmark):
    from dryheave.serialization import digest

    summary = run_experiment(
        store,
        create_experiment(store, measured_benchmark(store, graded_benchmark, MEASURE)),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    (metrics,) = result.metrics
    assert (metrics.metrics_id, metrics.outcome, metrics.error) == (
        "greeting-metrics",
        "completed",
        None,
    )
    assert [(item.metric_id, item.value, item.unit) for item in metrics.values] == [
        ("changed_lines", 1, "lines"),
        ("baseline_unfixed", True, None),
        ("baseline_writable", False, None),
    ]
    assert (result.outcome, result.eligible) == ("pass", True)
    evidence = store.read_blobs(result.evidence_id)
    stdout = next(
        content
        for name, content in evidence.items()
        if name.endswith("greeting-metrics-evidence/stdout.bin")
    )
    assert stdout == b"running 2 measurements; free-form log line\n"
    assert metrics.execution.stdout_sha256 == digest(stdout)


@pytest.mark.integration
def test_failing_metrics_command_is_an_error_that_leaves_completion_alone(store, graded_benchmark):
    summary = run_experiment(
        store,
        create_experiment(
            store,
            measured_benchmark(store, graded_benchmark, b'print("boom")\nraise SystemExit(4)\n'),
        ),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [(item.outcome, item.error) for item in result.metrics] == [
        ("error", "metrics command exited with status 4")
    ]
    assert (result.outcome, result.eligible, result.exclusion_reasons) == ("pass", True, ())


@pytest.mark.integration
@pytest.mark.parametrize(
    "program", [MEASURE, b'print("boom")\nraise SystemExit(4)\n'], ids=["completed", "error"]
)
def test_metrics_baseline_is_writable_again_after_the_run(
    store, graded_benchmark, monkeypatch, program
):
    from dryheave.case_metrics import run_metrics as real_run_metrics

    captured = {}

    def wrapped(context, command, inherited):
        result = real_run_metrics(context, command, inherited)
        captured["baseline"] = (
            context.root / "metrics" / command.metrics_id / "baseline" / "workspace"
        )
        return result

    monkeypatch.setattr("dryheave.assessments.run_metrics", wrapped)
    summary = run_experiment(
        store,
        create_experiment(store, measured_benchmark(store, graded_benchmark, program)),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    assess_run(store, summary.run_id)
    baseline = captured["baseline"]
    assert os.access(baseline, os.W_OK)
    for child in baseline.iterdir():
        assert os.access(child, os.W_OK)
    assert os.access(baseline.parent, os.W_OK)


@pytest.mark.integration
def test_metrics_restore_leaves_a_swapped_in_outside_directory_untouched(
    store, graded_benchmark, monkeypatch, tmp_path
):
    from dryheave.case_metrics import run_metrics as real_run_metrics

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workspace").mkdir()
    for path in (outside / "workspace", outside):
        path.chmod(0o555)
    program = f"""import os
from pathlib import Path
swapped = Path(os.environ["DRYHEAVE_BASELINE_DIR"]).parent
swapped.rename(swapped.with_name(swapped.name + "-moved"))
swapped.symlink_to({str(outside)!r}, target_is_directory=True)
raise SystemExit(1)
""".encode()
    captured = []

    def wrapped(context, command, inherited):
        result = real_run_metrics(context, command, inherited)
        captured.append(result)
        return result

    monkeypatch.setattr("dryheave.assessments.run_metrics", wrapped)
    summary = run_experiment(
        store,
        create_experiment(store, measured_benchmark(store, graded_benchmark, program)),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    try:
        assess_run(store, summary.run_id)
        assert [(item.outcome, item.error) for item in captured] == [
            ("error", "metrics_baseline_changed")
        ]
        assert [(outside / "workspace").stat().st_mode & 0o777, outside.stat().st_mode & 0o777] == [
            0o555,
            0o555,
        ]
    finally:
        for path in (outside, outside / "workspace"):
            path.chmod(0o755)


@pytest.mark.integration
def test_interrupted_metrics_command_becomes_an_error_on_resume_without_rerunning(
    store, graded_benchmark, monkeypatch
):
    from dryheave.case_metrics import run_metrics

    summary = run_experiment(
        store,
        create_experiment(store, measured_benchmark(store, graded_benchmark, MEASURE)),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )

    def interrupt(context, command, inherited):
        run_metrics(context, command, inherited)
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.assessments.run_metrics", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assess_run(store, summary.run_id)
    monkeypatch.setattr(
        "dryheave.assessments.run_metrics",
        lambda *_args: pytest.fail("An interrupted metrics command must not rerun."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [(item.outcome, item.error) for item in result.metrics] == [
        ("error", "interrupted_metrics")
    ]
    assert result.outcome == "pass"
    assert assess_run(store, summary.run_id) == (
        run_status(store, summary.run_id).attempts[0].assessment_id,
    )


@pytest.mark.integration
def test_fact_account_credits_a_cited_on_request_requirement_as_elicited(store, graded_benchmark):
    from dryheave.cases import TaskFact

    case_id = store.resolve(graded_benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    extra_fact = TaskFact(
        fact_id="greeting-style",
        kind="context",
        text="Keep the greeting informal.",
        disclosure="proactive",
        curator_authored=True,
    )
    changed_case = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"allowed_facts": (*case.allowed_facts, extra_fact)}),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    script = graded_benchmark.stand_in.script
    draft = graded_benchmark.model_copy(
        update={
            "cases": (changed_case,),
            "stand_in": graded_benchmark.stand_in.model_copy(
                update={
                    "script": (
                        script[0].model_copy(
                            update={
                                "decision": script[0].decision.model_copy(
                                    update={"fact_ids": ("punctuation", "greeting-style")}
                                )
                            }
                        ),
                        script[1],
                    )
                }
            ),
        }
    )
    summary = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    account = result.fact_account
    assert account is not None
    assert (account.stand_in_replies, account.fix_rounds, account.independence) == (1, 0, 0.75)
    assert [(item.fact_id, item.credit, item.elicited) for item in account.facts] == [
        ("punctuation", "elicited", True)
    ]
    assert (result.outcome, result.outcome_reason) == ("pass", "all_met")


@pytest.mark.integration
def test_a_run_without_a_workflow_that_answered_a_dialog_has_no_workflow_outcome(
    store, graded_benchmark, monkeypatch
):
    dialog = WorkflowProgress(
        steps=(),
        position=0,
        events=(WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=1),),
    )
    monkeypatch.setattr("dryheave.runner_attempt.workflow_progress", lambda *_a, **_k: dialog)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    assert load_capture(store, summary.pending_assessment[0]).workflow == dialog
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    case = load_frozen_case(store, store.resolve(graded_benchmark.cases[0]))
    assert result.workflow is None
    assert result.fact_account is not None
    assert result.fact_account.reached_phases == case.phases


@pytest.mark.integration
def test_elicited_facts_follow_the_captures_own_case_id_not_a_substitute_case(
    store, graded_benchmark
):
    from dryheave.assessment_inputs import AssessmentInputs
    from dryheave.assessments import _capture_case, stand_in_exchanges
    from dryheave.fact_metrics import elicited_facts

    def elicited(capture):
        case = _capture_case(store, AssessmentInputs(capture=capture))
        return elicited_facts(case, stand_in_exchanges(capture, ()))

    case_id = store.resolve(graded_benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    proactive_variant = store.put(
        ObjectKind.CASE,
        case.model_copy(
            update={
                "allowed_facts": tuple(
                    fact.model_copy(update={"disclosure": "proactive"})
                    for fact in case.allowed_facts
                )
            }
        ),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    assert capture.case_id == case_id
    assert elicited(capture) == {"punctuation"}
    assert elicited(capture.model_copy(update={"case_id": proactive_variant})) == frozenset()


@pytest.mark.integration
def test_stand_in_exchanges_mark_fix_its_raised_and_count_dialog_answers(store, graded_benchmark):
    from dryheave.assessments import stand_in_exchanges
    from dryheave.controller_models import RoleCall, StandInDecision
    from dryheave.fact_metrics import Exchange
    from dryheave.judges import RaiseEvent
    from dryheave.runner_models import WorkflowEvent, WorkflowProgress

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    baseline = stand_in_exchanges(capture, ())

    ordinary = RoleCall(
        call_id="ordinary-reply",
        role="stand_in",
        status="completed",
        decision=StandInDecision(
            action="reply", text="Use mutual TLS", fact_ids=("punctuation",), reason="asked"
        ),
    )
    fix_it = RoleCall(
        call_id="fix-it-reply",
        role="stand_in",
        status="completed",
        decision=StandInDecision(action="reply", text="Still broken", reason="fix"),
    )
    rejected = RoleCall(
        call_id="rejected-reply",
        role="stand_in",
        status="failed",
        error="invalid_dialog_answer",
        decision=StandInDecision(
            action="reply", text="Maybe", fact_ids=("punctuation",), reason="x"
        ),
    )
    dialog_answer = RoleCall(
        call_id="dialog-reply",
        role="stand_in",
        status="completed",
        decision=StandInDecision(
            action="reply", choices=("Option A",), fact_ids=("punctuation",), reason="answered"
        ),
    )
    workflow = WorkflowProgress(
        steps=("check",),
        position=0,
        events=(
            WorkflowEvent(
                step_id="check",
                kind="inspect",
                outcome="fix_requested",
                turn=1,
                call_ids=("fix-it-reply",),
            ),
            WorkflowEvent(
                step_id="dialog",
                kind="dialog",
                outcome="answered",
                turn=1,
                call_ids=("dialog-reply",),
            ),
        ),
    )
    modified = capture.model_copy(
        update={
            "controller_calls": (
                *capture.controller_calls,
                ordinary,
                fix_it,
                rejected,
                dialog_answer,
            ),
            "workflow": workflow,
        }
    )
    raised = RaiseEvent(
        call_id="fix-it-reply",
        phase="fix",
        fact_ids=("punctuation",),
        cause="fix_request",
        turn=1,
    )
    assert stand_in_exchanges(modified, (raised,)) == (
        *baseline,
        Exchange(raised=False, fact_ids=("punctuation",)),
        Exchange(raised=True, fact_ids=("punctuation",)),
        Exchange(raised=False, fact_ids=("punctuation",)),
    )


@pytest.mark.integration
def test_stand_in_exchanges_exclude_screen_steps_of_an_unanswered_screen(store, graded_benchmark):
    from dryheave.assessments import stand_in_exchanges
    from dryheave.controller_models import RoleCall, StandInDecision
    from dryheave.drivers.models import ScreenAction

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    baseline = stand_in_exchanges(capture, ())
    screen_step = RoleCall(
        call_id="screen-step",
        role="stand_in",
        status="completed",
        decision=StandInDecision(
            action="reply",
            reason="Moving to the option.",
            screen_kind="question",
            screen_action=ScreenAction(key="Down"),
        ),
    )
    modified = capture.model_copy(
        update={"controller_calls": (*capture.controller_calls, screen_step)}
    )
    assert (len(stand_in_exchanges(modified, ())), len(baseline)) == (1, 1)


def test_capture_case_reuses_the_trial_case_and_loads_only_a_different_one(store, monkeypatch):
    from dryheave import assessments
    from dryheave.assessment_inputs import AssessmentInputs
    from dryheave.experiment_models import TrialSpec
    from dryheave.runner_models import CapturedAttempt

    loaded = []
    other = object()
    monkeypatch.setattr(
        assessments, "load_frozen_case", lambda store, case_id: loaded.append(case_id) or other
    )
    trial_case = object()
    capture = CapturedAttempt.model_construct(case_id="c" * 64)
    inputs = AssessmentInputs(
        capture=capture, case=trial_case, trial=TrialSpec.model_construct(case_id="c" * 64)
    )
    assert (assessments._capture_case(store, inputs), loaded) == (trial_case, [])
    inputs.trial = TrialSpec.model_construct(case_id="d" * 64)
    assert (assessments._capture_case(store, inputs), loaded) == (other, ["c" * 64])


def test_assessment_payload_without_new_fields_loads_defaulted_and_schema_one_is_refused(
    store,
):
    from dryheave.models import Manifest

    legacy_payload = {
        "run_id": "a" * 32,
        "experiment_id": "a" * 64,
        "trial_id": "trial-1",
        "attempt_id": "attempt-1",
        "capture_id": None,
        "case_id": "a" * 64,
        "profile_id": None,
        "stand_in_id": None,
        "scoring_id": None,
        "criteria_id": None,
        "variant": "base",
        "mode": "offline-fixture",
        "created_at": "2026-01-01T00:00:00+00:00",
        "phase": "finished",
        "audit": {
            "input_integrity": "verified",
            "capture_integrity": "verified",
            "cleanup": "stopped",
            "capture_complete": True,
            "coverage": [],
        },
        "exclusion_reasons": [],
        "stop_reason": "completed",
        "subject_observation": "completed",
        "roles": [
            {
                "role": role,
                "records": [],
                "known_tokens": {"provenance": "fixture"},
                "coverage": "unknown",
                "reasons": ["fixture"],
            }
            for role in ("subject", "stand_in", "judge")
        ],
    }
    manifest = Manifest(kind=ObjectKind.RESULT, payload=legacy_payload)
    identifier = store.put_manifest(manifest, files={})
    result = load_assessment(store, identifier)
    assert (
        result.schema_version,
        result.facts,
        result.uncovered,
        result.metrics,
        result.rubric_set,
        result.fact_account,
        result.outcome,
        result.outcome_reason,
    ) == (2, (), (), (), None, None, "indeterminate", None)
    old = store.put_manifest(
        Manifest(kind=ObjectKind.RESULT, payload={**legacy_payload, "schema_version": 1}),
        files={},
    )
    with pytest.raises(InputError, match="schema_version"):
        load_assessment(store, old)


def test_assessment_serializer_drops_empty_optional_assessment_fields() -> None:
    import json
    from datetime import UTC, datetime

    from dryheave.models import TokenUsage
    from dryheave.result_models import Assessment, AuditReport, RoleMetrics
    from dryheave.serialization import canonical_json

    audit = AuditReport(
        input_integrity="verified",
        capture_integrity="verified",
        cleanup="stopped",
        capture_complete=True,
        coverage=(),
    )
    role = RoleMetrics(
        role="subject",
        records=(),
        known_tokens=TokenUsage(provenance="fixture"),
        coverage="unknown",
        reasons=("fixture",),
    )
    assessment = Assessment(
        run_id="a" * 32,
        experiment_id="a" * 64,
        trial_id="trial-1",
        attempt_id="attempt-1",
        capture_id=None,
        case_id=None,
        profile_id=None,
        stand_in_id=None,
        scoring_id=None,
        criteria_id=None,
        variant="base",
        mode="offline-fixture",
        created_at=datetime.now(UTC),
        phase="finished",
        audit=audit,
        exclusion_reasons=(),
        stop_reason="completed",
        subject_observation="completed",
        roles=(
            role,
            role.model_copy(update={"role": "stand_in"}),
            role.model_copy(update={"role": "judge"}),
        ),
        metrics=(),
        uncovered=(),
        rubric_set=None,
        fact_account=None,
        workflow=None,
        graded_case_id=None,
        regrade_id=None,
    )
    parsed = json.loads(canonical_json(assessment))
    assert not {
        "metrics",
        "uncovered",
        "outcome_reason",
        "rubric_set",
        "fact_account",
        "workflow",
        "graded_case_id",
        "regrade_id",
    } & set(parsed)


def judged_regrade_benchmark(store, graded_benchmark, root: Path):
    import sys

    from dryheave.cases import RubricCriterion
    from dryheave.controller_models import ControllerRecipe
    from dryheave.experiment_models import ScoringConfig
    from dryheave.models import CommandSpec

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                *case.criteria,
                RubricCriterion(
                    criterion_id="quality", rubric="The greeting keeps its public interface."
                ),
            ),
            "disabled_rubrics": ("code-quality", "codebase-consistency", "test-quality"),
        }
    )
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    script = root / "judge.py"
    script.write_text(
        "import pathlib,sys\n"
        f"root = pathlib.Path({str(root)!r})\n"
        "sys.stdin.buffer.read()\n"
        'calls = root / "calls"\n'
        'calls.write_text(str(int(calls.read_text()) + 1) if calls.exists() else "1")\n'
        'print((root / "response.json").read_text())\n'
    )
    judge_answers(root, "pass")
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )
    return graded_benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})


def judge_answers(root: Path, outcome: str) -> None:
    import json

    (root / "response.json").write_text(
        json.dumps(
            {
                "judgments": [
                    {"criterion_id": "quality", "outcome": outcome, "rationale": "Judged."}
                ],
            }
        )
    )


def judge_calls(root: Path) -> int:
    return int((root / "calls").read_text())


def judge_call_dirs(store, assessment) -> set[str]:
    return {
        name.split("/judge/")[1].split("/")[0]
        for name in store.read_blobs(assessment.evidence_id)
        if "/judge/" in name
    }


@pytest.mark.integration
def test_regrade_judges_finished_attempts_again_as_new_chained_assessments(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.assessments import regrade_run

    benchmark = judged_regrade_benchmark(store, graded_benchmark, tmp_path)
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    (original_id,) = assess_run(store, summary.run_id)
    original = load_assessment(store, original_id)
    assert [(item.criterion_id, item.outcome) for item in original.criteria] == [
        ("punctuation", "pass"),
        ("quality", "pass"),
    ]
    judge_answers(tmp_path, "fail")

    regrade_id, (regraded_id,) = regrade_run(store, summary.run_id)

    assert judge_calls(tmp_path) == 2
    regraded = load_assessment(store, regraded_id)
    assert regraded_id != original_id
    assert regraded.previous_id == original_id
    assert [(item.criterion_id, item.outcome) for item in regraded.criteria] == [
        ("punctuation", "pass"),
        ("quality", "fail"),
    ]
    assert (regraded.case_id, regraded.graded_case_id) == (original.case_id, None)
    assert (regraded.regrade_id, original.regrade_id) == (regrade_id, None)
    assert regraded.criteria_id == original.criteria_id
    assert len(regraded.roles[2].records) == 1
    assert len(judge_call_dirs(store, regraded)) == 1
    assert judge_call_dirs(store, regraded).isdisjoint(judge_call_dirs(store, original))
    assert "graded_case_id" not in store.get(regraded_id).payload
    state = run_status(store, summary.run_id).attempts[0]
    assert (state.stage, state.assessment_id) == (TrialStage.FINISHED, regraded_id)
    assert report_run(store, summary.run_id).attempts[0].result == regraded
    events = RunStore(store.root).inspect(summary.run_id).events
    assert [event.data.get("regrade_id") for event in events if event.event == "judge-intent"] == [
        None,
        regrade_id,
    ]

    monkeypatch.setattr(
        "dryheave.assessments.invoke_judge",
        lambda *_args: pytest.fail("A finished regrade must not be judged again."),
    )
    assert assess_run(store, summary.run_id) == (regraded_id,)


@pytest.mark.integration
def test_regrade_against_a_replacement_case_runs_its_hidden_metrics_script(store, graded_benchmark):
    from dryheave.assessments import regrade_run

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    (original_id,) = assess_run(store, summary.run_id)
    original = load_assessment(store, original_id)
    replacement = measured_benchmark(store, graded_benchmark, MEASURE).cases[0]
    assert "measure.py" not in load_frozen_case(store, original.case_id).hidden_files

    _, (regraded_id,) = regrade_run(store, summary.run_id, case=replacement)

    regraded = load_assessment(store, regraded_id)
    assert (regraded.case_id, regraded.graded_case_id) == (original.case_id, replacement)
    assert regraded.audit.input_integrity == "verified"
    assert [(item.metrics_id, item.outcome) for item in regraded.metrics] == [
        ("greeting-metrics", "completed")
    ]
    assert [(item.metric_id, item.value) for item in regraded.metrics[0].values][:1] == [
        ("changed_lines", 1)
    ]
    assert regraded.criteria_id != original.criteria_id
    assert (regraded.previous_id, regraded.outcome, regraded.eligible) == (
        original_id,
        "pass",
        True,
    )
    assert store.get(regraded_id).payload["graded_case_id"] == replacement


def other_repository(store, root: Path) -> str:
    from conftest import survey_commit, survey_repo
    from dryheave.repositories import capture_repository

    git = survey_repo(root, "other")
    commit = survey_commit(git, "initial", {"greet.py": "def greet(name):\n    return name\n"})
    return capture_repository(store, git.root, commit)


@pytest.mark.integration
@pytest.mark.parametrize("problem", ["initial_prompt", "repository_id", "judge"])
def test_regrade_refuses_an_incompatible_case_or_a_missing_judge(
    store, graded_benchmark, tmp_path, problem
):
    from dryheave.assessments import regrade_run
    from dryheave.cases import TaskFact
    from dryheave.errors import InputError

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    (original_id,) = assess_run(store, summary.run_id)
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    changes = {
        "initial_prompt": {"initial_prompt": "Rewrite the greeting in French."},
        "repository_id": {"repository_id": other_repository(store, tmp_path)},
        "judge": {
            "allowed_facts": (
                *case.allowed_facts,
                TaskFact(
                    fact_id="asked",
                    phase="fix",
                    text="Ask which punctuation to use.",
                    check="The agent asked which punctuation to use before editing.",
                    curator_authored=True,
                ),
            )
        },
    }[problem]
    changed = case.model_copy(update=changes)
    replacement = store.put(
        ObjectKind.CASE,
        changed,
        files=store.read_blobs(case_id),
        references=(changed.persona_id, changed.repository_id),
    )
    with pytest.raises(InputError, match=problem):
        regrade_run(store, summary.run_id, case=replacement)
    assert assess_run(store, summary.run_id) == (original_id,)
    events = RunStore(store.root).inspect(summary.run_id).events
    assert not [event for event in events if event.event.startswith("regrade-")]


@pytest.mark.integration
def test_interrupted_regrade_resumes_under_the_same_regrade_id(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.assessments import invoke_judge, regrade_run
    from dryheave.errors import InputError

    benchmark = judged_regrade_benchmark(store, graded_benchmark, tmp_path)
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    (original_id,) = assess_run(store, summary.run_id)

    def interrupt(*args):
        invoke_judge(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.assessments.invoke_judge", interrupt)
    with pytest.raises(KeyboardInterrupt):
        regrade_run(store, summary.run_id)
    assert run_status(store, summary.run_id).attempts[0].assessment_id == original_id
    started = [
        event.data["regrade_id"]
        for event in RunStore(store.root).inspect(summary.run_id).events
        if event.event == "regrade-start"
    ]
    with pytest.raises(InputError, match=started[0]):
        assess_run(store, summary.run_id)
    with pytest.raises(InputError, match=r"--regrade$"):
        regrade_run(store, summary.run_id, case=graded_benchmark.cases[0])

    monkeypatch.setattr(
        "dryheave.assessments.invoke_judge",
        lambda *_args: pytest.fail("An interrupted regrade judge call must not repeat."),
    )
    regrade_id, (regraded_id,) = regrade_run(store, summary.run_id)

    assert [regrade_id] == started
    assert judge_calls(tmp_path) == 2
    regraded = load_assessment(store, regraded_id)
    assert regraded.previous_id == original_id
    assert [(item.criterion_id, item.outcome, item.error) for item in regraded.criteria][1:] == [
        ("quality", "error", "interrupted_judge")
    ]
    assert len(regraded.roles[2].records) == 1
    assert assess_run(store, summary.run_id) == (regraded_id,)
    monkeypatch.setattr("dryheave.assessments.invoke_judge", invoke_judge)
    assert regrade_run(store, summary.run_id)[0] != regrade_id
    assert judge_calls(tmp_path) == 3


@pytest.mark.integration
def test_regrade_resume_treats_a_crash_before_the_regrade_assessed_marker_as_done(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.assessments import regrade_run
    from dryheave.journals import RunJournal

    benchmark = judged_regrade_benchmark(store, graded_benchmark, tmp_path)
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    (original_id,) = assess_run(store, summary.run_id)

    real_append = RunJournal.append

    def crash_before_marker(self, event, data=None, **kwargs):
        if event == "regrade-assessed":
            raise KeyboardInterrupt
        return real_append(self, event, data, **kwargs)

    monkeypatch.setattr(RunJournal, "append", crash_before_marker)
    with pytest.raises(KeyboardInterrupt):
        regrade_run(store, summary.run_id)
    monkeypatch.setattr(RunJournal, "append", real_append)

    started = [
        event.data["regrade_id"]
        for event in RunStore(store.root).inspect(summary.run_id).events
        if event.event == "regrade-start"
    ]
    assessed = [
        event
        for event in RunStore(store.root).inspect(summary.run_id).events
        if event.event == "regrade-assessed"
    ]
    assert started and not assessed

    state = run_status(store, summary.run_id).attempts[0]
    assert state.stage == TrialStage.FINISHED
    crashed_id = state.assessment_id
    assert crashed_id not in (None, original_id)
    crashed = load_assessment(store, crashed_id)
    assert (crashed.previous_id, crashed.regrade_id) == (original_id, started[0])

    monkeypatch.setattr(
        "dryheave.assessments.invoke_judge",
        lambda *_args: pytest.fail(
            "An attempt already regraded in this crashed pass must not be judged again."
        ),
    )
    regrade_id, (resumed_id,) = regrade_run(store, summary.run_id)

    assert regrade_id == started[0]
    assert resumed_id == crashed_id
    resumed = load_assessment(store, resumed_id)
    assert resumed.previous_id == original_id
    events = RunStore(store.root).inspect(summary.run_id).events
    assert [
        event.data.get("assessment_id") for event in events if event.event == "regrade-assessed"
    ] == [crashed_id]


@pytest.mark.integration
def test_regrade_resume_skips_a_finished_attempt_and_reconciles_one_left_grading(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.assessments import invoke_judge, regrade_run

    benchmark = judged_regrade_benchmark(store, graded_benchmark, tmp_path)
    summary = run_experiment(
        store,
        create_experiment(store, benchmark.model_copy(update={"repetitions": 2})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )

    calls = {"n": 0}

    def fail_second_call(*args):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return invoke_judge(*args)

    monkeypatch.setattr("dryheave.assessments.invoke_judge", fail_second_call)
    with pytest.raises(KeyboardInterrupt):
        regrade_run(store, summary.run_id)

    attempts = run_status(store, summary.run_id).attempts
    finished = [state for state in attempts if state.stage == TrialStage.FINISHED]
    grading = [state for state in attempts if state.stage == TrialStage.GRADING]
    assert len(finished) == 1 and len(grading) == 1
    finished_assessment_id = finished[0].assessment_id
    assert finished_assessment_id is not None
    grading_audit_id = grading[0].assessment_id
    assert grading_audit_id not in (None, finished_assessment_id)
    assert load_assessment(store, grading_audit_id).phase == "audited"

    monkeypatch.setattr(
        "dryheave.assessments.invoke_judge",
        lambda *_args: pytest.fail(
            "Neither a finished nor an in-progress judge intent should repeat."
        ),
    )
    regrade_id, resumed_ids = regrade_run(store, summary.run_id)

    attempts = run_status(store, summary.run_id).attempts
    assert all(state.stage == TrialStage.FINISHED for state in attempts)
    still_finished = next(state for state in attempts if state.attempt_id == finished[0].attempt_id)
    assert still_finished.assessment_id == finished_assessment_id
    resumed_grading = next(state for state in attempts if state.attempt_id == grading[0].attempt_id)
    assert resumed_grading.assessment_id in resumed_ids
    resumed_assessment = load_assessment(store, resumed_grading.assessment_id)
    assert resumed_assessment.previous_id == grading_audit_id
    assert resumed_assessment.regrade_id == regrade_id
    assert any(item.error == "interrupted_judge" for item in resumed_assessment.criteria)


@pytest.mark.integration
def test_regrade_keeps_reviews_of_findings_its_audit_still_reports(
    store, graded_benchmark, historical_repo, monkeypatch
):
    from datetime import UTC, datetime

    from dryheave.assessments import regrade_run, review_assessment
    from dryheave.drivers.models import NativeEvent
    from dryheave.fixture_subject import FixtureTerminal
    from dryheave.result_models import AuditReview

    enter = FixtureTerminal.enter

    def record_access(terminal):
        enter(terminal)
        if terminal.enters == 1:
            terminal.source.batches[-1].insert(
                0,
                NativeEvent(
                    kind="tool",
                    session_id="fixture-root",
                    data={
                        "name": "Read",
                        "input": {"file_path": str(historical_repo[0] / "greet.py")},
                    },
                ),
            )

    monkeypatch.setattr(FixtureTerminal, "enter", record_access)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    original = load_assessment(store, assess_run(store, summary.run_id)[0])
    review = AuditReview(
        finding_id=original.audit.findings[0].finding_id,
        decision="dismiss",
        reviewer="curator",
        reason="The recorded path request was examined against the retained evidence.",
        reviewed_at=datetime.now(UTC),
    )
    reviewed_id = review_assessment(store, summary.run_id, original.attempt_id, review)

    _, (regraded_id,) = regrade_run(store, summary.run_id)

    regraded = load_assessment(store, regraded_id)
    assert regraded.previous_id == reviewed_id
    assert regraded.reviews == (review,)
    assert (regraded.exclusion_reasons, regraded.eligible) == ((), True)


def final_judge_run(
    store, graded_benchmark, tmp_path, answers, *, workflow, max_calls=None, **case_changes
):
    import json

    from conftest import workflow_benchmark
    from dryheave.controller_models import RoleBudget

    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path, **case_changes)
    if max_calls is not None:
        judge = draft.scoring.judge
        draft = draft.model_copy(
            update={
                "scoring": draft.scoring.model_copy(
                    update={
                        "judge": judge.model_copy(
                            update={"budget": RoleBudget(max_calls=max_calls)}
                        )
                    }
                )
            }
        )
    (tmp_path / "fact-verdicts.json").write_text(json.dumps(answers))
    summary = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture", workflow=workflow),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    requests = [
        [item["fact_id"] for item in json.loads(path.read_text())["facts"]]
        for path in sorted(tmp_path.glob("fact-request-*.json"))
    ]
    return result, requests


def verdicts(result):
    return {item.fact_id: (item.verdict, item.source) for item in result.facts}


def interrupted_final_judge(store, graded_benchmark, tmp_path, monkeypatch, target):
    import json

    from conftest import workflow_benchmark

    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    (tmp_path / "fact-verdicts.json").write_text(json.dumps([{"interface": "met"}]))
    summary = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )

    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(f"dryheave.assessments.{target}", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assess_run(store, summary.run_id)
    return summary


def fact_requests(tmp_path):
    return len(list(tmp_path.glob("fact-request-*.json")))


@pytest.mark.integration
def test_resume_after_a_fact_judge_intent_without_a_grade_never_repeats_the_call(
    store, graded_benchmark, tmp_path, monkeypatch
):
    summary = interrupted_final_judge(
        store, graded_benchmark, tmp_path, monkeypatch, "invoke_final_judge"
    )
    monkeypatch.setattr(
        "dryheave.assessments.invoke_final_judge",
        lambda *_args: pytest.fail("An interrupted fact judge call must not repeat."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert fact_requests(tmp_path) == 0
    assert verdicts(result) == {"punctuation": ("met", "verifier")}
    assert (result.outcome, result.outcome_reason) == ("indeterminate", "grading_error")
    assert {item.criterion_id: item.outcome for item in result.criteria}["quality"] == "pass"


@pytest.mark.integration
def test_resume_after_the_fact_grade_reuses_the_saved_verdicts(
    store, graded_benchmark, tmp_path, monkeypatch
):
    summary = interrupted_final_judge(
        store, graded_benchmark, tmp_path, monkeypatch, "invoke_judge"
    )
    assert fact_requests(tmp_path) == 1
    monkeypatch.setattr(
        "dryheave.assessments.invoke_final_judge",
        lambda *_args: pytest.fail("Saved fact verdicts must not be judged again."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert fact_requests(tmp_path) == 1
    assert verdicts(result) == {"punctuation": ("met", "verifier"), "interface": ("met", "judge")}
    assert (result.outcome, result.outcome_reason) == ("pass", "all_met")


@pytest.mark.integration
def test_final_judge_reasks_only_missing_facts_and_keeps_uncovered_problems(
    store, graded_benchmark, tmp_path
):
    answers = [{"interface": "missing", "uncovered": ["An empty name greets nobody."]}, {}]
    result, requests = final_judge_run(store, graded_benchmark, tmp_path, answers, workflow=False)
    assert requests == [["interface"], ["interface"]]
    assert verdicts(result) == {"punctuation": ("met", "verifier"), "interface": ("met", "judge")}
    assert result.uncovered == ("An empty name greets nobody.",)
    assert (result.outcome, result.outcome_reason, result.eligible) == ("pass", "all_met", True)


@pytest.mark.integration
def test_an_over_long_uncovered_list_is_clipped_not_rejected(store, graded_benchmark, tmp_path):
    notes = ["x" * 700, *(f"problem {index}" for index in range(24))]
    result, requests = final_judge_run(
        store, graded_benchmark, tmp_path, [{"uncovered": notes}], workflow=False
    )
    assert requests == [["interface"]]
    assert verdicts(result)["interface"] == ("met", "judge")
    assert result.uncovered == ("x" * 600, *(f"problem {index}" for index in range(19)))
    assert (result.outcome, result.outcome_reason) == ("pass", "all_met")


@pytest.mark.integration
def test_a_fact_the_final_judge_never_returns_leaves_the_attempt_indeterminate(
    store, graded_benchmark, tmp_path
):
    result, requests = final_judge_run(
        store, graded_benchmark, tmp_path, [{"interface": "missing"}], workflow=False
    )
    assert requests == [["interface"], ["interface"]]
    assert verdicts(result) == {"punctuation": ("met", "verifier")}
    assert (result.outcome, result.outcome_reason) == ("indeterminate", "grading_error")
    assert "unscorable" in result.exclusion_reasons


@pytest.mark.integration
def test_an_unmet_judged_requirement_fails_the_attempt(store, graded_benchmark, tmp_path):
    result, _ = final_judge_run(
        store, graded_benchmark, tmp_path, [{"interface": "unmet"}], workflow=False
    )
    assert (result.outcome, result.outcome_reason, result.eligible) == ("fail", "unmet", True)
    assert result.fact_account is not None
    assert [(item.fact_id, item.credit) for item in result.fact_account.facts] == [
        ("punctuation", "elicited"),
        ("interface", "unmet"),
    ]


@pytest.mark.integration
def test_a_verifier_crashing_on_the_subject_tree_fails_the_attempt(
    store, graded_benchmark, tmp_path
):
    broken = graded_benchmark.fixture[1].model_copy(
        update={"files": {"greet.py": "def greet(name)\n    return name\n"}}
    )
    crashing = graded_benchmark.model_copy(
        update={"fixture": (graded_benchmark.fixture[0], broken)}
    )
    result, _ = final_judge_run(store, crashing, tmp_path, [{"interface": "met"}], workflow=False)
    assert verdicts(result) == {"punctuation": ("unmet", "verifier"), "interface": ("met", "judge")}
    (punctuation,) = (item for item in result.facts if item.fact_id == "punctuation")
    assert (punctuation.observation, punctuation.evidence) == (
        "Use a comma after Hello. (crashed)",
        "verifier hidden.py: exited, exit code 1, expected_execution_evidence_missing",
    )
    assert (result.outcome, result.outcome_reason, result.eligible) == ("fail", "unmet", True)


@pytest.mark.integration
def test_an_unmet_later_phase_requirement_without_a_workflow_fails_the_attempt(
    store, graded_benchmark, tmp_path, monkeypatch
):
    dialog = WorkflowProgress(
        steps=(),
        position=0,
        events=(WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=1),),
    )
    monkeypatch.setattr("dryheave.runner_attempt.workflow_progress", lambda *_a, **_k: dialog)
    later = {
        "fact_id": "docs-note",
        "phase": "docs",
        "text": "Document the greeting.",
        "check": "README mentions the greeting.",
        "curator_authored": True,
    }
    result, _ = final_judge_run(
        store,
        graded_benchmark,
        tmp_path,
        [{"docs-note": "unmet"}],
        workflow=False,
        facts=(later,),
        phases=("fix", "docs"),
    )
    assert result.workflow is None
    assert (result.outcome, result.outcome_reason) == ("fail", "unmet")


@pytest.mark.integration
def test_a_spent_judge_budget_skips_the_rejudge_and_the_rubrics_with_their_own_reason(
    store, graded_benchmark, tmp_path
):
    result, requests = final_judge_run(
        store, graded_benchmark, tmp_path, [{"interface": "unmet"}], workflow=True, max_calls=1
    )
    assert requests == [["interface"]]
    interface = next(item for item in result.facts if item.fact_id == "interface")
    assert interface.verdict == "disputed"
    assert interface.evidence.startswith("re-judge skipped, judge call budget spent; ")
    rubrics = [item for item in result.criteria if item.kind == "judge"]
    assert {(item.outcome, item.error) for item in rubrics} == {
        ("not_run", "judge_budget_exhausted")
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rejudged", "expected", "outcome"),
    [
        ("unmet", ("disputed", "judge"), ("indeterminate", "disputed")),
        ("met", ("met", "judge"), ("pass", "all_met")),
    ],
)
def test_a_final_verdict_contradicting_the_last_inspection_is_rejudged_then_disputed(
    store, graded_benchmark, tmp_path, rejudged, expected, outcome
):
    answers = [{"interface": "unmet"}, {"interface": rejudged}]
    result, requests = final_judge_run(store, graded_benchmark, tmp_path, answers, workflow=True)
    assert requests == [["interface"], ["interface"]]
    assert verdicts(result)["interface"] == expected
    assert (result.outcome, result.outcome_reason) == outcome
    assert result.workflow is not None
    assert (result.workflow.final_inspection, result.workflow.completed) == ("passed", True)


@pytest.mark.integration
def test_a_regrade_with_a_corrected_check_reports_the_new_verdict_instead_of_a_dispute(
    store, graded_benchmark, tmp_path
):
    import json

    from dryheave.assessments import regrade_run
    from dryheave.cases import load_frozen_case

    result, _ = final_judge_run(
        store, graded_benchmark, tmp_path, [{"interface": "met"}], workflow=True
    )
    case = load_frozen_case(store, result.case_id)
    corrected = tuple(
        fact.model_copy(update={"check": "greet also rejects an empty name."})
        if fact.fact_id == "interface"
        else fact
        for fact in case.allowed_facts
    )
    replacement = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"allowed_facts": corrected}),
        files=store.read_blobs(result.case_id),
        references=(case.persona_id, case.repository_id),
    )
    (tmp_path / "fact-verdicts.json").write_text(json.dumps([{"interface": "unmet"}]))
    for request in tmp_path.glob("fact-request-*.json"):
        request.unlink()
    _, (regraded_id,) = regrade_run(store, result.run_id, case=replacement)
    regraded = load_assessment(store, regraded_id)
    assert fact_requests(tmp_path) == 1
    assert verdicts(regraded)["interface"] == ("unmet", "judge")
    assert (regraded.outcome, regraded.outcome_reason) == ("fail", "unmet")
