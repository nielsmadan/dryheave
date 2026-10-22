import json
import sys

import pytest

from conftest import HOST
from dryheave.assessments import assess_run
from dryheave.cases import RubricCriterion, load_frozen_case
from dryheave.controller_models import ControllerRecipe
from dryheave.experiment_models import PriceRate, PriceTable, ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.integrity import load_assessment
from dryheave.models import CommandSpec, ObjectKind
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions


@pytest.mark.integration
def test_deterministic_criterion_named_judge_has_separate_artifacts(
    store, graded_benchmark, tmp_path
):
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                case.criteria[0].model_copy(update={"criterion_id": "judge"}),
                RubricCriterion(
                    criterion_id="quality",
                    rubric="The implementation preserves the existing public interface.",
                ),
            )
        }
    )
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    response = {
        "judgments": [
            {"criterion_id": "quality", "outcome": "pass", "rationale": "Interface preserved."}
        ]
    }
    script = tmp_path / "judge.py"
    script.write_text("print(" + repr(json.dumps(response)) + ")\n")
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )
    summary = run_experiment(
        store,
        create_experiment(
            store, graded_benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})
        ),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [(item.criterion_id, item.outcome) for item in result.criteria] == [
        ("judge", "pass"),
        ("quality", "pass"),
        ("code-quality", "error"),
        ("codebase-consistency", "error"),
        ("test-quality", "error"),
    ]
    assert [item.required for item in result.criteria[2:]] == [False, False, False]
    assert result.rubric_set == "default-v1"
    assert result.completion == "pass"
    assert result.eligible
    evidence = store.read_blobs(result.evidence_id)
    assert any("/criteria/judge/final/" in name for name in evidence)
    assert any("/judge/j-" in name for name in evidence)


@pytest.mark.integration
def test_partial_judge_response_retains_valid_judgment_and_spend(store, graded_benchmark, tmp_path):
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                *case.criteria,
                RubricCriterion(
                    criterion_id="quality",
                    rubric="The implementation should remain readable and concise.",
                ),
                RubricCriterion(
                    criterion_id="design",
                    rubric="The implementation should preserve the existing public interface.",
                ),
            )
        }
    )
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    response = {
        "judgments": [
            {
                "criterion_id": "quality",
                "outcome": "pass",
                "score": 0.8,
                "rationale": "The code uses a concise direct implementation.",
            }
        ],
        "usage": {
            "uncached_input": 100,
            "cache_read": 10,
            "cache_write": 0,
            "output": 50,
            "reasoning": 20,
            "provenance": "fixture response",
        },
        "observed_model": "fixture-model",
    }
    script = tmp_path / "judge.py"
    script.write_text(
        "import json,sys\nrequest=json.load(sys.stdin)\nassert request['criteria'][0]['criterion_id']=='quality'\nprint("
        + repr(json.dumps(response))
        + ")\n"
    )
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        ),
        prices=PriceTable(
            version="fixture-v1",
            effective_date="2026-09-01",
            currency="USD",
            rates=(
                PriceRate(
                    model="fixture-model",
                    uncached_input=1.0,
                    cache_read=0.1,
                    cache_write=1.0,
                    output=2.0,
                ),
            ),
        ),
    )
    benchmark = graded_benchmark.model_copy(update={"cases": (changed,), "scoring": scoring})
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.deterministic_completion == "pass"
    assert result.completion == "indeterminate"
    assert [(item.criterion_id, item.outcome) for item in result.criteria] == [
        ("greeting", "pass"),
        ("quality", "pass"),
        ("design", "error"),
        ("code-quality", "error"),
        ("codebase-consistency", "error"),
        ("test-quality", "error"),
    ]
    assert result.roles[2].known_cost == 201 / 1e6
    assert result.evidence_id
    assert result.criteria[1].evidence_hash
    assert "rationale" not in result.model_dump_json()


