import pytest

from dryheave.cases import RubricCriterion, load_frozen_case
from dryheave.rubrics import (
    DEFAULT_RUBRIC_SET,
    default_rubrics,
    effective_rubrics,
    graded_criteria,
    grading_identity,
    uses_default_rubrics,
)
from dryheave.serialization import canonical_json, digest


def test_default_rubrics_are_versioned():
    assert DEFAULT_RUBRIC_SET == "default-v1"
    assert [item.criterion_id for item in default_rubrics()] == [
        "code-quality",
        "codebase-consistency",
        "test-quality",
    ]


@pytest.mark.integration
def test_defaults_apply_only_with_a_judge(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    quality = RubricCriterion(
        criterion_id="quality", rubric="The greeting keeps its public interface."
    )
    case = case.model_copy(update={"criteria": (quality,)})
    assert graded_criteria(case, judged=False) == (quality,)
    assert [item.criterion_id for item in graded_criteria(case, judged=True)] == [
        "quality",
        "code-quality",
        "codebase-consistency",
        "test-quality",
    ]


@pytest.mark.integration
def test_case_override_and_disable_shape_effective_rubrics(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    override = RubricCriterion(
        criterion_id="test-quality",
        rubric="Tests must cover the empty-name greeting edge case.",
    )
    changed = case.model_copy(
        update={
            "criteria": (*case.criteria, override),
            "disabled_rubrics": ("codebase-consistency",),
        }
    )
    assert effective_rubrics(changed) == (
        override,
        default_rubrics()[0],
    )


@pytest.mark.integration
def test_uses_default_rubrics_is_false_once_every_default_is_overridden_or_disabled(
    store, graded_benchmark
):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    assert uses_default_rubrics(case)
    override = RubricCriterion(
        criterion_id="test-quality",
        rubric="Tests must cover the empty-name greeting edge case.",
    )
    all_covered = case.model_copy(
        update={
            "criteria": (*case.criteria, override),
            "disabled_rubrics": ("code-quality", "codebase-consistency"),
        }
    )
    assert not uses_default_rubrics(all_covered)
    one_default_left = all_covered.model_copy(
        update={"disabled_rubrics": ("codebase-consistency",)}
    )
    assert uses_default_rubrics(one_default_left)


@pytest.mark.integration
def test_grading_identity_covers_graded_facts_defaults_and_metrics(store, graded_benchmark):
    from dryheave.cases import MetricsCommand
    from dryheave.models import CommandSpec

    case = load_frozen_case(store, graded_benchmark.cases[0])
    punctuation = case.allowed_facts[0]
    expected = digest(
        canonical_json(
            {
                "facts": [
                    punctuation.model_dump(
                        mode="json", include={"fact_id", "kind", "phase", "check", "verifier"}
                    )
                ],
                "criteria": [],
            }
        )
    )
    reworded = case.model_copy(
        update={"allowed_facts": (punctuation.model_copy(update={"text": "Add a comma."}),)}
    )
    rechecked = case.model_copy(
        update={
            "allowed_facts": (
                punctuation.model_copy(update={"check": "greet('Ada') returns 'Hello, Ada'."}),
            )
        }
    )

    def measured(argv: tuple[str, ...]):
        command = MetricsCommand(
            metrics_id="tests",
            description="Count the tests in the final workspace.",
            command=CommandSpec(argv=argv),
            entrypoint="metrics.sh",
        )
        return case.model_copy(update={"metrics": (command,)})

    assert grading_identity(case, judged=False) == expected
    assert grading_identity(reworded, judged=False) == expected
    identities = {
        grading_identity(item, judged=judged)
        for item, judged in (
            (case, False),
            (case, True),
            (rechecked, False),
            (measured(("/bin/sh", "{verifier}/metrics.sh")), False),
            (measured(("/bin/sh", "{verifier}/metrics.sh", "--coverage")), False),
        )
    }
    assert len(identities) == 5
