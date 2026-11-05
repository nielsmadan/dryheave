import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import Terminal, fake_commands
from dryheave.cli import main
from dryheave.processes import CommandResult
from dryheave.skills import OWNER_FILE, SKILL_NAMES, bundled_skill
from dryheave.workspace_cli import transport_action
from dryheave.workspaces import CONFIG_FILE

FOUND = shutil.which
RELEASES = json.dumps(
    [
        {"tag_name": "0.2.0", "draft": False, "prerelease": False},
        {"tag_name": "0.1.3", "draft": False, "prerelease": False},
        {"tag_name": "0.1.1", "draft": False, "prerelease": False},
    ]
).encode()


def init(capsys, path, *arguments):
    code = main(["init", str(path), *arguments, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)["data"]


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["ask", "auto", "off"])
def test_init_sets_up_the_repository_gitignore_and_chosen_mode(
    tmp_path, short_runtime, capsys, git_identity, mode
):
    data = init(capsys, tmp_path / "ws", "--runtime-root", str(short_runtime), "--git-commit", mode)
    root = tmp_path / "ws"
    assert (root / ".git").is_dir()
    assert {key: data["git"][key] for key in ("repository", "gitignore", "git_commit")} == {
        "repository": "created",
        "gitignore": str(root / ".gitignore"),
        "git_commit": mode,
    }
    assert f'[git]\ncommit = "{mode}"\n' in (root / CONFIG_FILE).read_text()
    assert ".dryheave/" in (root / ".gitignore").read_text().splitlines()


@pytest.mark.integration
def test_init_without_a_terminal_or_flag_uses_ask(tmp_path, short_runtime, capsys, git_identity):
    data = init(capsys, tmp_path / "ws", "--runtime-root", str(short_runtime))
    assert data["git"]["git_commit"] == "ask"


@pytest.mark.integration
def test_init_prompts_on_two_terminals_and_uses_the_numbered_answer(
    tmp_path, short_runtime, monkeypatch, git_identity
):
    prompt, _ = on_terminal(monkeypatch, "2\n")
    args = ["init", str(tmp_path / "ws"), "--runtime-root", str(short_runtime)]
    assert main([*args, "--tui-test", "/opt/tui-test"]) == 0
    assert "2. auto" in prompt.getvalue()
    assert 'commit = "auto"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


@pytest.mark.integration
def test_the_flag_skips_the_prompt_even_on_terminals(
    tmp_path, short_runtime, monkeypatch, git_identity
):
    prompt, _ = on_terminal(monkeypatch, "3\n")
    args = ["init", str(tmp_path / "ws"), "--runtime-root", str(short_runtime)]
    assert main([*args, "--git-commit", "auto", "--tui-test", "/opt/tui-test"]) == 0
    assert prompt.getvalue() == ""
    assert 'commit = "auto"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


def test_init_without_git_fails_before_writing_anything(
    tmp_path, short_runtime, monkeypatch, capsys
):
    monkeypatch.setenv("PATH", "")
    code = main(["init", str(tmp_path / "ws"), "--runtime-root", str(short_runtime), "--json"])
    assert code == 2
    assert "git is not installed" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert not (tmp_path / "ws").exists()


def test_init_with_commits_off_works_without_git(tmp_path, short_runtime, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")
    data = init(
        capsys, tmp_path / "ws", "--runtime-root", str(short_runtime), "--git-commit", "off"
    )
    assert data["git"]["repository"] == "skipped"
    assert not (tmp_path / "ws" / ".git").exists()
    assert 'commit = "off"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


@pytest.mark.integration
def test_init_on_an_existing_workspace_adds_the_repository_and_mode_only(
    tmp_path, short_runtime, capsys, git_identity, monkeypatch
):
    root = tmp_path / "ws"
    monkeypatch.setenv("PATH", "")
    init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "off")
    monkeypatch.undo()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    store_marker = root / ".dryheave/store/format.json"
    before = store_marker.read_bytes()
    assert not (root / ".git").exists()
    data = init(capsys, root, "--git-commit", "auto")
    assert data["git"]["repository"] == "created"
    assert (root / ".git").is_dir()
    assert 'commit = "auto"' in (root / CONFIG_FILE).read_text()
    assert store_marker.read_bytes() == before
    again = init(capsys, root)
    assert again["git"]["repository"] == "existing"
    assert again["git"]["git_commit"] == "auto"


