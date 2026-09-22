import io
import tarfile
from pathlib import Path

import pytest

from dryheave.assessments import assess_run
from dryheave.bundles import export_bundle, import_bundle
from dryheave.cases import load_frozen_case
from dryheave.errors import ConflictError, IntegrityError
from dryheave.experiments import create_experiment
from dryheave.logs.base import Session
from dryheave.logs.service import import_session
from dryheave.models import ObjectKind
from dryheave.personas import freeze_persona, load_frozen_persona
from dryheave.reports import report_run
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore


def physical_header(kind, size=0):
    header = tarfile.TarInfo("extension")
    header.type = kind
    header.size = size
    return header.tobuf(format=tarfile.GNU_FORMAT)


def pax_record(key, value):
    body = f" {key}={value}\n".encode()
    size = len(body) + 1
    while len(str(size)) + len(body) != size:
        size = len(str(size)) + len(body)
    return str(size).encode() + body


@pytest.mark.parametrize(
    "kind", [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK]
)
@pytest.mark.parametrize("attack", ["chain", "huge-size"])
def test_tar_extension_limits_precede_parser_and_return_structured_errors(
    tmp_path, monkeypatch, kind, attack, capsys
):
    import json

    from dryheave.cli import main

    content = physical_header(kind) * 1200 if attack == "chain" else physical_header(kind, 2**50)
    source = tmp_path / "malicious.tar"
    source.write_bytes(content + bytes(1024))
    target = tmp_path / "target"

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unsafe extensions reached the recursive tar parser.")

    monkeypatch.setattr("dryheave.bundles.tarfile.open", forbidden)
    assert main(["--store", str(target), "import", str(source), "--json"]) == 2
    response = capsys.readouterr()
    assert response.out == ""
    assert json.loads(response.err)["error"] == {
        "code": "limit_exceeded",
        "message": "Bundle exceeds its tar extension metadata bounds.",
    }
    assert not (target / "objects").exists()


@pytest.mark.parametrize(
    "key", ["size", "GNU.sparse.size", "GNU.sparse.map", "GNU.sparse.major", "SCHILY.realsize"]
)
def test_tar_structural_overrides_cannot_change_preflight_boundaries(tmp_path, monkeypatch, key):
    record = pax_record(key, "0")
    source = tmp_path / "structural.tar"
    source.write_bytes(
        physical_header(tarfile.XHDTYPE, len(record))
        + record
        + bytes((-len(record)) % 512)
        + physical_header(tarfile.REGTYPE, 1024)
        + physical_header(tarfile.XHDTYPE) * 2
        + bytes(1024)
    )
    target = ObjectStore(tmp_path / "target")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Structural overrides reached the recursive tar parser.")

    monkeypatch.setattr("dryheave.bundles.tarfile.open", forbidden)
    with pytest.raises(IntegrityError, match="structural or sparse overrides"):
        import_bundle(target, source)
    assert not (target.root / "objects").exists()


def test_bounded_pax_long_paths_round_trip_exact_exported_bytes(store, benchmark, tmp_path):
    identifier = create_experiment(store, benchmark)
    path = tmp_path / "long-paths.tar"
    manifest = export_bundle(store, identifier, path)
    with tarfile.open(path) as archive:
        paths = [member.pax_headers["path"] for member in archive if "path" in member.pax_headers]
    assert paths
    assert all(len(name) > 100 for name in paths)
    target = ObjectStore(tmp_path / "imported")
    assert import_bundle(target, path) == manifest
    for object_id in manifest.objects:
        assert target.read_blobs(object_id) == store.read_blobs(object_id)


@pytest.fixture
def multi_source_case(store, benchmark, tmp_path):
    case = load_frozen_case(store, benchmark.cases[0])
    primary = store.load(case.source_session_id, Session)
    sessions = [case.source_session_id]
    for index in range(3):
        source = tmp_path / f"secondary-{index}.jsonl"
        source.write_bytes((Path(__file__).parent / "fixtures/codex-recorded.jsonl").read_bytes())
        sessions.append(import_session(store, source, primary.agent))
    excerpts = [
        case.evidence[0].model_copy(update={"session_id": identifier, "visibility": "subject"})
        for identifier in sessions[1:]
    ]
    persona = load_frozen_persona(store, case.persona_id)
    persona_id = freeze_persona(store, persona.model_copy(update={"examples": (excerpts[2],)}))
    case = case.model_copy(
        update={
            "persona_id": persona_id,
            "evidence": (*case.evidence, excerpts[0]),
            "allowed_facts": (
                case.allowed_facts[0].model_copy(update={"evidence": (excerpts[1], excerpts[2])}),
            ),
        }
    )
    identifier = store.put(
        ObjectKind.CASE,
        case,
        files=store.read_blobs(benchmark.cases[0]),
        references=(persona_id, case.repository_id),
    )
    return identifier, persona_id, tuple(sessions)


