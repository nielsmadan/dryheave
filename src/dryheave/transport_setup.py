import hashlib
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from dryheave.errors import InputError
from dryheave.models import CommandSpec
from dryheave.processes import CommandResult, run_command
from dryheave.tui_test_release import (
    TUI_TEST_INSTALL_SCRIPT_SHA256,
    TUI_TEST_SUPPORTED,
    TUI_TEST_TESTED_VERSION,
    install_script_url,
    manual_install,
    newest_supported_release,
    reported_version,
    supported_tui_test,
    tui_test_install_command,
    version_probe,
)

RELEASES_URL = "https://api.github.com/repos/microsoft/tui-test/releases?per_page=30"
RELEASES_SECONDS = 15
RELEASES_CURL_SECONDS = 10
CONNECT_SECONDS = 5
RELEASES_BYTES = 4 * 1024 * 1024
SCRIPT_SECONDS = 60
SCRIPT_CURL_SECONDS = 55
SCRIPT_BYTES = 64 * 1024
INSTALL_SECONDS = 300
INSTALL_OUTPUT_BYTES = 64 * 1024
INSTALLED_PATTERN = re.compile(r"^Installed tui-test to (.+)$", re.MULTILINE)
INSTALL_UNSET = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "PREFIX"})

type Lookup = Literal["found", "failed", "empty"]


class GithubRelease(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    tag_name: str
    draft: bool
    prerelease: bool


RELEASES = TypeAdapter(list[GithubRelease])


@dataclass(frozen=True)
class InstallTarget:
    version: str
    lookup: Lookup

    @property
    def command(self) -> str:
        return tui_test_install_command(self.version)

    @property
    def verified(self) -> bool:
        return self.version in TUI_TEST_INSTALL_SCRIPT_SHA256

    @property
    def description(self) -> str:
        if self.lookup == "failed":
            return (
                "Could not check GitHub for tui-test releases; "
                f"{self.version} is the last version tested with dryheave."
            )
        if self.lookup == "empty":
            return (
                f"GitHub lists no supported tui-test release ({TUI_TEST_SUPPORTED}); "
                f"{self.version} is the last version tested with dryheave."
            )
        newest = f"tui-test {self.version} is the newest supported release ({TUI_TEST_SUPPORTED})."
        if self.verified:
            return newest
        return (
            f"{newest} dryheave was last tested with {TUI_TEST_TESTED_VERSION}, and this "
            "release's install script has no checksum on record."
        )


class MissingTransportError(InputError):
    code = "missing_transport"


def releases_lookup() -> CommandSpec:
    return CommandSpec(
        argv=(
            "curl",
            "--proto",
            "=https",
            "--tlsv1.2",
            "-fsSL",
            "--connect-timeout",
            str(CONNECT_SECONDS),
            "--max-time",
            str(RELEASES_CURL_SECONDS),
            "-H",
            "Accept: application/vnd.github+json",
            RELEASES_URL,
        ),
        timeout_seconds=RELEASES_SECONDS,
        max_output_bytes=RELEASES_BYTES,
    )


def install_target_from(result: CommandResult) -> InstallTarget:
    if result.outcome != "exited" or result.returncode != 0:
        return InstallTarget(TUI_TEST_TESTED_VERSION, "failed")
    try:
        releases = RELEASES.validate_json(result.stdout)
    except ValidationError:
        return InstallTarget(TUI_TEST_TESTED_VERSION, "failed")
    newest = newest_supported_release(
        release.tag_name for release in releases if not release.draft and not release.prerelease
    )
    return (
        InstallTarget(newest, "found")
        if newest is not None
        else InstallTarget(TUI_TEST_TESTED_VERSION, "empty")
    )


def fetch_install_target(root: Path) -> InstallTarget:
    try:
        return install_target_from(run_command(releases_lookup(), root))
    except OSError:
        return InstallTarget(TUI_TEST_TESTED_VERSION, "failed")


def missing_transport_error(target: InstallTarget) -> MissingTransportError:
    return MissingTransportError(f"tui-test is not installed. {manual_install(target.version)}")


def usable_transport(executable: Path, root: Path) -> bool:
    try:
        version = reported_version(run_command(version_probe(executable), root))
    except OSError:
        return False
    return version is not None and supported_tui_test(version)


def installer_environment(environment: Mapping[str, str], version: str) -> dict[str, str]:
    kept = {name: value for name, value in environment.items() if name not in INSTALL_UNSET}
    return kept | {"TUI_TEST_VERSION": version, "TUI_TEST_NO_MODIFY_PATH": "1"}


def installed_path(result: CommandResult, cwd: Path) -> Path | None:
    printed = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    installed = INSTALLED_PATTERN.search(printed)
    if result.outcome != "exited" or result.returncode != 0 or installed is None:
        return None
    return cwd / installed.group(1).strip()


def _failed(step: str, target: InstallTarget) -> MissingTransportError:
    return MissingTransportError(f"{step} {manual_install(target.version)}")


def _download_script(target: InstallTarget, directory: Path, output: TextIO) -> Path:
    script = directory / "install.sh"
    print(f"Downloading the tui-test {target.version} install script…", file=output, flush=True)
    result = run_command(
        CommandSpec(
            argv=(
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "-fsSL",
                "--connect-timeout",
                str(CONNECT_SECONDS),
                "--max-time",
                str(SCRIPT_CURL_SECONDS),
                "-o",
                str(script),
                install_script_url(target.version),
            ),
            timeout_seconds=SCRIPT_SECONDS,
            max_output_bytes=SCRIPT_BYTES,
        ),
        directory,
    )
    if result.outcome != "exited" or result.returncode != 0 or not script.is_file():
        print(result.stderr.decode(errors="replace"), end="", file=output)
        raise _failed(
            f"Downloading the tui-test install script failed ({result.outcome}, exit "
            f"{result.returncode}).",
            target,
        )
    expected = TUI_TEST_INSTALL_SCRIPT_SHA256.get(target.version)
    if expected is not None and hashlib.sha256(script.read_bytes()).hexdigest() != expected:
        raise _failed(
            f"The tui-test {target.version} install script does not match its recorded checksum.",
            target,
        )
    return script


def install_tui_test(
    target: InstallTarget, *, environment: Mapping[str, str], output: TextIO
) -> Path:
    with tempfile.TemporaryDirectory(prefix="dryheave-tui-test-") as temporary:
        directory = Path(temporary)
        script = _download_script(target, directory, output)
        print(
            f"Installing tui-test {target.version}; this downloads a release archive and can "
            "take a minute…",
            file=output,
            flush=True,
        )
        result = run_command(
            CommandSpec(
                argv=("sh", str(script)),
                timeout_seconds=INSTALL_SECONDS,
                max_output_bytes=INSTALL_OUTPUT_BYTES,
            ),
            directory,
            environment=installer_environment(environment, target.version),
        )
        print(
            result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"),
            end="",
            file=output,
        )
        path = installed_path(result, directory)
    if path is None:
        raise _failed(
            f"The tui-test install did not finish ({result.outcome}, exit {result.returncode}).",
            target,
        )
    try:
        installed = reported_version(run_command(version_probe(path), path.parent))
    except OSError as error:
        raise _failed(f"The installed {path} could not run: {error}.", target) from error
    if installed is None or not supported_tui_test(installed):
        raise _failed(f"The installed {path} is not a supported tui-test.", target)
    return path


def path_to_record(installed: Path, on_path: Path | None, recorded: str | None) -> Path | None:
    return installed if recorded is not None or on_path != installed else None
