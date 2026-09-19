import json
import sys
import threading
import time

import pytest

from dryheave.cli import main
from dryheave.controller_models import ControllerRecipe
from dryheave.controllers import CallContext, invoke_controller
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.experiments import load_experiment
from dryheave.models import ObjectKind
from test_claude_controller import fake_executable
from test_controllers import request_for


def test_claude_setup_freezes_explicit_configuration_without_launch(store, benchmark, capsys):
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "experiment",
                "setup",
                "comparison",
                "--case",
                "task",
                "--profile",
                "base=subject",
                "--simulator",
                "claude",
                "--simulator-model",
                "claude-sonnet-4-6",
                "--simulator-effort",
                "low",
                "--design-approval",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)["data"]
    frozen = load_experiment(store, "comparison")
    recipe = store.load(frozen.simulator_id, ControllerRecipe, kind=ObjectKind.SIMULATOR)
    assert (recipe.kind, recipe.version, recipe.model, recipe.effort) == (
        "claude",
        "2.1.278",
        "claude-sonnet-4-6",
        "low",
    )
    assert recipe.runtime.discovery == "claude-tool-free-2.1.278"
    assert recipe.conversation_policy == "design-approval"
    assert recipe.command.argv == ("claude",)
    assert recipe.command.environment[0].name == "CLAUDE_CODE_OAUTH_TOKEN"
    assert (recipe.budget.max_calls, recipe.budget.call_seconds, recipe.budget.total_seconds) == (
        3,
        30,
        90,
    )
    assert response["id"] == store.resolve("comparison")
    assert "run comparison --assess" in response["next"]


@pytest.mark.parametrize(
    "simulator,flags",
    [
        ("none", ["--simulator-model", "claude-sonnet-4-6"]),
        ("none", ["--simulator-max-calls", "0"]),
        ("claude", []),
        ("claude", ["--simulator-model", "claude-haiku-4-5-20251001", "--simulator-effort", "low"]),
        ("claude", ["--simulator-model", "claude-sonnet-4-6", "--simulator-sandbox", "read-only"]),
        (
            "claude",
            [
                "--simulator-model",
                "claude-sonnet-4-6",
                "--simulator-environment",
                "ANTHROPIC_API_KEY",
            ],
        ),
        ("codex", ["--simulator-call-seconds", "0"]),
        ("codex", ["--simulator-auth-file-env", "OPAQUE"]),
    ],
)
def test_setup_rejects_inappropriate_or_unbounded_options(
    store, benchmark, capsys, simulator, flags
):
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "experiment",
                "setup",
                "invalid",
                "--case",
                "task",
                "--profile",
                "base=subject",
                "--simulator",
                simulator,
                *flags,
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("simulator", ["none", "codex"])
def test_legacy_agent_setup_and_explicit_no_reply_policy(store, benchmark, capsys, simulator):
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "experiment",
                "setup",
                "comparison",
                "--case",
                "task",
                "--profile",
                "base=subject",
                "--simulator",
                simulator,
            ]
        )
        == 0
    )
    recipe = json.loads(capsys.readouterr().out)["data"]["simulator"]
    assert recipe["kind"] == ("scripted" if simulator == "none" else "codex")
    if simulator == "none":
        assert recipe["budget"]["max_calls"] == 0
    else:
        assert recipe["runtime"]["discovery"] == "codex-clean-0.154.0"
        assert recipe["command"]["argv"] == ["codex"]


@pytest.mark.parametrize(
    "agent_selection",
    [
        ("claude", "./tools/claude"),
        ("claude", "tools/claude"),
        ("claude", "claude"),
        ("codex", "./tools/codex"),
        ("codex", "tools/codex"),
        ("codex", "codex"),
    ],
)
def test_setup_resolves_explicit_simulator_paths_before_owned_runtime_launch(
    store, benchmark, tmp_path, capsys, monkeypatch, agent_selection
):
    agent, selection = agent_selection
    tools = tmp_path / "tools"
    tools.mkdir()
    executable = tools / agent
    if agent == "claude":
        fake_executable(tools).rename(executable)
    else:
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,pathlib,sys\n"
            "if sys.argv[-1]=='--version':\n    print('codex-cli 0.154.0')\n    raise SystemExit(0)\n"
            "assert pathlib.Path.cwd().name == 'work'\n"
            "json.load(sys.stdin)\n"
            "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'action':'stop','text':'','fact_ids':[],'reason':'Synthetic fixture'}))\n"
        )
        executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    selected = selection
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "experiment",
                "setup",
                "relative",
                "--case",
                "task",
                "--profile",
                "base=subject",
                "--simulator",
                agent,
                "--simulator-model",
                "claude-haiku-4-5-20251001" if agent == "claude" else "synthetic-model",
                "--simulator-executable",
                selected,
            ]
        )
        == 0
    )
    capsys.readouterr()
    frozen = load_experiment(store, "relative")
    recipe = store.load(frozen.simulator_id, ControllerRecipe, kind=ObjectKind.SIMULATOR)
    assert recipe.command.argv == (str(executable) if "/" in selected else agent,)
    context = CallContext(
        call_id="relative-call",
        index=0,
        deadline=time.monotonic() + 5,
        cancelled=threading.Event(),
        inherited={"PATH": str(tools), "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-offline-value"},
        on_identity=lambda _identity: None,
        runtime_root=tmp_path / "separate-runtime/call",
    )
    writer = ArtifactWriter(tmp_path / "evidence", 1000000)
    call = invoke_controller(recipe, request_for(store), writer, context)
    assert call.status == "completed"
    assert call.cleanup.known_writers_stopped
    version = json.loads(next(writer.root.glob("*-version.json")).read_bytes())
    assert version["stdout"].strip() == (
        "2.1.278 (Claude Code)" if agent == "claude" else "codex-cli 0.154.0"
    )
