import hashlib
import json
import os
import shutil
import stat
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import live_codex_home
from dryheave import live_config
from dryheave.errors import InputError, SnapshotChangedError
from dryheave.experiment_models import VariantSpec
from dryheave.experiments import create_experiment, load_experiment
from dryheave.host import HostContext
from dryheave.journals import RunStore
from dryheave.live_config import (
    LIVE_CONFIG_DIRECTORY,
    LiveSource,
    live_source,
    materialize_live_config,
    shareable_state,
    snapshot_directory,
    snapshot_live_config,
)
from dryheave.models import AgentKind, EnvironmentReference
from dryheave.profile_launch import (
    LaunchOptions,
    LiveSnapshots,
    launch_environment,
    materialize_profile,
)
from dryheave.profile_models import CaptureSpec, NativeRecipe, RuntimeFileReference
from dryheave.profile_security import inspect_config
from dryheave.profiles import capture_profile
from dryheave.runner import _prepare_live_config
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json


def live_home(root: Path) -> LiveSource:
    config = root / "live"
    (config / "skills/review").mkdir(parents=True)
    (config / "skills/review/SKILL.md").write_text("Review carefully.")
    (config / "skills/linked").symlink_to(root / "elsewhere")
    (config / "skills/.system").mkdir()
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
    return LiveSource(AgentKind.CLAUDE, config, state)


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


def codex_live_recipe() -> NativeRecipe:
    return NativeRecipe(
        agent=AgentKind.CODEX,
        executable="codex",
        model="gpt-5.6-terra",
        effort="high",
        config_source="live",
        workspace_trust="trusted",
        runtime_files=(
            RuntimeFileReference(
                name="codex-auth",
                source_path_environment=EnvironmentReference(name="CODEX_AUTH_FILE"),
                acknowledge_source_writes=True,
            ),
        ),
    )


def test_live_source_follows_the_configured_directory(tmp_path):
    assert live_source(
        AgentKind.CLAUDE, {"CLAUDE_CONFIG_DIR": str(tmp_path / "c")}, tmp_path / "home"
    ) == (AgentKind.CLAUDE, tmp_path / "c", tmp_path / "c/.claude.json")
    assert live_source(AgentKind.CLAUDE, {}, tmp_path / "home") == (
        AgentKind.CLAUDE,
        tmp_path / "home/.claude",
        tmp_path / "home/.claude.json",
    )


def test_codex_live_source_follows_codex_home_and_has_no_state(tmp_path):
    assert live_source(
        AgentKind.CODEX, {"CODEX_HOME": str(tmp_path / "c"), "CLAUDE_CONFIG_DIR": "/x"}, tmp_path
    ) == (AgentKind.CODEX, tmp_path / "c", None)
    assert live_source(AgentKind.CODEX, {}, tmp_path / "home") == (
        AgentKind.CODEX,
        tmp_path / "home/.codex",
        None,
    )
    assert (snapshot_directory(AgentKind.CLAUDE), snapshot_directory(AgentKind.CODEX)) == (
        "live-config",
        "live-config-codex",
    )


def test_live_source_expands_a_configured_tilde_against_the_given_home(tmp_path):
    home = tmp_path / "home"
    assert live_source(AgentKind.CODEX, {"CODEX_HOME": "~/codex-alt"}, home).root == (
        home / "codex-alt"
    )
    assert live_source(AgentKind.CLAUDE, {"CLAUDE_CONFIG_DIR": "~"}, home) == (
        AgentKind.CLAUDE,
        home,
        home / ".claude.json",
    )


def test_live_layouts_refuse_an_agent_without_live_snapshots(tmp_path):
    with pytest.raises(InputError, match="not supported for droid"):
        snapshot_directory(AgentKind.DROID)
    with pytest.raises(InputError, match="not supported for droid"):
        live_source(AgentKind.DROID, {}, tmp_path)


def files_under(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_codex_snapshot_copies_only_config_entries_and_records_them(tmp_path):
    home = live_codex_home(tmp_path)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    manifest = snapshot_live_config(source, tmp_path / "snapshot")
    assert files_under(tmp_path / "snapshot") == {
        "config.toml",
        "AGENTS.md",
        "skills/review/SKILL.md",
        "rules/default.rules",
        "prompts/plan.md",
        "hooks/stop.sh",
        "hooks.json",
        "manifest.json",
    }
    assert manifest.agent == AgentKind.CODEX
    assert manifest.entries == (
        "config.toml",
        "AGENTS.md",
        "skills",
        "rules",
        "prompts",
        "hooks",
        "hooks.json",
    )
    assert manifest.skills == ("review",)
    assert manifest.enabled_plugins == ("nono@nolabs-ai",)
    assert manifest.mcp_servers == ("context7", "jina")
    assert manifest.hook_events == ("SessionStart", "Stop")
    assert manifest.files == 7
    assert (
        manifest.settings_sha256 == hashlib.sha256((home / "config.toml").read_bytes()).hexdigest()
    )
    assert (
        manifest.instructions_sha256
        == hashlib.sha256((home / "AGENTS.md").read_bytes()).hexdigest()
    )
    assert json.loads((tmp_path / "snapshot/manifest.json").read_text())["agent"] == "codex"


@pytest.mark.parametrize(("hooks", "events"), [('{"PreToolUse": []}', ("PreToolUse",)), ("[]", ())])
def test_codex_snapshot_reads_top_level_hook_events(tmp_path, hooks, events):
    home = live_codex_home(tmp_path)
    (home / "hooks.json").write_text(hooks)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    assert snapshot_live_config(source, tmp_path / "snapshot").hook_events == events


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("hooks.json", "{", "hooks file is not valid JSON"),
        ("config.toml", "model = ", "not valid TOML"),
    ],
)
def test_codex_snapshot_refuses_an_unparsable_structured_file(tmp_path, name, content, message):
    home = live_codex_home(tmp_path)
    (home / name).write_text(content)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match=message):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex"]


