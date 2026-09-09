from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import InputError
from dryheave.journals import RunJournal
from dryheave.models import StrictModel, TrialStage
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


class ExecutionJournal:
    def __init__(self, journal: RunJournal) -> None:
        self.journal = journal
        self.options: RunOptions | None = None
        self.attempts: dict[str, AttemptState] = {}
        for event in journal.events:
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

    def own(self, state: AttemptState, identity: ProcessIdentity) -> AttemptState:
        self.journal.append(
            "owned-process",
            identity.model_dump(mode="json"),
            trial_id=state.trial_id,
            attempt_id=state.attempt_id,
        )
        state = state.model_copy(update={"owned": (*state.owned, identity)})
        self.attempts[state.attempt_id] = state
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

    def attempt_root(self, state: AttemptState) -> Path:
        expected = self.journal.path / "attempts" / state.attempt_id / "workspace"
        if Path(state.workspace) != expected:
            raise InputError("Saved attempt workspace is outside its owned run directory.")
        return expected.parent
