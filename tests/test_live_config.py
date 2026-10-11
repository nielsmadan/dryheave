import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from dryheave.errors import InputError
from dryheave.experiment_models import VariantSpec
from dryheave.experiments import create_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.live_config import (
    LIVE_CONFIG_DIRECTORY,
    live_source,
    materialize_live_config,
    shareable_state,
    snapshot_live_config,
)
from dryheave.models import AgentKind, EnvironmentReference
from dryheave.profile_launch import LaunchOptions, launch_environment, materialize_profile
from dryheave.profile_models import CaptureSpec, NativeRecipe
from dryheave.profiles import capture_profile
from dryheave.runner import _prepare_live_config
from dryheave.runner_state import ExecutionJournal


def live_home(root: Path) -> tuple[Path, Path]:
    config = root / "live"
    (config / "skills/review").mkdir(parents=True)
    (config / "skills/review/SKILL.md").write_text("Review carefully.")
    (config / "skills/linked").symlink_to(root / "elsewhere")
    cache = config / "plugins/cache/market/tool/1.0"
    cache.mkdir(parents=True)
    (cache / "plugin.json").write_text("{}")
    (config / "plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"tool@market": [{"installPath": str(cache), "scope": "user"}]}})
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"tool@market": True, "off@market": False},
                "hooks": {"Stop": [], "SessionStart": []},
            }
        )
    )
    (config / "CLAUDE.md").write_text("Be terse.")
    (config / "projects/history").mkdir(parents=True)
    state = config / ".claude.json"
    state.write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "oauthAccount": {"emailAddress": "someone"},
                "userID": "abc",
                "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}},
                "mcpServers": {"jina": {"type": "http"}, "context7": {"command": "npx"}},
            }
        )
    )
    return config, state


def live_recipe(**changes: object) -> NativeRecipe:
    values: dict[str, object] = {
        "agent": AgentKind.CLAUDE,
        "executable": "claude",
        "model": "claude-sonnet-4-6",
        "effort": "low",
        "config_source": "live",
        "workspace_trust": "trusted",
        "environment": (EnvironmentReference(name="CLAUDE_CODE_OAUTH_TOKEN"),),
    }
    return NativeRecipe.model_validate(values | changes)


def test_live_source_follows_the_configured_directory(tmp_path):
    assert live_source({"CLAUDE_CONFIG_DIR": str(tmp_path / "c")}, tmp_path / "home") == (
        tmp_path / "c",
        tmp_path / "c/.claude.json",
    )
    assert live_source({}, tmp_path / "home") == (
        tmp_path / "home/.claude",
        tmp_path / "home/.claude.json",
    )


def test_shareable_state_keeps_settings_and_drops_identity_and_history():
    state = {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "oauthAccount": {},
        "userID": "x",
        "machineID": "y",
        "primaryApiKey": "z",
        "projects": {},
    }
    assert shareable_state(state) == {"hasCompletedOnboarding": True, "theme": "dark"}


def test_snapshot_records_what_the_live_setup_contains(tmp_path):
    config, state = live_home(tmp_path)
    manifest = snapshot_live_config(config, state, tmp_path / "snapshot")
    assert manifest.entries == ("settings.json", "CLAUDE.md", "skills", "plugins")
    assert manifest.skills == ("linked", "review")
    assert manifest.enabled_plugins == ("tool@market",)
    assert manifest.mcp_servers == ("context7", "jina")
    assert manifest.hook_events == ("SessionStart", "Stop")
    assert manifest.files == 5
    assert os.readlink(tmp_path / "snapshot/skills/linked") == str(tmp_path / "elsewhere")
    assert json.loads((tmp_path / "snapshot/state.json").read_text()) == {
        "hasCompletedOnboarding": True,
        "mcpServers": {"context7": {"command": "npx"}, "jina": {"type": "http"}},
    }
    assert sorted(path.name for path in (tmp_path / "snapshot").iterdir()) == [
        "CLAUDE.md",
        "manifest.json",
        "plugins",
        "settings.json",
        "skills",
        "state.json",
    ]


def test_snapshot_digest_changes_with_config_bytes(tmp_path):
    config, state = live_home(tmp_path)
    first = snapshot_live_config(config, state, tmp_path / "first")
    (config / "CLAUDE.md").write_text("Be verbose.")
    second = snapshot_live_config(config, state, tmp_path / "second")
    assert first.tree_sha256 != second.tree_sha256
    assert first.instructions_sha256 != second.instructions_sha256
    assert first.settings_sha256 == second.settings_sha256


