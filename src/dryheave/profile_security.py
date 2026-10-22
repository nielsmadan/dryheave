import re
import tomllib
from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import urlsplit

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
SECRET_DIRECTORIES = (
    (".ssh",),
    (".aws",),
    (".gnupg",),
    (".config", "gcloud"),
    (".docker",),
    (".kube",),
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
    rb"(?:(?<![A-Za-z0-9_-])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|"
    rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{16,}|"
    rb"github_pat_[A-Za-z0-9_]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"Bearer[ \t]+(?=[A-Za-z._~+/-]*[0-9])[A-Za-z0-9._~+/-]{8,}|"
    rb"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.)",
    re.IGNORECASE,
)
PROSE_KEY_PATTERN = re.compile(
    rb"(?:(?<![A-Za-z0-9_-])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}|"
    rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{36,}|"
    rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{80,}|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{64,}|"
    rb"Bearer[ \t]+(?=[A-Za-z._~+/-]*[0-9])[A-Za-z0-9._~+/-]{20,}|"
    rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,})",
    re.IGNORECASE,
)
CREDENTIAL_FILE = re.compile(
    r"\.env(?:\.(?!(?:example|sample|template)$).+)?|.+\.pem|id_(?:rsa|dsa|ecdsa|ed25519).*|"
    r"\.?credentials.*\.json|auth\.json(?:\..+)?|\.netrc|credentials|\.git-credentials|"
    r"application_default_credentials\.json|\.pgpass|\.npmrc|\.pypirc",
    re.IGNORECASE,
)
CODEX_TOKEN_FIELDS = frozenset({"experimental_bearer_token", "bearer_token"})
TOKEN_HEADER = re.compile(r"(?i)token|secret|key|auth|password|cookie")
ASSIGNMENT = re.compile(rb"""["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]""")
LITERAL = re.compile(rb"""[ \t]*["']?[^\s"'$<{%=:,;)\]}]""")
ENV_REFERENCES = frozenset({"env_http_headers", "env_vars", "env_key"})
SECRET_NAME_WORDS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "apikey",
        "key",
        "auth",
        "pat",
        "credential",
        "credentials",
        "cookie",
    }
)


def _credential_field(name: str) -> bool:
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


def _env_reference(name: str) -> bool:
    return name in ENV_REFERENCES or name.endswith("_env_var")


def sensitive_path(path: str) -> bool:
    return any(
        part.lower() in SENSITIVE_PARTS
        or part.lower().startswith((".env", "auth.json.", "history.", "state_"))
        for part in PurePosixPath(path).parts
    )


def credential_file(name: str) -> bool:
    return CREDENTIAL_FILE.fullmatch(name) is not None


def secret_location(path: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(path).parts)
    return any(
        parts[index : index + len(directory)] == directory
        for directory in SECRET_DIRECTORIES
        for index in range(len(parts))
    )


def reject_sensitive_path(path: str) -> None:
    if sensitive_path(path):
        raise InputError(
            "Selected asset includes a credential, history, cache or trust path; select individual safe inputs."
        )


def _line(content: bytes, offset: int) -> int:
    return content.count(b"\n", 0, offset) + 1


