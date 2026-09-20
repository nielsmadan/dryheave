import base64
import re

import pytest

from conftest import HOSTILE
from dryheave import journals, storage, viewer
from dryheave.assessments import assess_run
from dryheave.bundles import export_bundle, import_bundle
from dryheave.calibrations import calibrate_case
from dryheave.errors import InputError, LimitError, NotFoundError
from dryheave.experiments import create_experiment
from dryheave.models import ObjectKind
from dryheave.result_models import Assessment, AssessmentEvidence
from dryheave.runner import run_experiment
from dryheave.runner_models import CapturedAttempt, QuarantinedCapture, RunOptions
from dryheave.serialization import digest
from dryheave.storage import ObjectStore
from dryheave.viewer import ViewerService, resolve_target
from dryheave.viewer_models import RunListing


@pytest.fixture
def assessed(store, graded_benchmark):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    assess_run(store, summary.run_id)
    return summary


def _capture_blob(store, service, run_id, attempt_id, name):
    patch = service.patch("run", run_id, attempt_id)
    assert patch.capture_id is not None
    blob = store.read_envelope(patch.capture_id).files[name]
    return store.root / "objects" / patch.capture_id / "blobs" / blob


def test_empty_store_lists_no_runs_and_rejects_bad_pages(store):
    service = ViewerService(store)
    assert service.runs() == RunListing(runs=(), offset=0, limit=25, total=0)
    for kwargs in ({"offset": -1}, {"limit": 0}, {"limit": 101}):
        with pytest.raises(InputError, match=r"offset|limit"):
            service.runs(**kwargs)
    with pytest.raises(NotFoundError, match="does not exist"):
        service.progress("0" * 32)
    with pytest.raises(InputError, match="run ID"):
        service.run_report("not-a-run")


def test_listing_paginates_and_corrupt_rows_degrade(store, graded_benchmark):
    first = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    second = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    (store.root / "runs" / second.run_id / "metadata.json").write_bytes(b"tampered")
    service = ViewerService(store)
    page = service.runs(offset=0, limit=1)
    assert (page.total, len(page.runs), page.offset, page.limit) == (2, 1, 0, 1)
    rows = {row.run_id: row for row in service.runs().runs}
    healthy = rows[first.run_id]
    assert healthy.error is None
    assert healthy.experiment_id == first.experiment_id
    assert healthy.attempts == 1
    assert healthy.finished == 0
    assert healthy.mode == "offline-fixture"
    assert healthy.durable_sequence > 0
    corrupt = rows[second.run_id]
    assert corrupt.error == "integrity_error"
    assert corrupt.experiment_id is None
    assert str(store.root) not in corrupt.error


def test_progress_and_report_read_journal_and_typed_results(store, assessed):
    service = ViewerService(store)
    progress = service.progress(assessed.run_id)
    assert progress.durable_sequence == service.runs().runs[0].durable_sequence
    assert progress.mode == "offline-fixture"
    assert [attempt.stage for attempt in progress.attempts] == ["finished"]
    assert progress.attempts[0].assessment_id
    report = service.run_report(assessed.run_id)
    assert report.run_id == assessed.run_id
    assert report.portable is False
    assert report.groups[0].eligible == 1
    assert report.attempts[0].raw_evidence == "available"


def test_dialogue_serves_retained_transcript_as_inert_text(store, assessed):
    service = ViewerService(store)
    attempt_id = assessed.attempts[0].attempt_id
    dialogue = service.dialogue("run", assessed.run_id, attempt_id)
    assert dialogue.status == "ok"
    assert dialogue.quarantined is False
    assert dialogue.quarantine_id is None
    assert dialogue.interaction_coverage == "observed"
    assert dialogue.capture_errors == ()
    assert dialogue.evidence_omissions == ()
    messages = dialogue.messages
    assert (messages[0].role, messages[0].text) == (
        "user",
        "Fix the greeting; ask me which punctuation.",
    )
    assert messages[-1].text == "Done."
    assert len(messages) == 4
    with pytest.raises(NotFoundError, match="no retained attempt"):
        service.dialogue("run", assessed.run_id, "a-unknown")
    with pytest.raises(InputError, match="run ID"):
        service.dialogue("run", "bogus", attempt_id)