def test_codex_snapshot_needs_the_codex_home(tmp_path):
    source = live_source(AgentKind.CODEX, {}, tmp_path)
    with pytest.raises(InputError, match="Live Codex config directory does not exist"):
        snapshot_live_config(source, tmp_path / "snapshot")


@pytest.mark.parametrize("entry", ["skills", "AGENTS.md", "config.toml"])
def test_codex_snapshot_refuses_symlinked_top_level_entries(tmp_path, entry):
    home = live_codex_home(tmp_path)
    target = home / entry
    elsewhere = tmp_path / "elsewhere"
    if target.is_dir():
        shutil.rmtree(target)
        elsewhere.mkdir()
    else:
        target.unlink()
        elsewhere.write_text("replaced")
    target.symlink_to(elsewhere)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match="symlinked top-level entries") as info:
        snapshot_live_config(source, tmp_path / "snapshot")
    assert entry in str(info.value)


def test_codex_snapshot_skills_require_a_skill_md_file(tmp_path):
    home = live_codex_home(tmp_path)
    (home / "skills/.system/skill-creator").mkdir(parents=True)
    (home / "skills/.system/skill-creator/SKILL.md").write_text("Bundled.")
    (home / "skills/incomplete").mkdir(parents=True)
    (home / "skills/README.md").write_text("Not a skill.")
    (home / "skills/dangling").symlink_to(home / "skills/does-not-exist")
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    manifest = snapshot_live_config(source, tmp_path / "snapshot")
    assert manifest.skills == ("review", "skill-creator")


def test_codex_skills_name_bundled_skills_by_their_own_directory(tmp_path):
    home = live_codex_home(tmp_path)
    (home / "skills/.system/imagegen").mkdir(parents=True)
    (home / "skills/.system/imagegen/SKILL.md").write_text("Bundled.")
    (home / "skills/.system/SKILL.md").write_text("Container notes.")
    assert live_config.codex_skills(home) == ("imagegen", "review")


def test_codex_skills_walk_grouped_skills_and_prefer_their_frontmatter_name(tmp_path):
    home = live_codex_home(tmp_path)
    skills = {
        "group/lint/SKILL.md": "Lint.",
        "renamed/SKILL.md": '---\nname: "polish"\ndescription: Polish.\n---\nPolish.',
        "plain/SKILL.md": "---\ndescription: No name.\n---\nPlain.",
        "a/b/c/d/e/deep/SKILL.md": "Deep.",
        "a/b/c/d/e/deep/deeper/SKILL.md": "Too deep.",
        ".hidden/SKILL.md": "Hidden.",
        "group/.drafts/wip/SKILL.md": "Hidden draft.",
    }
    for name, content in skills.items():
        (home / "skills" / name).parent.mkdir(parents=True, exist_ok=True)
        (home / "skills" / name).write_text(content)
    assert live_config.codex_skills(home) == ("deep", "lint", "plain", "polish", "review")


def test_codex_snapshot_copies_nested_links_as_their_content(tmp_path):
    home = live_codex_home(tmp_path)
    shared = tmp_path / "shared/code-review"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("Review the change.")
    (home / "skills/code-review").symlink_to(Path("../../shared/code-review"))
    (home / "prompts/linked.md").symlink_to(home / "AGENTS.md")
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    manifest = snapshot_live_config(source, tmp_path / "snapshot")
    copied = tmp_path / "snapshot/skills/code-review"
    assert (copied.is_symlink(), (copied / "SKILL.md").read_text()) == (
        False,
        "Review the change.",
    )
    assert (tmp_path / "snapshot/prompts/linked.md").read_text() == "Be brief."
    assert manifest.skills == ("code-review", "review")
    assert manifest.files == 9
    target = tmp_path / "attempt"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot", target, tmp_path / "workspace", codex_live_recipe(), manifest
    )
    assert (target / "skills/code-review/SKILL.md").read_text() == "Review the change."


@pytest.mark.parametrize(
    ("link", "target", "refused"),
    [
        ("skills/helper/notes.txt", ".aws/credentials", "skills/helper/notes.txt"),
        ("skills/x/aws", ".aws", "skills/x/aws"),
        ("prompts/cluster.md", ".kube/config", "prompts/cluster.md"),
        ("skills/x/gcloud", ".config/gcloud", "skills/x/gcloud"),
        ("hooks/token.txt", "secrets/.git-credentials", "hooks/token.txt"),
        ("skills/x/state.md", "secrets/state.db", "skills/x/state.md"),
        ("skills/x/home", ".", "skills/x/home/.aws"),
    ],
)
def test_codex_snapshot_refuses_links_that_resolve_to_credentials(tmp_path, link, target, refused):
    home = live_codex_home(tmp_path)
    user = tmp_path / "user"
    for name in (".aws/credentials", ".kube/config", ".config/gcloud/adc.json"):
        (user / name).parent.mkdir(parents=True, exist_ok=True)
        (user / name).write_text("plain")
    (user / "secrets").mkdir()
    (user / "secrets/.git-credentials").write_text("plain")
    (user / "secrets/state.db").write_text("plain")
    (home / link).parent.mkdir(parents=True, exist_ok=True)
    (home / link).symlink_to(user / target)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match=f"{refused} resolves to a credential or secret path"):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex", "user"]


@pytest.mark.parametrize(
    ("link", "target", "message"),
    [
        ("skills/auth", "../auth.json", "points outside the snapshot entries"),
        ("skills/loop", ".", "forms a cycle"),
        ("skills/up", "../..", "points outside the snapshot entries"),
    ],
)
def test_codex_snapshot_refuses_nested_links_to_private_files_or_cycles(
    tmp_path, link, target, message
):
    home = live_codex_home(tmp_path)
    (home / link).symlink_to(Path(target))
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match=message):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex"]


