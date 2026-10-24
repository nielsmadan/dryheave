import html as html_lib
from collections.abc import Callable, Mapping, Sequence

from dryheave.report_models import (
    AttemptReport,
    AttemptTopline,
    CriterionSummary,
    Distribution,
    FactGroup,
    GroupMetrics,
    GroupTopline,
    MetricSummary,
    ModelSetup,
    PriceBasis,
    RunReport,
    RunSetup,
)
from dryheave.result_models import Assessment, CriterionResult, PhaseCorrections
from dryheave.runner_models import RunBudget
from dryheave.viewer_models import CaseFactView, CaseView, CriterionReasoning, JudgeReasoningView

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
OUTCOME_MARKS = {"pass": "✓", "fail": "✗", "indeterminate": "?"}
OUTCOME_CLASSES = {"pass": "pass", "fail": "fail", "indeterminate": ""}
INSPECTION_MARKS = {"passed": "✓", "failed": "✗", "unavailable": "?"}
INSPECTION_CLASSES = {"passed": "pass", "failed": "fail", "unavailable": "unavailable"}
TOPLINE_LEGEND = (
    "Outcome comes from final grading: ✓ every requirement met, ✗ a requirement unmet at the "
    "end, ? indeterminate (disputed, unclear, a phase never reached, or a grading error); "
    "variant rows count passes, independence and corrections over eligible attempts. "
    "Independence is the mean credit over "
    "the requirements the user did not volunteer: 1 solved on its own, 0.75 asked for, 0 "
    "pointed out by the user or unmet. Turns, time, tokens and cost are the agent's alone, "
    "without the user stand-in and the judge, and time excludes inspection; tokens include "
    "cache reads and writes, and cost is an API list-price estimate. First check is the "
    f"workflow's first inspection (✓ passed, ✗ failed, ? unavailable, {DASH} no inspection); "
    "corrections count rounds in which the user pointed out unmet requirements; workflow "
    f"shows delivered over declared prompt steps, or {DASH} without a workflow. Variant rows "
    "average the attempts below them; retried attempts are left out. A floor row is the "
    "pilot run the budget was derived from."
)


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


def _corrections_cell(value: int | None) -> str:
    return _cell(DASH) if value is None else _cell(str(value), "number")


def _outcome_cell(outcome: str | None, reason: str | None) -> str:
    if outcome is None:
        return _cell(DASH)
    text = OUTCOME_MARKS[outcome] + (f" {reason}" if reason and outcome != "pass" else "")
    return _cell(text, f"check {OUTCOME_CLASSES[outcome]}".strip())


def _independence_cell(value: float | None) -> str:
    return _cell(DASH) if value is None else _cell(_fmt_number(value), "number")


def _group_outcome_cell(passes: int, graded: int) -> str:
    if graded == 0:
        return _cell(DASH)
    tone = "pass" if passes == graded else "fail" if passes == 0 else ""
    return _cell(f"{passes}/{graded}", f"check {tone}".strip())


def _budget_text(budget: RunBudget) -> str:
    return (
        f"{budget.max_turns} turns, {budget.stand_in_replies} stand-in replies, "
        f"{budget.max_inspections} inspections"
    )


def _workflow_cell(value: str | None) -> str:
    return _cell(DASH) if value is None else _cell(value)


def _group_first_inspection_cell(passes: int, attempts: int) -> str:
    if attempts == 0:
        return _cell(DASH)
    tone = "pass" if passes == attempts else "fail" if passes == 0 else ""
    return _cell(f"{passes}/{attempts}", f"check {tone}".strip())


def _group_corrections_cell(value: float | None) -> str:
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


def _variant_row(label: str, topline: GroupTopline, run_cell: str) -> str:
    return (
        '<tr class="variant">'
        + run_cell
        + _cell(label, "label")
        + _group_outcome_cell(topline.passes, topline.graded)
        + _independence_cell(topline.independence)
        + _turns_cell(topline.turns)
        + _group_first_inspection_cell(
            topline.first_inspection_passes, topline.first_inspection_attempts
        )
        + _group_corrections_cell(topline.corrections)
        + _group_workflow_cell(topline.workflows_completed, topline.workflow_attempts)
        + _resource_cells(topline.seconds, topline.tokens, topline.cost, topline.currency)
        + "</tr>"
    )


def _floor_row(report: RunReport, run_column: bool) -> list[str]:
    floor = report.floor
    if floor is None or floor.group.topline is None:
        return []
    label = f"{floor.label} · floor · budget {_budget_text(floor.budget)}"
    return [_variant_row(label, floor.group.topline, "<td></td>" if run_column else "")]


