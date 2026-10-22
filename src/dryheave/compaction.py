import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from dryheave.errors import DryheaveError, InputError
from dryheave.journals import RunStore
from dryheave.models import TrialStage
from dryheave.runner_state import ExecutionJournal
from dryheave.storage import ObjectStore

WORKING_DIRECTORIES = ("workspace", "assessment", "regrades")
PRUNED_PROFILE_DIRECTORY_NAMES = frozenset({"plugins", "skills", "cache"})
PROFILE_CREDENTIAL_NAMES = frozenset({"auth.json", ".credentials.json"})
COMPACTED_EVENT = "compacted"
CHECK_MINIMUM_AGE_SECONDS = 3600.0
UNASSESSED_STAGES = frozenset({TrialStage.CAPTURED, TrialStage.AUDITED, TrialStage.GRADING})


@dataclass(frozen=True)
class Compaction:
    run_id: str
    attempts: int
    removed: tuple[str, ...]
    bytes_freed: int


def _size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        for name in (*directories, *files):
            info = os.lstat(Path(root) / name)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _require_retained(store: ObjectStore, execution: ExecutionJournal) -> None:
    for state in execution.attempts.values():
        if state.stage in UNASSESSED_STAGES:
            raise InputError(
                f"Attempt {state.attempt_id} is captured but not assessed; assess it first."
            )
        if state.stage != TrialStage.FINISHED:
            raise InputError(
                f"Attempt {state.attempt_id} is {state.stage.value}; only runs whose attempts "
                "have all finished can be compacted."
            )
        retained = [
            identifier
            for identifier in (state.capture_id, state.quarantine_id, state.assessment_id)
            if identifier is not None
        ]
        if state.assessment_id is None and (state.capture_id or state.quarantine_id):
            raise InputError(
                f"Attempt {state.attempt_id} is captured but not assessed; assess it first."
            )
        for identifier in retained:
            try:
                store.verify(identifier)
            except DryheaveError as error:
                raise InputError(
                    f"Attempt {state.attempt_id} has a retained object that does not verify "
                    f"({identifier}): {error}"
                ) from error


def _profile_removals(profile_root: Path) -> list[Path]:
    found: list[Path] = []
    for current, directories, files in os.walk(profile_root, followlinks=False):
        base = Path(current)
        pruned = {
            name
            for name in directories
            if name in PRUNED_PROFILE_DIRECTORY_NAMES and not (base / name).is_symlink()
        }
        credentials = {
            name
            for name in (*directories, *files)
            if name in PROFILE_CREDENTIAL_NAMES
            and ((base / name).is_symlink() or not (base / name).is_dir())
        }
        chosen = pruned | credentials
        found.extend(base / name for name in chosen)
        directories[:] = [name for name in directories if name not in chosen]
    return sorted(found)


def _require_owned(run: Path, root: Path) -> None:
    linked = next(
        (path for path in (root.parent, root) if path.is_symlink()),
        None,
    )
    if linked is not None:
        raise InputError(
            f"Attempt path {linked.relative_to(run)} is a symlink; compaction refuses to "
            "follow it out of the run directory."
        )


def _attempt_removals(root: Path) -> list[Path]:
    working = [
        root / name
        for name in WORKING_DIRECTORIES
        if (root / name).is_dir() and not (root / name).is_symlink()
    ]
    profile_root = root / "profile"
    if not profile_root.is_dir() or profile_root.is_symlink():
        return working
    return [*working, *_profile_removals(profile_root)]


def _remove(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        info = os.lstat(path)
        path.unlink()
        return info.st_size if stat.S_ISREG(info.st_mode) else 0
    size = _size(path)
    shutil.rmtree(path)
    return size


def compact_run(store: ObjectStore, run_id: str) -> Compaction:
    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        _require_retained(store, execution)
        removed = []
        freed = 0
        roots = [execution.attempt_root(state) for state in execution.attempts.values()]
        for root in roots:
            _require_owned(journal.path, root)
        for root in roots:
            for path in _attempt_removals(root):
                freed += _remove(path)
                removed.append(str(path.relative_to(journal.path)))
        journal.append(COMPACTED_EVENT, {"removed": list(removed), "bytes_freed": freed})
        return Compaction(run_id, len(execution.attempts), tuple(removed), freed)


def prune_checks(
    store: ObjectStore, now: float, minimum_age: float = CHECK_MINIMUM_AGE_SECONDS
) -> tuple[str, ...]:
    root = store.root / "checks"
    if not root.is_dir():
        return ()
    stale = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink() and now - path.stat().st_mtime >= minimum_age
    )
    for path in stale:
        shutil.rmtree(path)
    return tuple(path.name for path in stale)
