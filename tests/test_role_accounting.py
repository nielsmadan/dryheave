import json
import sys

import pytest

from dryheave.assessment_inputs import saved_judge_calls
from dryheave.assessments import assess_run
from dryheave.cases import RubricCriterion, load_frozen_case
from dryheave.controller_models import ControllerRecipe
from dryheave.controllers import failure_observation
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.journals import RunStore
from dryheave.models import CommandSpec, ObjectKind
from dryheave.reports import report_run
from dryheave.runner import load_capture, run_experiment
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


@pytest.mark.integration
@pytest.mark.parametrize("requested", [None, "different-requested-model"])
@pytest.mark.parametrize("invalid", ["decision", "fact", "judge-ids"])
def test_failed_roles_retain_observed_metadata_through_durable_report_pricing(
    store, benchmark, tmp_path, requested, invalid
):
    response = {
        "usage": {
            "uncached_input": 100,
            "cache_read": 0,
            "cache_write": 0,
            "output": 50,
            "provenance": "fixture observed usage",
        },
        "observed_model": "actual-model",
        "observed_effort": "high",
    }
    if invalid == "judge-ids":
        judgment = {"criterion_id": "quality", "outcome": "pass", "rationale": "Fixture rationale."}
        response["judgments"] = [judgment, judgment]
        case_id = store.resolve(benchmark.cases[0])
        case = load_frozen_case(store, case_id)
        changed = store.put(
            ObjectKind.CASE,
            case.model_copy(
                update={
                    "criteria": (
                        *case.criteria,
                        RubricCriterion(
                            criterion_id="quality", rubric="Evaluate the greeting implementation."
                        ),
                    )
                }
            ),
            files=store.read_blobs(case_id),
            references=(case.persona_id, case.repository_id),
        )
        benchmark = benchmark.model_copy(update={"cases": (changed,)})
    else:
        response["decision"] = (
            {"action": "approve", "reason": "Invalid authority."}
            if invalid == "decision"
            else {
                "action": "reply",
                "text": "Answer.",
                "fact_ids": ["unavailable-fact"],
                "reason": "Unavailable disclosure.",
            }
        )
    script = tmp_path / "role.py"
    script.write_text("print(" + repr(json.dumps(response)) + ")\n")
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        model=requested,
        command=CommandSpec(argv=(sys.executable, str(script))),
    )
    prices = PriceTable(
        version="fixture-v1",
        effective_date="2026-09-09",
        currency="EUR",
        rates=(
            PriceRate(
                model="actual-model",
                uncached_input=1.0,
                cache_read=0.0,
                cache_write=0.0,
                output=2.0,
            ),
            PriceRate(
                model="different-requested-model",
                uncached_input=20.0,
                cache_read=0.0,
                cache_write=0.0,
                output=20.0,
            ),
        ),
    )
    scoring = ScoringConfig(prices=prices, judge=recipe if invalid == "judge-ids" else None)
    benchmark = benchmark.model_copy(
        update={"scoring": scoring, **({"simulator": recipe} if invalid != "judge-ids" else {})}
    )
    summary = run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
    )
    assess_run(store, summary.run_id)
    if invalid == "judge-ids":
        with RunStore(store.root).open(summary.run_id) as journal:
            execution = ExecutionJournal(journal)
            call = saved_judge_calls(execution, execution.attempts[summary.attempts[0].attempt_id])[
                0
            ]
        role_index = 2
    else:
        call = load_capture(store, summary.pending_assessment[0]).controller_calls[0]
        role_index = 1
    assert (call.status, call.observed_model, call.observed_effort, call.usage.output) == (
        "failed",
        "actual-model",
        "high",
        50,
    )
    report = report_run(store, summary.run_id)
    role = report.attempts[0].roles[role_index]
    assert role.known_cost == 200 / 1e6
    assert role.coverage == "partial"
    assert role.records[0].model_basis == "observed"
    assert report.groups[0].known_spend_by_currency == {"EUR": 200 / 1e6}


@pytest.mark.parametrize("model", [None, ["invalid"], "x" * 257, "model\ncontrol"])
def test_failed_observations_validate_each_bounded_field_independently(tmp_path, model):
    recipe = ControllerRecipe(
        kind="json-command", isolation="trusted-native", command=CommandSpec(argv=("fixture",))
    )
    artifacts = ArtifactWriter(tmp_path / "artifacts", 100000)
    artifacts.append(
        "stdout.bin",
        json.dumps(
            {
                "observed_model": model,
                "observed_effort": "high",
                "usage": {"output": 4, "provenance": "fixture"},
            }
        ).encode(),
    )
    observed = failure_observation(recipe, artifacts)
    assert observed.observed_model is None
    assert observed.observed_effort == "high"
    assert observed.usage.output == 4


