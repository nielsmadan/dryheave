import pytest

from conftest import HOST
from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.drivers.models import NativeEvent, NativeUsage, UsageObservation
from dryheave.experiment_models import LongPromptRates, PriceRate, PriceTable
from dryheave.experiments import create_experiment
from dryheave.metrics import call_metrics, subject_metrics
from dryheave.models import CommandSpec, TokenUsage
from dryheave.runner import load_capture, run_experiment
from dryheave.runner_models import RunOptions


def tokens(number):
    return TokenUsage(
        uncached_input=number,
        cache_read=0,
        cache_write=0,
        output=number,
        reasoning=0,
        provenance="fixture measurement",
    )


def table():
    return PriceTable(
        version="fixture-2026-v1",
        effective_date="2026-01-01",
        currency="EUR",
        rates=(
            PriceRate(
                model="root-model", uncached_input=1.0, cache_read=0.5, cache_write=1.0, output=2.0
            ),
        ),
    )


def native(identity, number, *, accounting="cumulative", session="root", line=1):
    return NativeUsage(
        identity=identity,
        session_id=session,
        thread_id=session,
        turn_id=None,
        root_turn_id=None,
        response_id=identity if accounting == "response" else None,
        source="native",
        line=line,
        accounting=accounting,
        usage=tokens(number),
        raw={"input_tokens": number, "output_tokens": number},
    )


@pytest.mark.integration
def test_cumulative_deltas_resets_and_descendant_model_attribution(store, benchmark):
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    launch = capture.launch.model_copy(
        update={"observed_session_id": "root", "observed_model": "root-model"}
    )
    capture = capture.model_copy(
        update={
            "mode": "native",
            "launch": launch,
            "usage": UsageObservation(
                records=(
                    native("one", 10),
                    native("two", 20),
                    native("reset", 3),
                    native("child", 7, session="child", accounting="response"),
                ),
                unattributed=(),
                session_graph={"root": None, "child": "root"},
                missing_session_usage=(),
            ),
        }
    )
    metrics = subject_metrics(capture, table(), requested_model="root-model")
    assert metrics.known_tokens.uncached_input == 30
    assert metrics.known_cost == 69 / 1e6
    assert metrics.coverage == "partial"
    assert metrics.records[-1].cost is None
    assert metrics.records[-1].requested_model is None
    assert "reset" in metrics.reasons[0]
    event = NativeEvent(
        kind="metadata", session_id="child", source="native", line=0, data={"model": "root-model"}
    )
    priced = subject_metrics(capture, table(), (event,), "root-model")
    assert priced.known_cost == 90 / 1e6
    assert priced.records[-1].model_basis == "observed"
    assert priced.records[0].price_version == "fixture-2026-v1"


@pytest.mark.integration
def test_response_dedupe_supersedes_cumulative_and_does_not_invent_unknown_cost(store, benchmark):
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    record = native("response", 10, accounting="response")
    capture = capture.model_copy(
        update={
            "mode": "native",
            "usage": UsageObservation(
                records=(native("cumulative", 40), record, record),
                unattributed=(),
                session_graph={"root": None},
                missing_session_usage=(),
            ),
        }
    )
    metrics = subject_metrics(capture, None)
    assert len(metrics.records) == 1
    assert metrics.known_tokens.output == 10
    assert metrics.known_cost is None


def test_failed_role_calls_retain_known_spend_and_unknown_calls_are_not_zero():
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        model="root-model",
        command=CommandSpec(argv=("fixture",)),
    )
    calls = (
        RoleCall(call_id="known", status="failed", usage=tokens(100)),
        RoleCall(call_id="unknown", status="interrupted"),
    )
    metrics = call_metrics("judge", calls, recipe, table())
    assert metrics.known_cost == 300 / 1e6
    assert metrics.records[1].cost is None
    assert metrics.coverage == "partial"
    assert metrics.records[0].price_date == "2026-01-01"


def test_partial_reasoning_observations_preserve_valid_aggregate_subset():
    from dryheave.metrics import token_sum

    result = token_sum(
        (
            TokenUsage(output=2, reasoning=1, provenance="complete output"),
            TokenUsage(reasoning=10, provenance="output absent"),
        )
    )
    assert result.output == 2
    assert result.reasoning is None


