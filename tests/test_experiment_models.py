import json

from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.models import ObjectKind
from dryheave.serialization import canonical_json
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
