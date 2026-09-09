import json
import sys
import threading

from dryheave.assessments import assess_run
from dryheave.controller_models import ControllerRecipe
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.models import CommandSpec
from dryheave.reports import compare_runs, report_run
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions


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
    retry = run_experiment(store, resume=summary.run_id, retry=(state.trial_id,))
    comparison = compare_runs(store, earlier, summary.run_id)
    assert comparison.paired_count == 0
    assert (comparison.excluded_before, comparison.excluded_after) == (0, 1)
    assert comparison.after_metrics[0].attempts == 2
    assert comparison.after_metrics[0].known_spend_by_currency == {"UNSPECIFIED": 0.25}
    assert (
        report_run(store, summary.run_id).attempts[-1].attempt_id == retry.attempts[-1].attempt_id
    )


def test_completely_unassessed_matching_run_is_reported_as_attrition(store, graded_benchmark):
    identifier = create_experiment(store, graded_benchmark)
    before = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    assess_run(store, before.run_id)
    after = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    comparison = compare_runs(store, before.run_id, after.run_id)
    assert comparison.paired_count == 0
    assert (comparison.excluded_before, comparison.excluded_after) == (0, 1)
