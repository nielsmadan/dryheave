import json
from pathlib import Path

import pytest

from dryheave.cli import main

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
        allowed_facts=[
            {"fact_id": "punctuation", "text": "Use a comma after Hello.", "curator_authored": True}
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
    identifier = json.loads(capsys.readouterr().out)["data"]["id"]
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
