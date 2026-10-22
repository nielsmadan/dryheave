import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Literal

from dryheave.models import EFFORTS

CODEX_VERIFIED_VERSION = "0.154.0"
CODEX_DEFAULT_VERSION = CODEX_VERIFIED_VERSION
CODEX_PROFILE_VERSIONS = (CODEX_VERIFIED_VERSION, "0.162.1")
CODEX_PROFILE_VERSION_TEXT = " or ".join(CODEX_PROFILE_VERSIONS)
CODEX_CONTROLLER_VERSION = CODEX_VERIFIED_VERSION
CODEX_LEGACY_CONTROLLER_VERSION = "0.153.4"
CODEX_CONTROLLER_VERSIONS = (CODEX_LEGACY_CONTROLLER_VERSION, CODEX_CONTROLLER_VERSION)
CODEX_HOME_VARIABLE = "CODEX_HOME"
CODEX_HOME_DIRECTORY = ".codex"
CODEX_AUTH_VARIABLE = "CODEX_AUTH_FILE"
CODEX_FULL_ACCESS = "danger-full-access"
CODEX_NO_APPROVALS = "never"
CODEX_SANDBOX_ARGUMENTS = ("--sandbox", CODEX_FULL_ACCESS)
CODEX_BYPASS_ARGUMENTS = frozenset({"--dangerously-bypass-approvals-and-sandbox", "--yolo"})
CODEX_APPROVAL_FLAGS = frozenset({"-a", "--ask-for-approval"})
NEVER_APPROVAL_OVERRIDE = re.compile(r"\bapproval_policy\s*=\s*[\"']?never[\"']?\s*$")

Bypass = Literal["sandbox", "approvals"]


def validate_codex_effort(effort: str | None) -> None:
    if effort is not None and effort not in EFFORTS:
        raise ValueError("Codex effort must be one of " + ", ".join(EFFORTS))


def _never_approvals(arguments: Sequence[str]) -> bool:
    return any(
        flag in CODEX_APPROVAL_FLAGS and value == CODEX_NO_APPROVALS
        for flag, value in pairwise(arguments)
    ) or any(
        argument == "--ask-for-approval=" + CODEX_NO_APPROVALS
        or NEVER_APPROVAL_OVERRIDE.search(argument) is not None
        for argument in arguments
    )


def _bypasses(sandbox: bool, approvals: bool) -> frozenset[Bypass]:
    found: tuple[tuple[Bypass, bool], ...] = (("sandbox", sandbox), ("approvals", approvals))
    return frozenset(name for name, present in found if present)


def argument_bypasses(arguments: Sequence[str]) -> frozenset[Bypass]:
    bypass = any(argument in CODEX_BYPASS_ARGUMENTS for argument in arguments)
    return _bypasses(
        bypass or any(CODEX_FULL_ACCESS in argument for argument in arguments),
        bypass or _never_approvals(arguments),
    )


def _effective(config: Mapping[str, object], key: str) -> object:
    profiles, selected = config.get("profiles"), config.get("profile")
    profile = profiles.get(selected) if isinstance(profiles, dict) else None
    if isinstance(profile, dict) and key in profile:
        return profile[key]
    return config.get(key)


def config_bypasses(config: Mapping[str, object]) -> frozenset[Bypass]:
    return _bypasses(
        _effective(config, "sandbox_mode") == CODEX_FULL_ACCESS,
        _effective(config, "approval_policy") == CODEX_NO_APPROVALS,
    )
