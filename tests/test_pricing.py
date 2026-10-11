from dryheave.pricing import builtin_price_table


def test_builtin_price_table_covers_models_used_in_the_juggler_experiment() -> None:
    table = builtin_price_table()
    assert table.version == "anthropic-2026-10-07"
    models = {rate.model for rate in table.rates}
    assert {"claude-sonnet-4-6", "claude-opus-5-5", "claude-haiku-4-5-20251001"} <= models


def test_builtin_price_table_is_cached_and_immutable_across_calls() -> None:
    first = builtin_price_table()
    second = builtin_price_table()
    assert first is second
