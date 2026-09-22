import os
from pathlib import Path

import pytest

from dryheave.capture_models import CapturedFile
from dryheave.cases import CapturePolicy, load_frozen_case
from dryheave.errors import InputError
from dryheave.final_capture import capture_workspace, materialize_files
from dryheave.repositories import Git, SnapshotLimits, materialize_repository
from dryheave.serialization import digest


def workspace_for(store, tmp_path):
    case = load_frozen_case(store, "task")
    root = tmp_path / "workspace"
    materialize_repository(store, case.repository_id, root)
    return case, root


@pytest.mark.integration
def test_capture_patch_includes_untracked_and_preserves_git_evidence(store, benchmark, tmp_path):
    case, root = workspace_for(store, tmp_path)
    (root / "greet.py").write_text("new result\n")
    (root / "new.py").write_text("untracked content\n")
    (root / "empty").mkdir()
    (root / "link").symlink_to("new.py")
    captured, blobs = capture_workspace(
        store, case.repository_id, root, case.capture_policy, tmp_path / "scratch"
    )
    assert captured.complete
    assert "empty" in captured.directories
    assert b"untracked content" in blobs["final.patch"]
    assert b"new result" in blobs["final.patch"]
    assert any(entry.path.startswith("objects/") for entry in captured.git_files)
    assert blobs["git-inventory.txt"]
    copy = tmp_path / "copy"
    materialize_files(copy, captured.files, blobs)
    assert (copy / "link").readlink() == Path("new.py")


@pytest.mark.integration
def test_special_files_and_external_links_never_opened(store, benchmark, tmp_path):
    case, root = workspace_for(store, tmp_path)
    os.mkfifo(root / "pipe")
    (root / "escape").symlink_to("../../outside")
    (root / "cycle-a").symlink_to("cycle-b")
    (root / "cycle-b").symlink_to("cycle-a")
    (root / "ok").write_text("kept")
    capture, blobs = capture_workspace(
        store, case.repository_id, root, case.capture_policy, tmp_path / "scratch"
    )
    assert not capture.complete
    assert {(item.path, item.reason) for item in capture.omissions} >= {
        ("pipe", "special_file"),
        ("escape", "symlink_escape_cycle_or_missing_target"),
        ("cycle-a", "symlink_escape_cycle_or_missing_target"),
        ("cycle-b", "symlink_escape_cycle_or_missing_target"),
    }
    assert blobs["workspace/ok"] == b"kept"


@pytest.mark.integration
def test_links_to_workspace_root_preserve_bytes_and_remain_grade_eligible(
    store, graded_benchmark, tmp_path, monkeypatch
):
    from dryheave.assessments import assess_run
    from dryheave.experiments import create_experiment
    from dryheave.fixture_subject import FixtureTerminal
    from dryheave.integrity import load_assessment
    from dryheave.runner import load_capture, run_experiment
    from dryheave.runner_models import RunOptions

    close = FixtureTerminal.close
    targets = {
        "root-dot": ".",
        "root-nested": "sub/..",
        "sub/root-parent": "..",
        "root-chain": "sub/root-parent",
    }

    def add_root_links(terminal):
        workspace = Path(terminal.plan.cwd)
        (workspace / "sub").mkdir()
        for path, target in targets.items():
            (workspace / path).symlink_to(target)
        return close(terminal)

    monkeypatch.setattr(FixtureTerminal, "close", add_root_links)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    capture_id = summary.pending_assessment[0]
    captured = load_capture(store, capture_id)
    blobs = store.read_blobs(capture_id)
    assert captured.workspace.complete
    entries = {entry.path: entry for entry in captured.workspace.files}
    copy = tmp_path / "root-link-copy"
    materialize_files(
        copy, captured.workspace.files, blobs, directories=captured.workspace.directories
    )
    for path, target in targets.items():
        assert entries[path].mode == "symlink"
        assert blobs[entries[path].blob] == target.encode()
        assert os.readlink(copy / path) == target
        assert (copy / path).resolve() == copy
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.eligible
    assert result.completion == "pass"