def git_output(root, *argv):
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_init_installs_the_skills_and_commits_the_workspace(
    tmp_path, short_runtime, capsys, git_identity, mode
):
    root = tmp_path / "ws"
    data = init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", mode)
    installed = {"changed": list(SKILL_NAMES), "conflicts": [], "error": None}
    assert data["skill_installs"] == [
        {"target": str(root / ".agents/skills")} | installed,
        {"target": str(root / ".claude/skills")} | installed,
    ]
    assert data["commit"]["state"] == "committed"
    assert data["message"] == "workspace: initialize"
    assert git_output(root, "log", "--format=%H %s") == (
        f"{data['commit']['commit']} workspace: initialize\n"
    )
    committed = git_output(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert {".gitignore", CONFIG_FILE} < set(committed)
    assert ".agents/skills/dryheave-voice-profile/SKILL.md" in committed
    assert ".claude/skills/dryheave-voice-profile/SKILL.md" in committed
    assert git_output(root, "status", "--porcelain") == ""


@pytest.mark.integration
def test_init_with_commits_off_installs_the_skills_but_commits_nothing(
    tmp_path, short_runtime, capsys, git_identity
):
    root = tmp_path / "ws"
    data = init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "off")
    assert data["git_commit"] == "off"
    assert "commit" not in data
    assert (root / ".claude/skills/dryheave-voice-profile/SKILL.md").is_file()
    status = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, check=False)
    assert status.returncode != 0


@pytest.mark.integration
def test_rerunning_init_keeps_the_history_and_current_skills(
    tmp_path, short_runtime, capsys, git_identity
):
    root = tmp_path / "ws"
    init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "ask")
    skill = root / ".agents/skills/dryheave-voice-profile/SKILL.md"
    written = skill.stat().st_mtime_ns
    again = init(capsys, root)
    assert again["message"] == "workspace: update"
    assert again["commit_paths"] == []
    assert [item["changed"] for item in again["skill_installs"]] == [[], []]
    assert skill.stat().st_mtime_ns == written
    assert len(git_output(root, "log", "--format=%H").splitlines()) == 1


def age_skill(skill):
    (skill / "SKILL.md").write_text("Old packaged instructions.\n")
    owner = json.loads((skill / OWNER_FILE).read_text())
    owner["files"]["SKILL.md"] = hashlib.sha256(b"Old packaged instructions.\n").hexdigest()
    (skill / OWNER_FILE).write_text(json.dumps(owner))


@pytest.mark.integration
def test_rerunning_init_in_auto_mode_commits_the_skills_it_updated(
    tmp_path, short_runtime, capsys, git_identity
):
    root = tmp_path / "ws"
    init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "auto")
    age_skill(root / ".agents/skills/dryheave-case")
    git_output(root, "commit", "-qam", "older dryheave")
    again = init(capsys, root)
    assert again["commit_paths"] == [".agents/skills/dryheave-case"]
    assert again["commit"]["state"] == "committed"
    assert git_output(root, "log", "-1", "--format=%s") == "workspace: update\n"
    assert git_output(root, "status", "--porcelain") == ""


