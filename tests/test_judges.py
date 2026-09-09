import json
import sys

import pytest

from dryheave.assessments import assess_run
from dryheave.cases import RubricCriterion, load_frozen_case
from dryheave.controller_models import ControllerRecipe
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.integrity import load_assessment
from dryheave.models import CommandSpec, ObjectKind
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions


def test_partial_judge_response_retains_valid_judgment_and_spend(store, graded_benchmark, tmp_path):
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                *case.criteria,
                RubricCriterion(
                    criterion_id="quality",
                    rubric="The implementation should remain readable and concise.",
                ),
                RubricCriterion(
                    criterion_id="design",
                    rubric="The implementation should preserve the existing public interface.",
                ),
            )
        }
    )
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    response = {
        "judgments": [
            {
                "criterion_id": "quality",
                "outcome": "pass",
                "score": 0.8,
                "rationale": "The code uses a concise direct implementation.",
            }
        ],
        "usage": {
            "uncached_input": 100,
            "cache_read": 10,
            "cache_write": 0,
            "output": 50,
            "reasoning": 20,
            "provenance": "fixture response",
        },
        "observed_model": "fixture-model",
    }
    script = tmp_path / "judge.py"
    script.write_text(
        "import json,sys\nrequest=json.load(sys.stdin)\nassert request['criteria'][0]['criterion_id']=='quality'\nprint("
        + repr(json.dumps(response))
        + ")\n"
    )
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        ),
        prices=PriceTable(
            version="fixture-v1",
            effective_date="2026-09-01",
            currency="USD",
            rates=(
                PriceRate(
                    model="fixture-model",
                    uncached_input=1.0,
                    cache_read=0.1,
                    cache_write=1.0,
                    output=2.0,
                ),
            ),
        ),
    )
    benchmark = graded_benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})
    summary = run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.deterministic_completion == "pass"
    assert result.completion == "indeterminate"
    assert [(item.criterion_id, item.outcome) for item in result.criteria] == [
        ("greeting", "pass"),
        ("quality", "pass"),
        ("design", "error"),
    ]
    assert result.roles[2].known_cost == 201 / 1e6
    assert result.evidence_id
    assert result.criteria[1].evidence_hash
    assert "rationale" not in result.model_dump_json()


def test_codex_schema_requires_nullable_nested_fields_without_model_launch():
    from dryheave.controller_schema import controller_schema
    from dryheave.judges import JudgeResponse

    schema = controller_schema(JudgeResponse)
    judgment = schema["$defs"]["Judgment"]
    assert set(judgment["required"]) == set(judgment["properties"])
    assert "score" in judgment["required"]
    usage = schema["$defs"]["TokenUsage"]
    assert set(usage["required"]) == set(usage["properties"])
    assert {"type": "null"} in judgment["properties"]["score"]["anyOf"]


@pytest.mark.parametrize("completed,pending", [(False, True), (True, False), (True, True)])
def test_unfinished_assessment_and_export_reconstruct_durable_judge_spend(
    store, graded_benchmark, tmp_path, monkeypatch, completed, pending
):
    from dryheave.bundles import export_bundle, import_bundle
    from dryheave.controller_models import RoleCall
    from dryheave.reports import report_run
    from dryheave.storage import ObjectStore

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    active = []

    def interrupt(execution, state, *_args):
        if completed:
            call = RoleCall(call_id="completed-judge", role="judge", status="intent")
            execution.evidence(state, "judge-intent", call)
            execution.evidence(
                state,
                "judge-result",
                call.model_copy(
                    update={
                        "status": "completed",
                        "cost": 0.25,
                    }
                ),
            )
        if pending:
            execution.evidence(
                state,
                "judge-intent",
                RoleCall(call_id="pending-judge", role="judge", status="intent"),
            )
        active.append(report_run(store, summary.run_id))
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.assessments._grade", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assess_run(store, summary.run_id)
    report = report_run(store, summary.run_id)
    path = tmp_path / "pending.tar"
    export_bundle(store, summary.run_id, path)
    target = ObjectStore(tmp_path / "imported")
    portable = report_run(target, import_bundle(target, path).roots[0])
    for inspected in (*active, report, portable):
        attempt = inspected.attempts[0]
        assert attempt.result.phase == "audited"
        judge = attempt.roles[2]
        assert len(judge.records) == int(completed) + int(pending)
        assert sum(record.cost is None for record in judge.records) == int(pending)
        assert judge.known_cost == (0.25 if completed else None)
        assert inspected.groups[0].known_spend_by_currency == (
            {"UNSPECIFIED": 0.25} if completed else {}
        )
        assert inspected.groups[0].unknown_cost_records == int(pending)
