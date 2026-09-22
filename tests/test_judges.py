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


@pytest.mark.integration
def test_deterministic_criterion_named_judge_has_separate_artifacts(
    store, graded_benchmark, tmp_path
):
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                case.criteria[0].model_copy(update={"criterion_id": "judge"}),
                RubricCriterion(
                    criterion_id="quality",
                    rubric="The implementation preserves the existing public interface.",
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
            {"criterion_id": "quality", "outcome": "pass", "rationale": "Interface preserved."}
        ]
    }
    script = tmp_path / "judge.py"
    script.write_text("print(" + repr(json.dumps(response)) + ")\n")
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )
    summary = run_experiment(
        store,
        create_experiment(
            store, graded_benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})
        ),
        options=RunOptions(mode="offline-fixture"),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [(item.criterion_id, item.outcome) for item in result.criteria] == [
        ("judge", "pass"),
        ("quality", "pass"),
    ]
    assert result.completion == "pass"
    assert result.eligible
    evidence = store.read_blobs(result.evidence_id)
    assert any("/criteria/judge/final/" in name for name in evidence)
    assert any("/judge/j-" in name for name in evidence)


@pytest.mark.integration
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


@pytest.mark.integration
@pytest.mark.parametrize("invalid", [False, True])
def test_claude_judge_uses_structured_protocol_and_provider_usage(
    store, benchmark, tmp_path, invalid
):
    import threading
    import time

    from dryheave.controllers import CallContext
    from dryheave.drivers.artifacts import ArtifactWriter
    from dryheave.judges import JudgeInput, invoke_judge
    from test_claude_controller import FIXTURE, fake_executable, recipe_for

    payload = json.loads(FIXTURE.read_bytes())
    payload["structured_output"] = {
        "judgments": [
            {
                "criterion_id": "unknown" if invalid else "quality",
                "outcome": "pass",
                "score": 0.75,
                "rationale": "Synthetic rubric fixture.",
            },
        ]
    }
    recipe = recipe_for(fake_executable(tmp_path, payload))
    request = JudgeInput(
        initial_prompt="Review the greeting.",
        criteria=(
            RubricCriterion(criterion_id="quality", rubric="Preserve the public interface."),
        ),
        files={"greet.py": "synthetic"},
        omitted_binary_files=(),
    )
    context = CallContext(
        call_id="judge-call",
        index=0,
        deadline=time.monotonic() + 3,
        cancelled=threading.Event(),
        inherited={"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-offline-value"},
        on_identity=lambda _identity: None,
        runtime_root=tmp_path / "runtime",
    )
    call, results = invoke_judge(
        recipe, request, ArtifactWriter(tmp_path / "evidence", 1000000), context
    )
    assert call.status == ("failed" if invalid else "completed")
    assert call.usage.output == 12
    assert call.observed_model == "claude-haiku-4-5-20251001"
    assert call.observed_effort is None
    assert call.cleanup.known_writers_stopped
    assert results[0].outcome == ("error" if invalid else "pass")


@pytest.mark.integration
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
