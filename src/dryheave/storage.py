import os
import re
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from dryheave.constants import MAX_BLOB_BYTES, MAX_MANIFEST_BYTES
from dryheave.errors import (
    ConflictError,
    DryheaveError,
    InputError,
    IntegrityError,
    LimitError,
    NotFoundError,
)
from dryheave.filesystem import (
    atomic_write,
    directory_names,
    ensure_directory,
    file_lock,
    publish_directory,
    read_bytes,
    read_chunks,
)
from dryheave.models import AliasIndex, Manifest, ObjectKind, StrictModel, object_id
from dryheave.serialization import (
    canonical_json,
    digest,
    digest_chunks,
    parse_json,
    parse_model,
    validation_message,
)


def default_store_path() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise InputError("XDG_DATA_HOME must be an absolute path, or supply --store PATH.")
        return root / "dryheave"
    return Path.home() / ".local" / "share" / "dryheave"


def validated_id(value: str) -> str:
    try:
        return object_id(value)
    except ValueError as error:
        raise InputError("Expected a lowercase SHA-256 object ID.") from error


def validated_alias(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value):
        raise InputError("Alias must be 1-128 letters, digits, dots, underscores or hyphens.")
    if re.fullmatch(r"[0-9a-f]{64}", value):
        raise InputError("An alias cannot look like an object ID.")
    return value


@dataclass(frozen=True)
class ObjectEvidence:
    manifest: Manifest
    files: dict[str, bytes]
    dependency_error: str | None


