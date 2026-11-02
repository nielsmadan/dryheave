import hashlib
import io
import json
from pathlib import Path

import pytest

from conftest import fake_commands
from dryheave.processes import CommandResult
from dryheave.transport_setup import (
    InstallTarget,
    MissingTransportError,
    fetch_install_target,
    install_target_from,
    install_tui_test,
    installed_path,
    installer_environment,
    missing_transport_error,
    path_to_record,
    usable_transport,
)
from dryheave.tui_test_release import TUI_TEST_INSTALL_SCRIPT_SHA256

INSTALLED = b"Downloading tui-test for aarch64-apple-darwin...\nInstalled tui-test to /home/u/.local/bin/tui-test\n"
SCRIPT = b"#!/bin/sh\necho fixture\n"
NEWEST = InstallTarget("0.1.3", "found")
TESTED = InstallTarget("0.1.1", "failed")


def releases(*entries):
    return json.dumps(
        [
            {"tag_name": tag, "draft": draft, "prerelease": prerelease, "body": "notes"}
            for tag, draft, prerelease in entries
        ]
    ).encode()


def exited(stdout, returncode=0, outcome="exited"):
    return CommandResult(("curl",), returncode, stdout, b"", outcome)


def installer(monkeypatch, *, install=None, version=b"tui-test 0.1.3\n", download=0):
    def respond(command, _root, _options):
        if command.argv[0] == "curl":
            if download == 0:
                Path(command.argv[command.argv.index("-o") + 1]).write_bytes(SCRIPT)
            return CommandResult(command.argv, download, b"", b"curl: (6) no host\n", "exited")
        if command.argv[0] == "sh":
            return install or CommandResult(command.argv, 0, INSTALLED, b"", "exited")
        return CommandResult(command.argv, 0, version, b"", "exited")

    return fake_commands(monkeypatch, "dryheave.transport_setup", respond)


@pytest.mark.parametrize(
    "payload,target",
    [
        (
            releases(("0.1.1", False, False), ("0.1.3", False, False), ("0.2.0", False, False)),
            InstallTarget("0.1.3", "found"),
        ),
        (
            releases(("0.1.4", True, False), ("0.1.5", False, True), ("0.1.1", False, False)),
            InstallTarget("0.1.1", "found"),
        ),
        (releases(("0.2.0", False, False)), InstallTarget("0.1.1", "empty")),
        (b"[]", InstallTarget("0.1.1", "empty")),
        (b'{"message": "API rate limit exceeded"}', InstallTarget("0.1.1", "failed")),
        (b"<html>", InstallTarget("0.1.1", "failed")),
        (b'[{"tag_name": "0.1.3"}]', InstallTarget("0.1.1", "failed")),
        (
            b'[{"tag_name": "0.1.3", "draft": "false", "prerelease": false}]',
            InstallTarget("0.1.1", "failed"),
        ),
    ],
)
def test_the_newest_published_supported_release_is_read_from_github(payload, target):
    assert install_target_from(exited(payload)) == target


@pytest.mark.parametrize(
    "result",
    [
        exited(b"", 22),
        exited(b"", -9, "timeout"),
        exited(releases(("0.1.3", False, False)), -9, "output_limit"),
    ],
)
def test_a_failed_lookup_falls_back_to_the_tested_version(result):
    assert install_target_from(result) == TESTED


def test_the_lookup_asks_github_with_finite_limits(monkeypatch, tmp_path):
    calls = fake_commands(
        monkeypatch,
        "dryheave.transport_setup",
        lambda _c, _r, _o: exited(releases(("0.1.3", False, False))),
    )
    assert fetch_install_target(tmp_path) == NEWEST
    argv = calls[0].argv
    assert argv[0] == "curl"
    assert argv[argv.index("--connect-timeout") + 1] == "5"
    assert argv[argv.index("--max-time") + 1] == "10"
    assert argv[-1] == "https://api.github.com/repos/microsoft/tui-test/releases?per_page=30"
    assert (calls[0].timeout_seconds, calls[0].max_output_bytes) == (15, 4 * 1024 * 1024)


def test_a_lookup_without_curl_falls_back_to_the_tested_version(monkeypatch, tmp_path):
    def respond(_command, _root, _options):
        raise FileNotFoundError("curl")

    fake_commands(monkeypatch, "dryheave.transport_setup", respond)
    assert fetch_install_target(tmp_path) == TESTED


