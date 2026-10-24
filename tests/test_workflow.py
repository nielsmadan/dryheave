from datetime import UTC, datetime

import pytest

from dryheave.cases import CaseContent, FactVerifier, PromptStep, TaskFact
from dryheave.errors import MissingSkillError
from dryheave.judges import FactInspection, FactJudgment, RaiseEvent
from dryheave.models import CommandSpec, JournalEvent
from dryheave.workflow import (
    InspectionRecord,
    blocking_phase,
    dialog_source,
    inspection_records,
    inspection_verdict,
    missing_skills,
    next_workflow_action,
    raise_events,
    raised_facts,
    require_workflow_skills,
    slash_steps,
    workflow_steps,
)

PROMPT = PromptStep(step_id="review", prompt="/code-review --quick", after_phase="fix")
DOCS = PromptStep(step_id="docs", prompt="Update the docs.", after_phase="fix")
SUMMARY = PromptStep(step_id="summary", prompt="Summarize.", after_phase="docs")


def fact(fact_id: str, phase: str, kind: str = "requirement") -> TaskFact:
    return TaskFact(
        fact_id=fact_id,
        kind=kind,
        phase=phase,
        text=f"{fact_id} text",
        check=f"{fact_id} check",
        curator_authored=True,
    )


COMMA = fact("comma", "fix")
TONE = fact("tone", "fix", "optional")
README = fact("readme", "docs")
STYLE = fact("style", "docs", "optional")
CASE = CaseContent(
    schema_version=2,
    title="t",
    initial_prompt="p",
    phases=("fix", "docs"),
    allowed_facts=(COMMA, TONE, README, STYLE),
    workflow=(DOCS, SUMMARY),
)


@pytest.mark.parametrize(
    ("position", "verdicts", "expected"),
    [
        (0, {"comma": "unmet", "readme": "unmet"}, "raise"),
        (0, {"comma": "met", "readme": "unmet"}, "deliver_prompt"),
        (0, {"comma": "unclear", "readme": "unmet"}, "deliver_prompt"),
        (1, {"comma": "met", "readme": "unmet"}, "raise"),
        (1, {"comma": "unmet", "readme": "met"}, "raise"),
        (1, {"comma": "met", "readme": "met"}, "deliver_prompt"),
        (2, {"comma": "met", "readme": "unclear"}, "complete"),
        (2, {"comma": "met", "readme": "met", "tone": "unmet"}, "complete"),
        (2, {"comma": "met", "readme": "unmet"}, "raise"),
    ],
)
def test_next_workflow_action_table(position, verdicts, expected) -> None:
    assert next_workflow_action(CASE, position, verdicts) == expected


def test_next_workflow_action_rejects_negative_position() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        next_workflow_action(CASE, -1, {})


def test_the_earliest_phase_with_an_unmet_requirement_blocks() -> None:
    assert blocking_phase(CASE, {"comma": "unmet", "readme": "unmet"}) == "fix"
    assert blocking_phase(CASE, {"comma": "met", "readme": "unmet", "tone": "unmet"}) == "docs"
    assert blocking_phase(CASE, {"comma": "unclear", "readme": "met", "style": "unmet"}) is None


def test_raised_facts_are_the_unmet_requirements_and_optionals_of_one_phase() -> None:
    verdicts = {"comma": "unmet", "tone": "unmet", "readme": "unmet", "style": "met"}
    assert raised_facts(CASE, "fix", verdicts) == (COMMA, TONE)
    assert raised_facts(CASE, "docs", verdicts) == (README,)


VERIFIED = CASE.model_copy(
    update={
        "allowed_facts": (
            COMMA.model_copy(
                update={
                    "verifier": FactVerifier(
                        command=CommandSpec(argv=("python3", "{verifier}/comma.py")),
                        entrypoint="comma.py",
                        expected_stdout="CHECK_RAN",
                    )
                }
            ),
            TONE,
            README,
            STYLE,
        )
    }
)


def inspection(status: str = "completed", **verdicts: str) -> FactInspection:
    return FactInspection(
        inspection_id="i-1",
        turn=1,
        snapshot_digest="d" if status == "completed" else None,
        input_digest="d" if status == "completed" else None,
        status=status,
        error=None if status == "completed" else "deadline_exhausted",
        judgments=tuple(
            FactJudgment(fact_id=key, verdict=value, observation="seen", evidence="e")
            for key, value in verdicts.items()
        ),
    )