def test_hostile_transcript_patch_and_evidence_round_trip_verbatim(store, hostile_run):
    service = ViewerService(store)
    run_id, attempt_id = hostile_run
    dialogue = service.dialogue("run", run_id, attempt_id)
    assert dialogue.status == "ok"
    assert [message.text for message in dialogue.messages[:2]] == [HOSTILE, HOSTILE]
    patch = service.patch("run", run_id, attempt_id)
    assert patch.status == "ok"
    assert patch.encoding == "utf-8"
    assert HOSTILE in patch.content
    assert patch.sha256 == digest(patch.content.encode())
    listing = service.evidence("run", run_id, attempt_id)
    stdout_files = [
        name for name in listing.files if name.endswith("final/greeting-evidence/stdout.bin")
    ]
    assert stdout_files
    served = service.evidence_file("run", run_id, attempt_id, stdout_files[0])
    assert served.status == "ok"
    assert served.encoding == "base64"
    raw = base64.b64decode(served.content)
    assert HOSTILE.encode() in raw
    assert b"\xed\xa0\x80" in raw
    assert served.bytes == len(raw)
    assert served.sha256 == digest(raw)


def test_lone_surrogates_cannot_enter_a_retained_payload(store):
    with pytest.raises(InputError, match="canonical UTF-8 JSON"):
        store.put(
            ObjectKind.ASSESSMENT_EVIDENCE,
            AssessmentEvidence(run_id="0" * 32, attempt_id="a-1", files={}, omissions=("\ud800",)),
        )


def test_patch_reads_bounded_and_refuses_over_budget(store, assessed):
    service = ViewerService(store)
    attempt_id = assessed.attempts[0].attempt_id
    patch = service.patch("run", assessed.run_id, attempt_id)
    assert patch.status == "ok"
    assert patch.name == "final.patch"
    assert patch.encoding == "utf-8"
    assert '+    return "Hello, " + name' in patch.content
    assert patch.bytes == len(patch.content.encode())
    assert patch.sha256 == digest(patch.content.encode())
    assert patch.workspace_complete is True
    tight = ViewerService(store, max_detail_bytes=8)
    refused = tight.patch("run", assessed.run_id, attempt_id)
    assert refused.status == "refused"
    assert refused.reason_code == "serve_limit"
    assert refused.limit_bytes == 8
    assert refused.name == "final.patch"
    assert {key for key, value in refused.model_dump(mode="json").items() if value is not None} == {
        "attempt_id",
        "capture_id",
        "quarantined",
        "status",
        "reason",
        "reason_code",
        "name",
        "workspace_complete",
        "omissions",
        "limit_bytes",
    }
    assert refused.content is None
    assert str(store.root) not in refused.reason


def test_deleted_capture_blob_is_reported_as_invalid_without_paths(store, assessed):
    service = ViewerService(store)
    attempt_id = assessed.attempts[0].attempt_id
    _capture_blob(store, service, assessed.run_id, attempt_id, "final.patch").unlink()
    patch = service.patch("run", assessed.run_id, attempt_id)
    assert patch.status == "invalid"
    assert patch.reason_code == "integrity_error"
    assert patch.name == "final.patch"
    assert patch.content is None
    assert str(store.root) not in patch.reason


