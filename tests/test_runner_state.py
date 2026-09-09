from dryheave.drivers.models import ProcessIdentity
from dryheave.experiments import create_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.models import TrialStage
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


def test_assessment_index_observes_new_durable_events_and_keeps_attempts_separate(store):
    from dryheave.assessment_inputs import saved_judge_calls
    from dryheave.controller_models import RoleCall

    run_id = RunStore(store.root).create("a" * 64)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        first, second = execution.reserve("first"), execution.reserve("second")
        call = RoleCall(call_id="shared-id", role="judge", status="intent")
        execution.evidence(first, "judge-intent", call)
        assert saved_judge_calls(execution, first)[0].status == "interrupted"
        execution.evidence(second, "judge-intent", call)
        execution.evidence(
            first, "judge-result", call.model_copy(update={"status": "completed", "cost": 0.5})
        )
        assert saved_judge_calls(execution, first)[0].cost == 0.5
        assert saved_judge_calls(execution, second)[0].status == "interrupted"
        journal.append("criterion-intent", {"criterion_id": "check"}, attempt_id=first.attempt_id)
        events = execution.events_for(first.attempt_id, "criterion-intent")
        assert events[0].data == {"criterion_id": "check"}
        events[0].data["criterion_id"] = "edited"
        assert execution.events_for(first.attempt_id, "criterion-intent")[0].data == {
            "criterion_id": "check"
        }


def test_many_owned_identities_are_linear_journal_deltas(store, benchmark):
    identifier = create_experiment(store, benchmark)
    experiment = load_experiment(store, identifier)
    run_id = RunStore(store.root).create(identifier)
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
        state = execution.reserve(experiment.trials[0].trial_id)
        for index in range(1000):
            state = execution.own(state, ProcessIdentity(pid=10000 + index, created=float(index)))
        execution.save(state.model_copy(update={"stage": TrialStage.PREPARING}))
        assert (journal.path / "events.jsonl").stat().st_size < 600000
        assert len([event for event in journal.events if event.event == "owned-process"]) == 1000
        assert len([event for event in journal.events if event.event == "attempt-state"]) == 2
    with RunStore(store.root).open(run_id) as journal:
        restored = ExecutionJournal(journal).attempts[state.attempt_id]
        assert restored.owned == state.owned
        assert restored.stage == TrialStage.PREPARING
