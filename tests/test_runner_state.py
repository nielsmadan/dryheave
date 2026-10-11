import pytest

from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import InputError
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


def _owned_run(store, benchmark):
    identifier = create_experiment(store, benchmark)
    experiment = load_experiment(store, identifier)
    run_id = RunStore(store.root).create(identifier)
    return run_id, experiment.trials[0].trial_id


def test_many_owned_identities_are_batched_into_a_bounded_number_of_events(store, benchmark):
    run_id, trial_id = _owned_run(store, benchmark)
    identities = tuple(
        ProcessIdentity(pid=10000 + index, created=float(index), parent_pid=1)
        for index in range(2500)
    )
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
        state = execution.reserve(trial_id)
        state = execution.own(state, identities[:2000])
        state = execution.own(state, (*identities[:2000], *identities[2000:], identities[2400]))
        execution.save(state.model_copy(update={"stage": TrialStage.PREPARING}))
        batches = [event for event in journal.events if event.event == "owned-processes"]
        assert [len(event.data["identities"]) for event in batches] == [1024, 976, 500]
        assert (journal.path / "events.jsonl").stat().st_size < 250000
        assert len([event for event in journal.events if event.event == "attempt-state"]) == 2
        sequence = journal.sequence
        assert execution.own(state, identities[:10]) == state
        assert journal.sequence == sequence
    with RunStore(store.root).open(run_id) as journal:
        restored = ExecutionJournal(journal)
        assert restored.attempts[state.attempt_id].owned == identities
        assert restored.attempts[state.attempt_id].stage == TrialStage.PREPARING
        assert state.attempt_id in restored.unreconciled


def test_legacy_per_process_ownership_events_still_replay(store, benchmark):
    run_id, trial_id = _owned_run(store, benchmark)
    legacy = tuple(ProcessIdentity(pid=20000 + index, created=float(index)) for index in range(3))
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        state = execution.reserve(trial_id)
        for identity in legacy[:2]:
            journal.append(
                "owned-process",
                identity.model_dump(mode="json"),
                trial_id=trial_id,
                attempt_id=state.attempt_id,
            )
        restored = ExecutionJournal(journal)
        state = restored.own(restored.attempts[state.attempt_id], legacy)
        assert state.owned == legacy
        assert [len(event.data["identities"]) for event in journal.events[-1:]] == [1]
    with RunStore(store.root).open(run_id) as journal:
        replayed = ExecutionJournal(journal)
        assert replayed.attempts[state.attempt_id].owned == legacy
        assert state.attempt_id in replayed.unreconciled


def test_ownership_batch_with_unknown_fields_is_rejected_on_replay(store, benchmark):
    run_id, trial_id = _owned_run(store, benchmark)
    with RunStore(store.root).open(run_id) as journal:
        state = ExecutionJournal(journal).reserve(trial_id)
        journal.append(
            "owned-processes",
            {"identities": [{"pid": 1, "created": 1.0}], "extra": True},
            trial_id=trial_id,
            attempt_id=state.attempt_id,
        )
        with pytest.raises(InputError):
            ExecutionJournal(journal)


def test_regrade_scope_keeps_its_assessment_events_apart_from_the_normal_index(store):
    from dryheave.assessment_inputs import saved_judge_calls
    from dryheave.controller_models import RoleCall
    from dryheave.drivers.models import CleanupReport

    run_id = RunStore(store.root).create("a" * 64)
    with RunStore(store.root).open(run_id) as journal:
        normal = ExecutionJournal(journal)
        state = normal.reserve("trial")
        normal.evidence(
            state, "judge-intent", RoleCall(call_id="j-old", role="judge", status="intent")
        )
        regrade = ExecutionJournal(journal, regrade_id="r-1")
        regrade.evidence(
            state, "judge-intent", RoleCall(call_id="j-new", role="judge", status="intent")
        )
        regrade.record(state, "criterion-intent", {"criterion_id": "quality"})
        regrade.evidence(
            state,
            "assessment-recovery-cleanup",
            CleanupReport(terminal_closed=True, known_writers_stopped=True),
        )
        assert [call.call_id for call in saved_judge_calls(normal, state)] == ["j-old"]
        assert [call.call_id for call in saved_judge_calls(regrade, state)] == ["j-new"]
        assert normal.events_for(state.attempt_id, "criterion-intent") == ()
        assert regrade.events_for(state.attempt_id, "criterion-intent")[0].data == {
            "criterion_id": "quality"
        }
        assert regrade.assessment_root(state) == (
            normal.attempt_root(state) / "regrades" / "r-1" / "assessment"
        )
        raw = [event.data for event in journal.events if event.event == "judge-intent"]
        assert [item.get("regrade_id") for item in raw] == [None, "r-1"]
    with RunStore(store.root).open(run_id) as journal:
        reopened = ExecutionJournal(journal)
        assert reopened.cleanup_reports[state.attempt_id].known_writers_stopped
        assert [call.call_id for call in saved_judge_calls(reopened, state)] == ["j-old"]