def test_codex_schema_requires_nullable_nested_fields_without_model_launch():
    from dryheave.controller_schema import controller_schema
    from dryheave.judges import JudgeResponse

    schema = controller_schema(JudgeResponse)
    judgment = schema["$defs"]["Judgment"]
    assert set(judgment["required"]) == set(judgment["properties"])
    assert "score" in judgment["required"]
    usage = schema["$defs"]["TokenUsage"]
    assert set(usage["required"]) == set(usage["properties"])
    assert {"type": "null"} in judgment["properties"]["score"]["anyOf"]


@pytest.mark.integration
@pytest.mark.parametrize("invalid", [False, True])
def test_claude_judge_uses_structured_protocol_and_provider_usage(
    store, benchmark, tmp_path, invalid
):
    import threading
    import time

    from dryheave.controllers import CallContext
    from dryheave.drivers.artifacts import ArtifactWriter
    from dryheave.judges import JudgeCheck, JudgeInput, invoke_judge
    from test_claude_controller import FIXTURE, fake_executable, recipe_for

    payload = json.loads(FIXTURE.read_bytes())
    payload["structured_output"] = {
        "judgments": [
            {
                "criterion_id": "unknown" if invalid else "quality",
                "outcome": "pass",
                "score": 0.75,
                "rationale": "Synthetic rubric fixture.",
            },
        ],
        "checks": [
            {"check_id": "applied", "answer": "yes", "rationale": "The edit is in greet.py."}
        ],
    }
    recipe = recipe_for(fake_executable(tmp_path, payload))
    request = JudgeInput(
        initial_prompt="Review the greeting.",
        criteria=(
            RubricCriterion(criterion_id="quality", rubric="Preserve the public interface."),
        ),
        files={"greet.py": "synthetic"},
        omitted_binary_files=(),
        checks=(JudgeCheck(check_id="applied", question="Was the greeting edited in place?"),),
    )
    context = CallContext(
        call_id="judge-call",
        index=0,
        deadline=time.monotonic() + 3,
        cancelled=threading.Event(),
        inherited={"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-offline-value"},
        on_identities=lambda _identities: None,
        runtime_root=tmp_path / "runtime",
    )
    call, results, answers, judgments = invoke_judge(
        recipe, request, ArtifactWriter(tmp_path / "evidence", 1000000), context
    )
    assert call.status == ("failed" if invalid else "completed")
    assert call.usage.output == 12
    assert call.observed_model == "claude-haiku-4-5-20251001"
    assert call.observed_effort is None
    assert call.cleanup.known_writers_stopped
    assert results[0].outcome == ("error" if invalid else "pass")
    assert answers == ({} if invalid else {"applied": "yes"})
    assert [(item.criterion_id, item.rationale) for item in judgments] == (
        [] if invalid else [("quality", "Synthetic rubric fixture.")]
    )


@pytest.mark.integration
@pytest.mark.parametrize("completed,pending", [(False, True), (True, False), (True, True)])
def test_unfinished_assessment_and_export_reconstruct_durable_judge_spend(
    store, graded_benchmark, tmp_path, monkeypatch, completed, pending
):
    from dryheave.bundles import export_bundle, import_bundle
    from dryheave.controller_models import RoleCall
    from dryheave.reports import report_run
    from dryheave.storage import ObjectStore

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    active = []

    def interrupt(execution, state, *_args):
        if completed:
            call = RoleCall(call_id="completed-judge", role="judge", status="intent")
            execution.evidence(state, "judge-intent", call)
            execution.evidence(
                state,
                "judge-result",
                call.model_copy(
                    update={
                        "status": "completed",
                        "cost": 0.25,
                    }
                ),
            )
        if pending:
            execution.evidence(
                state,
                "judge-intent",
                RoleCall(call_id="pending-judge", role="judge", status="intent"),
            )
        active.append(report_run(store, summary.run_id))
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.assessments._grade", interrupt)
    with pytest.raises(KeyboardInterrupt):
        assess_run(store, summary.run_id)
    report = report_run(store, summary.run_id)
    path = tmp_path / "pending.tar"
    export_bundle(store, summary.run_id, path)
    target = ObjectStore(tmp_path / "imported")
    portable = report_run(target, import_bundle(target, path).roots[0])
    for inspected in (*active, report, portable):
        attempt = inspected.attempts[0]
        assert attempt.result.phase == "audited"
        judge = attempt.roles[2]
        assert len(judge.records) == int(completed) + int(pending)
        assert sum(record.cost is None for record in judge.records) == int(pending)
        assert judge.known_cost == (0.25 if completed else None)
        assert inspected.groups[0].known_spend_by_currency == (
            {"UNSPECIFIED": 0.25} if completed else {}
        )
        assert inspected.groups[0].unknown_cost_records == int(pending)


