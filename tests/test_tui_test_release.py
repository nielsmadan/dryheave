from pathlib import Path

import pytest

from dryheave.processes import CommandResult
from dryheave.tui_test_release import (
    TUI_TEST_ACTION,
    TUI_TEST_SPECIFIER,
    TUI_TEST_SUPPORTED,
    manual_install,
    newest_supported_release,
    parse_tui_test_version,
    reported_version,
    supported_tui_test,
    tui_test_install_command,
    version_probe,
)


def test_the_supported_range_reads_the_same_in_prose_and_as_a_specifier():
    assert TUI_TEST_SUPPORTED == "0.1.x, from 0.1.0"
    assert TUI_TEST_SPECIFIER == ">=0.1.0,<0.2.0"


@pytest.mark.parametrize(
    "version,supported",
    [
        ("0.1.0", True),
        ("0.1.1", True),
        ("0.1.27", True),
        ("0.1.0-beta.5", False),
        ("0.1.0-beta.3", False),
        ("0.2.0-beta.1", False),
        ("0.2.0", False),
        ("1.0.0", False),
        ("0.1", False),
        ("latest", False),
    ],
)
def test_any_0_1_release_from_0_1_0_is_supported(version, supported):
    assert supported_tui_test(version) is supported


@pytest.mark.parametrize(
    "tags,newest",
    [
        (["0.1.1", "0.1.10", "0.1.9", "0.1.0"], "0.1.10"),
        (["0.2.0", "0.1.2-beta.1", "0.1.1"], "0.1.1"),
        (["0.1.0-beta.5", "0.0.4", "1.0.0"], None),
        (["0.1.3; rm -rf ~", " 0.1.3", "v0.1.3", "0.1.2"], "0.1.2"),
        ([], None),
    ],
)
def test_the_newest_plain_supported_release_is_picked(tags, newest):
    assert newest_supported_release(tags) == newest


def test_the_install_command_fetches_the_script_and_binary_of_one_release():
    assert tui_test_install_command("0.1.4") == (
        "curl --proto '=https' --tlsv1.2 -LsSf "
        "https://raw.githubusercontent.com/microsoft/tui-test/0.1.4/install/install.sh"
        " | TUI_TEST_VERSION=0.1.4 sh"
    )


@pytest.mark.parametrize("version", ["0.1.4; rm -rf ~", "$(id)", "0.1.4-beta.1", "latest"])
def test_the_install_command_refuses_anything_but_a_release_version(version):
    with pytest.raises(ValueError, match="Not a tui-test release version"):
        tui_test_install_command(version)


@pytest.mark.parametrize(
    "output,version",
    [("tui-test 0.1.1\n", "0.1.1"), ("tui-test", None), ("other 0.1.1", None), ("", None)],
)
def test_the_version_is_read_from_the_tui_test_version_line(output, version):
    assert parse_tui_test_version(output) == version


def test_the_version_probe_has_finite_limits():
    probe = version_probe(Path("/opt/tui-test"))
    assert (probe.argv, probe.timeout_seconds, probe.max_output_bytes) == (
        ("/opt/tui-test", "--version"),
        5,
        4096,
    )


@pytest.mark.parametrize(
    ("outcome", "returncode", "version"),
    [("exited", 0, "0.1.1"), ("exited", 1, None), ("timeout", 0, None)],
)
def test_a_version_is_reported_only_by_a_clean_exit(outcome, returncode, version):
    result = CommandResult(("tui-test",), returncode, b"tui-test 0.1.1\n", b"", outcome)
    assert reported_version(result) == version


def test_install_advice_names_the_version_command_and_rerun():
    assert manual_install("0.1.3") == (
        "Install tui-test 0.1.3 (0.1.x, from 0.1.0) with:\n"
        f"  {tui_test_install_command('0.1.3')}\n"
        "then rerun dryheave init."
    )
    assert TUI_TEST_ACTION.endswith(
        f"install the last tested one yourself: {tui_test_install_command('0.1.1')}"
    )
