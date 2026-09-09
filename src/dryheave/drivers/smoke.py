import os
import sys
import time
from pathlib import Path

from dryheave.drivers.models import CleanupReport, DriverLimits
from dryheave.drivers.tui_test import TuiTestTerminal
from dryheave.errors import LimitError
from dryheave.filesystem import atomic_write
from dryheave.models import AgentKind, StrictModel
from dryheave.profile_models import ExecutableResolution, LaunchPlan

RECEIVER = (
    "import os,tty\n"
    "tty.setraw(0)\n"
    "os.write(1,b'READY\\r\\n')\n"
    "received=b''\n"
    "while not received.endswith(b'\\r'):\n"
    " received+=os.read(0,4096)\n"
    "os.write(1,b'RECEIVED_HEX='+received.hex().encode()+b'\\r\\n')\n"
)


class TransportSmokeResult(StrictModel):
    execution: str = "non-model-transport-smoke"
    exact_input_observed: bool
    terminal_exit_code: int | None
    cleanup: CleanupReport


def smoke_transport(executable: Path, runtime: Path) -> TransportSmokeResult:
    plan = LaunchPlan(
        profile_id="0" * 64,
        agent=AgentKind.CODEX,
        argv=(sys.executable, "-u", str(runtime.absolute() / "receiver.py")),
        cwd=str(runtime.absolute()),
        executable=ExecutableResolution(
            requested=sys.executable, resolved=sys.executable, expected_version=None
        ),
        config_roots={},
        generated_environment={},
        environment_references=(),
        runtime_files=(),
        generated_files={},
        overlays=(),
        discovery_roots=(),
        issues=(),
        fidelity="captured",
        launchable=True,
    )
    environment = {"PATH": os.defpath, "TERM": "xterm-256color", "TMPDIR": str(runtime.absolute())}
    limits = DriverLimits(duration_seconds=10, command_seconds=3)
    message = "-literal first line\nUnicode: \u03bb"
    expected = ("\x1b[200~" + message + "\x1b[201~\r").encode().hex()
    with TuiTestTerminal(executable, runtime, limits=limits) as terminal:
        atomic_write(runtime / "receiver.py", RECEIVER.encode(), replace=False)
        terminal.start(plan, environment)
        deadline = time.monotonic() + 5
        while "READY" not in terminal.state().text:
            if time.monotonic() >= deadline:
                raise LimitError("Non-model receiver did not become ready.")
            time.sleep(0.02)
        terminal.paste(message)
        terminal.enter()
        while True:
            screen = terminal.state()
            if (
                "RECEIVED_HEX=" + expected in screen.text.replace("\n", "")
                and screen.exited is not None
            ):
                break
            if time.monotonic() >= deadline:
                raise LimitError("Non-model receiver did not report exact input and exit.")
            time.sleep(0.02)
    return TransportSmokeResult(
        exact_input_observed=True, terminal_exit_code=screen.exited, cleanup=terminal.close()
    )
