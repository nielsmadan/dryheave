import io
import json
import sys

import pytest

from dryheave.cli import main
from dryheave.workspaces import CONFIG_FILE


class Terminal(io.StringIO):
    def isatty(self):
        return True


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
    prompt = io.StringIO()
    monkeypatch.setattr(sys, "stdin", Terminal("2\n"))
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stderr", prompt)
    assert main(["init", str(tmp_path / "ws"), "--runtime-root", str(short_runtime)]) == 0
    assert "2. auto" in prompt.getvalue()
    assert 'commit = "auto"' in (tmp_path / "ws" / CONFIG_FILE).read_text()


@pytest.mark.integration
def test_the_flag_skips_the_prompt_even_on_terminals(
    tmp_path, short_runtime, monkeypatch, git_identity
):
    prompt = io.StringIO()
    monkeypatch.setattr(sys, "stdin", Terminal("3\n"))
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stderr", prompt)
    args = ["init", str(tmp_path / "ws"), "--runtime-root", str(short_runtime)]
    assert main([*args, "--git-commit", "auto"]) == 0
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
