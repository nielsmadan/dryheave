import json
import sys
import threading
from pathlib import Path

import pytest

from dryheave.assessments import assess_run
from dryheave.controller_models import ControllerRecipe
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.models import CommandSpec
from dryheave.reports import compare_runs, report_run
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions


@pytest.mark.integration
def test_partial_judge_spend_across_attempts_uses_one_defensive_snapshot(
    store, benchmark, monkeypatch
):
    from dryheave.controller_models import RoleCall
    from dryheave.journals import RunJournal, RunStore
    from dryheave.runner_state import ExecutionJournal

    summary = run_experiment(
        store,
        create_experiment(store, benchmark.model_copy(update={"repetitions": 4})),
        options=RunOptions(mode="offline-fixture"),
    )
    with RunStore(store.root).open(summary.run_id) as journal:
        execution = ExecutionJournal(journal)
        for index, state in enumerate(execution.attempts.values(), 1):
            execution.evidence(
                state,
                "judge-result",
                RoleCall(call_id="observed", role="judge", status="completed", cost=index / 4),
            )
            execution.evidence(
                state, "judge-intent", RoleCall(call_id="pending", role="judge", status="intent")
            )
    getter = RunJournal.events.fget
    snapshots = []

    def snapshot(journal):
        snapshots.append(journal.metadata.run_id)
        return getter(journal)

    monkeypatch.setattr(RunJournal, "events", property(snapshot))
    report = report_run(store, summary.run_id)
    assert snapshots == [summary.run_id]
    assert [attempt.roles[2].known_cost for attempt in report.attempts] == [0.25, 0.5, 0.75, 1.0]
    assert report.groups[0].known_spend_by_currency == {"UNSPECIFIED": 2.5}
    assert report.groups[0].unknown_cost_records == 4
    assert all(attempt.roles[2].coverage == "partial" for attempt in report.attempts)


@pytest.mark.integration
def test_currencyless_proven_zero_is_neutral_and_pending_spend_is_visible(
    store, graded_benchmark, tmp_path
):
    response = {
        "decision": {"action": "stop", "reason": "The fixture is complete."},
        "usage": {
            "uncached_input": 100,
            "cache_read": 0,
            "cache_write": 0,
            "output": 50,
            "provenance": "synthetic observed usage",
        },
        "observed_model": "fixture-model",
    }
    script = tmp_path / "simulator.py"
    script.write_text("print(" + repr(json.dumps(response)) + ")\n")
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, str(script))),
    )
    scoring = ScoringConfig(
        prices=PriceTable(
            version="fixture-v1",
            effective_date="2026-09-01",
            currency="USD",
            rates=(
                PriceRate(
                    model="fixture-model",
                    uncached_input=1.0,
                    cache_read=0.0,
                    cache_write=0.0,
                    output=2.0,
                ),
            ),
        )
    )
    summary = run_experiment(
        store,
        create_experiment(
            store, graded_benchmark.model_copy(update={"simulator": recipe, "scoring": scoring})
        ),
        options=RunOptions(mode="offline-fixture"),
    )
    pending = report_run(store, summary.run_id)
    assert pending.groups[0].known_spend_by_currency == {"USD": 0.0002}
    assert pending.groups[0].costs.mean == 0.0002
    assess_run(store, summary.run_id)
    report = report_run(store, summary.run_id)
    assert report.groups[0].costs.mean == 0.0002
    assert report.groups[0].costs.unknown == 0
    assert report.groups[0].known_spend_by_currency == {"USD": 0.0002}


@pytest.mark.integration
def test_compatible_comparison_retains_partial_attempt_attrition(
    store, graded_benchmark, monkeypatch
):
    from dryheave.fixture_subject import FixtureTerminal

    identifier = create_experiment(store, graded_benchmark.model_copy(update={"repetitions": 2}))
    before = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    assess_run(store, before.run_id)
    cancelled = threading.Event()
    close = FixtureTerminal.close

    def stop_after_first(terminal):
        report = close(terminal)
        cancelled.set()
        return report

    monkeypatch.setattr(FixtureTerminal, "close", stop_after_first)
    after = run_experiment(
        store, identifier, options=RunOptions(mode="offline-fixture"), cancelled=cancelled
    )
    assert len(after.unstarted_trials) == 1
    assess_run(store, after.run_id)
    comparison = compare_runs(store, before.run_id, after.run_id)
    assert comparison.paired_count == 1
    assert comparison.after_metrics[0].scheduled_trials == 2
    assert comparison.after_metrics[0].attempts == 1


def test_unstarted_variant_scheduling_remains_visible(store, graded_benchmark):
    cancelled = threading.Event()
    cancelled.set()
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        cancelled=cancelled,
    )
    report = report_run(store, summary.run_id)
    assert report.groups[0].scheduled_trials == 1
    assert report.groups[0].attempts == 0
    assert report.groups[0].eligible_completion_rate is None