def _group_rows(report: RunReport, setup: RunSetup | None, run_column: bool) -> list[str]:
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
        rows.append(_variant_row(label, topline, run_cell if run_column else ""))
        rows.extend(
            "<tr>"
            + ("<td></td>" if run_column else "")
            + f'<td class="attempt" title="{_esc(attempt_id)}">'
            + f"{_esc(_short(attempt_id, ATTEMPT_ID_CHARS))}</td>"
            + _outcome_cell(item.outcome, item.outcome_reason)
            + _independence_cell(item.independence)
            + _turns_cell(item.turns)
            + _first_inspection_cell(item.first_inspection)
            + _corrections_cell(item.corrections)
            + _workflow_cell(item.workflow)
            + _resource_cells(item.seconds, item.tokens, item.cost, item.currency)
            + "</tr>"
            for attempt_id, item in members
        )
    return [*rows, *_floor_row(report, run_column)]


def _topline_section(entries: Sequence[tuple[RunReport, RunSetup | None]]) -> str:
    groups = [
        group for report, _setup in entries for group in report.groups if group.topline is not None
    ]
    if not groups:
        return _section("Summary", _note("This report has no variant groups to summarize."))
    run_column = len(entries) > 1
    headers = [
        *(["Run"] if run_column else []),
        "Variant / attempt",
        "Outcome",
        "Independence",
        "Turns",
        "First check",
        "Corrections",
        "Workflow",
        "Time",
        "Tokens",
        "Cost",
    ]
    rows = "".join(
        row for report, setup in entries for row in _group_rows(report, setup, run_column)
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
        "user stand-in",
        lambda setup: _model_text(setup.stand_in),
        lambda setup: _role_identity(setup.stand_in),
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
        ("Budget", _budget_text(report.budget) if report.budget else UNKNOWN),
        (
            "Floor",
            f"{report.floor.label} from run {report.floor.run_id}, budget "
            f"{_budget_text(report.floor.budget)}"
            if report.floor
            else "none",
        ),
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
            f"Cost by role: stand-in (API-equivalent estimate, {RETAINED})",
            money_cell(lambda g: g.cost_by_role.get("stand_in")),
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
            f"Ordinary stand-in replies, fix requests excluded ({ELIGIBLE})",
            lambda g: _fmt_distribution(g.stand_in_replies, _whole),
        ),
    ]
    rows = [[label, *[render(group) for group in groups]] for label, render in measures]
    caption = (
        "Cost is an API-equivalent estimate from a dated price table, not a provider invoice; "
        "each row's label states whether it covers every retained attempt (retries included) "
        "or eligible attempts only."
    )
    return _section("Results", _table(caption, headers, rows))


def _phase_corrections(corrections: Sequence[PhaseCorrections]) -> str:
    return (
        "; ".join(
            f"{item.phase}: {item.rounds} rounds ({', '.join(item.raised) or 'none raised'})"
            for item in corrections
        )
        or "none"
    )


def _fact_group_text(facts: FactGroup | None) -> list[str]:
    if facts is None:
        return [UNKNOWN] * 6
    outcomes = ", ".join(f"{key} {value}" for key, value in sorted(facts.outcomes.items()))
    reasons = ", ".join(f"{key} {value}" for key, value in sorted(facts.outcome_reasons.items()))
    return [
        f"{outcomes or 'none'}" + (f" ({reasons})" if reasons else ""),
        UNKNOWN if facts.pass_rate is None else _fmt_rate(facts.pass_rate),
        _fmt_distribution(facts.independence),
        _fmt_distribution(facts.corrections, _whole),
        _fmt_count(facts.unreached_attempts),
        _fmt_count(facts.uncovered),
    ]


def _fact_cell(facts: FactGroup | None, case_id: str, fact_id: str) -> str:
    row = next(
        (
            item
            for item in (facts.facts if facts else ())
            if item.case_id == case_id and item.fact_id == fact_id
        ),
        None,
    )
    if row is None:
        return "not measured"
    raised = UNKNOWN if row.raised_rate is None else _fmt_rate(row.raised_rate)
    first = (
        "no inspection"
        if row.first_inspection_met_rate is None
        else _fmt_rate(row.first_inspection_met_rate)
    )
    return " · ".join(
        [
            f"met {row.met}/{row.attempts}",
            (
                f"own {row.own}, asked {row.elicited}, corrected {row.corrected}, "
                f"unmet {row.unmet}, unreached {row.unreached}"
            ),
            f"raised {raised}",
            f"first check met {first}",
            f"regressions {row.regressions}",
            f"disputed {row.disputed}, unclear {row.unclear}",
        ]
    )


