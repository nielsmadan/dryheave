import pytest
from pydantic import ValidationError

from dryheave.runner_models import RunBudget, RunOptions, WorkflowEvent

BASE_KEYS = {"step_id", "kind", "outcome", "turn", "failed_criteria", "call_ids", "seconds"}


def test_workflow_event_drops_delivered_text_when_unset():
    event = WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=1)
    dumped = event.model_dump(mode="json")
    assert "delivered_text" not in dumped
    assert set(dumped) == BASE_KEYS


def test_workflow_event_with_delivered_text_round_trips():
    event = WorkflowEvent(
        step_id="review",
        kind="prompt",
        outcome="sent",
        turn=2,
        delivered_text="$code-review --quick",
    )
    dumped = event.model_dump(mode="json")
    assert set(dumped) == BASE_KEYS | {"delivered_text"}
    assert dumped["delivered_text"] == "$code-review --quick"
    assert WorkflowEvent.model_validate_json(event.model_dump_json()) == event


def test_run_options_always_record_the_budget_defaults():
    dumped = RunOptions(mode="offline-fixture").model_dump(mode="json")
    assert dumped["budget"] == {"max_turns": 60, "stand_in_replies": 40, "max_inspections": 20}
    assert RunOptions.model_validate(dumped).budget == RunBudget()


@pytest.mark.parametrize(
    "figures",
    [{"max_turns": 0}, {"stand_in_replies": 101}, {"max_inspections": 0}, {"turns": 3}],
)
def test_run_budget_rejects_out_of_range_or_unknown_figures(figures):
    with pytest.raises(ValidationError):
        RunBudget.model_validate(figures)