@pytest.mark.parametrize("kind", ["case", "persona"])
def test_explicit_source_sessions_include_embedded_evidence_only_when_selected(
    store, multi_source_case, tmp_path, kind
):
    case_id, persona_id, sessions = multi_source_case
    root = case_id if kind == "case" else persona_id
    expected = set(sessions if kind == "case" else sessions[-1:])
    curated = export_bundle(store, root, tmp_path / "curated.tar")
    assert set(sessions).isdisjoint(curated.objects)
    assert "full source transcripts" in curated.omitted
    path = tmp_path / "sources.tar"
    selected = export_bundle(store, root, path, include_sensitive=("source-sessions",))
    target = ObjectStore(tmp_path / "imported")
    restored = import_bundle(target, path)
    assert {
        identifier
        for identifier in restored.objects
        if target.get(identifier).kind == ObjectKind.SESSION
    } == expected
    assert len(selected.roots) == len(set(selected.roots))
    assert "full source transcripts" not in selected.omitted


def test_missing_secondary_sessions_remain_disclosed_on_reexport(
    store, multi_source_case, tmp_path
):
    case_id, _, sessions = multi_source_case
    path = tmp_path / "curated.tar"
    export_bundle(store, case_id, path)
    target = ObjectStore(tmp_path / "imported")
    import_bundle(target, path)
    for identifier in sessions[:-1]:
        assert target.put(ObjectKind.SESSION, store.load(identifier, Session)) == identifier
    selected = export_bundle(
        target, case_id, tmp_path / "partial.tar", include_sensitive=("source-sessions",)
    )
    assert set(sessions) & set(selected.objects) == set(sessions[:-1])
    assert "source-sessions" in selected.included
    assert "full source transcripts" in selected.omitted
    assert f"source-sessions unavailable: {sessions[-1]}" in selected.omitted


