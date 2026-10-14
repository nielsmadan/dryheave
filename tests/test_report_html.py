import re
from datetime import UTC, datetime

from dryheave.cases import CaseBudgets
from dryheave.models import CommandSpec, TokenUsage
from dryheave.report_html import DASH, render_report_html
from dryheave.report_models import AttemptReport, ModelSetup, RunReport, RunSetup
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
    WorkflowOutcome,
)
from dryheave.runner_models import WorkflowEvent
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
    html = render_report_html([(_build_report(), None)], {CASE_ID: _case_view()})
    assert "<script" not in html
    for match in re.finditer(r'(?:src|href)="([^"]*)"', html):
        assert match.group(1).startswith("#") or match.group(1) == ""


def test_render_report_html_shows_backend_derived_help_needed_and_eligibility() -> None:
    html = render_report_html([(_build_report(), None)], {CASE_ID: _case_view()})
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
    html = render_report_html(
        [(report, None)], {CASE_ID: case}, reasoning={report.run_id: {hostile: reasoning_view}}
    )
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
    html = render_report_html([(report, None)], {CASE_ID: _case_view()})
    results = html[html.index("<h2>Results</h2>") :]
    results_thead = re.search(r"<thead>.*?</thead>", results, flags=re.DOTALL)
    assert results_thead is not None
    assert "base" in results_thead.group(0)
    assert "treatment" in results_thead.group(0)
    assert "right-view" in html
    assert html.count("pass 1") >= 2


def test_render_report_html_includes_reasoning_only_when_provided() -> None:
    report = _build_report()
    html_without = render_report_html([(report, None)], {CASE_ID: _case_view()})
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
    html_with = render_report_html(
        [(report, None)], {CASE_ID: _case_view()}, reasoning={report.run_id: reasoning}
    )
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
    html = render_report_html([(report, None)], {CASE_ID: _case_view()})
    assert html.index("<h2>Summary</h2>") < html.index("<h2>Report</h2>")
    assert '<div class="scroller"><table class="topline">' in html
    assert (
        "<th>Variant / attempt</th><th>Turns</th><th>First check</th><th>Fix requests</th>"
        "<th>Workflow</th><th>Time</th><th>Tokens</th><th>Cost</th>"
        "<th>Final right view</th><th>Branch moves up</th><th>Recovered after collapse</th>"
    ) in html
    turns = '<td class="number">2</td>'
    no_workflow = "<td>\u2013</td><td>\u2013</td><td>\u2013</td>"
    resources = (
        '<td class="number">4m 30s</td><td class="number">1.55M</td><td class="number">$0.91</td>'
    )
    assert (
        '<tr class="variant"><td class="label">base · mean of 1</td>'
        + turns
        + no_workflow
        + resources
        + '<td class="check pass">1/1</td><td class="check fail">0/1</td><td>\u2013</td></tr>'
    ) in html
    assert (
        '<tr><td class="attempt" title="a-4ff975651a324a13893fb9edf14b4bef">a-4ff97565</td>'
        + turns
        + no_workflow
        + resources
        + '<td class="check pass">✓</td><td class="check fail">✗</td>'
        '<td class="check not_applicable">\u2013</td></tr>'
    ) in html


