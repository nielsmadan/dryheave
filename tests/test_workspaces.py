import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dryheave.errors import ConflictError, InputError, LockBusyError, PathError
from dryheave.filesystem import directory_fd
from dryheave.workspaces import (
    CONFIG_FILE,
    DIRECTORY_OWNER_FILE,
    WORKSPACE_OWNER_FILE,
    WorkspaceConfig,
    discover_workspace,
    initialize_workspace,
    initialized_workspace,
    validate_runtime_root,
)


@pytest.fixture
def short_runtime():
    path = (Path(".cache") / ("w" + uuid4().hex[:5])).absolute()
    yield path
    if path.exists():
        shutil.rmtree(path)


def cli(cwd, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "dryheave", *arguments, "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_init_is_private_idempotent_and_preserves_authoring_data(tmp_path, short_runtime):
    root = tmp_path / "workspace"
    first = initialize_workspace(root, runtime_root=short_runtime)
    assert first.root == root
    assert first.path("skills") == root / ".agents/skills"
    (first.path("authoring") / "draft.json").write_bytes(b"retained draft")
    files = [root / CONFIG_FILE, root / WORKSPACE_OWNER_FILE]
    for role in ("authoring", "store", "runtime"):
        directory = first.path(role)
        assert directory.stat().st_mode & 0o777 == 0o700
        files.append(directory / DIRECTORY_OWNER_FILE)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert initialize_workspace(root) == first
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before
    assert (first.path("authoring") / "draft.json").read_bytes() == b"retained draft"
    assert initialized_workspace(root) == first


def test_init_transport_path_is_config_relative_private_and_idempotent(tmp_path, short_runtime):
    root = tmp_path / "workspace"
    first = initialize_workspace(root, runtime_root=short_runtime, tui_test=Path("tools/tui-test"))
    assert first.transport() == root / "tools/tui-test"
    assert 'tui_test = "tools/tui-test"' in (root / CONFIG_FILE).read_text()
    assert initialize_workspace(root) == first
    with pytest.raises(ConflictError, match="options differ"):
        initialize_workspace(root, tui_test=Path("other"))
    assert discover_workspace(root) == first


@pytest.mark.parametrize("value", ["", "../tui-test", "~/tui-test", "bad\npath"])
def test_transport_config_rejects_unsafe_paths(value):
    with pytest.raises(ValidationError):
        WorkspaceConfig(tui_test=value)


@pytest.mark.integration
def test_real_cli_nested_discovery_precedence_and_relocation(tmp_path, short_runtime, monkeypatch):
    root = tmp_path / "original"
    result = cli(tmp_path, "init", str(root), "--runtime-root", str(short_runtime))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["store"] == str(root / ".dryheave/store")
    moved = tmp_path / "relocated"
    root.rename(moved)
    nested = moved / "nested" / "child"
    nested.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert json.loads(cli(nested, "store", "path").stdout)["data"]["path"] == str(
        moved / ".dryheave/store"
    )
    override = cli(nested, "store", "path", "--store", "explicit")
    assert override.returncode == 0, override.stderr
    assert json.loads(override.stdout)["data"]["path"] == str(nested / "explicit")
    fallback = cli(tmp_path, "store", "path")
    assert fallback.returncode == 0, fallback.stderr
    assert json.loads(fallback.stdout)["data"]["path"] == str(tmp_path / "xdg" / "dryheave")
    (nested / CONFIG_FILE).write_text('schema_version = 1\nstore = "nearby"\n')
    nearest = cli(nested, "store", "path")
    assert nearest.returncode == 0, nearest.stderr
    assert json.loads(nearest.stdout)["data"]["path"] == str(nested / "nearby")


@pytest.mark.integration
@pytest.mark.parametrize(
    "content",
    [
        "store = [",
        "schema_version = 2",
        "schema_version = true",
        "store = 3",
        'other = "bad"',
        'store = "../escape"',
    ],
)
def test_nearest_invalid_config_is_an_error_not_a_parent_fallback(tmp_path, content):
    (tmp_path / CONFIG_FILE).write_text('schema_version = 1\nstore = "outer"\n')
    child = tmp_path / "child"
    child.mkdir()
    (child / CONFIG_FILE).write_text(content)
    result = cli(child, "store", "path", "--store", "override")
    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_input"
    assert str(child / CONFIG_FILE) in error["message"]


def test_foreign_config_and_directories_are_preserved(tmp_path, short_runtime):
    root = tmp_path / "workspace"
    root.mkdir()
    config = root / CONFIG_FILE
    config.write_text("schema_version = 1\n")
    with pytest.raises(ConflictError, match="foreign or edited"):
        initialize_workspace(root, runtime_root=short_runtime)
    assert set(root.iterdir()) == {config}
    assert config.read_text() == "schema_version = 1\n"
    config.unlink()
    foreign = root / ".dryheave/store"
    foreign.mkdir(parents=True)
    (foreign / "notes").write_bytes(b"preserve")
    with pytest.raises(ConflictError, match="foreign"):
        initialize_workspace(root, runtime_root=short_runtime)
    assert set(root.rglob("*")) == {foreign.parent, foreign, foreign / "notes"}
    assert (foreign / "notes").read_bytes() == b"preserve"


def test_edited_config_changes_defaults_but_init_refuses_to_overwrite(tmp_path, short_runtime):
    workspace = initialize_workspace(tmp_path, runtime_root=short_runtime)
    config = tmp_path / CONFIG_FILE
    changed = config.read_text().replace(".dryheave/store", "selected-store")
    config.write_text(changed)
    assert discover_workspace(tmp_path).path("store") == tmp_path / "selected-store"
    with pytest.raises(ConflictError, match="foreign or edited"):
        initialize_workspace(tmp_path)
    assert config.read_text() == changed
    assert (workspace.path("store") / DIRECTORY_OWNER_FILE).is_file()


def test_interrupted_init_resumes_owned_directories_and_original_options(
    tmp_path, short_runtime, monkeypatch
):
    from dryheave import workspaces

    write = workspaces.atomic_write

    def interrupted(path, content, *, replace=True):
        if path.name == CONFIG_FILE:
            raise OSError("interrupted before config publication")
        write(path, content, replace=replace)

    with monkeypatch.context() as context:
        context.setattr(workspaces, "atomic_write", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            initialize_workspace(tmp_path, runtime_root=short_runtime)
    owner = (tmp_path / WORKSPACE_OWNER_FILE).read_bytes()
    recovered = initialize_workspace(tmp_path)
    assert recovered.path("runtime") == short_runtime
    assert (tmp_path / WORKSPACE_OWNER_FILE).read_bytes() == owner
    assert initialized_workspace(tmp_path) == recovered


@pytest.mark.integration
def test_concurrent_init_reports_lock_busy_without_changing_files(tmp_path, short_runtime):
    with directory_fd(tmp_path) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli(tmp_path, "init", "--runtime-root", str(short_runtime))
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"]["code"] == "lock_busy"
        with pytest.raises(LockBusyError):
            initialize_workspace(tmp_path, runtime_root=short_runtime)
    assert list(tmp_path.iterdir()) == []
    assert initialize_workspace(tmp_path, runtime_root=short_runtime).root == tmp_path


@pytest.mark.parametrize("part", ["root", "config", "ownership", "directory", "marker", "ancestor"])
def test_init_rejects_symlinks_and_preserves_targets(tmp_path, short_runtime, part):
    root = tmp_path / "workspace"
    workspace = initialize_workspace(root, runtime_root=short_runtime)
    selected = root
    if part == "root":
        original = root
    elif part == "config":
        original = root / CONFIG_FILE
    elif part == "ownership":
        original = root / WORKSPACE_OWNER_FILE
    elif part == "marker":
        original = workspace.path("authoring") / DIRECTORY_OWNER_FILE
    elif part == "ancestor":
        original = root / ".dryheave"
    else:
        original = workspace.path("authoring")
    saved = tmp_path / "saved"
    original.rename(saved)
    original.symlink_to(saved, target_is_directory=saved.is_dir())
    before = (
        {path: path.read_bytes() for path in saved.rglob("*") if path.is_file()}
        if saved.is_dir()
        else saved.read_bytes()
    )
    with pytest.raises(PathError):
        initialize_workspace(selected)
    after = (
        {path: path.read_bytes() for path in saved.rglob("*") if path.is_file()}
        if saved.is_dir()
        else saved.read_bytes()
    )
    assert after == before


def test_native_runtime_limit_counts_utf8_and_child_and_reports_override(tmp_path):
    short = Path("/" + "a" * 58)
    validate_runtime_root(short)
    assert len(os.fsencode(short / ("x" * 10))) == 70
    for root in (Path("/" + "a" * 59), Path("/" + "ü" * 30)):
        with pytest.raises(InputError, match="init --runtime-root"):
            validate_runtime_root(root)
    with pytest.raises(InputError, match="10-character"):
        initialize_workspace(tmp_path / ("long" * 20))
    assert list((tmp_path / ("long" * 20)).iterdir()) == []


@pytest.mark.parametrize("value", ["", "..", "../r", "a/../r", "~/r", "bad\x00", "bad\n"])
def test_config_rejects_unsafe_paths(value):
    with pytest.raises(ValidationError):
        WorkspaceConfig(runtime=value)


@pytest.mark.parametrize(
    "content",
    [
        'store = "/"',
        'store = "."',
        'store = "same"\nauthoring = "same"',
        'store = "parent"\nauthoring = "parent/child"',
    ],
)
def test_config_rejects_overlapping_or_root_directories(tmp_path, content):
    (tmp_path / CONFIG_FILE).write_text(content)
    with pytest.raises(PathError):
        discover_workspace(tmp_path)


@pytest.mark.integration
def test_real_cli_workspace_skill_install_defaults_and_explicit_override(tmp_path, short_runtime):
    from dryheave.skills import SKILL_NAMES, bundled_skill

    workspace = initialize_workspace(tmp_path / "workspace", runtime_root=short_runtime)
    child = workspace.root / "nested"
    child.mkdir()
    first = cli(child, "skills", "install")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["data"]["target"] == str(workspace.path("skills"))
    second = cli(child, "skills", "install")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == json.loads(first.stdout)
    explicit = tmp_path / "explicit"
    result = cli(child, "skills", "install", "--target", str(explicit), SKILL_NAMES[0])
    assert result.returncode == 0, result.stderr
    assert (explicit / SKILL_NAMES[0] / "SKILL.md").read_bytes() == bundled_skill(SKILL_NAMES[0])
    assert set(explicit.iterdir()) == {explicit / SKILL_NAMES[0]}


@pytest.mark.integration
def test_omitted_skill_target_requires_initialized_workspace(tmp_path):
    result = cli(tmp_path, "skills", "install")
    assert result.returncode == 2
    assert "dryheave init" in json.loads(result.stderr)["error"]["message"]
    (tmp_path / CONFIG_FILE).write_text("schema_version = 1\n")
    result = cli(tmp_path, "skills", "install")
    assert result.returncode == 1
    assert "not initialized" in json.loads(result.stderr)["error"]["message"]
    assert set(tmp_path.iterdir()) == {tmp_path / CONFIG_FILE}
