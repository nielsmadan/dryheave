import fcntl
import json

import pytest

from dryheave.authoring_catalog import AuthoringCatalog, edit_catalog, read_catalog, save_catalog
from dryheave.errors import ConflictError, InputError, LockBusyError, PathError
from dryheave.filesystem import directory_fd


def test_revision_conflict_and_lock_preserve_catalog(tmp_path):
    with edit_catalog(tmp_path, 0) as catalog:
        assert save_catalog(tmp_path, catalog).revision == 1
    before = (tmp_path / "catalog.json").read_bytes()
    with pytest.raises(ConflictError, match="expected 0"), edit_catalog(tmp_path, 0):
        pytest.fail("stale writer entered")
    with directory_fd(tmp_path) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockBusyError), edit_catalog(tmp_path, 1):
            pytest.fail("concurrent writer entered")
    assert (tmp_path / "catalog.json").read_bytes() == before
    assert read_catalog(tmp_path).revision == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2},
        {"revision": "1"},
        {"unexpected": True},
        {"schema_version": True},
        {"requests": {"missing": {"name": "missing", "selection_id": "a" * 64}}},
    ],
)
def test_persisted_catalog_rejects_invalid_schema_without_overwrite(tmp_path, payload):
    content = json.dumps(payload).encode()
    (tmp_path / "catalog.json").write_bytes(content)
    with pytest.raises(InputError), edit_catalog(tmp_path, 0):
        pytest.fail("invalid catalog accepted")
    assert (tmp_path / "catalog.json").read_bytes() == content


def test_catalog_rejects_symlink_and_atomic_failure_preserves_revision(tmp_path, monkeypatch):
    save_catalog(tmp_path, AuthoringCatalog())
    original = (tmp_path / "catalog.json").read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("injected publication failure")

    with monkeypatch.context() as context:
        context.setattr("dryheave.authoring_catalog.atomic_write", fail)
        with pytest.raises(OSError, match="publication"), edit_catalog(tmp_path, 1) as catalog:
            save_catalog(tmp_path, catalog)
    assert (tmp_path / "catalog.json").read_bytes() == original
    (tmp_path / "catalog.json").rename(tmp_path / "saved")
    (tmp_path / "catalog.json").symlink_to(tmp_path / "saved")
    with pytest.raises(PathError):
        read_catalog(tmp_path)
    assert (tmp_path / "saved").read_bytes() == original
