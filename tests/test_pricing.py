from dryheave.pricing import builtin_price_table


def test_builtin_price_table_covers_models_used_in_the_juggler_experiment() -> None:
    table = builtin_price_table()
    assert table.version == "anthropic-2026-10-14"
    models = {rate.model for rate in table.rates}
    assert {
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-haiku-4-5-20251001",
        "claude-haiku-5-5",
    } <= models


def test_builtin_price_table_prices_long_haiku_5_5_prompts_at_the_higher_tier() -> None:
    haiku = next(rate for rate in builtin_price_table().rates if rate.model == "claude-haiku-5-5")
    assert haiku.long_prompt is not None
    assert (haiku.uncached_input, haiku.long_prompt.uncached_input) == (0.1, 0.5)
    assert haiku.long_prompt.above_total_input_tokens == 100000


def test_builtin_price_table_is_cached_and_immutable_across_calls() -> None:
    first = builtin_price_table()
    second = builtin_price_table()
    assert first is second
