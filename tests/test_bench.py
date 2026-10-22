import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import codex_manifest, judged_case_benchmark, live_codex_home
from dryheave.bench import (
    CODEX_SANDBOX_ARGUMENTS,
    JUDGE_BUDGET,
    RoleChoice,
    VariantChoice,
    attempt_progress_lines,
    bench_draft,
    capture_variants,
    check_case,
    check_workflow,
    claude_role,
    parse_role,
    parse_variant,
    resumable_run,
    simulator_budget,
    summarize,
    summary_live_config,
    variant_recipe,
)
from dryheave.cases import PromptStep
from dryheave.cli import main
from dryheave.drivers.base import DeliveryUncertain
from dryheave.errors import InputError
from dryheave.experiment_models import ScoringConfig
from dryheave.experiments import create_experiment, load_experiment, load_role
from dryheave.journals import RunStore
from dryheave.live_config import live_source, snapshot_directory, snapshot_live_config
from dryheave.models import AgentKind, EnvironmentReference, JournalEvent, ObjectKind
from dryheave.pricing import builtin_price_table
from dryheave.profile_launch import LiveSnapshots
from dryheave.profile_models import LiveConfigManifest, RuntimeFileReference
from dryheave.profiles import load_profile
from dryheave.report_models import (
    AttemptReport,
    AttemptTopline,
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
from dryheave.serialization import canonical_json


def test_roles_and_variants_parse_model_effort_and_changes():
    assert parse_role("claude-opus-5-5@high") == RoleChoice("claude-opus-5-5", "high")
    assert parse_role("claude-haiku-4-5") == RoleChoice("claude-haiku-4-5", None)
    assert parse_variant("no-jina=claude-sonnet-4-6@low,-mcp:jina,-mcp:context7") == VariantChoice(
        "no-jina", AgentKind.CLAUDE, RoleChoice("claude-sonnet-4-6", "low"), ("jina", "context7")
    )


def test_variants_take_an_optional_agent_prefix():
    assert parse_variant("luna-high=codex:gpt-5.6-luna@high") == VariantChoice(
        "luna-high", AgentKind.CODEX, RoleChoice("gpt-5.6-luna", "high"), ()
    )
    assert parse_variant("sonnet=claude:claude-sonnet-4-6@low") == VariantChoice(
        "sonnet", AgentKind.CLAUDE, RoleChoice("claude-sonnet-4-6", "low"), ()
    )


def test_codex_variants_refuse_mcp_removal():
    with pytest.raises(InputError) as raised:
        parse_variant("terra=codex:gpt-5.6-terra@high,-mcp:jina")
    assert str(raised.value) == (
        "Variant terra: MCP server removal applies only to live Claude configs."
    )


def test_a_colon_in_the_model_name_is_not_mistaken_for_an_agent_prefix():
    assert parse_variant("oss=codex:gpt-oss:20b") == VariantChoice(
        "oss", AgentKind.CODEX, RoleChoice("gpt-oss:20b", None), ()
    )
    bedrock = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert parse_variant(f"br={bedrock}") == VariantChoice(
        "br", AgentKind.CLAUDE, RoleChoice(bedrock, None), ()
    )


@pytest.mark.parametrize(
    ("text", "agent"),
    [
        ("luna=codx:gpt-5.6-luna", "codx"),
        ("luna=Codex:gpt-5.6-luna", "Codex"),
        ("a=droid:m", "droid"),
    ],
)
def test_an_unknown_agent_prefix_is_refused_rather_than_read_as_a_claude_model(text, agent):
    with pytest.raises(InputError) as raised:
        parse_variant(text)
    assert str(raised.value) == (
        f"Variant {text.split('=')[0]}: unknown agent {agent!r}; AGENT is claude or codex."
    )


def test_variants_are_refused_with_their_name_when_the_profile_contract_rejects_them():
    with pytest.raises(InputError) as raised:
        parse_variant("haiku=claude-haiku-4-5@low")
    assert str(raised.value).startswith(
        "Variant haiku: Explicit Claude effort requires a supported Sonnet or Opus model"
    )


@pytest.mark.parametrize(
    "text",
    [
        "sonnet",
        "=claude-sonnet-4-6",
        "a=b@c@d",
        "a=claude-sonnet-4-6,+skill:x",
        "a=m,-mcp:",
        "a=codex:",
    ],
)
def test_malformed_variants_are_rejected(text):
    with pytest.raises(InputError):
        parse_variant(text)


EXECUTABLES = {AgentKind.CLAUDE: "claude", AgentKind.CODEX: "codex"}


def test_variant_profiles_are_live_trusted_and_validate_effort(store):
    haiku = VariantChoice("haiku", AgentKind.CLAUDE, RoleChoice("claude-haiku-4-5", None), ())
    sonnet = VariantChoice(
        "sonnet", AgentKind.CLAUDE, RoleChoice("claude-sonnet-4-6", "low"), ("jina",)
    )
    profiles = capture_variants(store, (haiku, sonnet), EXECUTABLES)
    recipe = load_profile(store, profiles["sonnet"]).recipe
    assert (recipe.config_source, recipe.workspace_trust, recipe.removed_mcp_servers) == (
        "live",
        "trusted",
        ("jina",),
    )
    assert capture_variants(store, (haiku,), EXECUTABLES)["haiku"] == profiles["haiku"]
    with pytest.raises(ValidationError):
        variant_recipe(
            VariantChoice("bad", AgentKind.CLAUDE, RoleChoice("claude-haiku-4-5", "low"), ()),
            "claude",
        )
    with pytest.raises(InputError, match="unique"):
        capture_variants(store, (haiku, haiku), EXECUTABLES)


def test_codex_variant_profiles_run_live_codex_with_full_access_and_bound_auth(store):
    terra = parse_variant("terra-high=codex:gpt-5.6-terra@high")
    sonnet = parse_variant("sonnet=claude-sonnet-4-6@low")
    profiles = capture_variants(
        store, (terra, sonnet), {AgentKind.CLAUDE: "claude", AgentKind.CODEX: "/opt/codex"}
    )
    recipe = load_profile(store, profiles["terra-high"]).recipe
    assert (
        recipe.agent,
        recipe.executable,
        recipe.model,
        recipe.effort,
        recipe.config_source,
        recipe.workspace_trust,
        recipe.arguments,
        recipe.environment,
    ) == (
        AgentKind.CODEX,
        "/opt/codex",
        "gpt-5.6-terra",
        "high",
        "live",
        "trusted",
        CODEX_SANDBOX_ARGUMENTS,
        (),
    )
    assert recipe.runtime_files == (
        RuntimeFileReference(
            name="codex-auth",
            source_path_environment=EnvironmentReference(name="CODEX_AUTH_FILE"),
            acknowledge_source_writes=True,
            default_source="codex-home",
        ),
    )
    assert load_profile(store, profiles["sonnet"]).recipe.environment == (
        EnvironmentReference(name="CLAUDE_CODE_OAUTH_TOKEN"),
    )
    with pytest.raises(ValidationError):
        variant_recipe(
            VariantChoice("bad", AgentKind.CODEX, RoleChoice("gpt-5.6-luna", "extreme"), ()),
            "codex",
        )


def _live_config_event(sequence: int, manifest: LiveConfigManifest) -> JournalEvent:
    return JournalEvent(
        sequence=sequence,
        previous_hash=None,
        timestamp=datetime.now(UTC),
        event="live-config",
        data=manifest.model_dump(mode="json"),
    )


MANIFEST_KEYS = {
    "schema_version",
    "source",
    "entries",
    "files",
    "bytes",
    "tree_sha256",
    "settings_sha256",
    "instructions_sha256",
    "skills",
    "enabled_plugins",
    "mcp_servers",
    "hook_events",
}


@pytest.mark.parametrize("agents", [("codex",), ("claude",), ("claude", "codex")])
def test_summary_live_config_keys_each_journaled_manifest_by_its_agent(tmp_path, agents):
    run = tmp_path / "run"
    run.mkdir()
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "CLAUDE.md").write_text("Be terse.")
    environment = {
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "CODEX_HOME": str(live_codex_home(tmp_path)),
    }
    events = [
        _live_config_event(
            sequence,
            snapshot_live_config(
                live_source(agent, environment, tmp_path), run / snapshot_directory(agent)
            ),
        )
        for sequence, agent in enumerate(map(AgentKind, agents), start=1)
    ]
    summary = summary_live_config(events)
    assert list(summary) == list(agents)
    keys = {"claude": MANIFEST_KEYS | {"state_sha256"}, "codex": MANIFEST_KEYS}
    sources = {"claude": str(claude_home), "codex": environment["CODEX_HOME"]}
    assert {agent: set(manifest) for agent, manifest in summary.items()} == {
        agent: keys[agent] for agent in agents
    }
    assert {agent: manifest["source"] for agent, manifest in summary.items()} == {
        agent: sources[agent] for agent in agents
    }