@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        (
            "config.toml",
            '[mcp_servers.github.env]\nGITHUB_API_KEY = "synthetic"\n',
            (
                "credential field mcp_servers.github.env.GITHUB_API_KEY in live Codex config file "
                "config.toml"
            ),
        ),
        (
            "config.toml",
            '[mcp_servers.gh]\nurl = "https://example.invalid"\nbearer_token = "synthetic"\n',
            "credential field mcp_servers.gh.bearer_token in live Codex config file config.toml",
        ),
        (
            "config.toml",
            (
                '[mcp_servers.gh]\nurl = "https://example.invalid"\n'
                'http_headers = { "Authorization" = "synthetic" }\n'
            ),
            "credential field mcp_servers.gh.http_headers.Authorization in live Codex config",
        ),
        (
            "hooks.json",
            '{"hooks": {"Stop": [{"command": "x", "env": {"GITHUB_AUTH_TOKEN": "synthetic"}}]}}',
            "credential field hooks.Stop.env.GITHUB_AUTH_TOKEN in live Codex config file hooks.json",
        ),
        (
            "config.toml",
            '[model_providers.proxy]\nname = "Proxy"\napi_key = 1234567890\n',
            "credential field model_providers.proxy.api_key in live Codex config file config.toml",
        ),
        (
            "config.toml",
            (
                '[mcp_servers.gh]\ncommand = "gh-mcp"\n'
                'args = ["--header", "Authorization: Bearer abc123defghijkl"]\n'
            ),
            "credential material on line 3 in live Codex config file config.toml",
        ),
        (
            "config.toml",
            '[mcp_servers.x]\nurl = "https://x.invalid/mcp?key=sk-proj-abcdefghijkl"\n',
            "credential material on line 2 in live Codex config file config.toml",
        ),
        (
            "config.toml",
            '[mcp_servers.exa]\nurl = "https://mcp.exa.ai/mcp?exaApiKey=0a1b2c3d4e5f60718293"\n',
            "credential field mcp_servers.exa.url\\?exaApiKey in live Codex config file config.toml",
        ),
        (
            "hooks.json",
            '{"hooks": {"Stop": [{"command": "curl -H \'x: ghp_synthetic0123456789abcdef\'"}]}}',
            "credential material on line 1 in live Codex config file hooks.json",
        ),
        (
            "rules/default.rules",
            'prefix_rule()\nprefix_rule(pattern=["curl", "-H", "Bearer abc123defghijkl"])\n',
            "credential material on line 2 in live Codex config file rules/default.rules",
        ),
        (
            "hooks/stop.sh",
            "#!/bin/sh\nexport GH=ghp_synthetic0123456789abcdef\n",
            "credential material on line 2 in live Codex config file hooks/stop.sh",
        ),
        (
            "skills/review/SKILL.md",
            "# Review\n\nUse ghp_synthetic0123456789abcdef0123456789ab for the API.\n",
            "credential material on line 3 in live Codex config file skills/review/SKILL.md",
        ),
        (
            "AGENTS.md",
            "Be brief.\nKEY=sk-proj-synthetic0123456789abcdefghijklmnop\n",
            "credential material on line 2 in live Codex config file AGENTS.md",
        ),
        (
            "prompts/plan.md",
            "export OPENAI_API_KEY=sk-synthetic0123456789abcdefghijklmnop\n",
            "credential material on line 1 in live Codex config file prompts/plan.md",
        ),
        (
            "skills/review/.env",
            "FOO_API_KEY=abcd1234efgh\n",
            "skills/review/.env is a credential file",
        ),
        (
            "hooks/stop.sh",
            "#!/bin/sh\nexport API_KEY=synthetic123\n",
            "credential field API_KEY on line 2 in live Codex config file hooks/stop.sh",
        ),
        (
            "config.toml",
            '[model_providers.proxy]\nname = "Proxy"\nexperimental_bearer_token = "synthetic"\n',
            (
                "credential field model_providers.proxy.experimental_bearer_token in live Codex "
                "config file config.toml"
            ),
        ),
        (
            "config.toml",
            '[mcp_servers.x.http_headers]\nX-Api-Token = "synthetic"\n',
            "credential field mcp_servers.x.http_headers.X-Api-Token in live Codex config file",
        ),
        (
            "config.toml",
            '# sk-proj-synthetic0123\n[projects."/Users/x/sk-learn"]\ntrust_level = "trusted"\n',
            "credential material on line 1 in live Codex config file config.toml",
        ),
    ],
)
def test_codex_snapshot_refuses_inline_credentials(tmp_path, path, content, message):
    home = live_codex_home(tmp_path)
    (home / path).write_text(content)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match=message) as raised:
        snapshot_live_config(source, tmp_path / "snapshot")
    assert "synthetic" not in str(raised.value)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex"]


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (
            "config.toml",
            (
                '[mcp_servers.gh]\nurl = "https://example.invalid"\n'
                'env_http_headers = { "Authorization" = "GH_AUTH_HEADER" }\n'
            ),
        ),
        (
            "config.toml",
            (
                '[mcp_servers.gh]\nurl = "https://example.invalid"\n'
                'bearer_token_env_var = "GH_TOKEN"\n'
            ),
        ),
        (
            "config.toml",
            (
                '[mcp_servers.gh]\ncommand = "gh-mcp"\nenv_vars = ["GITHUB_TOKEN"]\n'
                '[model_providers.proxy]\nname = "Proxy"\nenv_key = "PROXY_API_KEY"\n'
            ),
        ),
        (
            "hooks.json",
            '{"hooks": {"Stop": [{"command": "curl -H \'Authorization: Bearer $TOKEN\' x"}]}}',
        ),
        (
            "hooks/stop.sh",
            '#!/bin/sh\ntoken=$X\ntoken=$(gh auth token)\nif [ -n "$token" ]; then exit 0; fi\n',
        ),
        ("rules/default.rules", 'prefix_rule(pattern=["deploy", "--password=$X"])\n'),
        ("config.toml", '[shell_environment_policy.set]\nTOKENIZERS_PARALLELISM = "false"\n'),
        (
            "config.toml",
            '[mcp_servers.llm]\ncommand = "llm"\n[mcp_servers.llm.env]\nMAX_TOKENS = "4096"\n',
        ),
        (
            "config.toml",
            '[projects."/Users/x/wrksp/desk-booking-service"]\ntrust_level = "trusted"\n',
        ),
        (
            "config.toml",
            '[mcp_servers.gh]\nurl = "https://example.invalid"\nbearer_token = ""\n',
        ),
        (
            "rules/default.rules",
            'prefix_rule(pattern=["npm", "run", "task-runner-build"], decision="allow")\n',
        ),
        (
            "skills/review/SKILL.md",
            "Send requests with Bearer authentication and `token=$GITHUB_TOKEN`.\n",
        ),
        ("skills/review/SKILL.md", "async function login(email: string, password: string)\n"),
        ("skills/review/SKILL.md", "Credentials are injected as `Authorization: Bearer $TOKEN`.\n"),
        ("skills/review/SKILL.md", "qpdf --password=mypassword --decrypt in.pdf out.pdf\n"),
        ("skills/review/SKILL.md", "**Pre-release audit finds hardcoded secret:** rotate it.\n"),
        ("skills/review/SKILL.md", "const apiKey = 'sk-1234567890abcdef';\n"),
        (
            "config.toml",
            '[projects."/Users/x/sk-learn-experiments-2024"]\ntrust_level = "trusted"\n',
        ),
    ],
)
def test_codex_snapshot_accepts_credential_references(tmp_path, path, content):
    home = live_codex_home(tmp_path)
    (home / path).write_text(content)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    snapshot_live_config(source, tmp_path / "snapshot")
    assert (tmp_path / "snapshot" / path).read_text() == content


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("skills/x/.env", "skills/x/.env is a credential file"),
        ("skills/x/keys/server.pem", "skills/x/keys/server.pem is a credential file"),
        ("skills/x/id_rsa.pub", "skills/x/id_rsa.pub is a credential file"),
        ("skills/x/credentials-prod.json", "skills/x/credentials-prod.json is a credential file"),
        ("prompts/auth.json", "prompts/auth.json is a credential file"),
        ("skills/x/credentials", "skills/x/credentials is a credential file"),
        ("skills/x/.npmrc", "skills/x/.npmrc is a credential file"),
        (
            "skills/x/credentials/README.md",
            "directory skills/x/credentials is named like a credential file",
        ),
        ("rules/history.rules", "rules/history.rules is a credential, history, cache or trust"),
    ],
)
def test_codex_snapshot_refuses_credential_file_names(tmp_path, path, message):
    home = live_codex_home(tmp_path)
    (home / path).parent.mkdir(parents=True, exist_ok=True)
    (home / path).write_text("plain")
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match=message):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex"]