@pytest.mark.integration
def test_judge_is_not_called_when_the_subject_accepted_no_prompt(store, graded_benchmark, tmp_path):
    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                *case.criteria,
                RubricCriterion(criterion_id="quality", rubric="The code stays readable."),
            )
        }
    )
    changed = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    marker = tmp_path / "judge-called"
    script = tmp_path / "judge.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).touch()\nprint('{{}}')\n")
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )
    fixture = (graded_benchmark.fixture[0].model_copy(update={"state": "approval"}),)
    benchmark = graded_benchmark.model_copy(
        update={"cases": (changed,), "scoring": scoring, "fixture": fixture}
    )
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.accepted_turns == 0
    assert (result.criteria[-1].outcome, result.criteria[-1].error) == (
        "not_run",
        "subject_accepted_no_prompt",
    )
    assert not marker.exists()


WORKSPACE = "/runs/r1/attempts/a1/workspace"


def native_blobs(*events: dict) -> dict[str, bytes]:
    from dryheave.serialization import canonical_json

    return {
        f"evidence/driver/{len(events) - index:06d}-native-event.json": canonical_json(event)
        for index, event in enumerate(events)
    }


def tool_use(sequence: int, name: str, payload: dict) -> dict:
    return {
        "sequence": sequence,
        "kind": "tool",
        "data": {"type": "tool_use", "name": name, "input": payload},
    }


def test_transcript_orders_by_sequence_and_relativizes_paths():
    from dryheave.controller_models import DialogueMessage
    from dryheave.judges import judge_transcript

    blobs = native_blobs(
        {"sequence": 1, "kind": "accepted", "text": f"Add a toggle to {WORKSPACE}/App/View.swift"},
        {"sequence": 0, "kind": "metadata", "data": {"model": "fixture"}},
        tool_use(
            2,
            "Edit",
            {"file_path": f"{WORKSPACE}/App/View.swift", "old_string": "a", "new_string": "b"},
        ),
        {
            "sequence": 3,
            "kind": "tool",
            "data": {"type": "tool_result", "content": "The file was updated."},
        },
        tool_use(4, "Bash", {"command": f"cat /Users/someone/.netrc && ls {WORKSPACE}"}),
        {
            "sequence": 5,
            "kind": "tool",
            "data": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command": ["git", "status"]}',
            },
        },
        {"sequence": 6, "kind": "tool", "data": {"type": "function_call_output", "output": "ok"}},
        {"sequence": 7, "kind": "assistant", "text": "Added the toggle."},
    )
    dialogue = (DialogueMessage(role="user", text="unused fallback"),)
    transcript = judge_transcript(blobs, dialogue, (WORKSPACE,))
    assert [(entry.role, entry.text) for entry in transcript] == [
        ("user", "Add a toggle to App/View.swift"),
        ("action", 'Edit App/View.swift: {"new_string": "b", "old_string": "a"}'),
        ("action", 'Bash: {"command": "cat <host path> && ls ."}'),
        ("action", 'shell: {"command": ["git", "status"]}'),
        ("assistant", "Added the toggle."),
    ]


