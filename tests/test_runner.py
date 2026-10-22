import json
import threading
import time
from pathlib import Path

import pytest

from conftest import (
    CHANNEL,
    EDIT_PERMISSION,
    FIX_SCOPE,
    HOST,
    AskedQuestion,
    ClaudeQuestionUI,
    ScreenUI,
    live_codex_home,
    workflow_benchmark,
)
from dryheave.cli import main
from dryheave.drivers.models import CleanupReport, ProcessIdentity, Screen
from dryheave.errors import ConflictError, InputError, NotFoundError
from dryheave.experiments import create_experiment, load_experiment
from dryheave.fixture_subject import FixtureTerminal
from dryheave.host import HostContext
from dryheave.journals import RunStore
from dryheave.models import ObjectKind, TrialStage
from dryheave.runner import load_capture, run_experiment, run_status
from dryheave.runner_attempt import _combine_cleanup
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json


def test_run_options_drops_workflow_when_false() -> None:
    options = RunOptions(mode="offline-fixture")
    assert "workflow" not in json.loads(canonical_json(options))
    enabled = options.model_copy(update={"workflow": True})
    assert json.loads(canonical_json(enabled))["workflow"] is True


@pytest.mark.integration
def test_fixture_clarification_capture_and_resume_without_paid_launch(store, benchmark):
    identifier = create_experiment(store, benchmark)
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    assert len(result.pending_assessment) == 1
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.mode, capture.assessment, capture.subject_completion) == (
        "offline-fixture",
        "pending",
        "unassessed",
    )
    assert (capture.accepted_turns, capture.delivery_attempts, capture.stop_reason) == (
        2,
        2,
        "simulator_stop",
    )
    assert [item.text for item in capture.dialogue] == [
        "Fix the greeting; ask me which punctuation.",
        "Which punctuation?",
        "Use a comma after Hello.",
        "Done.",
    ]
    assert capture.workspace.complete
    assert capture.cleanup.known_writers_stopped and capture.cleanup.logs_drained
    assert (
        store.read_blob(result.pending_assessment[0], "workspace/added.txt") == b"new task file\n"
    )
    resumed = run_experiment(store, resume=result.run_id, host=HOST)
    assert resumed == result
    retry = run_experiment(
        store,
        resume=result.run_id,
        retry=(result.attempts[0].trial_id,),
        host=HOST,
    )
    assert len(retry.attempts) == 2
    assert len(retry.pending_assessment) == 2
    assert len({state.workspace for state in retry.attempts}) == 2


@pytest.mark.integration
def test_journal_ahead_of_checkpoint_recovery_preserves_launch_intent(
    store, benchmark, monkeypatch
):
    identifier = create_experiment(store, benchmark)
    experiment = load_experiment(store, identifier)
    run_id = RunStore(store.root).create(identifier)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
        state = execution.reserve(experiment.trials[0].trial_id)
        execution.save(state.model_copy(update={"stage": TrialStage.PREPARING}))
        monkeypatch.setattr(journal, "checkpoint", lambda _state: None)
        execution.save(
            state.model_copy(update={"stage": TrialStage.LAUNCHING, "launch_attempted": True})
        )
    result = run_experiment(store, resume=run_id, host=HOST)
    assert len(result.attempts) == 1
    assert result.attempts[0].launch_attempted
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.accepted_turns) == ("interrupted", 0)
    assert not capture.workspace.complete


@pytest.mark.integration
def test_conflicting_resume_and_tampered_capture_map_rejected(store, benchmark):
    identifier = create_experiment(store, benchmark)
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    other = create_experiment(store, benchmark.model_copy(update={"seed": 3}))
    with pytest.raises(ConflictError, match="differs"):
        run_experiment(store, other, resume=result.run_id, host=HOST)
    captured = load_capture(store, result.pending_assessment[0])
    bad = store.put(ObjectKind.CAPTURE, captured, references=(identifier,))
    with pytest.raises((InputError, NotFoundError)):
        load_capture(store, bad)


def test_cancellation_preserves_unstarted_trials_and_no_launch(store, benchmark):
    identifier = create_experiment(store, benchmark)
    cancelled = threading.Event()
    cancelled.set()
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        cancelled=cancelled,
        host=HOST,
    )
    assert result.attempts == ()
    assert len(result.unstarted_trials) == 1
    assert run_status(store, result.run_id) == result


