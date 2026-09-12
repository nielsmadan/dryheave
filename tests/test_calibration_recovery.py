from uuid import UUID

import pytest

from dryheave.calibration_recovery import (
    CalibrationOperation,
    CalibrationOwnership,
    reconcile_calibrations,
)
from dryheave.errors import InputError
from dryheave.serialization import canonical_json, parse_model


@pytest.mark.parametrize(
    "failure", ["filesystem.write_all", "calibration_recovery.publish_directory"]
)
def test_failed_initial_publication_cleans_only_owned_staging(store, monkeypatch, failure):
    root = store.root / "calibrations" / ("a" * 32)
    foreign = root.parent / ".pending-retained"
    foreign.mkdir(parents=True)
    (foreign / "evidence").write_bytes(b"preserved")

    def fail(*_args, **_kwargs):
        raise OSError("initial publication failed")

    with monkeypatch.context() as patch:
        patch.setattr("dryheave." + failure, fail)
        with pytest.raises(OSError, match="initial publication failed"):
            CalibrationOwnership.create(root, "a" * 64)
    assert set(root.parent.iterdir()) == {foreign}
    assert (foreign / "evidence").read_bytes() == b"preserved"
    ownership = CalibrationOwnership.create(root, "a" * 64)
    with store.native_lock():
        reconcile_calibrations(store)
    state = parse_model((root / "operation.json").read_bytes(), CalibrationOperation)
    assert state.operation_id == ownership.state.operation_id
    assert state.cleanup.known_writers_stopped
    assert state.cleanup.errors == ()
    assert set(root.parent.iterdir()) == {foreign, root}
    assert (foreign / "evidence").read_bytes() == b"preserved"


@pytest.mark.parametrize("populated", [False, True])
def test_initial_publication_conflict_preserves_foreign_destination(store, populated):
    root = store.root / "calibrations" / ("a" * 32)
    root.mkdir(parents=True)
    if populated:
        (root / "evidence").write_bytes(b"foreign operation")
    original = {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(FileExistsError):
        CalibrationOwnership.create(root, "a" * 64)
    assert set(root.parent.iterdir()) == {root}
    assert {path.name: path.read_bytes() for path in root.iterdir()} == original


def test_staging_name_collision_preserves_existing_directory(store, monkeypatch):
    root = store.root / "calibrations" / ("a" * 32)
    staged = root.parent / (".pending-" + "b" * 32)
    staged.mkdir(parents=True)
    (staged / "evidence").write_bytes(b"foreign staging")
    monkeypatch.setattr("dryheave.calibration_recovery.uuid4", lambda: UUID("b" * 32))
    with pytest.raises(FileExistsError):
        CalibrationOwnership.create(root, "a" * 64)
    assert set(root.parent.iterdir()) == {staged}
    assert (staged / "evidence").read_bytes() == b"foreign staging"


@pytest.mark.parametrize("damage", ["missing", "malformed", "identity"])
def test_damaged_published_operation_blocks_recovery_without_removal(store, damage):
    root = store.root / "calibrations" / ("a" * 32)
    ownership = CalibrationOwnership.create(root, "a" * 64)
    path = root / "operation.json"
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_bytes(b"{}")
    else:
        path.write_bytes(
            canonical_json(ownership.state.model_copy(update={"operation_id": "b" * 32}))
        )
    original = {item.name: item.read_bytes() for item in root.iterdir()}
    with store.native_lock(), pytest.raises((FileNotFoundError, InputError)):
        reconcile_calibrations(store)
    assert set(root.parent.iterdir()) == {root}
    assert {item.name: item.read_bytes() for item in root.iterdir()} == original
