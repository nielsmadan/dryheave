import fcntl
import json

import pytest
from pydantic import ValidationError

from dryheave.authoring_catalog import (
    AuthoringCatalog,
    ExcludedUserEvent,
    SessionEvidence,
    TriageDecision,
    TriageEvidence,
    TriageVariety,
    VoiceEvidence,
    edit_catalog,
    read_catalog,
    save_catalog,
)
from dryheave.errors import ConflictError, InputError, LockBusyError, PathError
from dryheave.filesystem import directory_fd


def test_revision_conflict_and_lock_preserve_catalog(tmp_path):
    with edit_catalog(tmp_path, 0) as catalog:
        assert save_catalog(tmp_path, catalog).revision == 1
    before = (tmp_path / "catalog.json").read_bytes()
    with pytest.raises(ConflictError, match="expected 0"), edit_catalog(tmp_path, 0):
        pytest.fail("stale writer entered")
    with directory_fd(tmp_path) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockBusyError), edit_catalog(tmp_path, 1):
            pytest.fail("concurrent writer entered")
    assert (tmp_path / "catalog.json").read_bytes() == before
    assert read_catalog(tmp_path).revision == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2},
        {"revision": "1"},
        {"unexpected": True},
        {"schema_version": True},
        {"requests": {"missing": {"name": "missing", "selection_id": "a" * 64}}},
    ],
)
def test_persisted_catalog_rejects_invalid_schema_without_overwrite(tmp_path, payload):
    content = json.dumps(payload).encode()
    (tmp_path / "catalog.json").write_bytes(content)
    with pytest.raises(InputError), edit_catalog(tmp_path, 0):
        pytest.fail("invalid catalog accepted")
    assert (tmp_path / "catalog.json").read_bytes() == content


def test_catalog_rejects_symlink_and_atomic_failure_preserves_revision(tmp_path, monkeypatch):
    save_catalog(tmp_path, AuthoringCatalog())
    original = (tmp_path / "catalog.json").read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("injected publication failure")

    with monkeypatch.context() as context:
        context.setattr("dryheave.authoring_catalog.atomic_write", fail)
        with pytest.raises(OSError, match="publication"), edit_catalog(tmp_path, 1) as catalog:
            save_catalog(tmp_path, catalog)
    assert (tmp_path / "catalog.json").read_bytes() == original
    (tmp_path / "catalog.json").rename(tmp_path / "saved")
    (tmp_path / "catalog.json").symlink_to(tmp_path / "saved")
    with pytest.raises(PathError):
        read_catalog(tmp_path)
    assert (tmp_path / "saved").read_bytes() == original


def voice_evidence_payload(**changes):
    payload = {
        "threshold": 8,
        "user_events": 3,
        "genuine": 1,
        "harness_injected": 1,
        "unclassified": 1,
        "sufficient": False,
        "candidates": [
            {"session_id": "a" * 64, "event_id": "e1", "characters": 5, "preview": "hello"}
        ],
        "excluded": [
            {
                "session_id": "a" * 64,
                "event_id": "e2",
                "classification": "harness_injected",
                "reasons": ["system_reminder"],
            },
            {"session_id": "a" * 64, "event_id": "e3", "classification": "unclassified"},
        ],
        "truncated": False,
    }
    return payload | changes