def test_token_sum_requires_every_cache_write_record_to_carry_the_1h_split():
    from dryheave.metrics import token_sum

    known = token_sum(
        (
            TokenUsage(cache_write=10, cache_write_1h=4, provenance="a"),
            TokenUsage(cache_write=20, cache_write_1h=6, provenance="b"),
        )
    )
    assert known.cache_write == 30
    assert known.cache_write_1h == 10
    unknown = token_sum(
        (
            TokenUsage(cache_write=10, cache_write_1h=4, provenance="a"),
            TokenUsage(cache_write=20, provenance="b"),
        )
    )
    assert unknown.cache_write == 30
    assert unknown.cache_write_1h is None


def test_cumulative_clips_a_1h_delta_that_would_exceed_the_cache_write_delta():
    from dryheave.metrics import _cumulative

    def usage(cache_write, cache_write_1h):
        return TokenUsage(
            cache_write=cache_write, cache_write_1h=cache_write_1h, provenance="native"
        )

    first = NativeUsage(
        identity="r1",
        session_id="s",
        thread_id="s",
        turn_id=None,
        root_turn_id=None,
        response_id=None,
        source="native",
        line=1,
        accounting="cumulative",
        usage=usage(10, 2),
        raw={},
    )
    second = NativeUsage(
        identity="r2",
        session_id="s",
        thread_id="s",
        turn_id=None,
        root_turn_id=None,
        response_id=None,
        source="native",
        line=2,
        accounting="cumulative",
        usage=usage(12, 10),
        raw={},
    )
    converted, reset = _cumulative([first, second])
    assert not reset
    assert converted[1].usage.cache_write == 2
    assert converted[1].usage.cache_write_1h is None


def _mixed_usage(cache_write, cache_write_1h):
    return TokenUsage(
        uncached_input=0,
        cache_read=0,
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
        output=0,
        reasoning=0,
        provenance="fixture",
    )


def _usage_charge(usage):
    from dryheave.result_models import UsageCharge

    return UsageCharge(
        identity="x",
        usage=usage,
        observed_model="root-model",
        requested_model=None,
        model_basis="observed",
        raw_evidence_sha256="0" * 64,
        reason="fixture usage",
    )


def test_charge_prices_a_mixed_5m_and_1h_cache_write_record_exactly():
    from dryheave.metrics import _charge

    rate_table = PriceTable(
        version="fixture-2026-v2",
        effective_date="2026-01-01",
        currency="EUR",
        rates=(
            PriceRate(
                model="root-model",
                uncached_input=1.0,
                cache_read=0.5,
                cache_write=1.0,
                cache_write_1h=2.0,
                output=2.0,
            ),
        ),
    )
    charge = _usage_charge(_mixed_usage(100, 40))
    priced = _charge(charge, rate_table, per_request=True)
    assert priced.cost == (60 * 1.0 + 40 * 2.0) / 1e6


def test_charge_without_a_1h_rate_prices_all_writes_at_the_5m_rate():
    from dryheave.metrics import _charge

    rate_table = PriceTable(
        version="fixture-2026-v2",
        effective_date="2026-01-01",
        currency="EUR",
        rates=(
            PriceRate(
                model="root-model", uncached_input=1.0, cache_read=0.5, cache_write=1.0, output=2.0
            ),
        ),
    )
    charge = _usage_charge(_mixed_usage(100, 40))
    priced = _charge(charge, rate_table, per_request=True)
    assert priced.cost == 100 * 1.0 / 1e6
    assert "5-minute rate" in priced.reason


def _tiered_table():
    return PriceTable(
        version="fixture-2026-v3",
        effective_date="2026-01-01",
        currency="EUR",
        rates=(
            PriceRate(
                model="root-model",
                uncached_input=1.0,
                cache_read=0.1,
                cache_write=1.0,
                cache_write_1h=2.0,
                output=2.0,
                long_prompt=LongPromptRates(
                    above_total_input_tokens=1000,
                    uncached_input=5.0,
                    cache_read=0.5,
                    cache_write=5.0,
                    cache_write_1h=10.0,
                    output=10.0,
                ),
            ),
        ),
    )


def _prompt_usage(uncached, cache_read, cache_write=0, cache_write_1h=0):
    return TokenUsage(
        uncached_input=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
        output=10,
        reasoning=0,
        provenance="fixture",
    )


