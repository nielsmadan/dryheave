import shutil
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from dryheave.cases import (
    FactVerifier,
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
GREETING_VERIFIER = FactVerifier(
    command=CommandSpec(argv=(sys.executable, "{verifier}/check.py")),
    entrypoint="check.py",
    expected_stdout="GREETING_CHECK_EXECUTED",
)


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
    return draft.model_copy(
        update={
            "persona": persona,
            "intent_confirmed": True,
            "facts_reviewed": True,
            "unresolved_issues": (),
            "phases": ("fix",),
            "hidden_files": {"check.py": str(verifier)},
            "allowed_facts": (
                TaskFact(
                    fact_id="punctuation",
                    phase="fix",
                    text="Use a comma after Hello.",
                    curator_authored=True,
                    verifier=GREETING_VERIFIER,
                ),
            ),
        }
    )


@pytest.mark.integration
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
    assert "At least one requirement fact is required." in issues
    assert "Declare at least one phase in phases." in issues
    assert any("subject-safe" in issue for issue in issues)
    with pytest.raises(InputError):
        freeze_case(store, draft, tmp_path)


@pytest.mark.integration
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


@pytest.mark.integration
def test_grading_rejects_empty_and_missing_checks(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    draft = curated(store, historical_repo, tmp_path)
    with pytest.raises(ValidationError, match="hidden entrypoint"):
        FactVerifier(
            command=CommandSpec(argv=("true",)), entrypoint="check.py", expected_stdout="DONE"
        )
    with pytest.raises(ValidationError, match="execution evidence"):
        FactVerifier(
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


@pytest.mark.integration
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


@pytest.mark.integration
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
    with pytest.raises(ValidationError, match="requirement fact"):
        FrozenCase.model_validate(case.model_dump() | {"allowed_facts": ()})


@pytest.mark.integration
def test_context_only_facts_cannot_freeze(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    context = TaskFact(
        fact_id="audience", kind="context", text="Users are in Germany.", curator_authored=True
    )
    draft = curated(store, historical_repo, tmp_path).model_copy(
        update={"allowed_facts": (context,), "hidden_files": {}}
    )
    assert validate_case(store, draft, tmp_path) == (
        "At least one requirement fact is required.",
        "Phase fix has no requirement fact.",
    )
    with pytest.raises(InputError, match="requirement fact"):
        freeze_case(store, draft, tmp_path)


@pytest.mark.integration
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


@pytest.mark.integration
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
    with pytest.raises(ValidationError):
        FactVerifier(
            command=CommandSpec(argv=("python3", "{verifier}/check.py")),
            entrypoint="check.py",
            expected_stdout="CHECKS_PASSED",
            expected_failure_stdout=marker,
        )


REQUIREMENT = {
    "fact_id": "punctuation",
    "kind": "requirement",
    "phase": "fix",
    "text": "Use a comma after Hello.",
    "check": "greet('Ada') returns 'Hello, Ada'.",
    "disclosure": "on_request",
    "verifier": None,
    "evidence": [],
    "curator_authored": True,
}

CONTENT = {
    "schema_version": 2,
    "title": "Fix the greeting",
    "initial_prompt": "Fix the greeting punctuation.",
    "allowed_facts": [REQUIREMENT],
    "phases": ["fix"],
    "evidence": [
        {
            "session_id": "a" * 64,
            "event_id": "e1",
            "visibility": "subject",
            "excerpt": "Fix the greeting punctuation.",
        }
    ],
    "setup": [],
    "tool_requirements": [],
    "budgets": {"subject_seconds": 180, "setup_seconds": 120},
    "capture_policy": {
        "max_files": 20000,
        "max_bytes": 64 * 1024 * 1024,
        "max_depth": 32,
        "exclude": [],
        "include_ignored": [],
    },
    "criteria": [
        {
            "criterion_id": "quality",
            "kind": "judge",
            "rubric": "The greeting keeps its public interface.",
        }
    ],
    "intent_confirmed": True,
    "facts_reviewed": True,
    "unresolved_issues": [],
}
FROZEN = {
    "repository_id": "b" * 64,
    "persona_id": "c" * 64,
    "source_session_id": "a" * 64,
    "source_path_hash": "d" * 64,
    "source_start_event": "e1",
    "source_end_event": "e1",
    "hidden_files": {},
    "reference_patch_file": None,
}


def test_frozen_case_and_draft_serialize_byte_identically() -> None:
    from dryheave.cases import CaseDraft
    from dryheave.serialization import canonical_json, parse_model

    frozen = CONTENT | FROZEN
    draft = CONTENT | {
        "source_session_id": "a" * 64,
        "start_event": "e1",
        "end_event": "e1",
        "repository_id": "b" * 64,
        "persona": None,
        "persona_id": "c" * 64,
        "hidden_files": {},
        "reference_patch": None,
    }
    assert canonical_json(parse_model(canonical_json(frozen), FrozenCase)) == canonical_json(frozen)
    assert parse_model(canonical_json(draft), CaseDraft).model_dump(mode="json") == draft


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 1}, "schema_version"),
        ({"checks": []}, "checks"),
        ({"budgets": {"max_turns": 4}}, "max_turns"),
        (
            {"criteria": [CONTENT["criteria"][0] | {"required": True}]},
            "required",
        ),
        (
            {"criteria": [{"criterion_id": "greeting", "kind": "deterministic"}]},
            "criteria",
        ),
        (
            {"workflow": [{"kind": "inspect", "step_id": "check", "criteria": ["quality"]}]},
            "workflow",
        ),
    ],
)
def test_case_content_refuses_removed_v1_shapes(changes, message) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        CaseContent.model_validate_json(canonical_json(CONTENT | changes))