def test_voice_evidence_accepts_consistent_measured_counts():
    evidence = VoiceEvidence.model_validate_json(json.dumps(voice_evidence_payload()))
    assert evidence.user_events == 3
    assert evidence.sufficient is False
    assert evidence.excluded[0].reasons == ("system_reminder",)
    assert evidence.excluded[1].reasons == ()
    sufficient = VoiceEvidence.model_validate_json(
        json.dumps(
            voice_evidence_payload(genuine=8, user_events=10, sufficient=True, truncated=True)
        )
    )
    assert sufficient.sufficient is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"user_events": 4}, "total the user-role event count"),
        ({"sufficient": True}, "sufficiency must follow"),
        ({"genuine": 8, "user_events": 10}, "sufficiency must follow"),
        ({"candidates": []}, "truncation must follow"),
        ({"truncated": True}, "truncation must follow"),
        (
            {
                "candidates": [
                    {"session_id": "a" * 64, "event_id": "e1", "characters": 5, "preview": "x"},
                    {"session_id": "a" * 64, "event_id": "e4", "characters": 5, "preview": "y"},
                ]
            },
            "exceed their counted classification",
        ),
    ],
)
def test_voice_evidence_rejects_counts_that_contradict_each_other(changes, message):
    with pytest.raises(ValidationError, match=message):
        VoiceEvidence.model_validate_json(json.dumps(voice_evidence_payload(**changes)))


@pytest.mark.parametrize(
    ("reasons", "message"),
    [
        ([], "must record a recognized wrapper"),
        (["system_reminder", "system_reminder"], "must be unique"),
    ],
)
def test_harness_injected_events_must_name_a_unique_recognized_wrapper(reasons, message):
    with pytest.raises(ValidationError, match=message):
        ExcludedUserEvent.model_validate_json(
            json.dumps(
                {
                    "session_id": "a" * 64,
                    "event_id": "e2",
                    "classification": "harness_injected",
                    "reasons": reasons,
                }
            )
        )


def session_evidence(**changes):
    return {
        "session_id": "a" * 64,
        "user_events": 3,
        "genuine": 1,
        "harness_injected": 1,
        "unclassified": 1,
        "genuine_characters": 5,
        "median_genuine_characters": 5,
    } | changes


def test_voice_evidence_accepts_a_consistent_per_session_breakdown():
    evidence = VoiceEvidence.model_validate_json(
        json.dumps(voice_evidence_payload(sessions=[session_evidence()]))
    )
    assert [item.session_id for item in evidence.sessions] == ["a" * 64]
    assert evidence.sessions[0].genuine == 1
    assert evidence.sessions[0].genuine_characters == 5
    assert evidence.sessions[0].median_genuine_characters == 5


def test_voice_evidence_reads_records_without_a_per_session_breakdown():
    assert VoiceEvidence.model_validate_json(json.dumps(voice_evidence_payload())).sessions == ()


@pytest.mark.parametrize(
    ("sessions", "message"),
    [
        ([session_evidence(genuine=2, user_events=4)], "total the selection counts"),
        ([session_evidence(harness_injected=0, user_events=2)], "total the selection counts"),
        (
            [
                session_evidence(unclassified=0, user_events=2),
                session_evidence(
                    user_events=1,
                    genuine=0,
                    harness_injected=0,
                    genuine_characters=0,
                    median_genuine_characters=0,
                ),
            ],
            "describe each session once",
        ),
    ],
)
def test_per_session_evidence_must_total_the_selection_counts(sessions, message):
    with pytest.raises(ValidationError, match=message):
        VoiceEvidence.model_validate_json(json.dumps(voice_evidence_payload(sessions=sessions)))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"genuine": 0, "user_events": 2}, "require at least one genuine user message"),
        ({"median_genuine_characters": 9}, "cannot exceed their total"),
        ({"user_events": 9}, "total the user-role event count"),
    ],
)
def test_session_evidence_rejects_unmeasured_character_counts(changes, message):
    with pytest.raises(ValidationError, match=message):
        SessionEvidence.model_validate_json(json.dumps(session_evidence(**changes)))


def triage_decision_payload(**changes):
    return {
        "session_id": "a" * 64,
        "kind": "feature",
        "grade": "medium",
        "chosen": True,
        "reason": "A two-turn feature request.",
    } | changes


