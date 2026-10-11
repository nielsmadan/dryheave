import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dryheave.assessments import assess_run
from dryheave.controller_models import ControllerRecipe
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.models import CommandSpec, TokenUsage
from dryheave.report_models import AttemptReport, Distribution, GroupMetrics
from dryheave.reports import compare_runs, distribution, group_metrics, report_run
from dryheave.result_models import (
    Assessment,
    AuditReport,
    CheckResult,
    CriterionResult,
    HelpNeeded,
    MetricsResult,
    MetricValue,
    RoleMetrics,
    UsageCharge,
)
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.serialization import canonical_json


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


def _fixture_role(name: str, tokens: TokenUsage) -> RoleMetrics:
    return RoleMetrics(
        role=name,
        records=(),
        known_tokens=tokens,
        known_cost=None,
        currency=None,
        coverage="unknown",
        reasons=("fixture",),
    )


def _fixture_assessment(tokens: TokenUsage, case_id: str = "a" * 64) -> Assessment:
    audit = AuditReport(
        input_integrity="verified",
        capture_integrity="verified",
        cleanup="stopped",
        capture_complete=True,
        coverage=(),
    )
    return Assessment(
        run_id="a" * 32,
        experiment_id="a" * 64,
        trial_id="trial-1",
        attempt_id="attempt-1",
        capture_id=None,
        case_id=case_id,
        profile_id=None,
        simulator_id=None,
        scoring_id=None,
        criteria_id=None,
        variant="base",
        mode="offline-fixture",
        created_at=datetime.now(UTC),
        phase="finished",
        audit=audit,
        completion="pass",
        eligible=True,
        exclusion_reasons=(),
        stop_reason="completed",
        subject_observation="completed",
        roles=(
            _fixture_role("subject", tokens),
            _fixture_role("simulator", tokens),
            _fixture_role("judge", tokens),
        ),
    )


def test_group_metrics_reports_total_input_as_sum_of_uncached_cache_read_and_cache_write() -> None:
    tokens = TokenUsage(
        uncached_input=100,
        cache_read=20,
        cache_write=30,
        output=10,
        reasoning=0,
        provenance="fixture",
    )
    attempt = AttemptReport(
        attempt_id="attempt-1",
        trial_id="trial-1",
        variant="base",
        stage="finished",
        assessment_id=None,
        result=_fixture_assessment(tokens),
        raw_evidence="unavailable",
    )
    groups = group_metrics((attempt,))
    group = groups[0]
    assert group.cache_read_tokens is not None and group.cache_read_tokens.mean == 20
    assert group.cache_write_tokens is not None and group.cache_write_tokens.mean == 30
    assert group.total_input_tokens is not None and group.total_input_tokens.mean == 150


def test_group_metrics_token_distributions_count_an_ineligible_retained_attempt() -> None:
    tokens = TokenUsage(
        uncached_input=100,
        cache_read=20,
        cache_write=30,
        output=10,
        reasoning=0,
        provenance="fixture",
    )
    eligible_attempt = AttemptReport(
        attempt_id="attempt-1",
        trial_id="trial-1",
        variant="base",
        stage="finished",
        assessment_id=None,
        result=_fixture_assessment(tokens),
        raw_evidence="unavailable",
    )
    excluded_result = _fixture_assessment(tokens).model_copy(
        update={"eligible": False, "exclusion_reasons": ("contaminated",)}
    )
    excluded_attempt = AttemptReport(
        attempt_id="attempt-2",
        trial_id="trial-2",
        variant="base",
        stage="finished",
        assessment_id=None,
        result=excluded_result,
        raw_evidence="unavailable",
    )
    group = group_metrics((eligible_attempt, excluded_attempt))[0]
    assert group.eligible == 1
    assert group.cache_read_tokens is not None
    assert group.cache_read_tokens.count == 2


def test_group_metrics_reports_a_partial_role_known_cost_as_a_lower_bound() -> None:
    tokens = TokenUsage(provenance="fixture")
    priced = RoleMetrics(
        role="subject",
        records=(
            UsageCharge(
                identity="r1",
                usage=None,
                cost=0.5,
                cost_kind="estimated",
                currency="USD",
                raw_evidence_sha256="a" * 64,
                reason="fixture",
            ),
        ),
        known_tokens=tokens,
        known_cost=0.5,
        currency="USD",
        coverage="partial",
        reasons=("fixture",),
    )
    assessment = _fixture_assessment(tokens).model_copy(
        update={
            "roles": (priced, _fixture_role("simulator", tokens), _fixture_role("judge", tokens))
        }
    )
    attempt = AttemptReport(
        attempt_id="attempt-1",
        trial_id="trial-1",
        variant="base",
        stage="finished",
        assessment_id=None,
        result=assessment,
        raw_evidence="unavailable",
    )
    group = group_metrics((attempt,))[0]
    assert group.cost_by_role["subject"].mean == 0.5


