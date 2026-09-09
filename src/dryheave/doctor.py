import os
import platform
import shutil
import sys
from pathlib import Path

from pydantic import JsonValue

from dryheave.models import CommandSpec
from dryheave.processes import run_command

TUI_TEST_VERSION = "0.1.0-beta.3"
TUI_TEST_RELEASE = "https://github.com/microsoft/tui-test/releases/tag/" + TUI_TEST_VERSION
MAX_RUNTIME_BYTES = 70


def _tool(executable: str, *, expected: str | None = None) -> dict[str, JsonValue]:
    resolved = shutil.which(executable)
    if resolved is None:
        return {
            "tool": executable,
            "status": "missing",
            "action": f"Install {executable}; then supply its executable path or add it to PATH.",
        }
    path = str(Path(resolved).absolute())
    if expected is None:
        return {
            "tool": executable,
            "path": path,
            "status": "available",
            "version": None,
            "action": "Record this executable's version; profile preflight --probe-version verifies the selected subject.",
        }
    try:
        result = run_command(
            CommandSpec(argv=(path, "--version"), timeout_seconds=5, max_output_bytes=4096),
            Path.cwd(),
        )
    except OSError as error:
        return {"tool": executable, "path": path, "status": "error", "action": str(error)}
    matched = (
        result.outcome == "exited"
        and result.returncode == 0
        and result.stdout.decode(errors="replace").strip() == expected
    )
    return {
        "tool": executable,
        "path": path,
        "status": "matched" if matched else "mismatch",
        "expected_version": expected,
        "observed_version": result.stdout.decode(errors="replace").strip(),
        "outcome": result.outcome,
        "exit_code": result.returncode,
        "action": "No action needed."
        if matched
        else f"Install the pinned release: {TUI_TEST_RELEASE}",
    }


def runtime_doctor(
    tui_test: str, *, agent: str | None = None, runtime_root: Path | None = None
) -> dict[str, JsonValue]:
    system = platform.system()
    checks: list[dict[str, JsonValue]] = [
        {
            "tool": "python",
            "status": "available",
            "version": platform.python_version(),
            "path": sys.executable,
        },
        {
            "tool": "platform",
            "status": "available" if system in {"Darwin", "Linux"} else "unsupported",
            "system": system,
        },
        _tool("git"),
        _tool(tui_test, expected=f"tui-test {TUI_TEST_VERSION}"),
    ]
    if agent is not None:
        checks.append(_tool(agent))
    if runtime_root is not None:
        length = len(os.fsencode(runtime_root.absolute())) + 11
        checks.append(
            {
                "tool": "runtime-root",
                "status": "available" if length <= MAX_RUNTIME_BYTES else "too-long",
                "attempt_path_bytes": length,
                "maximum_bytes": MAX_RUNTIME_BYTES,
                "action": "Use this explicit root with run --runtime-root."
                if length <= MAX_RUNTIME_BYTES
                else "Select a shorter explicit runtime root; each attempt adds 11 bytes.",
            }
        )
    return {
        "ready": all(check["status"] in {"available", "matched"} for check in checks),
        "checks": list(checks),
        "tui_test_release": TUI_TEST_RELEASE,
        "limitations": [
            "Only tui-test --version is executed; agents and Git are located without launching them.",
            "No downloads, configuration discovery, credential reads or model calls are performed.",
            "Availability is not authentication, full native fidelity or a successful trial.",
            "Use profile preflight for exact selected argv, assets, environment references and fidelity issues.",
        ],
    }
