import os
import stat
from collections import deque
from pathlib import Path

from dryheave.errors import InputError, LimitError, PathError
from dryheave.filesystem import directory_names, read_bytes
from dryheave.models import AgentKind
from dryheave.profile_models import AssetSelection, CaptureSpec, FrozenAsset
from dryheave.profile_security import reject_sensitive_path
from dryheave.serialization import digest


def validate_target(agent: AgentKind, selection: AssetSelection) -> None:
    target = selection.target
    native = ".codex" if agent == AgentKind.CODEX else ".claude"
    other = ".claude" if agent == AgentKind.CODEX else ".codex"
    instruction = "AGENTS" if agent == AgentKind.CODEX else "CLAUDE"
    if other in Path(target).parts or Path(target).name.startswith(
        "CLAUDE" if instruction == "AGENTS" else "AGENTS"
    ):
        raise InputError("Cross-agent configuration or instruction translation is unsupported.")
    if selection.target_root == "home" and not (
        agent == AgentKind.CODEX and target.startswith(".agents/skills/")
    ):
        raise InputError(
            "Home targets are limited to explicitly selected Codex .agents/skills assets."
        )
    if selection.kind == "config":
        name = Path(target).name
        valid = (
            name.endswith(".config.toml") or name == "config.toml"
            if agent == AgentKind.CODEX
            else name in {"settings.json", "settings.local.json"}
        )
        if not valid or (selection.layer == "project" and not target.startswith(native + "/")):
            raise InputError("Config target must use this agent's native settings path.")
        if selection.layer == "global" and "/" in target:
            raise InputError("Global settings must remain at the native config root.")
    if selection.kind == "instruction" and not Path(target).name.startswith(instruction):
        raise InputError("Instruction targets must use this agent's native instruction filename.")
    if selection.kind == "skill":
        prefixes = (
            ("skills/",)
            if selection.target_root == "config"
            else (f"{native}/skills/", ".agents/skills/")
        )
        if not target.startswith(prefixes):
            raise InputError("Skill targets must use a supported native skill directory.")
    if selection.kind == "plugin" and not target.startswith("plugins/"):
        raise InputError("Plugin resources must use an explicit plugins/name target.")
    reject_sensitive_path(target)


class SelectedReader:
    def __init__(self, spec: CaptureSpec, base: Path) -> None:
        self.spec = spec
        self.roots = {
            name: (base / value).resolve(strict=True) for name, value in spec.include_roots.items()
        }
        if any(not path.is_dir() for path in self.roots.values()):
            raise InputError("Each include root must be an existing directory.")
        for path in self.roots.values():
            reject_sensitive_path(path.name)
        self.total_bytes = 0
        self.total_entries = 0
        self.files: dict[str, bytes] = {}
        self.assets: list[FrozenAsset] = []

    def capture(self) -> tuple[tuple[FrozenAsset, ...], dict[str, bytes]]:
        for selection in self.spec.assets:
            validate_target(self.spec.recipe.agent, selection)
            if selection.root not in self.roots:
                raise InputError("Asset selection names an undeclared include root.")
            reject_sensitive_path(selection.path)
            root = self.roots[selection.root]
            self._visit(selection, root / selection.path, selection.target, (), 0)
        return tuple(sorted(self.assets, key=lambda item: item.blob)), self.files

    def _resolve(self, path: Path) -> Path:
        allowed = tuple(self.roots.values())
        root = next((root for root in allowed if path.is_relative_to(root)), None)
        if root is None:
            raise PathError("Selected symlink target escapes the explicit include roots.")
        current = root
        pending = deque(path.relative_to(root).parts)
        links = 0
        while pending:
            part = pending.popleft()
            if part == "..":
                current = current.parent
            elif part != ".":
                current /= part
            if not any(current.is_relative_to(item) for item in allowed):
                raise PathError("Selected symlink target escapes the explicit include roots.")
            reject_sensitive_path(
                str(
                    current.relative_to(
                        next(item for item in allowed if current.is_relative_to(item))
                    )
                )
            )
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                links += 1
                if links > self.spec.limits.max_symlinks:
                    raise LimitError("Selected symlink chain is cyclic or exceeds its limit.")
                target = Path(os.readlink(current))
                if target.is_absolute():
                    target_root = next(
                        (item for item in allowed if target.is_relative_to(item)), None
                    )
                    if target_root is None:
                        raise PathError(
                            "Selected symlink target escapes the explicit include roots."
                        )
                    current = target_root
                    pending.extendleft(reversed(target.relative_to(target_root).parts))
                else:
                    current = current.parent
                    pending.extendleft(reversed(target.parts))
        return current

    def _visit(
        self,
        selection: AssetSelection,
        path: Path,
        target: str,
        ancestors: tuple[Path, ...],
        depth: int,
    ) -> None:
        self.total_entries += 1
        if self.total_entries > self.spec.limits.max_files:
            raise LimitError("Selected asset total entries exceed their limit.")
        if depth > self.spec.limits.max_depth:
            raise LimitError("Selected asset directory depth exceeds its limit.")
        reject_sensitive_path(target)
        resolved = self._resolve(path)
        metadata = resolved.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if resolved in ancestors:
                raise PathError("Selected asset contains a directory symlink cycle.")
            names = directory_names(resolved)
            if len(names) + len(self.files) > self.spec.limits.max_files:
                raise LimitError("Selected asset entry count exceeds its limit.")
            for name in sorted(names):
                self._visit(
                    selection,
                    resolved / name,
                    f"{target}/{name}",
                    (*ancestors, resolved),
                    depth + 1,
                )
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise PathError("Selected assets must resolve to regular files or directories.")
        if len(self.files) >= self.spec.limits.max_files:
            raise LimitError("Selected asset file count exceeds its limit.")
        content = read_bytes(resolved, limit=self.spec.limits.max_file_bytes)
        self.total_bytes += len(content)
        if self.total_bytes > self.spec.limits.max_total_bytes:
            raise LimitError("Selected asset total bytes exceed their limit.")
        blob = f"{selection.layer}/{selection.target_root}/{target}"
        if blob in self.files:
            raise InputError("Selected assets collide at the same layer and target.")
        self.files[blob] = content
        self.assets.append(
            FrozenAsset(
                selection=selection,
                source_root=str(self.roots[selection.root]),
                source_path=str(path),
                resolved_source=str(resolved),
                target=target,
                blob=blob,
                sha256=digest(content),
                executable=bool(metadata.st_mode & 0o111),
            )
        )
