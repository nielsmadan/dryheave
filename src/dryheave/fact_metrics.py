import math
import statistics
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

from annotated_types import Le

from dryheave.cases import CaseContent, TaskFact, graded_facts, requirement_facts
from dryheave.judges import FactInspection, FactVerdict, RaiseEvent
from dryheave.models import Name
from dryheave.report_models import Distribution, FactGroup, FactRow
from dryheave.result_models import (
    Assessment,
    AttemptOutcome,
    FactAccount,
    FactCredit,
    FactProgress,
    FactResult,
    OutcomeReason,
    PhaseCorrections,
)
from dryheave.runner_models import RunBudget
from dryheave.workflow import blocking_phase

INDEPENDENCE_CREDIT: Mapping[FactCredit, float] = MappingProxyType(
    {"own": 1.0, "elicited": 0.75, "corrected": 0.0, "unmet": 0.0}
)
BUDGET_STOPS = frozenset({"turn_budget", "stand_in_budget", "controller_budget"})
SUBJECT_STOPS = BUDGET_STOPS | {"timeout", "stand_in_stop", "workflow_complete"}
BUDGET_FACTOR = 1.5
BUDGET_MARGIN = 5


@dataclass(frozen=True)
class Exchange:
    raised: bool
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class AttemptFacts:
    case: CaseContent
    results: Mapping[str, FactResult]
    inspections: Sequence[FactInspection]
    raises: Sequence[RaiseEvent]
    exchanges: Sequence[Exchange]
    workflow: bool


def reached_phases(
    case: CaseContent, verdict_sets: Iterable[Mapping[str, FactVerdict]], *, workflow: bool
) -> tuple[Name, ...]:
    if not workflow:
        return case.phases
    last = len(case.phases) - 1
    reached = max(
        (
            last
            if (blocker := blocking_phase(case, verdicts)) is None
            else case.phases.index(blocker)
            for verdicts in verdict_sets
        ),
        default=0,
    )
    return case.phases[: reached + 1]


def inspection_verdicts(
    inspections: Iterable[FactInspection],
) -> tuple[dict[str, FactVerdict], ...]:
    return tuple(
        {item.fact_id: item.verdict for item in inspection.judgments}
        for inspection in inspections
        if inspection.status == "completed"
    )


def elicited_facts(case: CaseContent, exchanges: Iterable[Exchange]) -> frozenset[str]:
    eligible = {fact.fact_id for fact in requirement_facts(case) if fact.disclosure != "proactive"}
    raised: set[str] = set()
    elicited: set[str] = set()
    for exchange in exchanges:
        if exchange.raised:
            raised.update(exchange.fact_ids)
        else:
            elicited.update(
                fact_id
                for fact_id in exchange.fact_ids
                if fact_id in eligible and fact_id not in raised
            )
    return frozenset(elicited)


def _credit(
    fact: TaskFact,
    result: FactResult | None,
    *,
    raised: Collection[str],
    elicited: Collection[str],
    reached: Collection[str],
) -> FactCredit:
    if fact.phase not in reached:
        return "unreached"
    if result is None or result.verdict != "met":
        return "unmet"
    if fact.fact_id in raised:
        return "corrected"
    return "elicited" if fact.fact_id in elicited else "own"


def fact_credits(
    facts: Iterable[TaskFact],
    results: Mapping[str, FactResult],
    raised: Collection[str],
    elicited: Collection[str],
    reached_phases: Collection[str],
) -> dict[Name, FactCredit]:
    return {
        fact.fact_id: _credit(
            fact,
            results.get(fact.fact_id),
            raised=raised,
            elicited=elicited,
            reached=reached_phases,
        )
        for fact in facts
        if fact.graded
    }


def scored_facts(case: CaseContent) -> tuple[TaskFact, ...]:
    return tuple(fact for fact in requirement_facts(case) if fact.disclosure != "proactive")


def independence_score(credits: Mapping[Name, FactCredit]) -> float | None:
    scored = [INDEPENDENCE_CREDIT[credit] for credit in credits.values() if credit != "unreached"]
    return statistics.fmean(scored) if scored else None


