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


def test_real_cli_resume_and_status_ignore_changed_workspace_runtime(store, benchmark, tmp_path):
    import json
    import subprocess
    import sys

    from dryheave.workspaces import CONFIG_FILE

    identifier = create_experiment(store, benchmark)
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
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


@pytest.mark.parametrize("mode", ["native", "offline-fixture"])
def test_new_cli_run_uses_workspace_runtime_only_for_native(
    store, benchmark, tmp_path, monkeypatch, capsys, mode
):
    import json
    from pathlib import Path

    from dryheave.workspaces import CONFIG_FILE

    identifier = create_experiment(store, benchmark)
    result = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
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
    monkeypatch.undo()
    resumed = run_experiment(store, resume=result.run_id, retry=(result.attempts[0].trial_id,))
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
