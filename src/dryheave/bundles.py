import io
import os
import re
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from pydantic import Field

from dryheave.bundle_validation import validate_bundle_object
from dryheave.cases import load_frozen_case
from dryheave.constants import MAX_BLOB_BYTES
from dryheave.errors import (
    ConflictError,
    DryheaveError,
    InputError,
    IntegrityError,
    LimitError,
    NotFoundError,
)
from dryheave.filesystem import atomic_write, ensure_directory, regular_fd
from dryheave.models import Manifest, Name, ObjectId, ObjectKind, StrictModel
from dryheave.personas import load_frozen_persona
from dryheave.report_models import RunReport
from dryheave.reports import report_run
from dryheave.serialization import canonical_json, parse_model
from dryheave.storage import ObjectStore, validated_alias
from dryheave.tar_bounds import preflight_tar

MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_BUNDLE_FILES = 200000
SENSITIVE_CLASSES = ("captures", "assessment-evidence", "source-sessions", "calibration-evidence")
RAW_CLASSES = {
    "raw terminal/native logs and screens": "captures",
    "final files and patches": "captures",
    "raw Git metadata": "captures",
    "simulator requests and responses": "captures",
    "judge requests and responses": "assessment-evidence",
    "verifier stdout/stderr": "assessment-evidence",
    "full source transcripts": "source-sessions",
    "standalone calibration verifier outputs": "calibration-evidence",
}
OMITTED_RAW = tuple(RAW_CLASSES)


