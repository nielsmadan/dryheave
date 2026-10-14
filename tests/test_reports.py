import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dryheave.assessments import assess_run
from dryheave.controller_models import ControllerRecipe, RoleBudget
from dryheave.errors import DryheaveError
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment, load_experiment
from dryheave.models import CommandSpec, TokenUsage
from dryheave.report_models import (
    AttemptReport,
    Distribution,
    GroupMetrics,
    ModelSetup,
    PriceBasis,
    RunReport,
)
from dryheave.reports import compare_runs, distribution, group_metrics, report_run, run_setups
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
from dryheave.serialization import canonical_json, digest


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


def _graded_items(outcome: str, value: int, graded_case_id: str | None) -> Assessment:
    return _fixture_assessment(TokenUsage(provenance="fixture")).model_copy(
        update={
            "graded_case_id": graded_case_id,
            "criteria": (
                CriterionResult(
                    criterion_id="quality", kind="judge", required=False, outcome=outcome
                ),
            ),
            "checks": (CheckResult(check_id="builds", outcome=outcome),),
            "metrics": (
                MetricsResult(
                    metrics_id="coverage",
                    outcome="completed",
                    values=(MetricValue(metric_id="tests", label="Tests", value=value),),
                ),
            ),
        }
    )


def test_group_metrics_merges_a_regrade_against_the_reported_case_with_unregraded_attempts() -> (
    None
):
    reported_case = "a" * 64
    group = group_metrics(
        (
            _fixture_attempt("attempt-1", _graded_items("pass", 4, reported_case)),
            _fixture_attempt("attempt-2", _graded_items("fail", 2, None)),
        )
    )[0]
    assert [(item.case_id, item.passes, item.graded) for item in group.criteria] == [
        (reported_case, 1, 2)
    ]
    assert [(item.case_id, item.passes, item.fails) for item in group.checks] == [
        (reported_case, 1, 1)
    ]
    assert [(item.case_id, item.values.count, item.values.mean) for item in group.metrics] == [
        (reported_case, 2, 3)
    ]


def test_group_metrics_keys_a_regrade_against_a_different_case_under_its_graded_case() -> None:
    original_case, regraded_case = "a" * 64, "c" * 64
    group = group_metrics(
        (
            _fixture_attempt("attempt-1", _graded_items("pass", 4, regraded_case)),
            _fixture_attempt("attempt-2", _graded_items("fail", 2, None)),
        )
    )[0]
    assert sorted((item.case_id, item.passes, item.graded) for item in group.criteria) == [
        (original_case, 0, 1),
        (regraded_case, 1, 1),
    ]
    assert sorted((item.case_id, item.passes, item.fails) for item in group.checks) == [
        (original_case, 0, 1),
        (regraded_case, 1, 0),
    ]
    assert sorted((item.case_id, item.values.mean) for item in group.metrics) == [
        (original_case, 2),
        (regraded_case, 4),
    ]


@pytest.mark.parametrize(
    ("before_graded", "after_graded", "paired"),
    [
        (None, None, 1),
        (None, "a" * 64, 1),
        ("c" * 64, "c" * 64, 1),
        (None, "c" * 64, 0),
        ("c" * 64, "d" * 64, 0),
    ],
)
def test_compare_pairs_attempts_only_when_graded_against_the_same_case(
    store, monkeypatch, before_graded, after_graded, paired
) -> None:
    from dryheave import reports
    from dryheave.report_models import RunReport

    def run(run_id: str, graded_case_id: str | None) -> RunReport:
        result = _graded_items("pass", 4, graded_case_id).model_copy(
            update={"run_id": run_id, "repetition": 1}
        )
        attempt = _fixture_attempt("attempt-1", result).model_copy(update={"pairing_id": "e" * 64})
        return RunReport(
            run_id=run_id,
            experiment_id="a" * 64,
            compatibility_id="f" * 64,
            durable_sequence=1,
            scheduled_trials=1,
            unstarted_trials=(),
            input_error=None,
            attempts=(attempt,),
            groups=(),
        )

    reports_by_id = {"1" * 32: run("1" * 32, before_graded), "2" * 32: run("2" * 32, after_graded)}
    monkeypatch.setattr(reports, "report_run", lambda _store, run_id, **_: reports_by_id[run_id])
    comparison = compare_runs(store, "1" * 32, "2" * 32)
    assert comparison.paired_count == paired


