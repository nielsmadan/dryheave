from collections.abc import Mapping
from pathlib import Path

from dryheave.drivers.fake import FakeEventSource, FakeTerminal
from dryheave.drivers.models import LaunchObservation, NativeEvent, Screen
from dryheave.errors import InputError
from dryheave.experiment_models import FixtureTurn
from dryheave.filesystem import atomic_write
from dryheave.models import AgentKind
from dryheave.profile_models import LaunchPlan


class FixtureTerminal(FakeTerminal):
    def __init__(
        self, turns: tuple[FixtureTurn, ...], source: FakeEventSource, agent: AgentKind
    ) -> None:
        super().__init__([])
        self.turns = turns
        self.source = source
        self.marker = "\u203a" if agent == AgentKind.CODEX else "\u276f"
        self.current = Screen(text=self.marker, exited=None)

    def start(self, plan: LaunchPlan, environment: Mapping[str, str]) -> LaunchObservation:
        observed = super().start(plan, environment)
        return observed.model_copy(
            update={"transport": "offline-fixture", "observed_cwd": plan.cwd}
        )

    def paste(self, text: str) -> None:
        super().paste(text)
        self.current = Screen(text=self.marker + " " + text, exited=None)

    def enter(self) -> None:
        if self.plan is None:
            raise InputError("Fixture was not started.")
        index = self.enters
        super().enter()
        if index >= len(self.turns) or self.pastes[-1] != self.turns[index].prompt.strip():
            raise InputError("Offline subject fixture diverged from its exact expected prompt.")
        turn = self.turns[index]
        for name, text in turn.files.items():
            atomic_write(Path(self.plan.cwd) / name, text.encode())
        identifier = f"turn-{index}"
        fields = {"session_id": "fixture-root", "thread_id": "fixture-root", "turn_id": identifier}
        self.source.batches.append(
            [
                NativeEvent.model_validate(
                    {"kind": "metadata", "data": {"cwd": self.plan.cwd}, **fields}
                ),
                NativeEvent.model_validate(
                    {
                        "kind": "accepted",
                        "text": self.pastes[-1],
                        "native_id": identifier + "-user",
                        **fields,
                    }
                ),
                NativeEvent.model_validate({"kind": "started", **fields}),
                NativeEvent.model_validate({"kind": "assistant", "text": turn.assistant, **fields}),
                NativeEvent.model_validate(
                    {
                        "kind": "aborted" if turn.state == "cancelled" else "completed",
                        "text": turn.assistant,
                        **fields,
                    }
                ),
            ]
        )
        if turn.state == "approval":
            screen = "Would you like to run the following command?\n\n\u203a 1. Yes, proceed (y)\n  2. No, and tell Codex what to do differently (esc)\n\nPress enter to confirm or esc to cancel"
        elif turn.state == "unsupported":
            screen = "Unknown service: select an option\n\u203a 1. Proceed\n  2. Cancel\n\nPress enter to confirm or esc to cancel"
        else:
            screen = turn.assistant + "\n" + self.marker
        self.current = Screen(text=screen, exited=None)
