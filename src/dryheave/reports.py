import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Literal

from pydantic import JsonValue

from dryheave.assessment_inputs import load_assessment_inputs, role_metrics, saved_judge_calls
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import TrialSpec
from dryheave.experiments import inspect_experiment
from dryheave.integrity import load_assessment
from dryheave.journals import RunStore
from dryheave.models import ObjectId, ObjectKind, TokenUsage, TrialStage
from dryheave.report_models import (
    AttemptReport,
    CheckSummary,
    Comparison,
    CriterionSummary,
    Distribution,
    GroupMetrics,
    MetricSummary,
    PairChange,
    RunReport,
)
from dryheave.result_models import Assessment, CheckResult, CriterionResult, MetricValue
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore


def _finite_float(value: float | int) -> float | None:
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) else None


def distribution(values: Iterable[float | int | None]) -> Distribution:
    numbers = list(values)
    observed = [
        converted
        for value in numbers
        if value is not None and (converted := _finite_float(value)) is not None
    ]
    try:
        stdev = statistics.stdev(observed) if len(observed) > 1 else None
    except (OverflowError, statistics.StatisticsError):
        stdev = None
    return Distribution(
        count=len(observed),
        unknown=len(numbers) - len(observed),
        minimum=min(observed) if observed else None,
        maximum=max(observed) if observed else None,
        mean=statistics.mean(observed) if observed else None,
        median=statistics.median(observed) if observed else None,
        stdev=stdev,
    )


def _total_input(tokens: TokenUsage) -> float | None:
    if tokens.uncached_input is None or tokens.cache_read is None or tokens.cache_write is None:
        return None
    return tokens.uncached_input + tokens.cache_read + tokens.cache_write


def _criteria_summary(eligible: list[Assessment]) -> tuple[CriterionSummary, ...]:
    by_case_criterion: dict[tuple[ObjectId, str], list[CriterionResult]] = defaultdict(list)
    for result in eligible:
        if result.case_id is None:
            continue
        for item in result.criteria:
            by_case_criterion[(result.case_id, item.criterion_id)].append(item)
    return tuple(
        CriterionSummary(
            case_id=case_id,
            criterion_id=criterion_id,
            passes=sum(item.outcome == "pass" for item in items),
            graded=sum(item.outcome in {"pass", "fail"} for item in items),
            scores=distribution(item.score for item in items),
        )
        for (case_id, criterion_id), items in by_case_criterion.items()
    )


MIN_AGREEMENT_SAMPLE = 2


def _agreement(items: list[CheckResult]) -> float | None:
    applicable = [item.outcome for item in items if item.outcome in {"pass", "fail"}]
    if len(applicable) < MIN_AGREEMENT_SAMPLE:
        return None
    return max(Counter(applicable).values()) / len(applicable)


def _checks_summary(eligible: list[Assessment]) -> tuple[CheckSummary, ...]:
    by_case_check: dict[tuple[ObjectId, str], list[CheckResult]] = defaultdict(list)
    for result in eligible:
        if result.case_id is None:
            continue
        for item in result.checks:
            by_case_check[(result.case_id, item.check_id)].append(item)
    return tuple(
        CheckSummary(
            case_id=case_id,
            check_id=check_id,
            passes=sum(item.outcome == "pass" for item in items),
            fails=sum(item.outcome == "fail" for item in items),
            not_applicable=sum(item.outcome == "not_applicable" for item in items),
            errors=sum(item.outcome == "error" for item in items),
            agreement=_agreement(items),
        )
        for (case_id, check_id), items in by_case_check.items()
    )


def _metrics_summary(eligible: list[Assessment]) -> tuple[MetricSummary, ...]:
    by_case_metric: dict[tuple[ObjectId, str], list[MetricValue]] = defaultdict(list)
    for result in eligible:
        if result.case_id is None:
            continue
        for metrics_result in result.metrics:
            for value in metrics_result.values:
                by_case_metric[(result.case_id, value.metric_id)].append(value)
    return tuple(
        MetricSummary(
            case_id=case_id,
            metric_id=metric_id,
            label=items[0].label,
            unit=items[0].unit,
            values=distribution(
                item.value for item in items if isinstance(item.value, int | float)
            ),
        )
        for (case_id, metric_id), items in by_case_metric.items()
    )


