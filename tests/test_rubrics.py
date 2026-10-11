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


def test_default_rubrics_are_optional_and_versioned():
    assert DEFAULT_RUBRIC_SET == "default-v1"
    assert [(item.criterion_id, item.required) for item in default_rubrics()] == [
        ("code-quality", False),
        ("codebase-consistency", False),
        ("test-quality", False),
    ]


@pytest.mark.integration
def test_defaults_apply_only_with_a_judge(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    assert [item.criterion_id for item in graded_criteria(case, judged=False)] == ["greeting"]
    assert [item.criterion_id for item in graded_criteria(case, judged=True)] == [
        "greeting",
        "code-quality",
        "codebase-consistency",
        "test-quality",
    ]


@pytest.mark.integration
def test_case_override_and_disable_shape_effective_rubrics(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    override = RubricCriterion(
        criterion_id="test-quality",
        required=True,
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
def test_grading_identity_keeps_the_criteria_only_hash_and_covers_defaults_and_checks(
    store, graded_benchmark
):
    from dryheave.cases import CaseCheck

    case = load_frozen_case(store, graded_benchmark.cases[0])
    legacy = digest(
        canonical_json({"criteria": [item.model_dump(mode="json") for item in case.criteria]})
    )
    checked = case.model_copy(
        update={
            "checks": (
                CaseCheck(check_id="asked", question="Did the agent ask first?", expect="yes"),
            )
        }
    )
    identities = {
        grading_identity(case, judged=False),
        grading_identity(case, judged=True),
        grading_identity(checked, judged=False),
    }
    assert grading_identity(case, judged=False) == legacy
    assert len(identities) == 3


@pytest.mark.integration
def test_grading_identity_separates_metrics_commands_and_keeps_metricless_identity(
    store, graded_benchmark
):
    from dryheave.cases import MetricsCommand
    from dryheave.models import CommandSpec

    case = load_frozen_case(store, graded_benchmark.cases[0])

    def measured(argv: tuple[str, ...]):
        command = MetricsCommand(
            metrics_id="tests",
            description="Count the tests in the final workspace.",
            command=CommandSpec(argv=argv),
            entrypoint="metrics.sh",
        )
        return case.model_copy(update={"metrics": (command,)})

    legacy = digest(
        canonical_json({"criteria": [item.model_dump(mode="json") for item in case.criteria]})
    )
    first = measured(("/bin/sh", "{verifier}/metrics.sh"))
    second = measured(("/bin/sh", "{verifier}/metrics.sh", "--coverage"))
    assert case.metrics == ()
    assert grading_identity(case, judged=False) == legacy
    assert grading_identity(first, judged=False) != legacy
    assert len({grading_identity(item, judged=False) for item in (case, first, second)}) == 3
