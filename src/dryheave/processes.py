import io
import os
import selectors
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dryheave.errors import InputError
from dryheave.models import CommandSpec


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
    deadline = time.monotonic() + command.timeout_seconds
    streams = (process.stdin, process.stdout, process.stderr)
    try:
        with selectors.DefaultSelector() as selector:
            _register(selector, streams, bool(input_bytes))
            offset = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
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
                outcome = _wait(process, deadline)
    finally:
        if process.returncode is None:
            _stop(process)
        for stream in streams:
            if stream is not None:
                stream.close()
    return CommandResult(
        command.argv, process.returncode, bytes(output["stdout"]), bytes(output["stderr"]), outcome
    )


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


def _wait(process: subprocess.Popen[bytes], deadline: float) -> Literal["exited", "timeout"]:
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return "timeout"
    return "exited"
