import pytest
from pydantic import ValidationError

from dryheave.bench import (
    JUDGE_BUDGET,
    RoleChoice,
    VariantChoice,
    bench_draft,
    capture_variants,
    check_case,
    claude_role,
    parse_role,
    parse_variant,
    resumable_run,
    simulator_budget,
    summarize,
    variant_recipe,
)
from dryheave.cli import main
from dryheave.errors import InputError
from dryheave.experiments import create_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.profiles import load_profile
from dryheave.report_models import AttemptReport, RunReport
from dryheave.result_models import Assessment, CriterionResult


def test_roles_and_variants_parse_model_effort_and_changes():
    assert parse_role("claude-opus-5-5@high") == RoleChoice("claude-opus-5-5", "high")
    assert parse_role("claude-haiku-4-5") == RoleChoice("claude-haiku-4-5", None)
    assert parse_variant("no-jina=claude-sonnet-4-6@low,-mcp:jina,-mcp:context7") == VariantChoice(
        "no-jina", RoleChoice("claude-sonnet-4-6", "low"), ("jina", "context7")
    )


@pytest.mark.parametrize(
    "text", ["sonnet", "=claude-sonnet-4-6", "a=b@c@d", "a=claude-sonnet-4-6,+skill:x", "a=m,-mcp:"]
)
def test_malformed_variants_are_rejected(text):
    with pytest.raises(InputError):
        parse_variant(text)


def test_variant_profiles_are_live_trusted_and_validate_effort(store):
    haiku = VariantChoice("haiku", RoleChoice("claude-haiku-4-5", None), ())
    sonnet = VariantChoice("sonnet", RoleChoice("claude-sonnet-4-6", "low"), ("jina",))
    profiles = capture_variants(store, (haiku, sonnet), "claude")
    recipe = load_profile(store, profiles["sonnet"]).recipe
    assert (recipe.config_source, recipe.workspace_trust, recipe.removed_mcp_servers) == (
        "live",
        "trusted",
        ("jina",),
    )
    assert capture_variants(store, (haiku,), "claude")["haiku"] == profiles["haiku"]
    with pytest.raises(ValidationError):
        variant_recipe(VariantChoice("bad", RoleChoice("claude-haiku-4-5", "low"), ()), "claude")
    with pytest.raises(InputError, match="unique"):
        capture_variants(store, (haiku, haiku), "claude")


def test_role_command_timeout_follows_the_per_call_budget():
    judge = claude_role(RoleChoice("claude-opus-5-5", "high"), "claude", JUDGE_BUDGET)
    assert judge.command is not None
    assert judge.command.timeout_seconds == JUDGE_BUDGET.call_seconds == 300
    assert judge.version is None
    simulator = claude_role(
        RoleChoice("claude-sonnet-4-6", "low"),
        "claude",
        simulator_budget(14),
        conversation_policy="design-approval",
    )
    assert simulator.conversation_policy == "design-approval"
    assert (simulator.budget.max_calls, simulator.budget.total_seconds) == (14, 2520)
    assert simulator_budget(0).total_seconds == 180
    assert simulator_budget(30).total_seconds == 3600


def test_bench_experiment_is_content_addressed_and_resumable(store, benchmark):
    case_id = check_case(store, benchmark.cases[0], judged=False)
    profiles = capture_variants(
        store, (VariantChoice("sonnet", RoleChoice("claude-sonnet-4-6", "low"), ()),), "claude"
    )

    def draft():
        return bench_draft(
            case_id,
            profiles,
            simulator=benchmark.simulator,
            judge=None,
            repetitions=2,
            seed=17,
        )

    identifier = create_experiment(store, draft())
    assert create_experiment(store, draft()) == identifier
    assert [trial.variant for trial in load_experiment(store, identifier).trials] == [
        "sonnet",
        "sonnet",
    ]
    assert resumable_run(store, identifier) is None
    run_id = RunStore(store.root).create(identifier)
    previous = resumable_run(store, identifier)
    assert previous is not None
    assert (previous.run_id, previous.finished) == (run_id, False)


def criterion(identifier, outcome):
    return CriterionResult.model_construct(criterion_id=identifier, outcome=outcome)


def test_summary_counts_outcomes_per_variant_and_criterion():
    def attempt(identifier, variant, outcomes):
        return AttemptReport.model_construct(
            attempt_id=identifier,
            variant=variant,
            result=Assessment.model_construct(
                completion="pass",
                exclusion_reasons=(),
                criteria=tuple(criterion(name, value) for name, value in outcomes),
            ),
        )

    report = RunReport.model_construct(
        attempts=(
            attempt("a1", "haiku", [("correctness", "fail"), ("tests", "pass")]),
            attempt("a2", "haiku", [("correctness", "pass"), ("tests", "pass")]),
            AttemptReport.model_construct(attempt_id="a3", variant="sonnet", result=None),
        )
    )
    summary = summarize(report, {"a1": "completed", "a2": "completed", "a3": "needs_input"})
    assert summary["haiku"]["criteria"] == {
        "correctness": {"fail": 1, "pass": 1},
        "tests": {"pass": 2},
    }
    assert summary["sonnet"] == {
        "attempts": [
            {"attempt_id": "a3", "stop_reason": "needs_input", "completion": None, "exclusions": []}
        ],
        "criteria": {},
    }


def test_bench_requires_the_claude_credential_reference(
    store, benchmark, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    code = main(
        [
            "--store",
            str(store.root),
            "bench",
            benchmark.cases[0],
            "--variant",
            "sonnet=claude-sonnet-4-6@low",
            "--runtime-root",
            str(tmp_path / "rt"),
            "--tui-test",
            str(tmp_path / "tui-test"),
        ]
    )
    assert code == 2
    assert "CLAUDE_CODE_OAUTH_TOKEN is not set" in capsys.readouterr().err
