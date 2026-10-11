import re
from datetime import UTC, datetime

from dryheave.cases import CaseBudgets
from dryheave.models import CommandSpec, TokenUsage
from dryheave.report_html import render_report_html
from dryheave.report_models import AttemptReport, RunReport
from dryheave.reports import group_metrics, with_derived
from dryheave.result_models import (
    Assessment,
    AuditReport,
    CheckResult,
    CriterionResult,
    HelpNeeded,
    MetricsResult,
    MetricValue,
    RoleMetrics,
)
from dryheave.viewer_models import (
    CaseCheckView,
    CaseDeterministicView,
    CaseFactView,
    CaseMetricsView,
    CaseRubricView,
    CaseView,
    CheckReasoning,
    CriterionReasoning,
    JudgeReasoningView,
)

CASE_ID = "a" * 64


def _role(name: str, tokens: TokenUsage) -> RoleMetrics:
    return RoleMetrics(
        role=name,
        records=(),
        known_tokens=tokens,
        known_cost=None,
        currency=None,
        coverage="unknown",
        reasons=("fixture",),
    )


def _assessment(**overrides: object) -> Assessment:
    tokens = TokenUsage(provenance="fixture")
    audit = AuditReport(
        input_integrity="verified",
        capture_integrity="verified",
        cleanup="stopped",
        capture_complete=True,
        coverage=(),
    )
    base = Assessment(
        run_id="a" * 32,
        experiment_id="a" * 64,
        trial_id="trial-1",
        attempt_id="attempt-1",
        capture_id=None,
        case_id=CASE_ID,
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
        roles=(_role("subject", tokens), _role("simulator", tokens), _role("judge", tokens)),
    )
    return base.model_copy(update=overrides)


def _attempt(attempt_id: str, result: Assessment) -> AttemptReport:
    return AttemptReport(
        attempt_id=attempt_id,
        trial_id=attempt_id,
        variant="base",
        stage="finished",
        assessment_id=None,
        result=result,
        raw_evidence="unavailable",
    )


def _case_view(title: str = "Case title") -> CaseView:
    return CaseView(
        case_id=CASE_ID,
        title=title,
        initial_prompt="Do the thing.",
        facts=(CaseFactView(fact_id="fact-1", text="A fact.", disclosure="proactive"),),
        rubrics=(
            CaseRubricView(
                criterion_id="code-quality", required=False, rubric="Be good.", origin="default"
            ),
        ),
        disabled_rubrics=(),
        deterministic=(
            CaseDeterministicView(
                criterion_id="builds",
                description="Builds cleanly.",
                required=True,
                command=CommandSpec(argv=("make", "build")),
            ),
        ),
        checks=(
            CaseCheckView(
                check_id="right-view",
                question="Did it touch the right view?",
                expect="yes",
                only_if=None,
            ),
        ),
        metrics=(
            CaseMetricsView(
                metrics_id="coverage",
                description="Runs the test suite.",
                command=CommandSpec(argv=("make", "test")),
            ),
        ),
        budgets=CaseBudgets(),
        tool_requirements=("xcodebuild",),
        repository_id="b" * 64,
        persona_id="c" * 64,
        baseline_commit="deadbeef",
        reference_patch=True,
    )


def _build_report(check_outcome: str = "pass") -> RunReport:
    result = _assessment(
        criteria=(
            CriterionResult(
                criterion_id="code-quality", kind="judge", required=False, outcome="pass", score=0.8
            ),
        ),
        checks=(CheckResult(check_id="right-view", outcome=check_outcome, answer="yes"),),
        metrics=(
            MetricsResult(
                metrics_id="coverage",
                outcome="completed",
                values=(
                    MetricValue(
                        metric_id="tests_passed", label="Tests passed", value=12, unit="tests"
                    ),
                ),
            ),
        ),
        help_needed=HelpNeeded(
            simulator_replies=1,
            facts_disclosed=("fact-1",),
            proactive_facts=("fact-1",),
            on_request_facts=(),
        ),
    )
    attempts = (_attempt("attempt-1", result),)
    groups = group_metrics(attempts)
    return with_derived(
        RunReport(
            run_id="a" * 32,
            experiment_id="a" * 64,
            durable_sequence=1,
            scheduled_trials=1,
            unstarted_trials=(),
            input_error=None,
            attempts=attempts,
            groups=groups,
        )
    )


