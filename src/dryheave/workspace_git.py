import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TextIO

from pydantic import JsonValue

from dryheave.errors import DryheaveError, InputError
from dryheave.filesystem import atomic_write, read_bytes
from dryheave.models import CommandSpec, ObjectKind
from dryheave.processes import CommandResult, run_command
from dryheave.storage import ObjectStore
from dryheave.workspaces import GIT_COMMIT_MODES, GitCommitMode, Workspace

GITIGNORE = ".gitignore"
GIT_TIMEOUT_SECONDS = 60
GIT_OUTPUT_LIMIT = 256 * 1024
ERROR_TAIL = 2000
BASE_IGNORES = (
    ".dryheave/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "replays/",
)
REPOSITORY_ENVIRONMENT = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_GRAFT_FILE",
        "GIT_SHALLOW_FILE",
    }
)
MISSING_GIT = "git is not installed or not on PATH"
HOOK_RUN_VERSION = (2, 36)
GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)")
PROMPT_DEFAULT: GitCommitMode = "ask"
MODE_CHOICES: tuple[tuple[GitCommitMode, str], ...] = (
    ("ask", "ask: offer a commit after each checkpoint and let you decide"),
    ("auto", "auto: commit the files each checkpoint command wrote"),
    ("off", "off: never offer commits"),
)


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(
    root: Path,
    *argv: str,
    input_bytes: bytes = b"",
    index_file: str | None = None,
    hook: bool = False,
) -> CommandResult:
    return run_command(
        CommandSpec(
            argv=("git", "--literal-pathspecs", *argv),
            timeout_seconds=GIT_TIMEOUT_SECONDS,
            max_output_bytes=GIT_OUTPUT_LIMIT,
        ),
        root,
        input_bytes=input_bytes,
        environment={
            **{k: v for k, v in os.environ.items() if k not in REPOSITORY_ENVIRONMENT},
            "GIT_TERMINAL_PROMPT": "0",
            **({"GIT_INDEX_FILE": index_file} if index_file else {}),
            **({"GIT_EDITOR": ":"} if hook else {}),
        },
    )


def _failure(result: CommandResult) -> str:
    text = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    if result.outcome != "exited":
        return f"git {result.argv[2]} did not finish ({result.outcome})"
    return text[-ERROR_TAIL:] or f"git {result.argv[2]} exited with {result.returncode}"


def require_git(mode: GitCommitMode) -> None:
    if mode != "off" and not git_available():
        raise InputError(
            f"{MISSING_GIT}; dryheave init sets up a git repository for the "
            "workspace. Install git, or run init --git-commit off to skip commits."
        )


def inside_repository(root: Path) -> bool:
    result = _run(root, "rev-parse", "--show-toplevel")
    return (
        result.returncode == 0
        and Path(result.stdout.decode("utf-8", errors="replace").strip()).resolve()
        == root.resolve()
    )


def ensure_repository(root: Path) -> str:
    if not git_available():
        return "skipped"
    if inside_repository(root):
        return "existing"
    result = _run(root, "init", "--quiet")
    if result.returncode != 0:
        raise InputError(f"git init failed in {root}: {_failure(result)}")
    return "created"


def ignore_entries(workspace: Workspace) -> tuple[str, ...]:
    configured = (
        f"/{value.rstrip('/')}/"
        for value in (
            workspace.config.authoring,
            workspace.config.store,
            workspace.config.runtime,
        )
        if not Path(value).is_absolute() and not value.startswith(".dryheave/")
    )
    return (*BASE_IGNORES, *configured)


def missing_lines(existing: str, wanted: Iterable[str]) -> tuple[str, ...]:
    present = {line.strip() for line in existing.splitlines()}
    return tuple(dict.fromkeys(line for line in wanted if line not in present))


def write_gitignore(workspace: Workspace) -> Path:
    path = workspace.root / GITIGNORE
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    added = missing_lines(existing, ignore_entries(workspace))
    if added:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        atomic_write(path, (existing + separator + "\n".join(added) + "\n").encode("utf-8"))
    return path


def setup_repository(workspace: Workspace) -> dict[str, JsonValue]:
    state = ensure_repository(workspace.root)
    return {
        "repository": state,
        "gitignore": str(write_gitignore(workspace)),
        "git_commit": workspace.config.git_commit,
    }


