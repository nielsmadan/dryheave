from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.inspection_scratch import writers_confirmed_stopped

WRITER = ProcessIdentity(pid=4_000_001, created=1.0)


def record(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def report(stopped):
    return CleanupReport(
        terminal_closed=True, known_writers_stopped=stopped, owned=(WRITER,)
    ).model_dump_json()


def test_only_dryheave_cleanup_records_decide_and_subject_files_are_ignored(tmp_path):
    criterion = tmp_path / "verifiers" / "criteria" / "greeting" / "final"
    record(criterion / "greeting-evidence" / "000003-cleanup.json", report(True))
    record(tmp_path / "judge" / "j-1" / "000002-cleanup.json", report(True))
    record(criterion / "workspace" / "000001-cleanup.json", "[1, 2]")
    record(criterion / "workspace" / "deep" / "x-cleanup.json", report(False))
    record(tmp_path / "capture-0" / "files" / "000001-cleanup.json", report(False))
    assert writers_confirmed_stopped(tmp_path) is True
    record(tmp_path / "judge" / "j-2" / "000001-cleanup.json", report(False))
    assert writers_confirmed_stopped(tmp_path) is False


def test_malformed_dryheave_record_is_unconfirmed_without_raising(tmp_path):
    record(tmp_path / "judge" / "j-1" / "000001-cleanup.json", "[1, 2]")
    assert writers_confirmed_stopped(tmp_path) is False


def test_unstopped_record_resolves_once_its_recorded_writers_are_gone(tmp_path):
    record(tmp_path / "judge" / "j-1" / "000001-cleanup.json", report(False))
    assert (
        writers_confirmed_stopped(tmp_path),
        writers_confirmed_stopped(tmp_path, lambda identity: identity == WRITER),
        writers_confirmed_stopped(tmp_path, lambda identity: False),
    ) == (False, False, True)