def regressions(inspections: Sequence[FactInspection]) -> dict[str, int]:
    completed = [item for item in inspections if item.status == "completed"]
    counts: Counter[str] = Counter()
    for before, after in pairwise(completed):
        if before.snapshot_digest == after.snapshot_digest:
            continue
        met = {item.fact_id for item in before.judgments if item.verdict == "met"}
        counts.update(
            item.fact_id
            for item in after.judgments
            if item.verdict == "unmet" and item.fact_id in met
        )
    return dict(counts)


def first_inspection(inspections: Iterable[FactInspection]) -> dict[str, FactVerdict]:
    return next(iter(inspection_verdicts(inspections)), {})


def corrections(case: CaseContent, raises: Sequence[RaiseEvent]) -> tuple[PhaseCorrections, ...]:
    return tuple(
        PhaseCorrections(
            phase=phase,
            rounds=sum(event.phase == phase for event in raises),
            raised=tuple(
                dict.fromkeys(
                    fact_id
                    for event in raises
                    if event.phase == phase
                    for fact_id in event.fact_ids
                )
            ),
        )
        for phase in case.phases
        if any(event.phase == phase for event in raises)
    )


def raise_counts(raises: Iterable[RaiseEvent]) -> Counter[str]:
    return Counter(fact_id for event in raises for fact_id in dict.fromkeys(event.fact_ids))


def attempt_reached(attempt: AttemptFacts) -> tuple[Name, ...]:
    return reached_phases(
        attempt.case, inspection_verdicts(attempt.inspections), workflow=attempt.workflow
    )


def fact_account(attempt: AttemptFacts) -> FactAccount:
    case = attempt.case
    reached = attempt_reached(attempt)
    raised = raise_counts(attempt.raises)
    elicited = elicited_facts(case, attempt.exchanges)
    credits = fact_credits(graded_facts(case), attempt.results, raised, elicited, reached)
    first = first_inspection(attempt.inspections)
    regressed = regressions(attempt.inspections)
    scored = {fact.fact_id for fact in scored_facts(case)}
    return FactAccount(
        independence=independence_score(
            {fact_id: credit for fact_id, credit in credits.items() if fact_id in scored}
        )
        if _all_settled(attempt.results, credits, scored)
        else None,
        stand_in_replies=sum(not exchange.raised for exchange in attempt.exchanges),
        fix_rounds=len(attempt.raises),
        reached_phases=reached,
        unreached_phases=tuple(phase for phase in case.phases if phase not in reached),
        corrections=corrections(case, attempt.raises),
        facts=tuple(
            FactProgress(
                fact_id=fact.fact_id,
                kind=fact.kind,
                phase=fact.phase or "",
                credit=credits[fact.fact_id],
                raised=raised[fact.fact_id],
                elicited=fact.fact_id in elicited,
                first_inspection=first.get(fact.fact_id),
                regressions=regressed.get(fact.fact_id, 0),
            )
            for fact in graded_facts(case)
        ),
    )


def _all_settled(
    results: Mapping[str, FactResult], credits: Mapping[Name, FactCredit], scored: Collection[str]
) -> bool:
    return all(
        fact_id in results and results[fact_id].verdict in {"met", "unmet"}
        for fact_id, credit in credits.items()
        if fact_id in scored and credit != "unreached"
    )


def attempt_outcome(
    case: CaseContent,
    results: Mapping[str, FactResult],
    reached: Collection[str],
    stop_reason: str,
) -> tuple[AttemptOutcome, OutcomeReason]:
    requirements = requirement_facts(case)
    verdicts = {
        fact.fact_id: results[fact.fact_id].verdict if fact.fact_id in results else None
        for fact in requirements
    }
    unmet = [fact for fact in requirements if verdicts[fact.fact_id] == "unmet"]
    budget = stop_reason in BUDGET_STOPS
    unobserved = stop_reason not in SUBJECT_STOPS
    if any(fact.phase in reached for fact in unmet) or (unmet and not unobserved):
        return "fail", "budget_exhausted" if budget else "unmet"
    if None in verdicts.values():
        return "indeterminate", "grading_error"
    if "disputed" in verdicts.values():
        return "indeterminate", "disputed"
    if "unclear" in verdicts.values():
        return "indeterminate", "unclear"
    if unmet:
        return "indeterminate", "phase_unreached"
    return "pass", "all_met"


