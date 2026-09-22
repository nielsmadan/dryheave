import json
from pathlib import Path

import pytest

from conftest import survey_commit as commit
from conftest import survey_repo as make_repo
from conftest import write_baseline_log as write_codex_log
from dryheave.cli import main

pytestmark = pytest.mark.integration


def run(arguments: list[str], tmp_path: Path, capsys) -> tuple[int, dict]:
    code = main(["--store", str(tmp_path / "store"), "--json", *arguments])
    captured = capsys.readouterr()
    return code, json.loads(captured.out if code == 0 else captured.err)


def test_survey_command_reports_tier_signal_bounds_and_session_join(tmp_path, capsys) -> None:
    git = make_repo(tmp_path, "source")
    parent = commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    target = commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "matching.jsonl", parent, str(git.root), "session-one")
    code, response = run(
        [
            "collect",
            "survey",
            "--repo",
            str(git.root),
            "--root",
            str(logs),
            "--agent",
            "codex",
            "--max-commits",
            "10",
            "--max-candidates",
            "3",
        ],
        tmp_path,
        capsys,
    )
    assert code == 0
    data = response["data"]
    survey = data["survey"]
    assert data["tier"] == 2
    assert "The newest 10 commits" in data["bounds"]
    assert survey["head"] == target
    assert survey["sessions_scanned"] == 1
    candidate = survey["candidates"][0]
    assert (candidate["sha"], candidate["parent"]) == (target, parent)
    assert candidate["join"] == "unique"
    assert candidate["sessions"][0]["cwd"] == str(git.root)
    assert candidate["signal"].startswith("Bug-indicating words")


def test_commit_command_resolves_the_baseline_and_hands_over_to_problem_request(
    tmp_path, capsys
) -> None:
    git = make_repo(tmp_path, "source")
    parent = commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    target = commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "matching.jsonl", parent, str(git.root), "session-one")
    code, response = run(
        [
            "collect",
            "commit",
            target[:10],
            "--repo",
            str(git.root),
            "--root",
            str(logs),
            "--agent",
            "codex",
        ],
        tmp_path,
        capsys,
    )
    assert code == 0
    data = response["data"]
    assert (data["commit"], data["baseline"], data["join"]) == (target, parent, "unique")
    assert data["resolution"]["sessions"][0]["path"] == str(logs / "matching.jsonl")
    assert "collect select" in data["next"]
    assert "problem request" in data["next"]
    assert parent in data["next"]
    assert "today's HEAD is never a substitute" in data["next"]


def test_commit_command_reports_an_unjoined_commit_without_a_session(tmp_path, capsys) -> None:
    git = make_repo(tmp_path, "source")
    commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    target = commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    code, response = run(
        [
            "collect",
            "commit",
            target,
            "--repo",
            str(git.root),
            "--root",
            str(logs),
            "--agent",
            "codex",
        ],
        tmp_path,
        capsys,
    )
    assert code == 0
    data = response["data"]
    assert data["join"] == "none"
    assert data["resolution"]["sessions"] == []
    assert "No session was joined" in data["next"]


def test_survey_command_refuses_a_log_root_without_a_parser(tmp_path, capsys) -> None:
    git = make_repo(tmp_path, "source")
    commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    code, response = run(
        ["collect", "survey", "--repo", str(git.root), "--root", str(logs)], tmp_path, capsys
    )
    assert code == 2
    assert "requires --agent codex or --agent claude" in response["error"]["message"]
    code, response = run(
        ["collect", "survey", "--repo", str(git.root), "--agent", "codex"], tmp_path, capsys
    )
    assert code == 2
    assert "supply the log root as well" in response["error"]["message"]


def test_survey_command_refuses_history_outside_its_bound(tmp_path, capsys) -> None:
    git = make_repo(tmp_path, "source")
    commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    code, response = run(
        ["collect", "survey", "--repo", str(git.root), "--max-commits", "5000"], tmp_path, capsys
    )
    assert code == 2
    assert response["error"]["code"] == "limit_exceeded"
    assert "1-2000 commits" in response["error"]["message"]
