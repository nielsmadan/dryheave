import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import psutil
from pydantic import AwareDatetime

from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import InputError
from dryheave.filesystem import (
    atomic_write,
    directory_fd,
    directory_names,
    file_lock,
    publish_directory,
    read_bytes,
)
from dryheave.models import ObjectId, RunId, StrictModel
from dryheave.process_ownership import ProcessOwner
from dryheave.serialization import canonical_json, parse_model
from dryheave.storage import ObjectStore


class CalibrationOperation(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: RunId
    case_id: ObjectId
    created_at: AwareDatetime
    owned: tuple[ProcessIdentity, ...] = ()
    cleanup: CleanupReport | None = None
    result_id: ObjectId | None = None


class CalibrationOwnership:
    def __init__(self, root: Path, state: CalibrationOperation) -> None:
        self.root = root
        self.state = state

    @classmethod
    def create(cls, root: Path, case_id: str) -> "CalibrationOwnership":
        owner = cls(
            root,
            CalibrationOperation(
                operation_id=root.name, case_id=case_id, created_at=datetime.now(UTC)
            ),
        )
        staged = root.parent / f".pending-{uuid4().hex}"
        with directory_fd(root.parent, create=True) as descriptor:
            os.mkdir(staged.name, mode=0o700, dir_fd=descriptor)
            try:
                os.fsync(descriptor)
                atomic_write(staged / "operation.json", canonical_json(owner.state), replace=False)
                publish_directory(staged, root)
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
        return owner

    def save(self) -> None:
        atomic_write(self.root / "operation.json", canonical_json(self.state))

    def own(self, identity: ProcessIdentity) -> None:
        if identity not in self.state.owned:
            self.state = self.state.model_copy(
                update={"owned": (*self.state.owned, identity), "cleanup": None}
            )
            self.save()

    def reconcile(self) -> None:
        owner = ProcessOwner()
        errors = []
        try:
            for identity in self.state.owned:
                try:
                    process = psutil.Process(identity.pid)
                    if process.create_time() == identity.created:
                        owner.add(process)
                except psutil.NoSuchProcess:
                    continue
                except psutil.AccessDenied:
                    errors.append("recovery_process_visibility_denied")
            owner.publish(self.own)
        finally:
            report = owner.stop(timeout=3, terminal_closed=True)
            owner.publish(self.own)
        cleanup = report.model_copy(
            update={
                "known_writers_stopped": report.known_writers_stopped and not errors,
                "errors": tuple(dict.fromkeys((*report.errors, *errors))),
            }
        )
        self.state = self.state.model_copy(update={"cleanup": cleanup})
        self.save()
        if not cleanup.known_writers_stopped or cleanup.errors:
            raise InputError(
                f"Calibration {self.root.name} has unresolved owned writers: {self.root}"
            )


def reconcile_calibrations(store: ObjectStore) -> None:
    root = store.root / "calibrations"
    try:
        names = directory_names(root)
    except FileNotFoundError:
        return
    for name in sorted(names):
        if not re.fullmatch(r"[0-9a-f]{32}", name):
            continue
        with file_lock(store.root / "locks" / ("calibration-" + name + ".lock")):
            state = parse_model(
                read_bytes(root / name / "operation.json", limit=16 * 1024 * 1024),
                CalibrationOperation,
            )
            if state.operation_id != name:
                raise InputError("Calibration recovery identity differs from its directory.")
            if (
                state.cleanup is None
                or not state.cleanup.known_writers_stopped
                or state.cleanup.errors
            ):
                CalibrationOwnership(root / name, state).reconcile()
