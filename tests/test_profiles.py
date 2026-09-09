import json
from pathlib import Path

import pytest

from conftest import capture_profile_fixture as capture
from conftest import profile_recipe as recipe
from conftest import profile_selection as selection
from dryheave.errors import InputError, IntegrityError, LimitError, PathError
from dryheave.models import AgentKind, EnvironmentReference, ObjectKind
from dryheave.profile_models import (
    CaptureLimits,
    CaptureSpec,
    DeriveSpec,
    NativeRecipe,
    RuntimeFileReference,
)
from dryheave.profiles import capture_profile, derive_profile, diff_profiles, load_profile
from dryheave.serialization import canonical_json, parse_model


def test_capture_freezes_actual_layered_bytes_and_exec_mode(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_bytes(b"Use concise answers.\r\n")
    (source / "config.toml").write_text('model = "test-model"\n')
    (source / "skill").mkdir()
    (source / "skill/SKILL.md").write_text("# Selected\nUse script.sh.\n")
    script = source / "skill/script.sh"
    script.write_text("exit 0\n")
    script.chmod(0o755)
    identifier = capture(
        store,
        source,
        selection("AGENTS.md"),
        selection("AGENTS.md", layer="project"),
        selection("config.toml", kind="config"),
        selection("skill", target="skills/selected", kind="skill"),
    )
    frozen = load_profile(store, identifier)
    assert len(frozen.assets) == 5
    assert store.read_blob(identifier, "global/config/AGENTS.md") == b"Use concise answers.\r\n"
    assert store.read_blob(identifier, "project/project/AGENTS.md") == b"Use concise answers.\r\n"
    assert (
        next(item for item in frozen.assets if item.target.endswith("script.sh")).executable is True
    )
    (source / "AGENTS.md").write_text("Changed source")
    script.unlink()
    assert load_profile(store, identifier) == frozen
    assert store.read_blob(identifier, "global/config/skills/selected/script.sh") == b"exit 0\n"


@pytest.mark.parametrize(
    "content",
    [
        'api_key = "synthetic-secret-value"',
        '[env]\nOPENAI_API_KEY="synthetic-secret-value"',
        '[model_providers.custom.http_headers]\nAuthorization="synthetic-secret-value"',
        'password = "synthetic-secret-value"',
        'model="sk-proj-abcdefghijklmnop"',
    ],
)
def test_secret_config_rejected_before_any_publication(store, tmp_path: Path, content: str) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "config.toml").write_text(content)
    with pytest.raises(InputError, match="credential") as error:
        capture(store, source, selection("config.toml", kind="config"))
    assert (
        str(error.value)
        == "Recognized credential field in selected asset global/config/config.toml; use a runtime reference."
        or str(error.value)
        == "Recognized credential material in selected asset global/config/config.toml; use a runtime reference."
    )
    assert list(store.root.rglob("manifest.json")) == []
    assert list(store.root.rglob("blobs")) == []


@pytest.mark.parametrize(
    "path",
    [
        "auth.json",
        ".credentials.json",
        ".env.local",
        "sessions/log.json",
        "trust/permissions.json",
        ".netrc",
        "history.jsonl",
    ],
)
def test_sensitive_paths_never_read(store, tmp_path: Path, monkeypatch, path: str) -> None:
    import dryheave.profile_capture as module

    source = tmp_path / "selected"
    source.mkdir()

    def forbidden(*args, **kwargs):
        pytest.fail("Sensitive files must be rejected before reading")

    monkeypatch.setattr(module, "read_bytes", forbidden)
    with pytest.raises(InputError, match="credential, history, cache or trust"):
        capture(store, source, selection(path, kind="resource"))


@pytest.mark.parametrize(
    "arguments",
    [
        ("--api-key", "synthetic-secret-value"),
        ("--config", "api_key=synthetic-secret-value"),
        ("--token=synthetic-secret-value",),
        ("exec wrapper --api-key synthetic-secret-value",),
    ],
)
def test_secret_argv_rejected_without_echo(store, arguments) -> None:
    with pytest.raises(InputError, match="argv entry") as error:
        capture_profile(store, CaptureSpec(recipe=recipe(arguments=arguments)))
    assert str(error.value).endswith("use a runtime reference.")
    assert list(store.root.rglob("manifest.json")) == []