def test_corrupt_available_secondary_session_is_an_export_error(store, multi_source_case, tmp_path):
    case_id, _, sessions = multi_source_case
    (store.object_path(sessions[-1]) / "manifest.json").write_bytes(b"{}")
    with pytest.raises(IntegrityError):
        export_bundle(
            store, case_id, tmp_path / "corrupt.tar", include_sensitive=("source-sessions",)
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "selection, present, absent",
    [
        (("captures",), "simulator requests and responses", "judge requests and responses"),
        (
            ("assessment-evidence",),
            "judge requests and responses",
            "simulator requests and responses",
        ),
    ],
)
def test_individual_sensitive_classes_disclose_their_actual_boundaries(
    store, graded_benchmark, tmp_path, selection, present, absent
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    assess_run(store, summary.run_id)
    path = tmp_path / "selected.tar"
    manifest = export_bundle(store, summary.run_id, path, include_sensitive=selection)
    assert absent in manifest.omitted
    assert present not in manifest.omitted
    target = ObjectStore(tmp_path / "imported")
    restored = import_bundle(target, path)
    assert restored == manifest
    kinds = {target.get(identifier).kind for identifier in restored.objects}
    expected = ObjectKind.CAPTURE if selection == ("captures",) else ObjectKind.ASSESSMENT_EVIDENCE
    assert expected in kinds
    assert report_run(target, restored.roots[0]).attempts[0].result.completion == "pass"


def test_bundle_mutation_and_path_traversal_publish_no_objects(store, benchmark, tmp_path):
    experiment_id = create_experiment(store, benchmark)
    source = tmp_path / "source.tar"
    export_bundle(store, experiment_id, source)
    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(source) as original, tarfile.open(unsafe, "w") as archive:
        for member in original:
            archive.addfile(member, original.extractfile(member))
        entry = tarfile.TarInfo("../escape")
        entry.size = 4
        archive.addfile(entry, io.BytesIO(b"evil"))
    target = ObjectStore(tmp_path / "target")
    with pytest.raises(IntegrityError, match="unsafe path"):
        import_bundle(target, unsafe)
    assert not (target.root / "objects").exists()
    tampered = tmp_path / "tampered.tar"
    changed = False
    with tarfile.open(source) as original, tarfile.open(tampered, "w") as archive:
        for member in original:
            data = original.extractfile(member).read()
            if not changed and "/blobs/" in member.name:
                data = b"x" + data[1:]
                changed = True
            archive.addfile(member, io.BytesIO(data))
    assert changed
    with pytest.raises(IntegrityError, match="hash mismatch"):
        import_bundle(target, tampered)
    assert not (target.root / "objects").exists()


def test_export_is_atomic_and_alias_collision_is_explicit(store, benchmark, tmp_path, monkeypatch):
    identifier = create_experiment(store, benchmark)
    store.set_alias("experiment", identifier)
    destination = tmp_path / "interrupted.tar"
    from dryheave import bundles

    member = bundles._member
    calls = 0

    def interrupt(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("fixture interruption")
        member(*args)

    monkeypatch.setattr(bundles, "_member", interrupt)
    with pytest.raises(OSError, match="interruption"):
        export_bundle(store, identifier, destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".bundle-*")) == []
    monkeypatch.setattr(bundles, "_member", member)
    export_bundle(store, identifier, destination, aliases=("experiment",))
    other = create_experiment(store, benchmark.model_copy(update={"seed": 3}))
    store.set_alias("experiment", other, replace=True)
    with pytest.raises(ConflictError, match="collides"):
        import_bundle(store, destination)
    import_bundle(store, destination, alias_policy="skip")
    assert store.resolve("experiment") == other
    import_bundle(store, destination, alias_policy="replace")
    assert store.resolve("experiment") == identifier


@pytest.mark.integration
def test_capture_selection_exports_durable_output_before_assessment(
    store, graded_benchmark, tmp_path
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    path = tmp_path / "captured.tar"
    manifest = export_bundle(
        store, summary.run_id, path, include_sensitive=("captures", "assessment-evidence")
    )
    capture_id = summary.attempts[0].capture_id
    assert capture_id in manifest.objects
    assert "captures" in manifest.included
    assert "assessment-evidence" not in manifest.included
    assert "verifier stdout/stderr" in manifest.omitted
    assert any(
        item.startswith("assessment-evidence unavailable for attempt") for item in manifest.omitted
    )
    assert "final files and patches" not in manifest.omitted
    target = ObjectStore(tmp_path / "imported")
    restored = import_bundle(target, path)
    assert target.get(capture_id).kind == ObjectKind.CAPTURE
    report = report_run(target, restored.roots[0])
    assert report.attempts[0].capture_id == capture_id
    assert report.attempts[0].raw_evidence == "available"
    assert report.attempts[0].result is None
    assert report.omissions == manifest.omitted


def test_unavailable_capture_selection_remains_disclosed(store, graded_benchmark, tmp_path):
    from dryheave.journals import RunStore
    from dryheave.runner_state import ExecutionJournal

    identifier = create_experiment(store, graded_benchmark)
    run_id = RunStore(store.root).create(identifier)
    from dryheave.experiments import load_experiment

    with RunStore(store.root).open(run_id) as journal:
        execution = ExecutionJournal(journal)
        execution.configure(RunOptions(mode="offline-fixture"))
        execution.reserve(load_experiment(store, identifier).trials[0].trial_id)
    manifest = export_bundle(
        store, run_id, tmp_path / "missing.tar", include_sensitive=("captures",)
    )
    assert "captures" not in manifest.included
    assert "final files and patches" in manifest.omitted
    assert any(item.startswith("captures unavailable for attempt") for item in manifest.omitted)


@pytest.mark.integration
def test_quarantine_capture_can_be_selected_before_assessment(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.fixture_subject import FixtureTerminal

    case_id = graded_benchmark.cases[0]
    blob = store.object_path(case_id) / "blobs" / store.get(case_id).files["verifiers/hidden.py"]
    close = FixtureTerminal.close

    def corrupt_after_subject(terminal):
        cleanup = close(terminal)
        blob.write_text("invalid frozen verifier bytes")
        return cleanup

    monkeypatch.setattr(FixtureTerminal, "close", corrupt_after_subject)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    quarantine_id = summary.attempts[0].quarantine_id
    assert quarantine_id
    path = tmp_path / "quarantine.tar"
    manifest = export_bundle(store, summary.run_id, path, include_sensitive=("captures",))
    assert quarantine_id in manifest.objects
    assert "captures" in manifest.included
    assert (
        "invalid frozen input closure; only its original identity is retained" in manifest.omitted
    )
    target = ObjectStore(tmp_path / "imported")
    restored = import_bundle(target, path)
    assert target.get(quarantine_id).kind == ObjectKind.QUARANTINE
    report = report_run(target, restored.roots[0])
    assert report.attempts[0].quarantine_id == quarantine_id
    assert report.attempts[0].result is None
    assert report.attempts[0].current_exclusions == ("current_inputs_invalid",)
