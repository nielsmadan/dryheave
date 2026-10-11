import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from pydantic import JsonValue

from dryheave.errors import InputError
from dryheave.filesystem import atomic_write, ensure_directory, read_bytes
from dryheave.profile_models import LiveConfigManifest
from dryheave.serialization import canonical_json, digest, parse_model

ENTRIES = (
    "settings.json",
    "CLAUDE.md",
    "keybindings.json",
    "skills",
    "agents",
    "commands",
    "output-styles",
    "hooks",
    "plugins",
)
REBASED_PLUGIN_FILES = ("installed_plugins.json", "known_marketplaces.json")
LIVE_CONFIG_DIRECTORY = "live-config"
STATE_FILE = "state.json"
MANIFEST_FILE = "manifest.json"
PRIVATE_STATE = re.compile(
    r"(?i)token|secret|password|credential|api.?key|oauth|account|user.?id|machine.?id|anonymous"
)
MAX_FILES = 50_000
MAX_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024


def live_source(environment: Mapping[str, str], home: Path) -> tuple[Path, Path]:
    configured = environment.get("CLAUDE_CONFIG_DIR")
    if configured:
        root = Path(configured).expanduser().absolute()
        return root, root / ".claude.json"
    return home / ".claude", home / ".claude.json"


def _json(path: Path) -> dict[str, JsonValue]:
    try:
        value = json.loads(read_bytes(path, limit=MAX_JSON_BYTES))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError) as error:
        raise InputError(f"Live Claude config file is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise InputError(f"Live Claude config file is not a JSON object: {path}")
    return value


def shareable_state(state: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        key: value
        for key, value in state.items()
        if key != "projects" and not PRIVATE_STATE.search(key)
    }


def _walk(top: Path) -> Iterator[Path]:
    if top.is_symlink() or not top.is_dir():
        yield top
        return
    for directory, names, files in os.walk(top):
        base = Path(directory)
        yield from (base / name for name in files)
        yield from (base / name for name in names if (base / name).is_symlink())


def _inventory(root: Path, entries: tuple[str, ...]) -> tuple[dict[str, str], int]:
    records: dict[str, str] = {}
    total = 0
    for path in (path for entry in entries for path in _walk(root / entry)):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records[relative] = "-> " + os.readlink(path)
        else:
            content = path.read_bytes()
            total += len(content)
            records[relative] = hashlib.sha256(content).hexdigest()
        if len(records) > MAX_FILES or total > MAX_BYTES:
            raise InputError("Live Claude config exceeds the snapshot bounds.")
    return records, total


def _manifest(
    source: Path,
    entries: tuple[str, ...],
    inventory: tuple[dict[str, str], int],
    state: Mapping[str, JsonValue],
) -> LiveConfigManifest:
    records, total = inventory
    settings = _json(source / "settings.json") if "settings.json" in entries else {}
    plugins = settings.get("enabledPlugins", {})
    hooks = settings.get("hooks", {})
    servers = state.get("mcpServers", {})
    skills = source / "skills"
    return LiveConfigManifest(
        source=str(source),
        entries=entries,
        files=sum(not value.startswith("-> ") for value in records.values()),
        bytes=total,
        tree_sha256=digest(
            "".join(f"{name}\0{value}\n" for name, value in sorted(records.items())).encode()
        ),
        settings_sha256=records.get("settings.json"),
        instructions_sha256=records.get("CLAUDE.md"),
        skills=tuple(sorted(path.name for path in skills.iterdir())) if skills.is_dir() else (),
        enabled_plugins=tuple(
            sorted(name for name, enabled in plugins.items() if enabled)
            if isinstance(plugins, dict)
            else ()
        ),
        mcp_servers=tuple(sorted(servers)) if isinstance(servers, dict) else (),
        hook_events=tuple(sorted(hooks)) if isinstance(hooks, dict) else (),
    )


def snapshot_live_config(source: Path, state_path: Path, destination: Path) -> LiveConfigManifest:
    if not source.is_dir():
        raise InputError(f"Live Claude config directory does not exist: {source}")
    entries = tuple(
        entry for entry in ENTRIES if (source / entry).exists() or (source / entry).is_symlink()
    )
    state = shareable_state(_json(state_path))
    manifest = _manifest(source, entries, _inventory(source, entries), state)
    staged = destination.with_name(destination.name + ".partial")
    if staged.exists():
        shutil.rmtree(staged)
    ensure_directory(staged)
    for entry in entries:
        origin = source / entry
        if origin.is_dir() and not origin.is_symlink():
            shutil.copytree(origin, staged / entry, symlinks=True)
        else:
            shutil.copy2(origin, staged / entry, follow_symlinks=False)
    atomic_write(staged / STATE_FILE, canonical_json(state), replace=False)
    atomic_write(staged / MANIFEST_FILE, canonical_json(manifest), replace=False)
    staged.rename(destination)
    return manifest


def load_snapshot_manifest(snapshot: Path) -> LiveConfigManifest:
    return parse_model(
        read_bytes(snapshot / MANIFEST_FILE, limit=MAX_JSON_BYTES), LiveConfigManifest
    )


def _rebase(value: JsonValue, old: str, new: str) -> JsonValue:
    if isinstance(value, str):
        return new + value[len(old) :] if value == old or value.startswith(old + "/") else value
    if isinstance(value, list):
        return [_rebase(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _rebase(item, old, new) for key, item in value.items()}
    return value


def require_servers(available: Iterable[str], removed: Iterable[str]) -> None:
    missing = sorted(set(removed) - set(available))
    if missing:
        raise InputError(
            "Removed MCP servers are not user-scoped in the live config: " + ", ".join(missing)
        )


def subject_state(
    state: Mapping[str, JsonValue], removed: tuple[str, ...], workspace: Path | None
) -> dict[str, JsonValue]:
    servers = state.get("mcpServers", {})
    servers = servers if isinstance(servers, dict) else {}
    require_servers(servers, removed)
    result = dict(state)
    if servers or removed:
        result["mcpServers"] = {name: item for name, item in servers.items() if name not in removed}
    if workspace is not None:
        result["projects"] = {str(workspace): {"hasTrustDialogAccepted": True}}
    return result


def materialize_live_config(
    snapshot: Path,
    config_root: Path,
    workspace: Path,
    *,
    removed_mcp_servers: tuple[str, ...],
    trusted: bool,
) -> LiveConfigManifest:
    manifest = load_snapshot_manifest(snapshot)
    state = subject_state(
        _json(snapshot / STATE_FILE), removed_mcp_servers, workspace if trusted else None
    )
    for entry in manifest.entries:
        origin = snapshot / entry
        if origin.is_dir() and not origin.is_symlink():
            shutil.copytree(origin, config_root / entry, symlinks=True)
        else:
            shutil.copy2(origin, config_root / entry, follow_symlinks=False)
    for name in REBASED_PLUGIN_FILES:
        path = config_root / "plugins" / name
        if path.is_file() and not path.is_symlink():
            rebased = _rebase(_json(path), manifest.source, str(config_root))
            atomic_write(path, json.dumps(rebased, indent=2).encode())
    atomic_write(config_root / ".claude.json", canonical_json(state), replace=False)
    os.chmod(config_root / ".claude.json", 0o600)
    return manifest
