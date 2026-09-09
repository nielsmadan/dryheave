from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.drivers.models import NativeEvent, NativeUsage, UsageObservation
from dryheave.experiment_models import PriceRate, PriceTable
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


def test_cumulative_deltas_resets_and_descendant_model_attribution(store, benchmark):
    summary = run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
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


def test_response_dedupe_supersedes_cumulative_and_does_not_invent_unknown_cost(store, benchmark):
    summary = run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
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
