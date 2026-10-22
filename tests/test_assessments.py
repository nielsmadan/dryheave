import os
from pathlib import Path

import pytest

from conftest import HOST
from dryheave.assessments import assess_run
from dryheave.cases import DeterministicCriterion, load_frozen_case
from dryheave.errors import IntegrityError
from dryheave.experiments import create_experiment
from dryheave.integrity import load_assessment
from dryheave.journals import RunStore
from dryheave.models import ObjectKind, TrialStage
from dryheave.reports import report_run
from dryheave.runner import load_capture, run_experiment, run_status
from dryheave.runner_models import RunOptions, WorkflowEvent, WorkflowProgress


def test_workflow_outcome_is_none_without_a_workflow() -> None:
    from dryheave.assessments import _workflow_outcome

    assert _workflow_outcome(None, {}) is None


def test_workflow_outcome_for_a_completed_workflow() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(
        steps=("review", "check"),
        position=2,
        events=(
            WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=1),
            WorkflowEvent(step_id="check", kind="inspect", outcome="passed", turn=2, seconds=1.5),
        ),
    )
    outcome = _workflow_outcome(progress, {"review": "prompt", "check": "inspect"})
    assert outcome is not None
    assert (outcome.steps, outcome.completed_steps, outcome.inspections) == (2, 2, 1)
    assert (outcome.fix_requests, outcome.dialogs_answered) == (0, 0)
    assert (outcome.first_inspection, outcome.final_inspection) == ("passed", "passed")
    assert outcome.inspection_seconds == 1.5
    assert outcome.events == progress.events


def test_workflow_outcome_for_a_failed_final_inspection_counts_reused_as_failed() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(
        steps=("check",),
        position=0,
        events=(
            WorkflowEvent(step_id="check", kind="inspect", outcome="failed", turn=1, seconds=1.0),
            WorkflowEvent(step_id="check", kind="inspect", outcome="fix_requested", turn=1),
            WorkflowEvent(step_id="check", kind="inspect", outcome="reused", turn=2, seconds=0.0),
            WorkflowEvent(step_id="check", kind="inspect", outcome="fix_requested", turn=2),
        ),
    )
    outcome = _workflow_outcome(progress, {"check": "inspect"})
    assert outcome is not None
    assert (outcome.inspections, outcome.fix_requests) == (2, 2)
    assert (outcome.first_inspection, outcome.final_inspection) == ("failed", "failed")


def test_workflow_outcome_is_not_reached_when_steps_exist_without_an_inspection() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(steps=("check",), position=0, events=())
    outcome = _workflow_outcome(progress, {"check": "inspect"})
    assert outcome is not None
    assert (outcome.inspections, outcome.first_inspection, outcome.final_inspection) == (
        0,
        "none",
        "not_reached",
    )


def test_workflow_outcome_for_an_unavailable_inspection() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(
        steps=("check",),
        position=0,
        events=(WorkflowEvent(step_id="check", kind="inspect", outcome="unavailable", turn=1),),
    )
    outcome = _workflow_outcome(progress, {"check": "inspect"})
    assert outcome is not None
    assert (outcome.first_inspection, outcome.final_inspection) == ("unavailable", "unavailable")
    assert outcome.inspections == 1


def test_workflow_outcome_for_dialogs_answered_without_a_declared_workflow() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(
        steps=(),
        position=0,
        events=(WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=1),),
    )
    outcome = _workflow_outcome(progress, {})
    assert outcome is not None
    assert (outcome.steps, outcome.completed_steps, outcome.dialogs_answered) == (0, 0, 1)
    assert (outcome.first_inspection, outcome.final_inspection) == ("none", "none")


def test_workflow_outcome_final_inspection_is_not_reached_when_the_retest_never_ran() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(
        steps=("manual-test", "review", "retest"),
        position=2,
        events=(
            WorkflowEvent(
                step_id="manual-test", kind="inspect", outcome="passed", turn=1, seconds=1.0
            ),
            WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=2),
        ),
    )
    outcome = _workflow_outcome(
        progress, {"manual-test": "inspect", "review": "prompt", "retest": "inspect"}
    )
    assert outcome is not None
    assert outcome.first_inspection == "passed"
    assert outcome.final_inspection == "not_reached"


