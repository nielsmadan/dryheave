import json
import os
import time

import pytest

from dryheave.assessments import assess_run, regrade_run
from dryheave.cli import main
from dryheave.compaction import WORKING_DIRECTORIES, compact_run, prune_checks
from dryheave.errors import InputError, LockBusyError
from dryheave.experiments import create_experiment
from dryheave.journals import RunStore
from dryheave.reports import report_run
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.viewer import ViewerService


def offline_run(store, benchmark):
    return run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
    ).run_id


def attempt_roots(store, run_id):
    return sorted((store.root / "runs" / run_id / "attempts").iterdir())


@pytest.mark.integration
def test_compaction_removes_working_copies_and_keeps_results_viewable_and_regradable(
    store, graded_benchmark
):
    run_id = offline_run(store, graded_benchmark)
    assess_run(store, run_id)
    before = report_run(store, run_id)
    (root,) = attempt_roots(store, run_id)
    assert (root / "workspace").is_dir()

    compaction = compact_run(store, run_id)

    assert compaction.attempts == 1
    assert compaction.bytes_freed > 0
    assert f"attempts/{root.name}/workspace" in compaction.removed
    assert not any((root / name).exists() for name in WORKING_DIRECTORIES)
    after = report_run(store, run_id)
    assert [attempt.result for attempt in after.attempts] == [
        attempt.result for attempt in before.attempts
    ]
    attempt_id = before.attempts[0].attempt_id
    assert ViewerService(store).dialogue("run", run_id, attempt_id).status == "ok"
    journal = RunStore(store.root).inspect(run_id)
    (event,) = [event for event in journal.events if event.event == "compacted"]
    assert event.data["bytes_freed"] == compaction.bytes_freed
    _, regraded = regrade_run(store, run_id)
    assert len(regraded) == 1


@pytest.mark.integration
def test_compaction_refuses_a_captured_run_that_is_not_assessed(store, graded_benchmark):
    run_id = offline_run(store, graded_benchmark)
    with pytest.raises(InputError, match="not assessed"):
        compact_run(store, run_id)
    (root,) = attempt_roots(store, run_id)
    assert (root / "workspace").is_dir()


@pytest.mark.integration
def test_compaction_refuses_a_run_whose_lock_is_held(store, graded_benchmark):
    run_id = offline_run(store, graded_benchmark)
    assess_run(store, run_id)
    with RunStore(store.root).open(run_id), pytest.raises(LockBusyError):
        compact_run(store, run_id)
    (root,) = attempt_roots(store, run_id)
    assert (root / "workspace").is_dir()


@pytest.mark.integration
def test_store_compact_writes_the_report_before_compacting(store, graded_benchmark, tmp_path):
    run_id = offline_run(store, graded_benchmark)
    assess_run(store, run_id)
    reports = tmp_path / "reports"
    arguments = ["--store", str(store.root), "--json", "store", "compact", run_id]
    assert main([*arguments, "--report", str(reports)]) == 0
    assert json.loads((reports / f"{run_id}.json").read_bytes())["run_id"] == run_id
    assert "<h2>Summary</h2>" in (reports / f"{run_id}.html").read_text()


def test_prune_checks_removes_only_check_evidence_older_than_the_cutoff(store):
    now = time.time()
    old, recent = store.root / "checks" / "old", store.root / "checks" / "recent"
    for path, age in ((old, 7200), (recent, 60)):
        (path / "live-config").mkdir(parents=True)
        os.utime(path, (now - age, now - age))
    assert prune_checks(store, now, 3600) == ("old",)
    assert not old.exists()
    assert recent.is_dir()


def test_prune_checks_without_a_checks_directory_removes_nothing(store):
    assert prune_checks(store, time.time()) == ()
