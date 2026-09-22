import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dryheave.authoring_catalog import ProblemDecision, VoiceDraft, read_catalog
from dryheave.cases import DeterministicCriterion, draft_case, load_frozen_case
from dryheave.errors import ConflictError, InputError
from dryheave.logs.base import EvidenceExcerpt, LogEvent, Session
from dryheave.logs.service import import_session
from dryheave.mining import (
    VOICE_EVIDENCE_THRESHOLD,
    EvidenceQuery,
    MetadataQuery,
    SelectionInputs,
    classify_user_event,
    create_request,
    create_voice,
    delete_voice,
    evidence_page,
    inspect_voice,
    metadata_page,
    record_decision,
    record_gap,
    record_triage,
    resolve_voice_name,
    select_logs,
    triage_decision,
    triage_evidence,
    user_message_profile,
    validate_problem,
    voice_evidence,
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


def synthetic_model(texts: list[tuple[str, str]], cwd: str | None = None) -> Session:
    return Session(
        agent=AgentKind.CLAUDE,
        source_id="synthetic",
        source_path_hash="a" * 64,
        source_content_hash="b" * 64,
        metadata={"cwd": cwd} if cwd is not None else {},
        events=tuple(
            LogEvent(event_id=f"e{index:06d}", source_line=index + 1, kind=kind, text=text)
            for index, (kind, text) in enumerate(texts)
        ),
    )


def synthetic_session(store, texts: list[tuple[str, str]], cwd: str | None = None) -> str:
    return store.put(ObjectKind.SESSION, synthetic_model(texts, cwd))


def test_user_message_profile_separates_substantial_messages_from_filler():
    rich = user_message_profile(
        synthetic_model([("user", text) for text in RICH_TEXTS] + [("assistant", "Done.")])
    )
    assert (rich.user_events, rich.genuine, rich.harness_injected, rich.unclassified) == (
        3,
        3,
        0,
        0,
    )
    assert rich.genuine_characters == 129
    assert rich.median_genuine_characters == 58
    filler = user_message_profile(synthetic_model([("user", text) for text in FILLER_TEXTS]))
    assert filler.genuine == 3
    assert filler.genuine_characters == 17
    assert filler.median_genuine_characters == 7
    injected = user_message_profile(synthetic_model([("user", text) for text in INJECTED_TEXTS]))
    assert (injected.user_events, injected.genuine, injected.harness_injected) == (3, 0, 3)
    assert (injected.genuine_characters, injected.median_genuine_characters) == (0, 0)


def test_voice_evidence_breaks_down_genuine_messages_per_selected_session(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    rich = synthetic_session(store, [("user", text) for text in RICH_TEXTS])
    thin = synthetic_session(
        store,
        [
            ("user", "continue"),
            ("user", "<system-reminder>noise</system-reminder>"),
            ("user", "<mystery-block>unknown harness shape</mystery-block>"),
        ],
    )
    silent = synthetic_session(store, [("assistant", "Nothing was asked.")])
    catalog = select_logs(
        store,
        root,
        "varied",
        SelectionInputs(sessions=(rich, thin, silent)),
        expected_revision=0,
    )
    selection = catalog.selections["varied"]
    evidence = voice_evidence(store, selection)
    assert [item.session_id for item in evidence.sessions] == list(selection.session_ids)
    breakdown = {item.session_id: item for item in evidence.sessions}
    assert (breakdown[rich].genuine, breakdown[rich].genuine_characters) == (3, 129)
    assert breakdown[rich].median_genuine_characters == 58
    assert (breakdown[thin].genuine, breakdown[thin].harness_injected) == (1, 1)
    assert (breakdown[thin].unclassified, breakdown[thin].genuine_characters) == (1, 8)
    assert (breakdown[silent].user_events, breakdown[silent].genuine) == (0, 0)
    assert evidence.genuine == 4
    assert evidence.user_events == 6
    assert evidence.harness_injected == 1
    assert evidence.unclassified == 1
    assert evidence.sufficient is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Add a greeting command.", ("genuine", (), "Add a greeting command.")),
        (
            "<div> should be block-level, right?",
            ("genuine", (), "<div> should be block-level, right?"),
        ),
        ("<skill>name: helper</skill>", ("harness_injected", ("skill_wrapper",), "")),
        ("<system-reminder>truncated block", ("harness_injected", ("system_reminder",), "")),
        (
            "Contents of /repo/AGENTS.md (project instructions)\nRules follow.",
            ("harness_injected", ("agents_instructions",), ""),
        ),
        (
            "<local-command-stdout>output</local-command-stdout>",
            ("harness_injected", ("command_wrapper",), ""),
        ),
        ("<mystery-block>unknown</mystery-block>", ("unclassified", ("unrecognized_wrapper",), "")),
        ("   ", ("unclassified", (), "")),
    ],
)
def test_user_event_classification_names_the_recognized_wrapper(text, expected):
    assert classify_user_event(text) == expected