def test_summary_live_config_reports_the_journal_not_the_snapshot_manifest_file(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    snapshot = run / snapshot_directory(AgentKind.CODEX)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(live_codex_home(tmp_path))}, tmp_path)
    recorded = snapshot_live_config(source, snapshot)
    forged = recorded.model_copy(update={"skills": ("forged",)})
    (snapshot / "manifest.json").write_bytes(canonical_json(forged))
    summary = summary_live_config([_live_config_event(1, recorded), _live_config_event(2, forged)])
    assert summary["codex"]["skills"] == ["review"]


def test_snapshot_for_check_writes_each_agent_to_its_own_snapshot_directory(tmp_path):
    from dryheave.bench import snapshot_for_check

    codex_home = live_codex_home(tmp_path)
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "CLAUDE.md").write_text("Be terse.")
    environment = {"CODEX_HOME": str(codex_home), "CLAUDE_CONFIG_DIR": str(claude_home)}
    root = tmp_path / "check"
    codex = snapshot_for_check(AgentKind.CODEX, root, environment=environment, home=tmp_path)
    claude = snapshot_for_check(AgentKind.CLAUDE, root, environment=environment, home=tmp_path)
    assert (codex.agent, codex.source) == (AgentKind.CODEX, str(codex_home))
    assert (claude.agent, claude.source) == (AgentKind.CLAUDE, str(claude_home))
    assert (root / "live-config-codex/AGENTS.md").read_text() == "Be brief."
    assert (root / "live-config/CLAUDE.md").read_text() == "Be terse."


