from collections.abc import Collection, Iterable, Mapping
from typing import Literal

from dryheave.cases import PromptStep, WorkflowStep
from dryheave.errors import MissingSkillError

type WorkflowAction = Literal["deliver_prompt", "inspect", "complete"]


def next_workflow_action(steps: tuple[WorkflowStep, ...], position: int) -> WorkflowAction:
    if position < 0:
        raise ValueError(f"workflow position must not be negative: {position}")
    if position >= len(steps):
        return "complete"
    return "deliver_prompt" if isinstance(steps[position], PromptStep) else "inspect"


def dialog_source(steps: tuple[WorkflowStep, ...], end: int) -> PromptStep | None:
    return next((step for step in reversed(steps[:end]) if isinstance(step, PromptStep)), None)


def slash_steps(steps: Iterable[WorkflowStep]) -> tuple[PromptStep, ...]:
    return tuple(step for step in steps if isinstance(step, PromptStep) and step.slash)


def missing_skills(skill_names: Iterable[str], available: Collection[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for name in skill_names if name not in available))


def skill_refusal(agent: str, variants: tuple[str, ...], skills: tuple[str, ...]) -> str:
    label = "Variant" if len(variants) == 1 else "Variants"
    noun = "skill" if len(skills) == 1 else "skills"
    return (
        f"{label} {', '.join(variants)} cannot run workflow {noun} "
        f"{', '.join(map(repr, skills))}: the {agent} config has no "
        f"{', '.join(f'skills/{skill}/SKILL.md' for skill in skills)}."
    )


def require_workflow_skills(
    agent: str, skill_names: Mapping[str, tuple[str, ...]], available: Collection[str]
) -> None:
    gaps = {variant: missing_skills(names, available) for variant, names in skill_names.items()}
    blocked = tuple(variant for variant, missing in gaps.items() if missing)
    if blocked:
        raise MissingSkillError(
            skill_refusal(
                agent,
                blocked,
                tuple(dict.fromkeys(name for missing in gaps.values() for name in missing)),
            )
        )
