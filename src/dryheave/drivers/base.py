from collections.abc import Mapping
from typing import Protocol

from dryheave.drivers.models import (
    CleanupReport,
    EventCursor,
    LaunchObservation,
    NativeEvent,
    Screen,
)
from dryheave.errors import DryheaveError
from dryheave.profile_models import LaunchPlan


class TransportError(DryheaveError):
    code = "transport_error"


class DeliveryUncertain(TransportError):
    code = "delivery_uncertain"


class Terminal(Protocol):
    def start(self, plan: LaunchPlan, environment: Mapping[str, str]) -> LaunchObservation: ...
    def state(self) -> Screen: ...
    def paste(self, text: str) -> None: ...
    def enter(self) -> None: ...
    def close(self) -> CleanupReport: ...


class EventSource(Protocol):
    def poll(self) -> tuple[NativeEvent, ...]: ...
    def cursor(self) -> EventCursor: ...
