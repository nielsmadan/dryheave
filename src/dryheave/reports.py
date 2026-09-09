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
from dryheave.models import ObjectKind, TrialStage
from dryheave.report_models import (
    AttemptReport,
    Comparison,
    Distribution,
    GroupMetrics,
    PairChange,
    RunReport,
)
from dryheave.result_models import Assessment
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, digest
from dryheave.storage import ObjectStore


def distribution(values: Iterable[float | int | None]) -> Distribution:
    numbers = list(values)
    observed = [float(value) for value in numbers if value is not None]
    return Distribution(
        count=len(observed),
        unknown=len(numbers) - len(observed),
        minimum=min(observed) if observed else None,
        maximum=max(observed) if observed else None,
        mean=statistics.mean(observed) if observed else None,
        median=statistics.median(observed) if observed else None,
        stdev=statistics.stdev(observed) if len(observed) > 1 else None,
    )


def _group(
    variant: str, mode: str, attempts: list[AttemptReport], scheduled: int | None
) -> GroupMetrics:
    results = [item.result for item in attempts if item.result is not None]
    eligible = [
        item.result
        for item in attempts
        if item.result and item.result.eligible and not item.current_exclusions
    ]
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
    for attempt in attempts:
        roles = attempt.roles or (attempt.result.roles if attempt.result else ())
        totals = []
        currency = set()
        for role in roles:
            for record in role.records:
                if record.cost is not None:
                    if record.cost or record.currency:
                        spend[record.currency or "UNSPECIFIED"] += record.cost
                    totals.append(record.cost)
                    if record.cost or record.currency:
                        currency.add(record.currency)
                else:
                    unknown += 1
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


def report_run(store: ObjectStore, reference: str) -> RunReport:
    if not re.fullmatch(r"[0-9a-f]{32}", reference):
        report = store.load(reference, RunReport, kind=ObjectKind.PORTABLE_REPORT)
        return report
    journal = RunStore(store.root).inspect(reference)
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
) -> Comparison:
    if bool(before_variant) != bool(after_variant):
        raise InputError("Select both comparison variants, or neither.")
    before_reference = before if re.fullmatch(r"[0-9a-f]{32}", before) else store.resolve(before)
    after_reference = after if re.fullmatch(r"[0-9a-f]{32}", after) else store.resolve(after)
    left, right = report_run(store, before_reference), report_run(store, after_reference)
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
