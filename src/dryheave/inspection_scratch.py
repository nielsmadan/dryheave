from collections.abc import Callable
from pathlib import Path

from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import DryheaveError
from dryheave.filesystem import read_bytes

CLEANUP_RECORDS = (
    "verifiers/criteria/*/final/*-evidence/[0-9][0-9][0-9][0-9][0-9][0-9]-cleanup.json",
    "judge/*/[0-9][0-9][0-9][0-9][0-9][0-9]-cleanup.json",
)
RECORD_BYTES = 16 * 1024 * 1024


def cleanup_records(private: Path) -> tuple[CleanupReport, ...] | None:
    try:
        paths = sorted(path for pattern in CLEANUP_RECORDS for path in private.glob(pattern))
        return tuple(
            CleanupReport.model_validate_json(read_bytes(path, limit=RECORD_BYTES))
            for path in paths
        )
    except (DryheaveError, OSError, ValueError):
        return None


def writers_confirmed_stopped(
    private: Path, alive: Callable[[ProcessIdentity], bool] | None = None
) -> bool:
    records = cleanup_records(private)
    return records is not None and all(
        record.known_writers_stopped or (alive is not None and not any(map(alive, record.owned)))
        for record in records
    )
