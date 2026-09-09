import shutil
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from dryheave.cases import (
    DeterministicCriterion,
    FrozenCase,
    TaskFact,
    draft_case,
    freeze_case,
    load_frozen_case,
    subject_context,
    validate_case,
)
from dryheave.errors import InputError
from dryheave.logs.base import EvidenceExcerpt
from dryheave.logs.service import import_session
from dryheave.models import AgentKind, CommandSpec, ObjectKind
from dryheave.personas import Persona, freeze_persona, subject_persona
from dryheave.repositories import capture_repository, load_repository, materialize_repository
from dryheave.storage import ObjectStore

FIXTURES = Path(__file__).parent / "fixtures"


def curated(store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path):
    source, baseline, _, _ = historical_repo
    session_id = import_session(store, FIXTURES / "codex-recorded.jsonl", AgentKind.CODEX)
    repo_id = capture_repository(store, source, baseline)
    draft = draft_case(store, session_id, repository_id=repo_id)
    persona = Persona(
        name="Precise user",
        instructions="Ask for the details needed to implement the greeting.",
        disclosure_policy="Reveal only the supplied allowed facts.",
        unknown_answer_policy="Say when the supplied facts do not answer a question.",
        examples=draft.evidence,
        reviewed_subject_safe=True,
    )
    verifier = tmp_path / "check.py"
    verifier.write_text(
        'from greet import greet\nassert greet("Ada") == "Hello, Ada"\nprint("GREETING_CHECK_EXECUTED")\n'
    )
    criterion = DeterministicCriterion(
        criterion_id="greeting",
        description="The greeting uses the requested comma and name.",
        command=CommandSpec(argv=(sys.executable, "{verifier}/check.py")),
        entrypoint="check.py",
        expected_stdout="GREETING_CHECK_EXECUTED",
    )
    return draft.model_copy(
        update={
            "persona": persona,
            "intent_confirmed": True,
            "facts_reviewed": True,
            "unresolved_issues": (),
            "criteria": (criterion,),
            "hidden_files": {"check.py": str(verifier)},
            "allowed_facts": (
                TaskFact(
                    fact_id="punctuation", text="Use a comma after Hello.", curator_authored=True
                ),
            ),
        }
    )


