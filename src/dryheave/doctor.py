import os
import platform
import shutil
import sys
from pathlib import Path

from pydantic import JsonValue

from dryheave.constants import MAX_RUNTIME_PATH_BYTES
from dryheave.processes import run_command
from dryheave.tui_test_release import (
    TUI_TEST_ACTION,
    TUI_TEST_SPECIFIER,
    TUI_TEST_TESTED_COMMAND,
    reported_version,
    supported_tui_test,
    version_probe,
)


def _tool(executable: str) -> dict[str, JsonValue]:
    resolved = shutil.which(executable)
    if resolved is None:
        return {
            "tool": executable,
            "status": "missing",
            "action": f"Install {executable}; then supply its executable path or add it to PATH.",
        }
    return {
        "tool": executable,
        "path": str(Path(resolved).absolute()),
        "status": "available",
        "version": None,
        "action": "Record this executable's version; profile preflight --probe-version verifies the selected subject.",
    }


def _transport(executable: str) -> dict[str, JsonValue]:
    resolved = shutil.which(executable)
    if resolved is None:
        return {"tool": executable, "status": "missing", "action": TUI_TEST_ACTION}
    path = Path(resolved).absolute()
    try:
        result = run_command(version_probe(path), Path.cwd())
    except OSError as error:
        return {"tool": executable, "path": str(path), "status": "error", "action": str(error)}
    version = reported_version(result)
    matched = version is not None and supported_tui_test(version)
    return {
        "tool": executable,
        "path": str(path),
        "status": "matched" if matched else "mismatch",
        "supported_versions": TUI_TEST_SPECIFIER,
        "observed_version": result.stdout.decode(errors="replace").strip(),
        "outcome": result.outcome,
        "exit_code": result.returncode,
        "action": "No action needed." if matched else TUI_TEST_ACTION,
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
        _transport(tui_test),
    ]
    if agent is not None:
        checks.append(_tool(agent))
    if runtime_root is not None:
        length = len(os.fsencode(runtime_root.absolute())) + 11
        checks.append(
            {
                "tool": "runtime-root",
                "status": "available" if length <= MAX_RUNTIME_PATH_BYTES else "too-long",
                "attempt_path_bytes": length,
                "maximum_bytes": MAX_RUNTIME_PATH_BYTES,
                "action": "Use this explicit root with run --runtime-root."
                if length <= MAX_RUNTIME_PATH_BYTES
                else "Select a shorter explicit runtime root; each attempt adds 11 bytes.",
            }
        )
    return {
        "ready": all(check["status"] in {"available", "matched"} for check in checks),
        "checks": list(checks),
        "tui_test_install_command": TUI_TEST_TESTED_COMMAND,
        "limitations": [
            "Only tui-test --version is executed; agents and Git are located without launching them.",
            "No downloads, configuration discovery, credential reads or model calls are performed.",
            "Availability is not authentication, full native fidelity or a successful trial.",
            "Use profile preflight for exact selected argv, assets, environment references and fidelity issues.",
        ],
    }