def test_report_derives_group_price_basis_help_needed_and_eligibility() -> None:
    from dryheave.report_models import FactDisclosure, HelpNeededSummary, PriceBasis, RunReport
    from dryheave.reports import with_derived

    def charge(kind: str, currency: str | None, version: str | None) -> UsageCharge:
        return UsageCharge(
            identity="charge",
            usage=None,
            model_basis="observed",
            cost=0.1,
            cost_kind=kind,
            currency=currency,
            price_version=version,
            price_date="2026-10-01" if version else None,
            raw_evidence_sha256="f" * 64,
            reason="fixture",
        )

    tokens = TokenUsage(provenance="fixture")

    def assessed(
        attempt_id: str, trial_id: str, facts: tuple[str, ...] | None, **update: object
    ) -> AttemptReport:
        role = _fixture_role("subject", tokens).model_copy(
            update={"records": (charge("estimated", "USD", "v1"), charge("unknown", None, None))}
        )
        result = _fixture_assessment(tokens).model_copy(
            update={
                "attempt_id": attempt_id,
                "trial_id": trial_id,
                "roles": (
                    role,
                    _fixture_role("simulator", tokens),
                    _fixture_role("judge", tokens),
                ),
                "help_needed": None
                if facts is None
                else HelpNeeded(
                    simulator_replies=1,
                    facts_disclosed=facts,
                    proactive_facts=("budget",),
                    on_request_facts=("deadline",),
                ),
                **update,
            }
        )
        return _fixture_attempt(attempt_id, result).model_copy(update={"trial_id": trial_id})

    attempts = (
        assessed("attempt-1", "trial-1", ("budget",), eligible=False, exclusion_reasons=("x",)),
        assessed("attempt-2", "trial-1", ("budget", "deadline", "other")),
        assessed("attempt-3", "trial-2", ("budget",)),
        assessed("attempt-4", "trial-3", None),
        assessed("attempt-5", "trial-4", None, eligible=False, phase="audited"),
        _fixture_attempt("attempt-6", None).model_copy(update={"trial_id": "trial-5"}),
        _fixture_attempt("attempt-7", _fixture_assessment(tokens)).model_copy(
            update={"variant": "other", "current_exclusions": ("current_inputs_invalid",)}
        ),
    )
    report = with_derived(
        RunReport(
            run_id="a" * 32,
            experiment_id="a" * 64,
            durable_sequence=1,
            scheduled_trials=6,
            unstarted_trials=(),
            input_error=None,
            attempts=attempts,
            groups=group_metrics(attempts),
        )
    )
    assert [item.eligibility for item in report.attempts] == [
        "excluded",
        "eligible",
        "eligible",
        "eligible",
        "ungraded",
        "unassessed",
        "excluded",
    ]
    by_variant = {(group.variant, group.mode): group for group in report.groups}
    base = by_variant[("base", "offline-fixture")]
    assert sorted(by_variant) == [
        ("base", "offline-fixture"),
        ("base", "pending"),
        ("other", "offline-fixture"),
    ]
    assert base.price_basis == PriceBasis(
        kinds=(("estimated", 5), ("unknown", 5)),
        bases=(("observed", 10),),
        currencies=("USD",),
        versions=("v1",),
        dates=("2026-10-01",),
    )
    assert base.help_needed == HelpNeededSummary(
        measured=2,
        unmeasured=1,
        facts=(
            FactDisclosure(fact_id="budget", kind="proactive", count=2),
            FactDisclosure(fact_id="deadline", kind="on request", count=1),
            FactDisclosure(fact_id="other", kind="unknown", count=1),
        ),
    )
    assert by_variant[("other", "offline-fixture")].help_needed is None


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


def _topline_attempt(
    attempt_id: str,
    trial_id: str,
    *,
    cost: float,
    seconds: float,
    checks: tuple[tuple[str, str], ...],
) -> AttemptReport:
    tokens = TokenUsage(
        uncached_input=100, cache_read=1000, cache_write=200, output=50, provenance="fixture"
    )
    subject = _fixture_role("subject", tokens).model_copy(
        update={"known_cost": cost, "currency": "USD"}
    )
    judge = _fixture_role("judge", TokenUsage(output=9999, provenance="fixture")).model_copy(
        update={"known_cost": 5.0, "currency": "USD"}
    )
    result = _fixture_assessment(TokenUsage(provenance="fixture")).model_copy(
        update={
            "attempt_id": attempt_id,
            "trial_id": trial_id,
            "accepted_turns": 3,
            "subject_seconds": seconds,
            "roles": (subject, _fixture_role("simulator", tokens), judge),
            "checks": tuple(
                CheckResult(
                    check_id=check_id,
                    outcome=outcome,
                    answer="yes" if outcome == "pass" else None,
                )
                for check_id, outcome in checks
            ),
        }
    )
    return _fixture_attempt(attempt_id, result).model_copy(update={"trial_id": trial_id})