@pytest.mark.integration
def test_init_reports_a_skill_it_cannot_install_and_still_commits_the_rest(
    tmp_path, short_runtime, capsys, git_identity
):
    root = tmp_path / "ws"
    foreign = root / ".claude/skills/dryheave-collect"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("mine\n")
    data = init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "ask")
    agents, claude = data["skill_installs"]
    assert agents["conflicts"] == []
    assert [item["name"] for item in claude["conflicts"]] == ["dryheave-collect"]
    assert "Foreign skill directory" in claude["conflicts"][0]["reason"]
    assert (foreign / "SKILL.md").read_text() == "mine\n"
    committed = git_output(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".agents/skills/dryheave-collect/SKILL.md" in committed
    assert ".claude/skills/dryheave-case/SKILL.md" in committed
    assert ".claude/skills/dryheave-collect/SKILL.md" not in committed


@pytest.mark.integration
def test_init_reports_a_skill_folder_it_cannot_use_and_commits_the_other(
    tmp_path, short_runtime, capsys, git_identity
):
    root = tmp_path / "ws"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude/skills").write_text("not a folder\n")
    data = init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "ask")
    agents, claude = data["skill_installs"]
    assert agents["changed"] == list(SKILL_NAMES)
    assert claude["error"]
    assert claude["changed"] == []
    committed = git_output(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".agents/skills/dryheave-case/SKILL.md" in committed
    assert (root / ".claude/skills").read_text() == "not a folder\n"


@pytest.mark.integration
def test_init_leaves_a_skill_folder_git_ignores_out_of_the_first_commit(
    tmp_path, short_runtime, capsys, git_identity
):
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".gitignore").write_text(".claude/\n")
    data = init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "ask")
    assert data["commit"]["state"] == "committed"
    assert data["ignored_paths"] == [f".claude/skills/{name}" for name in SKILL_NAMES]
    committed = git_output(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".agents/skills/dryheave-case/SKILL.md" in committed


def update(capsys, *arguments):
    code = main(["update", *arguments])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return captured.out


@pytest.mark.integration
def test_update_refreshes_outdated_skills_and_gitignore_and_lists_them_to_commit(
    tmp_path, short_runtime, capsys, git_identity, monkeypatch
):
    root = tmp_path / "ws"
    workspace_data = init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "ask")
    expected = (root / ".gitignore").read_text().splitlines()
    age_skill(root / ".agents/skills/dryheave-collect")
    (root / ".gitignore").write_text(".dryheave/\n")
    git_output(root, "commit", "-qam", "older dryheave")
    monkeypatch.chdir(root / ".agents")
    data = json.loads(update(capsys, "--json"))["data"]
    assert data["updated"] is True
    assert data["path"] == workspace_data["path"]
    assert [".dryheave/", *data["gitignore_added"]] == expected
    assert [item["changed"] for item in data["skill_installs"]] == [["dryheave-collect"], []]
    assert data["commit_paths"] == [".gitignore", ".agents/skills/dryheave-collect"]
    assert data["message"] == "workspace: update"
    assert "commit" not in data
    skill = root / ".agents/skills/dryheave-collect/SKILL.md"
    assert skill.read_bytes() == bundled_skill("dryheave-collect")
    assert len(git_output(root, "log", "--format=%H").splitlines()) == 2
    again = json.loads(update(capsys, "--json"))["data"]
    assert again["updated"] is False
    assert again["commit_paths"] == []


@pytest.mark.integration
def test_update_in_auto_mode_commits_only_what_it_changed_and_leaves_edits_alone(
    tmp_path, short_runtime, capsys, git_identity, monkeypatch
):
    root = tmp_path / "ws"
    init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "auto")
    age_skill(root / ".claude/skills/dryheave-case")
    edited = root / ".agents/skills/dryheave-results/SKILL.md"
    edited.write_text("my notes\n")
    git_output(root, "commit", "-qam", "older dryheave")
    (root / "notes.txt").write_text("mine\n")
    monkeypatch.chdir(root)
    lines = update(capsys).splitlines()
    sha = git_output(root, "rev-parse", "--short=7", "HEAD").strip()
    assert lines[:3] == [
        f"Workspace partly updated: {root.resolve()}",
        "  skills    .agents/skills: current",
        "            .claude/skills: updated dryheave-case",
    ]
    assert lines[3].startswith("            dryheave-results not updated in .agents/skills: ")
    assert lines[-1] == f"  git       committed {sha} workspace: update"
    assert edited.read_text() == "my notes\n"
    assert git_output(root, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        ".claude/skills/dryheave-case/.dryheave-owned.json",
        ".claude/skills/dryheave-case/SKILL.md",
    ]
    assert git_output(root, "status", "--porcelain") == "?? notes.txt\n"


@pytest.mark.integration
def test_update_in_a_current_workspace_changes_nothing(
    tmp_path, short_runtime, capsys, git_identity, monkeypatch
):
    root = tmp_path / "ws"
    init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "auto")
    monkeypatch.chdir(root)
    assert update(capsys) == f"\u2714 Workspace already up to date: {root.resolve()}\n"