def _latest_eligible(attempts: list[AttemptReport]) -> list[Assessment]:
    latest = {item.trial_id: item for item in attempts}
    return [
        item.result
        for item in latest.values()
        if item.result and item.result.eligible and not item.current_exclusions
    ]


def _group(
    variant: str, mode: str, attempts: list[AttemptReport], scheduled: int | None
) -> GroupMetrics:
    results = [item.result for item in attempts if item.result is not None]
    eligible = [
        item.result
        for item in attempts
        if item.result and item.result.eligible and not item.current_exclusions
    ]
    latest_eligible = _latest_eligible(attempts)
    reasons = Counter(
        reason
        for item in attempts
        for reason in dict.fromkeys(
            (*item.current_exclusions, *(item.result.exclusion_reasons if item.result else ()))
        )
    )
    spend: dict[str, float] = defaultdict(float)
    unknown = partial = 0
    costs = []
    role_costs: dict[str, list[float | None]] = defaultdict(list)
    subject_tokens: list[TokenUsage | None] = []
    for attempt in attempts:
        roles = attempt.roles or (attempt.result.roles if attempt.result else ())
        subject_tokens.append(roles[0].known_tokens if roles else None)
        totals = []
        currency = set()
        for role in roles:
            role_totals = []
            role_currency = set()
            for record in role.records:
                if record.cost is not None:
                    if record.cost or record.currency:
                        spend[record.currency or "UNSPECIFIED"] += record.cost
                    totals.append(record.cost)
                    role_totals.append(record.cost)
                    if record.cost or record.currency:
                        currency.add(record.currency)
                        role_currency.add(record.currency)
                else:
                    unknown += 1
            role_costs[role.role].append(
                sum(role_totals) if role_totals and len(role_currency) <= 1 else None
            )
        incomplete = not roles or any(role.coverage in {"partial", "unknown"} for role in roles)
        partial += int(incomplete)
        costs.append(sum(totals) if totals and len(currency) <= 1 and not incomplete else None)
    return GroupMetrics(
        variant=variant,
        mode=mode,
        scheduled_trials=scheduled,
        attempts=len(attempts),
        completed=sum(item.stage == TrialStage.FINISHED for item in attempts),
        scored=sum(item.completion != "indeterminate" for item in results),
        eligible=len(eligible),
        audit_excluded=sum(
            any(
                reason != "unscorable"
                for reason in (
                    *item.current_exclusions,
                    *(item.result.exclusion_reasons if item.result else ()),
                )
            )
            for item in attempts
        ),
        exclusion_reasons=dict(reasons),
        eligible_passes=sum(item.completion == "pass" for item in eligible),
        eligible_completion_rate=sum(item.completion == "pass" for item in eligible) / len(eligible)
        if eligible
        else None,
        subjects_reporting_completion=sum(
            item.subject_observation == "completed" for item in results
        ),
        durations=distribution(item.subject_seconds for item in eligible),
        input_tokens=distribution(item.roles[0].known_tokens.uncached_input for item in eligible),
        output_tokens=distribution(item.roles[0].known_tokens.output for item in eligible),
        accepted_turns=distribution(item.accepted_turns for item in eligible),
        known_spend_by_currency=dict(spend),
        unknown_cost_records=unknown,
        partial_cost_attempts=partial,
        costs=distribution(costs),
        cache_read_tokens=distribution(
            tokens.cache_read if tokens else None for tokens in subject_tokens
        ),
        cache_write_tokens=distribution(
            tokens.cache_write if tokens else None for tokens in subject_tokens
        ),
        total_input_tokens=distribution(
            _total_input(tokens) if tokens else None for tokens in subject_tokens
        ),
        criteria=_criteria_summary(latest_eligible),
        checks=_checks_summary(latest_eligible),
        metrics=_metrics_summary(latest_eligible),
        simulator_replies=distribution(
            item.help_needed.simulator_replies if item.help_needed else None
            for item in latest_eligible
        ),
        cost_by_role={role: distribution(values) for role, values in role_costs.items()},
    )


