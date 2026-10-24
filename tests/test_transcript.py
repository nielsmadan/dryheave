import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from conftest import FIX_REPLY, HOST, INTERFACE_FACT, broken_then_fixed, workflow_benchmark
from dryheave.cases import PromptStep, TaskFact, load_frozen_case
from dryheave.cli import main
from dryheave.controller_models import RoleCall, StandInDecision
from dryheave.experiments import create_experiment
from dryheave.journals import RunStore
from dryheave.judges import FactInspection, FactJudgment, RaiseEvent
from dryheave.models import JournalEvent, ObjectKind
from dryheave.result_models import Assessment, FactResult
from dryheave.runner import run_experiment
from dryheave.runner_models import (
    RunOptions,
    WorkflowEvent,
    WorkflowProgress,
    WorkflowStepRecord,
)
from dryheave.runner_state import ExecutionJournal
from dryheave.transcript import (
    AttemptMaterial,
    load_material,
    render_lanes,
    review_lanes,
    run_refs,
)

ATTEMPT = "a-recorded"
NOW = datetime(2026, 10, 23, tzinfo=UTC)


def recorded(*entries):
    return tuple(
        JournalEvent(
            sequence=index + 1,
            previous_hash=None,
            timestamp=NOW,
            event=name,
            trial_id="t-1",
            attempt_id=ATTEMPT,
            data=data,
        )
        for index, (name, data) in enumerate(entries)
    )


def interaction(prompt, text, accepted):
    return (
        "interaction",
        {
            "prompt": prompt,
            "observation": {"state": "completed", "text": text},
            "accepted_turns": accepted,
            "delivery_attempts": accepted,
        },
    )


def stand_in(call_id, decision, *, status="completed", error=None):
    return (
        "controller-result",
        RoleCall(call_id=call_id, status=status, decision=decision, error=error).model_dump(
            mode="json"
        ),
    )


def reply(text, *fact_ids, reason="The user would say this."):
    return StandInDecision(action="reply", text=text, fact_ids=fact_ids, reason=reason)


def stop(reason):
    return StandInDecision(action="stop", reason=reason)


def step(event):
    return ("workflow-step", WorkflowStepRecord(event=event, position=0).model_dump(mode="json"))


def judged(fact_id, verdict, observation):
    return FactJudgment(
        fact_id=fact_id, verdict=verdict, observation=observation, evidence="greet.py:2"
    )


def recorded_case(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    return case.model_copy(
        update={
            "allowed_facts": (*case.allowed_facts, TaskFact.model_validate(INTERFACE_FACT)),
            "workflow": (
                PromptStep(
                    step_id="review", prompt="Please review your change.", after_phase="fix"
                ),
            ),
        }
    )


def recorded_attempt():
    inspection = FactInspection(
        inspection_id="i-first",
        turn=2,
        snapshot_digest="1" * 64,
        input_digest="2" * 64,
        status="completed",
        judgments=(
            judged("punctuation", "unmet", "Hello has no comma."),
            judged("interface", "met", "greet still takes a name."),
        ),
    )
    return recorded(
        ("submission-intent", {}),
        (
            "controller-result",
            RoleCall(
                call_id="c-dialog",
                status="completed",
                decision=StandInDecision(
                    action="reply", choices=("Comma",), reason="The dialog asks for punctuation."
                ),
            ).model_dump(mode="json"),
        ),
        interaction("Fix the greeting; ask me which punctuation.", "Which punctuation?", 1),
        stand_in("c-answer", reply("Use a comma after Hello.", "punctuation")),
        ("submission-intent", {}),
        interaction("Use a comma after Hello.", "Done.", 2),
        stand_in("c-done", stop("The assistant reports the work done.")),
        ("controller-intent", RoleCall(call_id="c-done", status="intent").model_dump(mode="json")),
        ("inspection-intent", {"inspection_id": "i-first", "turn": 2, "private": []}),
        ("fact-inspection", inspection.model_dump(mode="json")),
        step(
            WorkflowEvent(
                step_id="i-first",
                kind="inspect",
                outcome="failed",
                turn=2,
                failed_criteria=("punctuation",),
                call_ids=("j-first",),
            )
        ),
        stand_in(
            "c-refused",
            stop("Looks done to me."),
            status="failed",
            error="stop_with_findings",
        ),
        stand_in("c-fix", reply(FIX_REPLY, "interface", "punctuation")),
        ("submission-intent", {}),
        interaction(FIX_REPLY, "Fixed.", 3),
        (
            "raise",
            RaiseEvent(
                call_id="c-fix",
                phase="fix",
                fact_ids=("punctuation",),
                cited=("interface", "punctuation"),
                cause="fix_request",
                turn=3,
            ).model_dump(mode="json"),
        ),
        ("submission-intent", {}),
        interaction("Please review your change.", "Reviewed.", 4),
        step(WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=3)),
        ("inspection-intent", {"inspection_id": "i-lost", "turn": 4, "private": []}),
        ("controller-intent", RoleCall(call_id="c-lost", status="intent").model_dump(mode="json")),
    )