def test_report_derives_agent_topline_per_attempt_and_variant_from_latest_attempts() -> None:
    from dryheave.report_models import AttemptTopline, GroupTopline, RunReport, ToplineCheck
    from dryheave.reports import with_derived

    attempts = (
        _topline_attempt(
            "attempt-1", "trial-1", cost=9.0, seconds=999.0, checks=(("right-view", "error"),)
        ),
        _topline_attempt(
            "attempt-2",
            "trial-1",
            cost=1.0,
            seconds=100.0,
            checks=(("right-view", "pass"), ("stable", "fail")),
        ),
        _topline_attempt(
            "attempt-3",
            "trial-2",
            cost=2.0,
            seconds=300.0,
            checks=(("right-view", "fail"), ("stable", "not_applicable")),
        ),
    )
    report = with_derived(
        RunReport(
            run_id="a" * 32,
            experiment_id="a" * 64,
            durable_sequence=1,
            scheduled_trials=2,
            unstarted_trials=(),
            input_error=None,
            attempts=attempts,
            groups=group_metrics(attempts),
        )
    )
    assert [item.topline.latest for item in report.attempts if item.topline] == [
        False,
        True,
        True,
    ]
    assert report.attempts[1].topline == AttemptTopline(
        latest=True,
        turns=3,
        seconds=100.0,
        tokens=1350,
        cost=1.0,
        currency="USD",
        checks=attempts[1].result.checks if attempts[1].result else (),
    )
    assert report.groups[0].topline == GroupTopline(
        attempts=2,
        turns=3.0,
        seconds=200.0,
        tokens=1350.0,
        cost=1.5,
        currency="USD",
        checks=(
            ToplineCheck(check_id="right-view", passes=1, answered=2),
            ToplineCheck(check_id="stable", passes=0, answered=1),
        ),
    )


def test_attempt_topline_leaves_tokens_unknown_when_a_category_is_unobserved() -> None:
    from dryheave.reports import attempt_topline

    item = _topline_attempt("attempt-1", "trial-1", cost=1.0, seconds=1.0, checks=())
    assert item.result is not None
    partial = item.model_copy(
        update={
            "result": item.result.model_copy(
                update={
                    "roles": (
                        _fixture_role(
                            "subject", TokenUsage(uncached_input=5, output=5, provenance="x")
                        ),
                        *item.result.roles[1:],
                    )
                }
            )
        }
    )
    topline = attempt_topline(partial, latest=True)
    assert (topline.tokens, topline.cost, topline.currency) == (None, None, None)


def _workflow_outcome(**overrides: object):
    from dryheave.result_models import WorkflowOutcome

    base = {
        "steps": 0,
        "completed_steps": 0,
        "inspections": 0,
        "fix_requests": 0,
        "dialogs_answered": 0,
        "first_inspection": "none",
        "final_inspection": "none",
        "inspection_seconds": 0.0,
    }
    base.update(overrides)
    return WorkflowOutcome(**base)


def test_attempt_topline_subtracts_inspection_seconds_and_carries_workflow_fields() -> None:
    from dryheave.reports import attempt_topline

    item = _topline_attempt("attempt-1", "trial-1", cost=1.0, seconds=100.0, checks=())
    assert item.result is not None
    outcome = _workflow_outcome(
        steps=2,
        completed_steps=2,
        inspections=1,
        fix_requests=1,
        first_inspection="passed",
        final_inspection="passed",
        inspection_seconds=15.0,
    )
    with_workflow = item.model_copy(
        update={"result": item.result.model_copy(update={"workflow": outcome})}
    )
    topline = attempt_topline(with_workflow, latest=True)
    assert topline.seconds == 85.0
    assert (topline.first_inspection, topline.fix_requests, topline.workflow) == (
        "passed",
        1,
        "2/2",
    )


def test_group_topline_aggregates_first_check_fix_requests_and_workflow_completion() -> None:
    from dryheave.reports import attempt_topline, group_topline

    passed = _workflow_outcome(
        steps=2,
        completed_steps=2,
        inspections=1,
        first_inspection="passed",
        final_inspection="passed",
    )
    failed = _workflow_outcome(
        steps=2,
        completed_steps=0,
        inspections=1,
        fix_requests=2,
        first_inspection="failed",
        final_inspection="failed",
    )
    attempt_a = _topline_attempt("attempt-a", "trial-a", cost=1.0, seconds=10.0, checks=())
    attempt_b = _topline_attempt("attempt-b", "trial-b", cost=1.0, seconds=10.0, checks=())
    attempt_c = _topline_attempt("attempt-c", "trial-c", cost=1.0, seconds=10.0, checks=())
    assert attempt_a.result and attempt_b.result and attempt_c.result
    with_a = attempt_a.model_copy(
        update={"result": attempt_a.result.model_copy(update={"workflow": passed})}
    )
    with_b = attempt_b.model_copy(
        update={"result": attempt_b.result.model_copy(update={"workflow": failed})}
    )
    toplines = [
        attempt_topline(with_a, latest=True),
        attempt_topline(with_b, latest=True),
        attempt_topline(attempt_c, latest=True),
    ]
    group = group_topline(toplines)
    assert group.first_inspection_passes == 1
    assert group.first_inspection_attempts == 2
    assert group.fix_requests == pytest.approx(1.0)
    assert group.workflows_completed == 1
    assert group.workflow_attempts == 2


