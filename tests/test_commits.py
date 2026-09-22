from pathlib import Path

import pytest

from conftest import survey_commit as commit
from conftest import survey_repo as make_repo
from conftest import write_baseline_log as write_codex_log
from dryheave.commits import MAX_HISTORY_LIMIT, resolve_commit, scan_sessions, survey_repository
from dryheave.errors import InputError, LimitError
from dryheave.logs.base import ImportLimits
from dryheave.models import AgentKind

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conventional(tmp_path_factory) -> Path:
    git = make_repo(tmp_path_factory.mktemp("conventional"), "source")
    for index in range(8):
        commit(git, f"feat: add feature {index}", {f"feature_{index}.py": f"value = {index}\n"})
        commit(git, f"chore: tidy module {index}", {f"feature_{index}.py": f"value = {index}0\n"})
        commit(git, f"fix: correct rounding {index}", {f"round_{index}.py": f"total = {index}\n"})
    return git.root


@pytest.fixture(scope="module")
def unconventional(tmp_path_factory) -> Path:
    git = make_repo(tmp_path_factory.mktemp("unconventional"), "source")
    for index in range(11):
        commit(git, f"Adds another widget {index}", {f"widget_{index}.py": f"value = {index}\n"})
    commit(git, "Rounding is broken for empty totals", {"round.py": "total = 0\n"})
    commit(git, "Tidy the launcher", {"launcher.py": "start = 1\n"}, body="The crash is gone now.")
    for index in range(11):
        commit(git, f"Extends the report {index}", {f"report_{index}.py": f"value = {index}\n"})
    return git.root


@pytest.fixture(scope="module")
def neutral(tmp_path_factory) -> Path:
    git = make_repo(tmp_path_factory.mktemp("neutral"), "source")
    for index in range(22):
        files = {f"module_{index}_{part}.py": f"value = {part}\n" for part in range(index % 7 + 1)}
        commit(git, f"Update module {index}", files)
    commit(git, "Tune one constant", {"constant.py": "limit = 4\n"})
    return git.root


@pytest.fixture(scope="module")
def ticketed(tmp_path_factory) -> Path:
    git = make_repo(tmp_path_factory.mktemp("ticketed"), "source")
    for index in range(100, 122):
        commit(git, f"PROJ-{index}: adjust module {index}", {f"module_{index}.py": "value = 1\n"})
    commit(git, "PROJ-900: rounding is broken for empty totals", {"round.py": "total = 0\n"})
    return git.root


def test_survey_learns_the_repository_prefix_vocabulary_and_selects_bug_prefixes(
    conventional: Path,
) -> None:
    survey = survey_repository(conventional, history_limit=30, candidate_limit=4)
    convention = survey.convention
    assert convention.detected is True
    assert convention.prefixed == convention.sampled == 24
    assert convention.prefixed_share == 1.0
    assert sorted(
        (item.token, item.commits, item.bug_indicating) for item in convention.vocabulary
    ) == [("chore", 8, False), ("feat", 8, False), ("fix", 8, True)]
    assert convention.vocabulary_coverage == 1.0
    assert survey.tier == 1
    assert [item.subject for item in survey.candidates] == [
        f"fix: correct rounding {index}" for index in (7, 6, 5, 4)
    ]
    assert all(item.tier == 1 for item in survey.candidates)
    assert "'fix'" in survey.candidates[0].signal


def test_survey_matches_bug_words_when_no_convention_is_detected(unconventional: Path) -> None:
    survey = survey_repository(unconventional, history_limit=30, candidate_limit=4)
    assert survey.convention.detected is False
    assert survey.convention.prefixed == 0
    assert "prefixed share is below" in survey.convention.reason
    assert survey.tier == 2
    assert [item.subject for item in survey.candidates] == [
        "Tidy the launcher",
        "Rounding is broken for empty totals",
    ]
    assert "crash" in survey.candidates[0].signal
    assert "broken" in survey.candidates[1].signal


def test_survey_ranks_by_change_shape_when_no_bug_evidence_exists(neutral: Path) -> None:
    survey = survey_repository(neutral, history_limit=30, candidate_limit=3)
    assert survey.convention.detected is False
    assert survey.tier == 3
    assert "no evidence that any candidate is a bug fix" in survey.tier_note
    assert [item.files_changed for item in survey.candidates] == [1, 1, 1]
    assert [item.subject for item in survey.candidates] == [
        "Tune one constant",
        "Update module 21",
        "Update module 14",
    ]
    assert "Change shape only" in survey.candidates[0].signal


def test_survey_detects_a_numbered_ticket_convention_without_bug_vocabulary(
    ticketed: Path,
) -> None:
    survey = survey_repository(ticketed, history_limit=30, candidate_limit=3)
    assert survey.convention.detected is True
    assert [item.token for item in survey.convention.vocabulary] == ["proj-#"]
    assert survey.convention.vocabulary[0].bug_indicating is False
    assert survey.tier == 2
    assert [item.subject for item in survey.candidates] == [
        "PROJ-900: rounding is broken for empty totals"
    ]


