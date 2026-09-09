import json
import sys
from pathlib import Path

import pytest

from conftest import capture_profile_fixture as capture
from conftest import profile_recipe as recipe
from conftest import profile_selection as selection
from dryheave.errors import InputError, PathError
from dryheave.models import AgentKind, CommandSpec, EnvironmentReference
from dryheave.processes import run_command
from dryheave.profile_launch import launch_environment, materialize_profile, preflight_profile
from dryheave.profile_models import CaptureSpec, NativeRecipe
from dryheave.profiles import capture_profile


def test_native_launch_preserves_home_and_discloses_canaries(
    store, tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "native-home"
    (home / ".agents/skills/operator").mkdir(parents=True)
    (home / ".agents/skills/operator/SKILL.md").write_text("GLOBAL SKILL CANARY")
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text('model="GLOBAL CONFIG CANARY"')
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ancestor/repo"
    workspace.mkdir(parents=True)
    (workspace.parent / "AGENTS.md").write_text("ANCESTOR INSTRUCTION CANARY")
    (workspace.parent / ".codex/skills/operator").mkdir(parents=True)
    (workspace.parent / ".codex/skills/operator/SKILL.md").write_text("ANCESTOR SKILL CANARY")
    identifier = capture_profile(
        store,
        CaptureSpec(
            recipe=recipe(
                model="subject-model",
                effort="high",
                environment=(EnvironmentReference(name="SUBJECT_TOKEN"),),
            )
        ),
    )
    plan = preflight_profile(store, identifier, tmp_path / "runtime", workspace)
    assert plan.argv[:5] == (
        sys.executable,
        "--model",
        "subject-model",
        "--config",
        'model_reasoning_effort="high"',
    )
    assert "dryheave-collect" in plan.argv[-1]
    assert plan.cwd == str(workspace)
    assert plan.config_roots["home"] == str(home)
    assert (
        next(
            root for root in plan.discovery_roots if root.path == str(home / ".agents/skills")
        ).status
        == "unverified"
    )
    assert (
        next(root for root in plan.discovery_roots if root.path == str(workspace.parent)).status
        == "unverified"
    )
    values = launch_environment(
        plan,
        {
            "HOME": str(home),
            "PATH": "/synthetic/bin",
            "SUBJECT_TOKEN": "runtime-only-value",
            "UNREQUESTED_SECRET": "excluded-value",
        },
    )
    assert values["HOME"] == str(home)
    assert values == {
        "HOME": str(home),
        "PATH": "/synthetic/bin",
        "SUBJECT_TOKEN": "runtime-only-value",
        **plan.generated_environment,
    }
    assert store.get(identifier).files == {}
    assert (home / ".agents/skills/operator/SKILL.md").read_text() == "GLOBAL SKILL CANARY"
    with pytest.raises(InputError, match="Strict faithful launch refused"):
        preflight_profile(store, identifier, tmp_path / "strict-runtime", workspace, strict=True)
    assert (tmp_path / "strict-runtime").exists() is False


def test_materialize_preserves_historical_instructions_and_layers(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("Selected global")
    (source / "project.md").write_text("Selected project")
    identifier = capture(
        store,
        source,
        selection("AGENTS.md"),
        selection("project.md", target="AGENTS.md", layer="project"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Historical task rules")
    plan = materialize_profile(store, identifier, tmp_path / "runtime", workspace)
    assert (workspace / "AGENTS.md").read_text() == "Historical task rules"
    assert (tmp_path / "runtime/config/AGENTS.md").read_text() == "Selected global"
    assert [(item.blob, item.action) for item in plan.overlays] == [
        ("global/config/AGENTS.md", "create"),
        ("project/project/AGENTS.md", "preserve"),
    ]
    assert json.loads((tmp_path / "runtime/launch-plan.json").read_text())["argv"] == list(
        plan.argv
    )
    with pytest.raises(PathError, match="fresh"):
        materialize_profile(store, identifier, tmp_path / "runtime", workspace)


def test_explicit_project_replace_and_identical_overlay(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("Replacement rules")
    identifier = capture(store, source, selection("AGENTS.md", layer="project", overlay="replace"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Historical rules")
    plan = materialize_profile(store, identifier, tmp_path / "runtime", workspace)
    assert plan.overlays[0].action == "replace"
    assert plan.overlays[0].previous_sha256 is not None
    assert (workspace / "AGENTS.md").read_text() == "Replacement rules"
    again = preflight_profile(store, identifier, tmp_path / "other-runtime", workspace)
    assert again.overlays[0].action == "identical"


@pytest.mark.parametrize("overlay", ["preserve", "replace"])
@pytest.mark.parametrize(
    ("selected_mode", "historical_mode"),
    [(0o700, 0o600), (0o600, 0o700), (0o700, 0o700), (0o600, 0o600)],
)
def test_identical_project_bytes_respect_executable_overlay(
    store, tmp_path: Path, overlay, selected_mode: int, historical_mode: int
) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    script = source / "script.sh"
    script.write_bytes(b"exit 0\n")
    script.chmod(selected_mode)
    identifier = capture(
        store, source, selection("script.sh", kind="resource", layer="project", overlay=overlay)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    historical = workspace / "script.sh"
    historical.write_bytes(script.read_bytes())
    historical.chmod(historical_mode)

    plan = materialize_profile(store, identifier, tmp_path / "runtime", workspace)

    expected_action = "identical" if selected_mode == historical_mode else overlay
    assert plan.overlays[0].action == expected_action
    expected_mode = selected_mode if overlay == "replace" else historical_mode
    assert historical.stat().st_mode & 0o777 == expected_mode
    assert historical.read_bytes() == b"exit 0\n"


def test_materialize_refuses_project_symlink_target_before_writing(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("replacement")
    identifier = capture(store, source, selection("AGENTS.md", layer="project", overlay="replace"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").symlink_to(source / "AGENTS.md")
    with pytest.raises(PathError, match="symlink"):
        materialize_profile(store, identifier, tmp_path / "runtime", workspace)
    assert (source / "AGENTS.md").read_text() == "replacement"
    assert (tmp_path / "runtime").exists() is False


def test_explicit_launcher_argv_and_missing_reference(store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = recipe(
        launcher=(sys.executable, "wrapper.py", "--native"),
        arguments=("--sandbox", "workspace-write"),
        environment=(EnvironmentReference(name="AUTH_REFERENCE"),),
        disabled_skill_paths=("/synthetic/skill/SKILL.md",),
    )
    identifier = capture_profile(store, CaptureSpec(recipe=native))
    plan = preflight_profile(store, identifier, tmp_path / "runtime", workspace)
    assert plan.argv[:5] == (
        sys.executable,
        "wrapper.py",
        "--native",
        "--sandbox",
        "workspace-write",
    )
    assert 'path="/synthetic/skill/SKILL.md",enabled=false' in plan.argv[-1]
    assert "launcher-effects" in {issue.code for issue in plan.issues}
    with pytest.raises(InputError, match="AUTH_REFERENCE"):
        launch_environment(plan, {})
    assert plan.launcher_executable == sys.executable


@pytest.mark.parametrize("on_path", [False, True])
@pytest.mark.parametrize("use_launcher", [False, True])
def test_relative_executables_work_from_launch_workspace(
    store, tmp_path: Path, monkeypatch, on_path: bool, use_launcher: bool
) -> None:
    executable = tmp_path / "bin/native-fixture"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.153.4\\n'\n")
    executable.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin")
    requested = executable.name if on_path else "bin/native-fixture"
    native = NativeRecipe(
        agent=AgentKind.CODEX,
        executable=requested,
        launcher=(requested,) if use_launcher else (),
        version="0.153.4",
    )
    identifier = capture_profile(store, CaptureSpec(recipe=native))

    plan = preflight_profile(store, identifier, tmp_path / "runtime", workspace, probe_version=True)

    assert plan.launchable is True
    assert plan.argv[0] == str(executable)
    assert plan.executable.requested == requested
    assert plan.executable.resolved == str(executable)
    assert plan.executable.version_status == "matched"
    assert plan.launcher_executable == (str(executable) if use_launcher else None)
    result = run_command(
        CommandSpec(argv=plan.argv, timeout_seconds=10, max_output_bytes=4096), Path(plan.cwd)
    )
    assert result.outcome == "exited"
    assert result.returncode == 0
    assert result.stdout == b"codex-cli 0.153.4\n"


def test_isolated_home_is_explicit_and_materialized(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "skill.md").write_text("Selected skill")
    asset = selection(
        "skill.md", kind="skill", target_root="home", target=".agents/skills/selected/SKILL.md"
    )
    identifier = capture(store, source, asset, native=recipe(home_policy="isolated"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = materialize_profile(store, identifier, tmp_path / "runtime", workspace)
    assert plan.generated_environment["HOME"] == str(tmp_path / "runtime/home")
    assert (
        tmp_path / "runtime/home/.agents/skills/selected/SKILL.md"
    ).read_text() == "Selected skill"
    assert (
        next(
            root for root in plan.discovery_roots if root.path.endswith("home/.agents/skills")
        ).status
        == "frozen"
    )
    assert "home-relocated" in {issue.code for issue in plan.issues}
    native_id = capture(store, source, asset)
    with pytest.raises(InputError, match="native home is read-only"):
        materialize_profile(store, native_id, tmp_path / "native-runtime", workspace)


def test_claude_native_settings_skills_and_ancestor_exclusion(store, tmp_path: Path) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "CLAUDE.md").write_text("Selected Claude instructions")
    (source / "plugin").mkdir()
    (source / "plugin/resource.txt").write_text("frozen plugin bytes")
    native = NativeRecipe(
        agent=AgentKind.CLAUDE,
        executable=sys.executable,
        version="2.1.263",
        model="subject",
        effort="high",
    )
    identifier = capture(
        store,
        source,
        selection("CLAUDE.md"),
        selection("plugin", kind="plugin", target="plugins/selected"),
        native=native,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = materialize_profile(store, identifier, tmp_path / "runtime", workspace)
    assert plan.argv[:7] == (
        sys.executable,
        "--setting-sources",
        "user,project,local",
        "--model",
        "subject",
        "--effort",
        "high",
    )
    assert plan.argv[-2:] == ("--plugin-dir", str(tmp_path / "runtime/config/plugins/selected"))
    overrides = json.loads(plan.generated_files["overrides.json"])
    assert str(tmp_path / "CLAUDE.md") in overrides["claudeMdExcludes"]
    assert overrides["skillOverrides"] == {
        "dryheave-collect": "off",
        "dryheave-case": "off",
        "dryheave-results": "off",
    }
    assert plan.generated_environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "runtime/config")
    assert plan.generated_environment["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_version_preflight_is_bounded_and_reports_mismatch(
    store, tmp_path: Path, monkeypatch
) -> None:
    import dryheave.profile_launch as module
    from dryheave.processes import CommandResult

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    def fake_run(command, root):
        calls.append(command)
        return CommandResult(command.argv, 0, b"codex-cli 0.153.4\n", b"", "exited")

    monkeypatch.setattr(module, "run_command", fake_run)
    identifier = capture_profile(store, CaptureSpec(recipe=recipe()))
    plan = preflight_profile(store, identifier, tmp_path / "runtime", workspace, probe_version=True)
    assert plan.executable.version_status == "matched"
    assert calls[0].argv == (sys.executable, "--version")
    assert calls[0].timeout_seconds == 10
    assert calls[0].max_output_bytes == 4096
    other = capture_profile(
        store,
        CaptureSpec(
            recipe=NativeRecipe(agent=AgentKind.CODEX, executable=sys.executable, version="0.1.0")
        ),
    )
    mismatched = preflight_profile(store, other, tmp_path / "other", workspace, probe_version=True)
    assert mismatched.launchable is False
    assert mismatched.executable.version_status == "mismatch"


def test_missing_executable_is_visible_and_no_version_probe_is_implicit(
    store, tmp_path: Path, monkeypatch
) -> None:
    import dryheave.profile_launch as module

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def forbidden(*args, **kwargs):
        pytest.fail("Preflight cannot launch a process without explicit version probe")

    monkeypatch.setattr(module, "run_command", forbidden)
    identifier = capture_profile(
        store,
        CaptureSpec(
            recipe=NativeRecipe(
                agent=AgentKind.CODEX, executable="/missing/native/subject", version="0.153.4"
            )
        ),
    )
    plan = preflight_profile(store, identifier, tmp_path / "runtime", workspace)
    assert plan.launchable is False
    assert plan.executable.resolved is None
    assert "executable-missing" in {item.code for item in plan.issues}


def test_captured_skill_disables_original_source_path_not_derived_copy(
    store, tmp_path: Path, monkeypatch
) -> None:
    from dryheave.profile_models import DeriveSpec
    from dryheave.profiles import derive_profile

    home = tmp_path / "native-home"
    source = home / ".agents/skills/original"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Original skill")
    monkeypatch.setenv("HOME", str(home))
    identifier = capture(
        store, source, selection("SKILL.md", target="skills/original/SKILL.md", kind="skill")
    )
    changed = derive_profile(store, identifier, DeriveSpec(recipe_changes={"model": "variant"}))
    (source / "SKILL.md").write_text("Ambient edit after freezing")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = materialize_profile(store, changed, tmp_path / "runtime", workspace)
    rule = "path=" + json.dumps(str(source / "SKILL.md")) + ",enabled=false"
    assert rule in plan.argv[-1]
    frozen = tmp_path / "runtime/config/skills/original/SKILL.md"
    assert frozen.read_text() == "Original skill"
    disabled = [root.path for root in plan.discovery_roots if root.status == "disabled"]
    assert str(source / "SKILL.md") in disabled
    assert all(path != str(frozen) for path in disabled)