def test_detail_content_distinguishes_text_from_binary(store):
    evidence = AssessmentEvidence(run_id="0" * 32, attempt_id="a-1", files={})
    identifier = store.put(
        ObjectKind.ASSESSMENT_EVIDENCE,
        evidence,
        files={"checks/out.bin": b"\xff\xfe\x00", "checks/out.txt": "héllo".encode()},
    )
    service = ViewerService(store)
    binary = service._read_detail(identifier, "checks/out.bin")
    assert binary.encoding == "base64"
    assert base64.b64decode(binary.content) == b"\xff\xfe\x00"
    text = service._read_detail(identifier, "checks/out.txt")
    assert text.encoding == "utf-8"
    assert text.content == "héllo"


def test_evidence_lists_and_serves_declared_files_only(store, assessed):
    service = ViewerService(store)
    attempt_id = assessed.attempts[0].attempt_id
    listing = service.evidence("run", assessed.run_id, attempt_id)
    assert listing.status == "ok"
    assert listing.complete is True
    assert listing.evidence_id != listing.assessment_id
    assert list(listing.files) == sorted(listing.files)
    result_files = [name for name in listing.files if name.endswith("result.json")]
    assert result_files
    served = service.evidence_file("run", assessed.run_id, attempt_id, result_files[0])
    assert served.status == "ok"
    assert served.evidence_id == listing.evidence_id
    assert served.bytes == len(served.content.encode())
    assert '"outcome"' in served.content
    with pytest.raises(NotFoundError, match="no file named"):
        service.evidence_file("run", assessed.run_id, attempt_id, "checks/missing.json")
    for name in ("../manifest.json", "checks/../../blobs/x", "a\\b"):
        with pytest.raises(InputError, match="normalized relative path"):
            service.evidence_file("run", assessed.run_id, attempt_id, name)


def test_corrupt_evidence_object_keeps_its_own_identifier(store, assessed):
    service = ViewerService(store)
    attempt_id = assessed.attempts[0].attempt_id
    listing = service.evidence("run", assessed.run_id, attempt_id)
    manifest = store.root / "objects" / listing.evidence_id / "manifest.json"
    manifest.write_bytes(b"tampered")
    broken = service.evidence("run", assessed.run_id, attempt_id)
    assert broken.status == "invalid"
    assert broken.evidence_id == listing.evidence_id
    assert broken.assessment_id == listing.assessment_id
    assert str(store.root) not in broken.reason


def test_portable_report_with_omitted_captures_is_a_degraded_view(store, assessed, tmp_path):
    bundle = tmp_path / "summary.tar"
    export_bundle(store, assessed.run_id, bundle)
    imported = ObjectStore(tmp_path / "imported")
    report_id = import_bundle(imported, bundle).roots[0]
    service = ViewerService(imported)
    report = service.portable_report(report_id)
    assert report.portable is True
    assert report.omissions
    assert report.attempts[0].raw_evidence == "unavailable"
    assert report.groups[0].eligible == 1
    attempt_id = report.attempts[0].attempt_id
    dialogue = service.dialogue("report", report_id, attempt_id)
    assert dialogue.status == "unavailable"
    assert dialogue.messages == ()
    assert dialogue.reason_code == "not_found"
    patch = service.patch("report", report_id, attempt_id)
    assert patch.status == "unavailable"
    evidence = service.evidence("report", report_id, attempt_id)
    assert evidence.status == "unavailable"
    assert evidence.files == ()
    with pytest.raises(NotFoundError, match="no retained attempt"):
        service.dialogue("report", report_id, "a-unknown")
    with pytest.raises(InputError, match="SHA-256"):
        service.portable_report("not-an-object")


def test_compare_reuses_report_semantics(store, assessed):
    service = ViewerService(store)
    comparison = service.compare(assessed.run_id, assessed.run_id)
    assert comparison.paired_count == 1
    assert comparison.pairs[0].before_completion == "pass"
    with pytest.raises(InputError, match="both comparison variants"):
        service.compare(assessed.run_id, assessed.run_id, before_variant="base")


