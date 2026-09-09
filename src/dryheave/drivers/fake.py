from collections import deque
from collections.abc import Mapping, Sequence

from dryheave.drivers.base import DeliveryUncertain
from dryheave.drivers.models import (
    CleanupReport,
    EventCursor,
    LaunchObservation,
    NativeEvent,
    Screen,
)
from dryheave.profile_models import LaunchPlan


class FakeTerminal:
    def __init__(
        self,
        screens: Sequence[Screen],
        *,
        partial_paste: bool = False,
        uncertain_enter: bool = False,
    ) -> None:
        self.screens = deque(screens)
        self.current = Screen(text="\u203a", exited=None)
        self.partial_paste = partial_paste
        self.uncertain_enter = uncertain_enter
        self.pastes: list[str] = []
        self.enters = 0
        self.closed = False
        self.plan: LaunchPlan | None = None

    def start(self, plan: LaunchPlan, environment: Mapping[str, str]) -> LaunchObservation:
        self.plan = plan
        return LaunchObservation(
            transport="fake",
            transport_version="1",
            session="fake",
            runtime="fixture",
            requested_profile_id=plan.profile_id,
            requested_argv=plan.argv,
            requested_cwd=plan.cwd,
        )

    def state(self) -> Screen:
        if self.screens:
            self.current = self.screens.popleft()
        return self.current

    def paste(self, text: str) -> None:
        self.pastes.append(text)
        self.current = Screen(
            text="\u203a " + (text[:1] if self.partial_paste else text), exited=None
        )

    def enter(self) -> None:
        self.enters += 1
        self.current = Screen(text="Working\n\u203a", exited=None)
        if self.uncertain_enter:
            raise DeliveryUncertain("Fixture Enter acknowledgement lost.")

    def close(self) -> CleanupReport:
        self.closed = True
        return CleanupReport(
            terminal_closed=True,
            known_writers_stopped=True,
            coverage="fixture",
            limitation="Deterministic fixture has no native processes.",
        )


class FakeEventSource:
    def __init__(self, batches: Sequence[Sequence[NativeEvent]]) -> None:
        self.batches = deque(batches)
        self.sequence = 0

    def poll(self) -> tuple[NativeEvent, ...]:
        if not self.batches:
            return ()
        result = []
        for event in self.batches.popleft():
            self.sequence += 1
            result.append(event.model_copy(update={"sequence": self.sequence}))
        return tuple(result)

    def cursor(self) -> EventCursor:
        return EventCursor(sequence=self.sequence)

    def drained(self) -> bool:
        return not self.batches
