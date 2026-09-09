from pathlib import Path

import pytest

from dryheave.assessments import assess_run
from dryheave.cases import DeterministicCriterion, load_frozen_case
from dryheave.errors import IntegrityError
from dryheave.experiments import create_experiment
from dryheave.integrity import load_assessment
from dryheave.models import ObjectKind, TrialStage
from dryheave.reports import report_run
from dryheave.runner import load_capture, run_experiment, run_status
from dryheave.runner_models import RunOptions


def test_real_hidden_check_calibrates_and_resume_never_relaunches(
    store, graded_benchmark, monkeypatch
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
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
    assert run_experiment(store, resume=summary.run_id).pending_assessment == ()


def test_verifier_blob_mutation_retains_invalid_report_without_loading_corrupt_inputs(
    store, graded_benchmark, monkeypatch
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
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
        run_experiment(store, resume=summary.run_id)


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
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.criteria[0].outcome == "error"
    assert result.criteria[0].calibration.status == "unavailable"
    assert result.completion == "indeterminate"


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
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.completion == "pass"
    assert result.audit.findings[0].rule_id == "forbidden-git-object"
    assert result.audit.findings[0].confidence == "confirmed"
    assert result.eligible is False


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


def test_interrupted_grading_resume_retains_error_without_reexecuting_command(
    store, graded_benchmark, monkeypatch
):
    from dryheave.grading import grade_criterion

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
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
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [item.outcome for item in result.criteria] == ["pass", "pass"]
    assert result.eligible


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
