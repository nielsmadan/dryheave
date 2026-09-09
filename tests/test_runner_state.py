from dryheave.drivers.models import ProcessIdentity
from dryheave.experiments import create_experiment, load_experiment
from dryheave.journals import RunStore
from dryheave.models import TrialStage
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal


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
