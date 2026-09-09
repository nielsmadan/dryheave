from pathlib import Path

import pytest

from dryheave.doctor import runtime_doctor
from dryheave.processes import CommandResult


def test_doctor_probes_only_transport_and_reports_missing_agent_and_runtime_root(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr("dryheave.doctor.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "dryheave.doctor.shutil.which", lambda name: None if name == "codex" else "/tools/" + name
    )

    def execute(command, root):
        calls.append((command, root))
        return CommandResult(command.argv, 0, b"tui-test 0.1.0-beta.3\n", b"", "exited")

    monkeypatch.setattr("dryheave.doctor.run_command", execute)
    result = runtime_doctor("tui-test", agent="codex", runtime_root=tmp_path / ("a" * 80))
    assert result["ready"] is False
    checks = {check["tool"]: check for check in result["checks"]}
    assert checks["tui-test"]["status"] == "matched"
    assert checks["codex"]["status"] == "missing"
    assert checks["runtime-root"]["status"] == "too-long"
    assert len(calls) == 1
    assert calls[0][0].argv == ("/tools/tui-test", "--version")
    assert (calls[0][0].timeout_seconds, calls[0][0].max_output_bytes) == (5, 4096)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "outcome,exit_code,text",
    [
        ("exited", 0, b"tui-test 9"),
        ("timeout", -9, b""),
        ("output_limit", -9, b"overflow"),
        ("exited", 1, b"tui-test 0.1.0-beta.3"),
    ],
)
def test_unusable_transport_versions_are_actionable(monkeypatch, outcome, exit_code, text):
    monkeypatch.setattr("dryheave.doctor.shutil.which", lambda name: "/tools/" + name)
    monkeypatch.setattr(
        "dryheave.doctor.run_command",
        lambda command, _root: CommandResult(command.argv, exit_code, text, b"", outcome),
    )
    result = runtime_doctor("tui-test")
    check = result["checks"][-1]
    assert result["ready"] is False
    assert check["status"] == "mismatch"
    assert check["exit_code"] == exit_code
    assert "0.1.0-beta.3" in check["action"]


def test_all_tools_missing_and_unsupported_platform_need_no_probes(monkeypatch):
    monkeypatch.setattr("dryheave.doctor.shutil.which", lambda _name: None)
    monkeypatch.setattr("dryheave.doctor.platform.system", lambda: "Windows")
    result = runtime_doctor("missing-tui", agent="claude")
    assert result["ready"] is False
    assert [check["status"] for check in result["checks"]] == [
        "available",
        "unsupported",
        "missing",
        "missing",
        "missing",
    ]


def test_available_tools_and_short_runtime_are_reported_without_agent_launch(monkeypatch):
    monkeypatch.setattr("dryheave.doctor.platform.system", lambda: "Linux")
    monkeypatch.setattr("dryheave.doctor.shutil.which", lambda name: "/tools/" + name)
    monkeypatch.setattr(
        "dryheave.doctor.run_command",
        lambda command, _root: CommandResult(
            command.argv, 0, b"tui-test 0.1.0-beta.3", b"", "exited"
        ),
    )
    result = runtime_doctor("tui-test", agent="claude", runtime_root=Path("/rt"))
    assert result["ready"] is True
    assert result["checks"][-1]["attempt_path_bytes"] == 14
    assert result["checks"][-2]["version"] is None


def test_transport_execution_error_becomes_a_diagnostic(monkeypatch):
    monkeypatch.setattr("dryheave.doctor.shutil.which", lambda name: "/tools/" + name)

    def fail(_command, _root):
        raise OSError("Executable disappeared")

    monkeypatch.setattr("dryheave.doctor.run_command", fail)
    result = runtime_doctor("tui-test")
    assert result["checks"][-1]["status"] == "error"
    assert result["checks"][-1]["action"] == "Executable disappeared"
