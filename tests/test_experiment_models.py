import json

from dryheave.controller_models import ControllerRecipe, RoleBudget
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig, inspection_recipe
from dryheave.models import ObjectKind
from dryheave.serialization import canonical_json, parse_model
from dryheave.storage import ObjectStore


def test_price_rate_without_cache_write_1h_round_trips_byte_identically() -> None:
    old = (
        b'{"model":"root-model","uncached_input":1.0,"cache_read":0.5,'
        b'"cache_write":1.0,"output":2.0}'
    )
    rate = PriceRate.model_validate_json(old)
    assert rate.cache_write_1h is None
    assert canonical_json(rate) == canonical_json(json.loads(old))


def test_price_rate_carries_cache_write_1h_when_present() -> None:
    rate = PriceRate(
        model="claude-sonnet-4-6",
        uncached_input=3.0,
        cache_read=0.30,
        cache_write=3.75,
        cache_write_1h=6.0,
        output=15.0,
    )
    assert rate.cache_write_1h == 6.0
    assert "cache_write_1h" in json.loads(rate.model_dump_json())


def test_stored_scoring_config_nesting_a_price_rate_pins_its_object_id(
    store: ObjectStore,
) -> None:
    scoring = ScoringConfig(
        prices=PriceTable(
            version="fixture-v1",
            effective_date="2026-01-01",
            currency="USD",
            rates=(
                PriceRate(
                    model="root-model",
                    uncached_input=1.0,
                    cache_read=0.5,
                    cache_write=1.0,
                    output=2.0,
                ),
            ),
        )
    )
    identifier = store.put(ObjectKind.SCORING, scoring)
    assert identifier == "16118c83c469b0dabcea959bb2270bd874146d7ad93ec8344075335fe1c90f40"


def test_scoring_config_without_inspector_drops_the_field() -> None:
    scoring = ScoringConfig()
    assert "inspector" not in json.loads(scoring.model_dump_json())
    assert parse_model(canonical_json(scoring), ScoringConfig) == scoring


def test_scoring_config_round_trips_a_declared_inspector() -> None:
    scoring = ScoringConfig(inspector=ControllerRecipe(kind="scripted"))
    dumped = json.loads(scoring.model_dump_json())
    assert dumped["inspector"]["kind"] == "scripted"
    assert parse_model(canonical_json(scoring), ScoringConfig) == scoring


def test_inspection_recipe_prefers_the_inspector_over_the_judge() -> None:
    judge = ControllerRecipe(kind="scripted", budget=RoleBudget(max_calls=2))
    inspector = ControllerRecipe(kind="scripted", budget=RoleBudget(max_calls=5))
    scoring = ScoringConfig(judge=judge, inspector=inspector)
    assert inspection_recipe(scoring) == inspector


def test_inspection_recipe_falls_back_to_the_judge() -> None:
    judge = ControllerRecipe(kind="scripted", budget=RoleBudget(max_calls=2))
    scoring = ScoringConfig(judge=judge)
    assert inspection_recipe(scoring) == judge


def test_inspection_recipe_is_none_without_a_usable_recipe() -> None:
    assert inspection_recipe(ScoringConfig()) is None
    exhausted = ControllerRecipe(kind="scripted", budget=RoleBudget(max_calls=0))
    assert inspection_recipe(ScoringConfig(judge=exhausted)) is None
    assert inspection_recipe(ScoringConfig(inspector=exhausted)) is None