def group_metrics(
    attempts: tuple[AttemptReport, ...],
    scheduled: dict[str, int] | None = None,
    mode: str = "pending",
) -> tuple[GroupMetrics, ...]:
    groups: dict[tuple[str, str], list[AttemptReport]] = defaultdict(list)
    for item in attempts:
        groups[
            (item.variant or "unknown", item.result.mode if item.result else item.mode or "pending")
        ].append(item)
    for variant in scheduled or {}:
        if not any(key[0] == variant for key in groups):
            groups[(variant, mode)] = []
    return tuple(
        _group(variant, mode, items, scheduled.get(variant) if scheduled else None)
        for (variant, mode), items in sorted(groups.items())
    )


def report_run(
    store: ObjectStore, reference: str, *, journal_limit: int | None = None
) -> RunReport:
    if not re.fullmatch(r"[0-9a-f]{32}", reference):
        report = store.load(reference, RunReport, kind=ObjectKind.PORTABLE_REPORT)
        return report
    journal = RunStore(store.root).inspect(reference, limit=journal_limit)
    execution = ExecutionJournal(journal)
    experiment, input_error = inspect_experiment(store, journal.metadata.experiment_id)
    trials = {item.trial_id: item for item in experiment.trials} if experiment else {}
    rows = []
    for state in execution.attempts.values():
        result = load_assessment(store, state.assessment_id) if state.assessment_id else None
        raw: Literal["available", "invalid", "unavailable"] = "unavailable"
        current = ("current_inputs_invalid",) if input_error else ()
        capture_id = state.capture_id or state.quarantine_id
        if capture_id:
            try:
                store.verify(capture_id)
                raw = "available"
            except DryheaveError:
                raw = "invalid"
                current = (*current, "current_capture_invalid")
        rows.append(
            AttemptReport(
                attempt_id=state.attempt_id,
                trial_id=state.trial_id,
                variant=trials[state.trial_id].variant
                if state.trial_id in trials
                else result.variant
                if result
                else None,
                stage=state.stage.value,
                assessment_id=state.assessment_id,
                capture_id=state.capture_id,
                quarantine_id=state.quarantine_id,
                pairing_id=_pair_identity(
                    trials.get(state.trial_id) or result,
                    experiment.simulator_id
                    if experiment
                    else result.simulator_id
                    if result
                    else None,
                    experiment.scoring_id if experiment else result.scoring_id if result else None,
                    execution.options.mode if execution.options else None,
                ),
                result=result,
                current_exclusions=current,
                raw_evidence=raw,
                mode=execution.options.mode if execution.options else None,
                roles=result.roles
                if result and result.phase == "finished"
                else role_metrics(
                    load_assessment_inputs(store, execution, state),
                    state,
                    saved_judge_calls(execution, state),
                ),
            )
        )
    attempted = {item.trial_id for item in rows}
    scheduled = dict(Counter(item.variant for item in trials.values())) if experiment else None
    return RunReport(
        run_id=reference,
        experiment_id=journal.metadata.experiment_id,
        compatibility_id=digest(
            canonical_json(
                {
                    "cases": ",".join(sorted(experiment.case_ids)),
                    "simulator": experiment.simulator_id,
                    "scoring": experiment.scoring_id,
                    "seed": experiment.seed,
                    "repetitions": experiment.repetitions,
                    "mode": execution.options.mode if execution.options else None,
                }
            )
        )
        if experiment
        else None,
        durable_sequence=journal.sequence,
        scheduled_trials=len(trials) if experiment else None,
        unstarted_trials=tuple(item for item in trials if item not in attempted),
        input_error=input_error,
        attempts=tuple(rows),
        groups=group_metrics(
            tuple(rows), scheduled, execution.options.mode if execution.options else "pending"
        ),
    )