def test_render_report_html_renders_workflow_topline_cells_and_the_per_attempt_card() -> None:
    workflow = WorkflowOutcome(
        steps=2,
        completed_steps=2,
        inspections=1,
        fix_requests=1,
        dialogs_answered=0,
        first_inspection="passed",
        final_inspection="passed",
        inspection_seconds=5.0,
        events=(
            WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=1),
            WorkflowEvent(
                step_id="check",
                kind="inspect",
                outcome="failed",
                turn=1,
                failed_criteria=("greeting",),
                seconds=2.0,
            ),
            WorkflowEvent(step_id="check", kind="inspect", outcome="fix_requested", turn=1),
            WorkflowEvent(step_id="check", kind="inspect", outcome="passed", turn=2, seconds=3.0),
        ),
    )
    result = _assessment(
        attempt_id="attempt-workflow",
        accepted_turns=2,
        subject_seconds=20.0,
        workflow=workflow,
    )
    attempts = (_attempt("attempt-workflow", result),)
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
    html = render_report_html([(report, None)], {CASE_ID: _case_view()})
    assert '<td class="check pass">✓</td><td class="number">1</td><td>2/2</td>' in html
    assert '<td class="number">15s</td>' in html
    assert (
        '<td class="check pass">1/1</td><td class="number">1.00</td><td class="check pass">1/1</td>'
    ) in html
    assert "Workflow events in order." in html
    assert "<td>greeting</td>" in html
    assert "<td>fix_requested</td>" in html


def _variant_report(run_id: str, attempt_id: str, variant: str = "sonnet-low") -> RunReport:
    attempts = (_variant_attempt(attempt_id, variant),)
    return with_derived(
        RunReport(
            run_id=run_id,
            experiment_id="a" * 64,
            durable_sequence=1,
            scheduled_trials=1,
            unstarted_trials=(),
            input_error=None,
            attempts=attempts,
            groups=group_metrics(attempts),
        )
    )


def _configured(model: str | None, effort: str | None = None, identity: str = "x") -> ModelSetup:
    return ModelSetup(
        status="configured", kind="claude", model=model, effort=effort, identity=identity
    )


def _setup(run_id: str, variant_model: str, simulator: ModelSetup, **fields: object) -> RunSetup:
    return RunSetup.model_validate(
        {
            "run_id": run_id,
            "case_ids": (CASE_ID,),
            "variants": {"sonnet-low": _configured(variant_model, "low", "p" + run_id)},
            "simulator": simulator,
            "judge": _configured("claude-opus-5-5", "high", "judge"),
            "inspector": ModelSetup(status="none"),
            **fields,
        }
    )


OLD_RUN, NEW_RUN = "1" * 32, "2" * 32


def test_render_report_html_puts_several_runs_in_one_summary_labelled_by_model() -> None:
    entries = [
        (
            _variant_report(OLD_RUN, "a-old"),
            _setup(
                OLD_RUN,
                "claude-sonnet-4-6",
                _configured("claude-sonnet-4-6", "low", "sim-old"),
                price_versions=("anthropic-2026-10-07",),
            ),
        ),
        (
            _variant_report(NEW_RUN, "a-new"),
            _setup(
                NEW_RUN,
                "claude-sonnet-5-5",
                _configured("claude-sonnet-5-5", "low", "sim-new"),
                price_versions=("anthropic-2026-10-14",),
            ),
        ),
    ]
    html = render_report_html(entries, {CASE_ID: _case_view()})
    summary = html[html.index("<h2>Summary</h2>") : html.index("<h2>Runs</h2>")]
    assert summary.count('<table class="topline">') == 1
    assert "<th>Run</th><th>Variant / attempt</th>" in summary
    assert summary.index('<td class="label">sonnet-low · sonnet-4-6@low · mean of 1</td>') < (
        summary.index('<td class="label">sonnet-low · sonnet-5-5@low · mean of 1</td>')
    )
    assert f'<td class="run" title="{OLD_RUN}">11111111</td>' in summary
    runs = html[html.index("<h2>Runs</h2>") :]
    assert '<td class="differs">sonnet-4-6@low</td>' in runs
    assert '<td class="differs">sonnet-5-5@low</td>' in runs
    assert "<td>opus-5-5@high</td>" in runs
    assert "<td>sonnet-low (sonnet-5-5@low)</td>" in runs
    assert "<td>anthropic-2026-10-14</td>" in runs
    assert "Highlighted settings differ between runs (simulated user)" in runs
    assert f'<h2 class="run">Run {OLD_RUN}</h2>' in html
    assert "<title>dryheave report 11111111, 22222222</title>" in html


