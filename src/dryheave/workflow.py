from typing import Literal

from dryheave.cases import PromptStep, WorkflowStep

type WorkflowAction = Literal["deliver_prompt", "inspect", "complete"]


def next_workflow_action(steps: tuple[WorkflowStep, ...], position: int) -> WorkflowAction:
    if position < 0:
        raise ValueError(f"workflow position must not be negative: {position}")
    if position >= len(steps):
        return "complete"
    return "deliver_prompt" if isinstance(steps[position], PromptStep) else "inspect"