def test_transcript_from_events_matches_the_blob_projection_and_falls_back_to_dialogue():
    from dryheave.controller_models import DialogueMessage
    from dryheave.drivers.models import NativeEvent
    from dryheave.judges import judge_transcript, judge_transcript_from_events

    records = (
        {"sequence": 2, "kind": "assistant", "text": f"Edited {WORKSPACE}/greet.py."},
        {"sequence": 1, "kind": "accepted", "text": "Fix the greeting."},
        tool_use(3, "Bash", {"command": f"python {WORKSPACE}/greet.py"}),
    )
    events = tuple(NativeEvent.model_validate(record) for record in records)
    dialogue = (DialogueMessage(role="user", text="Fix the greeting."),)
    transcript = judge_transcript_from_events(events, dialogue, (WORKSPACE,))
    assert transcript == judge_transcript(native_blobs(*records), dialogue, (WORKSPACE,))
    assert [(entry.role, entry.text) for entry in transcript] == [
        ("user", "Fix the greeting."),
        ("assistant", "Edited greet.py."),
        ("action", 'Bash: {"command": "python greet.py"}'),
    ]
    assert [
        (entry.role, entry.text)
        for entry in judge_transcript_from_events((), dialogue, (WORKSPACE,))
    ] == [("user", "Fix the greeting.")]


def test_transcript_bounds_action_excerpts_and_falls_back_to_dialogue():
    from dryheave.controller_models import DialogueMessage
    from dryheave.judges import ACTION_EXCERPT_CHARACTERS, judge_transcript

    long = judge_transcript(
        native_blobs(tool_use(1, "Write", {"file_path": "notes.md", "content": "x" * 5000})),
        (),
        (),
    )
    assert long[0].text.startswith("Write notes.md: ")
    assert len(long[0].text) == len("Write notes.md: ") + ACTION_EXCERPT_CHARACTERS + 1
    dialogue = (
        DialogueMessage(role="user", text=f"Fix {WORKSPACE}/greet.py"),
        DialogueMessage(role="assistant", text="Done."),
    )
    assert [(entry.role, entry.text) for entry in judge_transcript({}, dialogue, (WORKSPACE,))] == [
        ("user", "Fix greet.py"),
        ("assistant", "Done."),
    ]


def test_transcript_truncation_keeps_messages_and_first_and_last_actions():
    from dryheave.judges import JudgeInput, TranscriptEntry, fit_transcript
    from dryheave.serialization import canonical_json

    transcript = (
        TranscriptEntry(role="user", text="Start"),
        *(
            TranscriptEntry(role="action", text=f"Read file-{index}.txt" + "y" * 200)
            for index in range(6)
        ),
        TranscriptEntry(role="assistant", text="Finished"),
    )
    request = JudgeInput(
        initial_prompt="Start",
        criteria=(),
        files={},
        omitted_binary_files=(),
        transcript=transcript,
    )
    assert fit_transcript(request, len(canonical_json(request))) == request
    smallest = request.model_copy(
        update={
            "transcript": (transcript[0], transcript[1], transcript[6], transcript[7]),
            "transcript_truncated": True,
        }
    )
    fitted = fit_transcript(request, len(canonical_json(smallest)))
    assert fitted == smallest
    three = fit_transcript(request, len(canonical_json(smallest)) + 250)
    assert [entry.text[:11] for entry in three.transcript] == [
        "Start",
        "Read file-0",
        "Read file-1",
        "Read file-5",
        "Finished",
    ]
    assert three.instruction.endswith("every message is still present.")