def _pair_identity(
    identity: TrialSpec | Assessment | None,
    simulator_id: str | None,
    scoring_id: str | None,
    mode: str | None,
) -> str | None:
    if identity is None:
        return None
    values: dict[str, JsonValue] = {
        "case": identity.case_id,
        "simulator": simulator_id,
        "scoring": scoring_id,
        "repetition": identity.repetition,
        "seed": identity.seed,
        "mode": mode,
    }
    return None if None in values.values() else digest(canonical_json(values))


def _pairs(report: RunReport, variant: str | None) -> dict[tuple[object, ...], AttemptReport]:
    latest = {item.trial_id: item for item in report.attempts}
    pairs: dict[tuple[object, ...], AttemptReport] = {}
    for item in latest.values():
        if variant and item.variant != variant:
            continue
        result = item.result
        identity = item.pairing_id or (
            _pair_identity(result, result.simulator_id, result.scoring_id, result.mode)
            if result
            else None
        )
        pairs[(identity or item.trial_id, None if variant else item.variant)] = item
    return pairs


def compare_runs(
    store: ObjectStore,
    before: str,
    after: str,
    *,
    before_variant: str | None = None,
    after_variant: str | None = None,
    journal_limit: int | None = None,
) -> Comparison:
    if bool(before_variant) != bool(after_variant):
        raise InputError("Select both comparison variants, or neither.")
    before_reference = before if re.fullmatch(r"[0-9a-f]{32}", before) else store.resolve(before)
    after_reference = after if re.fullmatch(r"[0-9a-f]{32}", after) else store.resolve(after)
    left = report_run(store, before_reference, journal_limit=journal_limit)
    right = report_run(store, after_reference, journal_limit=journal_limit)
    earlier, later = _pairs(left, before_variant), _pairs(right, after_variant)
    signatures_known = left.compatibility_id is not None and right.compatibility_id is not None
    incompatible = (
        left.compatibility_id != right.compatibility_id
        if signatures_known
        else set(earlier) != set(later)
    )
    if not earlier or not later or incompatible:
        raise InputError(
            "Comparison requires matching frozen case/criteria/simulator/scoring, repetitions, seeds and modes; select compatible variants explicitly."
        )
    pairs = []
    for key, row in earlier.items():
        changed = later.get(key)
        if changed is None:
            continue
        first, second = row.result, changed.result
        if (
            first is None
            or second is None
            or first.criteria_id != second.criteria_id
            or not first.eligible
            or not second.eligible
            or row.current_exclusions
            or changed.current_exclusions
        ):
            continue
        if first.case_id is None or first.repetition is None:
            raise InputError("Pair identities are incomplete.")
        criteria = {item.criterion_id: item.outcome for item in first.criteria}
        pairs.append(
            PairChange(
                case_id=first.case_id,
                repetition=first.repetition,
                before_attempt=first.attempt_id,
                after_attempt=second.attempt_id,
                before_completion=first.completion,
                after_completion=second.completion,
                criterion_changes={
                    item.criterion_id: (criteria[item.criterion_id], item.outcome)
                    for item in second.criteria
                },
            )
        )
    return Comparison(
        before=left.run_id,
        after=right.run_id,
        before_input=before,
        after_input=after,
        before_reference=before_reference,
        after_reference=after_reference,
        before_sequence=left.durable_sequence,
        after_sequence=right.durable_sequence,
        before_variant=before_variant,
        after_variant=after_variant,
        pairs=tuple(pairs),
        paired_count=len(pairs),
        excluded_before=sum(
            not item.result or not item.result.eligible or bool(item.current_exclusions)
            for item in earlier.values()
        ),
        excluded_after=sum(
            not item.result or not item.result.eligible or bool(item.current_exclusions)
            for item in later.values()
        ),
        before_metrics=tuple(
            group
            for group in left.groups
            if before_variant is None or group.variant == before_variant
        ),
        after_metrics=tuple(
            group
            for group in right.groups
            if after_variant is None or group.variant == after_variant
        ),
    )