@pytest.mark.integration
def test_latest_unassessed_retry_is_attrition_and_retains_earlier_spend(
    store, graded_benchmark, tmp_path
):
    from dryheave.bundles import export_bundle
    from dryheave.controller_models import RoleCall
    from dryheave.journals import RunStore
    from dryheave.runner_state import ExecutionJournal

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    with RunStore(store.root).open(summary.run_id) as journal:
        execution = ExecutionJournal(journal)
        state = execution.attempts[summary.attempts[0].attempt_id]
        execution.evidence(
            state,
            "judge-result",
            RoleCall(
                call_id="paid-judge",
                role="judge",
                status="completed",
                cost=0.25,
            ),
        )
    assess_run(store, summary.run_id)
    earlier = export_bundle(store, summary.run_id, tmp_path / "before.tar").roots[0]
    store.set_alias("earlier-report", earlier)
    retry = run_experiment(store, resume=summary.run_id, retry=(state.trial_id,))
    comparison = compare_runs(store, "earlier-report", summary.run_id)
    assert (comparison.before_input, comparison.after_input) == ("earlier-report", summary.run_id)
    assert (comparison.before_reference, comparison.after_reference) == (earlier, summary.run_id)
    assert comparison.before_sequence < comparison.after_sequence
    assert (comparison.before, comparison.after) == (summary.run_id, summary.run_id)
    assert comparison.paired_count == 0
    assert (comparison.excluded_before, comparison.excluded_after) == (0, 1)
    assert comparison.after_metrics[0].attempts == 2
    assert comparison.after_metrics[0].known_spend_by_currency == {"UNSPECIFIED": 0.25}
    later = export_bundle(store, summary.run_id, tmp_path / "after.tar").roots[0]
    store.set_alias("earlier-report", later, replace=True)
    assert report_run(store, "earlier-report").durable_sequence == comparison.after_sequence
    assert (
        report_run(store, comparison.before_reference).durable_sequence
        == comparison.before_sequence
    )
    assert (
        report_run(store, summary.run_id).attempts[-1].attempt_id == retry.attempts[-1].attempt_id
    )


@pytest.mark.integration
def test_completely_unassessed_matching_run_is_reported_as_attrition(store, graded_benchmark):
    identifier = create_experiment(store, graded_benchmark)
    before = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    assess_run(store, before.run_id)
    after = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    comparison = compare_runs(store, before.run_id, after.run_id)
    assert comparison.paired_count == 0
    assert (comparison.excluded_before, comparison.excluded_after) == (0, 1)


@pytest.mark.integration
def test_saved_variant_comparison_records_real_failure_improvement_and_selected_spend(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.controller_models import RoleCall
    from dryheave.experiment_models import VariantSpec
    from dryheave.fixture_subject import FixtureTerminal
    from dryheave.journals import RunStore
    from dryheave.report_models import Comparison
    from dryheave.runner_state import ExecutionJournal
    from dryheave.serialization import canonical_json, parse_model

    base_profile = store.resolve(graded_benchmark.variants[0].profile)
    close = FixtureTerminal.close

    def finish_fixture(terminal):
        if terminal.plan.profile_id == base_profile:
            (Path(terminal.plan.cwd) / "greet.py").write_text(
                'def greet(name):\n    return "Hello " + name\n'
            )
        return close(terminal)

    monkeypatch.setattr(FixtureTerminal, "close", finish_fixture)
    draft = graded_benchmark.model_copy(
        update={
            "variants": (
                *graded_benchmark.variants,
                VariantSpec(name="changed", profile=base_profile, model="changed-model"),
            )
        }
    )
    summary = run_experiment(
        store, create_experiment(store, draft), options=RunOptions(mode="offline-fixture")
    )
    variants = {item.trial_id: item.variant for item in report_run(store, summary.run_id).attempts}
    with RunStore(store.root).open(summary.run_id) as journal:
        execution = ExecutionJournal(journal)
        for state in execution.attempts.values():
            execution.evidence(
                state,
                "judge-result",
                RoleCall(
                    call_id=state.attempt_id + "-cost",
                    role="judge",
                    status="completed",
                    cost=0.1 if variants[state.trial_id] == "base" else 0.2,
                ),
            )
    assess_run(store, summary.run_id)
    report = report_run(store, summary.run_id)
    results = {item.variant: item.result for item in report.attempts}
    assert [(results[name].completion, results[name].eligible) for name in ("base", "changed")] == [
        ("fail", True),
        ("pass", True),
    ]
    assert all(
        result.criteria[0].calibration.status == "demonstrated" for result in results.values()
    )
    assert {group.variant: group.eligible_completion_rate for group in report.groups} == {
        "base": 0.0,
        "changed": 1.0,
    }
    comparison = compare_runs(
        store, summary.run_id, summary.run_id, before_variant="base", after_variant="changed"
    )
    saved = tmp_path / "comparison.json"
    saved.write_bytes(canonical_json(comparison))
    restored = parse_model(saved.read_bytes(), Comparison)
    assert restored.paired_count == 1
    assert (restored.excluded_before, restored.excluded_after) == (0, 0)
    assert restored.pairs[0].criterion_changes == {"greeting": ("fail", "pass")}
    assert (restored.pairs[0].before_completion, restored.pairs[0].after_completion) == (
        "fail",
        "pass",
    )
    assert (restored.before_variant, restored.after_variant) == ("base", "changed")
    assert (restored.before_reference, restored.after_reference) == (summary.run_id, summary.run_id)
    assert (restored.before_sequence, restored.after_sequence) == (report.durable_sequence,) * 2
    assert [group.variant for group in restored.before_metrics] == ["base"]
    assert [group.variant for group in restored.after_metrics] == ["changed"]
    assert restored.before_metrics[0].known_spend_by_currency == {"UNSPECIFIED": pytest.approx(0.1)}
    assert restored.after_metrics[0].known_spend_by_currency == {"UNSPECIFIED": pytest.approx(0.2)}
