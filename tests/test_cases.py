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
    assert any("grading" in issue or "verifier" in issue for issue in issues)
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
    with pytest.raises(ValidationError, match="required"):
        FrozenCase.model_validate(case.model_dump() | {"criteria": ()})


@pytest.mark.integration
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


LEGACY_CONTENT = {
    "schema_version": 1,
    "title": "Fix the greeting",
    "initial_prompt": "Fix the greeting punctuation.",
    "allowed_facts": [],
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
    "budgets": {"max_turns": 4, "subject_seconds": 180, "setup_seconds": 120},
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
            "required": True,
            "rubric": "The greeting keeps its public interface.",
        }
    ],
    "intent_confirmed": True,
    "facts_reviewed": True,
    "unresolved_issues": [],
}


def test_legacy_frozen_case_and_draft_serialize_byte_identically() -> None:
    from dryheave.cases import CaseDraft
    from dryheave.serialization import canonical_json, parse_model

    frozen = LEGACY_CONTENT | {
        "repository_id": "b" * 64,
        "persona_id": "c" * 64,
        "source_session_id": "a" * 64,
        "source_path_hash": "d" * 64,
        "source_start_event": "e1",
        "source_end_event": "e1",
        "hidden_files": {},
        "reference_patch_file": None,
    }
    draft = LEGACY_CONTENT | {
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
        (
            {
                "checks": [
                    {"check_id": "asked", "question": "Did the agent ask?", "expect": "yes"},
                    {"check_id": "asked", "question": "Did the agent ask?", "expect": "no"},
                ]
            },
            "check IDs must be unique",
        ),
        (
            {
                "checks": [
                    {
                        "check_id": "kept",
                        "question": "Was the setting kept?",
                        "expect": "yes",
                        "only_if": {"check_id": "asked", "answer": "yes"},
                    },
                    {"check_id": "asked", "question": "Did the agent ask?", "expect": "yes"},
                ]
            },
            "only_if must reference an earlier check",
        ),
        (
            {
                "checks": [
                    {
                        "check_id": "asked",
                        "question": "Did the agent ask?",
                        "expect": "yes",
                        "only_if": {"check_id": "asked", "answer": "yes"},
                    }
                ]
            },
            "only_if must reference an earlier check",
        ),
        ({"disabled_rubrics": ["style"]}, "default rubric IDs: style"),
        ({"disabled_rubrics": ["test-quality", "test-quality"]}, "must be unique"),
    ],
)
def test_case_content_rejects_invalid_checks_and_disabled_rubrics(changes, message) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match=message):
        CaseContent.model_validate_json(canonical_json(LEGACY_CONTENT | changes))


def test_case_checks_and_disabled_rubrics_round_trip() -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json, parse_model

    payload = LEGACY_CONTENT | {
        "checks": [
            {"check_id": "asked", "question": "Did the agent ask first?", "expect": "yes"},
            {
                "check_id": "kept",
                "question": "Was the old setting kept?",
                "expect": "no",
                "only_if": {"check_id": "asked", "answer": "yes"},
            },
        ],
        "disabled_rubrics": ["test-quality"],
    }
    content = parse_model(canonical_json(payload), CaseContent)
    assert content.checks[1].only_if.check_id == "asked"
    assert content.disabled_rubrics == ("test-quality",)
    assert parse_model(canonical_json(content), CaseContent) == content


def test_legacy_case_without_workflow_keeps_its_object_id(store: ObjectStore) -> None:
    from dryheave.cases import CaseContent
    from dryheave.models import ObjectKind

    case = CaseContent(title="Pin case", initial_prompt="Do the thing.")
    assert "workflow" not in case.model_dump(mode="json")
    identifier = store.put(ObjectKind.CASE, case)
    assert identifier == "0e6bc5d8d7c84486d392c055305d09b2fe5238faa2335e5044261a83df4667b7"


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

    step = PromptStep(step_id="review", prompt=prompt)
    assert (step.slash, is_slash(prompt), step.skill_name) == (slash, slash, skill_name)


def test_workflow_round_trips_prompt_and_inspect_steps() -> None:
    from dryheave.cases import CaseContent, InspectStep, PromptStep
    from dryheave.serialization import canonical_json, parse_model

    payload = LEGACY_CONTENT | {
        "workflow": [
            {"kind": "prompt", "step_id": "review", "prompt": "/code-review --quick"},
            {
                "kind": "prompt",
                "step_id": "hint",
                "prompt": "fix the findings",
                "answer_hint": "fix Critical and Should Fix findings",
            },
            {"kind": "inspect", "step_id": "inspect-quality", "criteria": ["quality"]},
        ]
    }
    content = parse_model(canonical_json(payload), CaseContent)
    assert content.workflow[0] == PromptStep(step_id="review", prompt="/code-review --quick")
    assert content.workflow[1].answer_hint == "fix Critical and Should Fix findings"
    assert content.workflow[2] == InspectStep(step_id="inspect-quality", criteria=("quality",))
    assert canonical_json(content) == canonical_json(payload)
    assert "answer_hint" not in canonical_json(content.workflow[0]).decode()


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (
            [
                {"kind": "prompt", "step_id": "dup", "prompt": "/code-review"},
                {"kind": "prompt", "step_id": "dup", "prompt": "update the docs"},
            ],
            "workflow step IDs must be unique",
        ),
        (
            [{"kind": "inspect", "step_id": "inspect-unknown", "criteria": ["not-a-criterion"]}],
            "inspects unknown criteria",
        ),
        (
            [{"kind": "inspect", "step_id": "inspect-disabled", "criteria": ["test-quality"]}],
            "inspects unknown criteria",
        ),
        (
            [{"kind": "inspect", "step_id": "inspect-dup", "criteria": ["quality", "quality"]}],
            "inspects duplicate criteria",
        ),
        (
            [{"kind": "prompt", "step_id": "no-turn", "prompt": "/clear"}],
            "command with no model turn",
        ),
        (
            [{"kind": "prompt", "step_id": "no-turn", "prompt": "/compact please"}],
            "command with no model turn",
        ),
        (
            [{"kind": "prompt", "step_id": "mention", "prompt": " $code-review --quick"}],
            (
                r"step mention starts with '\$': write skill steps as /name; "
                r"Codex receives them as \$name"
            ),
        ),
        (
            [{"kind": "prompt", "step_id": "shell", "prompt": "!git status"}],
            "step shell starts with '!': write skill steps as /name",
        ),
    ],
)
def test_case_content_rejects_invalid_workflow_steps(workflow, message) -> None:
    from dryheave.cases import CaseContent
    from dryheave.serialization import canonical_json

    payload = LEGACY_CONTENT | {
        "disabled_rubrics": ["test-quality"],
        "workflow": workflow,
    }
    with pytest.raises(ValidationError, match=message):
        CaseContent.model_validate_json(canonical_json(payload))


