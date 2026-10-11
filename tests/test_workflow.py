import pytest

from dryheave.cases import InspectStep, PromptStep
from dryheave.workflow import dialog_source, next_workflow_action

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