def test_invalid_json_cannot_supply_accounting_metadata(tmp_path):
    recipe = ControllerRecipe(
        kind="json-command", isolation="trusted-native", command=CommandSpec(argv=("fixture",))
    )
    artifacts = ArtifactWriter(tmp_path / "artifacts", 100000)
    artifacts.append(
        "stdout.bin",
        b'{"observed_model":"actual-model","usage":{"output":4,"provenance":"fixture"}} trailing data',
    )
    assert failure_observation(recipe, artifacts).model_dump() == {
        "usage": None,
        "observed_model": None,
        "observed_effort": None,
    }


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["judgments", "usage"])
def test_codex_judge_failure_retains_response_settings_and_prices_native_usage(
    store, benchmark, tmp_path, invalid
):
    case_id = store.resolve(benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(
            update={
                "criteria": (
                    *case.criteria,
                    RubricCriterion(
                        criterion_id="quality", rubric="Evaluate the greeting implementation."
                    ),
                )
            }
        ),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    response = {
        "judgments": [
            {
                "criterion_id": "quality",
                "outcome": "approve" if invalid == "judgments" else "pass",
                "score": None,
                "rationale": "Fixture rationale.",
            }
        ],
        "usage": {
            "uncached_input": 900,
            "cache_read": 0,
            "cache_write": 0,
            "output": 1 if invalid == "usage" else 700,
            "reasoning": 2,
            "provenance": "Model-supplied fixture usage.",
        },
        "observed_model": "actual-model",
        "observed_effort": "high",
    }
    native = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 0,
            "output_tokens": 50,
            "reasoning_output_tokens": 5,
        },
    }
    script = tmp_path / "codex-judge-fixture.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "if sys.argv[-1] == '--version':\n"
        " print('codex-cli 0.153.4')\n"
        " raise SystemExit(0)\n"
        "assert sys.argv[1] == 'exec'\n"
        "assert sys.argv[sys.argv.index('--model') + 1] == 'different-requested-model'\n"
        "request = json.load(sys.stdin)\n"
        "assert request['criteria'][0]['criterion_id'] == 'quality'\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text("
        + repr(json.dumps(response))
        + ")\nprint("
        + repr(json.dumps(native))
        + ")\n"
    )
    recipe = ControllerRecipe(
        kind="codex",
        isolation="trusted-native",
        version="0.153.4",
        model="different-requested-model",
        effort="low",
        command=CommandSpec(argv=(sys.executable, str(script))),
    )
    scoring = ScoringConfig(
        judge=recipe,
        prices=PriceTable(
            version="fixture-v1",
            effective_date="2026-09-09",
            currency="EUR",
            rates=(
                PriceRate(
                    model="actual-model",
                    uncached_input=1.0,
                    cache_read=0.0,
                    cache_write=0.0,
                    output=2.0,
                ),
                PriceRate(
                    model="different-requested-model",
                    uncached_input=20.0,
                    cache_read=0.0,
                    cache_write=0.0,
                    output=20.0,
                ),
            ),
        ),
    )
    summary = run_experiment(
        store,
        create_experiment(
            store, benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})
        ),
        options=RunOptions(mode="offline-fixture"),
    )
    assess_run(store, summary.run_id)
    with RunStore(store.root).open(summary.run_id) as journal:
        execution = ExecutionJournal(journal)
        call = saved_judge_calls(execution, execution.attempts[summary.attempts[0].attempt_id])[0]
    assert (call.status, call.observed_model, call.observed_effort) == (
        "failed",
        "actual-model",
        "high",
    )
    assert (
        call.usage.uncached_input,
        call.usage.cache_read,
        call.usage.cache_write,
        call.usage.output,
        call.usage.reasoning,
    ) == (100, 20, 0, 50, 5)
    report = report_run(store, summary.run_id)
    judge = report.attempts[0].roles[2]
    assert report.attempts[0].result.criteria[-1].outcome == "error"
    assert judge.coverage == "partial"
    assert judge.records[0].model_basis == "observed"
    assert judge.records[0].requested_model == "different-requested-model"
    assert judge.known_cost == 200 / 1e6
    assert report.groups[0].known_spend_by_currency == {"EUR": 200 / 1e6}


@pytest.mark.parametrize("invalid_field", ["observed_model", "observed_effort"])
@pytest.mark.parametrize("output", [0, 4])
def test_codex_failure_observation_keeps_valid_peers_and_native_zero_unknown(
    tmp_path, invalid_field, output
):
    recipe = ControllerRecipe(
        kind="codex",
        isolation="trusted-native",
        command=CommandSpec(argv=("fixture",)),
        version="0.153.4",
        model="requested-model",
        effort="low",
    )
    artifacts = ArtifactWriter(tmp_path / "artifacts", 100000)
    response = {
        "observed_model": "actual-model",
        "observed_effort": "high",
        "usage": {"output": 999, "provenance": "Model-supplied fixture usage."},
    }
    response[invalid_field] = "x" * 257
    artifacts.append("response.json", json.dumps(response).encode())
    artifacts.append(
        "stdout.bin",
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": output,
                    "reasoning_output_tokens": 0,
                },
            }
        ).encode(),
    )
    observation = failure_observation(recipe, artifacts)
    assert observation.observed_model == (
        None if invalid_field == "observed_model" else "actual-model"
    )
    assert observation.observed_effort == (None if invalid_field == "observed_effort" else "high")
    if output:
        assert observation.usage.output == output
    else:
        assert observation.usage is None


@pytest.mark.parametrize("invalid_source", ["response.json", "stdout.bin"])
def test_codex_failure_observation_rejects_invalid_json_per_source(tmp_path, invalid_source):
    recipe = ControllerRecipe(
        kind="codex",
        isolation="trusted-native",
        command=CommandSpec(argv=("fixture",)),
        version="0.153.4",
        model="requested-model",
        effort="low",
    )
    artifacts = ArtifactWriter(tmp_path / "artifacts", 100000)
    for name, content in {
        "response.json": {
            "observed_model": "actual-model",
            "observed_effort": "high",
            "usage": {"output": 999, "provenance": "Model-supplied fixture usage."},
        },
        "stdout.bin": {"type": "turn.completed", "usage": {"output_tokens": 4}},
    }.items():
        artifacts.append(
            name,
            json.dumps(content).encode() + (b" trailing data" if name == invalid_source else b""),
        )
    observation = failure_observation(recipe, artifacts)
    if invalid_source == "response.json":
        assert observation.observed_model is None
        assert observation.observed_effort is None
        assert observation.usage.output == 4
    else:
        assert observation.observed_model == "actual-model"
        assert observation.observed_effort == "high"
        assert observation.usage is None