@pytest.mark.integration
def test_cli_experiment_to_capture_and_status(store, benchmark, tmp_path, capsys):
    import json

    draft = tmp_path / "experiment.json"
    draft.write_text(benchmark.model_dump_json())
    prefix = ["--store", str(store.root), "--json"]
    assert main([*prefix, "experiment", "validate", str(draft)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["trial_count"] == 1
    assert main([*prefix, "experiment", "create", str(draft)]) == 0
    identifier = json.loads(capsys.readouterr().out)["data"]["id"]
    assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
    result = json.loads(capsys.readouterr().out)["data"]
    assert main([*prefix, "run", "--status", result["run_id"]]) == 0
    assert (
        json.loads(capsys.readouterr().out)["data"]["pending_assessment"]
        == result["pending_assessment"]
    )
    assert main([*prefix, "capture", result["pending_assessment"][0]]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["capture"]["assessment"] == "pending"


def test_native_requires_explicit_short_runtime(store, benchmark):
    identifier = create_experiment(store, benchmark)
    with pytest.raises(InputError, match="runtime-root"):
        run_experiment(store, identifier, host=HOST)
    with pytest.raises(InputError, match="70-byte"):
        run_experiment(
            store,
            identifier,
            options=RunOptions(runtime_root="/" + "x" * 70, transport="/fixture/tui-test"),
            host=HOST,
        )


@pytest.mark.integration
def test_real_cli_resume_and_status_ignore_changed_workspace_runtime(store, benchmark, tmp_path):
    import json
    import subprocess
    import sys

    from dryheave.workspaces import CONFIG_FILE

    identifier = create_experiment(store, benchmark)
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    (tmp_path / CONFIG_FILE).write_text(
        f'store = {json.dumps(str(store.root))}\nruntime = "{"long" * 40}"\n'
    )
    for operation in ("--resume", "--status"):
        resumed = subprocess.run(
            [sys.executable, "-m", "dryheave", "run", operation, result.run_id, "--json"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert resumed.returncode == 0, resumed.stderr
        data = json.loads(resumed.stdout)["data"]
        assert data["options"] == result.options.model_dump(mode="json")
        assert data["attempts"] == result.model_dump(mode="json")["attempts"]


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["native", "offline-fixture"])
def test_new_cli_run_uses_workspace_runtime_only_for_native(
    store, benchmark, tmp_path, monkeypatch, capsys, mode
):
    import json

    from dryheave.workspaces import CONFIG_FILE

    identifier = create_experiment(store, benchmark)
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    runtime = (Path(".cache") / "rt").absolute()
    (tmp_path / CONFIG_FILE).write_text(
        f"store = {json.dumps(str(store.root))}\nruntime = {json.dumps(str(runtime))}\n"
    )
    observed = []

    def launch(_store, _reference, **kwargs):
        observed.append(kwargs["options"])
        return result

    monkeypatch.setattr("dryheave.runner_cli.run_experiment", launch)
    monkeypatch.chdir(tmp_path)
    assert main(["run", identifier, "--mode", mode, "--json"]) == 0
    capsys.readouterr()
    assert observed[-1].runtime_root == (str(runtime) if mode == "native" else None)
    assert observed[-1].mode == mode
    if mode == "native":
        assert main(["run", identifier, "--runtime-root", "override", "--json"]) == 2
        assert "native limit" in json.loads(capsys.readouterr().err)["error"]["message"]
        override = runtime.with_name("other")
        assert main(["run", identifier, "--runtime-root", str(override), "--json"]) == 0
        capsys.readouterr()
        assert observed[-1].runtime_root == str(override)


@pytest.mark.integration
def test_turn_and_controller_budgets_stop_further_submission(store, benchmark):
    from dryheave.controller_models import RoleBudget

    recipe = benchmark.simulator.model_copy(update={"budget": RoleBudget(max_calls=0)})
    identifier = create_experiment(store, benchmark.model_copy(update={"simulator": recipe}))
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.accepted_turns, capture.controller_calls) == (
        "controller_budget",
        1,
        (),
    )


@pytest.mark.integration
def test_stop_writers_then_capture_reads_their_final_bytes(store, benchmark, monkeypatch):

    from dryheave.fixture_subject import FixtureTerminal

    original = FixtureTerminal.close

    def close(terminal):
        if terminal.plan is not None:
            (Path(terminal.plan.cwd) / "last-write.txt").write_text("written during owned shutdown")
        return original(terminal)

    monkeypatch.setattr(FixtureTerminal, "close", close)
    result = run_experiment(
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    assert (
        store.read_blob(result.pending_assessment[0], "workspace/last-write.txt")
        == b"written during owned shutdown"
    )


@pytest.mark.integration
def test_controller_intent_is_interrupted_on_recovery_without_reexecution(store, benchmark):
    from dryheave.controller_models import RoleCall

    identifier = create_experiment(store, benchmark)
    experiment = load_experiment(store, identifier)
    run_id = RunStore(store.root).create(identifier)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
        state = execution.reserve(experiment.trials[0].trial_id)
        execution.save(
            state.model_copy(
                update={
                    "stage": TrialStage.PREPARING,
                    "calls": (RoleCall(call_id="possibly-paid", status="intent"),),
                }
            )
        )
    result = run_experiment(store, resume=run_id, host=HOST)
    call = load_capture(store, result.pending_assessment[0]).controller_calls[0]
    assert (call.call_id, call.status, call.usage, call.cost, call.elapsed_seconds) == (
        "possibly-paid",
        "interrupted",
        None,
        None,
        None,
    )


@pytest.mark.integration
def test_permission_dialog_stops_without_a_simulator_call(store, benchmark):
    turn = benchmark.fixture[0].model_copy(update={"state": "approval"})
    result = run_experiment(
        store,
        create_experiment(store, benchmark.model_copy(update={"fixture": (turn,)})),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "needs_input"
    assert capture.controller_calls == ()


@pytest.mark.integration
def test_native_and_run_locks_prevent_duplicate_work(store, benchmark):
    from dryheave.errors import LockBusyError

    identifier = create_experiment(store, benchmark)
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    with RunStore(store.root).open(result.run_id), pytest.raises(LockBusyError):
        run_experiment(store, resume=result.run_id, host=HOST)
    with store.native_lock(), pytest.raises(LockBusyError):
        run_experiment(store, resume=result.run_id, host=HOST)
    assert run_status(store, result.run_id).attempts == result.attempts


@pytest.mark.integration
@pytest.mark.parametrize("interrupted", [False, True])
def test_controller_cleanup_failure_preserves_capture_after_fresh_reconciliation(
    store, benchmark, monkeypatch, interrupted
):
    import sys

    from dryheave.controller_models import ControllerRecipe
    from dryheave.drivers.artifacts import ArtifactWriter
    from dryheave.models import CommandSpec
    from dryheave.process_ownership import ProcessOwner

    class DeniedOwner(ProcessOwner):
        def stop(self, *, timeout, terminal_closed):
            report = super().stop(timeout=timeout, terminal_closed=terminal_closed)
            return report.model_copy(
                update={
                    "known_writers_stopped": False,
                    "survivors": report.owned,
                    "errors": ("process_signal_denied",),
                }
            )

    record = ArtifactWriter.record

    def interrupt(writer, name, value):
        result = record(writer, name, value)
        if interrupted and name == "cleanup" and "controller" in writer.root.parts:
            raise KeyboardInterrupt
        return result

    def capture(*args):
        pytest.fail("A live writer must prevent final file snapshotting.")

    monkeypatch.setattr("dryheave.controllers.ProcessOwner", DeniedOwner)
    monkeypatch.setattr(ArtifactWriter, "record", interrupt)
    monkeypatch.setattr("dryheave.runner_attempt.capture_workspace", capture)
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(
            argv=(sys.executable, "-c", 'print(\'{"decision":{"action":"stop","reason":"done"}}\')')
        ),
    )
    identifier = create_experiment(
        store, benchmark.model_copy(update={"simulator": recipe, "repetitions": 2})
    )
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    assert len(result.attempts) == len(result.unstarted_trials) == 1
    captured = load_capture(store, result.pending_assessment[0])
    call = captured.controller_calls[0]
    assert call.status == ("interrupted" if interrupted else "failed")
    assert captured.cleanup.terminal_closed
    assert captured.cleanup.known_writers_stopped is False
    assert captured.cleanup.survivors == call.cleanup.survivors
    assert call.cleanup.survivors[0] in captured.cleanup.owned
    assert captured.cleanup.errors == ("process_signal_denied",)
    assert captured.workspace.complete is False
    assert captured.evidence_omissions == ("cleanup_unresolved",)
    monkeypatch.undo()
    resumed = run_experiment(
        store,
        resume=result.run_id,
        retry=(result.attempts[0].trial_id,),
        host=HOST,
    )
    assert len(resumed.attempts) == 3
    assert resumed.unstarted_trials == ()
    assert resumed.attempts[0].capture_id == result.attempts[0].capture_id
    assert load_capture(store, result.pending_assessment[0]) == captured
    journal = RunStore(store.root).inspect(result.run_id)
    fresh = [
        event
        for event in journal.events
        if event.event == "ownership-reconciled"
        and event.attempt_id == result.attempts[0].attempt_id
    ]
    assert fresh[-1].data["known_writers_stopped"] is True
    from dryheave.assessments import assess_run
    from dryheave.integrity import load_assessment

    historical = load_assessment(store, assess_run(store, result.run_id)[0])
    assert historical.eligible is False
    assert "cleanup_unresolved" in historical.exclusion_reasons


@pytest.mark.integration
def test_load_capture_batch_traversal_does_not_grow_per_file(store, benchmark, monkeypatch):
    additions = {f"file-{index}.txt": str(index) for index in range(30)}
    turn = benchmark.fixture[-1].model_copy(
        update={"files": benchmark.fixture[-1].files | additions}
    )
    modified = benchmark.model_copy(update={"fixture": (*benchmark.fixture[:-1], turn)})
    summary = run_experiment(
        store,
        create_experiment(store, modified),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    identifier = summary.pending_assessment[0]
    original = store._manifest
    visits = []

    def visit(current):
        visits.append(current)
        return original(current)

    monkeypatch.setattr(store, "_manifest", visit)
    capture = load_capture(store, identifier)
    assert {entry.path for entry in capture.workspace.files} >= set(additions)
    assert visits.count(identifier) == 3


@pytest.mark.integration
def test_a_failed_simulator_call_is_retried_once(store, benchmark, tmp_path):
    import sys

    from dryheave.controller_models import ControllerRecipe
    from dryheave.models import CommandSpec

    marker = tmp_path / "failed-once"
    script = (
        "import pathlib,sys\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "if not marker.exists():\n    marker.touch()\n    sys.exit(3)\n"
        'print(\'{"decision":{"action":"stop","reason":"done"}}\')\n'
    )
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "-c", script)),
    )
    identifier = create_experiment(store, benchmark.model_copy(update={"simulator": recipe}))
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    captured = load_capture(store, result.pending_assessment[0])
    assert [call.status for call in captured.controller_calls] == ["failed", "completed"]
    assert captured.stop_reason == "simulator_stop"


def test_attempt_errors_name_their_type_and_origin():
    from dryheave.runner_attempt import describe_error

    try:
        raise PermissionError(1, "Operation not permitted")
    except PermissionError as error:
        described = describe_error(error)
    assert described.startswith(
        "PermissionError: [Errno 1] Operation not permitted (test_runner.py:"
    )


@pytest.mark.integration
def test_setup_spawning_many_processes_journals_a_bounded_number_of_ownership_events(
    store, benchmark
):
    import math
    import sys

    from dryheave.cases import load_frozen_case
    from dryheave.models import CommandSpec
    from dryheave.process_ownership import PUBLISH_INTERVAL

    case_id = store.resolve("task")
    case = load_frozen_case(store, case_id)
    script = (
        "import subprocess,sys,time\n"
        "children=[]\n"
        "for _ in range(60):\n"
        " children.append(subprocess.Popen(['sleep','2']))\n"
        " time.sleep(.03)\n"
        "[child.wait() for child in children]\n"
    )
    frozen = store.put(
        ObjectKind.CASE,
        case.model_copy(
            update={
                "setup": (CommandSpec(argv=(sys.executable, "-c", script), timeout_seconds=30),)
            }
        ),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    identifier = create_experiment(store, benchmark.model_copy(update={"cases": (frozen,)}))
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    journal = RunStore(store.root).inspect(result.run_id)
    owned = ExecutionJournal(journal).attempts[result.attempts[0].attempt_id].owned
    batches = [event for event in journal.events if event.event == "owned-processes"]
    span = (batches[-1].timestamp - batches[0].timestamp).total_seconds()
    assert len(owned) >= 20
    assert len({(item.pid, item.created) for item in owned}) == len(owned)
    assert len(batches) <= 3 + math.ceil(span / PUBLISH_INTERVAL)
    assert len(batches) < len(owned)
    assert not any(event.event == "owned-process" for event in journal.events)


FIXED_GREETING = 'def greet(name):\n    return "Hello, " + name\n'
FIX_REPLY = "When I greet someone there is no comma after Hello."


def workflow_options() -> RunOptions:
    return RunOptions(mode="offline-fixture", workflow=True)


def workflow_events(capture):
    return [(item.kind, item.outcome) for item in capture.workflow.events]


def broken_then_fixed(benchmark, *, max_calls=4):
    from dryheave.controller_models import RoleBudget, ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn

    broken = benchmark.fixture[1].model_copy(update={"files": {"added.txt": "new task file\n"}})
    return benchmark.model_copy(
        update={
            "fixture": (
                benchmark.fixture[0],
                broken,
                FixtureTurn(
                    prompt=FIX_REPLY, assistant="Fixed.", files={"greet.py": FIXED_GREETING}
                ),
            ),
            "simulator": benchmark.simulator.model_copy(
                update={
                    "budget": RoleBudget(max_calls=max_calls),
                    "script": (
                        *benchmark.simulator.script,
                        ScriptStep(
                            match="contains",
                            assistant="Done.",
                            decision=SimulatorDecision(
                                action="reply",
                                text=FIX_REPLY,
                                reason="Using the greeting shows the comma is missing.",
                            ),
                        ),
                        ScriptStep(
                            assistant="Fixed.",
                            decision=SimulatorDecision(
                                action="stop", reason="The greeting now works."
                            ),
                        ),
                    ),
                },
            ),
        }
    )


@pytest.mark.integration
def test_default_workflow_inspects_once_then_grading_runs_its_own_judge(
    store, graded_benchmark, tmp_path
):
    from dryheave.assessments import assess_run
    from dryheave.integrity import load_assessment

    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert (capture.workflow.steps, capture.workflow.position) == (("inspect-required",), 1)
    assert workflow_events(capture) == [("inspect", "passed")]
    (inspected,) = [call for call in capture.controller_calls if call.role == "judge"]
    assert inspected.status == "completed"
    assert capture.workflow.events[0].call_ids == (inspected.call_id,)
    assert capture.workflow.events[0].seconds > 0
    (sent,) = [json.loads(path.read_text()) for path in inspector.glob("request-*.json")]
    assert [item["criterion_id"] for item in sent["criteria"]] == ["quality"]
    assert {"role": "assistant", "text": "Which punctuation?"} in sent["transcript"]
    assert any(name.startswith("evidence/inspection/") for name in capture.evidence_files)

    assessment = load_assessment(store, assess_run(store, result.run_id)[0])
    journal = RunStore(store.root).inspect(result.run_id)
    graded = [
        event.data["call_id"]
        for event in journal.events
        if event.event == "judge-result" and event.attempt_id == capture.attempt_id
    ]
    assert len(graded) == 1
    quality = next(item for item in assessment.criteria if item.criterion_id == "quality")
    assert quality.outcome == "pass"
    judge = next(item for item in assessment.roles if item.role == "judge")
    assert {record.identity for record in judge.records} == {graded[0], inspected.call_id}
    simulator = next(item for item in assessment.roles if item.role == "simulator")
    assert inspected.call_id not in {record.identity for record in simulator.records}


@pytest.mark.integration
def test_failed_inspection_sends_a_fix_request_then_passes(store, graded_benchmark, tmp_path):
    from dryheave.controller_models import FIX_IT_INSTRUCTION

    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, broken_then_fixed(draft)),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert [call.role for call in capture.controller_calls] == [
        "simulator",
        "simulator",
        "judge",
        "simulator",
        "simulator",
        "judge",
    ]
    assert workflow_events(capture) == [
        ("inspect", "failed"),
        ("inspect", "fix_requested"),
        ("inspect", "passed"),
    ]
    failed, requested, _ = capture.workflow.events
    assert failed.failed_criteria == requested.failed_criteria == ("greeting",)
    fix_call = capture.controller_calls[3]
    assert requested.call_ids == (fix_call.call_id,)
    assert fix_call.decision.text == FIX_REPLY
    assert capture.dialogue[-2].text == FIX_REPLY
    blobs = store.read_blobs(result.pending_assessment[0])
    request = json.loads(blobs[f"evidence/controller/{fix_call.call_id}/000001-request.json"])
    assert request["instruction"] == FIX_IT_INSTRUCTION
    assert request["inspection"] == [
        {
            "criterion_id": "greeting",
            "rationale": "Validate punctuation and correct name handling. (failed)",
        }
    ]


@pytest.mark.integration
def test_a_simulator_that_keeps_stopping_on_findings_gets_a_corrected_retry_then_a_fallback(
    store, graded_benchmark, tmp_path
):
    from dryheave.controller_models import ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn
    from dryheave.runner_attempt import FALLBACK_FIX_REPLY, STOP_CORRECTION

    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    fixed = broken_then_fixed(draft, max_calls=5)
    stop = ScriptStep(
        match="contains",
        assistant="Done.",
        decision=SimulatorDecision(action="stop", reason="Looks done to me."),
    )
    script = fixed.simulator.script
    stubborn = fixed.model_copy(
        update={
            "fixture": (
                *fixed.fixture[:2],
                FixtureTurn(
                    prompt=FALLBACK_FIX_REPLY,
                    assistant="Fixed.",
                    files={"greet.py": FIXED_GREETING},
                ),
            ),
            "simulator": fixed.simulator.model_copy(
                update={"script": (*script[:2], stop, stop, script[3])}
            ),
        }
    )
    result = run_experiment(
        store,
        create_experiment(store, stubborn),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert workflow_events(capture) == [
        ("inspect", "failed"),
        ("inspect", "fix_requested"),
        ("inspect", "passed"),
    ]
    first, retried = [
        call for call in capture.controller_calls if call.error == "stop_with_findings"
    ]
    blobs = store.read_blobs(result.pending_assessment[0])

    def request(call):
        return json.loads(blobs[f"evidence/controller/{call.call_id}/000001-request.json"])

    assert "correction" not in request(first)
    assert request(retried)["correction"] == STOP_CORRECTION
    assert capture.dialogue[-2].text == FALLBACK_FIX_REPLY
    journal = RunStore(store.root).inspect(result.run_id)
    (fallback,) = [event for event in journal.events if event.event == "simulator-fallback"]
    assert fallback.data["text"] == FALLBACK_FIX_REPLY


@pytest.mark.integration
def test_exhausted_simulator_budget_keeps_its_stop_reason_and_progress(
    store, graded_benchmark, tmp_path
):
    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, broken_then_fixed(draft, max_calls=3)),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "controller_budget"
    assert workflow_events(capture) == [("inspect", "failed"), ("inspect", "fix_requested")]
    assert capture.workflow.position == 0
    assert capture.accepted_turns == 3


@pytest.mark.integration
def test_prompt_step_is_delivered_before_the_inspection(store, graded_benchmark, tmp_path):
    from dryheave.cases import InspectStep, PromptStep
    from dryheave.controller_models import ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn

    draft, _ = workflow_benchmark(
        store,
        graded_benchmark,
        tmp_path,
        workflow=(
            PromptStep(step_id="docs", prompt="Update the docs."),
            InspectStep(step_id="check", criteria=("greeting",)),
        ),
    )
    draft = draft.model_copy(
        update={
            "fixture": (
                *draft.fixture,
                FixtureTurn(
                    prompt="Update the docs.",
                    assistant="Docs updated.",
                    files={"README.md": "Greets with a comma.\n"},
                ),
            ),
            "simulator": draft.simulator.model_copy(
                update={
                    "script": (
                        *draft.simulator.script,
                        ScriptStep(
                            assistant="Docs updated.",
                            decision=SimulatorDecision(action="stop", reason="Docs are done."),
                        ),
                    )
                }
            ),
        }
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert workflow_events(capture) == [("prompt", "sent"), ("inspect", "passed")]
    assert [(item.step_id, item.turn) for item in capture.workflow.events] == [
        ("docs", 2),
        ("check", 3),
    ]
    assert capture.dialogue[-2].text == "Update the docs."
    assert all(call.role == "simulator" for call in capture.controller_calls)


def claude_profile(store) -> str:
    import sys

    from dryheave.models import AgentKind
    from dryheave.profile_models import CaptureSpec, NativeRecipe
    from dryheave.profiles import capture_profile

    return capture_profile(
        store,
        CaptureSpec(recipe=NativeRecipe(agent=AgentKind.CLAUDE, executable=sys.executable)),
    )


def slash_draft(
    store, graded_benchmark, tmp_path, prompt="/code-review --quick", typed="/code-review --quick"
):
    from dryheave.cases import PromptStep
    from dryheave.controller_models import ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn

    draft, _ = workflow_benchmark(
        store,
        graded_benchmark,
        tmp_path,
        workflow=(PromptStep(step_id="review", prompt=prompt),),
    )
    return draft.model_copy(
        update={
            "fixture": (
                *draft.fixture,
                FixtureTurn(prompt=typed, assistant="Review clean."),
            ),
            "simulator": draft.simulator.model_copy(
                update={
                    "script": (
                        *draft.simulator.script,
                        ScriptStep(
                            assistant="Review clean.",
                            decision=SimulatorDecision(action="stop", reason="Reviewed."),
                        ),
                    )
                }
            ),
        }
    )


@pytest.mark.integration
@pytest.mark.parametrize("prompt", ["/code-review --quick", "  \n/code-review --quick"])
def test_slash_prompt_step_is_delivered_as_a_curated_native_command(
    store, graded_benchmark, tmp_path, prompt
):
    from dryheave.experiment_models import VariantSpec

    draft = slash_draft(store, graded_benchmark, tmp_path, prompt).model_copy(
        update={"variants": (VariantSpec(name="claude", profile=claude_profile(store)),)}
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert [sorted(event.model_dump(mode="json")) for event in capture.workflow.events] == [
        ["call_ids", "failed_criteria", "kind", "outcome", "seconds", "step_id", "turn"]
    ]
    journal = RunStore(store.root).inspect(result.run_id)
    assert [
        event.data["policy"] for event in journal.events if event.event == "submission-intent"
    ] == ["conversation", "conversation", "curated-native-command"]


@pytest.mark.parametrize("prompt", ["/code-review --quick", "  /code-review --quick"])
def test_slash_prompt_steps_are_refused_before_launch_for_other_agents(
    store, graded_benchmark, tmp_path, prompt
):
    draft = slash_draft(store, graded_benchmark, tmp_path, prompt)
    with pytest.raises(InputError, match="only delivered to Claude and to Codex on its live"):
        run_experiment(
            store,
            create_experiment(store, draft),
            options=workflow_options(),
            host=HOST,
        )
    assert RunStore(store.root).list_runs() == ()


def codex_live_profile(store):
    import sys

    from dryheave.bench import parse_variant, variant_recipe
    from dryheave.profile_models import CaptureSpec
    from dryheave.profiles import capture_profile

    return capture_profile(
        store,
        CaptureSpec(
            recipe=variant_recipe(parse_variant("terra=codex:gpt-5.6-terra@high"), sys.executable)
        ),
    )


def test_codex_workflow_skills_name_each_live_codex_variants_skills(
    store, graded_benchmark, tmp_path
):
    from dryheave.experiment_models import VariantSpec
    from dryheave.runner import codex_workflow_skills, require_workflow_support, workflow_plan

    draft = slash_draft(store, graded_benchmark, tmp_path).model_copy(
        update={
            "variants": (
                VariantSpec(name="terra", profile=codex_live_profile(store)),
                VariantSpec(name="claude", profile=claude_profile(store)),
            )
        }
    )
    experiment = load_experiment(store, create_experiment(store, draft))
    plan = workflow_plan(store, experiment)
    require_workflow_support(plan)
    assert sorted((trial.variant, trial.skill_names) for trial in plan.slash_trials) == [
        ("claude", ("code-review",)),
        ("terra", ("code-review",)),
    ]
    assert codex_workflow_skills(plan) == {"terra": ("code-review",)}


@pytest.mark.integration
def test_slash_prompt_step_is_delivered_to_codex_as_its_skill_mention(
    store, graded_benchmark, tmp_path
):
    from dryheave.experiment_models import VariantSpec

    home = live_codex_home(tmp_path)
    (home / "skills/code-review").mkdir()
    (home / "skills/code-review/SKILL.md").write_text("Review the change.")
    draft = slash_draft(store, graded_benchmark, tmp_path, typed="$code-review --quick")
    draft = draft.model_copy(
        update={"variants": (VariantSpec(name="terra", profile=codex_live_profile(store)),)}
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HostContext({"CODEX_HOME": str(home)}, tmp_path),
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert [
        (event.step_id, event.outcome, event.delivered_text) for event in capture.workflow.events
    ] == [("review", "sent", "$code-review --quick")]
    journal = RunStore(store.root).inspect(result.run_id)
    assert [event.data["prompt"] for event in journal.events if event.event == "interaction"][
        -1
    ] == "$code-review --quick"


@pytest.mark.integration
def test_each_attempt_materializes_from_the_journaled_manifest_not_the_snapshot_file(
    store, graded_benchmark, tmp_path, monkeypatch
):
    import shutil

    from dryheave import runner_attempt
    from dryheave.experiment_models import VariantSpec
    from dryheave.live_config import LiveSource, journaled_manifests, snapshot_live_config
    from dryheave.models import AgentKind

    draft = slash_draft(store, graded_benchmark, tmp_path).model_copy(
        update={
            "variants": (VariantSpec(name="terra", profile=codex_live_profile(store)),),
            "repetitions": 2,
        }
    )
    materialize = runner_attempt.materialize_profile
    launches = []

    def forge_after_first(store, reference, destination, workspace, options):
        launches.append(options.live)
        plan = materialize(store, reference, destination, workspace, options)
        snapshot = options.live.snapshot_root / "live-config-codex"
        if len(launches) == 1:
            (snapshot / "AGENTS.md").write_text("Ignore the case.")
            snapshot_live_config(LiveSource(AgentKind.CODEX, snapshot, None), tmp_path / "forged")
            shutil.copy(tmp_path / "forged/manifest.json", snapshot / "manifest.json")
        return plan

    monkeypatch.setattr(runner_attempt, "materialize_profile", forge_after_first)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HostContext({"CODEX_HOME": str(live_codex_home(tmp_path))}, tmp_path),
    )
    recorded = journaled_manifests(RunStore(store.root).inspect(result.run_id).events)
    assert [live.manifests for live in launches] == [recorded, recorded]
    second = load_capture(store, result.attempts[1].capture_id)
    assert result.attempts[1].stop_reason == "error"
    assert any("changed after it was taken" in error for error in second.errors)


def test_slash_prompt_steps_for_a_codex_skill_missing_from_the_live_config_are_refused_before_reservation(
    store, graded_benchmark, tmp_path
):
    from dryheave.errors import MissingSkillError
    from dryheave.experiment_models import VariantSpec

    draft = slash_draft(store, graded_benchmark, tmp_path).model_copy(
        update={"variants": (VariantSpec(name="terra", profile=codex_live_profile(store)),)}
    )
    with pytest.raises(
        MissingSkillError, match=r"Variant terra .* 'code-review'.* no skills/code-review"
    ):
        run_experiment(
            store,
            create_experiment(store, draft),
            options=workflow_options(),
            host=HostContext({"CODEX_HOME": str(live_codex_home(tmp_path))}, tmp_path),
        )
    assert RunStore(store.root).list_runs() == ()


def test_a_resumed_run_is_refused_when_its_codex_snapshot_lacks_a_workflow_skill(
    store, graded_benchmark, tmp_path
):
    from dryheave.errors import MissingSkillError
    from dryheave.experiment_models import VariantSpec

    home = live_codex_home(tmp_path)
    draft = slash_draft(store, graded_benchmark, tmp_path).model_copy(
        update={"variants": (VariantSpec(name="terra", profile=codex_live_profile(store)),)}
    )
    experiment_id = create_experiment(store, draft)
    run_id = RunStore(store.root).create(store.resolve(experiment_id))
    with pytest.raises(
        MissingSkillError, match=r"Variant terra .* no skills/code-review/SKILL\.md\.$"
    ):
        run_experiment(
            store,
            resume=run_id,
            options=workflow_options(),
            host=HostContext({"CODEX_HOME": str(home)}, tmp_path),
        )
    (home / "skills/code-review").mkdir()
    (home / "skills/code-review/SKILL.md").write_text("Review the change.")
    with pytest.raises(MissingSkillError, match="no skills/code-review"):
        run_experiment(
            store,
            resume=run_id,
            options=workflow_options(),
            host=HostContext({"CODEX_HOME": str(home)}, tmp_path),
        )
    assert [event.event for event in RunStore(store.root).inspect(run_id).events][-1] == (
        "live-config"
    )


def test_inspect_steps_without_an_inspector_are_refused_before_launch(store, graded_benchmark):
    from dryheave.cases import InspectStep, load_frozen_case

    case_id = store.resolve(graded_benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(
            update={"workflow": (InspectStep(step_id="check", criteria=("greeting",)),)}
        ),
        files=store.read_blobs(case_id),
        references=(case.persona_id, case.repository_id),
    )
    identifier = create_experiment(store, graded_benchmark.model_copy(update={"cases": (changed,)}))
    with pytest.raises(InputError, match="inspector"):
        run_experiment(store, identifier, options=workflow_options(), host=HOST)
    assert RunStore(store.root).list_runs() == ()


@pytest.mark.integration
def test_declined_stop_ends_the_run_without_an_inspection(store, graded_benchmark, tmp_path):
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    first, last = draft.simulator.script
    declined = last.model_copy(
        update={"decision": last.decision.model_copy(update={"stop_kind": "declined"})}
    )
    draft = draft.model_copy(
        update={"simulator": draft.simulator.model_copy(update={"script": (first, declined)})}
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "simulator_stop"
    assert (capture.workflow.position, capture.workflow.events) == (0, ())
    assert [call.role for call in capture.controller_calls] == ["simulator", "simulator"]
    assert list(inspector.glob("request-*.json")) == []


@pytest.mark.integration
def test_script_exhaustion_ends_the_run_without_running_the_workflow(
    store, graded_benchmark, tmp_path
):
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    first, _last = draft.simulator.script
    draft = draft.model_copy(
        update={"simulator": draft.simulator.model_copy(update={"script": (first,)})}
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "simulator_stop"
    assert (capture.workflow.position, capture.workflow.events) == (0, ())
    assert list(inspector.glob("request-*.json")) == []


@pytest.mark.integration
def test_script_divergence_ends_the_run_without_running_the_workflow(
    store, graded_benchmark, tmp_path
):
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    diverged = draft.fixture[1].model_copy(update={"assistant": "Something else happened."})
    draft = draft.model_copy(update={"fixture": (draft.fixture[0], diverged)})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "simulator_stop"
    assert (capture.workflow.position, capture.workflow.events) == (0, ())
    assert list(inspector.glob("request-*.json")) == []


@pytest.mark.integration
def test_unavailable_inspection_stops_the_run(store, graded_benchmark, tmp_path):
    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path, outcomes=("broken",))
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "inspection_unavailable"
    assert workflow_events(capture) == [("inspect", "unavailable")]
    judged = [call for call in capture.controller_calls if call.role == "judge"]
    assert [call.status for call in judged] == ["failed", "failed"]
    assert capture.workflow.events[0].call_ids == tuple(call.call_id for call in judged)
    journal = RunStore(store.root).inspect(result.run_id)
    (outcome,) = [event for event in journal.events if event.event == "inspection-result"]
    assert outcome.data["result"]["error"] == "judge_unavailable"


@pytest.mark.integration
def test_cancellation_outranks_an_unavailable_inspection(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.inspection import InspectionResult

    def cancelled(request):
        request.cancelled.set()
        return InspectionResult(
            step_id=request.step.step_id,
            status="unavailable",
            findings=(),
            calls=(),
            snapshot_digest=None,
            error="cancelled",
            seconds=0,
        )

    monkeypatch.setattr("dryheave.runner_attempt.inspect_workspace", cancelled)
    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        cancelled=threading.Event(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "cancelled"
    assert workflow_events(capture) == [("inspect", "unavailable")]


@pytest.mark.integration
def test_timeout_outranks_an_unavailable_inspection(store, graded_benchmark, tmp_path, monkeypatch):
    import time as time_module

    from dryheave.inspection import InspectionResult

    expired = {"flag": False}
    real_monotonic = time_module.monotonic

    def fake_monotonic():
        now = real_monotonic()
        return now + 1_000_000.0 if expired["flag"] else now

    def unavailable(request):
        expired["flag"] = True
        return InspectionResult(
            step_id=request.step.step_id,
            status="unavailable",
            findings=(),
            calls=(),
            snapshot_digest=None,
            error="judge_unavailable",
            seconds=0,
        )

    monkeypatch.setattr("dryheave.runner_attempt.inspect_workspace", unavailable)
    monkeypatch.setattr(time_module, "monotonic", fake_monotonic)
    draft, _ = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "timeout"
    assert workflow_events(capture) == [("inspect", "unavailable")]


@pytest.mark.integration
def test_events_arriving_during_inspection_rerun_it_once_then_make_it_unavailable(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.drivers.session import DriverSession

    observed = DriverSession.reconcile
    polls = []

    def growing(session, *, wait=False):
        result = observed(session, wait=wait)
        if not wait:
            polls.append(len(session.events))
            if len(polls) % 2 == 0:
                session.events.append(session.events[-1])
        return result

    monkeypatch.setattr(DriverSession, "reconcile", growing)
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "inspection_unavailable"
    assert len(list(inspector.glob("request-*.json"))) == 2
    assert len(capture.workflow.events[0].call_ids) == 2


@pytest.mark.integration
def test_an_inspection_rerun_shares_the_inspectors_call_budget(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.drivers.session import DriverSession

    observed = DriverSession.reconcile
    polls = []

    def growing(session, *, wait=False):
        result = observed(session, wait=wait)
        if not wait:
            polls.append(len(session.events))
            if len(polls) % 2 == 0:
                session.events.append(session.events[-1])
        return result

    monkeypatch.setattr(DriverSession, "reconcile", growing)
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    recipe = draft.scoring.inspector
    draft = draft.model_copy(
        update={
            "scoring": draft.scoring.model_copy(
                update={
                    "inspector": recipe.model_copy(
                        update={"budget": recipe.budget.model_copy(update={"max_calls": 1})}
                    )
                }
            )
        }
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "inspection_unavailable"
    assert len(list(inspector.glob("request-*.json"))) == 1


@pytest.mark.integration
def test_workflow_off_keeps_todays_run_and_serialization(store, graded_benchmark, tmp_path):
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.workflow) == ("simulator_stop", None)
    assert "workflow" not in json.loads(canonical_json(capture))
    assert not any(name.startswith("evidence/inspection/") for name in capture.evidence_files)
    assert list(inspector.glob("request-*.json")) == []
    journal = RunStore(store.root).inspect(result.run_id)
    states = [event.data for event in journal.events if event.event == "attempt-state"]
    assert states and all("workflow_position" not in data for data in states)
    assert not any(event.event == "workflow-step" for event in journal.events)


@pytest.mark.integration
def test_workflow_off_any_simulator_stop_ends_the_run_as_before(store, graded_benchmark, tmp_path):
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    first, last = draft.simulator.script
    declined = last.model_copy(
        update={"decision": last.decision.model_copy(update={"stop_kind": "declined"})}
    )
    draft = draft.model_copy(
        update={"simulator": draft.simulator.model_copy(update={"script": (first, declined)})}
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.workflow) == ("simulator_stop", None)
    assert list(inspector.glob("request-*.json")) == []
    journal = RunStore(store.root).inspect(result.run_id)
    assert not any(event.event == "workflow-step" for event in journal.events)


@pytest.mark.integration
def test_unchanged_work_reuses_the_failed_verdict_until_the_turn_budget(
    store, graded_benchmark, tmp_path
):
    from dryheave.controller_models import ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn

    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    draft = broken_then_fixed(draft, max_calls=6)
    unchanged = draft.fixture[2].model_copy(update={"files": {}})
    draft = draft.model_copy(
        update={
            "fixture": (
                *draft.fixture[:2],
                unchanged,
                FixtureTurn(prompt=FIX_REPLY, assistant="Fixed again."),
            ),
            "simulator": draft.simulator.model_copy(
                update={
                    "script": (
                        *draft.simulator.script,
                        ScriptStep(
                            match="contains",
                            assistant="Fixed.",
                            decision=SimulatorDecision(
                                action="reply",
                                text=FIX_REPLY,
                                reason="The greeting still lacks the comma.",
                            ),
                        ),
                    )
                }
            ),
        }
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "turn_budget"
    assert workflow_events(capture) == [
        ("inspect", "failed"),
        ("inspect", "fix_requested"),
        ("inspect", "reused"),
        ("inspect", "fix_requested"),
    ]
    assert capture.workflow.events[2].call_ids == ()
    assert len(list(inspector.glob("request-*.json"))) == 1
    assert capture.workflow.position == 0


@pytest.mark.integration
def test_the_inspections_cap_stops_a_workflow_of_passing_inspect_steps(
    store, graded_benchmark, tmp_path
):
    from dryheave.cases import InspectStep

    steps = tuple(
        InspectStep(step_id=f"check-{index}", criteria=("quality",)) for index in range(6)
    )
    draft, inspector = workflow_benchmark(
        store, graded_benchmark, tmp_path, outcomes=("pass",), workflow=steps
    )
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "turn_budget"
    assert workflow_events(capture) == [("inspect", "passed")] * 4
    assert len(list(inspector.glob("request-*.json"))) == 4
    assert capture.workflow.position == 4


class DialogFixtureTerminal(FixtureTerminal):
    def __init__(self, turns, source, agent, *, dialogs, startup=None):
        super().__init__(turns, source, agent)
        self.dialogs, self.startup = dialogs, startup
        self.showing = None
        self.held = []
        self.typed = []

    def state(self):
        if self.startup is not None:
            return Screen(text=self.startup, exited=None)
        if isinstance(self.showing, str):
            return Screen(text=self.showing, exited=None)
        if self.showing is not None and self.showing.open:
            if hasattr(self.showing, "screen"):
                return self.showing.screen()
            return Screen(text=self.showing.render(), exited=None)
        if self.held:
            self.source.batches.append(self.held)
            self.held = []
        return super().state()

    def enter(self):
        super().enter()
        shown = self.dialogs.get(self.pastes[-1])
        if shown is not None:
            batch = self.source.batches.pop()
            self.source.batches.append(batch[:3])
            self.held, self.showing = batch[3:], shown

    def press(self, key):
        super().press(key)
        if not isinstance(self.showing, str | None) and self.showing.open:
            self.showing.press(key)
            if not self.showing.open:
                self.source.batches.append(self.held)
                self.held = []

    def paste(self, text):
        if not isinstance(self.showing, str | None) and self.showing.open:
            self.typed.append(text)
            self.showing.type(text)
            return
        super().paste(text)


SCOPE = AskedQuestion(
    "Scope",
    "Which findings should I fix?",
    (("Everything", "All findings"), ("Critical only", "Just the blockers")),
)
AUTH = AskedQuestion("Auth", "Which auth method?", (("OAuth", "Flow"), ("API key", "Static")))
PERMISSION = "Bash command\n\n  rm -rf build\n\nDo you want to proceed?\n\u276f 1. Yes\n  2. No\n\nEsc to cancel"
TRUST = (
    " Accessing workspace:\n\n Quick safety check: Is this a project you created or one you trust?"
    "\n\n \u276f No, exit\n   Yes, I trust this folder\n\n Enter to confirm \u00b7 Esc to cancel"
)


def dialog_fixture(monkeypatch, dialogs, *, startup=None):
    terminals = []

    def build(turns, source, agent):
        terminal = DialogFixtureTerminal(turns, source, agent, dialogs=dialogs, startup=startup)
        terminals.append(terminal)
        return terminal

    monkeypatch.setattr("dryheave.runner_attempt.FixtureTerminal", build)
    return terminals


def dialog_reply(*choices, text=""):
    from dryheave.controller_models import SimulatorDecision

    return SimulatorDecision(
        action="reply", choices=choices, text=text, reason="Answering as the user would."
    )


def claude_draft(store, draft, *script):
    from dryheave.controller_models import RoleBudget
    from dryheave.experiment_models import VariantSpec

    return draft.model_copy(
        update={
            "variants": (VariantSpec(name="claude", profile=claude_profile(store)),),
            "simulator": draft.simulator.model_copy(
                update={"script": script, "budget": RoleBudget(max_calls=len(script))}
            ),
        }
    )


@pytest.mark.integration
def test_a_prompt_step_dialog_is_answered_and_the_workflow_completes(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.cases import InspectStep, PromptStep
    from dryheave.controller_models import DIALOG_INSTRUCTION, ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn

    hint = "Fix only the critical findings."
    draft, _ = workflow_benchmark(
        store,
        graded_benchmark,
        tmp_path,
        workflow=(
            PromptStep(step_id="review", prompt="Review the greeting.", answer_hint=hint),
            InspectStep(step_id="check", criteria=("greeting",)),
        ),
    )
    draft = claude_draft(
        store,
        draft.model_copy(
            update={
                "fixture": (
                    *draft.fixture,
                    FixtureTurn(
                        prompt="Review the greeting.", assistant="Fixed the critical findings."
                    ),
                )
            }
        ),
        *draft.simulator.script,
        ScriptStep(assistant=SCOPE.question, decision=dialog_reply("Critical only")),
        ScriptStep(
            assistant="Fixed the critical findings.",
            decision=SimulatorDecision(action="stop", reason="Reviewed."),
        ),
    )
    ui = ClaudeQuestionUI((SCOPE,))
    terminals = dialog_fixture(monkeypatch, {"Review the greeting.": ui})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert workflow_events(capture) == [
        ("prompt", "sent"),
        ("dialog", "answered"),
        ("inspect", "passed"),
    ]
    asked = capture.controller_calls[2]
    answered = capture.workflow.events[1]
    assert (answered.step_id, answered.turn, answered.call_ids) == ("review", 3, (asked.call_id,))
    assert (terminals[0].keys, ui.answers) == (["Down", "Enter"], {0: "Critical only"})
    assert capture.accepted_turns == 3
    assert [item.text for item in capture.dialogue[-2:]] == [
        "Review the greeting.",
        "Fixed the critical findings.",
    ]
    blobs = store.read_blobs(result.pending_assessment[0])
    request = json.loads(blobs[f"evidence/controller/{asked.call_id}/000001-request.json"])
    assert request["instruction"] == DIALOG_INSTRUCTION
    assert request["answer_hint"] == hint
    assert request["dialog"] == {
        "question": "Which findings should I fix?",
        "options": ["Everything", "Critical only"],
        "multi_select": False,
        "allows_text": True,
    }
    assert request["dialogue"][-1] == {"role": "user", "text": "Review the greeting."}


@pytest.mark.integration
def test_dialogs_are_answered_and_counted_without_a_workflow(store, benchmark, monkeypatch):
    from dryheave.controller_models import ScriptStep
    from dryheave.runner_models import WorkflowEvent, WorkflowProgress

    prompt = benchmark.fixture[0].prompt.strip()
    draft = claude_draft(
        store,
        benchmark,
        ScriptStep(assistant=AUTH.question, decision=dialog_reply(text="Use mutual TLS")),
        *benchmark.simulator.script,
    )
    ui = ClaudeQuestionUI((AUTH,))
    terminals = dialog_fixture(monkeypatch, {prompt: ui})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.accepted_turns) == ("simulator_stop", 2)
    asked = capture.controller_calls[0]
    assert capture.workflow == WorkflowProgress(
        steps=(),
        position=0,
        events=(
            WorkflowEvent(
                step_id="dialog",
                kind="dialog",
                outcome="answered",
                turn=1,
                call_ids=(asked.call_id,),
            ),
        ),
    )
    assert (terminals[0].keys, terminals[0].typed) == (
        ["Down", "Down", "Enter"],
        ["Use mutual TLS"],
    )
    assert ui.answers == {0: "Use mutual TLS"}
    request = json.loads(
        store.read_blob(
            result.pending_assessment[0], f"evidence/controller/{asked.call_id}/000001-request.json"
        )
    )
    assert "answer_hint" not in request
    assert request["dialogue"] == [{"role": "user", "text": prompt}]


@pytest.mark.integration
def test_an_invalid_dialog_answer_is_retried_once_then_needs_input(store, benchmark, monkeypatch):
    from dryheave.controller_models import ScriptStep

    draft = claude_draft(
        store,
        benchmark,
        ScriptStep(assistant=AUTH.question, decision=dialog_reply("Passwords")),
        ScriptStep(assistant=AUTH.question, decision=dialog_reply("OAuth", "API key")),
    )
    ui = ClaudeQuestionUI((AUTH,))
    terminals = dialog_fixture(monkeypatch, {benchmark.fixture[0].prompt.strip(): ui})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "needs_input"
    assert [(call.status, call.error) for call in capture.controller_calls] == [
        ("failed", "invalid_dialog_answer"),
        ("failed", "invalid_dialog_answer"),
    ]
    assert (terminals[0].keys, ui.answers, capture.workflow) == ([], {}, None)


class DialogBeforeAcceptanceTerminal(FixtureTerminal):
    def __init__(self, turns, source, agent, *, dialog_text, trigger_paste):
        super().__init__(turns, source, agent)
        self.dialog_text, self.trigger_paste = dialog_text, trigger_paste
        self.showing_dialog = False

    def paste(self, text):
        super().paste(text)
        if text == self.trigger_paste:
            self.showing_dialog = True

    def state(self):
        if self.showing_dialog:
            return Screen(text=self.dialog_text, exited=None)
        return super().state()


@pytest.mark.integration
def test_a_dialog_shown_before_enter_is_accepted_is_not_answered(store, benchmark, monkeypatch):
    draft = claude_draft(store, benchmark, *benchmark.simulator.script)
    ui = ClaudeQuestionUI((AUTH,))
    prompt = benchmark.fixture[0].prompt.strip()
    terminals = []

    def build(turns, source, agent):
        terminal = DialogBeforeAcceptanceTerminal(
            turns, source, agent, dialog_text=ui.render(), trigger_paste=prompt
        )
        terminals.append(terminal)
        return terminal

    monkeypatch.setattr("dryheave.runner_attempt.FixtureTerminal", build)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.last_observation.state) == (
        "needs_input",
        "structured_input",
    )
    assert (capture.controller_calls, terminals[0].keys, terminals[0].enters) == ((), [], 0)


@pytest.mark.integration
def test_a_claude_permission_dialog_still_stops_without_a_simulator_call(
    store, benchmark, monkeypatch
):
    draft = claude_draft(store, benchmark, *benchmark.simulator.script)
    terminals = dialog_fixture(monkeypatch, {benchmark.fixture[0].prompt.strip(): PERMISSION})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.last_observation.state) == ("needs_input", "approval")
    assert (capture.controller_calls, terminals[0].keys) == ((), [])


@pytest.mark.integration
def test_a_startup_trust_prompt_never_reaches_the_simulator(store, benchmark, monkeypatch):
    draft = claude_draft(store, benchmark, *benchmark.simulator.script)
    terminals = dialog_fixture(monkeypatch, {}, startup=TRUST)
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.last_observation.state) == ("needs_input", "approval")
    assert (capture.controller_calls, terminals[0].keys, terminals[0].pastes) == ((), [], [])


def cleanup_report(*errors, stopped=True, survivors=()):
    return CleanupReport(
        terminal_closed=True,
        known_writers_stopped=stopped,
        logs_drained=True,
        owned=survivors,
        survivors=survivors,
        errors=errors,
        coverage="partial-native",
    )


def test_attempt_cleanup_drops_a_recovered_call_limit_but_keeps_real_cleanup_errors():
    combined = _combine_cleanup(
        cleanup_report(),
        cleanup_report("controller_file_limit"),
        cleanup_report("controller_special_file"),
        cleanup_report("runtime_binding_cleanup_failed"),
    )
    assert combined.errors == ("controller_special_file", "runtime_binding_cleanup_failed")
    assert combined.known_writers_stopped
    unstopped = _combine_cleanup(
        cleanup_report(), cleanup_report("controller_file_limit", stopped=False)
    )
    assert (unstopped.errors, unstopped.known_writers_stopped) == ((), False)


def test_a_call_limit_never_excuses_surviving_writers():
    survivor = ProcessIdentity(pid=4242, created=1.0, parent_pid=1)
    combined = _combine_cleanup(
        cleanup_report(),
        cleanup_report("controller_file_limit", stopped=False, survivors=(survivor,)),
    )
    assert (combined.known_writers_stopped, combined.survivors) == (False, (survivor,))


def screen_reply(key=None, text=None, target="Fix all Should Fix"):
    from dryheave.controller_models import SimulatorDecision
    from dryheave.drivers.models import ScreenAction

    return SimulatorDecision(
        action="reply",
        reason="Acting as this user on the screen.",
        screen_kind="question",
        screen_action=ScreenAction(key=key, text=text),
        screen_target=target,
    )


def screen_step(decision, seen="Apply fixes for the Should Fix findings?"):
    from dryheave.controller_models import ScriptStep

    return ScriptStep(match="contains", assistant=seen, decision=decision)


def screen_judgement(kind):
    from dryheave.controller_models import SimulatorDecision

    return SimulatorDecision(action="stop", reason="Judging the screen.", screen_kind=kind)


def scripted(draft, *script):
    from dryheave.controller_models import RoleBudget

    return draft.model_copy(
        update={
            "simulator": draft.simulator.model_copy(
                update={"script": script, "budget": RoleBudget(max_calls=len(script))}
            )
        }
    )


def run_screen(store, draft, monkeypatch, ui):
    terminals = dialog_fixture(monkeypatch, {draft.fixture[0].prompt.strip(): ui})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    return capture, terminals[0], store.read_blobs(result.pending_assessment[0])


def screen_evidence(blobs, name):
    return [
        json.loads(content)
        for path, content in sorted(blobs.items())
        if path.startswith("evidence/driver/") and path.endswith(f"-{name}.json")
    ]


def answered_dialogs(capture):
    from dryheave.runner_models import WorkflowEvent, WorkflowProgress

    return WorkflowProgress(
        steps=(),
        position=0,
        events=(
            WorkflowEvent(
                step_id="dialog",
                kind="dialog",
                outcome="answered",
                turn=1,
                call_ids=tuple(
                    call.call_id
                    for call in capture.controller_calls
                    if call.decision is not None and call.decision.screen_kind is not None
                ),
            ),
        ),
    )


@pytest.mark.integration
def test_the_claude_fix_scope_screen_is_answered_through_the_screen_loop(
    store, benchmark, monkeypatch
):
    from dryheave.controller_models import SCREEN_INSTRUCTION

    prompt = benchmark.fixture[0].prompt.strip()
    draft = claude_draft(
        store, benchmark, screen_step(screen_reply(key="Enter")), *benchmark.simulator.script
    )
    ui = ScreenUI(FIX_SCOPE)
    capture, terminal, blobs = run_screen(store, draft, monkeypatch, ui)
    assert (capture.stop_reason, capture.accepted_turns) == ("simulator_stop", 2)
    assert (terminal.keys, ui.answer) == (["Enter"], "Fix all Should Fix")
    assert capture.workflow == answered_dialogs(capture)
    asked = capture.controller_calls[0]
    request = json.loads(blobs[f"evidence/controller/{asked.call_id}/000001-request.json"])
    assert request["instruction"] == SCREEN_INSTRUCTION
    assert request["screen"] == FIX_SCOPE.strip()
    assert request["dialogue"] == [{"role": "user", "text": prompt}]
    assert {"dialog", "answer_hint"}.isdisjoint(request)
    intents = screen_evidence(blobs, "screen-intent")
    assert [(item["action"], item["screen"]) for item in intents] == [
        ({"key": "Enter"}, FIX_SCOPE.rstrip("\n"))
    ]


@pytest.mark.integration
def test_a_screen_is_answered_one_key_at_a_time_guided_by_the_prompt_step_hint(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.cases import InspectStep, PromptStep
    from dryheave.controller_models import SimulatorDecision
    from dryheave.experiment_models import FixtureTurn

    hint = "Use the nightly channel."
    draft, _ = workflow_benchmark(
        store,
        graded_benchmark,
        tmp_path,
        workflow=(
            PromptStep(step_id="release", prompt="Pick a release channel.", answer_hint=hint),
            InspectStep(step_id="check", criteria=("greeting",)),
        ),
    )
    seen = "Which release channel?"
    draft = claude_draft(
        store,
        draft.model_copy(
            update={
                "fixture": (
                    *draft.fixture,
                    FixtureTurn(prompt="Pick a release channel.", assistant="Switched to nightly."),
                )
            }
        ),
        *draft.simulator.script,
        *(
            screen_step(screen_reply(key=key, target="Nightly"), seen)
            for key in ("Down", "Down", "Enter")
        ),
        screen_step(SimulatorDecision(action="stop", reason="Done."), "Switched to nightly."),
    )
    ui = ScreenUI(CHANNEL)
    terminals = dialog_fixture(monkeypatch, {"Pick a release channel.": ui})
    result = run_experiment(
        store,
        create_experiment(store, draft),
        options=workflow_options(),
        host=HOST,
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "workflow_complete"
    assert (terminals[0].keys, ui.answer) == (["Down", "Down", "Enter"], "Nightly")
    assert workflow_events(capture) == [
        ("prompt", "sent"),
        ("dialog", "answered"),
        ("inspect", "passed"),
    ]
    answered = capture.workflow.events[1]
    screen_calls = capture.controller_calls[2:5]
    assert (answered.step_id, answered.call_ids) == (
        "release",
        tuple(call.call_id for call in screen_calls),
    )
    blobs = store.read_blobs(result.pending_assessment[0])
    requests = [
        json.loads(blobs[f"evidence/controller/{call.call_id}/000001-request.json"])
        for call in screen_calls
    ]
    assert [request["answer_hint"] for request in requests] == [hint] * 3
    assert "\u276f 3. Nightly" in requests[2]["screen"]


@pytest.mark.integration
def test_free_text_is_typed_into_a_screen_then_submitted(store, benchmark, monkeypatch):
    text = "Only fix the prefix bug"
    draft = claude_draft(
        store,
        benchmark,
        screen_step(screen_reply(key="3", target=text)),
        screen_step(screen_reply(text=text, target=text)),
        screen_step(screen_reply(key="Enter", target=text)),
        *benchmark.simulator.script,
    )
    ui = ScreenUI(FIX_SCOPE)
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ui)
    assert capture.stop_reason == "simulator_stop"
    assert (terminal.keys, terminal.typed, ui.answer) == (["3", "Enter"], [text], text)
    assert capture.workflow == answered_dialogs(capture)


class BusyUI(ScreenUI):
    def __init__(self, template, reads):
        super().__init__(template)
        self.reads = reads

    def screen(self):
        self.reads -= 1
        if self.reads < 0:
            self.answer = "finished without input"
        return super().screen()


@pytest.mark.integration
def test_a_busy_screen_is_waited_out_without_a_key(store, benchmark, monkeypatch):
    draft = claude_draft(
        store, benchmark, screen_step(screen_judgement("busy")), *benchmark.simulator.script
    )
    capture, terminal, blobs = run_screen(store, draft, monkeypatch, BusyUI(FIX_SCOPE, 8))
    assert (capture.stop_reason, capture.accepted_turns) == ("simulator_stop", 2)
    assert (terminal.keys, terminal.typed, capture.workflow) == ([], [], None)
    assert capture.controller_calls[0].decision.screen_kind == "busy"
    assert screen_evidence(blobs, "screen-intent") == []


@pytest.mark.integration
def test_a_screen_judged_an_approval_stops_without_a_key(store, benchmark, monkeypatch):
    draft = claude_draft(store, benchmark, screen_step(screen_judgement("approval")))
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ScreenUI(FIX_SCOPE))
    assert (capture.stop_reason, capture.last_observation.state) == ("needs_input", "approval")
    assert capture.last_observation.reason == (
        "The simulator judged the screen a permission prompt; it was not answered."
    )
    assert (terminal.keys, len(capture.controller_calls), capture.workflow) == ([], 1, None)


@pytest.mark.integration
def test_an_unanchored_permission_prompt_never_reaches_the_simulator(store, benchmark, monkeypatch):
    draft = claude_draft(store, benchmark, *benchmark.simulator.script)
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ScreenUI(EDIT_PERMISSION))
    assert (capture.stop_reason, capture.last_observation.text) == (
        "needs_input",
        EDIT_PERMISSION.rstrip("\n"),
    )
    assert (capture.controller_calls, terminal.keys, terminal.pastes) == (
        (),
        [],
        [benchmark.fixture[0].prompt.strip()],
    )


@pytest.mark.integration
def test_a_screen_still_open_after_the_step_cap_needs_input(store, benchmark, monkeypatch):
    from dryheave.runner_attempt import SCREEN_ACTIONS

    keys = ["Down", "Up"] * SCREEN_ACTIONS
    draft = claude_draft(store, benchmark, *(screen_step(screen_reply(key=key)) for key in keys))
    ui = ScreenUI(FIX_SCOPE)
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ui)
    assert (capture.stop_reason, capture.last_observation.reason) == (
        "needs_input",
        "The screen stayed open after its bounded answer steps.",
    )
    assert (terminal.keys, ui.open, capture.workflow) == (keys[:SCREEN_ACTIONS], True, None)
    assert len(capture.controller_calls) == SCREEN_ACTIONS


@pytest.mark.integration
def test_an_invalid_screen_answer_is_retried_once_then_needs_input(store, benchmark, monkeypatch):
    from dryheave.controller_models import SimulatorDecision

    draft = claude_draft(
        store,
        benchmark,
        screen_step(SimulatorDecision(action="stop", reason="No judgement.")),
        screen_step(screen_reply(key="Enter").model_copy(update={"screen_kind": "busy"})),
    )
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ScreenUI(FIX_SCOPE))
    assert capture.stop_reason == "needs_input"
    assert [(call.status, call.error) for call in capture.controller_calls] == [
        ("failed", "invalid_screen_answer"),
        ("failed", "invalid_screen_answer"),
    ]
    assert terminal.keys == []


class CodexFreeformUI:
    def __init__(self, template):
        self.template, self.typed, self.answer = template, "", None

    @property
    def open(self):
        return self.answer is None

    def press(self, key):
        if key == "Enter" and self.typed:
            self.answer = self.typed

    def type(self, text):
        self.typed += text

    def screen(self):
        placeholder = "Type your answer (optional)"
        return Screen(
            text=self.template.replace(placeholder, self.typed or placeholder), exited=None
        )


@pytest.mark.integration
def test_a_codex_question_the_parser_cannot_answer_goes_through_the_screen_loop(
    store, benchmark, monkeypatch
):

    template = json.loads(Path("tests/fixtures/codex-dialogs.json").read_text())["freeform"]
    text = "Use a comma after Hello."
    draft = scripted(
        benchmark,
        screen_step(screen_reply(text=text, target=text), "Share details."),
        screen_step(screen_reply(key="Enter", target=text), "Share details."),
        *benchmark.simulator.script,
    )
    ui = CodexFreeformUI(template)
    capture, terminal, blobs = run_screen(store, draft, monkeypatch, ui)
    assert (capture.stop_reason, capture.accepted_turns) == ("simulator_stop", 2)
    assert (terminal.typed, terminal.keys, ui.answer) == ([text], ["Enter"], text)
    assert capture.workflow == answered_dialogs(capture)
    results = screen_evidence(blobs, "screen-result")
    assert [item["changed"] for item in results] == [True, True]
    assert "\u203a Use a comma after Hello." in results[0]["screen"]


class LostKeyUI(ScreenUI):
    def press(self, key):
        from dryheave.drivers.base import DeliveryUncertain

        raise DeliveryUncertain("Fixture screen key acknowledgement lost.")


@pytest.mark.integration
def test_a_lost_screen_key_acknowledgement_needs_input_without_an_answer(
    store, benchmark, monkeypatch
):
    from dryheave.drivers.session import SCREEN_KEY_UNCERTAIN

    draft = claude_draft(store, benchmark, screen_step(screen_reply(key="Enter")))
    capture, terminal, _ = run_screen(store, draft, monkeypatch, LostKeyUI(FIX_SCOPE))
    assert (capture.stop_reason, capture.last_observation.reason) == (
        "needs_input",
        SCREEN_KEY_UNCERTAIN,
    )
    assert (terminal.keys, capture.workflow) == (["Enter"], None)


@pytest.mark.integration
def test_a_screen_action_aimed_at_a_mode_option_needs_input_without_a_key(
    store, benchmark, monkeypatch
):
    from dryheave.drivers.session import SCREEN_ACTION_REFUSED

    draft = claude_draft(
        store, benchmark, screen_step(screen_reply(key="Enter", target="Yes, and use auto mode"))
    )
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ScreenUI(FIX_SCOPE))
    assert (capture.stop_reason, capture.last_observation.state) == ("needs_input", "approval")
    assert capture.last_observation.reason == SCREEN_ACTION_REFUSED
    assert (terminal.keys, capture.workflow) == ([], None)


@pytest.mark.integration
def test_a_screen_judged_busy_that_never_changes_needs_input_at_the_wait_bound(
    store, benchmark, monkeypatch
):
    from dryheave.drivers.session import SCREEN_WAIT_EXPIRED

    monkeypatch.setattr("dryheave.runner_attempt.SCREEN_WAIT_SECONDS", 0.5)
    draft = claude_draft(store, benchmark, screen_step(screen_judgement("busy")))
    started = time.monotonic()
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ScreenUI(FIX_SCOPE))
    assert (capture.stop_reason, capture.last_observation.reason) == (
        "needs_input",
        SCREEN_WAIT_EXPIRED,
    )
    assert (terminal.keys, len(capture.controller_calls)) == ([], 1)
    assert time.monotonic() - started < 30


class ExitedUI(ScreenUI):
    def screen(self):
        return super().screen().model_copy(update={"exited": 1})


@pytest.mark.integration
def test_an_exited_subject_stops_as_exited_before_any_screen_answer(store, benchmark, monkeypatch):
    draft = claude_draft(store, benchmark, screen_step(screen_reply(key="Enter")))
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ExitedUI(FIX_SCOPE))
    assert (capture.stop_reason, capture.last_observation.state) == ("exited", "exited")
    assert (capture.controller_calls, terminal.keys) == ((), [])


class QuestionsUI:
    def __init__(self, *templates):
        self.questions = [ScreenUI(template) for template in templates]
        self.answers = []

    @property
    def open(self):
        return bool(self.questions)

    def press(self, key):
        self.questions[0].press(key)
        if not self.questions[0].open:
            self.answers.append(self.questions.pop(0).answer)

    def type(self, text):
        self.questions[0].type(text)

    def screen(self):
        return self.questions[0].screen()


def screen_events(capture):
    return [
        (event.kind, event.outcome, len(event.call_ids))
        for event in (capture.workflow.events if capture.workflow is not None else ())
    ]


@pytest.mark.integration
def test_each_screen_in_a_run_of_questions_has_its_own_cap_and_answered_event(
    store, benchmark, monkeypatch
):
    from dryheave.runner_attempt import SCREEN_ACTIONS

    fix = ["Down", "Up", "Down", "Up", "Down", "Up", "Enter"]
    channel = ["Down", "Up", "Down", "Up", "Down", "Down", "Enter"]
    assert len(fix) < SCREEN_ACTIONS < len(fix) + len(channel)
    draft = claude_draft(
        store,
        benchmark,
        *(screen_step(screen_reply(key=key)) for key in fix),
        *(
            screen_step(screen_reply(key=key, target="Nightly"), "Which release channel?")
            for key in channel
        ),
        *benchmark.simulator.script,
    )
    ui = QuestionsUI(FIX_SCOPE, CHANNEL)
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ui)
    assert (capture.stop_reason, ui.answers) == (
        "simulator_stop",
        ["Fix all Should Fix", "Nightly"],
    )
    assert terminal.keys == fix + channel
    assert screen_events(capture) == [
        ("dialog", "answered", len(fix)),
        ("dialog", "answered", len(channel)),
    ]