def test_runtime_references_store_only_names_and_write_acknowledgement(store) -> None:
    native = recipe(
        environment=(EnvironmentReference(name="CODEX_ACCESS_TOKEN"),),
        runtime_files=(
            RuntimeFileReference(
                name="subscription",
                source_path_environment=EnvironmentReference(name="SUBJECT_AUTH_PATH"),
                acknowledge_source_writes=True,
            ),
        ),
    )
    identifier = capture_profile(store, CaptureSpec(recipe=native))
    manifest = store.get(identifier)
    assert manifest.files == {}
    assert (
        load_profile(store, identifier).recipe.runtime_files[0].source_path_environment.name
        == "SUBJECT_AUTH_PATH"
    )
    assert manifest.payload["recipe"]["environment"] == [
        {"name": "CODEX_ACCESS_TOKEN", "source": "inherited"}
    ]


def test_selected_symlinks_freeze_content_with_bounded_roots(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "real.md").write_text("Frozen target")
    (source / "link.md").symlink_to("real.md")
    identifier = capture(store, source, selection("link.md", target="AGENTS.md"))
    assert store.read_blob(identifier, "global/config/AGENTS.md") == b"Frozen target"
    assert load_profile(store, identifier).assets[0].resolved_source == str(source / "real.md")
    (source / "link.md").unlink()
    (source / "link.md").symlink_to("../outside.md")
    (tmp_path / "outside.md").write_text("External")
    with pytest.raises(PathError, match="include roots"):
        capture(store, source, selection("link.md", target="AGENTS.md"))


def test_symlink_components_resolve_before_parent_traversal(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "inside").mkdir()
    (source / "jump").symlink_to("inside")
    (source / "answer.md").write_text("correct")
    (source / "link").symlink_to("jump/../answer.md")
    identifier = capture(store, source, selection("link", target="AGENTS.md"))
    assert store.read_blob(identifier, "global/config/AGENTS.md") == b"correct"
    (source / "jump").unlink()
    (source / "jump").symlink_to("../outside")
    (tmp_path / "outside").mkdir()
    with pytest.raises(PathError, match="include roots"):
        capture(store, source, selection("link", target="AGENTS.md"))


def test_symlink_cycles_and_secret_targets_are_rejected(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "a").symlink_to("b")
    (source / "b").symlink_to("a")
    with pytest.raises(LimitError, match="symlink"):
        capture(store, source, selection("a", target="AGENTS.md"))
    (source / "a").unlink()
    (source / "a").symlink_to("auth.json")
    with pytest.raises(InputError, match="credential, history"):
        capture(store, source, selection("a", target="AGENTS.md"))
    (source / "dir").mkdir()
    (source / "dir/recurse").symlink_to(".")
    with pytest.raises(PathError, match="cycle"):
        capture(store, source, selection("dir", target="skills/loop", kind="skill"))