@pytest.mark.integration
@pytest.mark.parametrize("target", ["missing/..", "greet.py/.."])
def test_links_cannot_traverse_missing_or_regular_components_before_parent(
    store, benchmark, tmp_path, target
):
    case, root = workspace_for(store, tmp_path)
    (root / "invalid").symlink_to(target)
    capture, blobs = capture_workspace(
        store, case.repository_id, root, case.capture_policy, tmp_path / "scratch"
    )
    assert capture.complete is False
    assert ("invalid", "symlink_escape_cycle_or_missing_target", False) in {
        (item.path, item.reason, item.intentional) for item in capture.omissions
    }
    assert "invalid" not in {entry.path for entry in capture.files}
    content = target.encode()
    forged = CapturedFile(
        path="invalid",
        blob="workspace/invalid",
        mode="symlink",
        sha256=digest(content),
        size=len(content),
    )
    with pytest.raises(InputError, match="Captured symlink cannot be safely reconstructed"):
        materialize_files(
            tmp_path / "forged-copy",
            (*capture.files, forged),
            blobs | {forged.blob: content},
            directories=capture.directories,
        )


@pytest.mark.integration
def test_frozen_ignored_and_exclusion_policy(store, benchmark, tmp_path):
    case, root = workspace_for(store, tmp_path)
    (root / ".gitignore").write_text("build/\nkeep.log\n")
    (root / "build").mkdir()
    (root / "build/large").write_bytes(b"x" * 200000)
    (root / "keep.log").write_text("selected")
    (root / "secret.tmp").write_text("excluded")
    policy = CapturePolicy(max_bytes=100000, include_ignored=("keep.log",), exclude=("secret.tmp",))
    capture, blobs = capture_workspace(
        store, case.repository_id, root, policy, tmp_path / "scratch"
    )
    assert capture.complete
    assert blobs["workspace/keep.log"] == b"selected"
    assert {(item.path, item.reason, item.intentional) for item in capture.omissions} >= {
        ("build", "ignored", True),
        ("secret.tmp", "frozen_exclusion", True),
    }


@pytest.mark.integration
def test_git_inventory_retains_subject_added_commit_without_classifying_it(
    store, benchmark, tmp_path
):
    case, root = workspace_for(store, tmp_path)
    git = Git(root, SnapshotLimits())
    git.environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    (root / "new").write_text("task commit")
    git.run("add", "new")
    git.run("commit", "--quiet", "-m", "task")
    commit = git.run("rev-parse", "HEAD").stdout.strip()
    capture, blobs = capture_workspace(
        store, case.repository_id, root, case.capture_policy, tmp_path / "scratch"
    )
    assert capture.git_complete
    assert commit + b" commit" in blobs["git-inventory.txt"]


@pytest.mark.integration
def test_ignored_dependency_trees_are_pruned_before_capture_limit(store, benchmark, tmp_path):
    case, root = workspace_for(store, tmp_path)
    (root / ".gitignore").write_text("node_modules/\n.venv/\n")
    for name in ("node_modules/dependency", ".venv/packages"):
        tree = root / name
        tree.mkdir(parents=True)
        for index in range(100):
            (tree / f"{index}.py").write_text("ignored build output")
    (root / "node_modules/tracked.py").write_text("tracked input")
    Git(root, SnapshotLimits()).run("add", "--force", "node_modules/tracked.py")
    (root / "node_modules/selected").mkdir()
    (root / "node_modules/selected/input.py").write_text("explicitly selected input")
    (root / "greet.py").write_text("changed source\n")
    policy = CapturePolicy(max_files=20, include_ignored=("node_modules/selected/input.py",))
    capture, blobs = capture_workspace(
        store, case.repository_id, root, policy, tmp_path / "scratch"
    )
    assert capture.complete
    assert blobs["workspace/greet.py"] == b"changed source\n"
    assert blobs["workspace/node_modules/tracked.py"] == b"tracked input"
    assert blobs["workspace/node_modules/selected/input.py"] == b"explicitly selected input"
    assert b"changed source" in blobs["final.patch"]
    assert {(item.path, item.reason, item.intentional) for item in capture.omissions} >= {
        ("node_modules", "ignored_children", True),
        (".venv", "ignored", True),
    }


