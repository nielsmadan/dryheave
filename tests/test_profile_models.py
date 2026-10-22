import json

import pytest
from pydantic import ValidationError

from dryheave.errors import InputError
from dryheave.models import AgentKind, EnvironmentReference
from dryheave.profile_models import (
    LiveConfigManifest,
    NativeRecipe,
    RuntimeFileReference,
    codex_auth_reference,
)
from dryheave.serialization import canonical_json, parse_model

MANIFEST = {
    "schema_version": 1,
    "source": "/home/.claude",
    "entries": ["settings.json"],
    "files": 1,
    "bytes": 2,
    "tree_sha256": "a" * 64,
    "settings_sha256": "b" * 64,
    "instructions_sha256": None,
    "skills": [],
    "enabled_plugins": [],
    "mcp_servers": ["jina"],
    "hook_events": [],
}


def test_claude_manifest_keeps_its_serialized_identity():
    manifest = parse_model(json.dumps(MANIFEST).encode(), LiveConfigManifest)
    assert manifest.agent == AgentKind.CLAUDE
    assert json.loads(canonical_json(manifest)) == MANIFEST


def test_codex_manifest_serializes_its_agent():
    manifest = parse_model(json.dumps(MANIFEST | {"agent": "codex"}).encode(), LiveConfigManifest)
    assert json.loads(canonical_json(manifest)) == MANIFEST | {"agent": "codex"}
    assert parse_model(canonical_json(manifest), LiveConfigManifest) == manifest


def test_claude_manifest_serializes_a_recorded_state_digest():
    manifest = parse_model(
        json.dumps(MANIFEST | {"state_sha256": "c" * 64}).encode(), LiveConfigManifest
    )
    assert json.loads(canonical_json(manifest)) == MANIFEST | {"state_sha256": "c" * 64}


def test_manifest_refuses_a_version_field():
    with pytest.raises(InputError, match="version"):
        parse_model(
            json.dumps(MANIFEST | {"version": "codex-cli 0.162.1"}).encode(), LiveConfigManifest
        )


def codex_live_recipe(**changes: object) -> NativeRecipe:
    values: dict[str, object] = {
        "agent": AgentKind.CODEX,
        "executable": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
        "config_source": "live",
        "workspace_trust": "trusted",
        "arguments": ("--sandbox", "danger-full-access"),
        "runtime_files": (
            RuntimeFileReference(
                name="codex-auth",
                source_path_environment=EnvironmentReference(name="CODEX_AUTH_FILE"),
                acknowledge_source_writes=True,
            ),
        ),
    }
    return NativeRecipe.model_validate(values | changes)


@pytest.mark.parametrize(
    "effort", ["minimal", "low", "medium", "high", "xhigh", "max", "ultra", None]
)
def test_codex_live_recipe_accepts_codex_efforts(effort):
    recipe = codex_live_recipe(effort=effort)
    assert (recipe.config_source, recipe.effort) == ("live", effort)
    assert recipe.model_dump(mode="json")["config_source"] == "live"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"removed_mcp_servers": ("jina",)}, "only to live Claude"),
        ({"effort": "extreme"}, "Codex effort must be one of"),
        ({"codex_discovery": "disabled-plugins", "version": "0.154.0"}, "discovery policies"),
    ],
)
def test_codex_live_recipe_contract(changes, message):
    with pytest.raises(ValidationError, match=message):
        codex_live_recipe(**changes)


@pytest.mark.parametrize("version", ["0.153.4", "0.160.0", None])
def test_codex_discovery_rejects_an_unsupported_selected_version(version):
    with pytest.raises(ValidationError, match=r"requires version 0\.154\.0 or 0\.162\.1"):
        NativeRecipe(
            agent=AgentKind.CODEX,
            executable="codex",
            version=version,
            codex_discovery="disabled-plugins",
        )


@pytest.mark.parametrize("version", ["0.154.0", "0.162.1"])
def test_codex_discovery_accepts_a_supported_selected_version(version):
    recipe = NativeRecipe(
        agent=AgentKind.CODEX,
        executable="codex",
        version=version,
        codex_discovery="disabled-plugins",
    )
    assert (recipe.config_source, recipe.version) == ("selected", version)


@pytest.mark.parametrize(
    ("agent", "config_source", "live_codex"),
    [
        (AgentKind.CODEX, "live", True),
        (AgentKind.CODEX, "selected", False),
        (AgentKind.CLAUDE, "live", False),
    ],
)
def test_recipe_names_a_live_codex_subject(agent, config_source, live_codex):
    recipe = NativeRecipe(agent=agent, executable="native", config_source=config_source)
    assert recipe.live_codex is live_codex


def test_runtime_file_reference_serializes_its_default_source_only_when_set():
    explicit = codex_auth_reference("OPAQUE", default_source=None)
    assert explicit.model_dump(mode="json") == {
        "name": "codex-auth",
        "source_path_environment": {"name": "OPAQUE", "source": "inherited"},
        "target": "auth.json",
        "binding": "symlink",
        "acknowledge_source_writes": True,
    }
    assert codex_auth_reference().model_dump(mode="json")["default_source"] == "codex-home"
    assert (
        parse_model(canonical_json(codex_auth_reference()), RuntimeFileReference)
        == codex_auth_reference()
    )