def test_calibrations_match_exact_case_identity(store, graded_benchmark):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibration_id = calibrate_case(store, case_id)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    service = ViewerService(store)
    result = service.calibrations(summary.run_id)
    assert result.input_error is None
    assert set(result.calibrations) == {case_id}
    entries = result.calibrations[case_id]
    assert [entry.calibration_id for entry in entries] == [calibration_id]
    assert entries[0].verification == "verified"
    assert entries[0].status == "demonstrated"
    assert entries[0].evidence_complete is True
    assert result.scan.complete is True
    assert result.scan.unreadable == 0
    assert result.scan.attempted == 1
    assert result.scan.verified == 1
    assert result.scan.scanned > 0
    bounded = ViewerService(store, max_scan_objects=1)
    limited = bounded.calibrations(summary.run_id)
    assert limited.scan.complete is False
    assert limited.scan.scanned == 1


def test_calibration_rows_report_verification_state(store, graded_benchmark):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibration_id = calibrate_case(store, case_id)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    unverified = ViewerService(store, max_verified_calibrations=0).calibrations(summary.run_id)
    entry = unverified.calibrations[case_id][0]
    assert entry.verification == "unverified"
    assert entry.reason_code == "verification_budget"
    assert entry.status == "demonstrated"
    assert unverified.scan.attempted == 0
    assert unverified.scan.verified == 0
    blobs = store.root / "objects" / calibration_id / "blobs"
    next(iter(sorted(blobs.iterdir()))).write_bytes(b"PRIVATE_VERIFIER_SENTINEL\n")
    corrupted = ViewerService(store).calibrations(summary.run_id)
    broken = corrupted.calibrations[case_id][0]
    assert broken.verification == "failed"
    assert broken.reason_code == "integrity_error"
    assert broken.status is None
    assert str(store.root) not in broken.reason
    assert corrupted.scan.attempted == 1
    assert corrupted.scan.verified == 0


def test_calibration_scan_is_incomplete_when_case_inputs_are_unreadable(store, graded_benchmark):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    journal = store.root / "runs" / summary.run_id
    experiment_id = summary.experiment_id
    (store.root / "objects" / experiment_id / "manifest.json").write_bytes(b"tampered")
    assert journal.exists()
    result = ViewerService(store).calibrations(summary.run_id)
    assert result.input_error is not None
    assert result.calibrations == {}
    assert result.scan.complete is False
    assert result.scan.scanned == 0
    assert str(store.root) not in result.input_error


def test_resolve_target_prefers_runs_but_still_finds_32_hex_aliases(store, assessed, tmp_path):
    assert resolve_target(store, assessed.run_id).kind == "run"
    bundle = tmp_path / "summary.tar"
    export_bundle(store, assessed.run_id, bundle)
    imported = ObjectStore(tmp_path / "imported")
    report_id = import_bundle(imported, bundle).roots[0]
    alias = "deadbeef" * 4
    imported.set_alias(alias, report_id)
    target = resolve_target(imported, alias)
    assert (target.kind, target.id) == ("report", report_id)
    with pytest.raises(NotFoundError, match="Run does not exist"):
        resolve_target(store, "0" * 32)


def _portable(store, report, attempt):
    return store.put(
        ObjectKind.PORTABLE_REPORT,
        report.model_copy(update={"portable": True, "attempts": (attempt,)}),
    )


@pytest.fixture
def portable(store, assessed):
    report = ViewerService(store).run_report(assessed.run_id)
    return report, report.attempts[0]


