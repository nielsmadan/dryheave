import re
from collections.abc import Iterable
from pathlib import Path

from dryheave.models import CommandSpec
from dryheave.processes import CommandResult

TUI_TEST_MINIMUM = (0, 1, 0)
TUI_TEST_SERIES = TUI_TEST_MINIMUM[:2]
TUI_TEST_TESTED_VERSION = "0.1.1"
TUI_TEST_INSTALL_SCRIPT_SHA256 = {
    "0.1.1": "8a8f190c8998c2313725a87ae6000b9187e6453ba45ce3575c10076b88dbb884",
}
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")
RELEASE_PATTERN = re.compile(r"\d+\.\d+\.\d+")
VERSION_PROBE_SECONDS = 5
VERSION_PROBE_BYTES = 4096

_MINIMUM_TEXT = ".".join(map(str, TUI_TEST_MINIMUM))
TUI_TEST_SUPPORTED = f"{TUI_TEST_SERIES[0]}.{TUI_TEST_SERIES[1]}.x, from {_MINIMUM_TEXT}"
TUI_TEST_SPECIFIER = f">={_MINIMUM_TEXT},<{TUI_TEST_SERIES[0]}.{TUI_TEST_SERIES[1] + 1}.0"

type VersionKey = tuple[int, int, int, bool]


def _version_key(version: str) -> VersionKey | None:
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease is None


def _supported(key: VersionKey) -> bool:
    return key[:2] == TUI_TEST_SERIES and key >= (*TUI_TEST_MINIMUM, True)


def parse_tui_test_version(output: str) -> str | None:
    name, _, version = output.strip().partition(" ")
    return version if name == "tui-test" and version else None


def supported_tui_test(version: str) -> bool:
    key = _version_key(version)
    return key is not None and _supported(key)


def newest_supported_release(tags: Iterable[str]) -> str | None:
    releases = [
        (key, tag)
        for tag in tags
        if RELEASE_PATTERN.fullmatch(tag)
        and (key := _version_key(tag)) is not None
        and _supported(key)
    ]
    return max(releases)[1] if releases else None


def version_probe(executable: Path) -> CommandSpec:
    return CommandSpec(
        argv=(str(executable), "--version"),
        timeout_seconds=VERSION_PROBE_SECONDS,
        max_output_bytes=VERSION_PROBE_BYTES,
    )


def reported_version(result: CommandResult) -> str | None:
    if result.outcome != "exited" or result.returncode != 0:
        return None
    return parse_tui_test_version(result.stdout.decode(errors="replace"))


def install_script_url(version: str) -> str:
    if not RELEASE_PATTERN.fullmatch(version):
        raise ValueError(f"Not a tui-test release version: {version!r}")
    return f"https://raw.githubusercontent.com/microsoft/tui-test/{version}/install/install.sh"


def tui_test_install_command(version: str) -> str:
    return (
        f"curl --proto '=https' --tlsv1.2 -LsSf {install_script_url(version)}"
        f" | TUI_TEST_VERSION={version} sh"
    )


def manual_install(version: str) -> str:
    return (
        f"Install tui-test {version} ({TUI_TEST_SUPPORTED}) with:\n"
        f"  {tui_test_install_command(version)}\n"
        "then rerun dryheave init."
    )


TUI_TEST_TESTED_COMMAND = tui_test_install_command(TUI_TEST_TESTED_VERSION)
TUI_TEST_ACTION = (
    f"Install a supported tui-test ({TUI_TEST_SUPPORTED}): run dryheave init on a terminal "
    "(or with --install-tui-test) to install the newest release, or install the last tested "
    f"one yourself: {TUI_TEST_TESTED_COMMAND}"
)