@pytest.mark.integration
def test_update_refreshes_one_folder_when_the_configured_skills_are_claude_skills(
    tmp_path, short_runtime, capsys, git_identity, monkeypatch
):
    root = tmp_path / "ws"
    init(capsys, root, "--runtime-root", str(short_runtime), "--git-commit", "ask")
    config = root / CONFIG_FILE
    config.write_text(config.read_text().replace('".agents/skills"', '".claude/skills"'))
    monkeypatch.chdir(root)
    data = json.loads(update(capsys, "--json"))["data"]
    assert [item["target"] for item in data["skill_installs"]] == [str(root / ".claude/skills")]


def test_update_outside_a_workspace_names_init_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["update"]) != 0
    assert "run dryheave init first." in capsys.readouterr().err


@pytest.mark.integration
def test_init_prints_a_summary_and_the_next_steps_without_json(
    tmp_path, short_runtime, capsys, git_identity, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "ws", "--runtime-root", str(short_runtime), "--git-commit", "ask"]) == 0
    out = capsys.readouterr().out
    commit = git_output(tmp_path / "ws", "rev-parse", "--short=7", "HEAD").strip()
    assert out.splitlines()[:3] == [
        f"\u2714 Workspace ready: {(tmp_path / 'ws').resolve()}",
        (
            f"  git       new repository, committed {commit} workspace: initialize"
            " (checkpoint commits: ask)"
        ),
        "  skills    installed in .agents/skills and .claude/skills",
    ]
    assert "  1. cd ws\n" in out
    assert "Claude Code   /dryheave-voice-profile" in out
    assert "Codex         $dryheave-voice-profile" in out


def test_init_accepts_a_directory_that_already_holds_objects_or_runs_folders(
    tmp_path, short_runtime, capsys
):
    target = tmp_path / "ws"
    (target / "objects" / "notes").mkdir(parents=True)
    (target / "runs" / "draft").mkdir(parents=True)
    data = init(capsys, target, "--runtime-root", str(short_runtime), "--git-commit", "off")
    assert data["path"] == str(target.resolve())
    assert (target / CONFIG_FILE).is_file()


def on_path(monkeypatch, found):
    monkeypatch.setattr(
        "shutil.which",
        lambda name, *rest: found() if name == "tui-test" else FOUND(name, *rest),
    )


def without_tui_test(monkeypatch):
    on_path(monkeypatch, lambda: None)


def fake_install(monkeypatch, path, *, install=None, versions=None):
    def respond(command, _root, _options):
        if command.argv[0] == "curl" and "-o" in command.argv:
            Path(command.argv[command.argv.index("-o") + 1]).write_bytes(b"#!/bin/sh\n")
            return CommandResult(command.argv, 0, b"", b"", "exited")
        if command.argv[0] == "curl":
            return CommandResult(command.argv, 0, RELEASES, b"", "exited")
        if command.argv[0] == "sh":
            return install or CommandResult(
                command.argv, 0, f"Installed tui-test to {path}\n".encode(), b"", "exited"
            )
        reported = (versions or {}).get(command.argv[0], b"tui-test 0.1.3\n")
        return CommandResult(command.argv, 0, reported, b"", "exited")

    calls = fake_commands(monkeypatch, "dryheave.transport_setup", respond)
    return calls


def ran(calls):
    return [call.argv[0] for call in calls]


def on_terminal(monkeypatch, answers):
    prompt, stdout = io.StringIO(), Terminal()
    monkeypatch.setattr(sys, "stdin", Terminal(answers))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", prompt)
    return prompt, stdout


def init_args(tmp_path, short_runtime, *arguments):
    return ["init", str(tmp_path / "ws"), "--runtime-root", str(short_runtime), *arguments]


def test_init_stops_before_writing_when_the_user_will_install_tui_test(
    tmp_path, short_runtime, monkeypatch
):
    without_tui_test(monkeypatch)
    calls = fake_install(monkeypatch, tmp_path / "bin" / "tui-test")
    prompt, _ = on_terminal(monkeypatch, "2\n")
    assert main(init_args(tmp_path, short_runtime, "--git-commit", "off")) == 2
    shown = prompt.getvalue()
    assert shown.startswith("Checking GitHub for the newest tui-test release…\n")
    assert "  2. I will install it\n" in shown
    assert "tui-test 0.1.3 is the newest supported release" in shown
    assert "TUI_TEST_VERSION=0.1.3 sh\nthen rerun dryheave init." in shown
    assert ran(calls) == ["curl"]
    assert not (tmp_path / "ws").exists()