class BundleManifest(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["dryheave-bundle"] = "dryheave-bundle"
    roots: tuple[ObjectId, ...]
    objects: tuple[ObjectId, ...]
    included: tuple[str, ...]
    omitted: tuple[str, ...]
    aliases: dict[Name, ObjectId] = Field(default_factory=dict)


def _portable(report: RunReport, roots: list[str], omissions: tuple[str, ...]) -> RunReport:
    return report.model_copy(
        update={
            "portable": True,
            "omissions": omissions,
            "attempts": tuple(
                item.model_copy(
                    update={
                        "raw_evidence": item.raw_evidence
                        if (item.capture_id or item.quarantine_id) in roots
                        else "unavailable"
                    }
                )
                for item in report.attempts
            ),
        }
    )


def _selected_results(
    report: RunReport, roots: list[str], sensitive: tuple[str, ...], omitted: list[str]
) -> set[str]:
    complete = set(sensitive) & {"captures", "assessment-evidence"}
    for category in ("captures", "assessment-evidence"):
        if category not in sensitive:
            continue
        if not report.attempts:
            complete.discard(category)
        for attempt in report.attempts:
            identifier = (
                attempt.capture_id or attempt.quarantine_id
                if category == "captures"
                else attempt.result.evidence_id
                if attempt.result
                else None
            )
            if identifier:
                roots.append(identifier)
            else:
                complete.discard(category)
                omitted.append(f"{category} unavailable for attempt {attempt.attempt_id}")
    return complete


def _roots(
    store: ObjectStore, reference: str, sensitive: tuple[str, ...]
) -> tuple[list[str], tuple[str, ...]]:
    roots = []
    omitted = list(OMITTED_RAW)
    report = None
    complete = set(sensitive)
    if re.fullmatch(r"[0-9a-f]{32}", reference):
        report = report_run(store, reference)
        try:
            store.verify(report.experiment_id)
            roots.append(report.experiment_id)
        except DryheaveError:
            omitted.append("invalid frozen input closure; only its original identity is retained")
        complete = _selected_results(report, roots, sensitive, omitted)
    else:
        identifier = store.resolve(reference)
        manifest = store.get(identifier)
        required = {
            ObjectKind.CAPTURE: "captures",
            ObjectKind.QUARANTINE: "captures",
            ObjectKind.SESSION: "source-sessions",
            ObjectKind.ASSESSMENT_EVIDENCE: "assessment-evidence",
            ObjectKind.CALIBRATION: "calibration-evidence",
        }.get(manifest.kind)
        if required and required not in sensitive:
            raise InputError(f"Raw {required} export requires --include-sensitive {required}.")
        roots.append(identifier)
        complete = {required} if required else set()
    if "source-sessions" in sensitive:
        available, missing = _source_sessions(store, roots)
        complete.discard("source-sessions")
        if available and not missing:
            complete.add("source-sessions")
        if missing:
            omitted.extend(f"source-sessions unavailable: {identifier}" for identifier in missing)
        elif not available:
            omitted.append("source-sessions unavailable in selected input closure")
    omitted = [item for item in omitted if RAW_CLASSES.get(item) not in complete]
    if report is not None:
        roots.insert(
            0, store.put(ObjectKind.PORTABLE_REPORT, _portable(report, roots, tuple(omitted)))
        )
    return list(dict.fromkeys(roots)), tuple(omitted)


def _source_sessions(store: ObjectStore, roots: list[str]) -> tuple[int, tuple[str, ...]]:
    pending = list(roots)
    seen = set()
    sessions = set()
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        manifest = store.get(identifier)
        pending.extend(manifest.references)
        if manifest.kind == ObjectKind.SESSION:
            sessions.add(identifier)
        elif manifest.kind == ObjectKind.CASE:
            case = load_frozen_case(store, identifier)
            sessions.add(case.source_session_id)
            sessions.update(item.session_id for item in case.evidence)
            sessions.update(
                item.session_id for fact in case.allowed_facts for item in fact.evidence
            )
        elif manifest.kind == ObjectKind.PERSONA:
            persona = load_frozen_persona(store, identifier)
            sessions.update(item.session_id for item in persona.examples)
    missing = []
    for session_id in sorted(sessions):
        try:
            store.get(session_id, kind=ObjectKind.SESSION)
        except NotFoundError:
            if store.object_path(session_id).exists():
                raise
            missing.append(session_id)
        else:
            roots.append(session_id)
    return len(sessions) - len(missing), tuple(missing)


def _objects(store: ObjectStore, roots: list[str]) -> dict[str, tuple[Manifest, dict[str, bytes]]]:
    pending = list(roots)
    objects = {}
    total = 0
    while pending:
        identifier = pending.pop()
        if identifier in objects:
            continue
        evidence = store.read_evidence(identifier)
        if evidence.dependency_error:
            raise IntegrityError(evidence.dependency_error)
        total += len(canonical_json(evidence.manifest)) + sum(map(len, evidence.files.values()))
        if total > MAX_BUNDLE_BYTES:
            raise LimitError("Portable object closure exceeds its byte limit.")
        objects[identifier] = (evidence.manifest, evidence.files)
        pending.extend(evidence.manifest.references)
    return objects


def _member(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(content), 0o600
    archive.addfile(info, io.BytesIO(content))


def export_bundle(
    store: ObjectStore,
    reference: str,
    destination: Path,
    *,
    include_sensitive: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
) -> BundleManifest:
    if any(item not in SENSITIVE_CLASSES for item in include_sensitive):
        raise InputError("Unknown sensitive artifact selection.")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ConflictError("Bundle destination already exists.")
    roots, omitted = _roots(store, reference, include_sensitive)
    objects = _objects(store, roots)
    names = {validated_alias(name): store.resolve(name) for name in aliases}
    if any(identifier not in objects for identifier in names.values()):
        raise InputError("Exported aliases must target objects in the selected closure.")
    sensitive_kinds = {
        "captures": {ObjectKind.CAPTURE, ObjectKind.QUARANTINE},
        "assessment-evidence": {ObjectKind.ASSESSMENT_EVIDENCE},
        "source-sessions": {ObjectKind.SESSION},
        "calibration-evidence": {ObjectKind.CALIBRATION},
    }
    exported_kinds = {item.kind for item, _ in objects.values()}
    included = tuple(
        category for category in include_sensitive if exported_kinds & sensitive_kinds[category]
    )
    manifest = BundleManifest(
        roots=tuple(roots),
        objects=tuple(sorted(objects)),
        included=("curated-inputs", "structured-results", *included),
        omitted=omitted,
        aliases=names,
    )
    for identifier in objects:
        if objects[identifier][0].kind == ObjectKind.CALIBRATION:
            validate_bundle_object(store, identifier, manifest.included)
    ensure_directory(destination.parent)
    temporary = destination.parent / (".bundle-" + uuid4().hex)
    try:
        with (
            regular_fd(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as descriptor,
            os.fdopen(os.dup(descriptor), "wb") as stream,
            tarfile.open(fileobj=stream, mode="w") as archive,
        ):
            _member(archive, "bundle.json", canonical_json(manifest))
            for identifier, (object_manifest, files) in objects.items():
                _member(
                    archive, f"objects/{identifier}/manifest.json", canonical_json(object_manifest)
                )
                for sha, content in {
                    object_manifest.files[name]: data for name, data in files.items()
                }.items():
                    _member(archive, f"objects/{identifier}/blobs/{sha}", content)
        with regular_fd(temporary, os.O_RDONLY) as descriptor:
            os.fsync(descriptor)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def _unpack(source: Path, staging: Path) -> BundleManifest:
    try:
        with (
            regular_fd(source, os.O_RDONLY) as descriptor,
            os.fdopen(os.dup(descriptor), "rb") as stream,
        ):
            preflight_tar(stream, max_bytes=MAX_BUNDLE_BYTES, max_files=MAX_BUNDLE_FILES)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                return _unpack_members(archive, staging)
    except tarfile.TarError as error:
        raise IntegrityError("Bundle archive is invalid or truncated.") from error


def _unpack_members(archive: tarfile.TarFile, staging: Path) -> BundleManifest:
    seen = set()
    total = 0
    manifest = None
    for member in archive:
        if not member.isfile() or member.name in seen or member.size < 0:
            raise IntegrityError("Bundle contains a duplicate or non-regular entry.")
        seen.add(member.name)
        total += member.size
        if total > MAX_BUNDLE_BYTES or len(seen) > MAX_BUNDLE_FILES or member.size > MAX_BLOB_BYTES:
            raise LimitError("Bundle exceeds its extraction bounds.")
        reader = archive.extractfile(member)
        if reader is None:
            raise IntegrityError("Bundle entry has no content.")
        content = reader.read(member.size + 1)
        if len(content) != member.size:
            raise IntegrityError("Bundle entry is truncated.")
        if member.name == "bundle.json":
            manifest = parse_model(content, BundleManifest)
            if canonical_json(manifest) != content:
                raise IntegrityError("Bundle manifest must be canonical.")
        else:
            if not re.fullmatch(
                r"objects/[0-9a-f]{64}/(?:manifest\.json|blobs/[0-9a-f]{64})", member.name
            ):
                raise IntegrityError("Bundle contains an unexpected or unsafe path.")
            atomic_write(staging / member.name, content, replace=False)
    if manifest is None:
        raise IntegrityError("Bundle manifest is missing.")
    _verify_inventory(staging, manifest, seen)
    return manifest


def _verify_inventory(staging: Path, manifest: BundleManifest, seen: set[str]) -> None:
    expected = {"bundle.json"}
    staged = ObjectStore(staging)
    if set(manifest.roots) - set(manifest.objects) or len(set(manifest.objects)) != len(
        manifest.objects
    ):
        raise IntegrityError("Bundle object and root inventory is inconsistent.")
    for identifier in manifest.objects:
        ensure_directory(staged.object_path(identifier) / "blobs")
    for identifier in manifest.objects:
        envelope = staged.verify(identifier)
        expected.add(f"objects/{identifier}/manifest.json")
        expected.update(f"objects/{identifier}/blobs/{sha}" for sha in envelope.files.values())
        if set(envelope.references) - set(manifest.objects):
            raise IntegrityError("Bundle reference closure is incomplete.")
    if expected != seen:
        raise IntegrityError("Bundle contains undeclared or missing object bytes.")


def import_bundle(
    store: ObjectStore, source: Path, *, alias_policy: str = "error"
) -> BundleManifest:
    if alias_policy not in {"error", "skip", "replace"}:
        raise InputError("Alias collision policy must be error, skip or replace.")
    scratch = store.root / "scratch"
    ensure_directory(scratch)
    with TemporaryDirectory(prefix="bundle-", dir=scratch) as temporary:
        staging = Path(temporary)
        manifest = _unpack(source, staging)
        for identifier in manifest.objects:
            validate_bundle_object(ObjectStore(staging), identifier, manifest.included)
        current = store.aliases()
        for name, identifier in manifest.aliases.items():
            validated_alias(name)
            if identifier not in manifest.objects:
                raise IntegrityError("Bundle alias names an undeclared object.")
            if alias_policy == "error" and name in current and current[name] != identifier:
                raise ConflictError("Imported alias collides with existing alias: " + name)
        staged = ObjectStore(staging)
        pending = set(manifest.objects)
        while pending:
            ready = [
                identifier
                for identifier in pending
                if not set(staged.get(identifier).references) & pending
            ]
            if not ready:
                raise IntegrityError("Bundle references contain a cycle.")
            for identifier in ready:
                evidence = staged.read_evidence(identifier)
                store.put_manifest(evidence.manifest, evidence.files)
                pending.remove(identifier)
        for name, identifier in manifest.aliases.items():
            if alias_policy != "skip" or name not in current:
                store.set_alias(name, identifier, replace=alias_policy == "replace")
        return manifest
