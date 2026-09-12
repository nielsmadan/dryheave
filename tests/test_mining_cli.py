import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from dryheave.cli import main


@pytest.fixture
def authoring_workspace(tmp_path, monkeypatch, capsys):
    fixture = Path("tests/fixtures/codex-recorded.jsonl").absolute()
    runtime = (Path(".cache") / ("m" + uuid4().hex[:5])).absolute()
    workspace = tmp_path / "bench"
    assert main(["init", str(workspace), "--runtime-root", str(runtime), "--json"]) == 0
    capsys.readouterr()
    nested = workspace / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    yield workspace, fixture
    shutil.rmtree(runtime)


def invoke(capsys, *arguments):
    assert main([*arguments, "--json"]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out)["data"]


@pytest.mark.parametrize("draft_directory", [".dryheave/authoring", "drafts"])
def test_selected_log_public_cli_voice_decision_validate_freeze(
    authoring_workspace, historical_repo, capsys, draft_directory
):
    workspace, fixture = authoring_workspace
    source, baseline, _, _ = historical_repo
    selected = invoke(capsys, "collect", "select", "chosen", str(fixture), "--agent", "codex")
    session_id = selected["selection"]["session_ids"][0]
    assert selected["revision"] == 1
    assert invoke(capsys, "collect", "selections")["selections"][0]["name"] == "chosen"
    summary = invoke(capsys, "collect", "selection", selected["id"])
    assert summary["sessions"][0]["id"] == session_id
    page = invoke(
        capsys,
        "collect",
        "evidence",
        "chosen",
        "--session",
        session_id,
        "--kind",
        "user",
        "--limit",
        "1",
    )
    user = page["events"][0]
    assert user["text"] == "Add a greeting command."
    request = invoke(
        capsys,
        "problem",
        "request",
        "Find one command fix",
        "--selection",
        "chosen",
        "--name",
        "mine",
        "--micro-bug",
    )
    assert request["target_maximum"] == 1
    assert request["request"]["description"] == "Find one command fix"
    assert invoke(capsys, "problem", "list")["requests"][0]["name"] == "mine"
    assert invoke(capsys, "problem", "inspect", request["id"])["revision"] == 2
    voice_path = Path(invoke(capsys, "voice", "draft", "--selection", "chosen")["path"])
    payload = json.loads(voice_path.read_text())
    payload["safety_review"] = (
        "Reviewed direct user wording, without hidden solutions or injected blocks."
    )
    payload["persona"].update(
        instructions="Use concise factual replies.",
        disclosure_policy="Disclose approved case facts only.",
        unknown_answer_policy="Say when unknown.",
        reviewed_subject_safe=True,
        examples=[
            {
                "session_id": session_id,
                "event_id": user["event_id"],
                "visibility": "subject",
                "excerpt": user["text"],
            }
        ],
    )
    voice_path.write_text(json.dumps(payload))
    voice = invoke(capsys, "voice", "create", "my-voice", str(voice_path), "--expect-revision", "2")
    assert invoke(capsys, "voice", "list")["voices"][0]["persona_id"] == voice["id"]
    assert (
        invoke(capsys, "voice", "inspect", "my-voice")["persona"]["examples"][0]["excerpt"]
        == user["text"]
    )
    case_path = workspace / draft_directory / "case.json"
    invoke(
        capsys,
        "case",
        "draft",
        session_id,
        "--repo",
        str(source),
        "--commit",
        baseline,
        "--start",
        user["event_id"],
        "--end",
        user["event_id"],
        "--out",
        str(case_path),
    )
    payload = json.loads(case_path.read_text())
    payload.update(
        persona=None,
        persona_id=voice["id"],
        intent_confirmed=True,
        facts_reviewed=True,
        unresolved_issues=[],
        criteria=[
            {
                "kind": "judge",
                "criterion_id": "command",
                "rubric": "The requested greeting command behaves correctly for normal and empty names.",
            }
        ],
    )
    case_path.write_text(json.dumps(payload))
    record = [
        "problem",
        "record",
        "mine",
        "greeting",
        "--title",
        "Greeting command",
        "--category",
        "CLI behavior",
        "--session",
        session_id,
        "--start",
        user["event_id"],
        "--end",
        user["event_id"],
        "--evidence",
        user["event_id"],
        user["text"],
        "--rationale",
        "Explicit command request",
        "--boundary-rationale",
        "Single explicit request",
        "--baseline-rationale",
        f"Fixture historical SHA {baseline}",
        "--dirty-state",
        "clean",
        "--dirty-state-rationale",
        "Known clean fixture start",
        "--micro-bug-rationale",
        "One bounded CLI behavior",
        "--state",
        "drafted",
        "--draft",
        str(case_path),
    ]
    recorded = invoke(capsys, *record, "--expect-revision", "3")
    assert recorded["revision"] == 4
    assert recorded["request"]["decisions"]["greeting"]["draft_path"] == (
        "case.json" if draft_directory == ".dryheave/authoring" else str(case_path)
    )
    validated = invoke(capsys, "problem", "validate", "mine", "greeting", "--expect-revision", "4")
    assert validated["valid"] is True
    frozen = invoke(capsys, "problem", "freeze", "mine", "greeting", "--expect-revision", "5")
    assert frozen["decision"]["state"] == "frozen"
    assert invoke(capsys, "case", "inspect", frozen["id"])["case"]["persona_id"] == voice["id"]
    gap = invoke(
        capsys,
        "problem",
        "gap",
        "mine",
        "Only the command category is supported.",
        "--expect-revision",
        "6",
    )
    assert gap["request"]["gaps"] == ["Only the command category is supported."]


