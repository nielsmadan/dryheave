import pytest

from dryheave.cases import CaseContent, TaskFact
from dryheave.fact_metrics import (
    AttemptFacts,
    Exchange,
    attempt_outcome,
    corrections,
    derived_budget,
    elicited_facts,
    fact_account,
    fact_credits,
    first_inspection,
    independence_score,
    reached_phases,
    regressions,
)
from dryheave.judges import FactInspection, FactJudgment, RaiseEvent
from dryheave.result_models import FactResult
from dryheave.runner_models import RunBudget


def fact(fact_id, phase="fix", kind="requirement", disclosure="on_request"):
    return TaskFact(
        fact_id=fact_id,
        kind=kind,
        phase=phase,
        text=f"The user wants {fact_id}.",
        check=f"The work shows {fact_id}.",
        disclosure=disclosure,
        curator_authored=True,
    )


CASE = CaseContent(
    schema_version=2,
    title="Greeting",
    initial_prompt="Fix the greeting.",
    phases=("fix", "docs"),
    allowed_facts=(
        fact("comma"),
        fact("name"),
        fact("tone", disclosure="proactive"),
        fact("style", kind="optional"),
        fact("readme", phase="docs"),
    ),
)


def result(fact_id, verdict="met", phase="fix"):
    return FactResult(
        fact_id=fact_id,
        kind="requirement",
        phase=phase,
        verdict=verdict,
        observation="seen",
        evidence="greet.py:2",
        source="judge",
    )


def results(**verdicts):
    phases = {item.fact_id: item.phase for item in CASE.allowed_facts}
    return {
        fact_id: result(fact_id, verdict, phases[fact_id]) for fact_id, verdict in verdicts.items()
    }


def inspection(identifier, digest, **verdicts):
    return FactInspection(
        inspection_id=identifier,
        turn=1,
        snapshot_digest=digest,
        input_digest=identifier,
        status="completed",
        judgments=tuple(
            FactJudgment(fact_id=fact_id, verdict=verdict, observation="seen", evidence="x")
            for fact_id, verdict in verdicts.items()
        ),
    )


def raised(*fact_ids, phase="fix", cause="fix_request"):
    return RaiseEvent(call_id="s-1", phase=phase, fact_ids=fact_ids, cause=cause, turn=1)


def test_credits_split_own_elicited_corrected_unmet_and_unreached():
    credits = fact_credits(
        CASE.allowed_facts,
        results(comma="met", name="met", tone="met", style="unmet", readme="met"),
        raised={"tone"},
        elicited={"name", "tone"},
        reached_phases=("fix",),
    )
    assert credits == {
        "comma": "own",
        "name": "elicited",
        "tone": "corrected",
        "style": "unmet",
        "readme": "unreached",
    }


def test_a_fact_elicited_and_later_raised_counts_as_corrected():
    exchanges = (
        Exchange(raised=False, fact_ids=("comma",)),
        Exchange(raised=True, fact_ids=("comma",)),
    )
    elicited = elicited_facts(CASE, exchanges)
    assert elicited == {"comma"}
    credits = fact_credits(
        CASE.allowed_facts, results(comma="met"), {"comma"}, elicited, ("fix", "docs")
    )
    assert credits["comma"] == "corrected"


def test_a_citation_after_the_fact_was_raised_is_not_elicited_and_proactive_never_is():
    exchanges = (
        Exchange(raised=True, fact_ids=("comma",)),
        Exchange(raised=False, fact_ids=("comma", "tone", "style", "name")),
    )
    assert elicited_facts(CASE, exchanges) == {"name"}


def test_independence_scores_elicited_at_three_quarters_and_skips_unreached():
    assert independence_score({"a": "own", "b": "elicited", "c": "corrected", "d": "unmet"}) == (
        pytest.approx(1.75 / 4)
    )
    assert independence_score({"a": "elicited", "b": "unreached"}) == 0.75


def test_independence_is_none_without_a_scored_fact():
    assert independence_score({}) is None
    assert independence_score({"a": "unreached", "b": "unreached"}) is None


def test_reached_phases_follow_the_furthest_blocking_phase_and_all_without_workflow():
    first = {"comma": "unmet", "readme": "unmet"}
    later = {"comma": "met", "name": "met", "readme": "unmet"}
    assert reached_phases(CASE, (), workflow=True) == ("fix",)
    assert reached_phases(CASE, (first,), workflow=True) == ("fix",)
    assert reached_phases(CASE, (first, later), workflow=True) == ("fix", "docs")
    assert reached_phases(CASE, (later, first), workflow=True) == ("fix", "docs")
    assert reached_phases(CASE, (first,), workflow=False) == ("fix", "docs")


def test_regressions_count_met_to_unmet_only_when_the_snapshot_changed():
    inspections = (
        inspection("i-1", "d1", comma="met", name="met"),
        inspection("i-2", "d1", comma="unmet", name="met"),
        inspection("i-3", "d2", comma="unmet", name="unmet"),
        inspection("i-4", "d3", comma="met", name="met"),
        inspection("i-5", "d4", comma="unmet", name="unclear"),
    )
    assert regressions(inspections) == {"name": 1, "comma": 1}


def test_first_inspection_skips_unavailable_inspections():
    unavailable = FactInspection(
        inspection_id="i-0",
        turn=1,
        snapshot_digest=None,
        input_digest=None,
        status="unavailable",
        error="deadline_exhausted",
    )
    assert first_inspection(()) == {}
    assert first_inspection((unavailable, inspection("i-1", "d1", comma="unmet"))) == {
        "comma": "unmet"
    }


