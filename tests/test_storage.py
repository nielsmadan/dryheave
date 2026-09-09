import json
from pathlib import Path

import pytest

from conftest import ExamplePayload
from dryheave.errors import (
    ConflictError,
    InputError,
    IntegrityError,
    LockBusyError,
    NotFoundError,
    PathError,
)
from dryheave.filesystem import file_lock
from dryheave.models import CommandSpec, ObjectKind
from dryheave.serialization import digest
from dryheave.storage import ObjectStore, default_store_path


def test_hash_stability_deduplication_and_typed_round_trip(
    store: ObjectStore, payload: ExamplePayload
) -> None:
    first = store.put(ObjectKind.PERSONA, payload, files={"b.txt": b"same", "a.txt": b"same"})
    second = store.put(ObjectKind.PERSONA, payload, files={"a.txt": b"same", "b.txt": b"same"})
    assert first == second
    assert store.load(first, ExamplePayload, kind=ObjectKind.PERSONA) == payload
    assert store.read_blob(first, "a.txt") == b"same"
    assert len(list((store.object_path(first) / "blobs").iterdir())) == 1
    assert list((store.root / "objects").iterdir()) == [store.object_path(first)]


def test_payload_and_blob_boundaries_are_revalidated(store: ObjectStore) -> None:
    with pytest.raises(InputError, match="StrictModel"):
        store.put(ObjectKind.CASE, {"title": "unvalidated"})
    invalid = ExamplePayload.model_construct(title="title", count="unvalidated")
    with pytest.raises(InputError, match="schema"):
        store.put(ObjectKind.CASE, invalid)
    with pytest.raises(InputError, match="bytes"):
        store.put(ObjectKind.CASE, ExamplePayload(title="title"), files={"file": "text"})


def test_hash_covers_payload_kind_and_blob_names_and_contents(
    store: ObjectStore, payload: ExamplePayload
) -> None:
    identifiers = {
        store.put(ObjectKind.PERSONA, payload, files={"a": b"one"}),
        store.put(ObjectKind.PROFILE, payload, files={"a": b"one"}),
        store.put(ObjectKind.PERSONA, ExamplePayload(title="other"), files={"a": b"one"}),
        store.put(ObjectKind.PERSONA, payload, files={"b": b"one"}),
        store.put(ObjectKind.PERSONA, payload, files={"a": b"two"}),
    }
    assert len(identifiers) == 5


@pytest.mark.parametrize(
    "mutation",
    ["manifest", "blob", "missing-blob", "extra-blob", "extra-entry", "missing-manifest"],
)
def test_object_mutation_detected(
    store: ObjectStore, payload: ExamplePayload, mutation: str
) -> None:
    identifier = store.put(ObjectKind.CASE, payload, files={"task.txt": b"original"})
    path = store.object_path(identifier)
    blob = path / "blobs" / digest(b"original")
    if mutation == "manifest":
        (path / "manifest.json").write_bytes(b"{}")
    elif mutation == "blob":
        blob.write_bytes(b"changed")
    elif mutation == "missing-blob":
        blob.unlink()
    elif mutation == "extra-blob":
        (path / "blobs" / "unexpected").write_bytes(b"x")
    elif mutation == "extra-entry":
        (path / "surprise").write_bytes(b"x")
    else:
        (path / "manifest.json").unlink()
    with pytest.raises(IntegrityError):
        store.verify(identifier)
    with pytest.raises(IntegrityError):
        store.put(ObjectKind.CASE, payload, files={"task.txt": b"original"})


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":2,"kind":"case","payload":{},"files":{},"references":[]}',
        b'{"schema_version":1,"kind":"case","kind":"profile","payload":{},"files":{},"references":[]}',
        b'{"files":{}, "kind":"case","payload":{},"references":[],"schema_version":1}',
        b'{"files":{"../outside":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"kind":"case","payload":{},"references":[],"schema_version":1}',
    ],
)
def test_hash_alone_does_not_accept_invalid_manifest(store: ObjectStore, content: bytes) -> None:
    identifier = digest(content)
    path = store.object_path(identifier)
    (path / "blobs").mkdir(parents=True)
    (path / "manifest.json").write_bytes(content)
    with pytest.raises(IntegrityError):
        store.verify(identifier)


def test_reference_closure_is_checked_and_frozen(
    store: ObjectStore, payload: ExamplePayload
) -> None:
    child = store.put(ObjectKind.PERSONA, payload, files={"instructions": b"first"})
    parent = store.put(ObjectKind.CASE, payload, references=(child, child))
    assert store.get(parent).references == (child,)
    assert parent != store.put(ObjectKind.CASE, payload)
    (store.object_path(child) / "blobs" / digest(b"first")).write_bytes(b"altered")
    with pytest.raises(IntegrityError, match="blob hash"):
        store.verify(parent)


def test_missing_reference_prevents_publication(
    store: ObjectStore, payload: ExamplePayload
) -> None:
    with pytest.raises(NotFoundError):
        store.put(ObjectKind.CASE, payload, references=("a" * 64,))
    assert not store.root.exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../b", "a\\b"])
def test_unsafe_file_names_cannot_escape_store(
    store: ObjectStore, payload: ExamplePayload, name: str
) -> None:
    with pytest.raises(InputError):
        store.put(ObjectKind.PROFILE, payload, files={name: b"x"})
    assert not store.root.exists()