def graded(assessed_result, *facts, uncovered=()):
    return assessed_result.model_copy(update={"facts": facts, "uncovered": uncovered})


def assessed_result(store, assessed):
    execution = ExecutionJournal(RunStore(store.root).inspect(assessed.run_id))
    (state,) = execution.attempts.values()
    return store.read_payload(state.assessment_id, Assessment, kind=ObjectKind.RESULT)


def test_lanes_split_a_recorded_attempt_into_conversation_scenes_and_gaps(
    store, assessed, graded_benchmark
):
    result = graded(
        assessed_result(store, assessed),
        FactResult(
            fact_id="punctuation",
            kind="requirement",
            phase="fix",
            verdict="unmet",
            observation="Hello still has no comma.",
            evidence="greet.py:2",
            source="verifier",
        ),
        FactResult(
            fact_id="interface",
            kind="requirement",
            phase="fix",
            verdict="met",
            observation="greet takes a name.",
            evidence="greet.py:1",
            source="judge",
        ),
        uncovered=("The greeting drops the trailing newline.",),
    )
    lanes = review_lanes(
        AttemptMaterial(
            run_id=assessed.run_id,
            attempt_id=ATTEMPT,
            events=recorded_attempt(),
            case_id=graded_benchmark.cases[0],
            case=recorded_case(store, graded_benchmark),
            assessment=result,
            stop_reason="inspection_unavailable",
        )
    )
    assert [(line.turn, line.role, line.author, line.origin) for line in lanes.conversation] == [
        (1, "user", "initial", None),
        (1, "assistant", None, None),
        (2, "user", "stand_in", "c-answer"),
        (2, "assistant", None, None),
        (3, "user", "stand_in", "c-fix"),
        (3, "assistant", None, None),
        (4, "user", "workflow", "review"),
        (4, "assistant", None, None),
    ]
    scenes = lanes.behind_the_scenes
    assert [
        (scene.kind, scene.call_id or scene.inspection_id or scene.detail) for scene in scenes
    ] == [
        ("stand_in", "c-dialog"),
        ("stand_in", "c-answer"),
        ("stand_in", "c-done"),
        ("inspection", "i-first"),
        ("stand_in", "c-refused"),
        ("stand_in", "c-fix"),
        ("correction", "c-fix"),
        ("prompt_step", "review"),
        ("inspection", "i-lost"),
        ("stand_in", "c-lost"),
        ("stop", "inspection_unavailable"),
    ]
    dialog, answer, done, inspection, refused, fix, correction, prompt, lost, interrupted, _ = (
        scenes
    )
    assert (dialog.purpose, dialog.text, dialog.turn) == ("dialog", "Comma", 0)
    assert (answer.purpose, answer.cited, answer.reason) == (
        "reply",
        ("punctuation",),
        "The user would say this.",
    )
    assert (done.action, done.purpose) == ("stop", "reply")
    assert [(item.fact_id, item.verdict, item.observation) for item in inspection.facts] == [
        ("punctuation", "unmet", "Hello has no comma."),
        ("interface", "met", "greet still takes a name."),
    ]
    assert (refused.purpose, refused.rejection, refused.handed) == (
        "fix_request",
        "stop_with_findings",
        ("punctuation",),
    )
    assert (fix.purpose, fix.handed, fix.cited) == (
        "fix_request",
        ("punctuation",),
        ("interface", "punctuation"),
    )
    assert (correction.phase, correction.handed, correction.cited, correction.detail) == (
        "fix",
        ("punctuation",),
        ("interface", "punctuation"),
        "fix_request",
    )
    assert (prompt.text, prompt.turn) == ("Please review your change.", 3)
    assert (lost.status, lost.turn) == ("unknown", 4)
    assert (interrupted.status, interrupted.turn, interrupted.action) == ("unknown", 4, None)
    assert inspection.call_ids == ("j-first",)
    assert "      calls: j-first" in render_lanes(lanes)
    assert lanes.gaps.assessed
    assert [(item.fact_id, item.verdict, item.text) for item in lanes.gaps.facts] == [
        ("punctuation", "unmet", "Use a comma after Hello.")
    ]
    assert lanes.gaps.uncovered == ("The greeting drops the trailing newline.",)