def _fixture_attempt(attempt_id: str, result: Assessment) -> AttemptReport:
    return AttemptReport(
        attempt_id=attempt_id,
        trial_id=attempt_id,
        variant="base",
        stage="finished",
        assessment_id=None,
        result=result,
        raw_evidence="unavailable",
    )


def test_group_metrics_summarizes_criterion_pass_counts_and_score_distribution() -> None:
    tokens = TokenUsage(provenance="fixture")
    first = _fixture_assessment(tokens).model_copy(
        update={
            "criteria": (
                CriterionResult(
                    criterion_id="code-quality",
                    kind="judge",
                    required=False,
                    outcome="pass",
                    score=0.8,
                ),
            )
        }
    )
    second = _fixture_assessment(tokens).model_copy(
        update={
            "criteria": (
                CriterionResult(
                    criterion_id="code-quality",
                    kind="judge",
                    required=False,
                    outcome="fail",
                    score=0.2,
                ),
            )
        }
    )
    group = group_metrics(
        (_fixture_attempt("attempt-1", first), _fixture_attempt("attempt-2", second))
    )[0]
    assert len(group.criteria) == 1
    summary = group.criteria[0]
    assert (summary.criterion_id, summary.passes, summary.graded) == ("code-quality", 1, 2)
    assert (summary.scores.count, summary.scores.mean) == (2, 0.5)


def test_group_metrics_check_agreement_covers_matching_split_and_single_repetition() -> None:
    tokens = TokenUsage(provenance="fixture")

    def with_check(outcome: str) -> Assessment:
        return _fixture_assessment(tokens).model_copy(
            update={"checks": (CheckResult(check_id="right-view", outcome=outcome),)}
        )

    matching = group_metrics(
        (
            _fixture_attempt("attempt-1", with_check("pass")),
            _fixture_attempt("attempt-2", with_check("pass")),
        )
    )[0]
    assert (matching.checks[0].passes, matching.checks[0].agreement) == (2, 1.0)

    split = group_metrics(
        (
            _fixture_attempt("attempt-3", with_check("pass")),
            _fixture_attempt("attempt-4", with_check("fail")),
        )
    )[0]
    assert split.checks[0].agreement == 0.5

    single = group_metrics((_fixture_attempt("attempt-5", with_check("pass")),))[0]
    assert single.checks[0].agreement is None


def test_group_metrics_metric_distribution_ignores_non_numeric_values() -> None:
    tokens = TokenUsage(provenance="fixture")

    def with_metric(value: object) -> Assessment:
        return _fixture_assessment(tokens).model_copy(
            update={
                "metrics": (
                    MetricsResult(
                        metrics_id="coverage",
                        outcome="completed",
                        values=(
                            MetricValue(
                                metric_id="tests_passed",
                                label="Tests passed",
                                value=value,
                                unit="tests",
                            ),
                        ),
                    ),
                )
            }
        )

    group = group_metrics(
        (
            _fixture_attempt("attempt-1", with_metric(12)),
            _fixture_attempt("attempt-2", with_metric("error")),
        )
    )[0]
    assert len(group.metrics) == 1
    summary = group.metrics[0]
    assert (summary.metric_id, summary.label, summary.unit) == (
        "tests_passed",
        "Tests passed",
        "tests",
    )
    assert (summary.values.count, summary.values.mean) == (1, 12)


def test_group_metrics_simulator_replies_distribution_and_old_shape_assessment_still_builds() -> (
    None
):
    tokens = TokenUsage(provenance="fixture")
    with_help = _fixture_assessment(tokens).model_copy(
        update={
            "help_needed": HelpNeeded(
                simulator_replies=2,
                facts_disclosed=("a",),
                proactive_facts=("a",),
                on_request_facts=(),
            )
        }
    )
    old_shape = _fixture_assessment(tokens)
    group = group_metrics(
        (_fixture_attempt("attempt-1", with_help), _fixture_attempt("attempt-2", old_shape))
    )[0]
    assert group.simulator_replies is not None
    assert (group.simulator_replies.count, group.simulator_replies.unknown) == (1, 1)
    assert (group.criteria, group.checks, group.metrics) == ((), (), ())