def _skill_plan(variants, skills):
    from dryheave.runner import SlashTrial, WorkflowPlan

    return WorkflowPlan(
        True,
        {},
        tuple(
            SlashTrial(variant.name, variant_recipe(variant, variant.agent.value), skills)
            for variant in variants
            if skills
        ),
    )


def _check_snapshots(tmp_path, variants, *, skills=()):
    from dryheave import bench

    claude_home = tmp_path / "claude"
    claude_home.mkdir(exist_ok=True)
    (claude_home / ".claude.json").write_text('{"mcpServers": {"jina": {}}}')
    chosen = tuple(map(parse_variant, variants))
    return bench.check_snapshots(
        tmp_path / "check",
        chosen,
        _skill_plan(chosen, skills),
        environment={
            "CODEX_HOME": str(live_codex_home(tmp_path)),
            "CLAUDE_CONFIG_DIR": str(claude_home),
        },
        home=tmp_path,
    )


def test_check_snapshots_snapshot_each_agent_its_variants_use(tmp_path):
    manifests = _check_snapshots(
        tmp_path,
        ("terra=codex:gpt-5.6-terra@low", "no-jina=claude-sonnet-4-6@low,-mcp:jina"),
        skills=("review",),
    )
    assert {agent: manifest.source for agent, manifest in manifests.items()} == {
        AgentKind.CLAUDE: str(tmp_path / "claude"),
        AgentKind.CODEX: str(tmp_path / "codex"),
    }
    assert manifests[AgentKind.CLAUDE].mcp_servers == ("jina",)
    assert manifests[AgentKind.CODEX].skills == ("review",)


def test_check_snapshots_refuse_a_codex_variant_missing_a_workflow_skill(tmp_path):
    from dryheave.errors import MissingSkillError

    with pytest.raises(MissingSkillError) as raised:
        _check_snapshots(
            tmp_path,
            ("terra=codex:gpt-5.6-terra@low", "luna=codex:gpt-5.6-luna@high"),
            skills=("review", "release-notes"),
        )
    assert str(raised.value) == (
        "Variants terra, luna cannot run workflow skill 'release-notes': "
        "the Codex config has no skills/release-notes/SKILL.md."
    )