def test_codex_snapshot_excludes_git_directories_in_every_entry(tmp_path):
    home = live_codex_home(tmp_path)
    kept = {
        "skills/x/SKILL.md": "Use the history.",
        "skills/x/history.md": "Earlier notes.",
        "skills/x/state_machine.md": "States.",
        "skills/x/.env.example": "API_KEY=",
    }
    for name, content in kept.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(content)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    plain = snapshot_live_config(source, tmp_path / "plain")
    (home / "skills/x/.git/objects").mkdir(parents=True)
    (home / "skills/x/.git/config").write_text(
        "token = ghp_synthetic0123456789abcdef0123456789ab\n"
    )
    for name in ("prompts/.git", "skills/review/.Git", "rules/.git", "hooks/.git"):
        (home / name).mkdir(parents=True)
        (home / name / "config").write_text("token = ghp_synthetic0123456789abcdef0123456789ab\n")
    cloned = snapshot_live_config(source, tmp_path / "cloned")
    copied = {name: (tmp_path / "cloned" / name).read_text() for name in kept}
    live_config.verify_snapshot(tmp_path / "cloned", cloned)
    assert (copied, cloned.skills, cloned.tree_sha256, cloned.files) == (
        kept,
        ("review", "x"),
        plain.tree_sha256,
        plain.files,
    )
    assert sorted(path.name for path in (tmp_path / "cloned/skills/x").iterdir()) == [
        ".env.example",
        "SKILL.md",
        "history.md",
        "state_machine.md",
    ]


def test_codex_snapshot_skips_dependency_and_cache_directories_in_skills_and_prompts(tmp_path):
    home = live_codex_home(tmp_path)
    files = {
        "skills/x/SKILL.md": "Use the bundle.",
        "skills/x/dist/bundle.js": "bundle",
        "skills/x/build/notes.md": "notes",
        "skills/x/node_modules/pkg/index.js": "module",
        "skills/x/.venv/bin/python": "python",
        "skills/x/venv/pyvenv.cfg": "venv",
        "skills/x/scripts/__pycache__/run.pyc": "cache",
        "skills/x/.pytest_cache/README.md": "cache",
        "prompts/node_modules/pkg/index.js": "module",
        "prompts/.mypy_cache/x.json": "cache",
        "prompts/.ruff_cache/x": "cache",
        "prompts/.tox/log.txt": "cache",
    }
    for name, content in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(content)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    snapshot_live_config(source, tmp_path / "snapshot")
    assert {
        name
        for name in files_under(tmp_path / "snapshot")
        if name.startswith(("skills/x/", "prompts/"))
    } == {
        "skills/x/SKILL.md",
        "skills/x/dist/bundle.js",
        "skills/x/build/notes.md",
        "prompts/plan.md",
    }


def test_snapshot_bounds_are_checked_before_any_copy(tmp_path, monkeypatch):
    home = live_codex_home(tmp_path)
    staged = []
    monkeypatch.setattr(live_config, "MAX_BYTES", 64)
    monkeypatch.setattr(live_config, "ensure_directory", staged.append)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match="exceeds the snapshot bounds"):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert (staged, sorted(path.name for path in tmp_path.iterdir())) == ([], ["codex"])