def choose_mode(
    flag: GitCommitMode | None,
    existing: GitCommitMode | None,
    stdin: TextIO,
    stdout: TextIO,
    prompt_stream: TextIO,
) -> GitCommitMode:
    if flag is not None:
        return flag
    if existing is not None:
        return existing
    if not (stdin.isatty() and stdout.isatty()):
        return PROMPT_DEFAULT
    return prompt_mode(stdin, prompt_stream)


def prompt_mode(stdin: TextIO, output: TextIO) -> GitCommitMode:
    print(
        "Commit authored files (voices, cases, reports) to this workspace's git repository?",
        file=output,
    )
    for number, (_, text) in enumerate(MODE_CHOICES, start=1):
        print(f"  {number}. {text}", file=output)
    default_number = [name for name, _ in MODE_CHOICES].index(PROMPT_DEFAULT) + 1
    while True:
        print(f"Choose 1-{len(MODE_CHOICES)} [{default_number}]: ", end="", file=output, flush=True)
        answer = stdin.readline()
        if answer == "":
            return PROMPT_DEFAULT
        answer = answer.strip()
        if answer == "":
            return PROMPT_DEFAULT
        if answer.isdigit() and 1 <= int(answer) <= len(MODE_CHOICES):
            return MODE_CHOICES[int(answer) - 1][0]
        named = [mode for mode in GIT_COMMIT_MODES if mode == answer]
        if named:
            return named[0]
        print(f"Enter a number from 1 to {len(MODE_CHOICES)}.", file=output)


def relative_paths(root: Path, paths: Iterable[Path]) -> tuple[str, ...]:
    base = root.resolve()
    inside = (
        resolved.relative_to(base).as_posix()
        for resolved in (path.absolute().resolve() for path in paths)
        if resolved.is_relative_to(base) and resolved != base
    )
    return tuple(dict.fromkeys(inside))


def case_paths(draft: Path) -> tuple[Path, ...]:
    directory = draft.absolute().parent
    try:
        value = json.loads(read_bytes(draft.absolute(), limit=16 * 1024 * 1024))
    except (OSError, ValueError, InputError):
        return (draft,)
    hidden = value.get("hidden_files") if isinstance(value, dict) else None
    patch = value.get("reference_patch") if isinstance(value, dict) else None
    names = (*(hidden.values() if isinstance(hidden, dict) else ()), patch)
    referenced = tuple(directory / name for name in names if isinstance(name, str))
    return (draft, *referenced)


def _output(result: CommandResult) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