def triage_variety_payload(**changes):
    return {
        "sessions": 2,
        "triaged": 2,
        "untriaged": 0,
        "chosen": 2,
        "rejected": 0,
        "kinds": {"feature_medium": 1, "bugfix_easy": 1},
        "repositories": {"/repos/alpha": 1, "/repos/beta": 1},
        "unknown_repositories": 0,
        "varied": True,
    } | changes


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "extraneous"}, "carries a grade"),
        ({"grade": None}, "carries a grade"),
        ({"grade": "easy"}, "its kind's own scale"),
        ({"kind": "bugfix", "grade": "large"}, "its kind's own scale"),
        ({"kind": "config_change", "grade": "small"}, "carries a grade"),
    ],
)
def test_triage_decision_pairs_each_kind_with_its_own_scale(changes, message):
    with pytest.raises(ValidationError, match=message):
        TriageDecision.model_validate_json(json.dumps(triage_decision_payload(**changes)))


def test_triage_decision_labels_each_vocabulary_category():
    graded = [
        ("config_change", None),
        ("feature", "small"),
        ("feature", "medium"),
        ("feature", "large"),
        ("bugfix", "easy"),
        ("bugfix", "medium"),
        ("bugfix", "hard"),
        ("extraneous", None),
    ]
    categories = [
        TriageDecision.model_validate_json(
            json.dumps(triage_decision_payload(kind=kind, grade=grade))
        ).category
        for kind, grade in graded
    ]
    assert categories == [
        "config_change",
        "feature_small",
        "feature_medium",
        "feature_large",
        "bugfix_easy",
        "bugfix_medium",
        "bugfix_hard",
        "extraneous",
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sessions": 3}, "total the selected sessions"),
        ({"chosen": 1}, "total the triaged sessions"),
        ({"kinds": {"feature_medium": 1}}, "total the chosen sessions"),
        ({"repositories": {"/repos/alpha": 1}}, "total the chosen sessions"),
        ({"unknown_repositories": 1}, "total the chosen sessions"),
        ({"varied": False}, "chosen kind and repository spread"),
        ({"kinds": {"feature_medium": 2}, "varied": True}, "chosen kind and repository spread"),
    ],
)
def test_triage_variety_counts_must_measure_the_chosen_sessions(changes, message):
    with pytest.raises(ValidationError, match=message):
        TriageVariety.model_validate_json(json.dumps(triage_variety_payload(**changes)))


def test_triage_evidence_describes_each_selected_session_once():
    entry = triage_decision_payload() | {"repository": "/repos/alpha"}
    variety = triage_variety_payload(
        sessions=1,
        triaged=1,
        chosen=1,
        kinds={"feature_medium": 1},
        repositories={"/repos/alpha": 1},
        varied=False,
    )
    assert (
        TriageEvidence.model_validate_json(
            json.dumps({"entries": [entry], "untriaged": [], "variety": variety})
        ).variety.varied
        is False
    )
    with pytest.raises(ValidationError, match="at most once"):
        TriageEvidence.model_validate_json(
            json.dumps(
                {
                    "entries": [entry],
                    "untriaged": ["a" * 64],
                    "variety": variety | {"sessions": 2, "untriaged": 1},
                }
            )
        )
    with pytest.raises(ValidationError, match="total the counted sessions"):
        TriageEvidence.model_validate_json(
            json.dumps({"entries": [], "untriaged": [], "variety": variety})
        )
    with pytest.raises(ValidationError, match="total the counted chosen sessions"):
        TriageEvidence.model_validate_json(
            json.dumps(
                {
                    "entries": [entry | {"chosen": False}],
                    "untriaged": [],
                    "variety": variety,
                }
            )
        )


def test_catalog_triage_must_name_a_known_selection(tmp_path):
    payload = {
        "triage": {
            "work": {
                "name": "work",
                "selection_id": "a" * 64,
                "decisions": {"a" * 64: triage_decision_payload()},
            }
        }
    }
    content = json.dumps(payload).encode()
    (tmp_path / "catalog.json").write_bytes(content)
    with pytest.raises(InputError, match="Invalid schema"), edit_catalog(tmp_path, 0):
        pytest.fail("unknown selection accepted")
    assert (tmp_path / "catalog.json").read_bytes() == content