def test_render_report_html_says_runs_match_only_when_every_compared_setting_is_known() -> None:
    simulator = _configured("claude-sonnet-4-6", "low", "sim")
    same = render_report_html(
        [
            (_variant_report(OLD_RUN, "a-old"), _setup(OLD_RUN, "claude-sonnet-4-6", simulator)),
            (_variant_report(NEW_RUN, "a-new"), _setup(NEW_RUN, "claude-sonnet-5-5", simulator)),
        ],
        {},
    )
    assert "Every run used the same case, simulated user, judge and inspector." in same
    assert f"<td>{DASH}</td>" in same

    unavailable = render_report_html(
        [
            (
                _variant_report(OLD_RUN, "a-old"),
                RunSetup(run_id=OLD_RUN, issues=("Experiment a: missing",)),
            ),
            (
                _variant_report(NEW_RUN, "a-new"),
                RunSetup(run_id=NEW_RUN, issues=("Experiment b: missing",)),
            ),
        ],
        {},
    )
    assert (
        "Could not compare the case, simulated user, judge and inspector: some runs&#x27; "
        "frozen settings are unavailable."
    ) in unavailable
    assert "Every run used the same" not in unavailable
    assert f"<li>Run {OLD_RUN}: Experiment a: missing</li>" in unavailable


def test_render_report_html_names_roles_without_a_model() -> None:
    scripted = ModelSetup(status="configured", kind="scripted", identity="s")
    setups = [
        _setup(OLD_RUN, "claude-sonnet-4-6", scripted),
        _setup(NEW_RUN, "claude-sonnet-5-5", scripted).model_copy(
            update={"variants": {"sonnet-low": _configured(None, identity="profile")}}
        ),
    ]
    html = render_report_html(
        [
            (_variant_report(OLD_RUN, "a-old"), setups[0]),
            (_variant_report(NEW_RUN, "a-new"), setups[1]),
        ],
        {},
    )
    assert "<td>scripted</td>" in html
    assert "<td>sonnet-low (default model)</td>" in html


def test_render_report_html_escapes_hostile_run_settings() -> None:
    hostile = "<img src=x onerror=alert(1)>"
    setup = _setup(
        NEW_RUN,
        hostile,
        _configured(hostile, hostile),
        price_versions=(hostile,),
        issues=(hostile,),
    )
    html = render_report_html(
        [
            (_variant_report(OLD_RUN, "a-old"), _setup(OLD_RUN, "m", _configured("m"))),
            (_variant_report(NEW_RUN, "a-new"), setup),
        ],
        {},
    )
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def _judged(attempt_id: str, rationale: str) -> JudgeReasoningView:
    return JudgeReasoningView(
        attempt_id=attempt_id,
        assessment_id="d" * 64,
        status="ok",
        criteria=(CriterionReasoning(criterion_id="code-quality", rationale=rationale),),
        checks=(),
    )


def test_render_report_html_shows_each_runs_own_reasoning_for_a_shared_attempt_id() -> None:
    simulator = _configured("m", identity="sim")
    html = render_report_html(
        [
            (_variant_report(OLD_RUN, "a-shared"), _setup(OLD_RUN, "m", simulator)),
            (_variant_report(NEW_RUN, "a-shared"), _setup(NEW_RUN, "m", simulator)),
        ],
        {},
        reasoning={
            OLD_RUN: {"a-shared": _judged("a-shared", "before the regrade")},
            NEW_RUN: {"a-shared": _judged("a-shared", "after the regrade")},
        },
    )
    old_section = html[html.index(f"Run {OLD_RUN}</h2>") : html.index(f"Run {NEW_RUN}</h2>")]
    new_section = html[html.index(f"Run {NEW_RUN}</h2>") :]
    assert "before the regrade" in old_section
    assert "after the regrade" in new_section
