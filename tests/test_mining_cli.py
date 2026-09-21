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


def test_voice_defaults_to_one_default_name_and_reports_computed_evidence(
    authoring_workspace, capsys
):
    _, fixture = authoring_workspace
    selected = invoke(capsys, "collect", "select", "chosen", str(fixture), "--agent", "codex")
    session_id = selected["selection"]["session_ids"][0]
    page = invoke(
        capsys, "collect", "evidence", "chosen", "--session", session_id, "--kind", "user"
    )
    user = page["events"][0]
    drafted = invoke(capsys, "voice", "draft", "--selection", "chosen")
    assert drafted["name"] == "default"
    evidence = drafted["evidence"]
    assert evidence["threshold"] == 8
    assert evidence["user_events"] == 3
    assert evidence["genuine"] == 3
    assert evidence["harness_injected"] == 0
    assert evidence["unclassified"] == 0
    assert evidence["sufficient"] is False
    assert evidence["candidates"][0] == {
        "session_id": session_id,
        "event_id": user["event_id"],
        "characters": len(user["text"]),
        "preview": user["text"],
    }
    assert len(drafted["warnings"]) == 1
    assert "below threshold" in drafted["warnings"][0]
    assert "threshold of 8" in drafted["warnings"][0]
    path = Path(drafted["path"])
    payload = json.loads(path.read_text())
    assert payload["persona"]["name"] == "default"
    payload["safety_review"] = "Reviewed direct user wording without injected blocks."
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
    path.write_text(json.dumps(payload))
    created = invoke(capsys, "voice", "create", str(path), "--expect-revision", "1")
    assert created["name"] == "default"
    assert created["voice"]["name"] == "default"
    assert created["evidence"] == evidence
    assert created["warnings"] == drafted["warnings"]
    inspected = invoke(capsys, "voice", "inspect", "default")
    assert inspected["evidence"] == evidence
    assert inspected["voice"]["evidence"] == evidence
    assert invoke(capsys, "voice", "list")["voices"][0]["name"] == "default"
    assert main(["voice", "draft", "--selection", "chosen", "--json"]) == 2
    assert "name this voice explicitly" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert main(["voice", "create", str(path), "--expect-revision", "2", "--json"]) == 2
    assert "name this voice explicitly" in json.loads(capsys.readouterr().err)["error"]["message"]
    named = invoke(capsys, "voice", "draft", "--selection", "chosen", "--name", "second")
    assert named["name"] == "second"
    assert named["evidence"] == evidence
    path = Path(named["path"])
    payload["persona"]["name"] = "second"
    path.write_text(json.dumps(payload))
    again = invoke(capsys, "voice", "create", "second", str(path), "--expect-revision", "2")
    assert again["name"] == "second"
    assert again["evidence"]["sufficient"] is False
    assert {item["name"] for item in invoke(capsys, "voice", "list")["voices"]} == {
        "default",
        "second",
    }


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


def write_codex_log(path: Path, texts: tuple[str, ...], cwd: str | None = None) -> None:
    header = (
        json.dumps({"type": "session_meta", "payload": {"id": path.stem, "cwd": cwd}}) + "\n"
        if cwd is not None
        else ""
    )
    path.write_text(
        header
        + "".join(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": text}})
            + "\n"
            for text in texts
        )
    )


def test_voice_draft_reports_per_session_coverage_and_asks_for_more_sessions(
    authoring_workspace, tmp_path, capsys
):
    rich = tmp_path / "rich.jsonl"
    filler = tmp_path / "filler.jsonl"
    write_codex_log(
        rich,
        (
            "Can you add a greeting command that takes a name and prints it back?",
            "I want the empty name case handled too, and a test for it.",
        ),
    )
    write_codex_log(filler, ("continue", "<system-reminder>noise</system-reminder>"))
    selected = invoke(
        capsys, "collect", "select", "varied", str(rich), str(filler), "--agent", "codex"
    )
    assert len(selected["selection"]["session_ids"]) == 2
    drafted = invoke(capsys, "voice", "draft", "--selection", "varied")
    evidence = drafted["evidence"]
    assert evidence["genuine"] == 3
    assert [item["session_id"] for item in evidence["sessions"]] == selected["selection"][
        "session_ids"
    ]
    breakdown = {item["session_id"]: item for item in evidence["sessions"]}
    assert sorted(item["genuine"] for item in breakdown.values()) == [1, 2]
    assert sorted(item["genuine_characters"] for item in breakdown.values()) == [8, 126]
    assert sum(item["harness_injected"] for item in breakdown.values()) == 1
    assert len(drafted["warnings"]) == 1
    warning = drafted["warnings"][0]
    assert "Genuine messages per selected session (2 selected)" in warning
    assert all(f"{identifier}: " in warning for identifier in breakdown)
    assert "collect select NAME FILE [FILE ...] --agent AGENT" in warning