@pytest.mark.integration
def test_an_answered_screen_is_recorded_when_the_next_one_is_judged_an_approval(
    store, benchmark, monkeypatch
):
    draft = claude_draft(
        store,
        benchmark,
        screen_step(screen_reply(key="Enter")),
        screen_step(screen_judgement("approval"), "Which release channel?"),
    )
    ui = QuestionsUI(FIX_SCOPE, CHANNEL)
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ui)
    assert (capture.stop_reason, capture.last_observation.state) == ("needs_input", "approval")
    assert (terminal.keys, ui.answers) == (["Enter"], ["Fix all Should Fix"])
    assert screen_events(capture) == [("dialog", "answered", 1)]
    assert capture.workflow.events[0].call_ids == (capture.controller_calls[0].call_id,)


@pytest.mark.integration
def test_typed_text_starting_with_a_digit_is_retried_once_then_needs_input(
    store, benchmark, monkeypatch
):
    draft = claude_draft(
        store,
        benchmark,
        screen_step(screen_reply(text="2 files only")),
        screen_step(screen_reply(text="2")),
    )
    capture, terminal, _ = run_screen(store, draft, monkeypatch, ScreenUI(FIX_SCOPE))
    assert capture.stop_reason == "needs_input"
    assert [(call.status, call.error) for call in capture.controller_calls] == [
        ("failed", "invalid_screen_answer"),
        ("failed", "invalid_screen_answer"),
    ]
    assert (terminal.keys, terminal.typed) == ([], [])