def test_quarantined_captures_and_unexpected_kinds_are_reported_distinctly(store, portable):
    report, attempt = portable
    capture = store.read_payload(attempt.capture_id, CapturedAttempt, kind=ObjectKind.CAPTURE)
    quarantine_id = store.put(
        ObjectKind.QUARANTINE,
        QuarantinedCapture(failure="The subject terminal stopped early.", capture=capture),
        files=store.read_blobs(attempt.capture_id),
    )
    quarantined = _portable(
        store,
        report,
        attempt.model_copy(update={"capture_id": None, "quarantine_id": quarantine_id}),
    )
    service = ViewerService(store)
    view = service.dialogue("report", quarantined, attempt.attempt_id)
    assert view.status == "ok"
    assert view.quarantined is True
    assert (view.quarantine_id, view.capture_id) == (quarantine_id, None)
    assert view.messages[-1].text == "Done."
    patch = service.patch("report", quarantined, attempt.attempt_id)
    assert patch.status == "ok"
    assert patch.quarantine_id == quarantine_id
    foreign = _portable(
        store,
        report,
        attempt.model_copy(update={"capture_id": None, "quarantine_id": report.experiment_id}),
    )
    unexpected = service.dialogue("report", foreign, attempt.attempt_id)
    assert unexpected.status == "invalid"
    assert unexpected.reason_code == "integrity_error"
    assert unexpected.quarantine_id == report.experiment_id
    assert str(store.root) not in unexpected.reason


def test_capture_identity_mismatch_is_rejected(store, portable):
    report, attempt = portable
    renamed = _portable(store, report, attempt.model_copy(update={"attempt_id": "a-other"}))
    view = ViewerService(store).dialogue("report", renamed, "a-other")
    assert view.status == "invalid"
    assert view.reason_code == "integrity_error"
    assert view.capture_id == attempt.capture_id
    assert view.messages == ()
    assert str(store.root) not in view.reason


def test_assessment_and_evidence_identity_mismatches_are_rejected(store, portable):
    report, attempt = portable
    service = ViewerService(store)
    renamed = _portable(store, report, attempt.model_copy(update={"attempt_id": "a-other"}))
    mismatched = service.evidence("report", renamed, "a-other")
    assert mismatched.status == "invalid"
    assert mismatched.reason_code == "integrity_error"
    assert mismatched.assessment_id == attempt.assessment_id
    assert mismatched.evidence_id is None
    assessment = store.read_payload(attempt.assessment_id, Assessment, kind=ObjectKind.RESULT)
    evidence = store.read_payload(
        assessment.evidence_id, AssessmentEvidence, kind=ObjectKind.ASSESSMENT_EVIDENCE
    )
    foreign_evidence = store.put(
        ObjectKind.ASSESSMENT_EVIDENCE,
        evidence.model_copy(update={"attempt_id": "a-other"}),
        files=store.read_blobs(assessment.evidence_id),
    )
    relinked = store.put(
        ObjectKind.RESULT, assessment.model_copy(update={"evidence_id": foreign_evidence})
    )
    swapped = _portable(store, report, attempt.model_copy(update={"assessment_id": relinked}))
    broken = service.evidence("report", swapped, attempt.attempt_id)
    assert broken.status == "invalid"
    assert broken.reason_code == "integrity_error"
    assert broken.assessment_id == relinked
    assert broken.evidence_id == foreign_evidence
    assert broken.files == ()


def test_assessment_without_retained_evidence_reports_its_own_identifier(store, portable):
    report, attempt = portable
    assessment = store.read_payload(attempt.assessment_id, Assessment, kind=ObjectKind.RESULT)
    stripped = store.put(ObjectKind.RESULT, assessment.model_copy(update={"evidence_id": None}))
    identifier = _portable(store, report, attempt.model_copy(update={"assessment_id": stripped}))
    listing = ViewerService(store).evidence("report", identifier, attempt.attempt_id)
    assert listing.status == "unavailable"
    assert listing.reason_code == "not_retained"
    assert listing.assessment_id == stripped
    assert listing.evidence_id is None
    assert listing.complete is None