def _facts_section(report: RunReport) -> str:
    groups = report.groups
    if not groups:
        return _section("Facts", _note("This report has no variant groups."))
    measures = [
        "Outcomes",
        "Pass rate",
        "Independence",
        "Corrections",
        "Phases unreached",
        "Uncovered problems",
    ]
    columns = [_fact_group_text(group.facts) for group in groups]
    rows = [[label, *(column[index] for column in columns)] for index, label in enumerate(measures)]
    keys = list(
        dict.fromkeys(
            (item.case_id, item.fact_id, item.kind, item.phase or DASH)
            for group in groups
            if group.facts
            for item in group.facts.facts
        )
    )
    fact_rows = [
        [
            case_id,
            fact_id,
            kind,
            phase,
            *(_fact_cell(group.facts, case_id, fact_id) for group in groups),
        ]
        for case_id, fact_id, kind, phase in keys
    ]
    headers = [_group_label(group) for group in groups]
    return _section(
        "Facts",
        _table(
            "Outcomes over the latest attempt per trial; independence, corrections and facts "
            "over eligible attempts.",
            ["Measure", *headers],
            rows,
        )
        + (
            _table(
                "Per fact: final verdicts, how each met fact was reached, how often the user "
                "stand-in raised it, and the first inspection's verdict.",
                ["Case", "Fact", "Kind", "Phase", *headers],
                fact_rows,
            )
            if fact_rows
            else _note("No fact results are retained for any variant.")
        ),
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
                _attempt_outcome(attempt.result),
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
        "Outcome",
        "Eligibility",
        "Exclusions",
        "Raw evidence",
    ]
    return _table(caption, headers, rows)


def _attempt_outcome(result: Assessment | None) -> str:
    if result is None:
        return "no assessment"
    if result.outcome_reason is None or result.outcome == "pass":
        return result.outcome
    return f"{result.outcome} ({result.outcome_reason})"


def _criteria_rationale(items: Sequence[CriterionReasoning]) -> dict[str, str]:
    return {item.criterion_id: item.rationale for item in items if item.rationale}


def _criterion_role(criterion: CriterionResult) -> str:
    if criterion.kind == "judge":
        return "rubric"
    return "requirement" if criterion.required else "optional"


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
                _criterion_role(criterion),
                criterion.outcome,
                UNKNOWN if criterion.score is None else _fmt_number(criterion.score, 3),
                criterion.error or "none",
                text,
            ]
        )
    caption = "Criterion outcomes with the judge's reasoning."
    headers = ["Criterion", "Kind", "Role", "Outcome", "Score", "Error", "Rationale"]
    return _section("Criteria", _table(caption, headers, rows))


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


def _facts_card(result: Assessment | None) -> str:
    if result is None or (not result.facts and result.fact_account is None):
        return _section("Facts", _note("No fact results are retained for this attempt."))
    account = result.fact_account
    progress = {item.fact_id: item for item in account.facts} if account else {}
    graded = {item.fact_id: item for item in result.facts}
    rows = [
        [
            fact_id,
            graded[fact_id].verdict if fact_id in graded else "not graded",
            progress[fact_id].credit if fact_id in progress else UNKNOWN,
            _fmt_count(progress[fact_id].raised) if fact_id in progress else UNKNOWN,
            (progress[fact_id].first_inspection or DASH) if fact_id in progress else UNKNOWN,
            graded[fact_id].observation if fact_id in graded else "",
            graded[fact_id].evidence if fact_id in graded else "",
        ]
        for fact_id in dict.fromkeys((*graded, *progress))
    ]
    summary = [
        ("Outcome", _attempt_outcome(result)),
        (
            "Independence",
            UNKNOWN
            if account is None or account.independence is None
            else _fmt_number(account.independence),
        ),
        ("Corrections", _fmt_count(account.fix_rounds) if account else UNKNOWN),
        ("Corrections by phase", _phase_corrections(account.corrections) if account else UNKNOWN),
        ("Ordinary stand-in replies", _fmt_count(account.stand_in_replies) if account else UNKNOWN),
        (
            "Phases reached",
            ", ".join(account.reached_phases) if account and account.reached_phases else "none",
        ),
        (
            "Phases unreached",
            ", ".join(account.unreached_phases) if account and account.unreached_phases else "none",
        ),
    ]
    uncovered = (
        "<h3>Problems no fact covers</h3>" + _bullets(result.uncovered) if result.uncovered else ""
    )
    return _section(
        "Facts",
        _definitions(summary)
        + _table(
            "Final verdict, credit, times raised and first inspection per fact.",
            ["Fact", "Verdict", "Credit", "Raised", "First check", "Observation", "Evidence"],
            rows,
        )
        + uncovered,
    )