@pytest.mark.parametrize(
    ("case", "result", "expected"),
    [
        (VERIFIED, inspection(comma="met", readme="unmet"), "passed"),
        (VERIFIED, inspection(comma="unmet", readme="unmet"), "failed"),
        (VERIFIED, inspection(comma="unclear", readme="unmet"), "unavailable"),
        (CASE, inspection(comma="unclear", readme="unmet"), "passed"),
        (VERIFIED, inspection("unavailable"), "unavailable"),
    ],
    ids=["met", "raise", "unchecked-verifier", "unclear-judged-fact", "unavailable"],
)
def test_inspection_verdict_treats_an_unchecked_verifier_requirement_as_unavailable(
    case, result, expected
) -> None:
    assert inspection_verdict(case, 0, result) == expected


def test_workflow_steps_are_the_prompt_steps_only_when_enabled() -> None:
    assert workflow_steps(CASE, enabled=True) == (DOCS, SUMMARY)
    assert workflow_steps(CASE, enabled=False) == ()


def test_dialog_source_is_the_latest_prompt_step_before_the_end():
    review = PromptStep(
        step_id="review", prompt="/code-review --quick", answer_hint="Fix them.", after_phase="fix"
    )
    steps = (review, DOCS)
    assert [dialog_source(steps, end) for end in range(4)] == [None, review, DOCS, DOCS]


def event(sequence: int, name: str, data: dict, attempt_id: str = "a-1") -> JournalEvent:
    return JournalEvent(
        sequence=sequence,
        previous_hash=None,
        timestamp=datetime(2026, 10, 23, tzinfo=UTC),
        event=name,
        attempt_id=attempt_id,
        trial_id="t-1",
        data=data,
    )


def test_inspection_records_pair_each_intent_with_its_result_or_leave_it_unknown() -> None:
    finished = FactInspection(
        inspection_id="i-1",
        turn=2,
        snapshot_digest="s",
        input_digest="d",
        status="completed",
        judgments=(FactJudgment(fact_id="comma", verdict="met", observation="", evidence="x"),),
    )
    events = (
        event(1, "inspection-intent", {"inspection_id": "i-1", "turn": 2, "private": []}),
        event(2, "fact-inspection", finished.model_dump(mode="json")),
        event(3, "inspection-intent", {"inspection_id": "i-9", "turn": 1}, attempt_id="a-2"),
        event(4, "inspection-intent", {"inspection_id": "i-2", "turn": 4, "private": []}),
    )
    assert inspection_records(events, "a-1") == (
        InspectionRecord("i-1", 2, finished),
        InspectionRecord("i-2", 4, None),
    )


def test_raise_events_are_read_back_for_one_attempt() -> None:
    raised = RaiseEvent(
        call_id="c-1", phase="fix", fact_ids=("comma",), cited=("tone",), cause="fallback", turn=3
    )
    events = (
        event(1, "raise", raised.model_dump(mode="json")),
        event(2, "raise", raised.model_dump(mode="json"), attempt_id="a-2"),
    )
    assert raise_events(events, "a-1") == (raised,)


def test_missing_skills_names_every_skill_name_without_a_skill_once() -> None:
    names = ("code-review", "release-notes", "simplify", "release-notes")
    assert missing_skills(names, ("code-review",)) == ("release-notes", "simplify")
    assert missing_skills(names, ("code-review", "release-notes", "simplify")) == ()


def test_slash_steps_keep_only_slash_prompt_steps_in_order() -> None:
    notes = PromptStep(step_id="notes", prompt=" \n/release-notes", after_phase="fix")
    steps = (
        PROMPT,
        PromptStep(step_id="docs", prompt="Update the docs.", after_phase="fix"),
        notes,
    )
    assert slash_steps(steps) == (PROMPT, notes)


def test_workflow_skills_refusal_names_every_blocked_variant_and_skill() -> None:
    require_workflow_skills("Codex", {"luna": ("code-review",)}, ("code-review",))
    with pytest.raises(MissingSkillError) as raised:
        require_workflow_skills(
            "Codex",
            {
                "luna": ("code-review", "release-notes"),
                "terra": ("code-review",),
                "sol": ("simplify",),
            },
            ("code-review",),
        )
    assert str(raised.value) == (
        "Variants luna, sol cannot run workflow skills 'release-notes', 'simplify': "
        "the Codex config has no skills/release-notes/SKILL.md, skills/simplify/SKILL.md."
    )


def test_workflow_skills_refusal_for_one_variant_and_skill_names_the_agent_config() -> None:
    with pytest.raises(MissingSkillError) as raised:
        require_workflow_skills("Claude", {"terra": ("code-review",)}, ())
    assert str(raised.value) == (
        "Variant terra cannot run workflow skill 'code-review': "
        "the Claude config has no skills/code-review/SKILL.md."
    )