def test_capture_limits_and_collisions(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("12345")
    with pytest.raises(LimitError, match="limit"):
        capture(store, source, selection("AGENTS.md"), limits=CaptureLimits(max_file_bytes=4))
    with pytest.raises(InputError, match="collide"):
        capture(store, source, selection("AGENTS.md"), selection("AGENTS.md"))
    with pytest.raises(LimitError, match="entries"):
        capture(
            store,
            source,
            selection("AGENTS.md"),
            selection("AGENTS.md", layer="project"),
            limits=CaptureLimits(max_files=1),
        )


def test_immutable_derive_and_alias_change(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("original")
    first = capture(store, source, selection("AGENTS.md"))
    store.set_alias("everyday", first)
    (source / "AGENTS.md").write_text("new source")
    second = derive_profile(
        store,
        "everyday",
        DeriveSpec(
            recipe_changes={"model": "new-model", "effort": "high", "workflow": "ask-first"}
        ),
    )
    store.set_alias("everyday", second, replace=True)
    assert load_profile(store, first).recipe.model is None
    assert load_profile(store, second).recipe.model == "new-model"
    assert store.read_blob(second, "global/config/AGENTS.md") == b"original"
    assert store.get(second).references == (first,)
    differences = diff_profiles(store, first, second)
    assert differences["recipe_changes"]["effort"] == {"before": None, "after": "high"}
    assert differences["changed"] == []
    with pytest.raises(InputError, match="change an immutable"):
        derive_profile(store, second, DeriveSpec())
    with pytest.raises(InputError, match="Cross-agent"):
        derive_profile(store, second, DeriveSpec(recipe_changes={"agent": "claude"}))


def test_derive_config_replacement_requires_explicit_collision_list(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "config.toml").write_text('model="before"')
    asset = selection("config.toml", kind="config")
    first = capture(store, source, asset)
    (source / "config.toml").write_text('model="after"')
    additions = CaptureSpec(
        recipe=recipe(), include_roots={"selected": str(source)}, assets=(asset,)
    )
    with pytest.raises(InputError, match="Replacement list"):
        derive_profile(store, first, DeriveSpec(additions=additions))
    second = derive_profile(
        store, first, DeriveSpec(additions=additions, replace=("global/config/config.toml",))
    )
    assert store.read_blob(second, "global/config/config.toml") == b'model="after"'
    assert diff_profiles(store, first, second)["changed"] == ["global/config/config.toml"]
    removed = derive_profile(store, second, DeriveSpec(remove=("global/config/config.toml",)))
    assert load_profile(store, removed).assets == ()


@pytest.mark.parametrize("selected_path", ["tree", "tree/a", "tree/a/b", "tree/a/b/file.txt"])
def test_derive_enforces_parent_depth_from_each_selection(
    store, tmp_path: Path, selected_path: str
) -> None:
    source = tmp_path / "selected"
    (source / "tree/a/b").mkdir(parents=True)
    (source / "tree/a/b/file.txt").write_bytes(b"selected resource")
    parent = capture_profile(store, CaptureSpec(recipe=recipe(), limits=CaptureLimits(max_depth=1)))
    additions = CaptureSpec(
        recipe=recipe(),
        include_roots={"selected": str(source)},
        assets=(selection(selected_path, kind="resource", target="resources/deep/selection"),),
    )
    published = list(store.root.rglob("manifest.json"))

    if selected_path in {"tree", "tree/a"}:
        with pytest.raises(InputError, match="parent's capture bounds"):
            derive_profile(store, parent, DeriveSpec(additions=additions))
        assert list(store.root.rglob("manifest.json")) == published
    else:
        derived = derive_profile(store, parent, DeriveSpec(additions=additions))
        profile = load_profile(store, derived)
        assert profile.limits == CaptureLimits(max_depth=1)
        assert profile.assets[0].selection.target == "resources/deep/selection"
        assert store.read_blob(derived, profile.assets[0].blob) == b"selected resource"


def test_loading_rejects_expanded_targets_beyond_frozen_depth(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    (source / "tree/a/b").mkdir(parents=True)
    (source / "tree/a/b/file.txt").write_bytes(b"selected resource")
    captured = capture(store, source, selection("tree", kind="resource", target="resources/tree"))
    profile = load_profile(store, captured)
    forged = store.put(
        ObjectKind.PROFILE,
        profile.model_copy(update={"limits": CaptureLimits(max_depth=1)}),
        files={item.blob: store.read_blob(captured, item.blob) for item in profile.assets},
    )

    with pytest.raises(IntegrityError, match="depth exceeds its frozen capture bounds"):
        load_profile(store, forged)


def test_unknown_translation_and_forged_profile_rejected(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "CLAUDE.md").write_text("native Claude")
    with pytest.raises(InputError, match="Cross-agent"):
        capture(store, source, selection("CLAUDE.md"))
    first = capture_profile(store, CaptureSpec(recipe=recipe()))
    profile = load_profile(store, first)
    forged = store.put(ObjectKind.PROFILE, profile, files={"extra.txt": b"untracked dependency"})
    with pytest.raises(IntegrityError, match="asset hashes"):
        load_profile(store, forged)
    with pytest.raises(InputError, match="schema"):
        parse_model(
            canonical_json(profile).replace(b'"schema_version":1', b'"schema_version":2'),
            type(profile),
        )


def test_json_credentials_and_external_references(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "synthetic-secret-value"}})
    )
    native = NativeRecipe(agent=AgentKind.CLAUDE, executable="claude", version="2.1.263")
    with pytest.raises(InputError, match="credential"):
        capture(store, source, selection("settings.json", kind="config"), native=native)
    (source / "AGENTS.md").write_text("Read @docs/task.md and /opt/custom/helper")
    identifier = capture(store, source, selection("AGENTS.md"))
    assert {item.code for item in load_profile(store, identifier).issues} >= {
        "instruction-import",
        "absolute-reference",
    }


