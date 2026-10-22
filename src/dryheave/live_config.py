import json
import os
import re
import shutil
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from pydantic import JsonValue

from dryheave.codex_policy import CODEX_HOME_DIRECTORY, CODEX_HOME_VARIABLE
from dryheave.errors import InputError, SnapshotChangedError
from dryheave.filesystem import atomic_write, configured_directory, ensure_directory, read_bytes
from dryheave.models import AgentKind, JournalEvent
from dryheave.profile_models import LiveConfigManifest, NativeRecipe
from dryheave.profile_security import (
    SENSITIVE_PARTS,
    credential_file,
    reject_live_config_secrets,
    reject_prose_secrets,
    reject_secret_bytes,
    secret_location,
    sensitive_path,
)
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.tree_copy import TreeLimits, TreePolicy, copy_tree

CLAUDE_ENTRIES = (
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
CODEX_ENTRIES = ("config.toml", "AGENTS.md", "skills", "rules", "prompts", "hooks", "hooks.json")
CODEX_PROSE = frozenset({"AGENTS.md", "skills", "prompts"})
REBASED_PLUGIN_FILES = ("plugins/installed_plugins.json", "plugins/known_marketplaces.json")
LIVE_CONFIG_DIRECTORY = "live-config"
STATE_FILE = "state.json"
MANIFEST_FILE = "manifest.json"
BUNDLED_SKILLS = ".system"
SKILL_FILE = "SKILL.md"
SKILL_DEPTH = 6
SKILL_HEADER_BYTES = 64 * 1024
SKIPPED_DEPENDENCIES = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)
PRIVATE_STATE = re.compile(
    r"(?i)token|secret|password|credential|api.?key|oauth|account|user.?id|machine.?id|anonymous"
)
MAX_FILES = 50_000
MAX_DIRECTORIES = 50_000
MAX_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024


class LiveSource(NamedTuple):
    agent: AgentKind
    root: Path
    state: Path | None


class LiveDetails(NamedTuple):
    skills: tuple[str, ...]
    enabled_plugins: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    hook_events: tuple[str, ...]


class LiveLayout(NamedTuple):
    agent: AgentKind
    label: str
    variable: str
    directory: str
    snapshot: str
    entries: tuple[str, ...]
    settings: str
    instructions: str
    state_file: str | None
    rebased: tuple[str, ...]
    policy: TreePolicy
    details: Callable[[Path, Mapping[str, object]], LiveDetails]
    post_copy: Callable[[Path, LiveConfigManifest], None] | None