def test_inspect_step_rejects_answer_hint_field() -> None:
    from dryheave.cases import InspectStep
    from dryheave.serialization import canonical_json

    with pytest.raises(ValidationError, match="answer_hint"):
        InspectStep.model_validate_json(
            canonical_json(
                {"step_id": "inspect", "criteria": ["quality"], "answer_hint": "not allowed"}
            )
        )


def test_inspect_step_may_cite_a_non_disabled_default_rubric() -> None:
    from dryheave.cases import CaseContent, InspectStep
    from dryheave.serialization import canonical_json

    payload = LEGACY_CONTENT | {
        "workflow": [
            {"kind": "inspect", "step_id": "inspect-code-quality", "criteria": ["code-quality"]}
        ]
    }
    content = CaseContent.model_validate_json(canonical_json(payload))
    assert content.workflow == (
        InspectStep(step_id="inspect-code-quality", criteria=("code-quality",)),
    )


@pytest.mark.parametrize(
    ("enabled", "inspected", "has_required", "expected"),
    [
        (False, True, True, ()),
        (True, False, True, ()),
        (True, True, False, ()),
    ],
)
def test_effective_workflow_default_cases(enabled, inspected, has_required, expected) -> None:
    from dryheave.cases import CaseContent, RubricCriterion, effective_workflow

    criteria = (
        (RubricCriterion(criterion_id="quality", required=True, rubric="Quality rubric text."),)
        if has_required
        else (
            RubricCriterion(criterion_id="quality", required=False, rubric="Quality rubric text."),
        )
    )
    case = CaseContent(title="t", initial_prompt="p", criteria=criteria)
    assert effective_workflow(case, enabled=enabled, inspected=inspected) == expected


def test_effective_workflow_builds_the_default_inspect_step() -> None:
    from dryheave.cases import CaseContent, InspectStep, RubricCriterion, effective_workflow

    case = CaseContent(
        title="t",
        initial_prompt="p",
        criteria=(
            RubricCriterion(criterion_id="quality", required=True, rubric="Quality rubric text."),
            RubricCriterion(criterion_id="style", required=False, rubric="Style rubric text here."),
        ),
    )
    assert effective_workflow(case, enabled=True, inspected=True) == (
        InspectStep(step_id="inspect-required", criteria=("quality",)),
    )


def test_effective_workflow_prefers_the_declared_workflow() -> None:
    from dryheave.cases import CaseContent, PromptStep, RubricCriterion, effective_workflow

    workflow = (PromptStep(step_id="review", prompt="/code-review --quick"),)
    case = CaseContent(
        title="t",
        initial_prompt="p",
        criteria=(
            RubricCriterion(criterion_id="quality", required=True, rubric="Quality rubric text."),
        ),
        workflow=workflow,
    )
    assert effective_workflow(case, enabled=True, inspected=True) == workflow
    assert effective_workflow(case, enabled=True, inspected=False) == workflow
    assert effective_workflow(case, enabled=False, inspected=True) == ()


@pytest.mark.integration
def test_frozen_case_keeps_declared_checks(
    store: ObjectStore, historical_repo: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    from dryheave.cases import CaseCheck, CheckCondition

    checks = (
        CaseCheck(check_id="asked", question="Did the agent ask first?", expect="yes"),
        CaseCheck(
            check_id="kept",
            question="Was the old greeting kept?",
            expect="no",
            only_if=CheckCondition(check_id="asked", answer="yes"),
        ),
    )
    draft = curated(store, historical_repo, tmp_path).model_copy(
        update={"checks": checks, "disabled_rubrics": ("test-quality",)}
    )
    case = load_frozen_case(store, freeze_case(store, draft, tmp_path))
    assert (case.checks, case.disabled_rubrics) == (checks, ("test-quality",))


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
        CaseContent.model_validate_json(canonical_json(LEGACY_CONTENT | changes))


def test_frozen_case_requires_hidden_metrics_entrypoint() -> None:
    from dryheave.serialization import canonical_json

    frozen = LEGACY_CONTENT | {
        "repository_id": "b" * 64,
        "persona_id": "c" * 64,
        "source_session_id": "a" * 64,
        "source_path_hash": "d" * 64,
        "source_start_event": "e1",
        "source_end_event": "e1",
        "metrics": [METRICS_COMMAND],
    }
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