def test_render_report_html_has_no_script_and_no_external_resource_references() -> None:
    html = render_report_html(_build_report(), {CASE_ID: _case_view()})
    assert "<script" not in html
    for match in re.finditer(r'(?:src|href)="([^"]*)"', html):
        assert match.group(1).startswith("#") or match.group(1) == ""


def test_render_report_html_shows_backend_derived_help_needed_and_eligibility() -> None:
    html = render_report_html(_build_report(), {CASE_ID: _case_view()})
    assert "<td>fact-1 (proactive x1)</td>" in html
    assert "<td>finished</td><td>unknown</td><td>pass</td><td>eligible</td>" in html
    assert "cost kinds none · model basis none · currencies unrecorded" in html


def test_render_report_html_escapes_hostile_values_across_interpolation_paths() -> None:
    hostile = '<img src=x onerror=alert(1)>"hostile"'
    criterion = CriterionResult.model_construct(
        criterion_id=hostile,
        kind="judge",
        required=False,
        outcome="pass",
        execution=None,
        calibration=None,
        score=0.5,
        evidence_hash=None,
        error=None,
    )
    check = CheckResult.model_construct(check_id=hostile, outcome="pass", answer="yes")
    metric = MetricsResult(
        metrics_id="coverage",
        outcome="completed",
        values=(MetricValue(metric_id="tests_passed", label=hostile, value=1),),
    )
    result = _assessment(criteria=(criterion,), checks=(check,), metrics=(metric,))
    attempt = AttemptReport.model_construct(
        attempt_id=hostile,
        trial_id=hostile,
        variant=hostile,
        stage="finished",
        assessment_id=None,
        capture_id=None,
        quarantine_id=None,
        pairing_id=None,
        result=result,
        current_exclusions=(),
        raw_evidence="unavailable",
        mode=None,
        roles=(),
    )
    attempts = (attempt,)
    report = RunReport(
        run_id="a" * 32,
        experiment_id="a" * 64,
        durable_sequence=1,
        scheduled_trials=1,
        unstarted_trials=(),
        input_error=None,
        attempts=attempts,
        groups=(),
    )
    case = _case_view(title=hostile).model_copy(update={"initial_prompt": hostile})
    reasoning_view = JudgeReasoningView.model_construct(
        attempt_id=hostile,
        assessment_id=None,
        status="ok",
        reason=None,
        reason_code=None,
        criteria=(CriterionReasoning.model_construct(criterion_id=hostile, rationale=hostile),),
        checks=(CheckReasoning.model_construct(check_id=hostile, rationale=hostile),),
    )
    html = render_report_html(report, {CASE_ID: case}, {hostile: reasoning_view})
    escaped = "&lt;img src=x onerror=alert(1)&gt;&quot;hostile&quot;"
    assert "<img src=x onerror=alert(1)>" not in html
    assert html.count(escaped) >= 7


def _variant_attempt(attempt_id: str, variant: str) -> AttemptReport:
    result = _assessment(
        attempt_id=attempt_id,
        trial_id=attempt_id,
        variant=variant,
        criteria=(
            CriterionResult(
                criterion_id="code-quality", kind="judge", required=False, outcome="pass"
            ),
        ),
        checks=(CheckResult(check_id="right-view", outcome="pass", answer="yes"),),
    )
    return AttemptReport(
        attempt_id=attempt_id,
        trial_id=attempt_id,
        variant=variant,
        stage="finished",
        assessment_id=None,
        result=result,
        raw_evidence="unavailable",
    )