@pytest.mark.parametrize(
    "target,description",
    [
        (
            InstallTarget("0.1.1", "found"),
            "tui-test 0.1.1 is the newest supported release (0.1.x, from 0.1.0).",
        ),
        (
            NEWEST,
            (
                "tui-test 0.1.3 is the newest supported release (0.1.x, from 0.1.0). dryheave was "
                "last tested with 0.1.1, and this release's install script has no checksum on record."
            ),
        ),
        (
            TESTED,
            (
                "Could not check GitHub for tui-test releases; 0.1.1 is the last version tested "
                "with dryheave."
            ),
        ),
        (
            InstallTarget("0.1.1", "empty"),
            (
                "GitHub lists no supported tui-test release (0.1.x, from 0.1.0); 0.1.1 is the last "
                "version tested with dryheave."
            ),
        ),
    ],
)
def test_the_target_describes_where_its_version_came_from(target, description):
    assert target.description == description


def test_the_installer_gets_no_tokens_or_prefix_and_leaves_the_profile_alone():
    environment = {"HOME": "/home/u", "PATH": "/bin", "GH_TOKEN": "t", "GITHUB_TOKEN": "t"}
    assert installer_environment(environment | {"PREFIX": "/opt/conda"}, "0.1.3") == {
        "HOME": "/home/u",
        "PATH": "/bin",
        "TUI_TEST_VERSION": "0.1.3",
        "TUI_TEST_NO_MODIFY_PATH": "1",
    }


@pytest.mark.parametrize(
    "result,path",
    [
        (CommandResult(("sh",), 0, INSTALLED, b"", "exited"), Path("/home/u/.local/bin/tui-test")),
        (
            CommandResult(("sh",), 0, b"", b"Installed tui-test to bin/tui-test\n", "exited"),
            Path("/work/bin/tui-test"),
        ),
        (CommandResult(("sh",), 1, INSTALLED, b"", "exited"), None),
        (CommandResult(("sh",), -9, INSTALLED, b"", "timeout"), None),
        (CommandResult(("sh",), 0, b"nothing installed\n", b"", "exited"), None),
    ],
)
def test_the_installed_path_comes_from_a_clean_installer_run(result, path):
    assert installed_path(result, Path("/work")) == path


def test_install_runs_the_downloaded_script_and_returns_the_installed_binary(monkeypatch):
    output = io.StringIO()
    calls = installer(monkeypatch)
    path = install_tui_test(NEWEST, environment={"HOME": "/home/u", "GH_TOKEN": "t"}, output=output)
    assert path == Path("/home/u/.local/bin/tui-test")
    download, script, probe = calls
    assert download.argv[-1] == (
        "https://raw.githubusercontent.com/microsoft/tui-test/0.1.3/install/install.sh"
    )
    assert script.argv == ("sh", download.argv[download.argv.index("-o") + 1])
    assert (script.timeout_seconds, script.max_output_bytes) == (300, 64 * 1024)
    assert probe.argv == ("/home/u/.local/bin/tui-test", "--version")
    assert output.getvalue().startswith(
        "Downloading the tui-test 0.1.3 install script…\n"
        "Installing tui-test 0.1.3; this downloads a release archive and can take a minute…\n"
    )
    assert "Installed tui-test to /home/u/.local/bin/tui-test" in output.getvalue()


def test_install_passes_the_cleaned_environment_to_the_script(monkeypatch):
    seen = {}

    def respond(command, _root, options):
        if command.argv[0] == "curl":
            Path(command.argv[command.argv.index("-o") + 1]).write_bytes(SCRIPT)
        if command.argv[0] == "sh":
            seen.update(options["environment"])
            return CommandResult(command.argv, 0, INSTALLED, b"", "exited")
        return CommandResult(command.argv, 0, b"tui-test 0.1.3\n", b"", "exited")

    fake_commands(monkeypatch, "dryheave.transport_setup", respond)
    install_tui_test(NEWEST, environment={"HOME": "/home/u", "GH_TOKEN": "t"}, output=io.StringIO())
    assert seen == {"HOME": "/home/u", "TUI_TEST_VERSION": "0.1.3", "TUI_TEST_NO_MODIFY_PATH": "1"}


def test_a_checksummed_release_installs_only_a_matching_script(monkeypatch):
    tested = InstallTarget("0.1.1", "found")
    installer(monkeypatch, version=b"tui-test 0.1.1\n")
    with pytest.raises(MissingTransportError, match="does not match its recorded checksum"):
        install_tui_test(tested, environment={}, output=io.StringIO())
    monkeypatch.setitem(TUI_TEST_INSTALL_SCRIPT_SHA256, "0.1.1", hashlib.sha256(SCRIPT).hexdigest())
    path = install_tui_test(tested, environment={}, output=io.StringIO())
    assert path == Path("/home/u/.local/bin/tui-test")


