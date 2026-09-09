import threading

import pytest

from dryheave.cli import main
from dryheave.errors import ConflictError, InputError, NotFoundError
from dryheave.experiments import create_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.models import ObjectKind, TrialStage
from dryheave.runner import load_capture, run_experiment, run_status
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


def test_fixture_clarification_capture_and_resume_without_paid_launch(store, benchmark):
    identifier = create_experiment(store, benchmark)
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
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
    resumed = run_experiment(store, resume=result.run_id)
    assert resumed == result
    retry = run_experiment(store, resume=result.run_id, retry=(result.attempts[0].trial_id,))
    assert len(retry.attempts) == 2
    assert len(retry.pending_assessment) == 2
    assert len({state.workspace for state in retry.attempts}) == 2


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
    result = run_experiment(store, resume=run_id)
    assert len(result.attempts) == 1
    assert result.attempts[0].launch_attempted
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.accepted_turns) == ("interrupted", 0)
    assert not capture.workspace.complete


def test_conflicting_resume_and_tampered_capture_map_rejected(store, benchmark):
    identifier = create_experiment(store, benchmark)
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    other = create_experiment(store, benchmark.model_copy(update={"seed": 3}))
    with pytest.raises(ConflictError, match="differs"):
        run_experiment(store, other, resume=result.run_id)
    captured = load_capture(store, result.pending_assessment[0])
    bad = store.put(ObjectKind.CAPTURE, captured, references=(identifier,))
    with pytest.raises((InputError, NotFoundError)):
        load_capture(store, bad)


def test_cancellation_preserves_unstarted_trials_and_no_launch(store, benchmark):
    identifier = create_experiment(store, benchmark)
    cancelled = threading.Event()
    cancelled.set()
    result = run_experiment(
        store, identifier, options=RunOptions(mode="offline-fixture"), cancelled=cancelled
    )
    assert result.attempts == ()
    assert len(result.unstarted_trials) == 1
    assert run_status(store, result.run_id) == result


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
        run_experiment(store, identifier)
    with pytest.raises(InputError, match="70-byte"):
        run_experiment(
            store,
            identifier,
            options=RunOptions(runtime_root="/" + "x" * 70, transport="/fixture/tui-test"),
        )


def test_turn_and_controller_budgets_stop_further_submission(store, benchmark):
    from dryheave.controller_models import RoleBudget

    recipe = benchmark.simulator.model_copy(update={"budget": RoleBudget(max_calls=0)})
    identifier = create_experiment(store, benchmark.model_copy(update={"simulator": recipe}))
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    capture = load_capture(store, result.pending_assessment[0])
    assert (capture.stop_reason, capture.accepted_turns, capture.controller_calls) == (
        "controller_budget",
        1,
        (),
    )


def test_stop_writers_then_capture_reads_their_final_bytes(store, benchmark, monkeypatch):
    from pathlib import Path

    from dryheave.fixture_subject import FixtureTerminal

    original = FixtureTerminal.close

    def close(terminal):
        if terminal.plan is not None:
            (Path(terminal.plan.cwd) / "last-write.txt").write_text("written during owned shutdown")
        return original(terminal)

    monkeypatch.setattr(FixtureTerminal, "close", close)
    result = run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
    )
    assert (
        store.read_blob(result.pending_assessment[0], "workspace/last-write.txt")
        == b"written during owned shutdown"
    )


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
    result = run_experiment(store, resume=run_id)
    call = load_capture(store, result.pending_assessment[0]).controller_calls[0]
    assert (call.call_id, call.status, call.usage, call.cost, call.elapsed_seconds) == (
        "possibly-paid",
        "interrupted",
        None,
        None,
        None,
    )


def test_permission_dialog_stops_without_a_simulator_call(store, benchmark):
    turn = benchmark.fixture[0].model_copy(update={"state": "approval"})
    result = run_experiment(
        store,
        create_experiment(store, benchmark.model_copy(update={"fixture": (turn,)})),
        options=RunOptions(mode="offline-fixture"),
    )
    capture = load_capture(store, result.pending_assessment[0])
    assert capture.stop_reason == "needs_input"
    assert capture.controller_calls == ()


def test_native_and_run_locks_prevent_duplicate_work(store, benchmark):
    from dryheave.errors import LockBusyError

    identifier = create_experiment(store, benchmark)
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    with RunStore(store.root).open(result.run_id), pytest.raises(LockBusyError):
        run_experiment(store, resume=result.run_id)
    with store.native_lock(), pytest.raises(LockBusyError):
        run_experiment(store, resume=result.run_id)
    assert run_status(store, result.run_id).attempts == result.attempts


@pytest.mark.parametrize("interrupted", [False, True])
def test_controller_cleanup_failure_blocks_capture_and_continuation(
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
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
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
    with pytest.raises(InputError, match="reconciliation before another subject launch"):
        run_experiment(store, resume=result.run_id)


def test_load_capture_batch_traversal_does_not_grow_per_file(store, benchmark, monkeypatch):
    additions = {f"file-{index}.txt": str(index) for index in range(30)}
    turn = benchmark.fixture[-1].model_copy(
        update={"files": benchmark.fixture[-1].files | additions}
    )
    modified = benchmark.model_copy(update={"fixture": (*benchmark.fixture[:-1], turn)})
    summary = run_experiment(
        store, create_experiment(store, modified), options=RunOptions(mode="offline-fixture")
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