@pytest.mark.parametrize(
    ("fact", "message"),
    [
        (REQUIREMENT | {"phase": None}, "requirement fact punctuation must name its phase"),
        (
            REQUIREMENT | {"kind": "optional", "phase": None},
            "optional fact punctuation must name its phase",
        ),
        (REQUIREMENT | {"check": None}, "requirement fact punctuation needs a check or a verifier"),
        (
            REQUIREMENT | {"kind": "optional", "check": None},
            "optional fact punctuation needs a check or a verifier",
        ),
        (
            REQUIREMENT | {"kind": "context", "check": None},
            "context fact punctuation cannot have a phase",
        ),
        (
            REQUIREMENT | {"kind": "context", "phase": None},
            "context fact punctuation cannot have a check",
        ),
        (
            REQUIREMENT
            | {
                "kind": "context",
                "phase": None,
                "check": None,
                "verifier": GREETING_VERIFIER.model_dump(mode="json"),
            },
            "context fact punctuation cannot have a verifier",
        ),
        (REQUIREMENT | {"check": "TODO: describe"}, "substantive pass condition"),
    ],
)
def test_task_fact_rejects_invalid_kind_shapes(fact, message) -> None:
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        TaskFact.model_validate_json(canonical_json(fact))


def test_task_fact_accepts_a_verifier_in_place_of_a_check() -> None:
    from dryheave.serialization import canonical_json

    fact = TaskFact.model_validate_json(
        canonical_json(
            REQUIREMENT | {"check": None, "verifier": GREETING_VERIFIER.model_dump(mode="json")}
        )
    )
    assert (fact.check, fact.verifier) == (None, GREETING_VERIFIER)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"allowed_facts": [REQUIREMENT | {"phase": "review"}]},
            "fact punctuation names undeclared phase review; declared phases: fix",
        ),
        ({"phases": ["fix", "fix"]}, "phase names must be unique"),
        (
            {"workflow": [{"step_id": "review", "prompt": "/code-review", "after_phase": "ship"}]},
            "workflow step review runs after undeclared phase ship",
        ),
        (
            {
                "phases": ["fix", "polish"],
                "allowed_facts": [
                    REQUIREMENT,
                    REQUIREMENT | {"fact_id": "docs", "phase": "polish"},
                ],
                "workflow": [
                    {"step_id": "docs", "prompt": "Update the docs.", "after_phase": "polish"},
                    {"step_id": "review", "prompt": "/code-review", "after_phase": "fix"},
                ],
            },
            "workflow step review runs after phase fix, earlier than the step before it",
        ),
        ({"disabled_rubrics": ["style"]}, "default rubric IDs: style"),
        ({"disabled_rubrics": ["test-quality", "test-quality"]}, "must be unique"),
    ],
)
def test_case_content_rejects_inconsistent_phases_and_steps(changes, message) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        CaseContent.model_validate_json(canonical_json(CONTENT | changes))


def test_task_fact_accepts_check_starting_with_todo() -> None:
    from dryheave.serialization import canonical_json

    fact = TaskFact.model_validate_json(
        canonical_json(REQUIREMENT | {"check": "Todo items are listed newest first."})
    )
    assert fact.check == "Todo items are listed newest first."


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"phases": [], "allowed_facts": []}, "Declare at least one phase in phases."),
        ({"allowed_facts": []}, "At least one requirement fact is required."),
        (
            {"allowed_facts": [REQUIREMENT | {"kind": "optional"}]},
            "At least one requirement fact is required.",
        ),
        ({"phases": ["fix", "polish"]}, "Phase polish has no requirement fact."),
        (
            {"allowed_facts": [REQUIREMENT, REQUIREMENT]},
            "Allowed fact IDs must be unique.",
        ),
        (
            {"allowed_facts": [REQUIREMENT | {"fact_id": "code-quality"}]},
            "Fact ID code-quality collides with a grading criterion ID.",
        ),
    ],
)
def test_frozen_case_requires_complete_phases(changes, message) -> None:
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        FrozenCase.model_validate_json(canonical_json(CONTENT | FROZEN | changes))