def test_corrections_group_rounds_and_raised_facts_by_phase():
    raises = (
        raised("comma", "name"),
        raised("comma", cause="fallback"),
        raised("readme", phase="docs"),
    )
    assert [(item.phase, item.rounds, item.raised) for item in corrections(CASE, raises)] == [
        ("fix", 2, ("comma", "name")),
        ("docs", 1, ("readme",)),
    ]


def test_fact_account_combines_credits_rounds_and_first_inspection():
    account = fact_account(
        AttemptFacts(
            case=CASE,
            results=results(comma="met", name="met", tone="met", style="met", readme="unmet"),
            inspections=(inspection("i-1", "d1", comma="unmet", name="met", readme="unmet"),),
            raises=(raised("comma"),),
            exchanges=(Exchange(raised=False, fact_ids=("name",)), Exchange(True, ("comma",))),
            workflow=True,
        )
    )
    assert account.reached_phases == ("fix",)
    assert account.unreached_phases == ("docs",)
    assert (account.stand_in_replies, account.fix_rounds) == (1, 1)
    assert account.independence == pytest.approx((0 + 0.75) / 2)
    by_id = {item.fact_id: item for item in account.facts}
    assert (by_id["comma"].credit, by_id["comma"].raised, by_id["comma"].first_inspection) == (
        "corrected",
        1,
        "unmet",
    )
    assert (by_id["tone"].credit, by_id["readme"].credit) == ("own", "unreached")


@pytest.mark.parametrize(
    "verdicts",
    [{}, {"comma": "met", "name": "unclear"}, {"comma": "met", "name": "disputed"}],
)
def test_fact_account_has_no_independence_when_a_scored_fact_has_no_settled_verdict(verdicts):
    account = fact_account(
        AttemptFacts(
            case=CASE,
            results=results(**verdicts),
            inspections=(),
            raises=(),
            exchanges=(),
            workflow=False,
        )
    )
    assert account.independence is None
    settled = fact_account(
        AttemptFacts(
            case=CASE,
            results=results(comma="met", name="met", readme="met"),
            inspections=(),
            raises=(),
            exchanges=(),
            workflow=False,
        )
    )
    assert settled.independence == 1.0


def test_fact_account_with_every_phase_unreached_has_no_independence():
    case = CASE.model_copy(update={"allowed_facts": (fact("readme", phase="docs"),)})
    account = fact_account(
        AttemptFacts(
            case=case,
            results={},
            inspections=(),
            raises=(),
            exchanges=(),
            workflow=True,
        )
    )
    assert (account.independence, account.reached_phases) == (None, ("fix",))


ALL_MET = {"comma": "met", "name": "met", "tone": "met", "readme": "met"}


@pytest.mark.parametrize(
    ("verdicts", "reached", "stop", "expected"),
    [
        (ALL_MET, ("fix", "docs"), "workflow_complete", ("pass", "all_met")),
        ({**ALL_MET, "comma": "unmet"}, ("fix",), "stand_in_stop", ("fail", "unmet")),
        ({**ALL_MET, "comma": "unmet"}, ("fix",), "turn_budget", ("fail", "budget_exhausted")),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "stand_in_budget",
            ("fail", "budget_exhausted"),
        ),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "inspection_budget",
            ("indeterminate", "phase_unreached"),
        ),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "inspection_unavailable",
            ("indeterminate", "phase_unreached"),
        ),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "delivery_uncertain",
            ("indeterminate", "phase_unreached"),
        ),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "question",
            ("indeterminate", "phase_unreached"),
        ),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "exited",
            ("indeterminate", "phase_unreached"),
        ),
        ({**ALL_MET, "readme": "unmet"}, ("fix",), "timeout", ("fail", "unmet")),
        ({**ALL_MET, "readme": "unmet"}, ("fix",), "stand_in_stop", ("fail", "unmet")),
        (
            {**ALL_MET, "readme": "unmet"},
            ("fix",),
            "controller_error",
            ("indeterminate", "phase_unreached"),
        ),
        ({**ALL_MET, "name": "disputed"}, ("fix", "docs"), "x", ("indeterminate", "disputed")),
        ({**ALL_MET, "name": "unclear"}, ("fix", "docs"), "x", ("indeterminate", "unclear")),
        (
            {key: value for key, value in ALL_MET.items() if key != "name"},
            ("fix", "docs"),
            "x",
            ("indeterminate", "grading_error"),
        ),
        (
            {**ALL_MET, "comma": "unmet", "name": "unclear"},
            ("fix", "docs"),
            "x",
            ("fail", "unmet"),
        ),
    ],
)
def test_attempt_outcome_rules(verdicts, reached, stop, expected):
    assert attempt_outcome(CASE, results(**verdicts), reached, stop) == expected


def test_an_unmet_optional_fact_never_fails_the_attempt():
    assert attempt_outcome(
        CASE, results(**ALL_MET, style="unmet"), ("fix", "docs"), "workflow_complete"
    ) == ("pass", "all_met")


def test_derived_budget_adds_half_and_five_and_stays_within_the_run_budget_bounds():
    assert derived_budget(turns=10, stand_in_replies=3, inspections=0) == RunBudget(
        max_turns=20, stand_in_replies=10, max_inspections=5
    )
    assert derived_budget(turns=7, stand_in_replies=70, inspections=999).stand_in_replies == 100
    assert derived_budget(turns=7, stand_in_replies=1, inspections=999).max_inspections == 1000