def _parse_json(content: bytes, path: Path) -> dict[str, JsonValue]:
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise InputError(f"Live Claude config file is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise InputError(f"Live Claude config file is not a JSON object: {path}")
    return value


def _json(path: Path) -> dict[str, JsonValue]:
    try:
        content = read_bytes(path, limit=MAX_JSON_BYTES)
    except FileNotFoundError:
        return {}
    return _parse_json(content, path)


def _parse_toml(content: bytes, path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise InputError(f"Live Codex config file is not valid TOML: {path}") from error


def _parse_hooks(content: bytes, path: Path) -> object:
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise InputError(f"Live Codex hooks file is not valid JSON: {path}") from error


CODEX_STRUCTURED: Mapping[str, Callable[[bytes, Path], object]] = MappingProxyType(
    {"config.toml": _parse_toml, "hooks.json": _parse_hooks}
)


def _prose(relative: str) -> bool:
    return relative.partition("/")[0] in CODEX_PROSE


def _screen_codex(relative: str, content: bytes) -> object:
    location = f"live Codex config file {relative}"
    parse = CODEX_STRUCTURED.get(relative)
    if parse is not None:
        value = parse(content, Path(relative))
        reject_live_config_secrets(content, value, location=location)
        return value
    if _prose(relative):
        reject_prose_secrets(content, location=location)
    else:
        reject_secret_bytes(content, location=location)
    return None


def _admit_codex(relative: str, directory: bool) -> bool:
    name = relative.rpartition("/")[2]
    if name.lower() == ".git" or (_prose(relative) and name in SKIPPED_DEPENDENCIES):
        return False
    if _prose(relative) and credential_file(name):
        if directory:
            raise InputError(
                f"Live config directory {relative} is named like a credential file; "
                "refusing to snapshot it."
            )
        raise InputError(
            f"Live config path {relative} is a credential file; refusing to snapshot it."
        )
    if not _prose(relative) and sensitive_path(relative):
        raise InputError(
            f"Live config path {relative} is a credential, history, cache or trust "
            "path; refusing to snapshot it."
        )
    return True


def _codex_link_target(relative: str, real: str) -> None:
    name = real.rpartition("/")[2]
    if secret_location(real) or credential_file(name) or name.lower() in SENSITIVE_PARTS:
        raise InputError(
            f"Live config link {relative} resolves to a credential or secret path; "
            "refusing to snapshot it."
        )


def _keys(value: object) -> tuple[str, ...]:
    return tuple(sorted(value)) if isinstance(value, dict) else ()


def _claude_details(snapshot: Path, _parsed: Mapping[str, object]) -> LiveDetails:
    settings = _json(snapshot / "settings.json")
    plugins = settings.get("enabledPlugins", {})
    skills = snapshot / "skills"
    return LiveDetails(
        skills=tuple(sorted(path.name for path in skills.iterdir())) if skills.is_dir() else (),
        enabled_plugins=tuple(
            sorted(name for name, enabled in plugins.items() if enabled)
            if isinstance(plugins, dict)
            else ()
        ),
        mcp_servers=_keys(_json(snapshot / STATE_FILE).get("mcpServers", {})),
        hook_events=_keys(settings.get("hooks", {})),
    )


def _skill_name(directory: Path) -> str:
    try:
        with (directory / SKILL_FILE).open("rb") as stream:
            header = stream.read(SKILL_HEADER_BYTES).decode(errors="replace")
    except OSError:
        return directory.name
    lines = header.splitlines()
    if not lines or lines[0].strip() != "---":
        return directory.name
    frontmatter = next(
        (lines[1:index] for index in range(1, len(lines)) if lines[index].strip() == "---"), []
    )
    named = next(
        (
            match[1].strip().strip("\"'")
            for line in frontmatter
            if (match := re.fullmatch(r"name:(.*)", line.rstrip())) is not None
        ),
        "",
    )
    return named or directory.name


def _skill_directories(root: Path) -> list[Path]:
    found: list[Path] = []
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth >= SKILL_DEPTH or not directory.is_dir():
            continue
        children = [
            child
            for child in sorted(directory.iterdir())
            if not child.name.startswith(".") and child.is_dir()
        ]
        found.extend(child for child in children if (child / SKILL_FILE).is_file())
        pending.extend((child, depth + 1) for child in children)
    return found


def codex_skills(root: Path) -> tuple[str, ...]:
    skills = root / "skills"
    directories = _skill_directories(skills) + _skill_directories(skills / BUNDLED_SKILLS)
    return tuple(sorted({_skill_name(directory) for directory in directories}))


def _hook_events(value: object) -> tuple[str, ...]:
    return _keys(value.get("hooks", value)) if isinstance(value, dict) else ()


def _codex_details(snapshot: Path, parsed: Mapping[str, object]) -> LiveDetails:
    config = parsed.get("config.toml")
    config = config if isinstance(config, dict) else {}
    plugins = config.get("plugins", {})
    return LiveDetails(
        skills=codex_skills(snapshot),
        enabled_plugins=tuple(
            sorted(
                name
                for name, item in plugins.items()
                if isinstance(item, dict) and item.get("enabled") is True
            )
            if isinstance(plugins, dict)
            else ()
        ),
        mcp_servers=_keys(config.get("mcp_servers", {})),
        hook_events=_hook_events(parsed.get("hooks.json")),
    )


def _hook_trust(path: Path) -> dict[str, object]:
    try:
        content = read_bytes(path, limit=MAX_JSON_BYTES)
    except FileNotFoundError:
        return {}
    hooks = _parse_toml(content, path).get("hooks", {})
    state = hooks.get("state", {}) if isinstance(hooks, dict) else {}
    return state if isinstance(state, dict) else {}


def _moved_key(key: str, sources: tuple[str, ...], new: str) -> str | None:
    old = next((source for source in sources if key.startswith(source + "/")), None)
    return None if old is None else new + key[len(old) :]


def _rekey_hook_trust(path: Path, sources: tuple[str, ...], new: str) -> None:
    trusted = _hook_trust(path)
    moved = {
        key: rekeyed for key in trusted if (rekeyed := _moved_key(key, sources, new)) is not None
    }
    if not moved:
        return
    text = read_bytes(path, limit=MAX_JSON_BYTES).decode()
    for key, rekeyed in moved.items():
        text = text.replace(
            f"[hooks.state.{json.dumps(key, ensure_ascii=False)}]",
            f"[hooks.state.{json.dumps(rekeyed, ensure_ascii=False)}]",
        )
    atomic_write(path, text.encode())
    if _hook_trust(path) != {moved.get(key, key): value for key, value in trusted.items()}:
        raise InputError(
            f"Live Codex config {path.name} records hook trust in a form the snapshot cannot "
            'move to its copied path; write each [hooks.state."..."] entry as its own table.'
        )


def _move_hook_trust(config_root: Path, manifest: LiveConfigManifest) -> None:
    sources = tuple(dict.fromkeys((manifest.source, os.path.realpath(manifest.source))))
    _rekey_hook_trust(
        config_root / live_layout(manifest.agent).settings,
        sources,
        os.path.realpath(config_root),
    )


_LAYOUTS: Mapping[AgentKind, LiveLayout] = MappingProxyType(
    {
        layout.agent: layout
        for layout in (
            LiveLayout(
                agent=AgentKind.CLAUDE,
                label="Claude",
                variable="CLAUDE_CONFIG_DIR",
                directory=".claude",
                snapshot=LIVE_CONFIG_DIRECTORY,
                entries=CLAUDE_ENTRIES,
                settings="settings.json",
                instructions="CLAUDE.md",
                state_file=".claude.json",
                rebased=REBASED_PLUGIN_FILES,
                policy=TreePolicy(dereference=False),
                details=_claude_details,
                post_copy=None,
            ),
            LiveLayout(
                agent=AgentKind.CODEX,
                label="Codex",
                variable=CODEX_HOME_VARIABLE,
                directory=CODEX_HOME_DIRECTORY,
                snapshot=f"{LIVE_CONFIG_DIRECTORY}-codex",
                entries=CODEX_ENTRIES,
                settings="config.toml",
                instructions="AGENTS.md",
                state_file=None,
                rebased=(),
                policy=TreePolicy(
                    dereference=True,
                    screen=_screen_codex,
                    admit=_admit_codex,
                    link_target=_codex_link_target,
                ),
                details=_codex_details,
                post_copy=_move_hook_trust,
            ),
        )
    }
)


def live_layout(agent: AgentKind) -> LiveLayout:
    layout = _LAYOUTS.get(agent)
    if layout is None:
        raise InputError(f"Live config snapshots are not supported for {agent}.")
    return layout


def live_source(agent: AgentKind, environment: Mapping[str, str], home: Path) -> LiveSource:
    layout = live_layout(agent)
    configured = environment.get(layout.variable)
    root = configured_directory(environment, layout.variable, home, layout.directory)
    if layout.state_file is None:
        return LiveSource(agent, root, None)
    return LiveSource(agent, root, (root if configured else home) / layout.state_file)


def snapshot_directory(agent: AgentKind) -> str:
    return live_layout(agent).snapshot


def shareable_state(state: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        key: value
        for key, value in state.items()
        if key != "projects" and not PRIVATE_STATE.search(key)
    }


def _limits() -> TreeLimits:
    return TreeLimits(MAX_FILES, MAX_DIRECTORIES, MAX_BYTES, MAX_JSON_BYTES)


def _tree_sha256(records: Mapping[str, str]) -> str:
    return digest("".join(f"{name}\0{value}\n" for name, value in sorted(records.items())).encode())


def _present_entries(layout: LiveLayout, root: Path) -> tuple[str, ...]:
    return tuple(
        entry for entry in layout.entries if (root / entry).exists() or (root / entry).is_symlink()
    )


def _refuse_linked_entries(layout: LiveLayout, root: Path, entries: tuple[str, ...]) -> None:
    symlinked = sorted(entry for entry in entries if (root / entry).is_symlink())
    if layout.policy.dereference and symlinked:
        raise InputError(
            f"Live {layout.label} config has symlinked top-level entries, refusing to "
            "snapshot: " + ", ".join(symlinked)
        )


def _require_copyable(layout: LiveLayout, root: Path, entries: tuple[str, ...]) -> None:
    copy_tree(
        root,
        entries,
        None,
        policy=layout.policy._replace(screen=None),
        limits=_limits(),
        read=False,
    )


def _stage(
    layout: LiveLayout, source: LiveSource, real: Path, staged: Path, entries: tuple[str, ...]
) -> LiveConfigManifest:
    tree = copy_tree(real, entries, staged, policy=layout.policy, limits=_limits())
    state = (
        canonical_json(shareable_state(_json(source.state) if source.state is not None else {}))
        if layout.state_file is not None
        else None
    )
    if state is not None:
        atomic_write(staged / STATE_FILE, state, replace=False)
    return LiveConfigManifest(
        agent=layout.agent,
        source=str(source.root),
        entries=entries,
        files=sum(not value.startswith("-> ") for value in tree.records.values()),
        bytes=tree.bytes,
        tree_sha256=_tree_sha256(tree.records),
        settings_sha256=tree.records.get(layout.settings),
        instructions_sha256=tree.records.get(layout.instructions),
        state_sha256=None if state is None else digest(state),
        **layout.details(staged, tree.parsed)._asdict(),
    )


def snapshot_live_config(source: LiveSource, destination: Path) -> LiveConfigManifest:
    layout, root = live_layout(source.agent), source.root
    if not root.is_dir():
        raise InputError(f"Live {layout.label} config directory does not exist: {root}")
    entries = _present_entries(layout, root)
    _refuse_linked_entries(layout, root, entries)
    real = Path(os.path.realpath(root))
    _require_copyable(layout, real, entries)
    staged = destination.with_name(destination.name + ".partial")
    if staged.exists():
        shutil.rmtree(staged)
    ensure_directory(staged)
    try:
        manifest = _stage(layout, source, real, staged, entries)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    atomic_write(staged / MANIFEST_FILE, canonical_json(manifest), replace=False)
    staged.rename(destination)
    return manifest


def load_snapshot_manifest(snapshot: Path) -> LiveConfigManifest:
    return parse_model(
        read_bytes(snapshot / MANIFEST_FILE, limit=MAX_JSON_BYTES), LiveConfigManifest
    )


def journaled_manifests(events: Iterable[JournalEvent]) -> dict[AgentKind, LiveConfigManifest]:
    manifests = [
        parse_model(canonical_json(event.data), LiveConfigManifest)
        for event in events
        if event.event == "live-config"
    ]
    return {manifest.agent: manifest for manifest in reversed(manifests)}


def _changed(snapshot: Path) -> SnapshotChangedError:
    return SnapshotChangedError(f"Live config snapshot changed after it was taken: {snapshot}")


def _discard(destination: Path, entries: tuple[str, ...]) -> None:
    for path in (destination / entry for entry in entries):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()


def _walk_snapshot(snapshot: Path, manifest: LiveConfigManifest, destination: Path | None) -> None:
    if not set(manifest.entries) <= set(live_layout(manifest.agent).entries):
        raise _changed(snapshot)
    try:
        tree = copy_tree(
            snapshot,
            manifest.entries,
            destination,
            policy=TreePolicy(dereference=False),
            limits=_limits(),
        )
    except (FileNotFoundError, NotADirectoryError, InputError) as error:
        raise _changed(snapshot) from error
    if _tree_sha256(tree.records) != manifest.tree_sha256:
        raise _changed(snapshot)


def _require_tree(snapshot: Path, manifest: LiveConfigManifest) -> None:
    _walk_snapshot(snapshot, manifest, None)


def _copy_verified_tree(snapshot: Path, manifest: LiveConfigManifest, destination: Path) -> None:
    try:
        _walk_snapshot(snapshot, manifest, destination)
    except SnapshotChangedError:
        _discard(destination, manifest.entries)
        raise


def _state_content(snapshot: Path, manifest: LiveConfigManifest) -> bytes | None:
    if live_layout(manifest.agent).state_file is None:
        return None
    try:
        return read_bytes(snapshot / STATE_FILE, limit=MAX_JSON_BYTES)
    except (FileNotFoundError, InputError) as error:
        raise _changed(snapshot) from error


def _require_state(snapshot: Path, manifest: LiveConfigManifest, content: bytes) -> None:
    if manifest.state_sha256 is not None:
        if digest(content) != manifest.state_sha256:
            raise _changed(snapshot)
        return
    try:
        servers = _keys(_parse_json(content, snapshot / STATE_FILE).get("mcpServers", {}))
    except InputError as error:
        raise _changed(snapshot) from error
    if servers != manifest.mcp_servers:
        raise _changed(snapshot)


def verify_snapshot(snapshot: Path, manifest: LiveConfigManifest) -> None:
    _require_tree(snapshot, manifest)
    content = _state_content(snapshot, manifest)
    if content is not None:
        _require_state(snapshot, manifest, content)


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
    recipe: NativeRecipe,
    manifest: LiveConfigManifest,
) -> None:
    if manifest.agent != recipe.agent:
        raise InputError(
            f"Live config snapshot is for {manifest.agent}, not the profile's {recipe.agent}."
        )
    layout = live_layout(manifest.agent)
    content = _state_content(snapshot, manifest)
    if content is not None:
        _require_state(snapshot, manifest, content)
    state = (
        None
        if content is None
        else subject_state(
            _parse_json(content, snapshot / STATE_FILE),
            recipe.removed_mcp_servers,
            workspace if recipe.workspace_trust == "trusted" else None,
        )
    )
    _copy_verified_tree(snapshot, manifest, config_root)
    for name in layout.rebased:
        path = config_root / name
        if path.is_file() and not path.is_symlink():
            rebased = _rebase(_json(path), manifest.source, str(config_root))
            atomic_write(path, json.dumps(rebased, indent=2).encode())
    if layout.post_copy is not None:
        layout.post_copy(config_root, manifest)
    if layout.state_file is not None and state is not None:
        atomic_write(config_root / layout.state_file, canonical_json(state), replace=False)
        os.chmod(config_root / layout.state_file, 0o600)