class ObjectStore:
    def __init__(self, root: Path, *, read_budget: int | None = None) -> None:
        self.root = root.expanduser().absolute()
        self.read_budget = read_budget

    def _blob_ceiling(self) -> int:
        if self.read_budget is None:
            return MAX_BLOB_BYTES
        return min(self.read_budget, MAX_BLOB_BYTES)

    def _charge(self, content: bytes) -> None:
        if self.read_budget is not None:
            self.read_budget -= len(content)

    def object_path(self, identifier: str) -> Path:
        return self.root / "objects" / validated_id(identifier)

    @contextmanager
    def native_lock(self) -> Iterator[None]:
        with file_lock(self.root / "locks" / "native.lock"):
            yield

    def put(
        self,
        kind: ObjectKind,
        payload: StrictModel,
        *,
        files: Mapping[str, bytes] | None = None,
        references: tuple[str, ...] = (),
    ) -> str:
        contents = dict(files or {})
        if not isinstance(payload, StrictModel):
            raise InputError("Object payload must be a validated StrictModel.")
        validated_payload = parse_model(canonical_json(payload), type(payload))
        if any(not isinstance(content, bytes) for content in contents.values()):
            raise InputError("Object files must contain bytes.")
        if any(len(content) > MAX_BLOB_BYTES for content in contents.values()):
            raise LimitError(f"A blob exceeds the {MAX_BLOB_BYTES}-byte limit.")
        try:
            manifest = Manifest(
                kind=kind,
                payload=parse_json(canonical_json(validated_payload)),
                files={name: digest(content) for name, content in contents.items()},
                references=tuple(sorted(set(references))),
            )
        except ValidationError as error:
            raise InputError(validation_message(error)) from error
        encoded = canonical_json(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise LimitError(f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte limit.")
        identifier = digest(encoded)
        for reference in manifest.references:
            self.verify(reference)
        with file_lock(self.root / "locks" / "objects.lock"):
            try:
                self.verify(identifier)
            except NotFoundError:
                self._publish(identifier, encoded, contents, manifest)
        return identifier

    def put_manifest(self, manifest: Manifest, files: Mapping[str, bytes]) -> str:
        manifest = parse_model(canonical_json(manifest), Manifest)
        if {name: digest(content) for name, content in files.items()} != manifest.files:
            raise IntegrityError("Imported object bytes differ from their exact manifest map.")
        if any(len(content) > MAX_BLOB_BYTES for content in files.values()):
            raise LimitError("Imported object exceeds its blob byte limit.")
        encoded = canonical_json(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise LimitError("Imported manifest exceeds its byte limit.")
        identifier = digest(encoded)
        for reference in manifest.references:
            self.verify(reference)
        with file_lock(self.root / "locks" / "objects.lock"):
            try:
                self.verify(identifier)
            except NotFoundError:
                self._publish(identifier, encoded, dict(files), manifest)
        return identifier

    def _publish(
        self, identifier: str, encoded: bytes, files: dict[str, bytes], manifest: Manifest
    ) -> None:
        parent = self.root / "objects"
        staged = parent / f".pending-{uuid4().hex}"
        ensure_directory(staged / "blobs")
        try:
            atomic_write(staged / "manifest.json", encoded, replace=False)
            for blob_hash, content in {
                manifest.files[name]: data for name, data in files.items()
            }.items():
                atomic_write(staged / "blobs" / blob_hash, content, replace=False)
            publish_directory(staged, self.object_path(identifier))
        finally:
            if staged.exists():
                shutil.rmtree(staged)

    def _manifest(self, identifier: str) -> Manifest:
        path = self.object_path(identifier)
        try:
            content = read_bytes(path / "manifest.json", limit=MAX_MANIFEST_BYTES)
        except FileNotFoundError as error:
            if path.exists():
                raise IntegrityError(f"Object manifest is missing: {identifier}") from error
            raise NotFoundError(f"Object does not exist: {identifier}") from error
        if digest(content) != identifier:
            raise IntegrityError(f"Object manifest hash mismatch: {identifier}")
        try:
            manifest = parse_model(content, Manifest)
        except InputError as error:
            raise IntegrityError(f"Object manifest is invalid: {identifier}. {error}") from error
        if canonical_json(manifest) != content:
            raise IntegrityError(f"Object manifest is not canonical: {identifier}")
        return manifest

    def _structure(self, identifier: str, manifest: Manifest) -> Path:
        path = self.object_path(identifier)
        if directory_names(path) != {"manifest.json", "blobs"}:
            raise IntegrityError(f"Unexpected object entries: {identifier}")
        if directory_names(path / "blobs") != set(manifest.files.values()):
            raise IntegrityError(f"Object blob set mismatch: {identifier}")
        return path

    def _local(self, identifier: str) -> tuple[Manifest, dict[str, bytes]]:
        manifest = self._manifest(identifier)
        contents = {}
        try:
            path = self._structure(identifier, manifest)
            for blob_hash in set(manifest.files.values()):
                content = read_bytes(path / "blobs" / blob_hash, limit=self._blob_ceiling())
                if digest(content) != blob_hash:
                    raise IntegrityError(f"Object blob hash mismatch: {identifier}/{blob_hash}")
                self._charge(content)
                contents[blob_hash] = content
        except FileNotFoundError as error:
            raise IntegrityError(f"Object content is missing: {identifier}") from error
        return manifest, {name: contents[sha] for name, sha in manifest.files.items()}

    def _probe(self, identifier: str) -> Manifest:
        manifest = self._manifest(identifier)
        try:
            path = self._structure(identifier, manifest)
            for blob_hash in set(manifest.files.values()):
                blob = path / "blobs" / blob_hash
                if digest_chunks(read_chunks(blob, limit=MAX_BLOB_BYTES)) != blob_hash:
                    raise IntegrityError(f"Object blob hash mismatch: {identifier}/{blob_hash}")
        except FileNotFoundError as error:
            raise IntegrityError(f"Object content is missing: {identifier}") from error
        return manifest

    def verify(self, identifier: str) -> Manifest:
        pending = [validated_id(identifier)]
        verified: dict[str, Manifest] = {}
        while pending:
            current = pending.pop()
            if current in verified:
                continue
            verified[current] = self._probe(current)
            pending.extend(verified[current].references)
        return verified[identifier]

    def read_evidence(self, reference: str) -> ObjectEvidence:
        identifier = self.resolve(reference)
        manifest, files = self._local(identifier)
        dependency_error = None
        try:
            for dependency in manifest.references:
                self.verify(dependency)
        except DryheaveError as error:
            dependency_error = str(error)
        return ObjectEvidence(manifest, files, dependency_error)

    def get(self, reference: str, *, kind: ObjectKind | None = None) -> Manifest:
        identifier = self.resolve(reference)
        manifest = self.verify(identifier)
        if kind is not None and manifest.kind != kind:
            raise InputError(
                f"Expected {kind.value} object, found {manifest.kind.value}: {identifier}"
            )
        return manifest

    def load[T: StrictModel](
        self, reference: str, model: type[T], *, kind: ObjectKind | None = None
    ) -> T:
        manifest = self.get(reference, kind=kind)
        return parse_model(canonical_json(manifest.payload), model)

    def read_envelope(self, reference: str) -> Manifest:
        return self._manifest(self.resolve(reference))

    def read_payload[T: StrictModel](
        self, reference: str, model: type[T], *, kind: ObjectKind | None = None
    ) -> T:
        identifier = self.resolve(reference)
        manifest = self._manifest(identifier)
        if kind is not None and manifest.kind != kind:
            raise InputError(
                f"Expected {kind.value} object, found {manifest.kind.value}: {identifier}"
            )
        return parse_model(canonical_json(manifest.payload), model)

    def read_blob_bounded(self, reference: str, name: str, *, limit: int) -> bytes:
        identifier = self.resolve(reference)
        manifest = self._manifest(identifier)
        if name not in manifest.files:
            raise NotFoundError(f"Object has no file named {name!r}.")
        blob_hash = manifest.files[name]
        try:
            content = read_bytes(self.object_path(identifier) / "blobs" / blob_hash, limit=limit)
        except OSError as error:
            raise IntegrityError(f"Object content is missing: {identifier}/{blob_hash}") from error
        if digest(content) != blob_hash:
            raise IntegrityError(f"Object blob hash mismatch: {identifier}/{blob_hash}")
        return content

    def read_blobs(self, reference: str, names: tuple[str, ...] | None = None) -> dict[str, bytes]:
        identifier = self.resolve(reference)
        manifest = self.verify(identifier)
        selected = tuple(manifest.files) if names is None else names
        contents = {}
        for name in selected:
            if name not in manifest.files:
                raise NotFoundError(f"Object has no file named {name!r}.")
            blob_hash = manifest.files[name]
            content = read_bytes(
                self.object_path(identifier) / "blobs" / blob_hash, limit=self._blob_ceiling()
            )
            if digest(content) != blob_hash:
                raise IntegrityError(f"Object blob hash mismatch: {identifier}/{blob_hash}")
            self._charge(content)
            contents[name] = content
        return contents

    def read_blob(self, reference: str, name: str) -> bytes:
        return self.read_blobs(reference, (name,))[name]

    def aliases(self) -> dict[str, str]:
        try:
            content = read_bytes(self.root / "aliases.json", limit=MAX_MANIFEST_BYTES)
        except FileNotFoundError:
            return {}
        try:
            index = parse_model(content, AliasIndex)
            for name in index.aliases:
                validated_alias(name)
        except InputError as error:
            raise IntegrityError(f"Alias index is invalid. {error}") from error
        return index.aliases

    def resolve(self, reference: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", reference):
            return reference
        name = validated_alias(reference)
        try:
            return self.aliases()[name]
        except KeyError as error:
            raise NotFoundError(f"Alias does not exist: {name}") from error

    def set_alias(self, name: str, identifier: str, *, replace: bool = False) -> None:
        validated_alias(name)
        self.verify(validated_id(identifier))
        with file_lock(self.root / "locks" / "aliases.lock"):
            aliases = self.aliases()
            if name in aliases and aliases[name] != identifier and not replace:
                raise ConflictError(f"Alias {name!r} already exists; use --replace to move it.")
            aliases[name] = identifier
            atomic_write(self.root / "aliases.json", canonical_json(AliasIndex(aliases=aliases)))

    def delete_alias(self, name: str) -> None:
        validated_alias(name)
        with file_lock(self.root / "locks" / "aliases.lock"):
            aliases = self.aliases()
            if name not in aliases:
                raise NotFoundError(f"Alias does not exist: {name}")
            del aliases[name]
            atomic_write(self.root / "aliases.json", canonical_json(AliasIndex(aliases=aliases)))