def test_symlinked_object_or_blob_is_rejected(
    store: ObjectStore, payload: ExamplePayload, tmp_path: Path
) -> None:
    identifier = store.put(ObjectKind.PROFILE, payload, files={"config": b"x"})
    path = store.object_path(identifier) / "blobs" / digest(b"x")
    target = tmp_path / "external"
    target.write_bytes(b"x")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(PathError):
        store.verify(identifier)


def test_interrupted_publication_is_invisible_and_retryable(
    store: ObjectStore, payload: ExamplePayload, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dryheave import storage

    def fail(_staged: Path, _destination: Path) -> None:
        raise OSError("publication interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "publish_directory", fail)
        with pytest.raises(OSError, match="interrupted"):
            store.put(ObjectKind.CASE, payload, files={"task": b"x"})
    assert list((store.root / "objects").iterdir()) == []
    identifier = store.put(ObjectKind.CASE, payload, files={"task": b"x"})
    assert store.read_blob(identifier, "task") == b"x"


def test_alias_updates_are_explicit_and_leave_old_inputs_valid(
    store: ObjectStore, payload: ExamplePayload
) -> None:
    first = store.put(ObjectKind.PROFILE, payload)
    second = store.put(ObjectKind.PROFILE, ExamplePayload(title="revised"))
    store.set_alias("everyday", first)
    store.set_alias("everyday", first)
    assert store.resolve("everyday") == first
    with pytest.raises(ConflictError, match="--replace"):
        store.set_alias("everyday", second)
    store.set_alias("everyday", second, replace=True)
    assert store.aliases() == {"everyday": second}
    assert store.load(first, ExamplePayload) == payload
    store.delete_alias("everyday")
    assert store.aliases() == {}
    assert store.get(second).payload["title"] == "revised"
    with pytest.raises(NotFoundError):
        store.delete_alias("everyday")


@pytest.mark.parametrize("alias", ["", "../elsewhere", "a/b", "a" * 129, "a" * 64])
def test_invalid_aliases_fail(store: ObjectStore, payload: ExamplePayload, alias: str) -> None:
    identifier = store.put(ObjectKind.CASE, payload)
    with pytest.raises(InputError):
        store.set_alias(alias, identifier)


def test_alias_lock_contention_preserves_index(store: ObjectStore, payload: ExamplePayload) -> None:
    identifier = store.put(ObjectKind.CASE, payload)
    store.set_alias("first", identifier)
    with file_lock(store.root / "locks" / "aliases.lock"), pytest.raises(LockBusyError):
        store.set_alias("second", identifier)
    assert store.aliases() == {"first": identifier}


def test_alias_index_schema_is_verified(store: ObjectStore) -> None:
    store.root.mkdir()
    (store.root / "aliases.json").write_text(json.dumps({"schema_version": 2, "aliases": {}}))
    with pytest.raises(IntegrityError, match="Alias index"):
        store.aliases()


def test_native_lock_excludes_other_store_instances(store: ObjectStore) -> None:
    other = ObjectStore(store.root)
    with store.native_lock(), pytest.raises(LockBusyError), other.native_lock():
        pytest.fail("native execution was concurrent")


def test_read_errors_and_typed_schema_mismatch(store: ObjectStore, payload: ExamplePayload) -> None:
    assert store.aliases() == {}
    with pytest.raises(NotFoundError, match="Alias"):
        store.get("missing")
    with pytest.raises(NotFoundError, match="Object"):
        store.get("b" * 64)
    with pytest.raises(InputError):
        store.object_path("../outside")
    identifier = store.put(ObjectKind.CASE, payload)
    with pytest.raises(InputError, match="Expected profile"):
        store.get(identifier, kind=ObjectKind.PROFILE)
    with pytest.raises(InputError, match="schema"):
        store.load(identifier, CommandSpec)
    with pytest.raises(NotFoundError, match="no file"):
        store.read_blob(identifier, "missing")


def test_default_store_uses_only_documented_xdg_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_store_path() == tmp_path / "dryheave"
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    with pytest.raises(InputError, match="absolute"):
        default_store_path()
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_store_path() == tmp_path / ".local/share/dryheave"


def test_scoped_blob_read_traverses_closure_once_and_rechecks_returned_bytes(
    store, payload, monkeypatch
):
    from dryheave.errors import IntegrityError
    from dryheave.models import ObjectKind

    dependency = store.put(ObjectKind.PERSONA, payload, files={"dependency": b"stable"})
    files = {f"file-{index}": str(index).encode() for index in range(40)}
    identifier = store.put(ObjectKind.RESULT, payload, files=files, references=(dependency,))
    original = store._manifest
    visited = []

    def manifest(current):
        visited.append(current)
        return original(current)

    monkeypatch.setattr(store, "_manifest", manifest)
    assert store.read_blobs(identifier) == files
    assert visited == [identifier, dependency]
    verify = store.verify

    def mutate_after_verify(current):
        result = verify(current)
        (store.object_path(current) / "blobs" / result.files["file-0"]).write_bytes(b"altered")
        return result

    monkeypatch.setattr(store, "verify", mutate_after_verify)
    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.read_blobs(identifier, ("file-0",))
