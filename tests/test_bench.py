import pytest
from pydantic import ValidationError

from conftest import judged_case_benchmark
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
from dryheave.drivers.base import DeliveryUncertain
from dryheave.errors import InputError
from dryheave.experiment_models import ScoringConfig
from dryheave.experiments import create_experiment, load_experiment, load_role
from dryheave.journals import RunStore
from dryheave.models import ObjectKind
from dryheave.pricing import builtin_price_table
from dryheave.profiles import load_profile
from dryheave.report_models import (
    AttemptReport,
    CheckSummary,
    Distribution,
    GroupMetrics,
    MetricSummary,
    RunReport,
)
from dryheave.result_models import (
    Assessment,
    CriterionResult,
    HelpNeeded,
    RoleMetrics,
    UsageCharge,
)


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


def test_check_case_refuses_checks_or_judge_criteria_without_judge(
    store, graded_benchmark, tmp_path
):
    _benchmark, case_id = judged_case_benchmark(store, graded_benchmark, tmp_path)
    with pytest.raises(InputError, match="--judge"):
        check_case(store, case_id, judged=False)
    assert check_case(store, case_id, judged=True) == case_id


@pytest.mark.parametrize("kind", ["checks", "judge-criterion"])
def test_check_case_refuses_a_checks_only_or_judge_criterion_only_case_without_judge(
    store, graded_benchmark, kind
):
    from dryheave.cases import CaseCheck, RubricCriterion, load_frozen_case

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    update = (
        {
            "checks": (
                CaseCheck(
                    check_id="asked",
                    question="Did the agent ask which punctuation to use?",
                    expect="yes",
                ),
            )
        }
        if kind == "checks"
        else {
            "criteria": (
                *case.criteria,
                RubricCriterion(
                    criterion_id="quality", rubric="The greeting keeps its public interface."
                ),
            )
        }
    )
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(update=update),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    assert check_case(store, case_id, judged=False) == case_id
    with pytest.raises(InputError, match="--judge"):
        check_case(store, changed, judged=False)


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
    scoring = load_role(
        store, load_experiment(store, identifier).scoring_id, ScoringConfig, ObjectKind.SCORING
    )
    assert scoring.prices == builtin_price_table()


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
        ),
        groups=(),
    )
    summary = summarize(report, {"a1": "completed", "a2": "completed", "a3": "needs_input"})
    assert summary["haiku"]["criteria"] == {
        "correctness": {"fail": 1, "pass": 1},
        "tests": {"pass": 2},
    }
    assert summary["haiku"]["measures"] == {}
    assert summary["sonnet"] == {
        "attempts": [
            {"attempt_id": "a3", "stop_reason": "needs_input", "completion": None, "exclusions": []}
        ],
        "criteria": {},
        "measures": {},
    }