class _StepFailed(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _step(root: Path, index: str, *argv: str, input_bytes: bytes = b"") -> CommandResult:
    result = _run(root, *argv, input_bytes=input_bytes, index_file=index, hook=argv[0] == "hook")
    if result.returncode != 0:
        raise _StepFailed(_failure(result))
    return result


def git_version(text: str) -> tuple[int, int] | None:
    match = GIT_VERSION.search(text)
    return (int(match[1]), int(match[2])) if match else None


def _require_hook_run(root: Path) -> None:
    result = _run(root, "version")
    reported = _output(result)
    version = git_version(reported)
    if version is not None and version < HOOK_RUN_VERSION:
        found = reported.removeprefix("git version ") or "unknown"
        raise _StepFailed(
            f"checkpoint commits need git 2.36 or later (found {found}); "
            "upgrade git or set [git] commit to ask or off"
        )


def _moved_error(root: Path, parent: str | None, moved: CommandResult) -> str:
    current = _run(root, "rev-parse", "--quiet", "--verify", "HEAD^{commit}")
    head = _output(current) if current.returncode == 0 else None
    if head != parent:
        return (
            "HEAD moved while the checkpoint commit was built (another commit landed "
            f"first), so nothing was committed and your index is unchanged: {_failure(moved)}"
        )
    return (
        f"HEAD could not be moved from {parent or 'the unborn branch'}, so nothing was "
        f"committed and your index is unchanged: {_failure(moved)}"
    )


def _build_commit(root: Path, scratch: Path, message: str, paths: Sequence[str]) -> str | None:
    index = str(scratch / "index")
    message_file = scratch / "COMMIT_EDITMSG"
    recorded = _run(root, "rev-parse", "--quiet", "--verify", "HEAD^{commit}")
    parent = _output(recorded) if recorded.returncode == 0 else None
    _step(root, index, "read-tree", *((parent,) if parent else ("--empty",)))
    before = _output(_step(root, index, "write-tree"))
    _step(root, index, "add", "--", *paths)
    if _output(_step(root, index, "write-tree")) == before:
        return None
    _require_hook_run(root)
    _step(root, index, "hook", "run", "--ignore-missing", "pre-commit")
    if _output(_step(root, index, "write-tree")) == before:
        return None
    message_file.write_text(message + "\n", encoding="utf-8")
    _step(
        root,
        index,
        "hook",
        "run",
        "--ignore-missing",
        "prepare-commit-msg",
        "--",
        str(message_file),
        "message",
    )
    _step(root, index, "hook", "run", "--ignore-missing", "commit-msg", "--", str(message_file))
    cleaned = _step(root, index, "stripspace", input_bytes=message_file.read_bytes()).stdout
    if not cleaned.strip():
        raise _StepFailed("Aborting commit due to empty commit message.")
    message_file.write_bytes(cleaned)
    tree = _output(_step(root, index, "write-tree"))
    parents = ("-p", parent) if parent else ()
    commit = _output(_step(root, index, "commit-tree", tree, *parents, "-F", str(message_file)))
    subject = cleaned.decode("utf-8", errors="replace").partition("\n")[0]
    reflog = f"commit: {subject}" if parent else f"commit (initial): {subject}"
    moved = _run(root, "update-ref", "-m", reflog, "HEAD", commit, parent or "0" * len(commit))
    if moved.returncode != 0:
        raise _StepFailed(_moved_error(root, parent, moved))
    return commit


def _commit(root: Path, message: str, paths: Sequence[str]) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {"message": message}
    if not inside_repository(root):
        return base | {"state": "failed", "error": "The workspace is not a git repository."}
    if _run(root, "rev-parse", "--quiet", "--verify", "MERGE_HEAD").returncode == 0:
        return base | {
            "state": "failed",
            "error": "A merge is in progress; finish it before committing.",
        }
    try:
        with tempfile.TemporaryDirectory(prefix="dryheave-index-") as scratch:
            commit = _build_commit(root, Path(scratch), message, paths)
    except _StepFailed as failure:
        return base | {"state": "failed", "error": failure.error}
    if commit is None:
        return base | {"state": "nothing_to_commit"}
    synced = _run(root, "reset", "--quiet", "--", *paths)
    _run(root, "hook", "run", "--ignore-missing", "post-commit", hook=True)
    result: dict[str, JsonValue] = base | {"state": "committed", "commit": commit}
    if synced.returncode != 0:
        result["index_sync_error"] = _failure(synced)
    return result


def case_alias(
    store: ObjectStore, workspace: Workspace | None, draft: Path, identifier: str
) -> dict[str, JsonValue]:
    if workspace is None:
        return {}
    resolved = draft.resolve()
    if resolved.name != "case.json" or resolved.parent.parent != workspace.cases_dir.resolve():
        return {}
    name = resolved.parent.name
    try:
        existing = store.aliases().get(name)
        if existing is not None:
            kind = store.get(existing).kind
            if kind != ObjectKind.CASE:
                return {
                    "alias_error": f"Alias {name!r} already points at a {kind.value} object; "
                    "rename the case folder or move that alias first."
                }
        store.set_alias(name, identifier, replace=True)
    except (DryheaveError, OSError) as error:
        return {"alias_error": str(error)}
    return {"alias": name}


def checkpoint(
    workspace: Workspace | None, message: str, paths: Iterable[Path]
) -> dict[str, JsonValue]:
    if workspace is None:
        return {}
    relative = relative_paths(workspace.root, paths)
    mode = workspace.config.git_commit
    result: dict[str, JsonValue] = {"commit_paths": list(relative), "git_commit": mode}
    if mode != "auto":
        return result
    if not relative:
        return result | {"commit": {"message": message, "state": "nothing_to_commit"}}
    try:
        committed = _commit(workspace.root, message, relative)
    except OSError:
        committed = {"message": message, "state": "failed", "error": MISSING_GIT}
    return result | {"commit": committed}
