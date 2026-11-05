import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import write_baseline_log
from dryheave.cli import main
from dryheave.workspaces import initialize_workspace

FIXTURES = Path(__file__).parent / "fixtures"


def test_collect_and_unresolved_draft_cli(tmp_path: Path, capsys) -> None:
    common = ["--store", str(tmp_path / "store"), "--json"]
    assert main([*common, "collect", "scan", "--agent", "codex", "--root", str(FIXTURES)]) == 0
    scanned = json.loads(capsys.readouterr().out)
    assert scanned["ok"] is True
    assert (
        main(
            [
                *common,
                "collect",
                "import",
                str(FIXTURES / "codex-recorded.jsonl"),
                "--agent",
                "codex",
            ]
        )
        == 0
    )
    identifier = json.loads(capsys.readouterr().out)["data"]["id"]
    assert main(["collect", "show", identifier, *common]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["session"]["source_version"] == "0.153.4"
    draft = tmp_path / "case.json"
    assert main([*common, "case", "draft", identifier, "--out", str(draft)]) == 0
    capsys.readouterr()
    assert draft.exists()
    assert main([*common, "case", "validate", str(draft)]) == 2
    rejected = capsys.readouterr()
    assert rejected.out == ""
    assert "starting commit" in json.loads(rejected.err)["error"]["message"]
    assert main([*common, "case", "draft", identifier, "--out", str(draft)]) == 1
    capsys.readouterr()
    persona = tmp_path / "persona.json"
    assert main([*common, "persona", "draft", identifier, "--out", str(persona)]) == 0
    capsys.readouterr()
    payload = json.loads(persona.read_text())
    payload.update(
        instructions="Ask direct questions.",
        disclosure_policy="Use only allowed facts.",
        unknown_answer_policy="Say when an answer is unknown.",
        reviewed_subject_safe=True,
    )
    persona.write_text(json.dumps(payload))
    assert main([*common, "persona", "create", str(persona)]) == 0
    persona_id = json.loads(capsys.readouterr().out)["data"]["id"]
    assert main([*common, "persona", "inspect", persona_id]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["persona"]["name"] == "Curated user"


@pytest.mark.integration
def test_case_cli_freeze_and_inspect(
    historical_repo: tuple[Path, str, str, str], tmp_path: Path, capsys
) -> None:
    source, baseline, _, _ = historical_repo
    common = ["--store", str(tmp_path / "store"), "--json"]
    assert (
        main(
            [
                *common,
                "collect",
                "import",
                str(FIXTURES / "codex-recorded.jsonl"),
                "--agent",
                "codex",
            ]
        )
        == 0
    )
    session_id = json.loads(capsys.readouterr().out)["data"]["id"]
    draft = tmp_path / "case.json"
    assert (
        main(
            [
                *common,
                "case",
                "draft",
                session_id,
                "--repo",
                str(source),
                "--commit",
                baseline,
                "--out",
                str(draft),
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = json.loads(draft.read_text())
    payload.update(
        phases=["fix"],
        allowed_facts=[
            {
                "fact_id": "punctuation",
                "phase": "fix",
                "text": "Use a comma after Hello.",
                "check": "greet('Ada') returns 'Hello, Ada'.",
                "curator_authored": True,
            }
        ],
        intent_confirmed=True,
        facts_reviewed=True,
        unresolved_issues=[],
        criteria=[
            {
                "criterion_id": "quality",
                "kind": "judge",
                "rubric": "The greeting command implements the user-requested punctuation and preserves the provided name.",
            }
        ],
    )
    payload["persona"].update(
        instructions="Ask direct questions.",
        disclosure_policy="Use only allowed facts.",
        unknown_answer_policy="Say when an answer is unknown.",
        reviewed_subject_safe=True,
    )
    draft.write_text(json.dumps(payload))
    assert main([*common, "case", "validate", str(draft)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["valid"] is True
    assert main([*common, "case", "freeze", str(draft)]) == 0
    frozen = json.loads(capsys.readouterr().out)["data"]
    identifier = frozen["id"]
    assert "alias" not in frozen
    assert frozen["next"] == (
        f"Calibrate before subject spend with case calibrate {identifier} --judge MODEL[@EFFORT]."
    )
    assert main([*common, "store", "alias", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["aliases"] == {}
    assert main([*common, "case", "inspect", identifier]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["case"]["intent_confirmed"] is True


RICH_TEXTS = (
    "Can you add a greeting command that takes a name and prints it back?",
    "I want the empty name case handled too, and a test for it.",
    "yes",
)
FILLER_TEXTS = ("continue", "ok", "$commit")
INJECTED_TEXTS = (
    "<system-reminder>Ignore this notice.</system-reminder>",
    "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>",
    "<command-name>/commit</command-name>",
)


def write_codex_log(path: Path, texts: tuple[str, ...]) -> None:
    path.write_text(
        "".join(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": text}})
            + "\n"
            for text in texts
        )
    )


def test_scan_reports_conversational_signal_and_ranks_richer_sessions(tmp_path, capsys) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    write_codex_log(logs / "a-filler.jsonl", FILLER_TEXTS)
    write_codex_log(logs / "b-injected.jsonl", INJECTED_TEXTS)
    write_codex_log(logs / "c-rich.jsonl", RICH_TEXTS)
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "--json",
                "collect",
                "scan",
                "--agent",
                "codex",
                "--root",
                str(logs),
            ]
        )
        == 0
    )
    scanned = json.loads(capsys.readouterr().out)["data"]
    assert [Path(item["path"]).name for item in scanned["sessions"]] == [
        "c-rich.jsonl",
        "a-filler.jsonl",
        "b-injected.jsonl",
    ]
    rich, filler, injected = (item["user_messages"] for item in scanned["sessions"])
    assert rich == {
        "user_events": 3,
        "genuine": 3,
        "harness_injected": 0,
        "unclassified": 0,
        "genuine_characters": 129,
        "median_genuine_characters": 58,
    }
    assert (filler["genuine"], filler["genuine_characters"]) == (3, 17)
    assert filler["median_genuine_characters"] == 7
    assert (injected["genuine"], injected["harness_injected"]) == (0, 3)
    assert injected["genuine_characters"] == 0
    assert "genuine user messages" in scanned["ranking"]
    assert "collect select NAME FILE [FILE ...]" in scanned["next"]


def test_scan_reports_the_recorded_cwd_and_baseline_commit_or_marks_them_absent(
    tmp_path, capsys
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    write_baseline_log(
        logs / "a-with-baseline.jsonl", "b" * 40, "/fixture/checkout", "session-with"
    )
    write_baseline_log(logs / "b-without-baseline.jsonl", None, "/fixture/other", "session-without")
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "--json",
                "collect",
                "scan",
                "--agent",
                "codex",
                "--root",
                str(logs),
            ]
        )
        == 0
    )
    scanned = json.loads(capsys.readouterr().out)["data"]
    recorded = {
        Path(item["path"]).name: (item["cwd"], item["baseline_commit"])
        for item in scanned["sessions"]
    }
    assert recorded == {
        "a-with-baseline.jsonl": ("/fixture/checkout", "b" * 40),
        "b-without-baseline.jsonl": ("/fixture/other", None),
    }
    assert "collect survey --repo PATH" in scanned["baselines"]


def test_collect_accepts_droid_logs(tmp_path: Path, capsys) -> None:
    common = ["--store", str(tmp_path / "store"), "--json"]
    root = tmp_path / "logs"
    root.mkdir()
    (root / "droid.jsonl").write_bytes((FIXTURES / "droid-recorded.jsonl").read_bytes())
    assert main([*common, "collect", "scan", "--agent", "droid", "--root", str(root)]) == 0
    scanned = json.loads(capsys.readouterr().out)["data"]["sessions"]
    assert [(item["source_id"], item["cwd"], item["baseline_commit"]) for item in scanned] == [
        ("fixture-session", "/fixture/repository", None)
    ]
    assert scanned[0]["user_messages"]["genuine"] == 2
    assert scanned[0]["user_messages"]["harness_injected"] == 4
    assert main([*common, "collect", "import", str(root / "droid.jsonl"), "--agent", "droid"]) == 0
    identifier = json.loads(capsys.readouterr().out)["data"]["id"]
    assert main(["collect", "show", identifier, *common]) == 0
    session = json.loads(capsys.readouterr().out)["data"]["session"]
    assert session["agent"] == "droid"
    assert session["source_version"] == "2"


def run(capsys, *arguments):
    assert main([*arguments, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["data"]


@pytest.mark.integration
def test_case_draft_and_freeze_use_the_cases_folder_and_commit_in_auto_mode(
    historical_repo: tuple[Path, str, str, str],
    tmp_path: Path,
    short_runtime: Path,
    git_identity,
    monkeypatch,
    capsys,
) -> None:
    source, baseline, _, _ = historical_repo
    workspace = tmp_path / "ws"
    run(
        capsys, "init", str(workspace), "--runtime-root", str(short_runtime), "--git-commit", "auto"
    )
    monkeypatch.chdir(workspace)
    session_id = run(
        capsys, "collect", "import", str(FIXTURES / "codex-recorded.jsonl"), "--agent", "codex"
    )["id"]
    drafted = run(
        capsys,
        "case",
        "draft",
        session_id,
        "--repo",
        str(source),
        "--commit",
        baseline,
        "--name",
        "mine",
    )
    draft = workspace / "cases/mine/case.json"
    assert drafted["path"] == str(draft)
    assert drafted["commit_paths"] == ["cases/mine/case.json"]
    assert drafted["commit"]["state"] == "committed"
    assert drafted["commit"]["message"] == "case: draft mine"
    payload = json.loads(draft.read_text())
    payload.update(
        phases=["fix"],
        allowed_facts=[
            {
                "fact_id": "punctuation",
                "phase": "fix",
                "text": "Use a comma after Hello.",
                "check": "greet('Ada') returns 'Hello, Ada'.",
                "curator_authored": True,
            }
        ],
        intent_confirmed=True,
        facts_reviewed=True,
        unresolved_issues=[],
    )
    payload["persona"].update(
        instructions="Ask direct questions.",
        disclosure_policy="Use only allowed facts.",
        unknown_answer_policy="Say when an answer is unknown.",
        reviewed_subject_safe=True,
    )
    draft.write_text(json.dumps(payload))
    frozen = run(capsys, "case", "freeze", str(draft))
    assert frozen["commit_paths"] == ["cases/mine/case.json"]
    assert frozen["commit"]["message"] == "case: freeze mine"
    assert frozen["commit"]["state"] == "committed"
    log = subprocess.run(
        ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, check=False
    )
    assert log.stdout.splitlines() == [
        "case: freeze mine",
        "case: draft mine",
        "workspace: initialize",
    ]
    assert frozen["alias"] == "mine"
    assert frozen["next"] == (
        "Calibrate before subject spend with case calibrate mine --judge MODEL[@EFFORT]."
    )
    assert run(capsys, "store", "alias", "list")["aliases"] == {"mine": frozen["id"]}

    payload["title"] = "Edited title"
    draft.write_text(json.dumps(payload))
    refrozen = run(capsys, "case", "freeze", str(draft))
    assert refrozen["id"] != frozen["id"]
    assert refrozen["alias"] == "mine"
    assert run(capsys, "store", "alias", "list")["aliases"] == {"mine": refrozen["id"]}
    assert run(capsys, "case", "inspect", "mine")["id"] == refrozen["id"]


@pytest.mark.integration
def test_case_freeze_reports_an_unusable_folder_name_as_alias_error_and_still_freezes(
    historical_repo: tuple[Path, str, str, str],
    tmp_path: Path,
    short_runtime: Path,
    git_identity,
    monkeypatch,
    capsys,
) -> None:
    source, baseline, _, _ = historical_repo
    name = "a" * 64
    workspace = tmp_path / "ws"
    run(capsys, "init", str(workspace), "--runtime-root", str(short_runtime))
    monkeypatch.chdir(workspace)
    session_id = run(
        capsys, "collect", "import", str(FIXTURES / "codex-recorded.jsonl"), "--agent", "codex"
    )["id"]
    run(
        capsys,
        "case",
        "draft",
        session_id,
        "--repo",
        str(source),
        "--commit",
        baseline,
        "--name",
        name,
    )
    draft = workspace / "cases" / name / "case.json"
    payload = json.loads(draft.read_text())
    payload.update(
        phases=["fix"],
        allowed_facts=[
            {
                "fact_id": "punctuation",
                "phase": "fix",
                "text": "Use a comma after Hello.",
                "check": "greet('Ada') returns 'Hello, Ada'.",
                "curator_authored": True,
            }
        ],
        intent_confirmed=True,
        facts_reviewed=True,
        unresolved_issues=[],
    )
    payload["persona"].update(
        instructions="Ask direct questions.",
        disclosure_policy="Use only allowed facts.",
        unknown_answer_policy="Say when an answer is unknown.",
        reviewed_subject_safe=True,
    )
    draft.write_text(json.dumps(payload))
    assert main(["case", "freeze", str(draft), "--json"]) == 0
    frozen = json.loads(capsys.readouterr().out)["data"]
    assert re.fullmatch(r"[0-9a-f]{64}", frozen["id"])
    assert frozen["alias_error"] == "An alias cannot look like an object ID."
    assert run(capsys, "store", "alias", "list")["aliases"] == {}


def test_case_draft_outside_a_workspace_still_needs_an_explicit_out(tmp_path: Path, capsys) -> None:
    store = ["--store", str(tmp_path / "store")]
    session_id = run(
        capsys,
        *store,
        "collect",
        "import",
        str(FIXTURES / "codex-recorded.jsonl"),
        "--agent",
        "codex",
    )["id"]
    assert main([*store, "case", "draft", session_id, "--json"]) == 2
    assert "--out" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_case_draft_names_cannot_escape_the_cases_folder(
    tmp_path: Path, short_runtime: Path, git_identity, monkeypatch, capsys
) -> None:
    workspace = tmp_path / "ws"
    run(capsys, "init", str(workspace), "--runtime-root", str(short_runtime))
    monkeypatch.chdir(workspace)
    session_id = run(
        capsys, "collect", "import", str(FIXTURES / "codex-recorded.jsonl"), "--agent", "codex"
    )["id"]
    assert main(["case", "draft", session_id, "--name", "../evil", "--json"]) == 2
    assert not (workspace / "evil").exists()


def test_a_second_case_draft_from_one_session_takes_the_next_free_folder(
    tmp_path: Path, short_runtime: Path, monkeypatch, capsys
) -> None:
    workspace = tmp_path / "ws"
    initialize_workspace(workspace, runtime_root=short_runtime)
    monkeypatch.chdir(workspace)
    session_id = run(
        capsys, "collect", "import", str(FIXTURES / "codex-recorded.jsonl"), "--agent", "codex"
    )["id"]
    first = run(capsys, "case", "draft", session_id)["path"]
    second = run(capsys, "case", "draft", session_id)["path"]
    stem = f"case-{session_id[:12]}"
    assert first == str(workspace / "cases" / stem / "case.json")
    assert second == str(workspace / "cases" / f"{stem}-2" / "case.json")
    assert main(["case", "draft", session_id, "--name", stem, "--json"]) == 1
    assert "choose another --name" in json.loads(capsys.readouterr().err)["error"]["message"]