def derived_figure(used: int) -> int:
    return math.ceil(used * BUDGET_FACTOR) + BUDGET_MARGIN


def derived_budget(*, turns: int, stand_in_replies: int, inspections: int) -> RunBudget:
    limits = RunBudget.model_fields
    return RunBudget(
        max_turns=min(derived_figure(turns), _maximum(limits["max_turns"].metadata)),
        stand_in_replies=min(
            derived_figure(stand_in_replies), _maximum(limits["stand_in_replies"].metadata)
        ),
        max_inspections=min(
            derived_figure(inspections), _maximum(limits["max_inspections"].metadata)
        ),
    )


def _maximum(metadata: Sequence[object]) -> int:
    return next(int(str(item.le)) for item in metadata if isinstance(item, Le))


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _distribution(values: Sequence[float]) -> Distribution:
    return Distribution(
        count=len(values),
        unknown=0,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        mean=statistics.fmean(values) if values else None,
        median=statistics.median(values) if values else None,
        stdev=statistics.stdev(values) if len(values) > 1 else None,
    )


def _fact_row(case_id: str, fact_id: str, results: Sequence[Assessment]) -> FactRow:
    progress = [
        item
        for result in results
        if result.fact_account
        for item in result.fact_account.facts
        if item.fact_id == fact_id
    ]
    verdicts = Counter(
        item.verdict for result in results for item in result.facts if item.fact_id == fact_id
    )
    credits = Counter(item.credit for item in progress)
    raised = sum(item.raised > 0 for item in progress)
    inspected = [item.first_inspection for item in progress if item.first_inspection is not None]
    first_met = sum(verdict == "met" for verdict in inspected)
    return FactRow(
        case_id=case_id,
        fact_id=fact_id,
        kind=progress[0].kind,
        phase=progress[0].phase or None,
        attempts=len(progress),
        met=verdicts["met"],
        unmet=verdicts["unmet"],
        unclear=verdicts["unclear"],
        disputed=verdicts["disputed"],
        own=credits["own"],
        elicited=credits["elicited"],
        corrected=credits["corrected"],
        unreached=credits["unreached"],
        raised=raised,
        raised_rate=_rate(raised, len(progress)),
        first_inspection_met=first_met,
        first_inspected=len(inspected),
        first_inspection_met_rate=_rate(first_met, len(inspected)),
        regressions=sum(item.regressions for item in progress),
    )


def fact_group(results: Sequence[Assessment], eligible: Sequence[Assessment]) -> FactGroup | None:
    if not results:
        return None
    accounted = [item for item in eligible if item.fact_account is not None]
    keys = dict.fromkeys(
        (case_id, progress.fact_id)
        for result in accounted
        if (case_id := result.graded_case_id or result.case_id) is not None
        and result.fact_account is not None
        for progress in result.fact_account.facts
    )
    return FactGroup(
        attempts=len(results),
        outcomes=dict(Counter(item.outcome for item in results)),
        outcome_reasons=dict(
            Counter(item.outcome_reason for item in results if item.outcome_reason)
        ),
        pass_rate=_rate(sum(item.outcome == "pass" for item in eligible), len(eligible)),
        independence=_distribution(
            [
                item.fact_account.independence
                for item in accounted
                if item.fact_account and item.fact_account.independence is not None
            ]
        ),
        corrections=_distribution(
            [float(item.fact_account.fix_rounds) for item in accounted if item.fact_account]
        ),
        unreached_attempts=sum(
            bool(item.fact_account.unreached_phases) for item in accounted if item.fact_account
        ),
        uncovered=sum(len(item.uncovered) for item in eligible),
        facts=tuple(
            _fact_row(
                case_id,
                fact_id,
                [item for item in accounted if (item.graded_case_id or item.case_id) == case_id],
            )
            for case_id, fact_id in keys
        ),
    )