def test_mixed_live_run_snapshots_each_agent_into_its_own_directory(store, benchmark, tmp_path):
    from dryheave.experiment_models import VariantSpec
    from dryheave.live_config import journaled_manifests
    from dryheave.models import AgentKind, EnvironmentReference
    from dryheave.profile_models import CaptureSpec, NativeRecipe
    from dryheave.profiles import capture_profile
    from dryheave.runner import _prepare_live_config

    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "CLAUDE.md").write_text("Be terse.")
    codex_home = live_codex_home(tmp_path)
    profiles = {
        "claude": NativeRecipe(
            agent=AgentKind.CLAUDE,
            executable="claude",
            config_source="live",
            environment=(EnvironmentReference(name="CLAUDE_CODE_OAUTH_TOKEN"),),
        ),
        "codex": NativeRecipe(
            agent=AgentKind.CODEX,
            executable="codex",
            model="gpt-5.6-terra",
            effort="high",
            config_source="live",
        ),
    }
    variants = tuple(
        VariantSpec(name=name, profile=capture_profile(store, CaptureSpec(recipe=recipe)))
        for name, recipe in profiles.items()
    )
    draft = benchmark.model_copy(update={"variants": variants, "repetitions": 1})
    experiment = load_experiment(store, create_experiment(store, draft))
    run_id = RunStore(store.root).create(store.resolve(variants[0].profile))
    environment = {"CLAUDE_CONFIG_DIR": str(claude_home), "CODEX_HOME": str(codex_home)}
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        _prepare_live_config(store, execution, experiment, HostContext(environment, tmp_path))
        _prepare_live_config(store, execution, experiment, HostContext(environment, tmp_path))
        events = [event for event in journal.events if event.event == "live-config"]
        assert [event.data.get("agent") for event in events] == [None, "codex"]
        assert sorted(journaled_manifests(events)) == [AgentKind.CLAUDE, AgentKind.CODEX]
        assert events[1].data["source"] == str(codex_home)
        assert (journal.path / "live-config/CLAUDE.md").read_text() == "Be terse."
        assert (journal.path / "live-config-codex/AGENTS.md").read_text() == "Be brief."
