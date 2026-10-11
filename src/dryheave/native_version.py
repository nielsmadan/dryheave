import re

VERSION_OUTPUT = re.compile(
    rb"(?:codex-cli )?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)(?: \(Claude Code\))?\s*"
)


def parse_version(output: bytes) -> str | None:
    match = VERSION_OUTPUT.fullmatch(output)
    return match[1].decode("ascii") if match else None