def test_frozen_case_is_self_contained_without_full_sessions(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    identifier = freeze_case(store, draft, tmp_path)
    case = store.load(identifier, FrozenCase, kind=ObjectKind.CASE)
    assert store.get(identifier).references == tuple(sorted((case.repository_id, case.persona_id)))
    assert store.get(case.persona_id).references == ()
    other = ObjectStore(tmp_path / "portable-store")
    for item in (identifier, case.repository_id, case.persona_id):
        target = other.object_path(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(store.object_path(item), target)
    assert other.verify(identifier).kind == ObjectKind.CASE
    assert other.read_blob(identifier, "verifiers/check.py").endswith(
        b'print("GREETING_CHECK_EXECUTED")\n'
    )
    materialize_repository(other, case.repository_id, tmp_path / "replayed")
    assert (tmp_path / "replayed/greet.py").read_text().startswith("def greet")
    assert case.evidence[0].session_id == draft.source_session_id
    assert len(case.source_path_hash) == 64
    (tmp_path / "check.py").write_text("changed later")
    assert other.read_blob(identifier, "verifiers/check.py").startswith(b"from greet")


def test_draft_requires_real_curation(store: ObjectStore, tmp_path: Path) -> None:
    session_id = import_session(store, FIXTURES / "codex-recorded.jsonl", AgentKind.CODEX)
    draft = draft_case(store, session_id)
    issues = validate_case(store, draft, tmp_path)
    assert any("starting commit" in issue for issue in issues)
    assert any("grading" in issue or "verifier" in issue for issue in issues)
    assert any("subject-safe" in issue for issue in issues)
    with pytest.raises(InputError):
        freeze_case(store, draft, tmp_path)


def test_tampered_evidence_and_invalid_range(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    bad = draft.evidence[0].model_copy(update={"excerpt": "Invented source quote"})
    with pytest.raises(InputError, match="does not match"):
        freeze_case(store, draft.model_copy(update={"evidence": (bad,)}), tmp_path)
    with pytest.raises(InputError, match="event range"):
        freeze_case(store, draft.model_copy(update={"end_event": "missing"}), tmp_path)
    with pytest.raises(InputError, match="at or after"):
        draft_case(store, draft.source_session_id, end="missing")


def test_grading_rejects_empty_and_missing_checks(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    with pytest.raises(ValidationError, match="hidden entrypoint"):
        DeterministicCriterion(
            criterion_id="fake",
            description="Always succeeds without checking anything",
            command=CommandSpec(argv=("true",)),
            entrypoint="check.py",
            expected_stdout="DONE",
        )
    with pytest.raises(ValidationError, match="execution evidence"):
        DeterministicCriterion(
            criterion_id="fake",
            description="Always succeeds without checking anything",
            command=CommandSpec(argv=("python3", "{verifier}/check.py")),
            entrypoint="check.py",
            expected_stdout=" ",
        )
    (tmp_path / "check.py").write_text("")
    assert any("empty" in issue for issue in validate_case(store, draft, tmp_path))
    assert any(
        "missing" in issue
        for issue in validate_case(store, draft.model_copy(update={"hidden_files": {}}), tmp_path)
    )


def test_subject_projections_exclude_hidden_inputs(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    identifier = freeze_case(store, draft, tmp_path)
    case = store.load(identifier, FrozenCase, kind=ObjectKind.CASE)
    projection = subject_context(case)
    assert set(projection) == {"initial_prompt", "allowed_facts"}
    assert projection["allowed_facts"] == [
        {"fact_id": "punctuation", "text": "Use a comma after Hello.", "disclosure": "on_request"}
    ]
    persona = store.load(case.persona_id, Persona, kind=ObjectKind.PERSONA)
    assert set(subject_persona(persona)) == {
        "instructions",
        "disclosure_policy",
        "unknown_answer_policy",
        "examples",
    }


def test_persona_requires_safe_examples_and_verifiable_provenance(store: ObjectStore) -> None:
    session_id = import_session(store, FIXTURES / "codex-recorded.jsonl", AgentKind.CODEX)
    draft = draft_case(store, session_id)
    assert draft.persona is not None
    with pytest.raises(InputError, match="curator-authored"):
        freeze_persona(store, draft.persona)
    with pytest.raises(ValidationError, match="subject-safe"):
        TaskFact(
            fact_id="secret",
            text="Do this",
            evidence=(
                EvidenceExcerpt(
                    session_id=session_id,
                    event_id=draft.start_event,
                    visibility="judge",
                    excerpt="Add a greeting command.",
                ),
            ),
        )


def test_replay_validation_needs_no_sessions_and_rejects_bad_closure(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    identifier = freeze_case(store, draft, tmp_path)
    case = load_frozen_case(store, identifier)
    bad = store.put(
        ObjectKind.CASE,
        case,
        files={"verifiers/check.py": store.read_blob(identifier, "verifiers/check.py")},
    )
    with pytest.raises(InputError, match="reference closure"):
        load_frozen_case(store, bad)
    missing = store.put(ObjectKind.CASE, case, references=(case.repository_id, case.persona_id))
    with pytest.raises(InputError, match="blob map"):
        load_frozen_case(store, missing)
    shutil.rmtree(store.object_path(draft.source_session_id))
    assert load_frozen_case(store, identifier).title == case.title
    with pytest.raises(ValidationError, match="facts_reviewed"):
        FrozenCase.model_validate(case.model_dump() | {"facts_reviewed": False})
    with pytest.raises(ValidationError, match="required"):
        FrozenCase.model_validate(case.model_dump() | {"criteria": ()})


def test_reviewed_empty_facts_can_freeze_and_load(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path).model_copy(
        update={
            "initial_prompt": "Add a greet command that prints Hello, followed by the name.",
            "allowed_facts": (),
        }
    )
    assert validate_case(store, draft, tmp_path) == ()
    case = load_frozen_case(store, freeze_case(store, draft, tmp_path))
    assert case.allowed_facts == ()
    assert case.facts_reviewed is True
    assert subject_context(case) == {"initial_prompt": draft.initial_prompt, "allowed_facts": []}
    unreviewed = draft.model_copy(update={"facts_reviewed": False})
    assert validate_case(store, unreviewed, tmp_path) == (
        "Curator must review allowed facts and mark facts_reviewed.",
    )
    with pytest.raises(InputError, match="facts_reviewed"):
        freeze_case(store, unreviewed, tmp_path)


@pytest.mark.parametrize("invalid", ["archive", "pack", "extra_blob", "reference"])
def test_authoring_rejects_invalid_repository_inputs_before_publication(
    invalid: str,
    store: ObjectStore,
    historical_repo: tuple[Path, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    assert draft.repository_id is not None
    snapshot = load_repository(store, draft.repository_id)
    files = {
        name: store.read_blob(draft.repository_id, name) for name in ("tree.tar", "ancestry.pack")
    }
    if invalid in {"archive", "pack"}:
        files.pop("tree.tar" if invalid == "archive" else "ancestry.pack")
    elif invalid == "extra_blob":
        files["undeclared"] = b"unexpected input"
    identifier = store.put(
        ObjectKind.REPOSITORY,
        snapshot,
        files=files,
        references=(draft.source_session_id,) if invalid == "reference" else (),
    )
    draft = draft.model_copy(update={"repository_id": identifier})
    publish = Mock(wraps=store.put)
    monkeypatch.setattr(store, "put", publish)
    with pytest.raises(InputError, match="self-contained snapshot inputs"):
        validate_case(store, draft, tmp_path)
    with pytest.raises(InputError, match="self-contained snapshot inputs"):
        freeze_case(store, draft, tmp_path)
    publish.assert_not_called()


@pytest.mark.parametrize("invalid", ["blob", "reference"])
def test_authoring_rejects_invalid_persona_inputs_before_publication(
    invalid: str,
    store: ObjectStore,
    historical_repo: tuple[Path, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    assert draft.persona is not None
    identifier = store.put(
        ObjectKind.PERSONA,
        draft.persona,
        files={"undeclared": b"unexpected input"} if invalid == "blob" else {},
        references=(draft.source_session_id,) if invalid == "reference" else (),
    )
    draft = draft.model_copy(update={"persona": None, "persona_id": identifier})
    publish = Mock(wraps=store.put)
    monkeypatch.setattr(store, "put", publish)
    with pytest.raises(InputError, match="self-contained curated text"):
        validate_case(store, draft, tmp_path)
    with pytest.raises(InputError, match="self-contained curated text"):
        freeze_case(store, draft, tmp_path)
    publish.assert_not_called()


@pytest.mark.parametrize("marker", ["", " ", "x" * 1001])
def test_failure_execution_marker_requires_bounded_nonblank_text(marker):
    from pydantic import ValidationError

    from dryheave.cases import DeterministicCriterion
    from dryheave.models import CommandSpec

    with pytest.raises(ValidationError):
        DeterministicCriterion(
            criterion_id="check",
            description="Check the explicit failure evidence contract.",
            command=CommandSpec(argv=("python3", "{verifier}/check.py")),
            entrypoint="check.py",
            expected_stdout="CHECKS_PASSED",
            expected_failure_stdout=marker,
        )
