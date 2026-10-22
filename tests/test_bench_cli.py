from argparse import Namespace
from pathlib import Path

import pytest

from conftest import codex_manifest, live_codex_home
from dryheave import bench_cli
from dryheave.bench import parse_variant
from dryheave.cases import PromptStep
from dryheave.cli import build_parser
from dryheave.drivers.base import DeliveryUncertain
from dryheave.drivers.models import Observation
from dryheave.errors import InputError, MissingSkillError, SnapshotChangedError
from dryheave.host import HostContext
from dryheave.models import AgentKind
from dryheave.profiles import load_profile
from dryheave.runner import WorkflowPlan

REVIEW = WorkflowPlan(
    True, {"case": (PromptStep(step_id="review", prompt="/code-review --quick"),)}, ()
)
NO_WORKFLOW = WorkflowPlan(False, {}, ())
HOST = HostContext({}, Path("/nonexistent-home"))


def _parse_bench(*extra):
    return build_parser().parse_args(
        ["bench", "a-case", "--variant", "sonnet=claude-sonnet-4-6@low", *extra]
    )


def test_bench_workflow_defaults_on_and_no_workflow_turns_it_off():
    assert _parse_bench().workflow is True
    assert _parse_bench("--no-workflow").workflow is False


def test_bench_inspector_defaults_to_none_and_parses_model_effort():
    assert _parse_bench().inspector is None
    assert _parse_bench("--inspector", "claude-haiku-4-5@low").inspector == "claude-haiku-4-5@low"


def _run_launch_checks(store, variants, profiles, *, workflow=True, plan=NO_WORKFLOW):
    bench_cli._launch_checks(
        store,
        variants,
        profiles,
        (store.root / "rt", store.root / "tui-test"),
        workflow=workflow,
        plan=plan,
        host=HostContext({"HOME": str(store.root)}, store.root),
    )


def _launch_checks(store, monkeypatch, check):
    roots = []

    def snapshots(root, variants, plan, *, environment, home):
        roots.append(root)
        return {}

    monkeypatch.setattr(bench_cli, "check_snapshots", snapshots)
    monkeypatch.setattr(bench_cli, "launch_check", check)
    with pytest.raises(InputError) as raised:
        _run_launch_checks(
            store, (parse_variant("sonnet=claude-sonnet-4-6@low"),), {"sonnet": "a" * 64}
        )
    return str(raised.value), roots[0]


def test_launch_check_failure_names_its_reason_and_retained_evidence(store, monkeypatch):
    def unsupported(*args, **kwargs):
        return Observation(
            state="unsupported",
            reason="The typing probe lost its terminal acknowledgement: lost.",
            text="> ",
        )

    message, root = _launch_checks(store, monkeypatch, unsupported)
    assert message.startswith(
        "Variant sonnet did not reach an empty prompt (unsupported: The typing probe lost its "
        f"terminal acknowledgement: lost.). Nothing was sent to a model. Evidence: {root}\n"
    )


def test_launch_check_transport_failure_names_its_retained_evidence(store, monkeypatch):
    def uncertain(*args, **kwargs):
        raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")

    message, root = _launch_checks(store, monkeypatch, uncertain)
    assert message == (
        "Variant sonnet launch check lost its terminal transport (Terminal command ended without "
        f"a delivery acknowledgement.). Nothing was sent to a model. Evidence: {root}"
    )


@pytest.mark.parametrize("workflow", [True, False])
def test_launch_checks_probe_the_case_slash_prompts_with_the_snapshot_manifests(
    store, monkeypatch, workflow
):
    gated, seen = [], []
    manifests = {AgentKind.CODEX: codex_manifest("code-review")}

    def snapshots(root, variants, plan, *, environment, home):
        gated.append(([item.name for item in variants], plan, environment, home))
        return manifests

    def ready(store, profile, root, **kwargs):
        seen.append(
            (
                profile,
                kwargs["environment"],
                kwargs["manifests"],
                kwargs["workflow"],
                kwargs["commands"],
            )
        )
        return Observation(state="ready", reason="Composer is empty.", text="> ")

    monkeypatch.setattr(bench_cli, "check_snapshots", snapshots)
    monkeypatch.setattr(bench_cli, "launch_check", ready)
    _run_launch_checks(
        store,
        (parse_variant("a=codex:gpt-5.6-terra@low"), parse_variant("b=claude-haiku-4-5")),
        {"a": "a" * 64, "b": "b" * 64},
        workflow=workflow,
        plan=REVIEW,
    )
    environment = {"HOME": str(store.root)}
    assert gated == [(["a", "b"], REVIEW, environment, store.root)]
    assert seen == [
        (profile, environment, manifests, workflow, ("/code-review --quick",))
        for profile in ("a" * 64, "b" * 64)
    ]