def test_summary_includes_cost_tokens_checks_metrics_and_help_needed():
    def whole(value):
        return Distribution(
            count=1, unknown=0, minimum=value, maximum=value, mean=value, median=value, stdev=0.0
        )

    role_records = (
        UsageCharge.model_construct(
            identity="subject",
            usage=None,
            cost=0.5,
            price_version="anthropic-2026-10-07",
            raw_evidence_sha256="a" * 64,
            reason="observed",
        ),
    )
    attempt = AttemptReport.model_construct(
        attempt_id="a1",
        trial_id="t1",
        variant="haiku",
        current_exclusions=(),
        roles=(RoleMetrics.model_construct(role="subject", records=role_records),),
        result=Assessment.model_construct(
            completion="pass",
            eligible=True,
            exclusion_reasons=(),
            criteria=(),
            help_needed=HelpNeeded(
                simulator_replies=2,
                facts_disclosed=("punctuation",),
                proactive_facts=("punctuation",),
                on_request_facts=(),
            ),
        ),
    )
    group = GroupMetrics.model_construct(
        variant="haiku",
        costs=whole(0.5),
        cost_by_role={"subject": whole(0.5)},
        accepted_turns=whole(3),
        durations=whole(42.0),
        cache_read_tokens=whole(10),
        cache_write_tokens=whole(5),
        total_input_tokens=whole(100),
        checks=(
            CheckSummary(
                case_id="b" * 64,
                check_id="final-right-view",
                passes=2,
                fails=0,
                not_applicable=0,
                errors=0,
                agreement=1.0,
            ),
        ),
        metrics=(
            MetricSummary(
                case_id="b" * 64,
                metric_id="tests_passed",
                label="Tests passed",
                unit="tests",
                values=whole(12),
            ),
        ),
    )
    report = RunReport.model_construct(attempts=(attempt,), groups=(group,))
    measures = summarize(report, {"a1": "completed"})["haiku"]["measures"]
    assert measures["price_versions"] == ["anthropic-2026-10-07"]
    assert measures["help_needed"] == {
        "measured_attempts": 1,
        "facts_disclosed": {"punctuation": 1},
        "disclosed_proactively": {"punctuation": 1},
        "disclosed_on_request": {},
    }
    assert measures["checks"][0]["agreement"] == 1.0
    assert measures["metrics"][0]["label"] == "Tests passed"
    assert measures["cost_by_role"]["subject"]["mean"] == 0.5


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


class _ProbeTerminal:
    def __init__(self, transport, runtime, *, limits, fail_on):
        self.limits = limits
        self.fail_on = fail_on

    def paste(self, text):
        if self.fail_on == "paste":
            raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")

    def clear_line(self):
        if self.fail_on == "clear":
            raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")

    def state(self):
        from dryheave.drivers.models import Screen

        return Screen(text="> dryheave launch check", exited=None, ready=True)


class _MenuTerminal:
    def __init__(self, transport, runtime, *, limits, commands):
        self.limits = limits
        self.commands = commands
        self.typed = ""
        self.pastes = []
        self.clears = 0
        self.enters = 0

    def paste(self, text):
        self.pastes.append(text)
        self.typed = text

    def clear_line(self):
        self.clears += 1
        self.typed = ""

    def enter(self):
        self.enters += 1

    def state(self):
        from dryheave.drivers.models import Screen

        rule = "\u2500" * 60
        offered = [name for name in self.commands if name.startswith(self.typed)]
        menu = (
            [f"  \u276f {offered[0]:<30}Skill.", *(f"    {name:<30}Skill." for name in offered[1:])]
            if self.typed.startswith("/") and " " not in self.typed and offered
            else [f'  No commands match "{self.typed}"']
            if self.typed.startswith("/") and " " not in self.typed
            else []
        )
        return Screen(
            text="\n".join([*menu, rule, f"\u276f {self.typed}", rule]),
            exited=None,
            cursor={"x": 2 + len(self.typed), "y": len(menu) + 1},
        )


def _launch_check(store, tmp_path, monkeypatch, terminal, commands=(), workflow=True):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from dryheave import bench
    from dryheave.drivers.models import Observation
    from dryheave.models import AgentKind

    terminals = []

    def build(*args, **kwargs):
        terminals.append(terminal(*args, **kwargs))
        return terminals[-1]

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def start(self, plan, environment):
            return Observation(state="ready", reason="Composer is empty.", text="> ")

    plan = SimpleNamespace(
        launchable=True, issues=(), config_roots={"config": str(tmp_path)}, agent=AgentKind.CLAUDE
    )
    monkeypatch.setattr(bench, "materialize_profile", lambda *args, **kwargs: plan)
    monkeypatch.setattr(bench, "RuntimeBindings", lambda *args: nullcontext())
    monkeypatch.setattr(bench, "launch_environment", lambda *args: {})
    monkeypatch.setattr(bench, "NativeLogSource", lambda *args, **kwargs: None)
    monkeypatch.setattr(bench, "DriverSession", Session)
    monkeypatch.setattr(bench, "TuiTestTerminal", build)
    monkeypatch.setattr(bench, "_settles", lambda terminal, condition: condition(terminal.state()))
    observation = bench.launch_check(
        store,
        "a" * 64,
        tmp_path / "checks",
        paths=(tmp_path / "rt", tmp_path / "tui-test"),
        environment={},
        workflow=workflow,
        commands=commands,
    )
    return observation, terminals[0]