def test_render_report_html_contains_each_variant_column_and_check_row() -> None:
    attempts = (
        _variant_attempt("attempt-1", "base"),
        _variant_attempt("attempt-2", "treatment"),
    )
    report = with_derived(
        RunReport(
            run_id="a" * 32,
            experiment_id="a" * 64,
            durable_sequence=2,
            scheduled_trials=2,
            unstarted_trials=(),
            input_error=None,
            attempts=attempts,
            groups=group_metrics(attempts),
        )
    )
    html = render_report_html(report, {CASE_ID: _case_view()})
    results = html[html.index("<h2>Results</h2>") :]
    results_thead = re.search(r"<thead>.*?</thead>", results, flags=re.DOTALL)
    assert results_thead is not None
    assert "base" in results_thead.group(0)
    assert "treatment" in results_thead.group(0)
    assert "right-view" in html
    assert html.count("pass 1") >= 2


def test_render_report_html_includes_reasoning_only_when_provided() -> None:
    report = _build_report()
    html_without = render_report_html(report, {CASE_ID: _case_view()})
    assert "reasoning not included" in html_without
    assert "the judge decided this because" not in html_without

    reasoning = {
        "attempt-1": JudgeReasoningView(
            attempt_id="attempt-1",
            assessment_id="d" * 64,
            status="ok",
            criteria=(
                CriterionReasoning(
                    criterion_id="code-quality", rationale="the judge decided this because of X"
                ),
            ),
            checks=(CheckReasoning(check_id="right-view", rationale="checked the transcript"),),
        )
    }
    html_with = render_report_html(report, {CASE_ID: _case_view()}, reasoning)
    assert "the judge decided this because of X" in html_with
    assert "checked the transcript" in html_with


def test_render_report_html_opens_with_an_agent_summary_of_measures_and_checks() -> None:
    subject = _role(
        "subject",
        TokenUsage(
            uncached_input=28,
            cache_read=1_485_530,
            cache_write=53_840,
            output=9_191,
            provenance="fixture",
        ),
    ).model_copy(update={"known_cost": 0.906648, "currency": "USD"})
    result = _assessment(
        attempt_id="a-4ff975651a324a13893fb9edf14b4bef",
        accepted_turns=2,
        subject_seconds=269.5,
        roles=(
            subject,
            _role("simulator", TokenUsage(provenance="x")),
            _role("judge", TokenUsage(provenance="x")),
        ),
        checks=(
            CheckResult(check_id="final-right-view", outcome="pass", answer="yes"),
            CheckResult(check_id="branch-moves-up", outcome="fail", answer="no"),
            CheckResult(check_id="recovered-after-collapse", outcome="not_applicable"),
        ),
    )
    attempts = (_attempt("a-4ff975651a324a13893fb9edf14b4bef", result),)
    report = with_derived(
        RunReport(
            run_id="a" * 32,
            experiment_id="a" * 64,
            durable_sequence=1,
            scheduled_trials=1,
            unstarted_trials=(),
            input_error=None,
            attempts=attempts,
            groups=group_metrics(attempts),
        )
    )
    html = render_report_html(report, {CASE_ID: _case_view()})
    assert html.index("<h2>Summary</h2>") < html.index("<h2>Report</h2>")
    assert (
        "<th>Variant / attempt</th><th>Turns</th><th>Time</th><th>Tokens</th><th>Cost</th>"
        "<th>Final right view</th><th>Branch moves up</th><th>Recovered after collapse</th>"
    ) in html
    measures = (
        '<td class="number">2</td><td class="number">4m 30s</td>'
        '<td class="number">1.55M</td><td class="number">$0.91</td>'
    )
    assert (
        '<tr class="variant"><td>base · mean of 1</td>'
        + measures
        + '<td class="check pass">1/1</td><td class="check fail">0/1</td><td>\u2013</td></tr>'
    ) in html
    assert (
        '<tr><td class="attempt" title="a-4ff975651a324a13893fb9edf14b4bef">a-4ff97565</td>'
        + measures
        + '<td class="check pass">✓</td><td class="check fail">✗</td>'
        '<td class="check not_applicable">\u2013</td></tr>'
    ) in html