def test_snapshot_bounds_count_directories(tmp_path, monkeypatch):
    home = live_codex_home(tmp_path)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    monkeypatch.setattr(live_config, "MAX_DIRECTORIES", 5)
    assert snapshot_live_config(source, tmp_path / "first").skills == ("review",)
    for index in range(3):
        (home / f"skills/empty-{index}").mkdir()
    with pytest.raises(InputError, match="exceeds the snapshot bounds"):
        snapshot_live_config(source, tmp_path / "second")


@pytest.mark.parametrize("spelling", ["case", "firmlink"])
def test_codex_snapshot_refuses_a_private_file_reached_through_another_spelling(tmp_path, spelling):
    home = live_codex_home(tmp_path)
    alias = (
        home.parent / home.name.upper() / "auth.json"
        if spelling == "case"
        else Path("/System/Volumes/Data" + str(home / "auth.json"))
    )
    if not alias.exists() or not alias.samefile(home / "auth.json"):
        pytest.skip(f"this file system has no {spelling} alias")
    (home / "skills/review/leak").symlink_to(alias)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match="skills/review/leak points outside the snapshot entries"):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex"]


def test_codex_snapshot_checks_each_link_again_while_it_copies(tmp_path, monkeypatch):
    home = live_codex_home(tmp_path)
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared/notes.md").write_text("Shared.")
    link = home / "prompts/shared.md"
    link.symlink_to(tmp_path / "shared/notes.md")
    checked = live_config._require_copyable

    def retarget(*args):
        checked(*args)
        link.unlink()
        link.symlink_to(home / "auth.json")

    monkeypatch.setattr(live_config, "_require_copyable", retarget)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match=r"prompts/shared\.md points outside the snapshot entries"):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex", "shared"]


def test_codex_snapshot_hashes_the_bytes_it_copied(tmp_path, monkeypatch):
    home = live_codex_home(tmp_path)
    checked = live_config._require_copyable

    def rewrite(*args):
        checked(*args)
        (home / "AGENTS.md").write_text("Changed after the check.")

    monkeypatch.setattr(live_config, "_require_copyable", rewrite)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    manifest = snapshot_live_config(source, tmp_path / "snapshot")
    copied = (tmp_path / "snapshot/AGENTS.md").read_bytes()
    assert (copied, manifest.instructions_sha256) == (
        b"Changed after the check.",
        hashlib.sha256(copied).hexdigest(),
    )


def test_codex_snapshot_bounds_hold_while_it_copies(tmp_path, monkeypatch):
    home = live_codex_home(tmp_path)
    checked = live_config._require_copyable

    def grow(*args):
        checked(*args)
        monkeypatch.setattr(live_config, "MAX_BYTES", 8)

    monkeypatch.setattr(live_config, "_require_copyable", grow)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    with pytest.raises(InputError, match="exceeds the snapshot bounds"):
        snapshot_live_config(source, tmp_path / "snapshot")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["codex"]


def test_codex_materialization_copies_entries_without_claude_state(tmp_path):
    home = live_codex_home(tmp_path)
    recorded = snapshot_live_config(LiveSource(AgentKind.CODEX, home, None), tmp_path / "snapshot")
    target = tmp_path / "attempt"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot", target, tmp_path / "workspace", codex_live_recipe(), recorded
    )
    assert files_under(target) == {
        "config.toml",
        "AGENTS.md",
        "skills/review/SKILL.md",
        "rules/default.rules",
        "prompts/plan.md",
        "hooks/stop.sh",
        "hooks.json",
    }
    assert (target / "config.toml").read_bytes() == (home / "config.toml").read_bytes()


@pytest.mark.parametrize("parent", ["plain", "Zoë"])
def test_codex_materialization_moves_live_hook_trust_to_the_copied_config(tmp_path, parent):
    home = live_codex_home(tmp_path / parent)
    trusted = "sha256:" + "ab" * 32
    plugin = "sha256:" + "cd" * 32
    sibling = "sha256:" + "ef" * 32
    with (home / "config.toml").open("a") as config:
        config.write(
            f'\n[hooks.state."{home}/hooks.json:stop:0:0"]\ntrusted_hash = "{trusted}"\n'
            f'\n[hooks.state."demo@demo:hooks/hooks.json:stop:0:0"]\ntrusted_hash = "{plugin}"\n'
            f'\n[hooks.state."{home}-other/hooks.json:stop:0:0"]\ntrusted_hash = "{sibling}"\n'
        )
    recorded = snapshot_live_config(LiveSource(AgentKind.CODEX, home, None), tmp_path / "snapshot")
    target = tmp_path / "attempt"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot", target, tmp_path / "workspace", codex_live_recipe(), recorded
    )
    config = tomllib.loads((target / "config.toml").read_text())
    assert config["hooks"]["state"] == {
        f"{target}/hooks.json:stop:0:0": {"trusted_hash": trusted},
        "demo@demo:hooks/hooks.json:stop:0:0": {"trusted_hash": plugin},
        f"{home}-other/hooks.json:stop:0:0": {"trusted_hash": sibling},
    }
    assert config["model"] == "gpt-5.6-terra"


def test_codex_materialization_moves_hook_trust_keyed_by_the_canonical_codex_home(tmp_path):
    real = live_codex_home(tmp_path / "real")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    configured = tmp_path / "link/codex"
    trusted = "sha256:" + "ab" * 32
    with (real / "config.toml").open("a") as config:
        config.write(f'\n[hooks.state."{real}/hooks.json:stop:0:0"]\ntrusted_hash = "{trusted}"\n')
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(configured)}, tmp_path)
    recorded = snapshot_live_config(source, tmp_path / "snapshot")
    target = tmp_path / "attempt"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot", target, tmp_path / "workspace", codex_live_recipe(), recorded
    )
    assert recorded.source == str(configured)
    assert tomllib.loads((target / "config.toml").read_text())["hooks"]["state"] == {
        f"{target}/hooks.json:stop:0:0": {"trusted_hash": trusted}
    }