def test_transcript_emptied_to_fit_tells_the_judge_messages_were_omitted():
    from dryheave.judges import (
        EMPTIED_TRANSCRIPT_NOTE,
        JUDGE_INSTRUCTION,
        JudgeInput,
        TranscriptEntry,
        fit_transcript,
    )
    from dryheave.serialization import canonical_json

    request = JudgeInput(
        initial_prompt="Start",
        criteria=(),
        files={},
        omitted_binary_files=(),
        transcript=(
            TranscriptEntry(role="user", text="Start" + "m" * 400),
            TranscriptEntry(role="action", text="Read notes.md"),
            TranscriptEntry(role="assistant", text="Finished" + "m" * 400),
        ),
    )
    fitted = fit_transcript(request, len(canonical_json(request)) - 500)
    assert (fitted.transcript, fitted.transcript_truncated) == ((), True)
    assert fitted.instruction == JUDGE_INSTRUCTION + EMPTIED_TRANSCRIPT_NOTE


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd app\n/Users/someone/bin/run.sh", 'Bash: {"command": "cd app\\n<host path>"}'),
        (
            "docker run -v data:/var/lib/app image",
            'Bash: {"command": "docker run -v data:<host path> image"}',
        ),
        ("cc -I/usr/include/sdl main.c", 'Bash: {"command": "cc -I<host path> main.c"}'),
        ("ls src/lib/util.py", 'Bash: {"command": "ls src/lib/util.py"}'),
    ],
)
def test_transcript_redacts_host_paths_after_escapes_colons_and_include_flags(command, expected):
    from dryheave.judges import judge_transcript

    transcript = judge_transcript(native_blobs(tool_use(1, "Bash", {"command": command})), (), ())
    assert transcript[0].text == expected


def test_check_answers_map_to_outcomes():
    from dryheave.cases import CaseCheck, CheckCondition
    from dryheave.judges import check_results

    checks = (
        CaseCheck(check_id="asked", question="Did the agent ask first?", expect="yes"),
        CaseCheck(
            check_id="kept",
            question="Was the old greeting kept?",
            expect="no",
            only_if=CheckCondition(check_id="asked", answer="yes"),
        ),
        CaseCheck(
            check_id="guessed",
            question="Did the agent guess the punctuation?",
            expect="no",
            only_if=CheckCondition(check_id="asked", answer="no"),
        ),
        CaseCheck(
            check_id="nested",
            question="Did the agent explain its guess?",
            expect="yes",
            only_if=CheckCondition(check_id="guessed", answer="yes"),
        ),
        CaseCheck(check_id="tested", question="Did the agent run the tests?", expect="yes"),
        CaseCheck(
            check_id="followup",
            question="Did the agent fix failing tests?",
            expect="yes",
            only_if=CheckCondition(check_id="tested", answer="yes"),
        ),
        CaseCheck(check_id="silent", question="Did the agent stay silent?", expect="no"),
        CaseCheck(
            check_id="explained",
            question="Did the agent explain its silence?",
            expect="yes",
            only_if=CheckCondition(check_id="silent", answer="yes"),
        ),
    )
    answers = {
        "asked": "yes",
        "kept": "yes",
        "guessed": "yes",
        "nested": "yes",
        "tested": "unclear",
        "followup": "yes",
        "explained": "yes",
    }
    assert [
        (item.check_id, item.outcome, item.answer) for item in check_results(checks, answers)
    ] == [
        ("asked", "pass", "yes"),
        ("kept", "fail", "yes"),
        ("guessed", "not_applicable", "yes"),
        ("nested", "not_applicable", "yes"),
        ("tested", "error", "unclear"),
        ("followup", "not_applicable", "yes"),
        ("silent", "error", None),
        ("explained", "not_applicable", "yes"),
    ]


CHECKS = (
    {
        "check_id": "asked",
        "question": "Did the agent ask which punctuation to use?",
        "expect": "yes",
    },
    {
        "check_id": "kept",
        "question": "Did the agent keep the old greeting text?",
        "expect": "no",
        "only_if": {"check_id": "asked", "answer": "yes"},
    },
    {
        "check_id": "guessed",
        "question": "Did the agent guess the punctuation?",
        "expect": "no",
        "only_if": {"check_id": "asked", "answer": "no"},
    },
    {"check_id": "unsure", "question": "Did the agent run the greeting?", "expect": "yes"},
)


def checked_case(store, graded_benchmark):
    from dryheave.cases import CaseCheck

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "criteria": (
                *case.criteria,
                RubricCriterion(
                    criterion_id="quality", rubric="The greeting keeps its public interface."
                ),
            ),
            "checks": tuple(CaseCheck.model_validate_json(json.dumps(item)) for item in CHECKS),
            "disabled_rubrics": ("test-quality",),
        }
    )
    return store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )


def checks_only_case(store, graded_benchmark):
    from dryheave.cases import CaseCheck

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    case = case.model_copy(
        update={
            "checks": tuple(CaseCheck.model_validate_json(json.dumps(item)) for item in CHECKS),
            "disabled_rubrics": ("code-quality", "codebase-consistency", "test-quality"),
        }
    )
    return store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )


@pytest.mark.integration
def test_offline_assessment_calls_judge_for_checks_only_case(store, graded_benchmark, tmp_path):
    response = {
        "judgments": [],
        "checks": [
            {"check_id": "asked", "answer": "yes", "rationale": "It asked about punctuation."},
            {"check_id": "kept", "answer": "no", "rationale": "The text was replaced."},
            {"check_id": "guessed", "answer": "no", "rationale": "It asked instead."},
            {"check_id": "unsure", "answer": "unclear", "rationale": "No run is visible."},
        ],
    }
    benchmark = graded_benchmark.model_copy(
        update={
            "cases": (checks_only_case(store, graded_benchmark),),
            "scoring": json_judge(tmp_path, response),
        }
    )
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [(item.check_id, item.outcome, item.answer) for item in result.checks] == [
        ("asked", "pass", "yes"),
        ("kept", "pass", "no"),
        ("guessed", "not_applicable", "no"),
        ("unsure", "error", "unclear"),
    ]
    assert [item.criterion_id for item in result.criteria] == ["greeting"]
    assert result.rubric_set is None
    request = json.loads((tmp_path / "request.json").read_bytes())
    assert request["criteria"] == []


def json_judge(tmp_path, response: dict) -> ScoringConfig:
    script = tmp_path / "judge.py"
    script.write_text(
        "import sys,pathlib\n"
        f"pathlib.Path({str(tmp_path / 'request.json')!r}).write_bytes(sys.stdin.buffer.read())\n"
        "print(" + repr(json.dumps(response)) + ")\n"
    )
    return ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )


@pytest.mark.integration
def test_offline_assessment_answers_checks_in_the_judge_call(store, graded_benchmark, tmp_path):
    response = {
        "judgments": [
            {"criterion_id": "quality", "outcome": "pass", "rationale": "Interface preserved."},
            {
                "criterion_id": "code-quality",
                "outcome": "pass",
                "score": 0.9,
                "rationale": "Clean.",
            },
        ],
        "checks": [
            {"check_id": "asked", "answer": "yes", "rationale": "It asked about punctuation."},
            {"check_id": "kept", "answer": "no", "rationale": "The text was replaced."},
            {"check_id": "guessed", "answer": "no", "rationale": "It asked instead."},
            {"check_id": "unsure", "answer": "unclear", "rationale": "No run is visible."},
        ],
    }
    benchmark = graded_benchmark.model_copy(
        update={
            "cases": (checked_case(store, graded_benchmark),),
            "scoring": json_judge(tmp_path, response),
        }
    )
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert [(item.check_id, item.outcome, item.answer) for item in result.checks] == [
        ("asked", "pass", "yes"),
        ("kept", "pass", "no"),
        ("guessed", "not_applicable", "no"),
        ("unsure", "error", "unclear"),
    ]
    assert [(item.criterion_id, item.outcome) for item in result.criteria] == [
        ("greeting", "pass"),
        ("quality", "pass"),
        ("code-quality", "pass"),
        ("codebase-consistency", "error"),
    ]
    assert result.rubric_set == "default-v1"
    assert (result.completion, result.eligible) == ("pass", True)
    assert "rationale" not in result.model_dump_json()
    request = json.loads((tmp_path / "request.json").read_bytes())
    assert request["checks"] == [
        {"check_id": item["check_id"], "question": item["question"]} for item in CHECKS
    ]
    assert [item["criterion_id"] for item in request["criteria"]] == [
        "quality",
        "code-quality",
        "codebase-consistency",
    ]
    assert request["transcript"][:2] == [
        {"role": "user", "text": "Fix the greeting; ask me which punctuation."},
        {"role": "assistant", "text": "Which punctuation?"},
    ]
    assert request["transcript_truncated"] is False
    raw = (tmp_path / "request.json").read_text()
    assert '"expect"' not in raw
    assert '"only_if"' not in raw