@pytest.mark.integration
def test_nested_ignore_rules_apply_before_descent(store, benchmark, tmp_path):
    case, root = workspace_for(store, tmp_path)
    (root / "src/cache").mkdir(parents=True)
    (root / "src/.gitignore").write_text("cache/\n*.log\n!keep.log\n")
    (root / "src/cache/a").write_text("ignored")
    (root / "src/run.log").write_text("ignored")
    (root / "src/keep.log").write_text("kept")
    capture, blobs = capture_workspace(
        store, case.repository_id, root, case.capture_policy, tmp_path / "scratch"
    )
    assert capture.complete
    assert blobs["workspace/src/keep.log"] == b"kept"
    assert {(item.path, item.reason) for item in capture.omissions} >= {
        ("src/cache", "ignored"),
        ("src/run.log", "ignored"),
    }


@pytest.mark.integration
@pytest.mark.parametrize("included", ["node_modules/selected/input.py", "node_modules/selected"])
def test_protected_ignored_leaves_do_not_spend_budget_on_siblings(
    store, benchmark, tmp_path, included
):
    case, root = workspace_for(store, tmp_path)
    (root / ".gitignore").write_text("node_modules/\n")
    vendor = root / "node_modules/vendor"
    vendor.mkdir(parents=True)
    (vendor / "tracked.py").write_text("tracked input")
    deleted = vendor / "deleted.py"
    deleted.write_text("removed by the task")
    Git(root, SnapshotLimits()).run("add", "--force", "node_modules/vendor")
    deleted.unlink()
    for directory in (root / "node_modules", vendor):
        for index in range(30):
            (directory / f"package-{index}").mkdir()
            (directory / f"output-{index}.py").write_text("ignored output")
    selected = root / "node_modules/selected"
    (selected / "more").mkdir(parents=True)
    (selected / "input.py").write_text("explicitly selected input")
    (selected / "more/deep.py").write_text("selected directory content")
    (root / "src").mkdir()
    (root / "src/changed.py").write_text("changed source\n")
    policy = CapturePolicy(max_files=20, include_ignored=(included,))
    capture, blobs = capture_workspace(
        store, case.repository_id, root, policy, tmp_path / "scratch"
    )
    assert capture.complete
    assert blobs["workspace/src/changed.py"] == b"changed source\n"
    assert blobs["workspace/node_modules/vendor/tracked.py"] == b"tracked input"
    assert blobs["workspace/node_modules/selected/input.py"] == b"explicitly selected input"
    assert "workspace/node_modules/vendor/deleted.py" not in blobs
    assert b"changed source" in blobs["final.patch"]
    assert {(item.path, item.reason, item.intentional) for item in capture.omissions} >= {
        ("node_modules", "ignored_children", True),
        ("node_modules/vendor", "ignored_children", True),
    }
    if included == "node_modules/selected":
        assert (
            blobs["workspace/node_modules/selected/more/deep.py"] == b"selected directory content"
        )


@pytest.mark.integration
def test_explicitly_included_ignored_directory_still_obeys_entry_limit(store, benchmark, tmp_path):
    case, root = workspace_for(store, tmp_path)
    (root / ".gitignore").write_text("node_modules/\n")
    selected = root / "node_modules/selected"
    selected.mkdir(parents=True)
    for index in range(30):
        (selected / f"{index}.py").write_text("selected output")
    policy = CapturePolicy(max_files=20, include_ignored=("node_modules/selected",))
    capture, _ = capture_workspace(store, case.repository_id, root, policy, tmp_path / "scratch")
    assert not capture.complete
    assert ("node_modules/selected", "entry_limit", False) in {
        (item.path, item.reason, item.intentional) for item in capture.omissions
    }
    assert len(capture.files) <= policy.max_files
