import pytest

from dryheave.errors import InputError
from dryheave.experiment_models import VariantSpec
from dryheave.experiments import create_experiment, load_experiment, validate_experiment
from dryheave.models import ObjectKind
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