def test_codex_materialization_refuses_hook_trust_it_cannot_move(tmp_path):
    home = live_codex_home(tmp_path)
    with (home / "config.toml").open("a") as config:
        config.write(
            f'\n[hooks]\nstate = {{ "{home}/hooks.json:stop:0:0" = '
            f'{{ trusted_hash = "sha256:{"ab" * 32}" }} }}\n'
        )
    recorded = snapshot_live_config(LiveSource(AgentKind.CODEX, home, None), tmp_path / "snapshot")
    target = tmp_path / "attempt"
    target.mkdir()
    with pytest.raises(InputError, match="records hook trust in a form the snapshot cannot move"):
        materialize_live_config(
            tmp_path / "snapshot", target, tmp_path / "workspace", codex_live_recipe(), recorded
        )


def test_materialization_keeps_file_modes(tmp_path):
    home = live_codex_home(tmp_path)
    (home / "hooks/stop.sh").chmod(0o755)
    recorded = snapshot_live_config(LiveSource(AgentKind.CODEX, home, None), tmp_path / "snapshot")
    target = tmp_path / "attempt"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot", target, tmp_path / "workspace", codex_live_recipe(), recorded
    )
    assert stat.S_IMODE((target / "hooks/stop.sh").stat().st_mode) == 0o755


def _fifo(path: Path) -> None:
    path.unlink()
    os.mkfifo(path)


def _relink(path: Path) -> None:
    path.unlink()
    path.symlink_to(path.parent / "config.toml")


@pytest.mark.parametrize(
    ("agent", "tamper"),
    [
        (AgentKind.CODEX, lambda snapshot: (snapshot / "AGENTS.md").unlink()),
        (AgentKind.CODEX, lambda snapshot: (snapshot / "skills/review/SKILL.md").unlink()),
        (AgentKind.CODEX, lambda snapshot: _fifo(snapshot / "AGENTS.md")),
        (AgentKind.CODEX, lambda snapshot: _relink(snapshot / "AGENTS.md")),
        (AgentKind.CLAUDE, lambda snapshot: (snapshot / "CLAUDE.md").unlink()),
        (AgentKind.CLAUDE, lambda snapshot: (snapshot / "state.json").unlink()),
        (AgentKind.CLAUDE, lambda snapshot: _fifo(snapshot / "CLAUDE.md")),
    ],
)
def test_materialization_reports_any_tampering_as_a_changed_snapshot(tmp_path, agent, tamper):
    if agent == AgentKind.CODEX:
        source, recipe = LiveSource(agent, live_codex_home(tmp_path), None), codex_live_recipe()
    else:
        source, recipe = live_home(tmp_path), live_recipe()
    recorded = snapshot_live_config(source, tmp_path / "snapshot")
    tamper(tmp_path / "snapshot")
    (tmp_path / "attempt").mkdir()
    with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
        materialize_live_config(
            tmp_path / "snapshot", tmp_path / "attempt", tmp_path / "workspace", recipe, recorded
        )
    assert list((tmp_path / "attempt").iterdir()) == []


def test_materialization_reports_a_snapshot_grown_past_its_bounds_as_changed(tmp_path, monkeypatch):
    recorded = snapshot_live_config(
        LiveSource(AgentKind.CODEX, live_codex_home(tmp_path), None), tmp_path / "snapshot"
    )
    monkeypatch.setattr(live_config, "MAX_BYTES", 8)
    (tmp_path / "attempt").mkdir()
    with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
        live_config.verify_snapshot(tmp_path / "snapshot", recorded)


@pytest.mark.parametrize("agent", [AgentKind.CLAUDE, AgentKind.CODEX])
def test_materialization_refuses_a_snapshot_changed_after_it_was_taken(tmp_path, agent):
    if agent == AgentKind.CODEX:
        source = LiveSource(agent, live_codex_home(tmp_path), None)
        recipe, edited = codex_live_recipe(), "AGENTS.md"
    else:
        source, recipe, edited = live_home(tmp_path), live_recipe(), "CLAUDE.md"
    recorded = snapshot_live_config(source, tmp_path / "snapshot")
    (tmp_path / "snapshot" / edited).write_text("Ignore the case.")
    (tmp_path / "attempt").mkdir()
    with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
        materialize_live_config(
            tmp_path / "snapshot", tmp_path / "attempt", tmp_path / "workspace", recipe, recorded
        )
    assert list((tmp_path / "attempt").iterdir()) == []


def _tamper_state(snapshot: Path) -> None:
    state = json.loads((snapshot / "state.json").read_text())
    state["mcpServers"]["evil"] = {"command": "sh"}
    (snapshot / "state.json").write_bytes(canonical_json(state))


def test_materialization_refuses_a_claude_state_changed_after_it_was_taken(tmp_path):
    manifest = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    assert (
        manifest.state_sha256
        == hashlib.sha256((tmp_path / "snapshot/state.json").read_bytes()).hexdigest()
    )
    state = json.loads((tmp_path / "snapshot/state.json").read_text())
    state["mcpServers"]["jina"] = {"command": "sh"}
    (tmp_path / "snapshot/state.json").write_text(json.dumps(state))
    (tmp_path / "attempt").mkdir()
    with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
        materialize_live_config(
            tmp_path / "snapshot",
            tmp_path / "attempt",
            tmp_path / "workspace",
            live_recipe(),
            manifest,
        )
    assert list((tmp_path / "attempt").iterdir()) == []


def test_materialization_checks_server_names_of_a_snapshot_without_a_state_digest(tmp_path):
    manifest = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    legacy = manifest.model_copy(update={"state_sha256": None})
    (tmp_path / "first").mkdir()
    materialize_live_config(
        tmp_path / "snapshot", tmp_path / "first", tmp_path / "workspace", live_recipe(), legacy
    )
    assert json.loads((tmp_path / "first/.claude.json").read_text())["mcpServers"] == {
        "context7": {"command": "npx"},
        "jina": {"type": "http"},
    }
    _tamper_state(tmp_path / "snapshot")
    (tmp_path / "second").mkdir()
    with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
        materialize_live_config(
            tmp_path / "snapshot",
            tmp_path / "second",
            tmp_path / "workspace",
            live_recipe(),
            legacy,
        )