def test_init_installs_tui_test_when_asked_and_records_where_it_went(
    tmp_path, short_runtime, monkeypatch
):
    without_tui_test(monkeypatch)
    installed = tmp_path / "bin" / "tui-test"
    calls = fake_install(monkeypatch, installed)
    prompt, _ = on_terminal(monkeypatch, "1\n")
    assert main(init_args(tmp_path, short_runtime, "--git-commit", "off")) == 0
    assert ran(calls) == ["curl", "curl", "sh", str(installed)]
    assert f"Installed tui-test to {installed}" in prompt.getvalue()
    assert f'tui_test = "{installed}"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


def test_init_installs_only_after_every_question_is_answered(tmp_path, short_runtime, monkeypatch):
    without_tui_test(monkeypatch)
    calls = fake_install(monkeypatch, tmp_path / "bin" / "tui-test")
    prompt, _ = on_terminal(monkeypatch, "1\n")
    assert main(init_args(tmp_path, short_runtime)) == 130
    assert "init cancelled; nothing was written" in prompt.getvalue()
    assert ran(calls) == ["curl"]
    assert not (tmp_path / "ws").exists()


def test_cancelling_the_install_question_cancels_init(tmp_path, short_runtime, monkeypatch):
    without_tui_test(monkeypatch)
    calls = fake_install(monkeypatch, tmp_path / "bin" / "tui-test")
    prompt, _ = on_terminal(monkeypatch, "")
    assert main(init_args(tmp_path, short_runtime, "--git-commit", "off")) == 130
    assert "  tui-test: cancelled\n" in prompt.getvalue()
    assert ran(calls) == ["curl"]
    assert not (tmp_path / "ws").exists()


def test_init_without_a_terminal_creates_the_workspace_and_names_the_install(
    tmp_path, short_runtime, monkeypatch, capsys
):
    without_tui_test(monkeypatch)
    calls = fake_install(monkeypatch, tmp_path / "bin" / "tui-test")
    data = init(
        capsys, tmp_path / "ws", "--runtime-root", str(short_runtime), "--git-commit", "off"
    )
    assert data["tui_test"] is None
    assert "run dryheave init on a terminal (or with --install-tui-test)" in data["next"]
    assert "TUI_TEST_VERSION=0.1.1 sh" in data["next"]
    assert calls == []


def test_json_on_a_terminal_neither_asks_nor_looks_up(tmp_path, short_runtime, monkeypatch):
    without_tui_test(monkeypatch)
    calls = fake_install(monkeypatch, tmp_path / "bin" / "tui-test")
    prompt, stdout = on_terminal(monkeypatch, "1\n")
    assert main(init_args(tmp_path, short_runtime, "--git-commit", "off", "--json")) == 0
    assert json.loads(stdout.getvalue())["data"]["tui_test"] is None
    assert prompt.getvalue() == ""
    assert calls == []


def test_the_install_flag_installs_without_a_prompt_and_reports_the_install(
    tmp_path, short_runtime, monkeypatch, capsys
):
    without_tui_test(monkeypatch)
    installed = tmp_path / "bin" / "tui-test"
    calls = fake_install(monkeypatch, installed)
    data = init(
        capsys,
        tmp_path / "ws",
        "--runtime-root",
        str(short_runtime),
        "--git-commit",
        "off",
        "--install-tui-test",
    )
    assert data["tui_test"] == str(installed)
    assert data["installed_tui_test"] == {
        "path": str(installed),
        "version": "0.1.3",
        "lookup": "found",
    }
    assert calls[2].argv[0] == "sh"


def test_the_summary_names_the_tui_test_version_it_installed(
    tmp_path, short_runtime, monkeypatch, capsys
):
    without_tui_test(monkeypatch)
    installed = tmp_path / "bin" / "tui-test"
    fake_install(monkeypatch, installed)
    arguments = ["--runtime-root", str(short_runtime), "--git-commit", "off"]
    assert main(["init", str(tmp_path / "ws"), *arguments, "--install-tui-test"]) == 0
    out = capsys.readouterr().out
    assert f"  tui-test  0.1.3 installed at {installed}" in out.splitlines()