@pytest.mark.parametrize(
    "usage,expected",
    [
        (_prompt_usage(100, 900), (100 * 1.0 + 900 * 0.1 + 10 * 2.0) / 1e6),
        (_prompt_usage(100, 901), (100 * 5.0 + 901 * 0.5 + 10 * 10.0) / 1e6),
        (
            _prompt_usage(100, 0, cache_write=901, cache_write_1h=300),
            (100 * 5.0 + 601 * 5.0 + 300 * 10.0 + 10 * 10.0) / 1e6,
        ),
    ],
)
def test_charge_prices_a_request_by_its_total_input_including_cache_reads_and_writes(
    usage, expected
):
    from dryheave.metrics import _charge

    priced = _charge(_usage_charge(usage), _tiered_table(), per_request=True)
    assert priced.cost == pytest.approx(expected)


def test_charge_says_when_it_applied_the_long_prompt_rates():
    from dryheave.metrics import _charge

    priced = _charge(_usage_charge(_prompt_usage(100, 901)), _tiered_table(), per_request=True)
    assert "long-prompt rates: total input of 1001 tokens exceeds 1000" in priced.reason


def test_charge_leaves_a_record_not_known_to_be_one_request_of_a_tiered_model_unpriced():
    from dryheave.metrics import _charge

    priced = _charge(_usage_charge(_prompt_usage(100, 0)), _tiered_table(), per_request=False)
    assert priced.cost is None
    assert "not known to cover a single request" in priced.reason


def test_role_calls_of_a_tiered_model_stay_unpriced():
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        model="root-model",
        command=CommandSpec(argv=("fixture",)),
    )
    calls = (RoleCall(call_id="judge", status="completed", usage=tokens(100)),)
    metrics = call_metrics("judge", calls, recipe, _tiered_table())
    assert metrics.records[0].cost is None
    assert "not known to cover a single request" in metrics.records[0].reason


@pytest.mark.integration
@pytest.mark.parametrize(
    "accounting,expected",
    [("response", (1001 * 5.0 + 1001 * 10.0) / 1e6), ("cumulative", None)],
)
def test_subject_metrics_tiers_only_single_response_records(store, benchmark, accounting, expected):
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    capture = capture.model_copy(
        update={
            "mode": "native",
            "launch": capture.launch.model_copy(
                update={"observed_session_id": "root", "observed_model": "root-model"}
            ),
            "usage": UsageObservation(
                records=(native("record", 1001, accounting=accounting),),
                unattributed=(),
                session_graph={"root": None},
                missing_session_usage=(),
            ),
        }
    )
    metrics = subject_metrics(capture, _tiered_table(), requested_model="root-model")
    assert metrics.records[0].cost == (pytest.approx(expected) if expected else None)


@pytest.mark.integration
def test_subject_metrics_derives_the_1h_split_from_raw_usage_evidence_when_missing(
    store, benchmark
):
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, summary.pending_assessment[0])
    launch = capture.launch.model_copy(
        update={"observed_session_id": "root", "observed_model": "root-model"}
    )
    record = NativeUsage(
        identity="response",
        session_id="root",
        thread_id="root",
        turn_id=None,
        root_turn_id=None,
        response_id="response",
        source="native",
        line=1,
        accounting="response",
        usage=TokenUsage(
            uncached_input=0,
            cache_read=0,
            cache_write=100,
            output=0,
            reasoning=0,
            provenance="native usage",
        ),
        raw={"cache_creation": {"ephemeral_1h_input_tokens": 40}},
    )
    capture = capture.model_copy(
        update={
            "mode": "native",
            "launch": launch,
            "usage": UsageObservation(
                records=(record,),
                unattributed=(),
                session_graph={"root": None},
                missing_session_usage=(),
            ),
        }
    )
    rate_table = PriceTable(
        version="fixture-2026-v2",
        effective_date="2026-01-01",
        currency="EUR",
        rates=(
            PriceRate(
                model="root-model",
                uncached_input=1.0,
                cache_read=0.5,
                cache_write=1.0,
                cache_write_1h=2.0,
                output=2.0,
            ),
        ),
    )
    metrics = subject_metrics(capture, rate_table, requested_model="root-model")
    assert metrics.records[0].usage.cache_write_1h == 40
    assert metrics.known_cost == (60 * 1.0 + 40 * 2.0) / 1e6