def test_cli_errors_preserve_catalog_and_existing_files(authoring_workspace, capsys):
    workspace, fixture = authoring_workspace
    assert main(["collect", "select", "chosen", str(fixture), "--json"]) == 2
    assert "require --agent" in json.loads(capsys.readouterr().err)["error"]["message"]
    selected = invoke(capsys, "collect", "select", "chosen", str(fixture), "--agent", "codex")
    session_id = selected["selection"]["session_ids"][0]
    path = workspace / ".dryheave/authoring/catalog.json"
    before = path.read_bytes()
    assert main(["collect", "select", "chosen", "--session", session_id, "--json"]) == 1
    assert "immutable" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert path.read_bytes() == before
    request = invoke(capsys, "problem", "request", "--selection", "chosen")
    assert request["target_maximum"] == 6
    assert request["request"]["description"] == ""
    before = path.read_bytes()
    assert (
        main(["problem", "gap", request["id"], "Coverage gap", "--expect-revision", "1", "--json"])
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "conflict"
    assert path.read_bytes() == before
    voice = invoke(capsys, "voice", "draft", "--selection", "chosen")
    draft = Path(voice["path"])
    before = draft.read_bytes()
    assert main(["voice", "draft", "--selection", "chosen", "--out", str(draft), "--json"]) == 1
    capsys.readouterr()
    assert draft.read_bytes() == before
    assert main(["voice", "create", "empty", str(draft), "--expect-revision", "2", "--json"]) == 2
    assert "safety review" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_selection_commands_require_initialized_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["collect", "selections", "--json"]) == 2
    assert "init first" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_cli_recovers_baseline_only_in_selected_session_metadata(authoring_workspace, capsys):
    _, fixture = authoring_workspace
    selected = invoke(
        capsys,
        "collect",
        "select",
        "baseline",
        str(fixture.with_name("codex-baseline-metadata.jsonl")),
        "--agent",
        "codex",
    )
    session_id = selected["selection"]["session_ids"][0]
    summary = invoke(capsys, "collect", "selection", "baseline")
    assert summary["sessions"][0]["metadata_command"] == (
        f"dryheave collect metadata {selected['id']} --session {session_id} --json"
    )
    events = invoke(capsys, "collect", "evidence", "baseline", "--session", session_id)
    assert [(item["kind"], item["text"]) for item in events["events"]] == [
        ("user", "Fix the greeting command for empty names.")
    ]
    arguments = ["collect", "metadata", "baseline", "--session", session_id]
    metadata = invoke(capsys, *arguments)
    decoded = json.loads(metadata["metadata_json"])
    assert decoded["cwd"] == "/fixture/historical-repository"
    assert decoded["git"]["commit_hash"] == "0123456789abcdef0123456789abcdef01234567"
    assert metadata["truncated"] is False
    assert invoke(capsys, "collect", "show", session_id)["session"]["metadata"] == decoded
    first = invoke(capsys, *arguments, "--key", "git", "--text-limit", "25")
    assert first["text_next_offset"] == 25
    assert first["truncated"] is True
    second = invoke(capsys, *arguments, "--key", "git", "--text-offset", "25")
    assert second["text_next_offset"] is None
    assert second["text_offset"] == 25
    assert second["truncated"] is True
    assert json.loads(first["metadata_json"] + second["metadata_json"]) == decoded["git"]
    assert json.loads(invoke(capsys, *arguments, "--key", "cwd")["metadata_json"]) == decoded["cwd"]
    assert main([*arguments, "--key", "absent", "--json"]) == 2
    assert "No metadata key" in json.loads(capsys.readouterr().err)["error"]["message"]
    other = invoke(capsys, "collect", "select", "other", str(fixture), "--agent", "codex")
    outside = other["selection"]["session_ids"][0]
    assert main(["collect", "metadata", "baseline", "--session", outside, "--json"]) == 2
    assert "outside" in json.loads(capsys.readouterr().err)["error"]["message"]


@pytest.mark.parametrize("link_kind", ["file", "directory", "parent_traversal"])
def test_cli_refuses_symlink_draft_paths_without_changing_request(
    authoring_workspace, capsys, link_kind
):
    workspace, fixture = authoring_workspace
    selected = invoke(capsys, "collect", "select", "chosen", str(fixture), "--agent", "codex")
    session_id = selected["selection"]["session_ids"][0]
    user = invoke(
        capsys, "collect", "evidence", "chosen", "--session", session_id, "--kind", "user"
    )["events"][0]
    request = invoke(capsys, "problem", "request", "--selection", "chosen", "--name", "mine")
    drafts = workspace / "drafts"
    drafts.mkdir()
    draft = drafts / "case.json"
    draft.write_text("{}")
    if link_kind == "file":
        reference = drafts / "linked.json"
        reference.symlink_to(draft)
    else:
        linked = workspace / "linked"
        linked.symlink_to(drafts, target_is_directory=True)
        reference = (
            linked / "case.json"
            if link_kind == "directory"
            else linked / ".." / "drafts" / "case.json"
        )
    arguments = [
        "problem",
        "record",
        "mine",
        "candidate",
        "--title",
        "Greeting command",
        "--category",
        "CLI behavior",
        "--session",
        session_id,
        "--start",
        user["event_id"],
        "--end",
        user["event_id"],
        "--evidence",
        user["event_id"],
        user["text"],
        "--rationale",
        "Explicit request",
        "--boundary-rationale",
        "One request",
        "--baseline-rationale",
        "Fixture baseline",
        "--dirty-state",
        "clean",
        "--dirty-state-rationale",
        "Known fixture state",
        "--state",
        "drafted",
        "--draft",
        str(reference),
        "--expect-revision",
        str(request["revision"]),
        "--json",
    ]
    assert main(arguments) == 2
    assert "symlink" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert invoke(capsys, "problem", "inspect", "mine") == request