def test_corrupt_capture_object_and_blob_are_reported_as_invalid(store, assessed):
    service = ViewerService(store)
    attempt_id = assessed.attempts[0].attempt_id
    blob = _capture_blob(store, service, assessed.run_id, attempt_id, "final.patch")
    blob.write_bytes(b"replaced by a different patch\n")
    patch = service.patch("run", assessed.run_id, attempt_id)
    assert patch.status == "invalid"
    assert patch.reason_code == "integrity_error"
    assert patch.content is None
    assert str(store.root) not in patch.reason
    capture_id = patch.capture_id
    (store.root / "objects" / capture_id / "manifest.json").write_bytes(b"tampered")
    dialogue = service.dialogue("run", assessed.run_id, attempt_id)
    assert dialogue.status == "invalid"
    assert dialogue.reason_code == "integrity_error"
    assert dialogue.capture_id == capture_id
    assert dialogue.messages == ()


def test_listing_bounds_the_journal_bytes_it_reads(store, assessed):
    exact = ViewerService(store).runs().runs[0]
    assert exact.truncated is False
    assert exact.durable_sequence == exact.observed_events > 0
    bounded = ViewerService(store, max_request_journal_bytes=64).runs().runs[0]
    assert bounded.truncated is True
    assert bounded.durable_sequence is None
    assert bounded.observed_events < exact.observed_events
    assert bounded.experiment_id == assessed.experiment_id
    assert bounded.error is None


def test_detail_routes_bound_the_journal_bytes_they_read(store, assessed):
    service = ViewerService(store)
    attempt_id = service.progress(assessed.run_id).attempts[0].attempt_id
    bounded = ViewerService(store, max_request_journal_bytes=64)
    calls = (
        lambda: bounded.progress(assessed.run_id),
        lambda: bounded.run_report(assessed.run_id),
        lambda: bounded.compare(assessed.run_id, assessed.run_id),
        lambda: bounded.dialogue("run", assessed.run_id, attempt_id),
        lambda: bounded.patch("run", assessed.run_id, attempt_id),
        lambda: bounded.evidence("run", assessed.run_id, attempt_id),
        lambda: bounded.evidence_file("run", assessed.run_id, attempt_id, "log.txt"),
    )
    for call in calls:
        with pytest.raises(LimitError, match="64-byte limit"):
            call()
    assert service.dialogue("run", assessed.run_id, attempt_id).status == "ok"
    assert service.progress(assessed.run_id).attempts[0].attempt_id == attempt_id


def test_listing_charges_every_row_for_the_prefix_bytes_it_read(
    store, graded_benchmark, monkeypatch
):
    experiment = create_experiment(store, graded_benchmark)
    first = run_experiment(store, experiment, options=RunOptions(mode="offline-fixture"))
    second = run_experiment(store, experiment, options=RunOptions(mode="offline-fixture"))
    assert first.run_id != second.run_id
    original = journals.read_prefix
    read = []

    def spy(path, *, limit):
        content, truncated = original(path, limit=limit)
        read.append(len(content))
        return content, truncated

    monkeypatch.setattr(journals, "read_prefix", spy)
    listing = ViewerService(store, max_request_journal_bytes=100).runs()
    assert {row.run_id for row in listing.runs} == {first.run_id, second.run_id}
    assert len(read) == 2
    assert read[0] == 100
    assert sum(read) <= 100
    assert all(row.truncated for row in listing.runs)
    assert all(row.observed_events == 0 for row in listing.runs)


def test_listing_and_detail_routes_agree_on_the_run_metadata_limit(store, assessed):
    path = store.root / "runs" / assessed.run_id / "metadata.json"
    path.write_bytes(b"{" + b" " * (80 * 1024) + path.read_bytes().removeprefix(b"{"))
    assert path.stat().st_size > 64 * 1024
    service = ViewerService(store)
    row = next(row for row in service.runs().runs if row.run_id == assessed.run_id)
    assert row.error is None
    assert row.experiment_id == assessed.experiment_id
    assert service.progress(assessed.run_id).experiment_id == assessed.experiment_id