def test_init_keeps_a_supported_tui_test_without_offering_an_install(
    tmp_path, short_runtime, monkeypatch
):
    on_path(monkeypatch, lambda: "/opt/bin/tui-test")
    calls = fake_install(
        monkeypatch,
        tmp_path / "bin" / "tui-test",
        versions={"/opt/bin/tui-test": b"tui-test 0.1.1\n"},
    )
    prompt, _ = on_terminal(monkeypatch, "3\n")
    assert main(init_args(tmp_path, short_runtime)) == 0
    assert [call.argv for call in calls] == [("/opt/bin/tui-test", "--version")]
    assert prompt.getvalue().endswith("\u2714 git commits: off\n")


def test_init_offers_an_install_over_an_unsupported_tui_test(tmp_path, short_runtime, monkeypatch):
    on_path(monkeypatch, lambda: "/opt/bin/tui-test")
    installed = tmp_path / "bin" / "tui-test"
    calls = fake_install(
        monkeypatch, installed, versions={"/opt/bin/tui-test": b"tui-test 0.1.0-beta.3\n"}
    )
    on_terminal(monkeypatch, "1\n")
    assert main(init_args(tmp_path, short_runtime, "--git-commit", "off")) == 0
    assert ran(calls) == ["/opt/bin/tui-test", "curl", "curl", "sh", str(installed)]
    assert f'tui_test = "{installed}"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


def test_an_install_that_lands_on_path_is_not_recorded(
    tmp_path, short_runtime, monkeypatch, capsys
):
    installed = tmp_path / "bin" / "tui-test"
    calls = fake_install(monkeypatch, installed)
    on_path(monkeypatch, lambda: str(installed) if "sh" in ran(calls) else None)
    data = init(
        capsys,
        tmp_path / "ws",
        "--runtime-root",
        str(short_runtime),
        "--git-commit",
        "off",
        "--install-tui-test",
    )
    assert data["tui_test"] == str(installed)
    assert "tui_test" not in (tmp_path / "ws" / CONFIG_FILE).read_text()


def test_a_failed_install_leaves_no_workspace(tmp_path, short_runtime, monkeypatch, capsys):
    without_tui_test(monkeypatch)
    failed = CommandResult(("sh",), 1, b"", b"curl: (22) 404\n", "exited")
    fake_install(monkeypatch, tmp_path / "bin" / "tui-test", install=failed)
    args = init_args(tmp_path, short_runtime, "--git-commit", "off", "--install-tui-test", "--json")
    assert main(args) == 2
    error = json.loads(capsys.readouterr().err.splitlines()[-1])["error"]
    assert error["code"] == "missing_transport"
    assert "The tui-test install did not finish (exited, exit 1)." in error["message"]
    assert not (tmp_path / "ws").exists()


def test_rerunning_init_installs_and_records_tui_test_in_an_existing_workspace(
    tmp_path, short_runtime, monkeypatch, capsys
):
    without_tui_test(monkeypatch)
    installed = tmp_path / "bin" / "tui-test"
    fake_install(monkeypatch, installed)
    init(capsys, tmp_path / "ws", "--runtime-root", str(short_runtime), "--git-commit", "off")
    data = init(capsys, tmp_path / "ws", "--install-tui-test")
    assert data["tui_test"] == str(installed)
    assert f'tui_test = "{installed}"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


def test_workspace_problems_stop_init_before_any_lookup(tmp_path, monkeypatch, capsys):
    without_tui_test(monkeypatch)
    calls = fake_install(monkeypatch, tmp_path / "bin" / "tui-test")
    long_root = tmp_path / ("r" * 80)
    args = ["init", str(tmp_path / "ws"), "--runtime-root", str(long_root), "--install-tui-test"]
    assert main([*args, "--git-commit", "off", "--json"]) == 2
    assert "native limit" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert calls == []


@pytest.mark.parametrize(
    ("explicit", "usable", "install", "interactive", "action"),
    [
        (True, False, True, True, "explicit"),
        (False, True, True, True, "keep"),
        (False, False, True, False, "install"),
        (False, False, False, True, "ask"),
        (False, False, False, False, "skip"),
    ],
)
def test_the_transport_action_prefers_explicit_then_usable_then_install_then_asking(
    explicit, usable, install, interactive, action
):
    assert (
        transport_action(explicit=explicit, usable=usable, install=install, interactive=interactive)
        == action
    )
