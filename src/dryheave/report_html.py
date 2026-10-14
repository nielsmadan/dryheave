import html as html_lib
from collections.abc import Callable, Mapping, Sequence

from dryheave.report_models import (
    AttemptReport,
    AttemptTopline,
    CheckSummary,
    CriterionSummary,
    Distribution,
    GroupMetrics,
    GroupTopline,
    HelpNeededSummary,
    MetricSummary,
    ModelSetup,
    PriceBasis,
    RunReport,
    RunSetup,
)
from dryheave.result_models import Assessment, CheckResult
from dryheave.viewer_models import CaseView, CheckReasoning, CriterionReasoning, JudgeReasoningView

UNKNOWN = "unknown"
NO_VARIANT = "(no variant)"
RUN_ID_CHARS = 8
ATTEMPT_ID_CHARS = 10
CASE_ID_CHARS = 12
RETAINED = "all retained attempts, retries included"
ELIGIBLE = "eligible attempts only"


def _esc(value: object) -> str:
    return html_lib.escape(str(value), quote=True)


def _fmt_count(value: int | None) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else UNKNOWN


def _fmt_number(value: float | None, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    return f"{value:.{digits}f}"


def _whole(value: float | None) -> str:
    return _fmt_number(value, 0)


def _money(value: float | None) -> str:
    return _fmt_number(value, 4)


def _fmt_seconds(value: float | None) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    return f"{value:.2f} s"


def _fmt_rate(value: float | None) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    return f"{value * 100:.1f}%"


def _fmt_distribution(
    dist: Distribution | None, fmt: Callable[[float | None], str] = _fmt_number
) -> str:
    if dist is None:
        return UNKNOWN
    observed = dist.count
    missing = dist.unknown
    head = "no observed values" if observed == 0 else f"n={observed}"
    tail = "" if missing == 0 else f" · {missing} unknown"
    if observed == 0:
        return f"{head}{tail}"
    body = " · ".join(
        [
            f"min {fmt(dist.minimum)}",
            f"median {fmt(dist.median)}",
            f"max {fmt(dist.maximum)}",
            f"mean {fmt(dist.mean)}",
            f"sd {fmt(dist.stdev)}",
        ]
    )
    return f"{head} · {body}{tail}"


def _fmt_metric_value(value: object) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _table_html(caption: str, headers: Sequence[str], body: str, class_name: str = "") -> str:
    head_cells = "".join(f"<th>{_esc(item)}</th>" for item in headers)
    table_class = f' class="{class_name}"' if class_name else ""
    return (
        f'<div class="scroller"><table{table_class}><caption>{_esc(caption)}</caption>'
        f"<thead><tr>{head_cells}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _table(caption: str, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return _table_html(caption, headers, body)


def _section(title: str, inner: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{inner}</section>"


def _note(text: str) -> str:
    return f'<p class="note">{_esc(text)}</p>'


def _definitions(pairs: Sequence[tuple[str, str]]) -> str:
    items = "".join(f"<dt>{_esc(key)}</dt><dd>{_esc(value)}</dd>" for key, value in pairs)
    return f'<dl class="definitions">{items}</dl>'


def _bullets(items: Sequence[str]) -> str:
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _group_label(group: GroupMetrics) -> str:
    variant = group.variant if group.variant else NO_VARIANT
    return f"{variant} · {group.mode}"


def _describe_price_basis(basis: PriceBasis | None) -> str:
    if basis is None:
        return UNKNOWN

    def listed(values: Sequence[str], empty: str) -> str:
        return ", ".join(values) if values else empty

    kinds = [f"{kind}x{count}" for kind, count in basis.kinds]
    bases = [f"{name}x{count}" for name, count in basis.bases]
    return " · ".join(
        [
            f"cost kinds {listed(kinds, 'none')}",
            f"model basis {listed(bases, 'none')}",
            f"currencies {listed(basis.currencies, 'unrecorded')}",
            f"price tables {listed(basis.versions, 'unrecorded')}",
            f"price dates {listed(basis.dates, 'unrecorded')}",
        ]
    )


def _currency_suffix(group: GroupMetrics, dist: Distribution | None) -> str:
    if dist is None or dist.count == 0 or group.price_basis is None:
        return ""
    currencies = group.price_basis.currencies
    return f" {'/'.join(currencies)}" if currencies else ""


def _facts_disclosed_text(summary: HelpNeededSummary | None) -> str:
    if summary is None:
        return "not measured"
    suffix = "" if summary.unmeasured == 0 else f" ({summary.unmeasured} attempt(s) not measured)"
    if not summary.facts:
        return f"none disclosed{suffix}"
    body = ", ".join(f"{item.fact_id} ({item.kind} x{item.count})" for item in summary.facts)
    return f"{body}{suffix}"


def _union_case_keys(
    groups: Sequence[GroupMetrics], field: str, id_attr: str
) -> list[tuple[str, str]]:
    keys: dict[tuple[str, str], None] = {}
    for group in groups:
        for item in getattr(group, field):
            keys[(item.case_id, getattr(item, id_attr))] = None
    return sorted(keys)


def _find_criterion(
    items: Sequence[CriterionSummary], case_id: str, criterion_id: str
) -> CriterionSummary | None:
    for item in items:
        if item.case_id == case_id and item.criterion_id == criterion_id:
            return item
    return None


def _find_check(items: Sequence[CheckSummary], case_id: str, check_id: str) -> CheckSummary | None:
    for item in items:
        if item.case_id == case_id and item.check_id == check_id:
            return item
    return None


def _find_metric(
    items: Sequence[MetricSummary], case_id: str, metric_id: str
) -> MetricSummary | None:
    for item in items:
        if item.case_id == case_id and item.metric_id == metric_id:
            return item
    return None


DASH = "\u2013"
MILLION = 1_000_000
THOUSAND = 1_000
CHECK_MARKS = {"pass": "✓", "fail": "✗", "not_applicable": DASH, "error": "?"}
INSPECTION_MARKS = {"passed": "✓", "failed": "✗", "unavailable": "?"}
INSPECTION_CLASSES = {"passed": "pass", "failed": "fail", "unavailable": "unavailable"}
TOPLINE_LEGEND = (
    f"✓ the outcome the case wants · ✗ the opposite · {DASH} not applicable · ? no clear answer. "
    "Turns, time, tokens and cost are the agent's alone, without the simulated user and the "
    "judge, and time excludes inspection; tokens include cache reads and writes, and cost is "
    "an API list-price estimate. First check is the workflow's first inspection "
    f"(✓ passed, ✗ failed, ? unavailable, {DASH} no inspection); fix requests count fix-it "
    f"replies; workflow shows completed over declared steps, or {DASH} without a workflow. "
    "Variant rows average the attempts below them; retried attempts are left out."
)


def check_label(check_id: str) -> str:
    return check_id.replace("-", " ").replace("_", " ").capitalize()


def _fmt_duration(value: float | None) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _fmt_tokens(value: float | None) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    if value >= MILLION:
        return f"{value / MILLION:.2f}M"
    if value >= THOUSAND:
        return f"{value / THOUSAND:.1f}k"
    return f"{value:.0f}"


def _fmt_cost(value: float | None, currency: str | None) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    return f"${value:.2f}" if currency == "USD" else f"{value:.2f} {currency or ''}".rstrip()


def _fmt_turns(value: float | None) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return UNKNOWN
    return f"{value:g}" if float(value).is_integer() else f"{value:.1f}"


def _cell(text: str, class_name: str = "") -> str:
    attribute = f' class="{_esc(class_name)}"' if class_name else ""
    return f"<td{attribute}>{_esc(text)}</td>"


def _attempt_check_cell(checks: Sequence[CheckResult], check_id: str) -> str:
    outcome = next((item.outcome for item in checks if item.check_id == check_id), None)
    if outcome is None:
        return _cell("")
    return _cell(CHECK_MARKS[outcome], f"check {outcome}")


def _group_check_cell(topline: GroupTopline, check_id: str) -> str:
    tally = next((item for item in topline.checks if item.check_id == check_id), None)
    if tally is None or tally.answered == 0:
        return _cell(DASH)
    tone = "pass" if tally.passes == tally.answered else "fail" if tally.passes == 0 else ""
    return _cell(f"{tally.passes}/{tally.answered}", f"check {tone}".strip())


def _turns_cell(turns: float | None) -> str:
    return _cell(_fmt_turns(turns), "number")


def _resource_cells(
    seconds: float | None, tokens: float | None, cost: float | None, currency: str | None
) -> str:
    return "".join(
        _cell(text, "number")
        for text in (_fmt_duration(seconds), _fmt_tokens(tokens), _fmt_cost(cost, currency))
    )


def _first_inspection_cell(value: str | None) -> str:
    if value is None or value == "none":
        return _cell(DASH)
    return _cell(INSPECTION_MARKS[value], f"check {INSPECTION_CLASSES[value]}")


def _fix_requests_cell(value: int | None) -> str:
    return _cell(DASH) if value is None else _cell(str(value), "number")


def _workflow_cell(value: str | None) -> str:
    return _cell(DASH) if value is None else _cell(value)


def _group_first_inspection_cell(passes: int, attempts: int) -> str:
    if attempts == 0:
        return _cell(DASH)
    tone = "pass" if passes == attempts else "fail" if passes == 0 else ""
    return _cell(f"{passes}/{attempts}", f"check {tone}".strip())


def _group_fix_requests_cell(value: float | None) -> str:
    return _cell(DASH) if value is None else _cell(_fmt_number(value), "number")


def _group_workflow_cell(completed: int, attempts: int) -> str:
    if attempts == 0:
        return _cell(DASH)
    tone = "pass" if completed == attempts else "fail" if completed == 0 else ""
    return _cell(f"{completed}/{attempts}", f"check {tone}".strip())


def _short(identifier: str, width: int) -> str:
    return identifier[:width]


def _model_text(setup: ModelSetup) -> str:
    if setup.status == "none":
        return DASH
    if setup.status == "unavailable":
        return UNKNOWN
    if setup.model is None:
        return "scripted" if setup.kind == "scripted" else "default model"
    model = setup.model.removeprefix("claude-")
    return f"{model}@{setup.effort}" if setup.effort else model


def _group_rows(
    report: RunReport, setup: RunSetup | None, check_ids: Sequence[str], run_column: bool
) -> list[str]:
    rows = []
    for group in report.groups:
        topline = group.topline
        if topline is None:
            continue
        members: list[tuple[str, AttemptTopline]] = [
            (attempt.attempt_id, attempt.topline)
            for attempt in report.attempts
            if attempt.topline
            and attempt.topline.latest
            and (attempt.variant or "") == (group.variant or "")
        ]
        variant = setup.variants.get(group.variant) if setup and group.variant else None
        label = " · ".join(
            item
            for item in (
                group.variant or NO_VARIANT,
                _model_text(variant) if variant else None,
                f"mean of {topline.attempts}",
            )
            if item
        )
        run_cell = (
            f'<td class="run" title="{_esc(report.run_id)}">'
            f"{_esc(_short(report.run_id, RUN_ID_CHARS))}</td>"
        )
        rows.append(
            '<tr class="variant">'
            + (run_cell if run_column else "")
            + _cell(label, "label")
            + _turns_cell(topline.turns)
            + _group_first_inspection_cell(
                topline.first_inspection_passes, topline.first_inspection_attempts
            )
            + _group_fix_requests_cell(topline.fix_requests)
            + _group_workflow_cell(topline.workflows_completed, topline.workflow_attempts)
            + _resource_cells(topline.seconds, topline.tokens, topline.cost, topline.currency)
            + "".join(_group_check_cell(topline, check_id) for check_id in check_ids)
            + "</tr>"
        )
        rows.extend(
            "<tr>"
            + ("<td></td>" if run_column else "")
            + f'<td class="attempt" title="{_esc(attempt_id)}">'
            + f"{_esc(_short(attempt_id, ATTEMPT_ID_CHARS))}</td>"
            + _turns_cell(item.turns)
            + _first_inspection_cell(item.first_inspection)
            + _fix_requests_cell(item.fix_requests)
            + _workflow_cell(item.workflow)
            + _resource_cells(item.seconds, item.tokens, item.cost, item.currency)
            + "".join(_attempt_check_cell(item.checks, check_id) for check_id in check_ids)
            + "</tr>"
            for attempt_id, item in members
        )
    return rows


def _topline_section(entries: Sequence[tuple[RunReport, RunSetup | None]]) -> str:
    groups = [
        group for report, _setup in entries for group in report.groups if group.topline is not None
    ]
    if not groups:
        return _section("Summary", _note("This report has no variant groups to summarize."))
    check_ids = list(
        dict.fromkeys(
            check.check_id for group in groups if group.topline for check in group.topline.checks
        )
    )
    run_column = len(entries) > 1
    headers = [
        *(["Run"] if run_column else []),
        "Variant / attempt",
        "Turns",
        "First check",
        "Fix requests",
        "Workflow",
        "Time",
        "Tokens",
        "Cost",
        *map(check_label, check_ids),
    ]
    rows = "".join(
        row
        for report, setup in entries
        for row in _group_rows(report, setup, check_ids, run_column)
    )
    caption = (
        "Per run, variant and attempt, agent only."
        if run_column
        else "Per variant and attempt, agent only."
    )
    return _section(
        "Summary", _table_html(caption, headers, rows, "topline") + _note(TOPLINE_LEGEND)
    )


def _role_identity(setup: ModelSetup) -> str | None:
    if setup.status == "unavailable":
        return None
    return setup.identity if setup.status == "configured" else DASH


_SETUP_FIELDS: tuple[
    tuple[str, Callable[[RunSetup], str], Callable[[RunSetup], str | None]], ...
] = (
    (
        "case",
        lambda setup: (
            ", ".join(_short(item, CASE_ID_CHARS) for item in setup.case_ids)
            if setup.case_ids is not None
            else UNKNOWN
        ),
        lambda setup: ",".join(setup.case_ids) if setup.case_ids is not None else None,
    ),
    (
        "simulated user",
        lambda setup: _model_text(setup.simulator),
        lambda setup: _role_identity(setup.simulator),
    ),
    ("judge", lambda setup: _model_text(setup.judge), lambda setup: _role_identity(setup.judge)),
    (
        "inspector",
        lambda setup: _model_text(setup.inspector),
        lambda setup: _role_identity(setup.inspector),
    ),
)


def _listed(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _setups_section(setups: Sequence[RunSetup]) -> str:
    unknown = [
        name
        for name, _text, identity in _SETUP_FIELDS
        if any(identity(setup) is None for setup in setups)
    ]
    differing = [
        name
        for name, _text, identity in _SETUP_FIELDS
        if name not in unknown and len({identity(setup) for setup in setups}) > 1
    ]
    headers = [
        "Run",
        "Variants",
        *(name.capitalize() for name, _text, _identity in _SETUP_FIELDS),
        "Price table",
    ]
    rows = "".join(
        "<tr>"
        + _cell(setup.run_id)
        + _cell(
            ", ".join(
                f"{name} ({_model_text(variant)})" for name, variant in setup.variants.items()
            )
            or UNKNOWN
        )
        + "".join(
            _cell(text(setup), "differs" if name in differing else "")
            for name, text, _identity in _SETUP_FIELDS
        )
        + _cell(", ".join(setup.price_versions) or UNKNOWN)
        + "</tr>"
        for setup in setups
    )
    notes = []
    if differing:
        notes.append(
            f"Highlighted settings differ between runs ({_listed(differing)}), so rows from "
            "different runs were not measured under identical conditions."
        )
    if unknown:
        notes.append(
            f"Could not compare the {_listed(unknown)}: some runs' frozen settings are unavailable."
        )
    if not differing and not unknown:
        notes.append(
            "Every run used the same "
            + _listed([name for name, _text, _identity in _SETUP_FIELDS])
            + "."
        )
    issues = [f"Run {setup.run_id}: {issue}" for setup in setups for issue in setup.issues]
    body = (
        _table_html("Settings each run froze.", headers, rows)
        + "".join(_note(note) for note in notes)
        + (_bullets(issues) if issues else "")
    )
    return _section("Runs", body)


def _report_header(report: RunReport) -> str:
    rows = [
        ("Run", report.run_id),
        ("Experiment", report.experiment_id),
        ("Compatibility", report.compatibility_id or "none"),
        ("Durable sequence", _fmt_count(report.durable_sequence)),
        ("Scheduled trials", _fmt_count(report.scheduled_trials)),
        ("Retained attempts", _fmt_count(len(report.attempts))),
        ("Portable report", "yes" if report.portable else "no"),
        (
            "Unstarted trials",
            ", ".join(report.unstarted_trials) if report.unstarted_trials else "none",
        ),
    ]
    children = [_definitions(rows)]
    if report.input_error:
        children.append(_note(f"Input error: {report.input_error}"))
    children.append(
        "<h3>Omissions</h3>" + _bullets(report.omissions)
        if report.omissions
        else _note("No omissions recorded.")
    )
    children.append("<h3>Limitations</h3>" + _bullets(report.limitations))
    return _section("Report", "".join(children))


def _results_section(report: RunReport) -> str:
    groups = report.groups
    if not groups:
        return _section("Results", _note("This report has no variant groups to compare."))
    headers = ["Measure", *[_group_label(group) for group in groups]]

    def money_cell(
        selector: Callable[[GroupMetrics], Distribution | None],
    ) -> Callable[[GroupMetrics], str]:
        def render(group: GroupMetrics) -> str:
            dist = selector(group)
            return f"{_fmt_distribution(dist, _money)}{_currency_suffix(group, dist)}"

        return render

    measures = [
        (f"Cost (API-equivalent estimate) total ({RETAINED})", money_cell(lambda g: g.costs)),
        (
            f"Cost by role: subject (API-equivalent estimate, {RETAINED})",
            money_cell(lambda g: g.cost_by_role.get("subject")),
        ),
        (
            f"Cost by role: simulator (API-equivalent estimate, {RETAINED})",
            money_cell(lambda g: g.cost_by_role.get("simulator")),
        ),
        (
            f"Cost by role: judge (API-equivalent estimate, {RETAINED})",
            money_cell(lambda g: g.cost_by_role.get("judge")),
        ),
        (
            f"Price basis ({RETAINED})",
            lambda g: _describe_price_basis(g.price_basis),
        ),
        (
            f"Total input tokens ({RETAINED})",
            lambda g: _fmt_distribution(g.total_input_tokens, _whole),
        ),
        (
            f"Cache read tokens ({RETAINED})",
            lambda g: _fmt_distribution(g.cache_read_tokens, _whole),
        ),
        (
            f"Cache write tokens ({RETAINED})",
            lambda g: _fmt_distribution(g.cache_write_tokens, _whole),
        ),
        (
            f"Subject output tokens ({ELIGIBLE})",
            lambda g: _fmt_distribution(g.output_tokens, _whole),
        ),
        (f"Accepted turns ({ELIGIBLE})", lambda g: _fmt_distribution(g.accepted_turns, _whole)),
        (
            f"Subject duration ({ELIGIBLE})",
            lambda g: _fmt_distribution(g.durations, _fmt_seconds),
        ),
        (
            f"Simulator replies, help needed ({ELIGIBLE})",
            lambda g: _fmt_distribution(g.simulator_replies, _whole),
        ),
    ]
    rows = [[label, *[render(group) for group in groups]] for label, render in measures]
    caption = (
        "Cost is an API-equivalent estimate from a dated price table, not a provider invoice; "
        "each row's label states whether it covers every retained attempt (retries included) "
        "or eligible attempts only."
    )
    return _section("Results", _table(caption, headers, rows))


def _help_needed_section(report: RunReport) -> str:
    groups = report.groups
    if not groups:
        return _section("Help needed", _note("This report has no variant groups."))
    rows = [
        [
            _group_label(group),
            _fmt_distribution(group.simulator_replies, _whole),
            _facts_disclosed_text(group.help_needed),
        ]
        for group in groups
    ]
    caption = "Simulator replies and facts disclosed by variant, among eligible attempts."
    return _section(
        "Help needed", _table(caption, ["Variant", "Simulator replies", "Facts disclosed"], rows)
    )


def _criteria_summary_section(report: RunReport) -> str:
    keys = _union_case_keys(report.groups, "criteria", "criterion_id")
    if not keys:
        return _section("Criteria", _note("No criterion results are retained for any variant."))
    headers = ["Case", "Criterion", *[_group_label(group) for group in report.groups]]
    rows = []
    for case_id, criterion_id in keys:
        row = [case_id, criterion_id]
        for group in report.groups:
            item = _find_criterion(group.criteria, case_id, criterion_id)
            row.append(
                "not measured"
                if item is None
                else f"{_fmt_count(item.passes)}/{_fmt_count(item.graded)} pass · "
                f"mean score {_fmt_number(item.scores.mean, 2)}"
            )
        rows.append(row)
    caption = "Per-criterion pass rate and mean score by variant, among eligible attempts."
    return _section("Criteria", _table(caption, headers, rows))


def _checks_summary_section(report: RunReport) -> str:
    keys = _union_case_keys(report.groups, "checks", "check_id")
    if not keys:
        return _section("Checks", _note("No case checks are retained for any variant."))
    headers = ["Case", "Check", *[_group_label(group) for group in report.groups]]
    rows = []
    for case_id, check_id in keys:
        row = [case_id, check_id]
        for group in report.groups:
            item = _find_check(group.checks, case_id, check_id)
            if item is None:
                row.append("not measured")
                continue
            agreement = UNKNOWN if item.agreement is None else _fmt_rate(item.agreement)
            row.append(
                " · ".join(
                    [
                        f"pass {_fmt_count(item.passes)}",
                        f"fail {_fmt_count(item.fails)}",
                        f"n/a {_fmt_count(item.not_applicable)}",
                        f"error {_fmt_count(item.errors)}",
                        f"agreement {agreement}",
                    ]
                )
            )
        rows.append(row)
    caption = (
        "Per-check outcomes and agreement across repetitions by variant, among eligible attempts."
    )
    return _section("Checks", _table(caption, headers, rows))


def _metrics_summary_section(report: RunReport) -> str:
    keys = _union_case_keys(report.groups, "metrics", "metric_id")
    if not keys:
        return _section("Metrics", _note("No case metrics are retained for any variant."))
    headers = ["Case", "Metric", "Unit", *[_group_label(group) for group in report.groups]]
    rows = []
    for case_id, metric_id in keys:
        sample = None
        for group in report.groups:
            sample = _find_metric(group.metrics, case_id, metric_id)
            if sample is not None:
                break
        row = [
            case_id,
            sample.label if sample is not None else metric_id,
            sample.unit if sample is not None and sample.unit else UNKNOWN,
        ]
        for group in report.groups:
            item = _find_metric(group.metrics, case_id, metric_id)
            row.append("not measured" if item is None else _fmt_distribution(item.values))
        rows.append(row)
    caption = "Per-metric value distribution by variant, among eligible attempts."
    return _section("Metrics", _table(caption, headers, rows))


def _attempts_table(attempts: Sequence[AttemptReport]) -> str:
    if not attempts:
        return _note("No attempt matches the current filter.")
    rows = []
    for attempt in attempts:
        exclusions = [
            *attempt.current_exclusions,
            *(attempt.result.exclusion_reasons if attempt.result else ()),
        ]
        rows.append(
            [
                attempt.attempt_id,
                attempt.trial_id,
                attempt.variant or NO_VARIANT,
                attempt.stage,
                attempt.mode or UNKNOWN,
                attempt.result.completion if attempt.result else "no assessment",
                attempt.eligibility or UNKNOWN,
                "none" if not exclusions else ", ".join(exclusions),
                attempt.raw_evidence,
            ]
        )
    caption = "Attempts, including superseded retries of the same trial."
    headers = [
        "Attempt",
        "Trial",
        "Variant",
        "Stage",
        "Mode",
        "Completion",
        "Eligibility",
        "Exclusions",
        "Raw evidence",
    ]
    return _table(caption, headers, rows)


def _criteria_rationale(items: Sequence[CriterionReasoning]) -> dict[str, str]:
    return {item.criterion_id: item.rationale for item in items if item.rationale}


def _checks_rationale(items: Sequence[CheckReasoning]) -> dict[str, str]:
    return {item.check_id: item.rationale for item in items if item.rationale}


def _criteria_card(result: Assessment | None, reasoning_view: JudgeReasoningView | None) -> str:
    if result is None or not result.criteria:
        return _section("Criteria", _note("No criterion results are retained."))
    rationale = _criteria_rationale(reasoning_view.criteria if reasoning_view else ())
    rows = []
    for criterion in result.criteria:
        text = rationale.get(criterion.criterion_id)
        if text is None:
            text = (
                "not judged (deterministic)"
                if criterion.kind == "deterministic"
                else "reasoning not included"
            )
        rows.append(
            [
                criterion.criterion_id,
                criterion.kind,
                "required" if criterion.required else "optional",
                criterion.outcome,
                UNKNOWN if criterion.score is None else _fmt_number(criterion.score, 3),
                criterion.error or "none",
                text,
            ]
        )
    caption = "Criterion outcomes with the judge's reasoning."
    headers = ["Criterion", "Kind", "Required", "Outcome", "Score", "Error", "Rationale"]
    return _section("Criteria", _table(caption, headers, rows))


def _checks_card(result: Assessment | None, reasoning_view: JudgeReasoningView | None) -> str:
    checks = result.checks if result else ()
    if not checks:
        return _section("Checks", _note("No case checks are retained for this attempt."))
    rationale = _checks_rationale(reasoning_view.checks if reasoning_view else ())
    rows = [
        [
            check.check_id,
            check.outcome,
            check.answer or UNKNOWN,
            rationale.get(check.check_id, "reasoning not included"),
        ]
        for check in checks
    ]
    caption = "Case-declared check outcomes with the judge's reasoning."
    return _section("Checks", _table(caption, ["Check", "Outcome", "Answer", "Rationale"], rows))


def _metrics_card(result: Assessment | None) -> str:
    metrics = result.metrics if result else ()
    if not metrics:
        return _section("Metrics", _note("No case metrics are retained for this attempt."))
    parts = []
    for item in metrics:
        parts.append(f"<h3>{_esc(item.metrics_id)}</h3>")
        note_text = f"outcome {item.outcome}" + (f" · {item.error}" if item.error else "")
        parts.append(_note(note_text))
        if not item.values:
            parts.append(_note("No metric values recorded."))
        else:
            rows = [
                [
                    value.metric_id,
                    value.label,
                    _fmt_metric_value(value.value),
                    value.unit or UNKNOWN,
                    value.better or UNKNOWN,
                ]
                for value in item.values
            ]
            parts.append(
                _table(
                    f"Metric values for {item.metrics_id}.",
                    ["Metric", "Label", "Value", "Unit", "Better"],
                    rows,
                )
            )
    return _section("Metrics", "".join(parts))


def _help_needed_card(result: Assessment | None) -> str:
    help_needed = result.help_needed if result else None
    if help_needed is None:
        return _section(
            "Help needed", _note("No help-needed accounting is retained for this attempt.")
        )

    def listed(values: Sequence[str]) -> str:
        return "none" if not values else ", ".join(values)

    rows = [
        ("Simulator replies", _fmt_count(help_needed.simulator_replies)),
        ("Facts disclosed", listed(help_needed.facts_disclosed)),
        ("Disclosed proactively", listed(help_needed.proactive_facts)),
        ("Disclosed on request", listed(help_needed.on_request_facts)),
    ]
    return _section("Help needed", _definitions(rows))


def _workflow_card(result: Assessment | None) -> str:
    workflow = result.workflow if result else None
    if workflow is None:
        return _section("Workflow", _note("No workflow is retained for this attempt."))
    rows = [
        ("Declared steps", _fmt_count(workflow.steps)),
        ("Completed steps", _fmt_count(workflow.completed_steps)),
        ("Inspections", _fmt_count(workflow.inspections)),
        ("Fix requests", _fmt_count(workflow.fix_requests)),
        ("Dialogs answered", _fmt_count(workflow.dialogs_answered)),
        ("First inspection", workflow.first_inspection),
        ("Final inspection", workflow.final_inspection),
        ("Inspection time", _fmt_seconds(workflow.inspection_seconds)),
    ]
    events = (
        _note("No workflow events are retained for this attempt.")
        if not workflow.events
        else _table(
            "Workflow events in order.",
            ["Step", "Kind", "Outcome", "Turn", "Failed criteria"],
            [
                [
                    event.step_id,
                    event.kind,
                    event.outcome,
                    str(event.turn),
                    ", ".join(event.failed_criteria) if event.failed_criteria else "none",
                ]
                for event in workflow.events
            ],
        )
    )
    return _section("Workflow", _definitions(rows) + events)


def _attempts_section(report: RunReport, reasoning: Mapping[str, JudgeReasoningView]) -> str:
    children = [_attempts_table(report.attempts)]
    for attempt in report.attempts:
        reasoning_view = reasoning.get(attempt.attempt_id)
        summary_text = (
            f"Attempt {attempt.attempt_id} "
            f"(trial {attempt.trial_id}, variant {attempt.variant or NO_VARIANT})"
        )
        detail = "".join(
            [
                _criteria_card(attempt.result, reasoning_view),
                _checks_card(attempt.result, reasoning_view),
                _metrics_card(attempt.result),
                _help_needed_card(attempt.result),
                _workflow_card(attempt.result),
            ]
        )
        children.append(
            f'<details class="attempt"><summary>{_esc(summary_text)}</summary>{detail}</details>'
        )
    return _section("Attempts", "".join(children))


def _case_section(case_id: str, view: CaseView | None) -> str:
    if view is None:
        return _section(f"Case {case_id}", _note("case not available in this store"))
    rows = [
        ("Case ID", case_id),
        ("Repository", view.repository_id),
        ("Persona", view.persona_id),
        ("Baseline commit", view.baseline_commit or UNKNOWN),
        ("Reference patch", "present" if view.reference_patch else "none"),
        ("Max turns", _fmt_count(view.budgets.max_turns)),
        ("Subject seconds", _fmt_seconds(view.budgets.subject_seconds)),
        ("Setup seconds", _fmt_seconds(view.budgets.setup_seconds)),
        (
            "Tool requirements",
            ", ".join(view.tool_requirements) if view.tool_requirements else "none",
        ),
    ]
    children = [
        _definitions(rows),
        "<h3>Initial prompt</h3>",
        f"<pre>{_esc(view.initial_prompt)}</pre>",
    ]
    if not view.facts:
        children.append(_section("Facts", _note("No allowed facts are recorded for this case.")))
    else:
        fact_rows = [[fact.fact_id, fact.disclosure, fact.text] for fact in view.facts]
        children.append(
            _section(
                "Facts",
                _table(
                    "Facts the subject may be given, and how they may be disclosed.",
                    ["Fact", "Disclosure", "Text"],
                    fact_rows,
                ),
            )
        )
    rubric_children = []
    rubric_children.append(
        _note("No default rubrics are disabled for this case.")
        if not view.disabled_rubrics
        else _note(f"Disabled default rubrics: {', '.join(view.disabled_rubrics)}.")
    )
    if not view.rubrics:
        rubric_children.append(_note("No rubric criteria are retained for this case."))
    else:
        rubric_rows = [
            [
                item.criterion_id,
                item.origin,
                "required" if item.required else "optional",
                item.rubric,
            ]
            for item in view.rubrics
        ]
        rubric_children.append(
            _table(
                "Effective judge rubrics, marking case-authored, default and overridden criteria.",
                ["Criterion", "Origin", "Required", "Rubric"],
                rubric_rows,
            )
        )
    children.append(_section("Rubrics", "".join(rubric_children)))
    if not view.deterministic:
        children.append(
            _section(
                "Deterministic criteria",
                _note("No deterministic (hidden-verifier) criteria are retained for this case."),
            )
        )
    else:
        det_rows = [
            [
                item.criterion_id,
                "required" if item.required else "optional",
                " ".join(item.command.argv),
                item.description,
            ]
            for item in view.deterministic
        ]
        caption = (
            "Hidden-verifier criteria, by description, required flag and command argv; "
            "the hidden entrypoint's content is never shown."
        )
        children.append(
            _section(
                "Deterministic criteria",
                _table(caption, ["Criterion", "Required", "Command", "Description"], det_rows),
            )
        )
    if not view.checks:
        children.append(_section("Checks", _note("No case-declared checks are retained.")))
    else:
        check_rows = [
            [
                item.check_id,
                item.expect,
                f"{item.only_if.check_id} = {item.only_if.answer}" if item.only_if else "none",
                item.question,
            ]
            for item in view.checks
        ]
        caption = "Case-declared yes/no checks the judge answers from the transcript and files."
        children.append(
            _section(
                "Checks", _table(caption, ["Check", "Expect", "Only if", "Question"], check_rows)
            )
        )
    if not view.metrics:
        children.append(
            _section("Metrics commands", _note("No case metrics commands are retained."))
        )
    else:
        metric_rows = [
            [item.metrics_id, item.description, " ".join(item.command.argv)]
            for item in view.metrics
        ]
        caption = "Case-supplied metrics commands run against the final workspace."
        children.append(
            _section(
                "Metrics commands",
                _table(caption, ["Metrics", "Description", "Command"], metric_rows),
            )
        )
    return _section(f"Case {view.title}", "".join(children))


def _cases_section(cases: Mapping[str, CaseView | None]) -> str:
    if not cases:
        return _section("Cases", _note("No case summaries are included in this report."))
    inner = "".join(_case_section(case_id, cases[case_id]) for case_id in sorted(cases))
    return f"<section><h2>Cases</h2>{inner}</section>"


_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #5a5a5a;
  --border: #d7d7d7;
  --stripe: #f4f6f9;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c;
    --fg: #e8e8e8;
    --muted: #a0a6b0;
    --border: #3a3f47;
    --stripe: #1b1f26;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
main { max-width: 1440px; margin: 0 auto; padding: 24px 16px 64px; }
section { margin: 24px 0; padding: 16px; border: 1px solid var(--border); border-radius: 8px; }
h2 { margin-top: 0; }
.scroller { overflow-x: auto; }
.scroller::-webkit-scrollbar { height: 10px; }
.scroller::-webkit-scrollbar-thumb { background: var(--muted); border-radius: 5px; }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }
caption { text-align: left; color: var(--muted); font-size: 13px; margin-bottom: 6px; }
th, td { border: 1px solid var(--border); padding: 6px 8px; text-align: left; vertical-align: top; }
thead th { background: var(--stripe); }
tbody tr:nth-child(even) { background: var(--stripe); }
.note { color: var(--muted); font-size: 13px; }
dl.definitions { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 8px 0; }
dl.definitions dt { font-weight: 600; color: var(--muted); }
dl.definitions dd { margin: 0; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--stripe); padding: 8px; border-radius: 6px; }
details.attempt { border: 1px solid var(--border); border-radius: 6px; margin: 8px 0; padding: 8px; }
details.attempt summary { cursor: pointer; font-weight: 600; }
table.topline td.number { text-align: right; white-space: nowrap; }
table.topline td.check { text-align: center; font-weight: 600; }
table.topline td.pass { color: #1a7f37; }
table.topline td.fail { color: #cf222e; }
table.topline tr.variant td { font-weight: 600; border-top: 2px solid var(--border); }
td.differs { background: rgba(207, 34, 46, 0.14); font-weight: 600; }
h2.run { margin: 40px 0 0; }
table.topline td.attempt { padding-left: 24px; font-family: ui-monospace, monospace; white-space: nowrap; }
table.topline td.label { white-space: nowrap; }
table.topline td.run { font-family: ui-monospace, monospace; white-space: nowrap; }
@media (prefers-color-scheme: dark) {
  table.topline td.pass { color: #3fb950; }
  table.topline td.fail { color: #f85149; }
}
"""


def _run_sections(report: RunReport, reasoning: Mapping[str, JudgeReasoningView]) -> str:
    return "".join(
        [
            _report_header(report),
            _results_section(report),
            _help_needed_section(report),
            _criteria_summary_section(report),
            _checks_summary_section(report),
            _metrics_summary_section(report),
            _attempts_section(report, reasoning),
        ]
    )


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        f'<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_STYLE}</style>"
        f"</head><body><main>{body}</main></body></html>"
    )


def render_report_html(
    entries: Sequence[tuple[RunReport, RunSetup | None]],
    cases: Mapping[str, CaseView | None],
    *,
    reasoning: Mapping[str, Mapping[str, JudgeReasoningView]] | None = None,
) -> str:
    combined = len(entries) > 1
    sections = "".join(
        (f'<h2 class="run">Run {_esc(report.run_id)}</h2>' if combined else "")
        + _run_sections(report, (reasoning or {}).get(report.run_id, {}))
        for report, _setup in entries
    )
    setups = [setup for _report, setup in entries if setup is not None]
    body = (
        _topline_section(entries)
        + (_setups_section(setups) if combined else "")
        + sections
        + _cases_section(cases)
    )
    title = "dryheave report " + ", ".join(
        _short(report.run_id, RUN_ID_CHARS) if combined else report.run_id
        for report, _setup in entries
    )
    return _page(title, body)