@pytest.mark.parametrize("fail_on", ["paste", "clear"])
def test_launch_check_reports_a_lost_probe_acknowledgement_as_unsupported(
    store, tmp_path, monkeypatch, fail_on
):
    from functools import partial

    from dryheave import bench

    monkeypatch.setattr(bench, "submittable", lambda agent, screen, text: True)
    observation, terminal = _launch_check(
        store, tmp_path, monkeypatch, partial(_ProbeTerminal, fail_on=fail_on)
    )
    assert terminal.limits.duration_seconds == bench.LAUNCH_CHECK_SECONDS == 180
    assert (observation.state, observation.reason, observation.text) == (
        "unsupported",
        "The typing probe lost its terminal acknowledgement: "
        "Terminal command ended without a delivery acknowledgement.",
        "> ",
    )


def test_launch_check_types_each_workflow_command_through_the_menu_without_submitting(
    store, tmp_path, monkeypatch
):
    from functools import partial

    offered = ("/code-review", "/review-plan")
    observation, terminal = _launch_check(
        store,
        tmp_path,
        monkeypatch,
        partial(_MenuTerminal, commands=offered),
        commands=("/code-review --quick", "/review-plan"),
    )
    assert observation.state == "ready"
    assert terminal.pastes == [
        "dryheave launch check",
        "/code-review",
        "/code-review --quick",
        "/review-plan",
    ]
    assert (terminal.clears, terminal.enters, terminal.typed) == (4, 0, "")


def test_launch_check_skips_slash_probing_when_workflows_are_disabled(store, tmp_path, monkeypatch):
    from functools import partial

    observation, terminal = _launch_check(
        store,
        tmp_path,
        monkeypatch,
        partial(_MenuTerminal, commands=()),
        commands=("/code-review --quick",),
        workflow=False,
    )
    assert observation.state == "ready"
    assert terminal.pastes == ["dryheave launch check"]
    assert (terminal.clears, terminal.enters) == (1, 0)


@pytest.mark.parametrize(
    ("offered", "command"),
    [(("/code-review",), "/simplify"), (("/review-plan", "/review"), "/review")],
)
def test_launch_check_fails_a_workflow_command_the_menu_does_not_highlight(
    store, tmp_path, monkeypatch, offered, command
):
    from functools import partial

    observation, terminal = _launch_check(
        store,
        tmp_path,
        monkeypatch,
        partial(_MenuTerminal, commands=offered),
        commands=(command,),
    )
    assert (observation.state, observation.reason) == (
        "unsupported",
        f"Workflow command {command!r} was not ready to submit as typed; "
        "check that Claude's command menu offers it.",
    )
    assert (terminal.pastes[-1], terminal.enters) == (command, 0)


def test_slash_prompts_are_the_case_workflow_commands(store, monkeypatch):
    from dryheave import bench
    from dryheave.cases import CaseContent, InspectStep, PromptStep, RubricCriterion

    case = CaseContent(
        title="t",
        initial_prompt="p",
        criteria=(
            RubricCriterion(criterion_id="quality", required=True, rubric="Quality rubric text."),
        ),
        workflow=(
            InspectStep(step_id="check", criteria=("quality",)),
            PromptStep(step_id="review", prompt="/code-review --quick"),
            PromptStep(step_id="docs", prompt="Update the docs."),
        ),
    )
    monkeypatch.setattr(bench, "load_frozen_case", lambda store, case_id: case)
    assert bench.slash_prompts(store, "case") == ("/code-review --quick",)