def test_frozen_case_requires_hidden_verifier_entrypoint() -> None:
    from dryheave.serialization import canonical_json

    fact = REQUIREMENT | {"verifier": GREETING_VERIFIER.model_dump(mode="json")}
    with pytest.raises(ValidationError, match="hidden verifier entrypoint is missing"):
        FrozenCase.model_validate_json(canonical_json(CONTENT | FROZEN | {"allowed_facts": [fact]}))


def test_phases_and_ordered_prompt_steps_round_trip() -> None:
    from dryheave.cases import CaseContent, PromptStep
    from dryheave.serialization import canonical_json, parse_model

    payload = CONTENT | {
        "phases": ["fix", "polish"],
        "allowed_facts": [
            REQUIREMENT,
            REQUIREMENT | {"fact_id": "docs", "phase": "polish", "kind": "requirement"},
            REQUIREMENT | {"fact_id": "tone", "phase": "polish", "kind": "optional"},
            REQUIREMENT | {"fact_id": "audience", "kind": "context", "phase": None, "check": None},
        ],
        "workflow": [
            {"kind": "prompt", "step_id": "review", "prompt": "/code-review", "after_phase": "fix"},
            {
                "kind": "prompt",
                "step_id": "hint",
                "prompt": "fix the findings",
                "answer_hint": "fix Critical and Should Fix findings",
                "after_phase": "fix",
            },
            {
                "kind": "prompt",
                "step_id": "docs",
                "prompt": "Update docs.",
                "after_phase": "polish",
            },
        ],
        "disabled_rubrics": ["test-quality"],
    }
    content = parse_model(canonical_json(payload), CaseContent)
    assert content.workflow[0] == PromptStep(
        step_id="review", prompt="/code-review", after_phase="fix"
    )
    assert [fact.kind for fact in content.allowed_facts] == [
        "requirement",
        "requirement",
        "optional",
        "context",
    ]
    assert canonical_json(content) == canonical_json(payload)
    assert "answer_hint" not in canonical_json(content.workflow[0]).decode()


def test_case_without_workflow_omits_empty_optional_fields(store: ObjectStore) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json, parse_model

    case = parse_model(canonical_json(CONTENT), CaseContent)
    assert {"workflow", "metrics", "disabled_rubrics"}.isdisjoint(case.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("prompt", "slash", "skill_name"),
    [
        ("  /code-review --quick", True, "code-review"),
        ("/simplify", True, "simplify"),
        (" \n/release-notes", True, "release-notes"),
        ("/code-review\nfocus on auth", True, "code-review"),
        ("/code-review\tfocus on auth", True, "code-review"),
        ("fix the findings", False, None),
    ],
)
def test_prompt_step_names_its_slash_skill(
    prompt: str, slash: bool, skill_name: str | None
) -> None:
    from dryheave.cases import PromptStep, is_slash

    step = PromptStep(step_id="review", prompt=prompt, after_phase="fix")
    assert (step.slash, is_slash(prompt), step.skill_name) == (slash, slash, skill_name)


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (
            [
                {"step_id": "dup", "prompt": "/code-review", "after_phase": "fix"},
                {"step_id": "dup", "prompt": "update the docs", "after_phase": "fix"},
            ],
            "workflow step IDs must be unique",
        ),
        (
            [{"step_id": "no-turn", "prompt": "/clear", "after_phase": "fix"}],
            "command with no model turn",
        ),
        (
            [{"step_id": "no-turn", "prompt": "/compact please", "after_phase": "fix"}],
            "command with no model turn",
        ),
        (
            [{"step_id": "mention", "prompt": " $code-review --quick", "after_phase": "fix"}],
            (
                r"step mention starts with '\$': write skill steps as /name; "
                r"Codex receives them as \$name"
            ),
        ),
        (
            [{"step_id": "shell", "prompt": "!git status", "after_phase": "fix"}],
            "step shell starts with '!': write skill steps as /name",
        ),
        ([{"step_id": "late", "prompt": "Review it."}], "after_phase"),
    ],
)
def test_case_content_rejects_invalid_workflow_steps(workflow, message) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        CaseContent.model_validate_json(canonical_json(CONTENT | {"workflow": workflow}))


