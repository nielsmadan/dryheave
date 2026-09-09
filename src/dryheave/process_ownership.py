import threading
import time
from collections.abc import Callable
from contextlib import suppress

import psutil

from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import LimitError


class ProcessOwner:
    def __init__(
        self, *, max_processes: int = 256, max_observations: int = 20000, interval: float = 0.05
    ) -> None:
        self.max_processes = max_processes
        self.max_observations = max_observations
        self.interval = interval
        self.processes: dict[ProcessIdentityKey, psutil.Process] = {}
        self.identities: dict[ProcessIdentityKey, ProcessIdentity] = {}
        self.published: set[ProcessIdentityKey] = set()
        self.errors: set[str] = set()
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()

    def add(self, process: psutil.Process) -> ProcessIdentity:
        with self.lock:
            identity = ProcessIdentity(
                pid=process.pid, created=process.create_time(), parent_pid=process.ppid()
            )
            key = (identity.pid, identity.created)
            if key not in self.processes:
                self._prune()
                if (
                    len(self.processes) >= self.max_processes
                    or len(self.identities) >= self.max_observations
                ):
                    self.errors.add("process_limit")
                    raise LimitError("Owned process observation limit exceeded.")
                self.processes[key] = process
                self.identities[key] = identity
            return identity

    def snapshot(self) -> tuple[ProcessIdentity, ...]:
        with self.lock:
            return tuple(self.identities.values())

    def publish(self, on_identity: Callable[[ProcessIdentity], None]) -> None:
        for identity in self.snapshot():
            key = (identity.pid, identity.created)
            if key not in self.published:
                on_identity(identity)
                self.published.add(key)

    def scan(self) -> None:
        with self.lock:
            self._prune()
            pending = list(self.processes.values())
            seen: set[psutil.Process] = set()
            while pending:
                process = pending.pop()
                if process in seen:
                    continue
                seen.add(process)
                try:
                    if not process.is_running():
                        continue
                    children = process.children()
                    for child in children:
                        self.add(child)
                    pending.extend(children)
                except psutil.NoSuchProcess:
                    continue
                except psutil.AccessDenied:
                    self.errors.add("process_visibility_denied")

    def _prune(self) -> None:
        for key, process in tuple(self.processes.items()):
            try:
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    del self.processes[key]
            except psutil.NoSuchProcess:
                del self.processes[key]
            except psutil.AccessDenied:
                self.errors.add("process_visibility_denied")

    def _monitor(self) -> None:
        while not self.stopping.wait(self.interval):
            try:
                self.scan()
            except LimitError:
                return

    def _alive(self) -> list[psutil.Process]:
        result = []
        for process in self.processes.values():
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    result.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                self.errors.add("process_visibility_denied")
                result.append(process)
        return result

    def stop(self, *, timeout: float, terminal_closed: bool) -> CleanupReport:
        self.stopping.set()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            self.errors.add("process_monitor_did_not_stop")
        deadline = time.monotonic() + timeout
        with self.lock:
            with suppress(LimitError):
                self.scan()
            for hard in (False, True):
                alive = self._alive()
                for process in reversed(alive):
                    try:
                        if hard:
                            process.kill()
                        else:
                            process.terminate()
                    except psutil.NoSuchProcess:
                        continue
                    except psutil.AccessDenied:
                        self.errors.add("process_signal_denied")
                remaining = max(0.0, deadline - time.monotonic())
                wait_deadline = time.monotonic() + remaining / (1 if hard else 2)
                while self._alive() and time.monotonic() < wait_deadline:
                    time.sleep(min(self.interval, max(0, wait_deadline - time.monotonic())))
            survivors = {(p.pid, p.create_time()) for p in self._alive()}
            return CleanupReport(
                terminal_closed=terminal_closed,
                known_writers_stopped=not survivors and not self.errors,
                owned=tuple(self.identities.values()),
                survivors=tuple(self.identities[key] for key in survivors),
                errors=tuple(sorted(self.errors)),
            )


type ProcessIdentityKey = tuple[int, float]
