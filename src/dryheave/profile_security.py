import re
import tomllib
from collections.abc import Mapping
from pathlib import PurePosixPath

from dryheave.errors import InputError
from dryheave.serialization import parse_json

SENSITIVE_PARTS = frozenset(
    {
        ".netrc",
        "id_rsa",
        "id_ed25519",
        "auth.json",
        ".credentials.json",
        "credentials.json",
        "credentials",
        ".claude.json",
        "sessions",
        "history",
        "history.jsonl",
        "history.sqlite",
        "trust",
        "trusted-configs",
        "keychains",
        ".ssh",
        ".git",
        ".cache",
        "cache",
        "logs",
        "projects",
        "state.db",
    }
)
SECRET_FIELDS = frozenset(
    {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "clientsecret",
        "password",
        "passwd",
        "authorization",
        "bearertoken",
        "authtoken",
        "token",
        "secret",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "awssessiontoken",
        "privatekey",
    }
)
SECRET_PATTERN = re.compile(
    rb"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
    rb"github_pat_[A-Za-z0-9_]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"Bearer[ \t]+[A-Za-z0-9._~+/-]{8,}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.)",
    re.IGNORECASE,
)
ASSIGNMENT = re.compile(rb"""["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]""")


def credential_field(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized in SECRET_FIELDS or normalized.endswith(
        (
            "apikey",
            "accesstoken",
            "refreshtoken",
            "clientsecret",
            "password",
            "authorization",
            "authtoken",
        )
    )


def reject_sensitive_path(path: str) -> None:
    for part in PurePosixPath(path).parts:
        lowered = part.lower()
        if lowered in SENSITIVE_PARTS or lowered.startswith(
            (".env", "auth.json.", "history.", "state_")
        ):
            raise InputError(
                "Selected asset includes a credential, history, cache or trust path; select individual safe inputs."
            )


def reject_secret_bytes(content: bytes, *, location: str) -> None:
    if SECRET_PATTERN.search(content):
        raise InputError(f"Recognized credential material in {location}; use a runtime reference.")
    for match in ASSIGNMENT.finditer(content):
        if credential_field(match[1].decode("ascii")):
            raise InputError(f"Recognized credential field in {location}; use a runtime reference.")


def reject_secret_fields(value: object, *, location: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and credential_field(key):
                raise InputError(
                    f"Recognized credential field in {location}; use a runtime reference."
                )
            reject_secret_fields(item, location=location)
    elif isinstance(value, (tuple, list)):
        for item in value:
            reject_secret_fields(item, location=location)
    elif isinstance(value, str):
        reject_secret_bytes(value.encode(), location=location)


def validate_arguments(arguments: tuple[str, ...]) -> None:
    for index, argument in enumerate(arguments):
        if re.search(
            r"--(?:api[-_]?key|access[-_]?token|refresh[-_]?token|password|client[-_]?secret)(?:\s|=|$)",
            argument,
            re.IGNORECASE,
        ):
            raise InputError(f"Credential-bearing argv entry {index}; use a runtime reference.")
        name = argument.split("=", maxsplit=1)[0].lstrip("-")
        if credential_field(name):
            raise InputError(f"Credential-bearing argv entry {index}; use a runtime reference.")
        reject_secret_bytes(argument.encode(), location=f"argv entry {index}")


def inspect_config(content: bytes, target: str) -> dict[str, object]:
    reject_secret_bytes(content, location=f"selected asset {target}")
    try:
        if target.endswith(".toml"):
            parsed: dict[str, object] = tomllib.loads(content.decode())
        elif target.endswith(".json"):
            parsed = dict(parse_json(content))
        else:
            raise InputError("Configuration assets require a JSON or TOML target.")
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise InputError(f"Invalid config syntax in selected asset {target}.") from error
    reject_secret_fields(parsed, location=f"selected asset {target}")
    reject_trust_fields(parsed)
    return parsed


def reject_trust_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and re.sub(r"[^a-z]", "", key.lower()) in {
                "trustlevel",
                "hastrustdialogaccepted",
                "hooktrust",
            }:
                raise InputError(
                    "Selected config contains persisted trust state; select a config without trust-store fields."
                )
            reject_trust_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_trust_fields(item)