def test_attempt_topline_hides_workflow_and_fix_requests_for_dialogs_without_a_workflow() -> None:
    from dryheave.reports import attempt_topline, group_topline

    dialog_only = _workflow_outcome(steps=0, completed_steps=0, dialogs_answered=1)
    attempt_a = _topline_attempt("attempt-a", "trial-a", cost=1.0, seconds=10.0, checks=())
    assert attempt_a.result is not None
    with_dialog = attempt_a.model_copy(
        update={"result": attempt_a.result.model_copy(update={"workflow": dialog_only})}
    )
    topline = attempt_topline(with_dialog, latest=True)
    assert (topline.first_inspection, topline.fix_requests, topline.workflow) == (None, None, None)
    group = group_topline([topline])
    assert (
        group.first_inspection_attempts,
        group.workflow_attempts,
        group.workflows_completed,
    ) == (0, 0, 0)


JUDGE = ControllerRecipe(
    kind="json-command",
    isolation="trusted-native",
    model="claude-opus-5-5",
    effort="high",
    command=CommandSpec(argv=(sys.executable, "judge.py")),
)


def _bare_report(experiment_id: str, run_id: str = "a" * 32, versions=()) -> RunReport:
    return RunReport.model_construct(
        run_id=run_id,
        experiment_id=experiment_id,
        groups=(GroupMetrics.model_construct(price_basis=PriceBasis(versions=versions)),),
    )


def test_run_setups_read_the_frozen_case_roles_and_variant_profiles(store, benchmark):
    identifier = create_experiment(
        store, benchmark.model_copy(update={"scoring": ScoringConfig(judge=JUDGE)})
    )
    experiment = load_experiment(store, identifier)
    (setup,) = run_setups(store, [_bare_report(identifier, versions=("v1",))])
    assert setup.case_ids == experiment.case_ids
    assert setup.variants["base"].status == "configured"
    assert setup.variants["base"].identity == experiment.variants[0].profile_id
    assert (setup.simulator.kind, setup.simulator.identity) == ("scripted", experiment.simulator_id)
    judge = ModelSetup(
        status="configured",
        kind="json-command",
        model="claude-opus-5-5",
        effort="high",
        identity=digest(canonical_json(JUDGE)),
    )
    assert (setup.judge, setup.inspector) == (judge, judge)
    assert setup.price_versions == ("v1",)
    assert setup.issues == ()


def test_run_setups_show_no_inspector_when_its_budget_disables_inspection(store, benchmark):
    disabled = JUDGE.model_copy(update={"budget": RoleBudget(max_calls=0)})
    identifier = create_experiment(
        store, benchmark.model_copy(update={"scoring": ScoringConfig(judge=disabled)})
    )
    (setup,) = run_setups(store, [_bare_report(identifier)])
    assert setup.judge.status == "configured"
    assert setup.inspector == ModelSetup(status="none")


def test_run_setups_mark_a_missing_experiment_unavailable_with_its_reason(store):
    (setup,) = run_setups(store, [_bare_report("f" * 64)])
    assert setup.case_ids is None
    assert setup.simulator.status == "unavailable"
    assert setup.issues[0].startswith("Experiment " + "f" * 64)


def test_run_setups_record_an_unloadable_profile_as_an_issue(store, benchmark, monkeypatch):
    identifier = create_experiment(store, benchmark)
    profile_id = load_experiment(store, identifier).variants[0].profile_id

    def unloadable(_store, _reference):
        raise DryheaveError("profile corrupt")

    monkeypatch.setattr("dryheave.reports.load_profile", unloadable)
    (setup,) = run_setups(store, [_bare_report(identifier)])
    assert setup.variants["base"] == ModelSetup(status="unavailable", identity=profile_id)
    assert setup.issues == (f"Profile {profile_id}: profile corrupt",)


def test_run_setups_load_a_shared_experiment_once(store, benchmark, monkeypatch):
    identifier = create_experiment(store, benchmark)
    from dryheave import reports

    inspected = []
    original = reports.inspect_experiment

    def counting(store_, reference):
        inspected.append(reference)
        return original(store_, reference)

    monkeypatch.setattr(reports, "inspect_experiment", counting)
    setups = run_setups(
        store, [_bare_report(identifier, "1" * 32), _bare_report(identifier, "2" * 32)]
    )
    assert inspected == [identifier]
    assert [setup.run_id for setup in setups] == ["1" * 32, "2" * 32]