def test_check_snapshots_refuse_a_claude_variant_removing_an_unknown_mcp_server(tmp_path):
    with pytest.raises(InputError, match="not user-scoped in the live config: context7"):
        _check_snapshots(tmp_path, ("no-c7=claude-sonnet-4-6@low,-mcp:context7",))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["claude", "codex"]


def test_a_refused_snapshot_check_removes_its_config_copy(tmp_path):
    from dryheave.errors import MissingSkillError

    with pytest.raises(MissingSkillError, match="no skills/code-review"):
        _check_snapshots(tmp_path, ("terra=codex:gpt-5.6-terra@low",), skills=("code-review",))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["claude", "codex"]


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
        store, (parse_variant("sonnet=claude-sonnet-4-6@low"),), EXECUTABLES
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
    assert resumable_run(store, identifier, workflow=True) is None
    run_id = RunStore(store.root).create(identifier)
    previous = resumable_run(store, identifier, workflow=True)
    assert previous is not None
    assert (previous.run_id, previous.finished) == (run_id, False)
    scoring = load_role(
        store, load_experiment(store, identifier).scoring_id, ScoringConfig, ObjectKind.SCORING
    )
    assert scoring.prices == builtin_price_table()


def test_bench_resumes_only_a_run_with_the_same_workflow_setting(store, benchmark):
    from dryheave.runner_models import RunOptions
    from dryheave.runner_state import ExecutionJournal

    case_id = check_case(store, benchmark.cases[0], judged=False)
    profiles = capture_variants(
        store, (parse_variant("sonnet=claude-sonnet-4-6@low"),), EXECUTABLES
    )
    identifier = create_experiment(
        store,
        bench_draft(
            case_id, profiles, simulator=benchmark.simulator, judge=None, repetitions=1, seed=1
        ),
    )
    runs = RunStore(store.root)

    def frozen_run(workflow):
        run_id = runs.create(identifier)
        with runs.open(run_id) as journal:
            ExecutionJournal(journal).configure(
                RunOptions(mode="offline-fixture", workflow=workflow)
            )
        return run_id

    with_workflow = frozen_run(True)
    assert resumable_run(store, identifier, workflow=False) is None
    assert resumable_run(store, identifier, workflow=True).run_id == with_workflow
    without_workflow = frozen_run(False)
    assert resumable_run(store, identifier, workflow=False).run_id == without_workflow
    assert resumable_run(store, identifier, workflow=True).run_id == with_workflow


def criterion(identifier, outcome):
    return CriterionResult.model_construct(criterion_id=identifier, outcome=outcome)