def test_a_fallback_reply_carries_the_set_its_inspection_raised(store, graded_benchmark):
    events = recorded(
        ("submission-intent", {}),
        interaction("Fix the greeting.", "Done.", 1),
        stand_in("c-done", stop("Done.")),
        (
            "fact-inspection",
            FactInspection(
                inspection_id="i-one",
                turn=1,
                snapshot_digest="1" * 64,
                input_digest="2" * 64,
                status="completed",
                judgments=(judged("punctuation", "unmet", "No comma."),),
            ).model_dump(mode="json"),
        ),
        step(WorkflowEvent(step_id="i-one", kind="inspect", outcome="failed", turn=1)),
        (
            "stand-in-fallback",
            StandInDecision(
                action="reply", text="It still does not work.", reason="Fallback fix request."
            ).model_dump(mode="json"),
        ),
        ("submission-intent", {}),
        interaction("It still does not work.", "Fixed.", 2),
    )
    lanes = review_lanes(
        AttemptMaterial(
            run_id="0" * 32,
            attempt_id=ATTEMPT,
            events=events,
            case=load_frozen_case(store, graded_benchmark.cases[0]),
        )
    )
    fallback = next(scene for scene in lanes.behind_the_scenes if scene.kind == "fallback")
    assert (fallback.phase, fallback.handed, fallback.text) == (
        "fix",
        ("punctuation",),
        "It still does not work.",
    )
    assert lanes.conversation[-2].author == "fallback"
    assert not lanes.gaps.assessed


def test_a_capture_without_a_journal_still_gives_conversation_and_stand_in_calls(store, assessed):
    execution = ExecutionJournal(RunStore(store.root).inspect(assessed.run_id))
    (attempt_id,) = execution.attempts
    material = load_material(store, run_refs(store, execution, attempt_id), None)
    lanes = review_lanes(material)
    assert [line.text for line in lanes.conversation] == [
        "Fix the greeting; ask me which punctuation.",
        "Which punctuation?",
        "Use a comma after Hello.",
        "Done.",
    ]
    assert lanes.conversation[2].author == "stand_in"
    assert [(scene.kind, scene.action) for scene in lanes.behind_the_scenes] == [
        ("stand_in", "reply"),
        ("stand_in", "stop"),
        ("stop", None),
    ]
    assert "No run journal" in lanes.notes[0]


def test_capture_workflow_events_stand_in_for_journal_inspections_and_corrections(
    store, assessed, graded_benchmark
):
    execution = ExecutionJournal(RunStore(store.root).inspect(assessed.run_id))
    (attempt_id,) = execution.attempts
    material = load_material(store, run_refs(store, execution, attempt_id), None)
    fix_call = material.capture.controller_calls[0].call_id
    workflow = WorkflowProgress(
        steps=("review",),
        position=1,
        events=(
            WorkflowEvent(
                step_id="i-1",
                kind="inspect",
                outcome="failed",
                turn=2,
                failed_criteria=("punctuation",),
                call_ids=("j-one",),
            ),
            WorkflowEvent(
                step_id="fix",
                kind="inspect",
                outcome="fix_requested",
                turn=2,
                failed_criteria=("punctuation",),
                call_ids=(fix_call,),
            ),
            WorkflowEvent(step_id="review", kind="prompt", outcome="sent", turn=3),
            WorkflowEvent(step_id="dialog", kind="dialog", outcome="answered", turn=3),
        ),
    )
    lanes = review_lanes(
        replace(
            material,
            case=recorded_case(store, graded_benchmark),
            capture=material.capture.model_copy(update={"workflow": workflow}),
        )
    )
    inspection, correction, prompt = lanes.behind_the_scenes[2:5]
    assert (inspection.kind, inspection.status, inspection.handed, inspection.turn) == (
        "inspection",
        "failed",
        ("punctuation",),
        2,
    )
    assert inspection.call_ids == ("j-one",)
    assert (correction.kind, correction.phase, correction.call_id, correction.handed) == (
        "correction",
        "fix",
        fix_call,
        ("punctuation",),
    )
    assert (prompt.kind, prompt.detail, prompt.text) == (
        "prompt_step",
        "review",
        "Please review your change.",
    )
    assert lanes.behind_the_scenes[5].kind == "stop"


