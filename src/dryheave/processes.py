import io
import os
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dryheave.errors import InputError
from dryheave.models import CommandSpec


@dataclass(frozen=True)
class CommandControl:
    cancelled: threading.Event | None = None
    deadline: float | None = None
    on_poll: Callable[[], None] | None = None

    def poll(self) -> None:
        if self.on_poll is not None:
            self.on_poll()


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    outcome: Literal["exited", "timeout", "output_limit"]
    cleanup_coverage: str = "Process-group cleanup covers open inherited pipes; detached or pipe-closing descendants are unobserved."


def _stop(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def run_command(
    command: CommandSpec,
    root: Path,
    *,
    input_bytes: bytes = b"",
    environment: dict[str, str] | None = None,
    on_start: Callable[[int], None] | None = None,
    control: CommandControl | None = None,
) -> CommandResult:
    cwd = root if command.cwd == "." else root / command.cwd
    if not cwd.resolve().is_relative_to(root.resolve()):
        raise InputError("Command cwd escapes its workspace.")
    process = subprocess.Popen(
        command.argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    outcome: Literal["exited", "timeout", "output_limit"] = "exited"
    control = control or CommandControl()
    deadline = min(control.deadline or float("inf"), time.monotonic() + command.timeout_seconds)
    streams = (process.stdin, process.stdout, process.stderr)
    try:
        _observe_start(on_start, process.pid)
        with selectors.DefaultSelector() as selector:
            _register(selector, streams, bool(input_bytes))
            offset = 0
            while selector.get_map():
                control.poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (control.cancelled is not None and control.cancelled.is_set()):
                    outcome = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.data == "stdin":
                        try:
                            offset += os.write(key.fd, input_bytes[offset : offset + 65536])
                        except BrokenPipeError:
                            offset = len(input_bytes)
                        if offset == len(input_bytes):
                            selector.unregister(key.fileobj)
                            _close_input(process)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    space = command.max_output_bytes - sum(map(len, output.values()))
                    output[key.data].extend(chunk[:space])
                    if len(chunk) > space:
                        outcome = "output_limit"
                        break
                if outcome != "exited":
                    break
            if outcome == "exited":
                outcome = _wait(process, deadline, control)
    finally:
        if process.returncode is None:
            _stop(process)
        outcome = "output_limit" if _drain(streams, output, command.max_output_bytes) else outcome
        for stream in streams:
            if stream is not None:
                stream.close()
    return CommandResult(
        command.argv, process.returncode, bytes(output["stdout"]), bytes(output["stderr"]), outcome
    )


def _drain(
    streams: tuple[object, ...], output: dict[str, bytearray], max_output_bytes: int
) -> bool:
    for stream, label in zip(streams, ("stdin", "stdout", "stderr"), strict=True):
        if label == "stdin" or not isinstance(stream, io.BufferedIOBase) or stream.closed:
            continue
        while True:
            space = max_output_bytes - sum(map(len, output.values()))
            try:
                chunk = os.read(stream.fileno(), max(1, min(65536, space)))
            except OSError:
                break
            if not chunk:
                break
            output[label].extend(chunk[:space])
            if len(chunk) > space:
                return True
    return False


def _register(
    selector: selectors.BaseSelector, streams: tuple[object, ...], has_input: bool
) -> None:
    for stream, label in zip(streams, ("stdin", "stdout", "stderr"), strict=True):
        if not isinstance(stream, io.BufferedIOBase):
            raise InputError("Failed to allocate subprocess pipes.")
        os.set_blocking(stream.fileno(), False)
        if label == "stdin" and not has_input:
            stream.close()
        else:
            selector.register(
                stream, selectors.EVENT_WRITE if label == "stdin" else selectors.EVENT_READ, label
            )


def _close_input(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        process.stdin.close()


def _wait(
    process: subprocess.Popen[bytes], deadline: float, control: CommandControl
) -> Literal["exited", "timeout"]:
    while time.monotonic() < deadline and not (
        control.cancelled is not None and control.cancelled.is_set()
    ):
        control.poll()
        try:
            process.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            return "exited"
        except subprocess.TimeoutExpired:
            continue
    return "timeout"


def _observe_start(callback: Callable[[int], None] | None, pid: int) -> None:
    if callback is not None:
        callback(pid)