def test_a_failed_download_says_how_to_install_by_hand(monkeypatch):
    calls = installer(monkeypatch, download=6)
    with pytest.raises(
        MissingTransportError,
        match=r"Downloading the tui-test install script failed \(exited, exit 6\)\. "
        r"Install tui-test 0\.1\.3 .*\n  curl .*/0\.1\.3/",
    ):
        install_tui_test(NEWEST, environment={}, output=io.StringIO())
    assert [call.argv[0] for call in calls] == ["curl"]


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(("sh",), 1, b"", b"curl: (6) Could not resolve host\n", "exited"),
        CommandResult(("sh",), -9, b"Downloading...\n", b"", "timeout"),
        CommandResult(("sh",), 0, b"nothing installed\n", b"", "exited"),
    ],
)
def test_a_failed_install_says_how_to_install_by_hand(monkeypatch, result):
    output = io.StringIO()
    installer(monkeypatch, install=result)
    with pytest.raises(MissingTransportError, match=r"did not finish.*\n  curl .*/0\.1\.3/"):
        install_tui_test(NEWEST, environment={}, output=output)
    assert output.getvalue().endswith(result.stdout.decode() + result.stderr.decode())


@pytest.mark.parametrize(
    "probe",
    [
        CommandResult(("tui-test",), 0, b"tui-test 0.2.0\n", b"", "exited"),
        CommandResult(("tui-test",), 1, b"tui-test 0.1.3\n", b"", "exited"),
    ],
)
def test_an_installed_binary_must_report_a_supported_version(monkeypatch, probe):
    def respond(command, _root, _options):
        if command.argv[0] == "curl":
            Path(command.argv[command.argv.index("-o") + 1]).write_bytes(SCRIPT)
            return CommandResult(command.argv, 0, b"", b"", "exited")
        if command.argv[0] == "sh":
            return CommandResult(command.argv, 0, INSTALLED, b"", "exited")
        return probe

    fake_commands(monkeypatch, "dryheave.transport_setup", respond)
    with pytest.raises(MissingTransportError, match="is not a supported tui-test"):
        install_tui_test(NEWEST, environment={}, output=io.StringIO())


def test_an_installed_binary_that_cannot_run_is_reported(monkeypatch):
    def respond(command, _root, _options):
        if command.argv[0] == "curl":
            Path(command.argv[command.argv.index("-o") + 1]).write_bytes(SCRIPT)
            return CommandResult(command.argv, 0, b"", b"", "exited")
        if command.argv[0] == "sh":
            return CommandResult(command.argv, 0, INSTALLED, b"", "exited")
        raise PermissionError("not executable")

    fake_commands(monkeypatch, "dryheave.transport_setup", respond)
    with pytest.raises(MissingTransportError, match="could not run: not executable"):
        install_tui_test(NEWEST, environment={}, output=io.StringIO())


@pytest.mark.parametrize(
    "respond,usable",
    [
        (lambda c, _r, _o: CommandResult(c.argv, 0, b"tui-test 0.1.1\n", b"", "exited"), True),
        (
            lambda c, _r, _o: CommandResult(c.argv, 0, b"tui-test 0.1.0-beta.3\n", b"", "exited"),
            False,
        ),
    ],
)
def test_a_transport_is_usable_only_when_it_reports_a_supported_version(
    monkeypatch, tmp_path, respond, usable
):
    fake_commands(monkeypatch, "dryheave.transport_setup", respond)
    assert usable_transport(Path("/opt/tui-test"), tmp_path) is usable


def test_a_missing_transport_is_not_usable(tmp_path):
    assert usable_transport(tmp_path / "gone", tmp_path) is False


def test_the_stop_message_names_the_version_command_and_the_rerun():
    message = str(missing_transport_error(NEWEST))
    assert message.startswith(
        "tui-test is not installed. Install tui-test 0.1.3 (0.1.x, from 0.1.0)"
    )
    assert "TUI_TEST_VERSION=0.1.3 sh\n" in message
    assert message.endswith("then rerun dryheave init.")


@pytest.mark.parametrize(
    "on_path,recorded,record",
    [
        (Path("/home/u/.local/bin/tui-test"), None, None),
        (None, None, Path("/home/u/.local/bin/tui-test")),
        (Path("/usr/local/bin/tui-test"), None, Path("/home/u/.local/bin/tui-test")),
        (
            Path("/home/u/.local/bin/tui-test"),
            "/gone/tui-test",
            Path("/home/u/.local/bin/tui-test"),
        ),
    ],
)
def test_an_install_is_recorded_unless_path_already_finds_it(on_path, recorded, record):
    assert path_to_record(Path("/home/u/.local/bin/tui-test"), on_path, recorded) == record
