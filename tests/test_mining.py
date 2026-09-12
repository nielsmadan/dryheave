import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dryheave.authoring_catalog import ProblemDecision, VoiceDraft, read_catalog
from dryheave.cases import DeterministicCriterion, draft_case, load_frozen_case
from dryheave.errors import ConflictError, InputError
from dryheave.logs.base import EvidenceExcerpt, Session
from dryheave.logs.service import import_session
from dryheave.mining import (
    EvidenceQuery,
    MetadataQuery,
    SelectionInputs,
    create_request,
    create_voice,
    evidence_page,
    metadata_page,
    record_decision,
    record_gap,
    select_logs,
    validate_problem,
)
from dryheave.models import AgentKind, CommandSpec, ObjectKind
from dryheave.personas import Persona, load_frozen_persona
from dryheave.repositories import capture_repository

FIXTURE = Path("tests/fixtures/codex-recorded.jsonl")


@pytest.fixture
def mining_context(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    catalog = select_logs(
        store,
        root,
        "chosen",
        SelectionInputs(paths=(FIXTURE,), agent=AgentKind.CODEX),
        expected_revision=0,
    )
    selection = catalog.selections["chosen"]
    session_id = selection.session_ids[0]
    session = store.load(session_id, Session, kind=ObjectKind.SESSION)
    user = next(event for event in session.events if event.kind == "user")
    evidence = EvidenceExcerpt(
        session_id=session_id, event_id=user.event_id, visibility="subject", excerpt=user.text
    )
    catalog = create_request(
        root, "tasks", "chosen", "Find a greeting change", micro_bug=False, expected_revision=1
    )
    persona = Persona(
        name="Fixture voice",
        instructions="Answer directly.",
        disclosure_policy="Use approved case facts only.",
        unknown_answer_policy="Say when unknown.",
        examples=(evidence,),
        reviewed_subject_safe=True,
    )
    decision = ProblemDecision(
        name="greeting",
        title="Greeting command",
        category="cli",
        state="unresolved",
        session_id=session_id,
        start_event=user.event_id,
        end_event=session.events[-1].event_id,
        evidence=(evidence,),
        rationale="An explicit command request.",
        boundary_rationale="The selected exchange covers the command request.",
        baseline_rationale="Needs historical repository evidence.",
        dirty_state="unknown",
        dirty_state_rationale="Not established from the selected log.",
    )
    return root, selection, session, persona, decision


def test_selection_pins_ids_and_refuses_replacement_without_editing_sources(
    store, mining_context, tmp_path
):
    root, selection, session, _, _ = mining_context
    source = tmp_path / "copy.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    before = source.read_bytes()
    imported = import_session(store, source, AgentKind.CODEX)
    store.set_alias("picked", imported)
    selected = select_logs(
        store,
        root,
        "second",
        SelectionInputs(paths=(source,), sessions=("picked",), agent=AgentKind.CODEX),
        expected_revision=2,
    )
    assert selected.selections["second"].session_ids == (imported,)
    changed = store.put(ObjectKind.SESSION, session.model_copy(update={"source_id": "different"}))
    store.set_alias("picked", changed, replace=True)
    assert read_catalog(root).selections["second"].session_ids == (imported,)
    with pytest.raises(ConflictError, match="immutable"):
        select_logs(
            store, root, "second", SelectionInputs(sessions=(changed,)), expected_revision=3
        )
    assert source.read_bytes() == before
    assert read_catalog(root).selections["chosen"] == selection
    assert read_catalog(root).revision == 3


@pytest.mark.parametrize(
    "inputs",
    [
        SelectionInputs(),
        SelectionInputs(paths=(FIXTURE,)),
        SelectionInputs(sessions=("a" * 64,) * 25),
    ],
)
def test_selection_requires_explicit_bounded_inputs(store, tmp_path, inputs):
    with pytest.raises(InputError):
        select_logs(store, tmp_path, "test", inputs, expected_revision=0)
    assert read_catalog(tmp_path).revision == 0


def test_evidence_pages_actual_user_and_tool_context_with_clipping(store, mining_context):
    _, selection, session, _, decision = mining_context
    page = evidence_page(
        store, selection, decision.session_id, EvidenceQuery(kind="user", limit=1, text_limit=5)
    )
    assert page["events"][0]["text"] == "Add a"
    assert page["events"][0]["text_next_offset"] == 5
    assert page["next_offset"] == 1
    continuation = evidence_page(
        store,
        selection,
        decision.session_id,
        EvidenceQuery(event_id=decision.start_event, text_offset=5),
    )
    assert continuation["events"][0]["text"] == " greeting command."
    tool = next(event for event in session.events if event.kind == "tool_call")
    page = evidence_page(
        store, selection, decision.session_id, EvidenceQuery(event_id=tool.event_id, text_limit=3)
    )
    assert (
        page["events"][0]["tool_context"]
        == json.dumps(tool.data, ensure_ascii=False, sort_keys=True)[:3]
    )
    assert page["events"][0]["context_truncated"] is True
    assert (
        evidence_page(store, selection, decision.session_id, EvidenceQuery(offset=100))["events"]
        == []
    )
    with pytest.raises(InputError, match="outside"):
        evidence_page(store, selection, "a" * 64)
    with pytest.raises(InputError, match="No matching"):
        evidence_page(store, selection, decision.session_id, EvidenceQuery(event_id="absent"))
    for value in ({"limit": 26}, {"offset": -1}, {"text_limit": 8001}, {"text_offset": -1}):
        with pytest.raises(ValidationError):
            EvidenceQuery(**value)


def test_metadata_pagination_preserves_large_values_and_targeted_keys(store, mining_context):
    root, _, session, _, _ = mining_context
    metadata = {
        "cwd": "/fixture/repository",
        "git": {"commit_hash": "a" * 40},
        "source": "λ" * 18000,
    }
    identifier = store.put(ObjectKind.SESSION, session.model_copy(update={"metadata": metadata}))
    selection = select_logs(
        store, root, "large", SelectionInputs(sessions=(identifier,)), expected_revision=2
    ).selections["large"]
    offset = 0
    parts = []
    while True:
        page = metadata_page(store, selection, identifier, MetadataQuery(text_offset=offset))
        parts.append(page["metadata_json"])
        assert len(page["metadata_json"]) <= 4000
        assert page["text_offset"] == offset
        assert page["truncated"] is True
        if page["text_next_offset"] is None:
            break
        offset = page["text_next_offset"]
    assert json.loads("".join(parts)) == metadata
    assert page["text_characters"] == len("".join(parts))
    git = metadata_page(store, selection, identifier, MetadataQuery(key="git"))
    assert json.loads(git["metadata_json"]) == metadata["git"]
    assert git["truncated"] is False
    assert git["text_next_offset"] is None
    past_end = metadata_page(store, selection, identifier, MetadataQuery(text_offset=100000))
    assert past_end["metadata_json"] == ""
    assert past_end["text_next_offset"] is None
    assert past_end["truncated"] is True
    for value in ({"text_limit": 0}, {"text_limit": 8001}, {"text_offset": -1}):
        with pytest.raises(ValidationError):
            MetadataQuery(**value)


@pytest.mark.parametrize(
    "fault",
    ["empty", "assistant", "outside", "fabricated", "whitespace", "unsafe", "policy", "review"],
)
def test_voice_requires_safe_exact_selected_actual_user_evidence(store, mining_context, fault):
    root, selection, session, persona, _ = mining_context
    example = persona.examples[0]
    review = "Reviewed ordinary user wording; excludes answers and injected blocks."
    if fault == "empty":
        persona = persona.model_copy(update={"examples": ()})
    elif fault == "unsafe":
        persona = persona.model_copy(update={"reviewed_subject_safe": False})
    elif fault == "policy":
        persona = persona.model_copy(update={"unknown_answer_policy": ""})
    elif fault == "review":
        review = ""
    else:
        if fault == "assistant":
            event = next(event for event in session.events if event.kind == "assistant")
            example = example.model_copy(update={"event_id": event.event_id, "excerpt": event.text})
        elif fault == "outside":
            example = example.model_copy(update={"session_id": "a" * 64})
        else:
            example = example.model_copy(
                update={"excerpt": " " if fault == "whitespace" else "invented user words"}
            )
        persona = persona.model_copy(update={"examples": (example,)})
    with pytest.raises(InputError):
        create_voice(
            store,
            root,
            "voice",
            VoiceDraft(selection_id=selection.identifier, persona=persona, safety_review=review),
            expected_revision=2,
        )
    assert read_catalog(root).voices == {}
    assert read_catalog(root).revision == 2


def test_voice_freezes_compatible_persona_and_preserves_names(store, mining_context):
    root, selection, _, persona, _ = mining_context
    draft = VoiceDraft(
        selection_id=selection.identifier,
        persona=persona,
        safety_review="Reviewed exact user wording for safe style only.",
    )
    catalog = create_voice(store, root, "voice", draft, expected_revision=2)
    assert load_frozen_persona(store, catalog.voices["voice"].persona_id) == persona
    with pytest.raises(ConflictError, match="already exists"):
        create_voice(store, root, "voice", draft, expected_revision=3)
    assert read_catalog(root).revision == 3


@pytest.mark.parametrize(
    "fault", ["source", "boundary", "assistant", "evidence_range", "evidence_text"]
)
def test_decisions_reject_inconsistent_membership_and_ranges(store, mining_context, fault):
    root, _, session, _, decision = mining_context
    if fault == "source":
        decision = decision.model_copy(update={"session_id": "a" * 64})
    elif fault == "boundary":
        decision = decision.model_copy(update={"end_event": "absent"})
    elif fault == "assistant":
        event = next(event for event in session.events if event.kind == "assistant")
        decision = decision.model_copy(update={"start_event": event.event_id})
    elif fault == "evidence_range":
        decision = decision.model_copy(
            update={
                "end_event": decision.start_event,
                "evidence": (
                    decision.evidence[0].model_copy(
                        update={"event_id": session.events[-1].event_id}
                    ),
                ),
            }
        )
    else:
        decision = decision.model_copy(
            update={"evidence": (decision.evidence[0].model_copy(update={"excerpt": "invented"}),)}
        )
    with pytest.raises(InputError):
        record_decision(store, root, "tasks", decision, expected_revision=2)
    assert read_catalog(root).requests["tasks"].decisions == {}


def test_rejections_and_coverage_gaps_remain_explicit(store, mining_context):
    root, _, _, _, decision = mining_context
    catalog = record_decision(
        store,
        root,
        "tasks",
        decision.model_copy(
            update={"state": "rejected", "rationale": "Cannot independently recover the baseline."}
        ),
        expected_revision=2,
    )
    assert catalog.requests["tasks"].decisions[decision.name].state == "rejected"
    catalog = record_gap(
        root, "tasks", "No supported UI task in the selected sessions.", expected_revision=3
    )
    assert catalog.requests["tasks"].gaps == ("No supported UI task in the selected sessions.",)
    assert catalog.requests["tasks"].target_maximum == 6


@pytest.fixture
def drafted_problem(store, historical_repo, mining_context):
    root, _, _, persona, decision = mining_context
    source, baseline, _, _ = historical_repo
    repository = capture_repository(store, source, baseline)
    draft = draft_case(
        store,
        decision.session_id,
        repository_id=repository,
        start=decision.start_event,
        end=decision.end_event,
    )
    hidden = root / "hidden.py"
    hidden.write_text('print("fixture verifier")\n')
    reference = root / "reference.patch"
    reference.write_text("fixture reference bytes\n")
    draft = draft.model_copy(
        update={
            "persona": persona,
            "intent_confirmed": True,
            "facts_reviewed": True,
            "unresolved_issues": (),
            "hidden_files": {"hidden.py": "hidden.py"},
            "reference_patch": "reference.patch",
            "criteria": (
                DeterministicCriterion(
                    criterion_id="check",
                    description="Verify greeting punctuation and name behavior.",
                    command=CommandSpec(argv=("python3", "{verifier}/hidden.py")),
                    entrypoint="hidden.py",
                    expected_stdout="fixture verifier",
                ),
            ),
        }
    )
    path = root / "case.json"
    path.write_text(draft.model_dump_json())
    decision = decision.model_copy(
        update={
            "state": "drafted",
            "draft_path": "case.json",
            "dirty_state": "clean",
            "dirty_state_rationale": "Fixture baseline starts with no initial patch.",
            "baseline_rationale": f"Fixture baseline commit {baseline}.",
        }
    )
    record_decision(store, root, "tasks", decision, expected_revision=2)
    return root, decision, path, hidden, reference


@pytest.mark.parametrize("changed", ["draft", "hidden", "reference", "request"])
def test_freeze_rejects_stale_validation_for_every_content_boundary(
    store, drafted_problem, changed
):
    root, decision, path, hidden, reference = drafted_problem
    catalog = validate_problem(store, root, "tasks", decision.name, expected_revision=3)
    assert catalog.requests["tasks"].decisions[decision.name].validation is not None
    revision = 4
    if changed == "draft":
        payload = json.loads(path.read_text())
        payload["title"] = "Edited title after validation"
        path.write_text(json.dumps(payload))
    elif changed == "request":
        record_gap(root, "tasks", "A new coverage decision", expected_revision=4)
        revision = 5
    else:
        selected = hidden if changed == "hidden" else reference
        selected.write_text(selected.read_text() + "changed bytes\n")
    with pytest.raises(ConflictError, match="stale"):
        validate_problem(
            store, root, "tasks", decision.name, expected_revision=revision, freeze=True
        )
    assert read_catalog(root).revision == revision
    assert read_catalog(root).requests["tasks"].decisions[decision.name].state == "drafted"
    assert list(root.glob(".validation-*")) == []


def test_validate_freeze_uses_reviewed_snapshot_and_pins_case(store, drafted_problem):
    root, decision, _, hidden, reference = drafted_problem
    with pytest.raises(ConflictError, match="missing or stale"):
        validate_problem(store, root, "tasks", decision.name, expected_revision=3, freeze=True)
    validate_problem(store, root, "tasks", decision.name, expected_revision=3)
    catalog = validate_problem(
        store, root, "tasks", decision.name, expected_revision=4, freeze=True
    )
    frozen = catalog.requests["tasks"].decisions[decision.name]
    assert frozen.state == "frozen"
    case = load_frozen_case(store, frozen.case_id)
    assert (case.source_session_id, case.source_start_event, case.source_end_event) == (
        decision.session_id,
        decision.start_event,
        decision.end_event,
    )
    assert store.read_blob(frozen.case_id, "verifiers/hidden.py") == hidden.read_bytes()
    assert store.read_blob(frozen.case_id, "reference.patch") == reference.read_bytes()
    with pytest.raises(ConflictError, match="retained"):
        record_decision(store, root, "tasks", decision, expected_revision=5)


@pytest.mark.parametrize("fault", ["provenance", "evidence", "dirty", "patch", "micro"])
def test_drafted_candidates_require_matching_case_baseline_and_scope(store, drafted_problem, fault):
    root, decision, path, _, _ = drafted_problem
    payload = json.loads(path.read_text())
    if fault == "provenance":
        payload["end_event"] = decision.start_event
    elif fault == "evidence":
        payload["evidence"][0]["session_id"] = "a" * 64
    elif fault == "dirty":
        decision = decision.model_copy(update={"dirty_state": "unknown"})
    elif fault == "patch":
        decision = decision.model_copy(update={"dirty_state": "patch"})
    else:
        create_request(
            root, "micro", "chosen", "One small bug", micro_bug=True, expected_revision=3
        )
    path.write_text(json.dumps(payload))
    with pytest.raises(InputError):
        record_decision(
            store,
            root,
            "micro" if fault == "micro" else "tasks",
            decision,
            expected_revision=4 if fault == "micro" else 3,
        )


def test_micro_request_accepts_only_one_supported_candidate(store, drafted_problem):
    root, decision, _, _, _ = drafted_problem
    create_request(root, "micro", "chosen", "One small bug", micro_bug=True, expected_revision=3)
    decision = decision.model_copy(
        update={"micro_bug_rationale": "A single bounded command behavior."}
    )
    record_decision(store, root, "micro", decision, expected_revision=4)
    with pytest.raises(InputError, match="Invalid schema"):
        record_decision(
            store,
            root,
            "micro",
            decision.model_copy(update={"name": "second"}),
            expected_revision=5,
        )
    assert list(read_catalog(root).requests["micro"].decisions) == [decision.name]
