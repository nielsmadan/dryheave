import pytest

from dryheave.cases import InspectStep, PromptStep
from dryheave.errors import MissingSkillError
from dryheave.workflow import (
    dialog_source,
    missing_skills,
    next_workflow_action,
    require_workflow_skills,
    slash_steps,
)

PROMPT = PromptStep(step_id="review", prompt="/code-review --quick")
INSPECT = InspectStep(step_id="inspect-required", criteria=("quality",))


@pytest.mark.parametrize(
    ("steps", "position", "expected"),
    [
        ((), 0, "complete"),
        ((PROMPT,), 0, "deliver_prompt"),
        ((INSPECT,), 0, "inspect"),
        ((PROMPT, INSPECT), 0, "deliver_prompt"),
        ((PROMPT, INSPECT), 1, "inspect"),
        ((PROMPT, INSPECT), 2, "complete"),
        ((PROMPT, INSPECT), 5, "complete"),
    ],
)
def test_next_workflow_action_table(steps, position, expected) -> None:
    assert next_workflow_action(steps, position) == expected


def test_next_workflow_action_rejects_negative_position() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        next_workflow_action((PROMPT, INSPECT), -1)


def test_dialog_source_is_the_latest_prompt_step_before_the_end():
    review = PromptStep(step_id="review", prompt="/code-review --quick", answer_hint="Fix them.")
    docs = PromptStep(step_id="docs", prompt="Update the docs.")
    steps = (InspectStep(step_id="first", criteria=("ok",)), review, docs)
    assert [dialog_source(steps, end) for end in range(4)] == [None, None, review, docs]


def test_missing_skills_names_every_skill_name_without_a_skill_once() -> None:
    names = ("code-review", "release-notes", "simplify", "release-notes")
    assert missing_skills(names, ("code-review",)) == ("release-notes", "simplify")
    assert missing_skills(names, ("code-review", "release-notes", "simplify")) == ()


def test_slash_steps_keep_only_slash_prompt_steps_in_order() -> None:
    notes = PromptStep(step_id="notes", prompt=" \n/release-notes")
    steps = (
        InspectStep(step_id="check", criteria=("quality",)),
        PROMPT,
        PromptStep(step_id="docs", prompt="Update the docs."),
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