def test_group_metrics_does_not_pool_colliding_ids_across_different_cases() -> None:
    tokens = TokenUsage(provenance="fixture")
    case_a, case_b = "a" * 64, "b" * 64

    def with_check(case_id: str, outcome: str) -> Assessment:
        return _fixture_assessment(tokens, case_id=case_id).model_copy(
            update={"checks": (CheckResult(check_id="builds", outcome=outcome),)}
        )

    group = group_metrics(
        (
            _fixture_attempt("attempt-1", with_check(case_a, "pass")),
            _fixture_attempt("attempt-2", with_check(case_a, "pass")),
            _fixture_attempt("attempt-3", with_check(case_b, "fail")),
            _fixture_attempt("attempt-4", with_check(case_b, "fail")),
        )
    )[0]
    assert len(group.checks) == 2
    by_case = {summary.case_id: summary for summary in group.checks}
    assert (by_case[case_a].passes, by_case[case_a].fails, by_case[case_a].agreement) == (2, 0, 1.0)
    assert (by_case[case_b].passes, by_case[case_b].fails, by_case[case_b].agreement) == (0, 2, 1.0)


def test_group_metrics_check_agreement_denominator_excludes_not_applicable_and_error() -> None:
    tokens = TokenUsage(provenance="fixture")

    def with_check(outcome: str) -> Assessment:
        return _fixture_assessment(tokens).model_copy(
            update={"checks": (CheckResult(check_id="right-view", outcome=outcome),)}
        )

    group = group_metrics(
        (
            _fixture_attempt("attempt-1", with_check("pass")),
            _fixture_attempt("attempt-2", with_check("pass")),
            _fixture_attempt("attempt-3", with_check("fail")),
            _fixture_attempt("attempt-4", with_check("not_applicable")),
            _fixture_attempt("attempt-5", with_check("error")),
        )
    )[0]
    summary = group.checks[0]
    assert (summary.passes, summary.fails, summary.not_applicable, summary.errors) == (2, 1, 1, 1)
    assert summary.agreement == pytest.approx(2 / 3)


def test_group_metrics_metric_distribution_counts_booleans_as_zero_and_one() -> None:
    tokens = TokenUsage(provenance="fixture")

    def with_metric(value: object) -> Assessment:
        return _fixture_assessment(tokens).model_copy(
            update={
                "metrics": (
                    MetricsResult(
                        metrics_id="coverage",
                        outcome="completed",
                        values=(
                            MetricValue(
                                metric_id="build_succeeded", label="Build succeeded", value=value
                            ),
                        ),
                    ),
                )
            }
        )

    group = group_metrics(
        (
            _fixture_attempt("attempt-1", with_metric(True)),
            _fixture_attempt("attempt-2", with_metric(False)),
        )
    )[0]
    summary = group.metrics[0]
    assert (summary.values.count, summary.values.mean) == (2, 0.5)


def test_group_metrics_metric_distribution_skips_overflowing_and_non_finite_values() -> None:
    tokens = TokenUsage(provenance="fixture")

    def with_metric(value: object) -> Assessment:
        return _fixture_assessment(tokens).model_copy(
            update={
                "metrics": (
                    MetricsResult(
                        metrics_id="coverage",
                        outcome="completed",
                        values=(MetricValue(metric_id="huge", label="Huge value", value=value),),
                    ),
                )
            }
        )

    group = group_metrics(
        (
            _fixture_attempt("attempt-1", with_metric(12)),
            _fixture_attempt("attempt-2", with_metric(10**400)),
        )
    )[0]
    summary = group.metrics[0]
    assert (summary.values.count, summary.values.unknown, summary.values.mean) == (1, 1, 12)


def test_distribution_guards_stdev_overflow_on_large_finite_floats() -> None:
    result = distribution([1.7e308, -1.7e308, 1.7e308])
    assert result.count == 3
    assert result.stdev is None


