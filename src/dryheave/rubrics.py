from pydantic import JsonValue

from dryheave.cases import (
    DEFAULT_RUBRIC_IDS,
    CaseContent,
    DeterministicCriterion,
    FrozenCase,
    RubricCriterion,
)
from dryheave.serialization import canonical_json, digest

DEFAULT_RUBRIC_SET = "default-v1"

_DEFAULT_TEXTS = {
    "code-quality": (
        "Judge the quality of the code the agent wrote or changed, independent of whether the "
        "task is complete. Pass when the changed code is correct for the cases it handles, uses "
        "the language's and libraries' idioms rather than fighting them, is clear to read "
        "(meaningful names, small focused units, no needless indirection), handles errors and "
        "edge inputs deliberately instead of ignoring or swallowing them, and leaves no dead "
        "code, debugging leftovers, commented-out blocks or speculative abstractions. Fail when "
        "any of these is clearly violated in a way a careful reviewer would ask to fix before "
        "merging. Score 0 to 1 by how close the change is to code a careful reviewer would "
        "approve without changes."
    ),
    "codebase-consistency": (
        "Judge whether the change fits the existing project rather than how it would look in a "
        "fresh codebase. Pass when new code follows the patterns already used nearby: naming, "
        "file and module structure, how settings, configuration and persistence are declared "
        "and read, how state and dependencies are passed, error and logging style, and the "
        "project's documentation and comment conventions; and when the change avoids unrelated "
        "churn such as reformatting, renaming or restructuring code the task did not need. Fail "
        "when the change introduces a parallel mechanism where an established one exists, "
        "ignores a clear project convention, or rewrites unrelated code. Score 0 to 1 by how "
        "indistinguishable the change is from code the project's maintainers would write."
    ),
    "test-quality": (
        "Judge the tests that accompany the change. Pass when new or changed logic that can "
        "reasonably be tested is covered by added or updated tests, the tests assert meaningful "
        "observable behaviour rather than restating the implementation or only checking that "
        "code runs, they cover the important edge and failure cases, and they follow the "
        "project's existing test layout, naming, fixtures and frameworks. Fail when testable "
        "new logic has no tests, when tests are trivially weak or would pass for a wrong "
        "implementation, or when they ignore the project's test conventions. When the change "
        "contains no logic a test could reasonably exercise, pass and say so. Score 0 to 1 by "
        "how much confidence the tests give that the change works and stays working."
    ),
}


def default_rubrics() -> tuple[RubricCriterion, ...]:
    return tuple(
        RubricCriterion(criterion_id=name, required=False, rubric=_DEFAULT_TEXTS[name])
        for name in DEFAULT_RUBRIC_IDS
    )


def graded_criteria(
    case: CaseContent, *, judged: bool
) -> tuple[DeterministicCriterion | RubricCriterion, ...]:
    if not judged:
        return case.criteria
    declared = {item.criterion_id for item in case.criteria}
    return (
        *case.criteria,
        *(
            item
            for item in default_rubrics()
            if item.criterion_id not in declared and item.criterion_id not in case.disabled_rubrics
        ),
    )


def uses_default_rubrics(case: CaseContent) -> bool:
    declared = {item.criterion_id for item in case.criteria}
    return any(
        name not in declared and name not in case.disabled_rubrics for name in DEFAULT_RUBRIC_IDS
    )


def effective_rubrics(case: FrozenCase) -> tuple[RubricCriterion, ...]:
    return tuple(
        item for item in graded_criteria(case, judged=True) if isinstance(item, RubricCriterion)
    )


def grading_identity(case: CaseContent, *, judged: bool) -> str:
    payload: dict[str, JsonValue] = {
        "criteria": [item.model_dump(mode="json") for item in graded_criteria(case, judged=judged)]
    }
    if case.checks:
        payload["checks"] = [item.model_dump(mode="json") for item in case.checks]
    if case.metrics:
        payload["metrics"] = [item.model_dump(mode="json") for item in case.metrics]
    return digest(canonical_json(payload))