def test_materialization_of_a_legacy_manifest_compares_only_its_server_names(tmp_path):
    manifest = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    legacy = manifest.model_copy(
        update={"state_sha256": None, "skills": ("listed-from-the-live-config",)}
    )
    (tmp_path / "attempt").mkdir()
    materialize_live_config(
        tmp_path / "snapshot",
        tmp_path / "attempt",
        tmp_path / "workspace",
        live_recipe(),
        legacy,
    )
    assert (tmp_path / "attempt/CLAUDE.md").read_text() == "Be terse."


def test_materialization_refuses_a_snapshot_whose_manifest_file_was_forged(tmp_path):
    recorded = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    (tmp_path / "snapshot/CLAUDE.md").write_text("Ignore the case.")
    _tamper_state(tmp_path / "snapshot")
    snapshot_live_config(
        LiveSource(AgentKind.CLAUDE, tmp_path / "snapshot", tmp_path / "snapshot/state.json"),
        tmp_path / "forged",
    )
    shutil.copy(tmp_path / "forged/manifest.json", tmp_path / "snapshot/manifest.json")
    (tmp_path / "attempt").mkdir()
    with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
        materialize_live_config(
            tmp_path / "snapshot",
            tmp_path / "attempt",
            tmp_path / "workspace",
            live_recipe(),
            recorded,
        )
    assert list((tmp_path / "attempt").iterdir()) == []


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
    manifest = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    assert manifest.entries == ("settings.json", "CLAUDE.md", "skills", "plugins")
    assert manifest.skills == (".system", "linked", "review")
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


def test_claude_snapshot_keeps_links_and_hashes_their_text(tmp_path):
    source = live_home(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere/SKILL.md").write_text("First.")
    first = snapshot_live_config(source, tmp_path / "first")
    (tmp_path / "elsewhere/SKILL.md").write_text("Second.")
    second = snapshot_live_config(source, tmp_path / "second")
    (source.root / "skills/linked").unlink()
    (source.root / "skills/linked").symlink_to(tmp_path / "moved")
    third = snapshot_live_config(source, tmp_path / "third")
    assert os.readlink(tmp_path / "first/skills/linked") == str(tmp_path / "elsewhere")
    assert (first.tree_sha256 == second.tree_sha256, second.tree_sha256 == third.tree_sha256) == (
        True,
        False,
    )


def test_snapshot_digest_changes_with_config_bytes(tmp_path):
    source = live_home(tmp_path)
    first = snapshot_live_config(source, tmp_path / "first")
    (source.root / "CLAUDE.md").write_text("Be verbose.")
    second = snapshot_live_config(source, tmp_path / "second")
    assert first.tree_sha256 != second.tree_sha256
    assert first.instructions_sha256 != second.instructions_sha256
    assert first.settings_sha256 == second.settings_sha256


def test_materialized_copy_rebases_plugins_trusts_workspace_and_removes_servers(tmp_path):
    recorded = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    target, workspace = tmp_path / "attempt", tmp_path / "workspace"
    target.mkdir()
    materialize_live_config(
        tmp_path / "snapshot",
        target,
        workspace,
        live_recipe(removed_mcp_servers=("jina",)),
        recorded,
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
    recorded = snapshot_live_config(live_home(tmp_path), tmp_path / "snapshot")
    (tmp_path / "attempt").mkdir()
    with pytest.raises(InputError, match="not user-scoped"):
        materialize_live_config(
            tmp_path / "snapshot",
            tmp_path / "attempt",
            tmp_path / "workspace",
            live_recipe(removed_mcp_servers=("github",)),
            recorded,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"agent": AgentKind.DROID},
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


def test_live_profile_launches_from_snapshot_with_inherited_environment(
    store, tmp_path, monkeypatch
):
    executable = tmp_path / "bin/claude"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(executable.parent))
    source = live_home(tmp_path)
    config = source.root
    recorded = snapshot_live_config(source, tmp_path / "snapshots" / LIVE_CONFIG_DIRECTORY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = capture_profile(store, CaptureSpec(recipe=live_recipe()))
    with pytest.raises(InputError, match="run's recorded config snapshot"):
        materialize_profile(store, profile, tmp_path / "missing", workspace)
    with pytest.raises(InputError, match="run's recorded config snapshot"):
        materialize_profile(
            store,
            profile,
            tmp_path / "unrecorded",
            workspace,
            LaunchOptions(live=LiveSnapshots(tmp_path / "snapshots", {})),
        )
    plan = materialize_profile(
        store,
        profile,
        tmp_path / "runtime",
        workspace,
        LaunchOptions(live=LiveSnapshots(tmp_path / "snapshots", {AgentKind.CLAUDE: recorded})),
    )
    assert plan.environment_policy == "inherited"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in plan.generated_environment
    assert sorted({item.code for item in plan.issues}) == [
        "ambient-config",
        "ambient-plugin",
        "ambient-skill",
        "authentication-unverified",
        "live-config",
        "native-environment",
        "operator-skills",
        "permissions-unverified",
        "version-unobserved",
        "version-unpinned",
    ]
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
    assert (environment["JINA_API_KEY"], environment["CLAUDE_CODE_OAUTH_TOKEN"]) == ("j", "t")
    assert environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "runtime/config")
    assert {"CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"}.isdisjoint(environment)


def test_run_snapshots_live_config_once_and_journals_it(store, benchmark, tmp_path):
    config = live_home(tmp_path).root
    profile = capture_profile(store, CaptureSpec(recipe=live_recipe()))
    draft = benchmark.model_copy(
        update={"variants": (VariantSpec(name="live", profile=profile),), "repetitions": 1}
    )
    experiment = load_experiment(store, create_experiment(store, draft))
    run_id = RunStore(store.root).create(store.resolve(profile))
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        environment = {"CLAUDE_CONFIG_DIR": str(config)}
        _prepare_live_config(store, execution, experiment, HostContext(environment, tmp_path))
        (config / "CLAUDE.md").write_text("Changed mid-run.")
        _prepare_live_config(store, execution, experiment, HostContext(environment, tmp_path))
        events = [event for event in journal.events if event.event == "live-config"]
        assert len(events) == 1
        assert events[0].data["mcp_servers"] == ["context7", "jina"]
        assert (journal.path / LIVE_CONFIG_DIRECTORY / "CLAUDE.md").read_text() == "Be terse."


def test_run_refuses_to_remove_a_server_missing_from_the_snapshot(store, benchmark, tmp_path):
    config = live_home(tmp_path).root
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
            store,
            ExecutionJournal(journal),
            experiment,
            HostContext({"CLAUDE_CONFIG_DIR": str(config)}, tmp_path),
        )


