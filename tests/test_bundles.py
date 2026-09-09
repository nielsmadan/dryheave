import io
import tarfile

import pytest

from dryheave.assessments import assess_run
from dryheave.bundles import export_bundle, import_bundle
from dryheave.errors import ConflictError, IntegrityError
from dryheave.experiments import create_experiment
from dryheave.models import ObjectKind
from dryheave.reports import report_run
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore


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
