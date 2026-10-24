from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from dryheave.cases import CaseContent, PromptStep, TaskFact, verifier_facts
from dryheave.errors import MissingSkillError
from dryheave.judges import FactInspection, FactVerdict, RaiseEvent
from dryheave.models import JournalEvent
from dryheave.serialization import canonical_json, parse_model

type WorkflowAction = Literal["deliver_prompt", "raise", "complete"]
INSPECTION_INTENT_EVENT = "inspection-intent"
FACT_INSPECTION_EVENT = "fact-inspection"
RAISE_EVENT = "raise"


@dataclass(frozen=True)
class InspectionRecord:
    inspection_id: str
    turn: int
    result: FactInspection | None


def workflow_steps(case: CaseContent, *, enabled: bool) -> tuple[PromptStep, ...]:
    return case.workflow if enabled else ()


def blocking_phase(case: CaseContent, verdicts: Mapping[str, FactVerdict]) -> str | None:
    unmet = {
        fact.phase
        for fact in case.allowed_facts
        if fact.kind == "requirement" and verdicts.get(fact.fact_id) == "unmet"
    }
    return next((phase for phase in case.phases if phase in unmet), None)


def raised_facts(
    case: CaseContent, phase: str, verdicts: Mapping[str, FactVerdict]
) -> tuple[TaskFact, ...]:
    return tuple(
        fact
        for fact in case.allowed_facts
        if fact.phase == phase
        and fact.kind in {"requirement", "optional"}
        and verdicts.get(fact.fact_id) == "unmet"
    )


def next_workflow_action(
    case: CaseContent, position: int, verdicts: Mapping[str, FactVerdict]
) -> WorkflowAction:
    if position < 0:
        raise ValueError(f"workflow position must not be negative: {position}")
    blocker = blocking_phase(case, verdicts)
    pending = case.workflow[position] if position < len(case.workflow) else None
    if pending is not None and (
        blocker is None or case.phases.index(pending.after_phase) < case.phases.index(blocker)
    ):
        return "deliver_prompt"
    return "complete" if blocker is None else "raise"


def inspection_verdict(
    case: CaseContent, position: int, inspection: FactInspection
) -> Literal["passed", "failed", "unavailable"]:
    if inspection.status == "unavailable":
        return "unavailable"
    verdicts = {item.fact_id: item.verdict for item in inspection.judgments}
    if next_workflow_action(case, position, verdicts) == "raise":
        return "failed"
    unchecked = any(
        fact.kind == "requirement" and verdicts.get(fact.fact_id) == "unclear"
        for fact in verifier_facts(case)
    )
    return "unavailable" if unchecked else "passed"


def dialog_source(steps: tuple[PromptStep, ...], end: int) -> PromptStep | None:
    return steps[min(end, len(steps)) - 1] if end > 0 and steps else None


def slash_steps(steps: Iterable[PromptStep]) -> tuple[PromptStep, ...]:
    return tuple(step for step in steps if step.slash)


def inspection_records(
    events: Iterable[JournalEvent], attempt_id: str
) -> tuple[InspectionRecord, ...]:
    intents: dict[str, InspectionRecord] = {}
    for event in events:
        if event.attempt_id != attempt_id:
            continue
        if event.event == INSPECTION_INTENT_EVENT:
            identifier, turn = event.data.get("inspection_id"), event.data.get("turn")
            if isinstance(identifier, str) and type(turn) is int:
                intents[identifier] = InspectionRecord(identifier, turn, None)
        elif event.event == FACT_INSPECTION_EVENT:
            result = parse_model(canonical_json(event.data), FactInspection)
            intents[result.inspection_id] = InspectionRecord(
                result.inspection_id, result.turn, result
            )
    return tuple(intents.values())


def raise_events(events: Iterable[JournalEvent], attempt_id: str) -> tuple[RaiseEvent, ...]:
    return tuple(
        parse_model(canonical_json(event.data), RaiseEvent)
        for event in events
        if event.attempt_id == attempt_id and event.event == RAISE_EVENT
    )


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