def _workflow_card(result: Assessment | None) -> str:
    workflow = result.workflow if result else None
    if workflow is None:
        return _section("Workflow", _note("No workflow is retained for this attempt."))
    rows = [
        ("Declared steps", _fmt_count(workflow.steps)),
        ("Completed steps", _fmt_count(workflow.completed_steps)),
        ("Inspections", _fmt_count(workflow.inspections)),
        ("Fix requests", _fmt_count(workflow.fix_requests)),
        ("Fallback replies", _fmt_count(workflow.fallbacks)),
        ("Dialogs answered", _fmt_count(workflow.dialogs_answered)),
        ("First inspection", workflow.first_inspection),
        ("Final inspection", workflow.final_inspection),
        ("Completed", "yes" if workflow.completed else "no"),
        ("Inspection time", _fmt_seconds(workflow.inspection_seconds)),
    ]
    events = (
        _note("No workflow events are retained for this attempt.")
        if not workflow.events
        else _table(
            "Workflow events in order.",
            ["Step", "Kind", "Outcome", "Turn", "Unmet facts"],
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
                _facts_card(attempt.result),
                _criteria_card(attempt.result, reasoning_view),
                _metrics_card(attempt.result),
                _workflow_card(attempt.result),
            ]
        )
        children.append(
            f'<details class="attempt"><summary>{_esc(summary_text)}</summary>{detail}</details>'
        )
    return _section("Attempts", "".join(children))


FACTS_CAPTION = (
    "Requirements gate pass and fail, optional facts are graded but never gate, and context "
    "is background the stand-in may mention. The stand-in sees the text; only the inspector "
    "and judge see the check, and a verifier's hidden entrypoint is never shown."
)


def _fact_check(fact: CaseFactView) -> str:
    if fact.verifier is not None:
        return "verifier: " + " ".join(fact.verifier.argv)
    if fact.kind == "context":
        return "not graded"
    return fact.check or "judged against the text"


def _case_facts(view: CaseView) -> str:
    if not view.facts:
        return _section("Facts", _note("No allowed facts are recorded for this case."))
    rows = [
        [fact.fact_id, fact.kind, fact.phase or DASH, fact.disclosure, fact.text, _fact_check(fact)]
        for fact in view.facts
    ]
    headers = ["Fact", "Kind", "Phase", "Disclosure", "Text", "Check"]
    return _section("Facts", _table(FACTS_CAPTION, headers, rows))


def _case_prompt_steps(view: CaseView) -> str:
    if not view.workflow:
        return _section("Prompt steps", _note("No prompt steps are declared for this case."))
    rows = [
        [item.step_id, item.after_phase, "slash command" if item.slash else "chat", item.prompt]
        for item in view.workflow
    ]
    caption = (
        "Prompts the user stand-in sends once no phase up to the step's after-phase has an "
        "unmet requirement."
    )
    headers = ["Step", "After phase", "Sent as", "Prompt"]
    return _section("Prompt steps", _table(caption, headers, rows))


def _case_section(case_id: str, view: CaseView | None) -> str:
    if view is None:
        return _section(f"Case {case_id}", _note("case not available in this store"))
    rows = [
        ("Case ID", case_id),
        ("Repository", view.repository_id),
        ("Persona", view.persona_id),
        ("Baseline commit", view.baseline_commit or UNKNOWN),
        ("Reference patch", "present" if view.reference_patch else "none"),
        ("Phases", " \u2192 ".join(view.phases) if view.phases else "none"),
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
        _case_facts(view),
        _case_prompt_steps(view),
    ]
    rubric_children = [
        _note("No default rubrics are disabled for this case.")
        if not view.disabled_rubrics
        else _note(f"Disabled default rubrics: {', '.join(view.disabled_rubrics)}.")
    ]
    if not view.rubrics:
        rubric_children.append(_note("No rubric criteria are retained for this case."))
    else:
        rubric_rows = [[item.criterion_id, item.origin, item.rubric] for item in view.rubrics]
        rubric_children.append(
            _table(
                "Quality rubrics scored by the judge; they never gate the outcome.",
                ["Criterion", "Origin", "Rubric"],
                rubric_rows,
            )
        )
    children.append(_section("Rubrics", "".join(rubric_children)))
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
            _facts_section(report),
            _criteria_summary_section(report),
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
