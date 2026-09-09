import json
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Self

import psutil
from pydantic import JsonValue

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.base import DeliveryUncertain, TransportError
from dryheave.drivers.models import (
    CleanupReport,
    DriverLimits,
    LaunchObservation,
    ProcessIdentity,
    Screen,
)
from dryheave.errors import InputError, LimitError
from dryheave.filesystem import atomic_write
from dryheave.logs.base import mapping, string
from dryheave.models import CommandSpec
from dryheave.process_ownership import ProcessOwner
from dryheave.processes import run_command
from dryheave.profile_models import LaunchPlan
from dryheave.serialization import parse_json

MAX_RUNTIME_PATH_BYTES = 70
DAEMON_ABSENT_EXIT = 3


class TuiTestTerminal:
    version = "0.1.0-beta.3"

    def __init__(
        self, executable: Path, runtime: Path, *, limits: DriverLimits | None = None
    ) -> None:
        self.executable = executable.absolute()
        self.runtime = runtime.absolute()
        if len(os.fsencode(self.runtime)) > MAX_RUNTIME_PATH_BYTES:
            raise InputError("TUI_TEST_HOME must be at most 70 bytes for portable Unix sockets.")
        self.limits = limits or DriverLimits()
        self.session = "s" + uuid.uuid4().hex[:8]
        self.artifacts = ArtifactWriter(self.runtime, self.limits.max_artifact_bytes)
        self.environment: dict[str, str] = {}
        self.owner = ProcessOwner(
            max_processes=self.limits.max_processes, max_observations=self.limits.max_events
        )
        self.calls = 0
        self.controller_pid = os.getpid()
        self.created = time.time()
        self.deadline = time.monotonic() + self.limits.duration_seconds
        self.started = False
        self.daemon_identity: ProcessIdentity | None = None
        self.closed: CleanupReport | None = None
        self.watchdog_stop = threading.Event()
        self.watchdog_error: str | None = None
        self.watchdog = threading.Thread(target=self._watch, daemon=True)
        self.watchdog.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _claim(self, pid: int) -> None:
        with suppress(psutil.NoSuchProcess):
            self.owner.add(psutil.Process(pid))

    def _check(self) -> None:
        if os.getpid() != self.controller_pid:
            raise TransportError(
                "Terminal operations require their original persistent controller."
            )
        if self.closed is not None:
            raise TransportError("Terminal is closed.")
        if self.watchdog_error is not None:
            raise LimitError(self.watchdog_error)
        if time.monotonic() >= self.deadline:
            raise LimitError("Terminal lifetime exceeded.")

    def _call(self, *arguments: str, cleanup: bool = False) -> dict[str, JsonValue]:
        if not cleanup:
            self._check()
            self.calls += 1
            if self.calls > self.limits.max_polls:
                raise LimitError("Terminal command count exceeded its polling limit.")
        remaining = (
            self.limits.command_seconds
            if cleanup
            else min(self.limits.command_seconds, self.deadline - time.monotonic())
        )
        result = run_command(
            CommandSpec(
                argv=(str(self.executable), "--json", "--session", self.session, *arguments),
                timeout_seconds=max(1, math.ceil(remaining)),
                max_output_bytes=self.limits.max_command_bytes,
            ),
            self.runtime,
            environment=self.environment,
            on_start=self._claim,
        )
        if not cleanup:
            self.artifacts.record(
                "command",
                {
                    "operation": arguments[0],
                    "outcome": result.outcome,
                    "exit_code": result.returncode,
                    "stdout": result.stdout.decode(errors="replace"),
                    "stderr": result.stderr.decode(errors="replace"),
                },
            )
        if result.outcome != "exited":
            raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")
        response = parse_json(result.stdout)
        if arguments == ("daemon", "status"):
            if result.returncode == DAEMON_ABSENT_EXIT and response.get("running") is False:
                return response
            data = mapping(response.get("data"))
            if (
                result.returncode == 0
                and response.get("ok") is True
                and type(data.get("pid")) is int
            ):
                return data | {"running": True}
            raise TransportError("Invalid daemon status response.")
        if result.returncode != 0 or response.get("ok") is not True:
            raise TransportError(
                "Terminal rejected the requested operation; see retained command evidence."
            )
        return mapping(response.get("data"))

    def start(self, plan: LaunchPlan, environment: Mapping[str, str]) -> LaunchObservation:
        try:
            return self._start(plan, environment)
        except BaseException:
            self.close()
            raise

    def _start(self, plan: LaunchPlan, environment: Mapping[str, str]) -> LaunchObservation:
        self._check()
        if self.started or not plan.launchable:
            raise InputError("Terminal needs one launchable, unused LaunchPlan.")
        self.environment = dict(environment)
        self.environment["TUI_TEST_HOME"] = str(self.runtime)
        version = run_command(
            CommandSpec(
                argv=(str(self.executable), "--version"), timeout_seconds=5, max_output_bytes=4096
            ),
            self.runtime,
            environment=self.environment,
            on_start=self._claim,
        )
        if (
            version.outcome != "exited"
            or version.returncode != 0
            or version.stdout.strip() != f"tui-test {self.version}".encode()
        ):
            raise TransportError("Driver executable does not report the pinned tui-test version.")
        config = self.runtime / "transport.toml"
        atomic_write(
            config,
            (
                '[recording]\nmode = "always"\ndirectory = '
                + json.dumps(str(self.runtime / "recordings"))
                + "\n"
            ).encode(),
            replace=False,
        )
        observed = LaunchObservation(
            transport="tui-test",
            transport_version=self.version,
            session=self.session,
            runtime=str(self.runtime),
            requested_profile_id=plan.profile_id,
            requested_argv=plan.argv,
            requested_cwd=plan.cwd,
        )
        self.artifacts.record("launch-intent", observed)
        self.started = True
        try:
            self._call(
                "run",
                "--cwd",
                plan.cwd,
                "--cols",
                "120",
                "--rows",
                "40",
                "--config",
                str(config),
                "--no-wait-ready",
                *plan.argv,
            )
            status = self._call("daemon", "status")
            pid = status.get("pid")
            if type(pid) is not int or not status.get("running"):
                raise TransportError("Launched terminal has no observed daemon identity.")
            daemon = psutil.Process(pid)
            if (
                daemon.create_time() < self.created - 1
                or Path(daemon.exe()).resolve() != self.executable.resolve()
            ):
                raise TransportError("Daemon identity does not match this owned launch.")
            self.daemon_identity = self.owner.add(daemon)
            observed = observed.model_copy(update={"daemon": self.daemon_identity})
            self.owner.scan()
            self.artifacts.record("launch-observed", observed)
            return observed
        except BaseException:
            self.close()
            raise

    def state(self) -> Screen:
        data = self._call("state")
        if (
            not isinstance(data.get("text"), str)
            or (data.get("exited") is not None and type(data.get("exited")) is not int)
            or "exited" not in data
        ):
            raise TransportError("Terminal state is missing valid text or nullable exit status.")
        screen = Screen.model_validate(
            {
                "text": data["text"],
                "exited": data["exited"],
                "cols": data.get("cols", 120),
                "rows": data.get("rows", 40),
                "ready": data.get("ready") is True,
                "cwd": string(data.get("cwd")),
                "cursor": mapping(data.get("cursor")),
            }
        )
        self.owner.scan()
        self.artifacts.record("screen", screen)
        return screen

    def paste(self, text: str) -> None:
        if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
            raise InputError("Terminal text contains control characters.")
        if len(text.encode()) > self.limits.max_input_bytes:
            raise LimitError("Terminal text exceeds its input byte limit.")
        self._call("type", "--", "\x1b[200~" + text + "\x1b[201~")

    def enter(self) -> None:
        try:
            self._call("key", "press", "Enter")
        except (TransportError, InputError, OSError) as error:
            raise DeliveryUncertain("Enter delivery needs native-log reconciliation.") from error

    def _watch(self) -> None:
        while not self.watchdog_stop.wait(self.limits.poll_seconds):
            try:
                exceeded = self._artifact_size() > self.limits.max_artifact_bytes
            except (OSError, LimitError):
                exceeded = True
            if exceeded or time.monotonic() >= self.deadline or self.owner.errors:
                self.watchdog_error = "Terminal artifact or lifetime limit exceeded."
                self.owner.stop(timeout=self.limits.cleanup_seconds, terminal_closed=False)
                return

    def _artifact_size(self) -> int:
        total = count = 0
        for root, dirs, files in os.walk(self.runtime, followlinks=False):
            count += len(dirs) + len(files)
            if count > self.limits.max_files * 100:
                raise LimitError("Transport artifact entry limit exceeded.")
            for name in files:
                with suppress(FileNotFoundError):
                    total += (Path(root) / name).lstat().st_size
        return total

    def close(self) -> CleanupReport:
        if self.closed is not None:
            return self.closed
        self.watchdog_stop.set()
        self.watchdog.join(timeout=self.limits.cleanup_seconds)
        terminal_closed = not self.started
        errors: list[str] = []
        with suppress(LimitError):
            self.owner.scan()
        if self.started:
            try:
                status = self._call("daemon", "status", cleanup=True)
                if status.get("running"):
                    self._verify_daemon(status)
                    self._call("close", cleanup=True)
                terminal_closed = (
                    self._call("daemon", "status", cleanup=True).get("running") is False
                )
            except (TransportError, InputError, OSError):
                errors.append("terminal_close_failed")
        report = self.owner.stop(
            timeout=self.limits.cleanup_seconds, terminal_closed=terminal_closed
        )
        report = report.model_copy(update={"errors": report.errors + tuple(errors)})
        self.closed = report
        with suppress(LimitError, OSError):
            self.artifacts.record("cleanup", report)
        return report

    def _verify_daemon(self, status: dict[str, JsonValue]) -> None:
        pid = status.get("pid")
        if type(pid) is not int or self.daemon_identity is None or pid != self.daemon_identity.pid:
            raise TransportError("Terminal daemon ownership could not be reconciled.")
        try:
            process = psutil.Process(pid)
            if process.create_time() != self.daemon_identity.created:
                raise TransportError("Terminal daemon PID identity changed.")
        except psutil.Error as error:
            raise TransportError("Terminal daemon identity is unavailable.") from error
