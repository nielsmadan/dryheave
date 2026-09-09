import json
from pathlib import Path

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