def test_fact_selectors_split_by_kind_and_verifier() -> None:
    from dryheave.cases import (
        CaseContent,
        graded_facts,
        judged_facts,
        requirement_facts,
        verifier_facts,
    )
    from dryheave.serialization import canonical_json, parse_model

    verified = REQUIREMENT | {
        "fact_id": "verified",
        "check": None,
        "verifier": GREETING_VERIFIER.model_dump(mode="json"),
    }
    optional = REQUIREMENT | {"fact_id": "tone", "kind": "optional"}
    context = REQUIREMENT | {"fact_id": "audience", "kind": "context", "phase": None, "check": None}
    case = parse_model(
        canonical_json(CONTENT | {"allowed_facts": [REQUIREMENT, verified, optional, context]}),
        CaseContent,
    )

    def ids(facts) -> list[str]:
        return [fact.fact_id for fact in facts]

    assert ids(graded_facts(case)) == ["punctuation", "verified", "tone"]
    assert ids(requirement_facts(case)) == ["punctuation", "verified"]
    assert ids(verifier_facts(case)) == ["verified"]
    assert ids(judged_facts(case)) == ["punctuation", "tone"]


def test_a_case_needs_a_judge_for_judged_facts_or_rubric_criteria() -> None:
    from dryheave.cases import CaseContent, needs_judge
    from dryheave.serialization import canonical_json, parse_model

    verified = REQUIREMENT | {"check": None, "verifier": GREETING_VERIFIER.model_dump(mode="json")}
    rubric = {"criterion_id": "quality", "kind": "judge", "rubric": "Keeps the interface."}

    def case(**changes) -> CaseContent:
        return parse_model(canonical_json(CONTENT | changes), CaseContent)

    assert needs_judge(case(allowed_facts=[REQUIREMENT], criteria=[])) is True
    assert needs_judge(case(allowed_facts=[verified], criteria=[rubric])) is True
    assert needs_judge(case(allowed_facts=[verified], criteria=[])) is False


METRICS_COMMAND = {
    "metrics_id": "tests",
    "description": "Count tests and measure line coverage of the final workspace.",
    "command": {"argv": ["/bin/sh", "{verifier}/metrics.sh"]},
    "entrypoint": "metrics.sh",
}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"metrics": [METRICS_COMMAND, METRICS_COMMAND]}, "metrics IDs must be unique"),
        (
            {"metrics": [METRICS_COMMAND | {"command": {"argv": ["/bin/sh", "metrics.sh"]}}]},
            "must invoke the supplied hidden entrypoint",
        ),
        (
            {"metrics": [METRICS_COMMAND | {"description": "TODO: describe the metrics"}]},
            "metrics description cannot be a placeholder",
        ),
    ],
)
def test_case_content_rejects_invalid_metrics_commands(changes, message) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        CaseContent.model_validate_json(canonical_json(CONTENT | changes))


def test_frozen_case_requires_hidden_metrics_entrypoint() -> None:
    from dryheave.serialization import canonical_json

    frozen = CONTENT | FROZEN | {"metrics": [METRICS_COMMAND]}
    with pytest.raises(ValidationError, match="hidden metrics entrypoint is missing"):
        FrozenCase.model_validate_json(canonical_json(frozen | {"hidden_files": {}}))
    case = FrozenCase.model_validate_json(
        canonical_json(frozen | {"hidden_files": {"metrics.sh": "verifiers/metrics.sh"}})
    )
    assert case.metrics[0].command.argv == ("/bin/sh", "{verifier}/metrics.sh")


@pytest.mark.integration
def test_frozen_case_keeps_metrics_command_and_its_hidden_script(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    from dryheave.cases import MetricsCommand
    from dryheave.serialization import canonical_json

    draft = curated(store, historical_repo, tmp_path)
    command = MetricsCommand.model_validate_json(canonical_json(METRICS_COMMAND))
    with_metrics = draft.model_copy(update={"metrics": (command,)})
    assert validate_case(store, with_metrics, tmp_path) == (
        "Hidden metrics entrypoint is missing: metrics.sh.",
    )
    script = tmp_path / "metrics.sh"
    script.write_text('printf \'{"metrics": []}\' > "$DRYHEAVE_METRICS_OUTPUT"\n')
    with_metrics = with_metrics.model_copy(
        update={"hidden_files": {**draft.hidden_files, "metrics.sh": str(script)}}
    )
    identifier = freeze_case(store, with_metrics, tmp_path)
    case = load_frozen_case(store, identifier)
    assert case.metrics == (command,)
    assert store.read_blobs(identifier)[case.hidden_files["metrics.sh"]] == script.read_bytes()
    assert freeze_case(store, draft, tmp_path) != identifier