def test_workflow_outcome_final_inspection_is_none_for_a_prompt_only_workflow() -> None:
    from dryheave.assessments import _workflow_outcome

    progress = WorkflowProgress(
        steps=("review",),
        position=1,
        events=(WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=1),),
    )
    outcome = _workflow_outcome(progress, {"review": "prompt"})
    assert outcome is not None
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
    assert (result.audit.input_integrity, result.completion, result.eligible) == (
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
        "dryheave.assessments.grade_criterion",
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
        "dryheave.assessments.grade_criterion",
        lambda *_args: pytest.fail("Tampered inputs cannot be graded."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.audit.input_integrity == "invalid"
    assert result.completion == "indeterminate"
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
def test_zero_checks_and_failure_are_not_success(store, graded_benchmark, program):
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
    assert result.completion == "indeterminate"


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
    assert result.completion == "pass"
    assert result.audit.findings[0].rule_id == "forbidden-git-object"
    assert result.audit.findings[0].confidence == "confirmed"
    assert result.eligible is False


@pytest.mark.integration
def test_relative_verifier_executable_requires_an_explicit_trusted_path(store, graded_benchmark):
    import json

    from dryheave.models import CommandSpec

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    criterion = case.criteria[0].model_copy(
        update={"command": CommandSpec(argv=("./wrapper", "{verifier}/hidden.py"))}
    )
    case = case.model_copy(update={"criteria": (criterion,)})
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
    assert result.completion == "indeterminate"
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
    from dryheave.grading import grade_criterion

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )

    def interrupt(context, criterion):
        grade_criterion(context, criterion)
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.assessments.grade_criterion", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assess_run(store, summary.run_id)
    assert run_status(store, summary.run_id).attempts[0].stage == TrialStage.GRADING
    monkeypatch.setattr(
        "dryheave.assessments.grade_criterion",
        lambda *_args: pytest.fail("Ambiguous verifier execution must not repeat."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].error == "interrupted_verifier"
    assert result.completion == "indeterminate"
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
    criteria = tuple(
        DeterministicCriterion(
            criterion_id="suite-shared"
            if layout == "colliding-names" and name == "first"
            else name,
            description="Confirm independent or explicitly shared workspace state.",
            command=CommandSpec(argv=(sys.executable, "{verifier}/" + name + ".py")),
            entrypoint=name + ".py",
            expected_stdout="CHECK_RAN",
            suite_id="shared" if layout == "colliding-names" and name == "second" else suite,
        )
        for name in ("first", "second")
    )
    case = case.model_copy(
        update={
            "criteria": criteria,
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
    criteria = tuple(
        case.criteria[0].model_copy(update={"criterion_id": name, "suite_id": "ordered"})
        for name in ("first", "second")
    )
    case = case.model_copy(update={"criteria": criteria})
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
        "dryheave.assessments.grade_criterion",
        lambda *_args: pytest.fail("Interrupted suites cannot continue in fresh workspaces."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].outcome == ("pass" if saved_first else "error")
    assert result.criteria[1].error == "interrupted_verifier"
    assert result.completion == "indeterminate"


@pytest.mark.integration
def test_unjudged_assessment_keeps_legacy_shape_without_default_rubrics(store, assessed):
    from dryheave.result_models import Assessment
    from dryheave.serialization import canonical_json, parse_model

    identifier = run_status(store, assessed.run_id).attempts[0].assessment_id
    payload = store.get(identifier).payload
    result = parse_model(canonical_json(payload), Assessment)
    assert [item.criterion_id for item in result.criteria] == ["greeting"]
    assert (result.checks, result.metrics, result.rubric_set) == ((), (), None)
    assert not {"checks", "metrics", "rubric_set"} & set(payload)
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
    assert (result.completion, result.eligible) == ("pass", True)
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
    assert (result.completion, result.eligible, result.exclusion_reasons) == ("pass", True, ())


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
    assert result.completion == "pass"
    assert assess_run(store, summary.run_id) == (
        run_status(store, summary.run_id).attempts[0].assessment_id,
    )


@pytest.mark.integration
def test_help_needed_separates_disclosed_facts_by_kind(store, graded_benchmark):
    from dryheave.cases import TaskFact

    case_id = store.resolve(graded_benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    extra_fact = TaskFact(
        fact_id="greeting-style",
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
    script = graded_benchmark.simulator.script
    draft = graded_benchmark.model_copy(
        update={
            "cases": (changed_case,),
            "simulator": graded_benchmark.simulator.model_copy(
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
    assert result.help_needed is not None
    assert result.help_needed.simulator_replies == 1
    assert set(result.help_needed.facts_disclosed) == {"punctuation", "greeting-style"}
    assert result.help_needed.on_request_facts == ("punctuation",)
    assert result.help_needed.proactive_facts == ("greeting-style",)


@pytest.mark.integration
def test_help_needed_follows_the_captures_own_case_id_not_a_substitute_case(
    store, graded_benchmark
):
    from dryheave.assessment_inputs import AssessmentInputs
    from dryheave.assessments import _capture_case, _help_needed

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
    original = _help_needed(capture, _capture_case(store, AssessmentInputs(capture=capture)))
    assert original is not None
    assert original.on_request_facts == ("punctuation",)
    assert original.proactive_facts == ()

    substitute = capture.model_copy(update={"case_id": proactive_variant})
    swapped = _help_needed(substitute, _capture_case(store, AssessmentInputs(capture=substitute)))
    assert swapped is not None
    assert swapped.on_request_facts == ()
    assert swapped.proactive_facts == ("punctuation",)


@pytest.mark.integration
def test_help_needed_excludes_fix_it_and_dialog_answer_simulator_calls(store, graded_benchmark):
    from dryheave.assessments import _help_needed
    from dryheave.controller_models import RoleCall, SimulatorDecision
    from dryheave.runner_models import WorkflowEvent, WorkflowProgress

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    case = load_frozen_case(store, capture.case_id)
    baseline = _help_needed(capture, case)
    assert baseline is not None

    ordinary = RoleCall(
        call_id="ordinary-reply",
        role="simulator",
        status="completed",
        decision=SimulatorDecision(action="reply", text="Use mutual TLS", reason="asked"),
    )
    fix_it = RoleCall(
        call_id="fix-it-reply",
        role="simulator",
        status="completed",
        decision=SimulatorDecision(action="reply", text="Still broken", reason="fix"),
    )
    dialog_answer = RoleCall(
        call_id="dialog-reply",
        role="simulator",
        status="completed",
        decision=SimulatorDecision(action="reply", choices=("Option A",), reason="answered"),
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
            "controller_calls": (*capture.controller_calls, ordinary, fix_it, dialog_answer),
            "workflow": workflow,
        }
    )
    result = _help_needed(modified, case)
    assert result is not None
    assert result.simulator_replies == baseline.simulator_replies + 1


@pytest.mark.integration
def test_help_needed_excludes_screen_steps_of_an_unanswered_screen(store, graded_benchmark):
    from dryheave.assessments import _help_needed
    from dryheave.controller_models import RoleCall, SimulatorDecision
    from dryheave.drivers.models import ScreenAction

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    case = load_frozen_case(store, capture.case_id)
    baseline = _help_needed(capture, case)
    screen_step = RoleCall(
        call_id="screen-step",
        role="simulator",
        status="completed",
        decision=SimulatorDecision(
            action="reply",
            reason="Moving to the option.",
            screen_kind="question",
            screen_action=ScreenAction(key="Down"),
        ),
    )
    modified = capture.model_copy(
        update={"controller_calls": (*capture.controller_calls, screen_step)}
    )
    result = _help_needed(modified, case)
    assert (result.simulator_replies, baseline.simulator_replies) == (1, 1)


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


def test_legacy_shaped_assessment_payload_loads_through_the_store_with_new_fields_defaulted(
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
        "simulator_id": None,
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
            for role in ("subject", "simulator", "judge")
        ],
    }
    manifest = Manifest(kind=ObjectKind.RESULT, payload=legacy_payload)
    identifier = store.put_manifest(manifest, files={})
    result = load_assessment(store, identifier)
    assert (
        result.checks,
        result.metrics,
        result.rubric_set,
        result.help_needed,
        result.workflow,
    ) == (
        (),
        (),
        None,
        None,
        None,
    )


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
        simulator_id=None,
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
            role.model_copy(update={"role": "simulator"}),
            role.model_copy(update={"role": "judge"}),
        ),
        checks=(),
        metrics=(),
        rubric_set=None,
        help_needed=None,
        workflow=None,
        graded_case_id=None,
        regrade_id=None,
    )
    parsed = json.loads(canonical_json(assessment))
    assert not {
        "checks",
        "metrics",
        "rubric_set",
        "help_needed",
        "workflow",
        "graded_case_id",
        "regrade_id",
    } & set(parsed)


def judged_regrade_benchmark(store, graded_benchmark, root: Path):
    import sys

    from dryheave.cases import CaseCheck, RubricCriterion
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
            "checks": (
                CaseCheck(
                    check_id="asked",
                    question="Did the agent ask which punctuation to use?",
                    expect="yes",
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
    judge_answers(root, "pass", "yes")
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )
    return graded_benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})


def judge_answers(root: Path, outcome: str, answer: str) -> None:
    import json

    (root / "response.json").write_text(
        json.dumps(
            {
                "judgments": [
                    {"criterion_id": "quality", "outcome": outcome, "rationale": "Judged."}
                ],
                "checks": [{"check_id": "asked", "answer": answer, "rationale": "Judged."}],
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
        ("greeting", "pass"),
        ("quality", "pass"),
    ]
    judge_answers(tmp_path, "fail", "no")

    regrade_id, (regraded_id,) = regrade_run(store, summary.run_id)

    assert judge_calls(tmp_path) == 2
    regraded = load_assessment(store, regraded_id)
    assert regraded_id != original_id
    assert regraded.previous_id == original_id
    assert [(item.criterion_id, item.outcome) for item in regraded.criteria] == [
        ("greeting", "pass"),
        ("quality", "fail"),
    ]
    assert [(item.check_id, item.outcome) for item in regraded.checks] == [("asked", "fail")]
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
    assert (regraded.previous_id, regraded.completion, regraded.eligible) == (
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
    from dryheave.cases import CaseCheck
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
            "checks": (
                CaseCheck(
                    check_id="asked",
                    question="Did the agent ask which punctuation to use?",
                    expect="yes",
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
    assert any(item.outcome == "error" for item in resumed_assessment.checks)


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