def test_survey_excludes_merge_and_documentation_only_commits(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    commit(git, "fix: correct the guide wording", {"docs/guide.md": "Corrected.\n"})
    commit(git, "chore: pin the settings", {"settings.toml": "value = 1\n"})
    main = commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    git.run("checkout", "--quiet", "-b", "side", f"{main}~1")
    commit(git, "Add a side note", {"side.py": "value = 3\n"})
    git.run("checkout", "--quiet", "main")
    git.run("merge", "--quiet", "--no-ff", "-m", "Merge side", "side")
    survey = survey_repository(git.root, history_limit=30, candidate_limit=5)
    assert survey.merges_excluded == 1
    assert survey.change_class_excluded == 2
    assert {(item.subject, item.reason) for item in survey.excluded} == {
        ("Merge side", "merge"),
        ("fix: correct the guide wording", "documentation"),
        ("chore: pin the settings", "configuration"),
    }
    assert survey.tier == 2
    assert [item.subject for item in survey.candidates] == ["fix: correct empty totals"]


def test_survey_joins_a_candidate_to_the_session_recording_its_parent(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    parent = commit(git, "Initial broken tool", {"tool.py": "value = 1\n"})
    commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "matching.jsonl", parent, str(git.root), "session-one")
    write_codex_log(logs / "other.jsonl", "0" * 40, "/elsewhere", "session-two")
    scan = scan_sessions(logs, AgentKind.CODEX)
    assert (scan.sessions_with_baseline, scan.skipped_count) == (2, 0)
    survey = survey_repository(git.root, history_limit=10, candidate_limit=5, scan=scan)
    candidate = survey.candidates[0]
    assert candidate.subject == "fix: correct empty totals"
    assert candidate.parent == parent
    assert candidate.join == "unique"
    assert "first task, not every commit it produced" in candidate.join_note
    assert [(item.source_id, item.cwd, item.repository_match) for item in candidate.sessions] == [
        ("session-one", str(git.root), "same")
    ]
    assert survey.candidates[1].join == "no_parent"
    assert "no parent" in survey.candidates[1].join_note


def test_survey_reports_no_matching_session_and_an_ambiguous_match(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    parent = commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    commit(git, "fix: correct the totals again", {"tool.py": "value = 3\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "first.jsonl", parent, str(git.root), "session-one")
    write_codex_log(logs / "second.jsonl", parent, "/other/checkout", "session-two", "session-one")
    survey = survey_repository(
        git.root, history_limit=10, candidate_limit=5, scan=scan_sessions(logs, AgentKind.CODEX)
    )
    latest, first = survey.candidates[0], survey.candidates[1]
    assert latest.subject == "fix: correct the totals again"
    assert (latest.join, latest.sessions) == ("none", ())
    assert "may be hand-written" in latest.join_note
    assert first.join == "ambiguous"
    assert "2 scanned sessions record this parent" in first.join_note
    assert [item.repository_match for item in first.sessions] == ["same", "different"]
    assert [item.parent_session_id for item in first.sessions] == [None, "session-one"]
    assert "parent_session_id" in first.join_note


def test_survey_without_a_log_root_reports_that_no_join_was_attempted(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    survey = survey_repository(git.root, history_limit=10, candidate_limit=5)
    assert survey.sessions_scanned is None
    assert survey.candidates[0].join == "not_scanned"
    assert survey.candidates[0].sessions == ()


def test_scan_reports_sessions_it_could_not_read_instead_of_failing(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "readable.jsonl", "a" * 40, "/repo", "session-one")
    (logs / "oversized.jsonl").write_text("{}\n" * 8)
    scan = scan_sessions(logs, AgentKind.CODEX, ImportLimits(max_events=4))
    assert [item.source_id for item in scan.sessions] == ["session-one"]
    assert scan.skipped_count == 1
    assert Path(scan.skipped[0].path).name == "oversized.jsonl"
    assert "event limit" in scan.skipped[0].reason


def test_scan_reports_an_absent_baseline_rather_than_inferring_one(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "no-baseline.jsonl", None, "/repo", "session-one")
    scan = scan_sessions(logs, AgentKind.CODEX)
    assert scan.sessions[0].baseline_commit is None
    assert scan.sessions[0].cwd == "/repo"
    assert scan.sessions_with_baseline == 0


def test_resolve_commit_reports_its_parent_and_matching_session(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    parent = commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    target = commit(git, "fix: correct empty totals", {"tool.py": "value = 2\n"})
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "matching.jsonl", parent, str(git.root), "session-one")
    resolution = resolve_commit(git.root, target[:8], scan=scan_sessions(logs, AgentKind.CODEX))
    assert resolution.commit == target
    assert resolution.parent == parent
    assert resolution.subject == "fix: correct empty totals"
    assert resolution.join == "unique"
    assert [item.source_id for item in resolution.sessions] == ["session-one"]


def test_resolve_commit_refuses_merges_and_nonexplicit_references(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    base = commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    commit(git, "Extend the tool", {"tool.py": "value = 2\n"})
    git.run("checkout", "--quiet", "-b", "side", base)
    commit(git, "Add a side note", {"side.py": "value = 3\n"})
    git.run("checkout", "--quiet", "main")
    git.run("merge", "--quiet", "--no-ff", "-m", "Merge side", "side")
    merge = git.run("rev-parse", "HEAD").stdout.decode().strip()
    with pytest.raises(InputError, match="Merge commits record no single starting baseline"):
        resolve_commit(git.root, merge)
    with pytest.raises(InputError, match="branches and HEAD are not accepted"):
        resolve_commit(git.root, "HEAD")
    with pytest.raises(InputError, match="branches and HEAD are not accepted"):
        resolve_commit(git.root, "main")


def test_survey_rejects_unbounded_history_and_a_missing_repository(tmp_path: Path) -> None:
    git = make_repo(tmp_path, "source")
    commit(git, "Start the tool", {"tool.py": "value = 1\n"})
    with pytest.raises(LimitError, match=f"1-{MAX_HISTORY_LIMIT} commits"):
        survey_repository(git.root, history_limit=MAX_HISTORY_LIMIT + 1)
    with pytest.raises(LimitError, match="1-50"):
        survey_repository(git.root, candidate_limit=0)
    with pytest.raises(InputError, match="must be a real directory"):
        survey_repository(tmp_path / "absent")
