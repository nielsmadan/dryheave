import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from conftest import Terminal, fake_commands
from dryheave.cli import main
from dryheave.processes import CommandResult
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
    assert data["git"] == {
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