def test_voice_evidence_measures_harness_injection_against_a_stated_threshold(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    session_id = synthetic_session(
        store,
        [
            ("user", "Add a greeting command."),
            ("user", "<system-reminder>Ignore this notice.</system-reminder>"),
            ("user", "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>"),
            ("assistant", "Which punctuation?"),
            ("user", "<command-name>/commit</command-name><command-args></command-args>"),
            ("user", "<system-reminder>noise</system-reminder>\n  Fix   empty names.  "),
            ("user", "<mystery-block>unknown harness shape</mystery-block>"),
            ("user", "continue"),
        ],
    )
    catalog = select_logs(
        store, root, "mixed", SelectionInputs(sessions=(session_id,)), expected_revision=0
    )
    evidence = voice_evidence(store, catalog.selections["mixed"])
    assert evidence.threshold == VOICE_EVIDENCE_THRESHOLD
    assert evidence.user_events == 7
    assert evidence.genuine == 3
    assert evidence.harness_injected == 3
    assert evidence.unclassified == 1
    assert evidence.sufficient is False
    assert evidence.truncated is False
    assert [(item.event_id, item.classification, item.reasons) for item in evidence.excluded] == [
        ("e000001", "harness_injected", ("system_reminder",)),
        ("e000002", "harness_injected", ("environment_context",)),
        ("e000004", "harness_injected", ("command_wrapper",)),
        ("e000006", "unclassified", ("unrecognized_wrapper",)),
    ]
    assert [(item.event_id, item.characters, item.preview) for item in evidence.candidates] == [
        ("e000000", 23, "Add a greeting command."),
        ("e000005", 18, "Fix empty names."),
        ("e000007", 8, "continue"),
    ]
    assert all(item.session_id == session_id for item in evidence.candidates)


def test_voice_evidence_calls_a_selection_sufficient_only_at_the_threshold(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    revision = 0
    for count in (VOICE_EVIDENCE_THRESHOLD - 1, VOICE_EVIDENCE_THRESHOLD):
        session_id = synthetic_session(
            store, [("user", f"Message number {index}.") for index in range(count)]
        )
        catalog = select_logs(
            store,
            root,
            f"selection{count}",
            SelectionInputs(sessions=(session_id,)),
            expected_revision=revision,
        )
        revision = catalog.revision
        evidence = voice_evidence(store, catalog.selections[f"selection{count}"])
        assert evidence.genuine == count
        assert evidence.sufficient is (count >= VOICE_EVIDENCE_THRESHOLD)


def test_voice_record_persists_computed_evidence_for_later_inspection(store, mining_context):
    root, selection, _, persona, _ = mining_context
    draft = VoiceDraft(
        selection_id=selection.identifier,
        persona=persona,
        safety_review="Reviewed exact user wording for safe style only.",
    )
    catalog = create_voice(store, root, "voice", draft, expected_revision=2)
    evidence = catalog.voices["voice"].evidence
    assert evidence == voice_evidence(store, selection)
    assert evidence is not None
    assert evidence.user_events == 3
    assert evidence.genuine == 3
    assert evidence.sufficient is False
    assert read_catalog(root).voices["voice"].evidence == evidence
    reported = inspect_voice(store, read_catalog(root), "voice")["evidence"]
    assert reported == evidence.model_dump(mode="json")


def test_default_voice_name_is_used_once_and_then_demands_an_explicit_name(store, mining_context):
    root, selection, _, persona, _ = mining_context
    assert resolve_voice_name(read_catalog(root), None) == "default"
    assert resolve_voice_name(read_catalog(root), "chosen-name") == "chosen-name"
    create_voice(
        store,
        root,
        resolve_voice_name(read_catalog(root), None),
        VoiceDraft(
            selection_id=selection.identifier,
            persona=persona,
            safety_review="Reviewed exact user wording for safe style only.",
        ),
        expected_revision=2,
    )
    catalog = read_catalog(root)
    assert "default" in catalog.voices
    with pytest.raises(InputError, match="name this voice explicitly"):
        resolve_voice_name(catalog, None)
    assert resolve_voice_name(catalog, "second") == "second"


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


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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


TRIAGED_TEXTS = (
    ("user", "Please add a greeting command that takes a name."),
    ("user", "Handle the empty name case too, with a test."),
)


def triaged_selection(store, root, name, sessions, *, expected_revision):
    return select_logs(
        store,
        root,
        name,
        SelectionInputs(sessions=tuple(sessions)),
        expected_revision=expected_revision,
    ).selections[name]


def test_voice_delete_removes_only_the_catalog_entry_and_frees_its_name(store, mining_context):
    root, selection, _, persona, _ = mining_context
    draft = VoiceDraft(
        selection_id=selection.identifier,
        persona=persona,
        safety_review="Reviewed exact user wording for safe style only.",
    )
    catalog = create_voice(store, root, "voice", draft, expected_revision=2)
    persona_id = catalog.voices["voice"].persona_id
    catalog, removed = delete_voice(root, "voice", expected_revision=3)
    assert removed.persona_id == persona_id
    assert catalog.voices == {}
    assert read_catalog(root).voices == {}
    assert read_catalog(root).revision == 4
    assert load_frozen_persona(store, persona_id) == persona
    again = create_voice(store, root, "voice", draft, expected_revision=4)
    assert again.voices["voice"].persona_id == persona_id


def test_voice_delete_refuses_an_unknown_name_and_a_stale_revision(store, mining_context):
    root, selection, _, persona, _ = mining_context
    create_voice(
        store,
        root,
        "voice",
        VoiceDraft(
            selection_id=selection.identifier,
            persona=persona,
            safety_review="Reviewed exact user wording for safe style only.",
        ),
        expected_revision=2,
    )
    with pytest.raises(InputError, match="Unknown voice"):
        delete_voice(root, "absent", expected_revision=3)
    with pytest.raises(ConflictError, match="expected 2"):
        delete_voice(root, "voice", expected_revision=2)
    assert list(read_catalog(root).voices) == ["voice"]
    assert read_catalog(root).revision == 3


def test_triage_decision_requires_the_grade_scale_of_its_own_kind():
    decision = triage_decision(
        "a" * 64, "bugfix", "hard", chosen=True, reason="A long debugging session."
    )
    assert decision.category == "bugfix_hard"
    assert triage_decision(
        "a" * 64, "config_change", None, chosen=False, reason="Config"
    ).category == ("config_change")
    with pytest.raises(InputError, match="easy/medium/hard"):
        triage_decision("a" * 64, "bugfix", "large", chosen=True, reason="Wrong scale.")
    with pytest.raises(InputError, match="needs --grade"):
        triage_decision("a" * 64, "feature", None, chosen=True, reason="No grade.")
    with pytest.raises(InputError, match="carries no grade"):
        triage_decision("a" * 64, "extraneous", "small", chosen=False, reason="Graded nothing.")


def test_triage_records_one_decision_per_session_at_a_checked_revision(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    first = synthetic_session(store, list(TRIAGED_TEXTS), "/repos/alpha")
    second = synthetic_session(
        store, [("user", "Why does the parser drop the flag?")], "/repos/beta"
    )
    selection = triaged_selection(store, root, "work", (first, second), expected_revision=0)
    outside = synthetic_session(store, [("user", "Unrelated work.")])
    catalog = record_triage(
        store,
        root,
        "work",
        triage_decision(first, "feature", "medium", chosen=True, reason="A two-turn feature."),
        expected_revision=1,
    )
    assert catalog.triage["work"].selection_id == selection.identifier
    assert catalog.triage["work"].decisions[first].category == "feature_medium"
    catalog = record_triage(
        store,
        root,
        "work",
        triage_decision(first, "feature", "large", chosen=False, reason="Reread: far too broad."),
        expected_revision=2,
    )
    assert catalog.triage["work"].decisions[first].chosen is False
    assert catalog.triage["work"].decisions[first].grade == "large"
    with pytest.raises(ConflictError, match="expected 1"):
        record_triage(
            store,
            root,
            "work",
            triage_decision(second, "bugfix", "easy", chosen=True, reason="Short bug hunt."),
            expected_revision=1,
        )
    with pytest.raises(InputError, match="outside the selected"):
        record_triage(
            store,
            root,
            "work",
            triage_decision(outside, "bugfix", "easy", chosen=True, reason="Not selected."),
            expected_revision=3,
        )
    assert list(read_catalog(root).triage["work"].decisions) == [first]
    assert read_catalog(root).revision == 3


def test_triage_evidence_reports_chosen_kinds_and_repository_spread(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    feature = synthetic_session(store, list(TRIAGED_TEXTS), "/repos/alpha")
    bug = synthetic_session(store, [("user", "The parser drops the trailing flag.")])
    config = synthetic_session(store, [("user", "Bump the linter settings.")], "/repos/beta")
    untriaged = synthetic_session(
        store, [("user", "Anything else worth doing here?")], "/repos/beta"
    )
    selection = triaged_selection(
        store, root, "work", (feature, bug, config, untriaged), expected_revision=0
    )
    revision = 1
    for session_id, kind, grade, chosen, reason in (
        (feature, "feature", "medium", True, "A two-turn feature request."),
        (bug, "bugfix", "easy", True, "A short bug hunt with no recorded cwd."),
        (config, "config_change", None, False, "Only a settings edit; no conversation."),
    ):
        revision = record_triage(
            store,
            root,
            "work",
            triage_decision(session_id, kind, grade, chosen=chosen, reason=reason),
            expected_revision=revision,
        ).revision
    triage = triage_evidence(store, read_catalog(root), selection)
    assert [item.session_id for item in triage.entries] == [
        item for item in selection.session_ids if item != untriaged
    ]
    assert triage.untriaged == (untriaged,)
    assert {item.session_id: item.repository for item in triage.entries} == {
        feature: "/repos/alpha",
        bug: None,
        config: "/repos/beta",
    }
    variety = triage.variety
    assert (variety.sessions, variety.triaged, variety.untriaged) == (4, 3, 1)
    assert (variety.chosen, variety.rejected) == (2, 1)
    assert variety.kinds == {"feature_medium": 1, "bugfix_easy": 1}
    assert variety.repositories == {"/repos/alpha": 1}
    assert variety.unknown_repositories == 1
    assert variety.varied is True


def test_triage_evidence_calls_one_kind_or_one_repository_unvaried(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    first = synthetic_session(store, list(TRIAGED_TEXTS), "/repos/alpha")
    second = synthetic_session(
        store, [("user", "Another bug in the same repository.")], "/repos/alpha"
    )
    selection = triaged_selection(store, root, "narrow", (first, second), expected_revision=0)
    revision = 1
    for session_id, kind in ((first, "bugfix"), (second, "bugfix")):
        revision = record_triage(
            store,
            root,
            "narrow",
            triage_decision(session_id, kind, "easy", chosen=True, reason="A short bug hunt."),
            expected_revision=revision,
        ).revision
    variety = triage_evidence(store, read_catalog(root), selection).variety
    assert variety.kinds == {"bugfix_easy": 2}
    assert variety.repositories == {"/repos/alpha": 2}
    assert variety.varied is False
    revision = record_triage(
        store,
        root,
        "narrow",
        triage_decision(second, "feature", "small", chosen=True, reason="A small feature."),
        expected_revision=revision,
    ).revision
    variety = triage_evidence(store, read_catalog(root), selection).variety
    assert variety.kinds == {"bugfix_easy": 1, "feature_small": 1}
    assert variety.repositories == {"/repos/alpha": 2}
    assert variety.varied is False


def test_voice_record_persists_the_triage_folded_from_its_selection(store, tmp_path):
    root = tmp_path / "authoring"
    root.mkdir()
    session_id = synthetic_session(store, list(TRIAGED_TEXTS), "/repos/alpha")
    selection = triaged_selection(store, root, "work", (session_id,), expected_revision=0)
    session = store.load(session_id, Session, kind=ObjectKind.SESSION)
    excerpt = EvidenceExcerpt(
        session_id=session_id,
        event_id=session.events[0].event_id,
        visibility="subject",
        excerpt=session.events[0].text,
    )
    persona = Persona(
        name="Triaged voice",
        instructions="Answer directly.",
        disclosure_policy="Use approved case facts only.",
        unknown_answer_policy="Say when unknown.",
        examples=(excerpt,),
        reviewed_subject_safe=True,
    )
    draft = VoiceDraft(
        selection_id=selection.identifier,
        persona=persona,
        safety_review="Reviewed exact user wording for safe style only.",
    )
    untriaged = create_voice(store, root, "before", draft, expected_revision=1)
    assert untriaged.voices["before"].triage is not None
    assert untriaged.voices["before"].triage.untriaged == (session_id,)
    record_triage(
        store,
        root,
        "work",
        triage_decision(session_id, "feature", "medium", chosen=True, reason="A two-turn feature."),
        expected_revision=2,
    )
    catalog = create_voice(store, root, "after", draft, expected_revision=3)
    triage = catalog.voices["after"].triage
    assert triage is not None
    assert triage == triage_evidence(store, catalog, selection)
    assert triage.entries[0].repository == "/repos/alpha"
    assert triage.variety.kinds == {"feature_medium": 1}
    assert triage.variety.varied is False
    assert read_catalog(root).voices["after"].triage == triage
    assert inspect_voice(store, read_catalog(root), "after")["triage"] == triage.model_dump(
        mode="json"
    )


def test_claude_skill_invocation_bodies_are_harness_injected() -> None:
    body = (
        "Base directory for this skill: /Users/x/.claude/skills/doc\n"
        "<!-- Generated by loadout from skills/doc/. -->\n\n"
        "# Documentation\n\nRun `just check` before declaring work complete.\n"
    )
    classification, reasons, remainder = classify_user_event(body)
    assert classification == "harness_injected"
    assert reasons == ("skill_wrapper",)
    assert remainder == ""

    preceded = f"please run the doc skill\n{body}"
    classification, reasons, remainder = classify_user_event(preceded)
    assert classification == "genuine"
    assert reasons == ()
    assert remainder == "please run the doc skill"