def test_summary_counts_outcomes_per_variant_and_criterion():
    workflow_topline = AttemptTopline.model_construct(
        first_inspection="passed", fix_requests=1, workflow="2/2"
    )

    def attempt(identifier, variant, outcomes, topline=None):
        return AttemptReport.model_construct(
            attempt_id=identifier,
            variant=variant,
            result=Assessment.model_construct(
                completion="pass",
                exclusion_reasons=(),
                criteria=tuple(criterion(name, value) for name, value in outcomes),
            ),
            topline=topline,
        )

    report = RunReport.model_construct(
        attempts=(
            attempt("a1", "haiku", [("correctness", "fail"), ("tests", "pass")], workflow_topline),
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
    assert summary["haiku"]["attempts"][0] == {
        "attempt_id": "a1",
        "stop_reason": "completed",
        "completion": "pass",
        "exclusions": [],
        "first_inspection": "passed",
        "fix_requests": 1,
        "workflow": "2/2",
    }
    assert summary["haiku"]["attempts"][1]["workflow"] is None
    assert summary["sonnet"] == {
        "attempts": [
            {
                "attempt_id": "a3",
                "stop_reason": "needs_input",
                "completion": None,
                "exclusions": [],
                "first_inspection": None,
                "fix_requests": None,
                "workflow": None,
            }
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


class _CodexTerminal(_MenuTerminal):
    def state(self):
        from dryheave.drivers.models import Screen

        shown = self.typed or "Ask Codex to do anything"
        return Screen(
            text=f"\u203a {shown}\n\n  gpt-5.6-terra high",
            exited=None,
            cursor={"x": 2 + len(self.typed), "y": 0},
        )


def _launch_check(  # noqa: PLR0913
    store,
    tmp_path,
    monkeypatch,
    terminal,
    *,
    commands=(),
    workflow=True,
    agent=None,
    environment=None,
    runtime_files=(),
    linked=None,
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from dryheave import bench
    from dryheave.drivers.models import Observation
    from dryheave.models import AgentKind

    agent = agent or AgentKind.CLAUDE
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
            if linked is not None:
                linked.append(os.readlink(Path(plan.config_roots["config"]) / "auth.json"))
            return Observation(state="ready", reason="Composer is empty.", text="> ")

    plan = SimpleNamespace(
        launchable=True,
        issues=(),
        config_roots={"config": str(tmp_path)},
        agent=agent,
        runtime_files=runtime_files,
    )
    options = []

    def materialize(*args):
        options.append(args[-1])
        return plan

    monkeypatch.setattr(bench, "materialize_profile", materialize)
    if not runtime_files:
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
        environment=environment or {},
        manifests={AgentKind.CODEX: codex_manifest("code-review")},
        workflow=workflow,
        commands=commands,
    )
    assert options[0].live == LiveSnapshots(
        tmp_path / "checks", {AgentKind.CODEX: codex_manifest("code-review")}
    )
    return observation, terminals[0]


def test_launch_check_binds_the_default_codex_auth_file_under_the_inherited_home(
    store, tmp_path, monkeypatch
):
    from functools import partial

    from dryheave.profile_models import codex_auth_reference

    auth = tmp_path / "user/.codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    environment = {"HOME": str(tmp_path / "user")}
    linked: list[str] = []
    observation, _terminal = _launch_check(
        store,
        tmp_path,
        monkeypatch,
        partial(_CodexTerminal, commands=()),
        agent=AgentKind.CODEX,
        environment=environment,
        runtime_files=(codex_auth_reference("CODEX_AUTH_FILE"),),
        linked=linked,
    )
    assert observation.state == "ready"
    assert linked == [str(auth)]
    assert environment == {"HOME": str(tmp_path / "user")}


class _CodexPopupTerminal(_CodexTerminal):
    def state(self):
        from dryheave.drivers.models import Screen

        if not self.typed.startswith("$"):
            return super().state()
        return Screen(
            text="\u203a $\n\n  code-review  Review the change.",
            exited=None,
            cursor={"x": 3, "y": 0},
        )


def test_launch_check_fails_a_codex_skill_mention_the_input_box_changes(
    store, tmp_path, monkeypatch
):
    from functools import partial

    observation, terminal = _launch_check(
        store,
        tmp_path,
        monkeypatch,
        partial(_CodexPopupTerminal, commands=()),
        commands=("/code-review --quick",),
        agent=AgentKind.CODEX,
    )
    assert (observation.state, observation.reason) == (
        "unsupported",
        (
            "Workflow command '$code-review' was not ready to submit as typed; "
            "check that the input box shows it unchanged."
        ),
    )
    assert (terminal.pastes, terminal.enters) == (["dryheave launch check", "$code-review"], 0)


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
        (
            "The typing probe lost its terminal acknowledgement: "
            "Terminal command ended without a delivery acknowledgement."
        ),
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


def test_launch_check_types_workflow_commands_into_codex_as_skill_mentions(
    store, tmp_path, monkeypatch
):
    from functools import partial

    observation, terminal = _launch_check(
        store,
        tmp_path,
        monkeypatch,
        partial(_CodexTerminal, commands=()),
        commands=("/code-review --quick", " \n/release-notes"),
        agent=AgentKind.CODEX,
    )
    assert observation.state == "ready"
    assert terminal.pastes == [
        "dryheave launch check",
        "$code-review",
        "$code-review --quick",
        "$release-notes",
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
        (
            f"Workflow command {command!r} was not ready to submit as typed; "
            "check that Claude's command menu offers it."
        ),
    )
    assert (terminal.pastes[-1], terminal.enters) == (command, 0)


@pytest.mark.parametrize("workflow", [True, False])
def test_bench_workflow_derives_slash_commands_and_codex_skills_from_the_experiment(
    store, benchmark, workflow
):
    from dryheave.bench import bench_workflow, slash_commands
    from dryheave.cases import load_frozen_case
    from dryheave.runner import codex_workflow_skills

    case_id = store.resolve(benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    steps = (
        PromptStep(step_id="review", prompt="/code-review --quick"),
        PromptStep(step_id="docs", prompt="Update the docs."),
        PromptStep(step_id="notes", prompt=" \n/release-notes"),
    )
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"workflow": steps}),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    profiles = capture_variants(
        store,
        (
            parse_variant("terra=codex:gpt-5.6-terra@low"),
            parse_variant("sonnet=claude-sonnet-4-6@low"),
        ),
        EXECUTABLES,
    )
    experiment_id = create_experiment(
        store,
        bench_draft(
            changed, profiles, simulator=benchmark.simulator, judge=None, repetitions=1, seed=0
        ),
    )
    plan = bench_workflow(store, experiment_id, workflow=workflow)
    assert (slash_commands(plan), codex_workflow_skills(plan)) == (
        (("/code-review --quick", " \n/release-notes"), {"terra": ("code-review", "release-notes")})
        if workflow
        else ((), {})
    )


def _workflow_case(*, workflow):
    from dryheave.cases import CaseContent, RubricCriterion

    return CaseContent(
        title="t",
        initial_prompt="p",
        criteria=(
            RubricCriterion(criterion_id="quality", required=True, rubric="Quality rubric text."),
        ),
        workflow=workflow,
    )


def test_check_workflow_refuses_an_inspect_step_without_judge_or_inspector():
    from dryheave.cases import InspectStep

    case = _workflow_case(workflow=(InspectStep(step_id="check", criteria=("quality",)),))
    with pytest.raises(InputError, match="--inspector"):
        check_workflow(case, workflow=True, scoring=ScoringConfig())


def test_check_workflow_allows_an_inspect_step_with_an_inspector():
    from dryheave.cases import InspectStep

    case = _workflow_case(workflow=(InspectStep(step_id="check", criteria=("quality",)),))
    inspector = claude_role(RoleChoice("claude-haiku-4-5", None), "claude", JUDGE_BUDGET)
    check_workflow(case, workflow=True, scoring=ScoringConfig(inspector=inspector))
    check_workflow(case, workflow=True, scoring=ScoringConfig(judge=inspector))


def test_check_workflow_ignores_inspect_steps_when_workflows_are_off():
    from dryheave.cases import InspectStep

    case = _workflow_case(workflow=(InspectStep(step_id="check", criteria=("quality",)),))
    check_workflow(case, workflow=False, scoring=ScoringConfig())


def test_check_workflow_allows_prompt_only_workflows_without_an_inspector():
    from dryheave.cases import PromptStep

    case = _workflow_case(workflow=(PromptStep(step_id="review", prompt="/code-review"),))
    check_workflow(case, workflow=True, scoring=ScoringConfig())


def test_attempt_progress_lines_name_stop_reason_and_workflow_fields():
    workflow_topline = AttemptTopline.model_construct(
        first_inspection="failed", fix_requests=2, workflow="1/2"
    )
    report = RunReport.model_construct(
        attempts=(
            AttemptReport.model_construct(
                attempt_id="a1", variant="haiku", result=None, topline=workflow_topline
            ),
            AttemptReport.model_construct(attempt_id="a2", variant="haiku", result=None),
        ),
        groups=(),
    )
    lines = attempt_progress_lines(report, {"a1": "workflow_complete", "a2": "needs_input"})
    assert lines == (
        "attempt a1 stopped (workflow_complete); workflow 1/2; fix requests 2; first check failed",
        "attempt a2 stopped (needs_input)",
    )


def test_bench_draft_sets_the_inspector_scoring_recipe(store, benchmark):
    case_id = check_case(store, benchmark.cases[0], judged=False)
    profiles = capture_variants(
        store, (parse_variant("sonnet=claude-sonnet-4-6@low"),), EXECUTABLES
    )
    inspector = claude_role(RoleChoice("claude-haiku-4-5", None), "claude", JUDGE_BUDGET)
    draft = bench_draft(
        case_id,
        profiles,
        simulator=benchmark.simulator,
        judge=None,
        inspector=inspector,
        repetitions=1,
        seed=0,
    )
    assert draft.scoring.inspector == inspector
