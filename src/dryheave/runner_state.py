from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import InputError
from dryheave.journals import RunJournal
from dryheave.models import JournalEvent, StrictModel, TrialStage
from dryheave.runner_models import AttemptState, RunOptions
from dryheave.serialization import canonical_json, parse_model

STAGE_TRANSITIONS = {
    TrialStage.RESERVED: {TrialStage.PREPARING, TrialStage.STOPPING},
    TrialStage.PREPARING: {TrialStage.LAUNCHING, TrialStage.STOPPING},
    TrialStage.LAUNCHING: {TrialStage.INTERACTING, TrialStage.STOPPING},
    TrialStage.INTERACTING: {TrialStage.STOPPING},
    TrialStage.STOPPING: {TrialStage.CAPTURED},
    TrialStage.CAPTURED: {TrialStage.AUDITED},
    TrialStage.AUDITED: {TrialStage.GRADING},
    TrialStage.GRADING: {TrialStage.FINISHED},
    TrialStage.FINISHED: set(),
}

CLEANUP_EVENTS = {
    "cleanup",
    "recovery-cleanup",
    "assessment-recovery-cleanup",
    "ownership-reconciled",
}


class ExecutionJournal:
    def __init__(self, journal: RunJournal) -> None:
        self.journal = journal
        self.options: RunOptions | None = None
        self.attempts: dict[str, AttemptState] = {}
        self.cleanup_reports: dict[str, CleanupReport] = {}
        self.unreconciled: set[str] = set()
        self._assessment_events: dict[tuple[str, str], list[JournalEvent]] = {}
        events = journal.events
        self._indexed_sequence = len(events)
        self._index_assessment_events(events)
        for event in events:
            if event.event == "run-options":
                if self.options is not None:
                    raise InputError("Run options may only be frozen once.")
                self.options = parse_model(canonical_json(event.data), RunOptions)
            elif event.event == "attempt-state":
                state = parse_model(canonical_json(event.data), AttemptState)
                if "owned" not in event.data and state.attempt_id in self.attempts:
                    state = state.model_copy(
                        update={"owned": self.attempts[state.attempt_id].owned}
                    )
                if (event.attempt_id, event.trial_id) != (state.attempt_id, state.trial_id):
                    raise InputError("Attempt journal envelope differs from its payload.")
                self._validate(state)
                self.attempts[state.attempt_id] = state
            elif event.event == "owned-process":
                owned_state = self.attempts.get(event.attempt_id or "")
                if owned_state is None or event.trial_id != owned_state.trial_id:
                    raise InputError("Process ownership event has no matching attempt.")
                identity = parse_model(canonical_json(event.data), ProcessIdentity)
                self.attempts[owned_state.attempt_id] = owned_state.model_copy(
                    update={"owned": (*owned_state.owned, identity)}
                )
                self.unreconciled.add(owned_state.attempt_id)
            elif event.event in CLEANUP_EVENTS:
                cleanup_state = self.attempts.get(event.attempt_id or "")
                if cleanup_state is None or event.trial_id != cleanup_state.trial_id:
                    raise InputError("Cleanup event has no matching attempt.")
                self.cleanup_reports[cleanup_state.attempt_id] = parse_model(
                    canonical_json(event.data), CleanupReport
                )
                self.unreconciled.discard(cleanup_state.attempt_id)

    def _index_assessment_events(self, events: tuple[JournalEvent, ...]) -> None:
        for event in events:
            if event.attempt_id and event.event in {
                "criterion-intent",
                "criterion-result",
                "judge-intent",
                "judge-result",
            }:
                self._assessment_events.setdefault((event.attempt_id, event.event), []).append(
                    event
                )

    def events_for(self, attempt_id: str, *names: str) -> tuple[JournalEvent, ...]:
        fresh = self.journal.events_since(self._indexed_sequence)
        self._index_assessment_events(fresh)
        self._indexed_sequence += len(fresh)
        events = (
            event for name in names for event in self._assessment_events.get((attempt_id, name), ())
        )
        return tuple(
            event.model_copy(deep=True) for event in sorted(events, key=lambda item: item.sequence)
        )

    def own(self, state: AttemptState, identity: ProcessIdentity) -> AttemptState:
        self.journal.append(
            "owned-process",
            identity.model_dump(mode="json"),
            trial_id=state.trial_id,
            attempt_id=state.attempt_id,
        )
        state = state.model_copy(update={"owned": (*state.owned, identity)})
        self.attempts[state.attempt_id] = state
        self.unreconciled.add(state.attempt_id)
        return state

    def _validate(self, state: AttemptState) -> None:
        previous = self.attempts.get(state.attempt_id)
        if previous is None:
            if state.stage != TrialStage.RESERVED:
                raise InputError("New attempt must begin reserved.")
            return
        if state.trial_id != previous.trial_id or state.workspace != previous.workspace:
            raise InputError("Attempt identity cannot change.")
        if state.stage != previous.stage and state.stage not in STAGE_TRANSITIONS[previous.stage]:
            raise InputError("Invalid attempt lifecycle transition.")
        if previous.launch_attempted and not state.launch_attempted:
            raise InputError("A durable launch intent cannot be erased.")
        if previous.capture_id is not None and state.capture_id != previous.capture_id:
            raise InputError("A captured attempt cannot replace its frozen output.")

    def configure(self, options: RunOptions) -> None:
        if self.options is not None:
            raise InputError("Run already has frozen execution options.")
        self.journal.append("run-options", options.model_dump(mode="json"))
        self.options = options

    def save(self, state: AttemptState) -> None:
        state = parse_model(canonical_json(state), AttemptState)
        self._validate(state)
        previous = self.attempts.get(state.attempt_id)
        if previous is not None:
            known = {(identity.pid, identity.created) for identity in previous.owned}
            for identity in state.owned:
                if (identity.pid, identity.created) not in known:
                    previous = self.own(previous, identity)
        self.journal.append(
            "attempt-state",
            state.model_dump(mode="json", exclude={"owned"}),
            trial_id=state.trial_id,
            attempt_id=state.attempt_id,
        )
        self.attempts[state.attempt_id] = state
        self.journal.checkpoint(
            {"attempts": {key: value.stage.value for key, value in self.attempts.items()}}
        )

    def reserve(self, trial_id: str) -> AttemptState:
        attempt_id = "a-" + uuid4().hex
        state = AttemptState(
            attempt_id=attempt_id,
            trial_id=trial_id,
            created_at=datetime.now(UTC),
            workspace=str(self.journal.path / "attempts" / attempt_id / "workspace"),
        )
        self.save(state)
        return state

    def evidence(self, state: AttemptState, name: str, model: StrictModel) -> None:
        self.journal.append(
            name,
            model.model_dump(mode="json"),
            trial_id=state.trial_id,
            attempt_id=state.attempt_id,
        )
        if name in CLEANUP_EVENTS:
            self.cleanup_reports[state.attempt_id] = parse_model(
                canonical_json(model), CleanupReport
            )
            self.unreconciled.discard(state.attempt_id)

    def attempt_root(self, state: AttemptState) -> Path:
        expected = self.journal.path / "attempts" / state.attempt_id / "workspace"
        if Path(state.workspace) != expected:
            raise InputError("Saved attempt workspace is outside its owned run directory.")
        return expected.parent