def test_imported_profile_cannot_redirect_expanded_asset(store, tmp_path: Path) -> None:
    from dryheave.models import ObjectKind
    from dryheave.serialization import parse_json

    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("safe input")
    first = capture(store, source, selection("AGENTS.md"))
    profile = load_profile(store, first)
    forged_data = parse_json(canonical_json(profile))
    forged_data["assets"][0]["target"] = "auth.json"
    forged_data["assets"][0]["blob"] = "global/config/auth.json"
    with pytest.raises(InputError, match="schema"):
        parse_model(json.dumps(forged_data).encode(), type(profile))
    forged_asset = profile.assets[0].model_copy(
        update={"target": "auth.json", "blob": "global/config/auth.json"}
    )
    with pytest.raises(InputError, match="schema"):
        store.put(
            ObjectKind.PROFILE,
            profile.model_copy(update={"assets": (forged_asset,)}),
            files={"global/config/auth.json": b"safe input"},
        )


def test_case_insensitive_target_collisions_are_rejected(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "readme.txt").write_text("same filesystem spelling")
    with pytest.raises(ValueError, match="case-insensitive"):
        capture(
            store,
            source,
            selection("readme.txt", kind="resource", target="resources/README"),
            selection("readme.txt", kind="resource", target="resources/readme"),
        )


def test_selected_second_root_symlink_and_directory_bounds(store, tmp_path: Path) -> None:
    source, second = tmp_path / "selected", tmp_path / "dependency"
    source.mkdir()
    second.mkdir()
    (second / "instructions.md").write_text("Explicit dependency")
    (source / "linked").symlink_to(second / "instructions.md")
    identifier = capture_profile(
        store,
        CaptureSpec(
            recipe=recipe(),
            include_roots={"selected": str(source), "dependency": str(second)},
            assets=(selection("linked", target="AGENTS.md"),),
        ),
    )
    assert store.read_blob(identifier, "global/config/AGENTS.md") == b"Explicit dependency"
    (source / "deep/a/b").mkdir(parents=True)
    with pytest.raises(LimitError, match="depth"):
        capture(
            store,
            source,
            selection("deep", kind="resource", target="resources"),
            limits=CaptureLimits(max_depth=1),
        )
    (source / "a.md").write_text("123")
    (source / "b.md").write_text("456")
    with pytest.raises(LimitError, match="total bytes"):
        capture(
            store,
            source,
            selection("a.md", kind="resource"),
            selection("b.md", kind="resource"),
            limits=CaptureLimits(max_total_bytes=5),
        )


def test_nested_argv_header_and_persisted_trust_are_rejected(store, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="credential field"):
        capture_profile(
            store,
            CaptureSpec(
                recipe=recipe(
                    arguments=(
                        "--config",
                        'model_providers.custom.http_headers={Authorization="synthetic-value"}',
                    )
                )
            ),
        )
    source = tmp_path / "selected"
    source.mkdir()
    (source / "config.toml").write_text('[projects."/synthetic/project"]\ntrust_level="trusted"')
    with pytest.raises(InputError, match="persisted trust"):
        capture(store, source, selection("config.toml", kind="config"))
    assert list(store.root.rglob("manifest.json")) == []