@pytest.mark.integration
def test_judge_projection_truncates_actions_of_a_fixture_capture(store, graded_benchmark, tmp_path):
    from dryheave.judges import judge_projection
    from dryheave.runner import load_capture
    from dryheave.serialization import canonical_json

    benchmark = graded_benchmark.model_copy(
        update={
            "cases": (checked_case(store, graded_benchmark),),
            "scoring": json_judge(tmp_path, {"judgments": []}),
        }
    )
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture_id = summary.pending_assessment[0]
    capture = load_capture(store, capture_id)
    case = load_frozen_case(store, capture.case_id)
    workspace = summary.attempts[0].workspace
    files = store.read_evidence(capture_id).files | native_blobs(
        {"sequence": 1, "kind": "accepted", "text": "Fix the greeting."},
        *(
            tool_use(index + 2, "Read", {"file_path": f"{workspace}/greet.py", "offset": index})
            for index in range(8)
        ),
        {"sequence": 20, "kind": "assistant", "text": "Done."},
    )
    full = judge_projection(case, capture, files, roots=(workspace,))
    assert [entry.text for entry in full.transcript][1:3] == [
        'Read greet.py: {"offset": 0}',
        'Read greet.py: {"offset": 1}',
    ]
    assert [item.check_id for item in full.checks] == [item["check_id"] for item in CHECKS]
    limited = judge_projection(
        case, capture, files, roots=(workspace,), limit=len(canonical_json(full)) - 1
    )
    assert limited.transcript_truncated
    actions = [entry.text for entry in limited.transcript if entry.role == "action"]
    assert actions[0] == 'Read greet.py: {"offset": 0}'
    assert actions[-1] == 'Read greet.py: {"offset": 7}'
    assert len(actions) == 7
    assert [entry for entry in limited.transcript if entry.role != "action"] == [
        entry for entry in full.transcript if entry.role != "action"
    ]


@pytest.mark.integration
def test_resumed_assessment_reuses_journaled_check_results(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.controller_models import RoleCall
    from dryheave.journals import RunStore
    from dryheave.models import TrialStage
    from dryheave.result_models import CheckResult
    from dryheave.runner_state import ExecutionJournal

    benchmark = graded_benchmark.model_copy(
        update={
            "cases": (checked_case(store, graded_benchmark),),
            "scoring": json_judge(tmp_path, {"judgments": []}),
        }
    )
    summary = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    saved = CheckResult(check_id="asked", outcome="pass", answer="yes")
    with RunStore(store.root).open(summary.run_id) as journal:
        execution = ExecutionJournal(journal)
        state = execution.attempts[summary.attempts[0].attempt_id]
        for stage in (TrialStage.AUDITED, TrialStage.GRADING):
            state = state.model_copy(update={"stage": stage})
            execution.save(state)
        call = RoleCall(call_id="j-saved", role="judge", status="intent")
        execution.evidence(state, "judge-intent", call)
        execution.evidence(state, "judge-result", call.model_copy(update={"status": "completed"}))
        execution.evidence(state, "check-result", saved)
    monkeypatch.setattr(
        "dryheave.assessments.invoke_judge",
        lambda *_args: pytest.fail("A journaled judge call must not repeat."),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.checks[0] == saved
    assert [item.outcome for item in result.checks[1:]] == ["error", "error", "error"]
    assert result.criteria[1].error == "interrupted_judge"
    with RunStore(store.root).open(summary.run_id) as journal:
        events = ExecutionJournal(journal).events_for(state.attempt_id, "check-result")
    assert [event.data["check_id"] for event in events] == [
        "asked",
        "kept",
        "guessed",
        "unsure",
    ]