def test_live_profile_materializes_from_the_recorded_manifest(store, tmp_path):
    source = live_home(tmp_path)
    snapshots = tmp_path / "snapshots"
    recorded = snapshot_live_config(source, snapshots / LIVE_CONFIG_DIRECTORY)
    (snapshots / LIVE_CONFIG_DIRECTORY / "manifest.json").write_bytes(
        canonical_json(recorded.model_copy(update={"tree_sha256": "f" * 64}))
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = capture_profile(store, CaptureSpec(recipe=live_recipe()))
    materialize_profile(
        store,
        profile,
        tmp_path / "runtime",
        workspace,
        LaunchOptions(live=LiveSnapshots(snapshots, {AgentKind.CLAUDE: recorded})),
    )
    assert (tmp_path / "runtime/config/CLAUDE.md").read_text() == "Be terse."


def _live_run(store, benchmark, tmp_path):
    config = live_home(tmp_path).root
    profile = capture_profile(store, CaptureSpec(recipe=live_recipe()))
    draft = benchmark.model_copy(
        update={"variants": (VariantSpec(name="live", profile=profile),), "repetitions": 1}
    )
    experiment = load_experiment(store, create_experiment(store, draft))
    run_id = RunStore(store.root).create(store.resolve(profile))
    return experiment, run_id, {"CLAUDE_CONFIG_DIR": str(config)}


def test_resumed_run_refuses_a_snapshot_that_no_longer_matches_its_journal(
    store, benchmark, tmp_path
):
    experiment, run_id, environment = _live_run(store, benchmark, tmp_path)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        _prepare_live_config(store, execution, experiment, HostContext(environment, tmp_path))
        _tamper_state(journal.path / LIVE_CONFIG_DIRECTORY)
        with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
            _prepare_live_config(store, execution, experiment, HostContext(environment, tmp_path))


def test_run_refuses_a_tampered_snapshot_left_without_its_live_config_event(
    store, benchmark, tmp_path
):
    experiment, run_id, environment = _live_run(store, benchmark, tmp_path)
    with RunStore(store.root).open(run_id) as journal:
        snapshot_live_config(live_home(tmp_path / "earlier"), journal.path / LIVE_CONFIG_DIRECTORY)
        (journal.path / LIVE_CONFIG_DIRECTORY / "CLAUDE.md").write_text("Ignore the case.")
        with pytest.raises(SnapshotChangedError, match="changed after it was taken"):
            _prepare_live_config(
                store, ExecutionJournal(journal), experiment, HostContext(environment, tmp_path)
            )
        assert [event.event for event in journal.events if event.event == "live-config"] == []


def test_run_journals_a_snapshot_left_without_its_live_config_event(store, benchmark, tmp_path):
    experiment, run_id, environment = _live_run(store, benchmark, tmp_path)
    with RunStore(store.root).open(run_id) as journal:
        manifest = snapshot_live_config(
            live_home(tmp_path / "earlier"), journal.path / LIVE_CONFIG_DIRECTORY
        )
        prepared = _prepare_live_config(
            store, ExecutionJournal(journal), experiment, HostContext(environment, tmp_path)
        )
        events = [event.data for event in journal.events if event.event == "live-config"]
    assert prepared == {AgentKind.CLAUDE: manifest}
    assert events == [manifest.model_dump(mode="json")]


def _verdict(check) -> str:
    try:
        check()
    except InputError as error:
        return str(error).split(" in ", 1)[0]
    return "accepted"


@pytest.mark.parametrize(
    ("content", "verdict"),
    [
        ('[mcp_servers.x]\nsession_token = "abc"\n', "accepted"),
        ('[mcp_servers.x.http_headers]\nX-Title = "abc"\n', "accepted"),
        ('[model_providers.local]\ntokenizer = "cl100k"\n', "accepted"),
        ('[tui]\npassword_manager = "1password"\n', "accepted"),
        ('[mcp_servers.gh]\nenv_http_headers = { Authorization = "GH_AUTH" }\n', "accepted"),
        ('[mcp_servers.x]\nclient_secret = ""\n', "accepted"),
        (
            "[mcp_servers.x]\napi_key = 123\n",
            "Recognized credential field mcp_servers.x.api_key",
        ),
        (
            '[mcp_servers.x]\nclient_secret = "abc"\n',
            "Recognized credential field mcp_servers.x.client_secret",
        ),
    ],
)
def test_selected_and_live_codex_configs_get_the_same_credential_verdict(
    tmp_path, content, verdict
):
    home = live_codex_home(tmp_path)
    (home / "config.toml").write_text(content)
    source = live_source(AgentKind.CODEX, {"CODEX_HOME": str(home)}, tmp_path)
    selected = _verdict(
        lambda: inspect_config(content.encode(), "config.toml", location="selected asset")
    )
    live = _verdict(lambda: snapshot_live_config(source, tmp_path / "snapshot"))
    assert (selected, live) == (verdict, verdict)