def test_bench_with_a_codex_variant_refuses_a_missing_codex_auth_file(
    store, tmp_path, monkeypatch, capsys
):
    from dryheave.cli import main

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "reference-only")
    monkeypatch.delenv("CODEX_AUTH_FILE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(bench_cli, "check_case", lambda store, reference, judged: "case")
    code = main(
        [
            "--store",
            str(store.root),
            "bench",
            "a-case",
            "--variant",
            "luna=codex:gpt-5.6-luna@high",
        ]
    )
    assert code == 2
    assert f"Codex auth file {tmp_path / 'codex-home' / 'auth.json'} does not exist" in (
        capsys.readouterr().err
    )


def test_bench_parses_the_codex_executable():
    assert _parse_bench().codex == "codex"
    assert _parse_bench("--codex", "/opt/codex").codex == "/opt/codex"


def _bench_args(case, *variants, codex="codex"):
    return Namespace(
        case=case,
        variant=list(variants),
        judge=None,
        inspector=None,
        workflow=True,
        simulator="claude-sonnet-4-6@low",
        simulator_replies=14,
        repetitions=1,
        seed=0,
        claude="/opt/claude",
        codex=codex,
        check_only=True,
        skip_launch_check=True,
        new_run=False,
    )


def test_bench_routes_claude_and_codex_variants_to_their_own_executables(
    store, benchmark, tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "reference-only")
    monkeypatch.delenv("CODEX_AUTH_FILE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(live_codex_home(tmp_path)))
    monkeypatch.setattr(
        bench_cli, "_native_paths", lambda args: (store.root / "rt", store.root / "tui-test")
    )
    result = bench_cli._bench(
        _bench_args(
            benchmark.cases[0],
            "luna=codex:gpt-5.6-luna@high",
            "sonnet=claude-sonnet-4-6@low",
            codex="/opt/codex",
        ),
        store,
    )
    assert {
        name: load_profile(store, profile).recipe.executable
        for name, profile in result["profiles"].items()
    } == {"luna": "/opt/codex", "sonnet": "/opt/claude"}


def test_a_claude_only_bench_needs_no_codex_auth_file(store, benchmark, tmp_path, monkeypatch):
    empty = tmp_path / "no-codex"
    empty.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "reference-only")
    monkeypatch.delenv("CODEX_AUTH_FILE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(empty))
    monkeypatch.setattr(
        bench_cli, "_native_paths", lambda args: (store.root / "rt", store.root / "tui-test")
    )
    result = bench_cli._bench(
        _bench_args(benchmark.cases[0], "sonnet=claude-sonnet-4-6@low"), store
    )
    assert list(result["profiles"]) == ["sonnet"]
    assert load_profile(store, result["profiles"]["sonnet"]).recipe.agent == AgentKind.CLAUDE


def test_a_resumed_bench_run_missing_a_codex_skill_suggests_a_new_run(store, monkeypatch):
    def refuse(*args, **kwargs):
        raise MissingSkillError("Variant terra cannot run workflow skill 'code-review'.")

    monkeypatch.setattr(bench_cli, "run_experiment", refuse)
    paths = (store.root / "rt", store.root / "tui-test")
    with pytest.raises(MissingSkillError) as resumed:
        bench_cli._execute(store, "exp", "run-1", paths, workflow=True, host=HOST)
    assert str(resumed.value) == (
        "Variant terra cannot run workflow skill 'code-review'. If the skill was added to the "
        "live Codex config after run run-1 started, pass --new-run instead of resuming it."
    )
    assert (resumed.value.code, resumed.value.exit_code) == ("missing_skill", 2)
    with pytest.raises(MissingSkillError) as fresh:
        bench_cli._execute(store, "exp", None, paths, workflow=True, host=HOST)
    assert str(fresh.value) == "Variant terra cannot run workflow skill 'code-review'."


def test_a_resumed_bench_run_with_a_changed_snapshot_suggests_a_new_run(store, monkeypatch):
    hosts = []

    def refuse(*args, host, **kwargs):
        hosts.append(host)
        raise SnapshotChangedError("Live config snapshot changed after it was taken: /run")

    monkeypatch.setattr(bench_cli, "run_experiment", refuse)
    paths = (store.root / "rt", store.root / "tui-test")
    with pytest.raises(SnapshotChangedError) as resumed:
        bench_cli._execute(store, "exp", "run-1", paths, workflow=False, host=HOST)
    assert hosts == [HOST]
    assert str(resumed.value) == (
        "Live config snapshot changed after it was taken: /run Run run-1 cannot reuse its live "
        "config snapshot; pass --new-run to take a fresh one."
    )
    assert (resumed.value.code, resumed.value.exit_code) == ("snapshot_changed", 1)
    with pytest.raises(SnapshotChangedError) as fresh:
        bench_cli._execute(store, "exp", None, paths, workflow=False, host=HOST)
    assert str(fresh.value) == "Live config snapshot changed after it was taken: /run"
