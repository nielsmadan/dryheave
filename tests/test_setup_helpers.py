import json
import sys

import pytest

from dryheave.cli import main
from dryheave.controller_models import ControllerRecipe
from dryheave.models import AgentKind
from dryheave.profile_launch import materialize_profile
from dryheave.profile_models import NativeRecipe
from dryheave.profiles import diff_profiles, load_profile
from dryheave.skills import SKILL_NAMES


def test_claude_capture_targets_and_haiku_to_sonnet_derivation(store, tmp_path, capsys):
    config = tmp_path / "selected.json"
    config.write_text('{"alwaysThinkingEnabled":true}')
    instruction = tmp_path / "instructions.md"
    instruction.write_text("Use precise language.")
    skill = tmp_path / "testing"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Run the tests.")
    common = ["--store", str(store.root), "--json"]
    assert (
        main(
            [
                *common,
                "profile",
                "create",
                "haiku",
                "--agent",
                "claude",
                "--executable",
                sys.executable,
                "--model",
                "claude-haiku-4-5-20251001",
                "--config",
                str(config),
                "--instruction",
                str(instruction),
                "--skill",
                str(skill),
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)["data"]
    assert "--effort high" not in response["next"]
    base = load_profile(store, "haiku")
    assert base.recipe.effort is None
    assert base.recipe.disabled_skill_names == SKILL_NAMES
    assert base.recipe.environment[0].name == "CLAUDE_CODE_OAUTH_TOKEN"
    assert [item.target for item in base.assets] == [
        "CLAUDE.md",
        "settings.json",
        "skills/testing/SKILL.md",
    ]
    assert (
        main(
            [
                *common,
                "profile",
                "derive",
                "haiku",
                "--model",
                "claude-sonnet-4-6",
                "--effort",
                "low",
                "--name",
                "sonnet-low",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert diff_profiles(store, "haiku", "sonnet-low")["recipe_changes"] == {
        "model": {"before": "claude-haiku-4-5-20251001", "after": "claude-sonnet-4-6"},
        "effort": {"before": None, "after": "low"},
    }
    assert load_profile(store, "sonnet-low").assets == base.assets
    assert (
        main(
            [
                *common,
                "profile",
                "derive",
                "sonnet-low",
                "--model",
                "claude-haiku-4-5-20251001",
                "--name",
                "invalid-haiku",
            ]
        )
        == 2
    )
    capsys.readouterr()
    assert (
        main(
            [
                *common,
                "profile",
                "derive",
                "sonnet-low",
                "--model",
                "claude-haiku-4-5-20251001",
                "--clear-effort",
                "--name",
                "haiku-again",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert load_profile(store, "haiku-again").recipe == base.recipe
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = materialize_profile(store, "sonnet-low", tmp_path / "profile", workspace)
    assert plan.argv[plan.argv.index("--effort") + 1] == "low"
    assert plan.argv[plan.argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert "--print" not in plan.argv
    assert (tmp_path / "profile/config/CLAUDE.md").read_bytes() == instruction.read_bytes()
    assert (tmp_path / "profile/config/settings.json").read_bytes() == config.read_bytes()


@pytest.mark.parametrize(
    "flags",
    [
        ["--effort", "high"],
        ["--plugins", "selected-plugins"],
        ["--workspace-trust", "prompt"],
        ["--auth-file-env", "OPAQUE", "--acknowledge-auth-source-writes"],
        ["--native-version", "0.154.0"],
        ["--native-arg=--bare"],
        ["--native-arg=--print"],
        ["--native-arg=--dangerously-skip-permissions"],
        ["--native-arg=--disable-slash-commands"],
        ["--environment", "ANTHROPIC_API_KEY"],
    ],
)
def test_friendly_claude_rejects_unsupported_options(store, capsys, flags):
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "profile",
                "create",
                "rejected",
                "--agent",
                "claude",
                "--model",
                "claude-haiku-4-5-20251001",
                *flags,
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"
    assert not store.root.exists()


def test_codex_capture_and_effort_only_variant_preserve_selected_config(store, tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-terra"\n')
    common = ["--store", str(store.root), "--json"]
    assert main([*common, "profile", "create", "terra-low", "--config", str(config)]) == 0
    capsys.readouterr()
    assert (
        main(
            [*common, "profile", "derive", "terra-low", "--effort", "high", "--name", "terra-high"]
        )
        == 0
    )
    capsys.readouterr()
    assert diff_profiles(store, "terra-low", "terra-high")["recipe_changes"] == {
        "effort": {"before": "low", "after": "high"}
    }
    base = load_profile(store, "terra-low")
    assert base.recipe.codex_discovery == "disabled-plugins"
    assert base.recipe.claude_discovery is None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = materialize_profile(store, "terra-low", tmp_path / "profile", workspace)
    assert "--ignore-user-config" not in plan.argv
    assert plan.argv[plan.argv.index("--disable") + 1] == "plugins"
    assert "skills.bundled.enabled=false" in plan.argv
    assert (tmp_path / "profile/config/config.toml").read_bytes() == config.read_bytes()


def test_legacy_serialization_does_not_add_implicit_policy_fields():
    recipe = NativeRecipe(agent=AgentKind.CODEX, executable="codex", version="0.153.4")
    expected = {
        "agent": "codex",
        "executable": "codex",
        "version": "0.153.4",
        "launcher": [],
        "arguments": [],
        "model": None,
        "effort": None,
        "workflow": None,
        "home_policy": "native",
        "workspace_trust": "prompt",
        "disabled_skill_paths": [],
        "disabled_skill_names": ["dryheave-collect", "dryheave-case", "dryheave-results"],
        "environment": [],
        "runtime_files": [],
    }
    assert recipe.model_dump(mode="json") == expected
    controller = ControllerRecipe()
    assert set(controller.model_dump()) == {
        "schema_version",
        "kind",
        "budget",
        "script",
        "command",
        "version",
        "model",
        "effort",
        "isolation",
    }


def test_derive_options_require_a_change_and_reject_spec_mixing(store, benchmark, tmp_path, capsys):
    spec = tmp_path / "derive.json"
    spec.write_text('{"recipe_changes":{"model":"new"}}')
    common = ["--store", str(store.root), "--json", "profile", "derive", "subject"]
    for flags in (
        [],
        ["--spec", str(spec), "--model", "other"],
        ["--effort", "low", "--clear-effort"],
    ):
        assert main([*common, *flags]) == 2
        capsys.readouterr()