def test_transcript_prints_the_three_lanes_as_text(store, assessed, capsys):
    assert main(["--store", str(store.root), "transcript", assessed.run_id]) == 0
    printed = capsys.readouterr().out
    attempt_id = assessed.attempts[0].attempt_id
    assert printed.startswith(f"Attempt {attempt_id} of run {assessed.run_id}\n")
    assert "Outcome: pass (all_met) · stop: stand_in_stop" in printed
    conversation = printed.split("== Conversation (what the agent saw) ==\n")[1].split("\n\n")[0]
    assert conversation.splitlines()[:2] == [
        "  turn 1 · user (initial):",
        "      Fix the greeting; ask me which punctuation.",
    ]
    assert "  turn 2 · agent:\n      Done." in conversation
    scenes = printed.split("== Behind the scenes ==\n")[1].split("\n\n")[0]
    assert "  turn 1 · stand-in reply c-" in scenes
    assert "      cites: punctuation" in scenes
    assert scenes.splitlines()[-1] == "  stopped: stand_in_stop"
    assert printed.split("== Gaps ==\n")[1].strip() == (
        "(no gaps: every fact was met and nothing uncovered)"
    )


def test_transcript_json_carries_one_lane_set_per_attempt(store, assessed, capsys):
    attempt_id = assessed.attempts[0].attempt_id
    arguments = ["--store", str(store.root), "--json", "transcript", assessed.run_id]
    assert main([*arguments, "--attempt", attempt_id]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    (lanes,) = data["attempts"]
    assert (data["run_id"], lanes["attempt_id"], lanes["outcome"]) == (
        assessed.run_id,
        attempt_id,
        "pass",
    )
    assert [line["role"] for line in lanes["conversation"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_transcript_refuses_an_unknown_attempt(store, assessed, capsys):
    arguments = ["--store", str(store.root), "transcript", assessed.run_id, "--attempt", "a-none"]
    assert main(arguments) != 0
    assert "has no attempt a-none" in capsys.readouterr().err


@pytest.mark.integration
def test_a_real_fix_round_shows_the_raised_fact_and_the_correction(
    store, graded_benchmark, tmp_path
):
    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, broken_then_fixed(draft)),
        options=RunOptions(mode="offline-fixture", workflow=True),
        host=HOST,
    )
    execution = ExecutionJournal(RunStore(store.root).inspect(result.run_id))
    (attempt_id,) = execution.attempts
    lanes = review_lanes(
        load_material(store, run_refs(store, execution, attempt_id), execution.journal.events)
    )
    kinds = [scene.kind for scene in lanes.behind_the_scenes]
    assert kinds == [
        "stand_in",
        "stand_in",
        "inspection",
        "stand_in",
        "correction",
        "stand_in",
        "inspection",
        "stop",
    ]
    first = lanes.behind_the_scenes[2]
    assert {item.fact_id: item.verdict for item in first.facts} == {
        "punctuation": "unmet",
        "interface": "met",
    }
    fix = lanes.behind_the_scenes[3]
    assert (fix.purpose, fix.handed, fix.text) == ("fix_request", ("punctuation",), FIX_REPLY)
    correction = lanes.behind_the_scenes[4]
    assert (correction.call_id, correction.handed) == (fix.call_id, ("punctuation",))
    assert lanes.conversation[4].text == FIX_REPLY
    assert (lanes.conversation[4].author, lanes.conversation[4].origin) == (
        "stand_in",
        fix.call_id,
    )
    text = render_lanes(lanes)
    assert f"stand-in fix request {fix.call_id}: reply\n      handed: punctuation" in text
    assert "correction in phase fix via" in text
    portable = review_lanes(load_material(store, run_refs(store, execution, attempt_id), None))
    marks = {"inspection", "correction", "prompt_step"}
    assert [
        (scene.kind, scene.turn) for scene in portable.behind_the_scenes if scene.kind in marks
    ] == [(scene.kind, scene.turn) for scene in lanes.behind_the_scenes if scene.kind in marks]
    assert correction.turn == fix.turn


def test_a_report_review_loads_the_case_the_attempt_was_graded_against(
    store, graded_benchmark, assessed
):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    regraded = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"initial_prompt": "Regraded prompt."}),
        files=store.read_blobs(graded_benchmark.cases[0]),
        references=(case.persona_id, case.repository_id),
    )
    result = assessed_result(store, assessed).model_copy(update={"graded_case_id": regraded})
    execution = ExecutionJournal(RunStore(store.root).inspect(assessed.run_id))
    (attempt_id,) = execution.attempts
    refs = replace(run_refs(store, execution, attempt_id), assessment_id=None)
    material = load_material(store, refs, None, assessment=result)
    assert (material.assessment, material.graded_case.initial_prompt) == (
        result,
        "Regraded prompt.",
    )