def test_group_metrics_restricts_new_summaries_to_the_latest_retained_attempt_per_trial() -> None:
    tokens = TokenUsage(provenance="fixture")

    def with_check(outcome: str, accepted_turns: int) -> Assessment:
        return _fixture_assessment(tokens).model_copy(
            update={
                "help_needed": HelpNeeded(
                    simulator_replies=1,
                    facts_disclosed=(),
                    proactive_facts=(),
                    on_request_facts=(),
                ),
                "checks": (CheckResult(check_id="right-view", outcome=outcome),),
                "accepted_turns": accepted_turns,
            }
        )

    first_attempt = AttemptReport(
        attempt_id="attempt-1",
        trial_id="trial-1",
        variant="base",
        stage="finished",
        assessment_id=None,
        result=with_check("fail", 5),
        raw_evidence="unavailable",
    )
    retry_attempt = AttemptReport(
        attempt_id="attempt-2",
        trial_id="trial-1",
        variant="base",
        stage="finished",
        assessment_id=None,
        result=with_check("pass", 7),
        raw_evidence="unavailable",
    )
    group = group_metrics((first_attempt, retry_attempt))[0]
    assert group.checks[0].passes == 1
    assert group.checks[0].fails == 0
    assert group.checks[0].agreement is None
    assert group.simulator_replies is not None
    assert group.simulator_replies.count == 1
    assert group.accepted_turns.count == 2
    assert group.accepted_turns.mean == 6


def _empty_distribution() -> Distribution:
    return Distribution(
        count=0, unknown=0, minimum=None, maximum=None, mean=None, median=None, stdev=None
    )


def test_group_metrics_serializer_drops_empty_criteria_checks_metrics_and_simulator_replies() -> (
    None
):
    group = GroupMetrics(
        variant="base",
        mode="offline-fixture",
        scheduled_trials=None,
        attempts=0,
        completed=0,
        scored=0,
        eligible=0,
        audit_excluded=0,
        exclusion_reasons={},
        eligible_passes=0,
        eligible_completion_rate=None,
        subjects_reporting_completion=0,
        durations=_empty_distribution(),
        input_tokens=_empty_distribution(),
        output_tokens=_empty_distribution(),
        accepted_turns=_empty_distribution(),
        known_spend_by_currency={},
        unknown_cost_records=0,
        partial_cost_attempts=0,
        costs=_empty_distribution(),
        criteria=(),
        checks=(),
        metrics=(),
        simulator_replies=None,
    )
    parsed = json.loads(canonical_json(group))
    assert not {"criteria", "checks", "metrics", "simulator_replies"} & set(parsed)


def test_legacy_shaped_portable_report_loads_with_new_group_fields_defaulted(store) -> None:
    from dryheave.models import Manifest, ObjectKind

    legacy_group = {
        "variant": "base",
        "mode": "offline-fixture",
        "scheduled_trials": None,
        "attempts": 0,
        "completed": 0,
        "scored": 0,
        "eligible": 0,
        "audit_excluded": 0,
        "exclusion_reasons": {},
        "eligible_passes": 0,
        "eligible_completion_rate": None,
        "subjects_reporting_completion": 0,
        "durations": json.loads(canonical_json(_empty_distribution())),
        "input_tokens": json.loads(canonical_json(_empty_distribution())),
        "output_tokens": json.loads(canonical_json(_empty_distribution())),
        "accepted_turns": json.loads(canonical_json(_empty_distribution())),
        "known_spend_by_currency": {},
        "unknown_cost_records": 0,
        "partial_cost_attempts": 0,
        "costs": json.loads(canonical_json(_empty_distribution())),
    }
    legacy_report = {
        "schema_version": 1,
        "kind": "run-report",
        "run_id": "a" * 32,
        "experiment_id": "a" * 64,
        "compatibility_id": None,
        "durable_sequence": 0,
        "scheduled_trials": None,
        "unstarted_trials": [],
        "input_error": None,
        "attempts": [],
        "groups": [legacy_group],
        "portable": True,
        "omissions": [],
    }
    manifest = Manifest(kind=ObjectKind.PORTABLE_REPORT, payload=legacy_report)
    identifier = store.put_manifest(manifest, files={})
    report = report_run(store, identifier)
    group = report.groups[0]
    assert (group.criteria, group.checks, group.metrics, group.simulator_replies) == (
        (),
        (),
        (),
        None,
    )
    assert (group.cache_read_tokens, group.cache_write_tokens, group.total_input_tokens) == (
        None,
        None,
        None,
    )
    assert group.cost_by_role == {}
