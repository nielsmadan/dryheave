import json
import os
import shutil
import time

import pytest

from conftest import HOST
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
        store,
        create_experiment(store, benchmark),
        options=RunOptions(mode="offline-fixture"),
        host=HOST,
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
    session_log = root / "profile" / "config" / "projects" / "a-project" / "session.jsonl"
    session_log.parent.mkdir(parents=True)
    session_log.write_text("{}\n")
    pruned = [
        root / "profile" / "config" / "plugins",
        root / "profile" / "config" / "skills",
        root / "profile" / "home" / "x" / "cache",
    ]
    for path in (pruned[0] / "demo" / "plugin.json", pruned[1] / "review" / "SKILL.md"):
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    pruned[2].mkdir(parents=True)
    (pruned[2] / "blob").write_text("{}")
    working = [name for name in WORKING_DIRECTORIES if (root / name).is_dir()]

    compaction = compact_run(store, run_id)

    assert compaction.attempts == 1
    assert compaction.bytes_freed > 0
    assert "workspace" in working
    assert compaction.removed == (
        *(f"attempts/{root.name}/{name}" for name in working),
        f"attempts/{root.name}/profile/config/plugins",
        f"attempts/{root.name}/profile/config/skills",
        f"attempts/{root.name}/profile/home/x/cache",
    )
    assert [(root / name).exists() for name in WORKING_DIRECTORIES] == [False] * len(
        WORKING_DIRECTORIES
    )
    assert session_log.is_file()
    assert [path.exists() for path in pruned] == [False, False, False]
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
def test_compaction_removes_credential_files_and_never_follows_profile_links(
    store, graded_benchmark, tmp_path
):
    run_id = offline_run(store, graded_benchmark)
    assess_run(store, run_id)
    (root,) = attempt_roots(store, run_id)
    outside = tmp_path / "outside"
    outside_files = [outside / "cache" / "keep.txt", outside / "plugins" / "keep.txt"]
    for path in outside_files:
        path.parent.mkdir(parents=True)
        path.write_text("keep")
    live_auth = tmp_path / "live-auth.json"
    live_auth.write_text("{}")
    config = root / "profile" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "linked").symlink_to(outside, target_is_directory=True)
    (config / "cache").symlink_to(outside, target_is_directory=True)
    (config / "auth.json").symlink_to(live_auth)
    (config / ".credentials.json").write_text('{"token": "x"}')
    credential_directory = config / "nested" / "auth.json"
    credential_directory.mkdir(parents=True)
    (credential_directory / "keep.txt").write_text("keep")
    replaced = root / "profile" / "home" / ".codex" / "auth.json"
    replaced.parent.mkdir(parents=True)
    replaced.write_text('{"tokens": {}}')
    session_log = config / "sessions" / "rollout.jsonl"
    session_log.parent.mkdir(parents=True)
    session_log.write_text("{}\n")

    compaction = compact_run(store, run_id)

    prefix = f"attempts/{root.name}/profile"
    assert [path for path in compaction.removed if path.startswith(prefix)] == [
        f"{prefix}/config/.credentials.json",
        f"{prefix}/config/auth.json",
        f"{prefix}/home/.codex/auth.json",
    ]
    assert [path.read_text() for path in outside_files] == ["keep", "keep"]
    assert live_auth.read_text() == "{}"
    assert (config / "linked").is_symlink()
    assert (config / "cache").is_symlink()
    assert [os.path.lexists(config / name) for name in ("auth.json", ".credentials.json")] == [
        False,
        False,
    ]
    assert [path.name for path in replaced.parent.iterdir()] == []
    assert session_log.is_file()
    assert (credential_directory / "keep.txt").read_text() == "keep"


@pytest.mark.integration
def test_compaction_leaves_a_symlinked_profile_and_its_target_alone(
    store, graded_benchmark, tmp_path
):
    run_id = offline_run(store, graded_benchmark)
    assess_run(store, run_id)
    (root,) = attempt_roots(store, run_id)
    outside = tmp_path / "outside-profile"
    kept = [outside / "config" / "plugins" / "keep.txt", outside / "config" / "auth.json"]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
    shutil.rmtree(root / "profile")
    (root / "profile").symlink_to(outside, target_is_directory=True)
    working = [name for name in WORKING_DIRECTORIES if (root / name).is_dir()]

    compaction = compact_run(store, run_id)

    assert compaction.removed == tuple(f"attempts/{root.name}/{name}" for name in working)
    assert (root / "profile").is_symlink()
    assert [path.read_text() for path in kept] == ["keep", "keep"]


@pytest.mark.integration
@pytest.mark.parametrize("linked", ["attempt", "attempts"])
def test_compaction_refuses_a_symlinked_attempt_directory(
    store, graded_benchmark, tmp_path, linked
):
    run_id = offline_run(store, graded_benchmark)
    assess_run(store, run_id)
    (root,) = attempt_roots(store, run_id)
    moved = root if linked == "attempt" else root.parent
    outside = tmp_path / "outside"
    shutil.move(moved, outside)
    moved.symlink_to(outside, target_is_directory=True)
    relative = moved.relative_to(store.root / "runs" / run_id)
    with pytest.raises(InputError, match=f"Attempt path {relative} is a symlink"):
        compact_run(store, run_id)
    inside = outside / root.name if linked == "attempts" else outside
    assert (inside / "workspace").is_dir()


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
