import sys

import pytest

from dryheave.cases import RubricCriterion, load_frozen_case
from dryheave.controller_models import ControllerRecipe, RoleBudget
from dryheave.errors import InputError
from dryheave.experiment_models import ScoringConfig, VariantSpec
from dryheave.experiments import create_experiment, load_experiment, references, validate_experiment
from dryheave.models import CommandSpec, ObjectKind
from dryheave.profile_models import DeriveSpec
from dryheave.profiles import derive_profile, load_profile


def test_frozen_aliases_variants_and_pair_seed(store, benchmark):
    draft = benchmark.model_copy(
        update={
            "repetitions": 2,
            "seed": 41,
            "variants": (
                *benchmark.variants,
                VariantSpec(name="changed", profile="subject", model="different-model"),
            ),
        }
    )
    pinned = validate_experiment(store, draft)
    assert len(pinned.cases[0]) == 64
    identifier = create_experiment(store, draft)
    frozen = load_experiment(store, identifier)
    assert len(frozen.trials) == 4
    assert load_profile(store, frozen.variants[1].profile_id).recipe.model == "different-model"
    assert len({trial.seed for trial in frozen.trials}) == 2
    changed = derive_profile(store, "subject", DeriveSpec(recipe_changes={"model": "later"}))
    store.set_alias("subject", changed, replace=True)
    assert load_experiment(store, identifier) == frozen
    assert create_experiment(store, pinned) == identifier


def test_frozen_experiment_id_is_pinned(store):
    from dryheave.experiment_models import FrozenExperiment, FrozenVariant, TrialSpec

    case_id = "a" * 64
    profile_id = "b" * 64
    simulator_id = "c" * 64
    scoring_id = "d" * 64
    frozen = FrozenExperiment(
        name="Pinned",
        case_ids=(case_id,),
        variants=(FrozenVariant(name="base", profile_id=profile_id),),
        repetitions=1,
        seed=0,
        simulator_id=simulator_id,
        scoring_id=scoring_id,
        trials=(
            TrialSpec(
                trial_id="t-" + "e" * 24,
                case_id=case_id,
                profile_id=profile_id,
                variant="base",
                repetition=1,
                seed=0,
            ),
        ),
    )
    identifier = store.put(ObjectKind.EXPERIMENT, frozen)
    assert identifier == "5aaeb75e2c8265525843fd79f180972126813b0298439834453f17156398e172"


def test_matrix_validation_rejects_alias_duplicates_and_unknown_facts(store, benchmark):
    store.set_alias("same-task", store.resolve("task"))
    with pytest.raises(InputError, match="same case"):
        validate_experiment(store, benchmark.model_copy(update={"cases": ("task", "same-task")}))
    bad = benchmark.simulator.script[0].decision.model_copy(update={"fact_ids": ("secret",)})
    script = benchmark.simulator.model_copy(
        update={"script": (benchmark.simulator.script[0].model_copy(update={"decision": bad}),)}
    )
    with pytest.raises(InputError, match="outside"):
        validate_experiment(store, benchmark.model_copy(update={"simulator": script}))


def test_domain_load_rejects_manually_inconsistent_expansion(store, benchmark):
    identifier = create_experiment(store, benchmark)
    frozen = load_experiment(store, identifier)
    bad = store.put(
        ObjectKind.EXPERIMENT,
        frozen.model_copy(update={"seed": 99}),
        references=store.get(identifier).references,
    )
    with pytest.raises(InputError, match="expansion"):
        load_experiment(store, bad)


def test_controller_literal_credentials_are_rejected_without_echo(store, benchmark):
    from dryheave.controller_models import ControllerRecipe
    from dryheave.models import CommandSpec

    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=("controller", "--api-key", "synthetic-credential")),
    )
    with pytest.raises(InputError, match="runtime reference") as error:
        validate_experiment(store, benchmark.model_copy(update={"simulator": recipe}))
    assert "synthetic-credential" not in str(error.value)


def test_inspector_recipe_gets_the_same_validation_as_the_judge(store, benchmark):
    secret_bearing = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=("controller", "--api-key", "synthetic-credential")),
    )
    with pytest.raises(InputError, match="runtime reference") as error:
        validate_experiment(
            store, benchmark.model_copy(update={"scoring": ScoringConfig(inspector=secret_bearing)})
        )
    assert "synthetic-credential" not in str(error.value)
    scripted = benchmark.model_copy(update={"scoring": ScoringConfig(inspector=ControllerRecipe())})
    for operation in (validate_experiment, create_experiment):
        with pytest.raises(InputError, match="inspector"):
            operation(store, scripted)


def test_frozen_inspector_revalidated_on_load(store, benchmark):
    usable = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "judge.py")),
    )
    draft = benchmark.model_copy(update={"scoring": ScoringConfig(judge=usable, inspector=usable)})
    identifier = create_experiment(store, draft)
    frozen = load_experiment(store, identifier)
    frozen = frozen.model_copy(
        update={
            "scoring_id": store.put(
                ObjectKind.SCORING,
                ScoringConfig(judge=usable, inspector=ControllerRecipe()),
            )
        }
    )
    invalid = store.put(ObjectKind.EXPERIMENT, frozen, references=references(frozen))
    with pytest.raises(InputError, match="inspector"):
        load_experiment(store, invalid)


@pytest.mark.parametrize(
    "rubric, judge_kind, valid",
    [
        ("required", "absent", False),
        ("required", "scripted", False),
        ("required", "disabled", False),
        ("required", "enabled", True),
        ("optional", "absent", True),
        ("optional", "scripted", False),
        ("none", "scripted", False),
    ],
)
def test_judge_usability_is_checked_in_drafts_and_frozen_loads(
    store, benchmark, rubric, judge_kind, valid
):
    case = load_frozen_case(store, benchmark.cases[0])
    if rubric != "none":
        case = case.model_copy(
            update={
                "criteria": (
                    *case.criteria,
                    RubricCriterion(
                        criterion_id="quality",
                        required=rubric == "required",
                        rubric="The implementation preserves the existing public interface.",
                    ),
                )
            }
        )
    case_id = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(benchmark.cases[0]),
        references=(case.persona_id, case.repository_id),
    )
    usable = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "judge.py")),
    )
    judge = {
        "absent": None,
        "scripted": ControllerRecipe(),
        "disabled": usable.model_copy(update={"budget": RoleBudget(max_calls=0)}),
        "enabled": usable,
    }[judge_kind]
    draft = benchmark.model_copy(
        update={"cases": (case_id,), "scoring": ScoringConfig(judge=judge)}
    )
    if valid:
        validate_experiment(store, draft)
        assert load_experiment(store, create_experiment(store, draft)).case_ids == (case_id,)
        return
    for operation in (validate_experiment, create_experiment):
        with pytest.raises(InputError, match="judge"):
            operation(store, draft)
    usable_draft = draft.model_copy(update={"scoring": ScoringConfig(judge=usable)})
    frozen = load_experiment(store, create_experiment(store, usable_draft))
    frozen = frozen.model_copy(update={"scoring_id": store.put(ObjectKind.SCORING, draft.scoring)})
    invalid = store.put(ObjectKind.EXPERIMENT, frozen, references=references(frozen))
    with pytest.raises(InputError, match="judge"):
        load_experiment(store, invalid)
