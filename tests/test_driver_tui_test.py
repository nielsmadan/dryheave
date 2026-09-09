import json
from pathlib import Path

import pytest

from dryheave.drivers.base import DeliveryUncertain, TransportError
from dryheave.drivers.models import DriverLimits
from dryheave.drivers.tui_test import TuiTestTerminal
from dryheave.errors import InputError
from dryheave.processes import CommandResult


def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("dryheave.drivers.tui_test.MAX_RUNTIME_PATH_BYTES", 1000)
    return TuiTestTerminal(Path(".tools/tui-test/tui-test"), tmp_path / "terminal")


def test_cli_envelopes_literal_argument_and_exit_zero(tmp_path, monkeypatch):
    calls = []
    responses = [
        b'{"ok":true,"data":{}}',
        b'{"ok":true,"data":{"text":"Done","exited":0,"ready":false}}',
        b'{"pid":null,"running":false,"session":"s"}',
    ]

    def run(command, root, **kwargs):
        calls.append(command.argv)
        output = responses.pop(0)
        return CommandResult(
            command.argv, 3 if b'"running"' in output else 0, output, b"", "exited"
        )

    monkeypatch.setattr("dryheave.drivers.tui_test.run_command", run)
    with terminal(tmp_path, monkeypatch) as transport:
        transport.paste("-literal\nsecond")
        screen = transport.state()
        assert screen.exited == 0
        assert screen.text == "Done"
        assert transport._call("daemon", "status")["running"] is False
    assert calls[0][-3:] == ("type", "--", "\x1b[200~-literal\nsecond\x1b[201~")


def test_command_timeout_is_delivery_uncertain(tmp_path, monkeypatch):
    def run(command, root, **kwargs):
        return CommandResult(command.argv, -9, b"", b"", "timeout")

    monkeypatch.setattr("dryheave.drivers.tui_test.run_command", run)
    with terminal(tmp_path, monkeypatch) as transport, pytest.raises(DeliveryUncertain):
        transport.enter()


def test_wrong_state_shape_and_control_input_fail(tmp_path, monkeypatch):
    def run(command, root, **kwargs):
        return CommandResult(
            command.argv, 0, b'{"ok":true,"data":{"text":"x","exited":false}}', b"", "exited"
        )

    monkeypatch.setattr("dryheave.drivers.tui_test.run_command", run)
    with terminal(tmp_path, monkeypatch) as transport:
        with pytest.raises(TransportError, match="nullable"):
            transport.state()
        with pytest.raises(InputError, match="control"):
            transport.paste("\x1b[A")


def test_runtime_must_be_new_short_and_controller_owned(tmp_path, monkeypatch):
    with pytest.raises(InputError, match="70 bytes"):
        TuiTestTerminal(Path("driver"), tmp_path / ("x" * 100))
    with terminal(tmp_path, monkeypatch) as transport:
        transport.controller_pid = -1
        with pytest.raises(TransportError, match="original persistent"):
            transport.state()


def test_watchdog_stops_known_processes_without_caller_polling(tmp_path, monkeypatch):
    monkeypatch.setattr("dryheave.drivers.tui_test.MAX_RUNTIME_PATH_BYTES", 1000)
    with TuiTestTerminal(
        Path("driver"),
        tmp_path / "terminal",
        limits=DriverLimits(duration_seconds=0.02, poll_seconds=0.01, cleanup_seconds=0.1),
    ) as transport:
        transport.watchdog.join(timeout=1)
        assert transport.watchdog_error == "Terminal artifact or lifetime limit exceeded."
        assert transport.owner.stopping.is_set()


@pytest.mark.parametrize("name,code,running", [("running", 0, True), ("absent", 3, False)])
def test_recorded_daemon_status_shapes(tmp_path, monkeypatch, name, code, running):
    output = Path(f"tests/fixtures/tui-test-{name}-status.json").read_bytes()

    def run(command, root, **kwargs):
        return CommandResult(command.argv, code, output, b"", "exited")

    monkeypatch.setattr("dryheave.drivers.tui_test.run_command", run)
    with terminal(tmp_path, monkeypatch) as transport:
        assert transport._call("daemon", "status")["running"] is running


def test_subject_flags_follow_transport_option_boundary(tmp_path, monkeypatch, driver_plan):
    calls = []

    def run(command, root, **kwargs):
        return CommandResult(command.argv, 0, b"tui-test 0.1.0-beta.3\n", b"", "exited")

    def call(transport, *arguments, cleanup=False):
        calls.append(arguments)
        if arguments[0] == "run":
            raise TransportError("fixture stopped before subject launch")
        return {"running": False}

    monkeypatch.setattr("dryheave.drivers.tui_test.run_command", run)
    monkeypatch.setattr(TuiTestTerminal, "_call", call)
    plan = driver_plan.model_copy(
        update={
            "argv": (*driver_plan.argv, "--config", 'first="café path"', "--config", "second=true")
        }
    )
    with terminal(tmp_path, monkeypatch) as transport:
        with pytest.raises(TransportError, match="fixture stopped"):
            transport.start(plan, {})
        assert calls[0][calls[0].index("--") + 1 :] == plan.argv


def test_cli_parse_rejection_retains_original_diagnostics(tmp_path, monkeypatch):
    diagnostic = b"error: the argument '--config <PATH>' cannot be used multiple times\n"

    def run(command, root, **kwargs):
        return CommandResult(command.argv, 2, b"", diagnostic, "exited")

    monkeypatch.setattr("dryheave.drivers.tui_test.run_command", run)
    with terminal(tmp_path, monkeypatch) as transport:
        with pytest.raises(TransportError, match=r"Terminal run returned exit 2.*retained command"):
            transport._call("run", "--config", "first", "--config", "second")
        evidence = json.loads(next(transport.runtime.glob("*-command.json")).read_bytes())
        assert evidence["exit_code"] == 2
        assert evidence["stderr"] == diagnostic.decode()
        assert evidence["stdout"] == ""