FEATURE_TEXTS = (
    "Can you add a greeting command that takes a name and prints it back?",
    "I want the empty name case handled too, and a test for it.",
)
BUGFIX_TEXTS = (
    "The parser drops the trailing flag; can you work out why?",
    "Yes, the second one, and keep the fix small.",
)


def curate_voice(capsys, selection: str, name: str, revision: int) -> dict:
    drafted = invoke(capsys, "voice", "draft", "--selection", selection, "--name", name)
    candidate = drafted["evidence"]["candidates"][0]
    page = invoke(
        capsys,
        "collect",
        "evidence",
        selection,
        "--session",
        candidate["session_id"],
        "--event",
        candidate["event_id"],
    )
    path = Path(drafted["path"])
    payload = json.loads(path.read_text())
    payload["safety_review"] = "Reviewed direct user wording without injected blocks."
    payload["persona"].update(
        instructions="Use concise factual replies.",
        disclosure_policy="Disclose approved case facts only.",
        unknown_answer_policy="Say when unknown.",
        reviewed_subject_safe=True,
        examples=[
            {
                "session_id": candidate["session_id"],
                "event_id": candidate["event_id"],
                "visibility": "subject",
                "excerpt": page["events"][0]["text"],
            }
        ],
    )
    path.write_text(json.dumps(payload))
    return invoke(capsys, "voice", "create", name, str(path), "--expect-revision", str(revision))


def test_collect_triage_records_the_sampling_frame_against_a_checked_revision(
    authoring_workspace, tmp_path, capsys
):
    feature = tmp_path / "feature.jsonl"
    bug = tmp_path / "bug.jsonl"
    write_codex_log(feature, FEATURE_TEXTS, cwd="/repos/alpha")
    write_codex_log(bug, BUGFIX_TEXTS, cwd="/repos/beta")
    selected = invoke(
        capsys, "collect", "select", "varied", str(feature), str(bug), "--agent", "codex"
    )
    first, second = selected["selection"]["session_ids"]
    summary = invoke(capsys, "collect", "selection", "varied")
    assert sorted(item["repository"] for item in summary["sessions"]) == [
        "/repos/alpha",
        "/repos/beta",
    ]
    assert all(item["triage"] is None for item in summary["sessions"])
    assert summary["triage"]["variety"] == {
        "sessions": 2,
        "triaged": 0,
        "untriaged": 2,
        "chosen": 0,
        "rejected": 0,
        "kinds": {},
        "repositories": {},
        "unknown_repositories": 0,
        "varied": False,
    }
    assert len(summary["triage_warnings"]) == 1
    assert "Untriaged sessions: 2 of 2" in summary["triage_warnings"][0]
    assert all(identifier in summary["triage_warnings"][0] for identifier in (first, second))
    partial = invoke(
        capsys,
        "collect",
        "triage",
        "varied",
        "--session",
        first,
        "--kind",
        "feature",
        "--grade",
        "medium",
        "--decision",
        "chosen",
        "--reason",
        "Two turns of feature conversation.",
        "--expect-revision",
        "1",
    )
    assert partial["revision"] == 2
    assert partial["triage"]["untriaged"] == [second]
    assert len(partial["triage_warnings"]) == 2
    assert "Untriaged sessions: 1 of 2" in partial["triage_warnings"][0]
    assert "lack variety" in partial["triage_warnings"][1]
    complete = invoke(
        capsys,
        "collect",
        "triage",
        "varied",
        "--session",
        second,
        "--kind",
        "bugfix",
        "--grade",
        "easy",
        "--decision",
        "chosen",
        "--reason",
        "A short bug hunt in another repository.",
        "--expect-revision",
        "2",
    )
    assert complete["revision"] == 3
    assert complete["triage"]["variety"]["kinds"] == {"feature_medium": 1, "bugfix_easy": 1}
    assert complete["triage"]["variety"]["repositories"] == {"/repos/alpha": 1, "/repos/beta": 1}
    assert complete["triage"]["variety"]["varied"] is True
    assert complete["triage_warnings"] == []
    arguments = [
        "collect",
        "triage",
        "varied",
        "--session",
        second,
        "--kind",
        "bugfix",
        "--grade",
        "hard",
        "--decision",
        "rejected",
        "--reason",
        "Reconsidered after rereading.",
        "--expect-revision",
        "1",
        "--json",
    ]
    assert main(arguments) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "conflict"
    created = curate_voice(capsys, "varied", "balanced", 3)
    assert created["triage"]["variety"]["varied"] is True
    assert created["triage_warnings"] == []
    assert invoke(capsys, "voice", "inspect", "balanced")["triage"] == created["triage"]