def test_calibration_verification_bounds_the_blob_bytes_it_reads(
    store, graded_benchmark, monkeypatch
):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibrate_case(store, case_id)
    original = storage.read_bytes
    limits = []

    def spy(path, *, limit):
        if path.parent.name == "blobs":
            limits.append(limit)
        return original(path, limit=limit)

    monkeypatch.setattr(storage, "read_bytes", spy)
    view = ViewerService(store, max_detail_bytes=64 * 1024).case_calibrations(case_id)
    assert view.calibrations[0].verification == "verified"
    assert limits
    assert max(limits) <= 64 * 1024


def test_calibration_verification_refuses_evidence_over_its_serve_limit(store, graded_benchmark):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibration_id = calibrate_case(store, case_id)
    view = ViewerService(store, max_detail_bytes=8).case_calibrations(case_id)
    entry = view.calibrations[0]
    assert entry.calibration_id == calibration_id
    assert entry.verification == "unverified"
    assert entry.reason_code == "serve_limit"
    assert entry.status is None
    assert view.scan.attempted == 1
    assert view.scan.verified == 0


def test_resolve_target_bounds_the_journal_bytes_it_reads(store, assessed, monkeypatch):
    monkeypatch.setattr(viewer, "MAX_REQUEST_JOURNAL_BYTES", 64)
    with pytest.raises(LimitError, match="64-byte limit"):
        resolve_target(store, assessed.run_id)


def test_case_calibrations_are_reachable_without_a_run(store, graded_benchmark):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibration_id = calibrate_case(store, case_id)
    service = ViewerService(store)
    view = service.case_calibrations(case_id)
    assert view.case_id == case_id
    assert [entry.calibration_id for entry in view.calibrations] == [calibration_id]
    assert view.calibrations[0].verification == "verified"
    assert view.scan.complete is True
    assert view.scan.next_cursor is None
    with pytest.raises(InputError, match="SHA-256 case ID"):
        service.case_calibrations("not-a-case")
    with pytest.raises(InputError, match="Scan limit"):
        service.case_calibrations(case_id, limit=0)
    with pytest.raises(InputError, match="SHA-256 scan cursor"):
        service.case_calibrations(case_id, after="nope")


def test_calibration_scan_pages_through_objects_and_counts_unreadable(store, graded_benchmark):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibration_id = calibrate_case(store, case_id)
    names = sorted(
        name for name in (store.root / "objects").iterdir() if name.is_dir() for name in [name.name]
    )
    unreadable_id = next(name for name in names if name not in {case_id, calibration_id})
    (store.root / "objects" / unreadable_id / "manifest.json").write_bytes(b"tampered")
    service = ViewerService(store)
    found = []
    cursor = None
    pages = 0
    while pages < len(names) + 1:
        page = service.case_calibrations(case_id, after=cursor, limit=1)
        found.extend(entry.calibration_id for entry in page.calibrations)
        pages += 1
        assert page.scan.scanned == 1
        cursor = page.scan.next_cursor
        if cursor is None:
            break
    assert found == [calibration_id]
    assert pages == len(names)
    whole = service.case_calibrations(case_id)
    assert whole.scan.unreadable == 1
    assert whole.scan.scanned == len(names)
    assert whole.scan.complete is True


def test_alias_index_is_served_as_human_labels_for_immutable_ids(store, assessed):
    service = ViewerService(store)
    before = service.labels().labels
    assert assessed.experiment_id not in before
    store.set_alias("nightly", assessed.experiment_id)
    store.set_alias("baseline", assessed.experiment_id)
    labels = service.labels().labels
    assert labels[assessed.experiment_id] == ("baseline", "nightly")
    assert list(labels) == sorted(labels)
    assert all(re.fullmatch(r"[0-9a-f]{64}", identifier) for identifier in labels)
    assert assessed.run_id not in labels
    assert ViewerService(ObjectStore(store.root / "empty")).labels().labels == {}