def test_materialized_copy_rebases_plugins_trusts_workspace_and_removes_servers(tmp_path):
    config, state = live_home(tmp_path)
    snapshot_live_config(config, state, tmp_path / "snapshot")
    target, workspace = tmp_path / "attempt", tmp_path / "workspace"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot", target, workspace, removed_mcp_servers=("jina",), trusted=True
    )
    installed = json.loads((target / "plugins/installed_plugins.json").read_text())
    assert installed["plugins"]["tool@market"][0]["installPath"] == str(
        target / "plugins/cache/market/tool/1.0"
    )
    subject = json.loads((target / ".claude.json").read_text())
    assert subject["mcpServers"] == {"context7": {"command": "npx"}}
    assert subject["projects"] == {str(workspace): {"hasTrustDialogAccepted": True}}
    assert subject["hasCompletedOnboarding"] is True
    assert (target / "CLAUDE.md").read_text() == "Be terse."


def test_removing_an_unknown_server_is_refused(tmp_path):
    config, state = live_home(tmp_path)
    snapshot_live_config(config, state, tmp_path / "snapshot")
    (tmp_path / "attempt").mkdir()
    with pytest.raises(InputError, match="not user-scoped"):
        materialize_live_config(
            tmp_path / "snapshot",
            tmp_path / "attempt",
            tmp_path / "workspace",
            removed_mcp_servers=("github",),
            trusted=True,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"agent": AgentKind.CODEX},
        {"config_source": "selected", "removed_mcp_servers": ("jina",)},
        {"removed_mcp_servers": ("jina", "jina")},
        {"model": "claude-haiku-4-5", "effort": "low"},
    ],
)
def test_live_recipe_contract(changes):
    with pytest.raises(ValidationError):
        live_recipe(**changes)


def test_selected_recipes_serialize_without_live_fields():
    selected = NativeRecipe(agent=AgentKind.CLAUDE, executable="claude")
    assert "config_source" not in selected.model_dump(mode="json")
    assert "removed_mcp_servers" not in selected.model_dump(mode="json")
    assert live_recipe(removed_mcp_servers=("jina",)).model_dump(mode="json")[
        "removed_mcp_servers"
    ] == ["jina"]


def test_live_profile_launches_from_snapshot_with_inherited_environment(store, tmp_path):
    config, state = live_home(tmp_path)
    snapshot_live_config(config, state, tmp_path / "snapshot")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = capture_profile(store, CaptureSpec(recipe=live_recipe()))
    with pytest.raises(InputError, match="run's config snapshot"):
        materialize_profile(store, profile, tmp_path / "missing", workspace)
    plan = materialize_profile(
        store,
        profile,
        tmp_path / "runtime",
        workspace,
        LaunchOptions(live_config=tmp_path / "snapshot"),
    )
    assert plan.environment_policy == "inherited"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in plan.generated_environment
    assert "live-config" in {item.code for item in plan.issues}
    assert (tmp_path / "runtime/config/skills/review/SKILL.md").read_text() == "Review carefully."
    environment = launch_environment(
        plan,
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "t",
            "JINA_API_KEY": "j",
            "CLAUDE_CONFIG_DIR": str(config),
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
        },
    )
    assert environment["JINA_API_KEY"] == "j"
    assert environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "runtime/config")
    assert {"CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"}.isdisjoint(environment)


def test_run_snapshots_live_config_once_and_journals_it(store, benchmark, tmp_path):
    config, _ = live_home(tmp_path)
    profile = capture_profile(store, CaptureSpec(recipe=live_recipe()))
    draft = benchmark.model_copy(
        update={"variants": (VariantSpec(name="live", profile=profile),), "repetitions": 1}
    )
    experiment = load_experiment(store, create_experiment(store, draft))
    run_id = RunStore(store.root).create(store.resolve(profile))
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        environment = {"CLAUDE_CONFIG_DIR": str(config)}
        _prepare_live_config(store, execution, experiment, environment)
        (config / "CLAUDE.md").write_text("Changed mid-run.")
        _prepare_live_config(store, execution, experiment, environment)
        events = [event for event in journal.events if event.event == "live-config"]
        assert len(events) == 1
        assert events[0].data["mcp_servers"] == ["context7", "jina"]
        assert (journal.path / LIVE_CONFIG_DIRECTORY / "CLAUDE.md").read_text() == "Be terse."


def test_run_refuses_to_remove_a_server_missing_from_the_snapshot(store, benchmark, tmp_path):
    config, _ = live_home(tmp_path)
    profile = capture_profile(
        store, CaptureSpec(recipe=live_recipe(removed_mcp_servers=("github",)))
    )
    draft = benchmark.model_copy(update={"variants": (VariantSpec(name="live", profile=profile),)})
    experiment = load_experiment(store, create_experiment(store, draft))
    run_id = RunStore(store.root).create(store.resolve(profile))
    with (
        RunStore(store.root).open(run_id) as journal,
        pytest.raises(InputError, match="github"),
    ):
        _prepare_live_config(
            store, ExecutionJournal(journal), experiment, {"CLAUDE_CONFIG_DIR": str(config)}
        )