def test_voice_create_warns_about_untriaged_sessions_and_a_narrow_chosen_set(
    authoring_workspace, tmp_path, capsys
):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_codex_log(first, FEATURE_TEXTS, cwd="/repos/alpha")
    write_codex_log(second, BUGFIX_TEXTS, cwd="/repos/alpha")
    invoke(capsys, "collect", "select", "narrow", str(first), str(second), "--agent", "codex")
    untriaged = curate_voice(capsys, "narrow", "untriaged", 1)
    assert untriaged["triage"]["variety"]["untriaged"] == 2
    assert len(untriaged["triage_warnings"]) == 1
    assert "Untriaged sessions: 2 of 2" in untriaged["triage_warnings"][0]
    assert "collect triage SELECTION" in untriaged["triage_warnings"][0]
    assert (
        invoke(capsys, "voice", "inspect", "untriaged")["triage_warnings"]
        == (untriaged["triage_warnings"])
    )
    revision = untriaged["revision"]
    for identifier in invoke(capsys, "collect", "selection", "narrow")["selection"]["session_ids"]:
        revision = invoke(
            capsys,
            "collect",
            "triage",
            "narrow",
            "--session",
            identifier,
            "--kind",
            "bugfix",
            "--grade",
            "easy",
            "--decision",
            "chosen",
            "--reason",
            "A short bug hunt in the same repository.",
            "--expect-revision",
            str(revision),
        )["revision"]
    narrow = curate_voice(capsys, "narrow", "one-kind", revision)
    assert narrow["triage"]["variety"]["kinds"] == {"bugfix_easy": 2}
    assert narrow["triage"]["variety"]["repositories"] == {"/repos/alpha": 2}
    assert narrow["triage"]["variety"]["varied"] is False
    assert len(narrow["triage_warnings"]) == 1
    warning = narrow["triage_warnings"][0]
    assert "Chosen sessions lack variety: 2 chosen" in warning
    assert "bugfix_easy: 2" in warning and "/repos/alpha: 2" in warning
    assert len(narrow["warnings"]) == 1
    assert "below threshold" in narrow["warnings"][0]
    assert (
        invoke(capsys, "voice", "inspect", "one-kind")["triage_warnings"]
        == narrow["triage_warnings"]
    )


def test_voice_delete_frees_the_name_and_keeps_the_frozen_persona(
    authoring_workspace, tmp_path, capsys
):
    source = tmp_path / "source.jsonl"
    write_codex_log(source, FEATURE_TEXTS, cwd="/repos/alpha")
    invoke(capsys, "collect", "select", "chosen", str(source), "--agent", "codex")
    created = curate_voice(capsys, "chosen", "mistake", 1)
    assert main(["voice", "delete", "absent", "--expect-revision", "2", "--json"]) == 2
    assert "Unknown voice" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert main(["voice", "delete", "mistake", "--expect-revision", "1", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "conflict"
    assert invoke(capsys, "voice", "list")["voices"][0]["name"] == "mistake"
    removed = invoke(capsys, "voice", "delete", "mistake", "--expect-revision", "2")
    assert removed == {
        "revision": 3,
        "name": "mistake",
        "id": created["id"],
        "deleted": True,
        "next": removed["next"],
    }
    assert created["id"] in removed["next"]
    assert "frozen persona object" in removed["next"]
    assert invoke(capsys, "voice", "list")["voices"] == []
    assert main(["voice", "inspect", "mistake", "--json"]) == 2
    assert "Unknown voice" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert invoke(capsys, "persona", "inspect", created["id"])["persona"]["name"] == "mistake"
    again = curate_voice(capsys, "chosen", "mistake", 3)
    assert again["id"] == created["id"]
    assert invoke(capsys, "voice", "list")["voices"][0]["name"] == "mistake"