def _literal(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return (isinstance(value, str) and bool(value)) or isinstance(value, int | float)


def _query_field(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not (parts.scheme and parts.netloc and parts.query):
        return None
    pairs = (pair.partition("=") for pair in parts.query.split("&"))
    return next(
        (
            name
            for name, _, item in pairs
            if _credential_field(name) and item and not item.startswith("$")
        ),
        None,
    )


def _string_credential(value: str, path: tuple[str, ...]) -> str | None:
    where = ".".join(path)
    if SECRET_PATTERN.search(value.encode()):
        return f"material in field {where}" if where else "material"
    query = _query_field(value)
    return None if query is None else f"field {where}?{query}"


def _credential_location(value: object, path: tuple[str, ...] = ()) -> str | None:
    if isinstance(value, Mapping):
        items = [(str(key), item) for key, item in value.items() if not _env_reference(str(key))]
        named = next(
            (key for key, item in items if _credential_field(key) and _literal(item)), None
        )
        if named is not None:
            return "field " + ".".join((*path, named))
        children = [(item, (*path, key)) for key, item in items]
    elif isinstance(value, list | tuple):
        children = [(item, path) for item in value]
    elif isinstance(value, str):
        return _string_credential(value, path)
    else:
        return None
    return next(
        (
            found
            for item, where in children
            if (found := _credential_location(item, where)) is not None
        ),
        None,
    )


def _material(content: bytes) -> str | None:
    match = SECRET_PATTERN.search(content)
    return None if match is None else f"material on line {_line(content, match.start())}"


def credential_in_bytes(content: bytes) -> str | None:
    material = _material(content)
    if material is not None:
        return material
    assignment = next(
        (
            match
            for match in ASSIGNMENT.finditer(content)
            if _credential_field(match[1].decode("ascii")) and LITERAL.match(content, match.end())
        ),
        None,
    )
    if assignment is None:
        return None
    return f"field {assignment[1].decode('ascii')} on line {_line(content, assignment.start())}"


def credential_in_config(content: bytes, value: object) -> str | None:
    return _material(content) or _credential_location(value)


def credential_in_prose(content: bytes) -> str | None:
    match = PROSE_KEY_PATTERN.search(content)
    return None if match is None else f"material on line {_line(content, match.start())}"


def _key_names(value: object) -> set[str]:
    if isinstance(value, Mapping):
        items = [(str(key), item) for key, item in value.items()]
        return {key for key, _ in items}.union(*(_key_names(item) for _, item in items))
    if isinstance(value, list | tuple):
        return set[str]().union(*(_key_names(item) for item in value))
    return set()


def _material_outside_keys(content: bytes, value: object) -> str | None:
    names = _key_names(value)
    match = next(
        (
            found
            for found in SECRET_PATTERN.finditer(content)
            if not any(found[0].decode() in name for name in names)
        ),
        None,
    )
    return None if match is None else f"material on line {_line(content, match.start())}"


def _secret_name(name: str) -> bool:
    words = [word for word in re.split(r"[^a-z0-9]+", name.lower()) if word]
    return bool(words) and words[-1] in SECRET_NAME_WORDS


def _environment_table(path: tuple[str, ...]) -> bool:
    return path[-1:] == ("env",) or path[-2:] == ("shell_environment_policy", "set")


def _codex_token(path: tuple[str, ...], key: str, item: object) -> bool:
    if not _literal(item):
        return False
    if _environment_table(path):
        return _secret_name(key) and not (isinstance(item, str) and item.startswith("$"))
    return key.lower() in CODEX_TOKEN_FIELDS or (
        path[-1:] == ("http_headers",) and TOKEN_HEADER.search(key) is not None
    )


def _url_password(value: str) -> bool:
    try:
        password = urlsplit(value).password
    except ValueError:
        return False
    return password is not None and password != "" and not password.startswith("$")


def _codex_token_location(value: object, path: tuple[str, ...] = ()) -> str | None:
    if isinstance(value, str):
        if not _url_password(value):
            return None
        return "URL password in field " + ".".join(path) if path else "URL password"
    if isinstance(value, Mapping):
        items = [(str(key), item) for key, item in value.items()]
        named = next((key for key, item in items if _codex_token(path, key, item)), None)
        if named is not None:
            return "field " + ".".join((*path, named))
        children = [(item, (*path, key)) for key, item in items]
    elif isinstance(value, list | tuple):
        children = [(item, path) for item in value]
    else:
        return None
    return next(
        (
            found
            for item, where in children
            if (found := _codex_token_location(item, where)) is not None
        ),
        None,
    )


def credential_in_live_config(content: bytes, value: object) -> str | None:
    return (
        _material_outside_keys(content, value)
        or _credential_location(value)
        or _codex_token_location(value)
    )


def _refuse(finding: str | None, location: str) -> None:
    if finding is not None:
        raise InputError(f"Recognized credential {finding} in {location}; use a runtime reference.")


def reject_secret_bytes(content: bytes, *, location: str) -> None:
    _refuse(credential_in_bytes(content), location)


def reject_config_secrets(content: bytes, value: object, *, location: str) -> None:
    _refuse(credential_in_config(content, value), location)


def reject_prose_secrets(content: bytes, *, location: str) -> None:
    _refuse(credential_in_prose(content), location)


def reject_live_config_secrets(content: bytes, value: object, *, location: str) -> None:
    _refuse(credential_in_live_config(content, value), location)


def reject_secret_fields(value: object, *, location: str) -> None:
    _refuse(_credential_location(value), location)


def reject_codex_config_secrets(value: object, *, location: str) -> None:
    _refuse(_codex_token_location(value), location)


def validate_arguments(arguments: tuple[str, ...]) -> None:
    for index, argument in enumerate(arguments):
        if re.search(
            r"--(?:api[-_]?key|access[-_]?token|refresh[-_]?token|password|client[-_]?secret)(?:\s|=|$)",
            argument,
            re.IGNORECASE,
        ):
            raise InputError(f"Credential-bearing argv entry {index}; use a runtime reference.")
        name = argument.split("=", maxsplit=1)[0].lstrip("-")
        if _credential_field(name):
            raise InputError(f"Credential-bearing argv entry {index}; use a runtime reference.")
        reject_secret_bytes(argument.encode(), location=f"argv entry {index}")


def inspect_config(content: bytes, target: str, *, location: str) -> dict[str, object]:
    try:
        if target.endswith(".toml"):
            parsed: dict[str, object] = tomllib.loads(content.decode())
        elif target.endswith(".json"):
            parsed = dict(parse_json(content))
        else:
            raise InputError("Configuration assets require a JSON or TOML target.")
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        reject_secret_bytes(content, location=location)
        raise InputError(f"Invalid config syntax in selected asset {target}.") from error
    reject_config_secrets(content, parsed, location=location)
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
