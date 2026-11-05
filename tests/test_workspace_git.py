import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from dryheave import workspace_git
from dryheave.errors import ConflictError, InputError, PathError
from dryheave.models import ObjectKind
from dryheave.workspace_git import (
    case_alias,
    case_paths,
    checkpoint,
    ensure_repository,
    ignore_entries,
    inside_repository,
    missing_lines,
    relative_paths,
    require_git,
    setup_repository,
    update_gitignore,
    workspace_commit,
)
from dryheave.workspaces import initialize_workspace


def git(root, *argv):
    result = subprocess.run(
        ["git", *argv], cwd=root, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def make_workspace(tmp_path, short_runtime, git_identity):
    def make(mode, name="ws"):
        workspace = initialize_workspace(
            tmp_path / name, runtime_root=short_runtime, git_commit=mode
        )
        setup_repository(workspace)
        return workspace

    return make


def test_relative_paths_are_workspace_posix_unique_and_inside_only(tmp_path):
    root = tmp_path / "ws"
    (root / "cases/a").mkdir(parents=True)
    paths = [
        root / "cases/a/case.json",
        root / "cases" / "a" / ".." / "a" / "case.json",
        tmp_path / "elsewhere.json",
        root,
        root / "voices/v.json",
    ]
    assert relative_paths(root, paths) == ("cases/a/case.json", "voices/v.json")


def test_missing_lines_keep_order_and_skip_present_entries():
    assert missing_lines("a/\nb/\n", ["b/", "c/", "a/", "c/"]) == ("c/",)


@pytest.mark.integration
def test_ignore_entries_cover_state_caches_and_replays(make_workspace):
    entries = ignore_entries(make_workspace("ask"))
    assert {".dryheave/", "__pycache__/", "replays/"} <= set(entries)


@pytest.mark.integration
def test_init_creates_a_repository_and_gitignore(make_workspace):
    workspace = make_workspace("ask")
    assert inside_repository(workspace.root)
    assert (workspace.root / ".git").is_dir()
    ignored = (workspace.root / ".gitignore").read_text().splitlines()
    assert ignored[:3] == [".dryheave/", ".dryheave-workspace.json", "__pycache__/"]
    assert "replays/" in ignored


@pytest.mark.integration
def test_existing_repository_and_gitignore_are_extended_not_replaced(tmp_path, short_runtime):
    root = tmp_path / "ws"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / ".gitignore").write_text("notes.txt")
    workspace = initialize_workspace(root, runtime_root=short_runtime)
    assert ensure_repository(root) == "existing"
    added = update_gitignore(workspace)
    lines = (root / ".gitignore").read_text().splitlines()
    assert lines == ["notes.txt", *added]
    assert added == ignore_entries(workspace)
    assert update_gitignore(workspace) == ()
    assert (root / ".gitignore").read_text().splitlines() == lines


@pytest.mark.integration
def test_a_gitignore_that_is_not_utf8_or_not_a_regular_file_is_refused(make_workspace):
    workspace = make_workspace("ask")
    path = workspace.root / ".gitignore"
    path.write_bytes(b"\xff\n")
    with pytest.raises(InputError, match="not UTF-8"):
        update_gitignore(workspace)
    path.unlink()
    path.symlink_to(workspace.root / "elsewhere")
    (workspace.root / "elsewhere").write_text("")
    with pytest.raises(PathError):
        update_gitignore(workspace)


def touch(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    return path


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_the_first_workspace_commit_is_made_in_ask_and_auto(make_workspace, mode):
    workspace = make_workspace(mode)
    first = touch(workspace.root, "a.txt")
    result = workspace_commit(workspace, (first,), (touch(workspace.root, "b.txt"),))
    assert result["commit_paths"] == ["a.txt"]
    assert result["message"] == "workspace: initialize"
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "log", "--format=%s") == "workspace: initialize\n"


@pytest.mark.integration
def test_a_later_workspace_commit_is_an_update_checkpoint(make_workspace):
    workspace = make_workspace("auto")
    workspace_commit(workspace, (touch(workspace.root, "a.txt"),), ())
    result = workspace_commit(workspace, (), (touch(workspace.root, "b.txt"),))
    assert result["commit_paths"] == ["b.txt"]
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "log", "--format=%s").splitlines() == [
        "workspace: update",
        "workspace: initialize",
    ]


@pytest.mark.integration
def test_with_commits_off_the_workspace_commit_only_reports(make_workspace):
    workspace = make_workspace("off")
    result = workspace_commit(workspace, (touch(workspace.root, "a.txt"),), ())
    assert result == {"commit_paths": [], "git_commit": "off", "message": "workspace: update"}


@pytest.mark.integration
def test_a_failing_head_lookup_fails_the_first_commit_with_its_error(make_workspace, monkeypatch):
    workspace = make_workspace("ask")

    def denied(_root):
        raise PermissionError("permission denied: .git")

    monkeypatch.setattr(workspace_git, "_head", denied)
    result = workspace_commit(workspace, (touch(workspace.root, "a.txt"),), ())
    assert result["commit"] == {
        "message": "workspace: initialize",
        "state": "failed",
        "error": "permission denied: .git",
    }


@pytest.mark.integration
def test_an_enclosing_repository_s_ignore_rules_do_not_filter_a_workspace_without_its_own(
    tmp_path, short_runtime, git_identity
):
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text("ws/\n")
    workspace = initialize_workspace(tmp_path / "ws", runtime_root=short_runtime)
    result = checkpoint(workspace, "voice: create v", (touch(workspace.root, "voices/v.json"),))
    assert result["commit_paths"] == ["voices/v.json"]


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_paths_git_ignores_are_left_out_of_the_commit_and_listed(make_workspace, mode):
    workspace = make_workspace(mode)
    (workspace.root / ".gitignore").write_text(".claude/\n")
    kept = touch(workspace.root, "a.txt")
    ignored = touch(workspace.root, ".claude/skills/x/SKILL.md").parent
    result = workspace_commit(workspace, (kept, ignored), ())
    assert result["commit_paths"] == ["a.txt"]
    assert result["ignored_paths"] == [".claude/skills/x"]
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "ls-tree", "-r", "--name-only", "HEAD") == "a.txt\n"


@pytest.mark.integration
def test_a_workspace_inside_another_repository_gets_its_own_and_never_commits_outward(
    tmp_path, short_runtime, git_identity
):
    git(tmp_path, "init", "--quiet")
    nested = tmp_path / "inner"
    workspace = initialize_workspace(nested, runtime_root=short_runtime, git_commit="auto")
    assert ensure_repository(nested) == "created"
    assert (nested / ".git").is_dir()
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    assert checkpoint(workspace, "voice: create v", (target,))["commit"]["state"] == "committed"
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=tmp_path, capture_output=True, check=False
        ).returncode
        != 0
    )


@pytest.mark.integration
def test_auto_commits_refuse_a_workspace_that_is_not_itself_a_repository(
    tmp_path, short_runtime, git_identity
):
    git(tmp_path, "init", "--quiet")
    workspace = initialize_workspace(
        tmp_path / "inner", runtime_root=short_runtime, git_commit="auto"
    )
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "failed"
    assert "not a git repository" in result["commit"]["error"]


def test_missing_git_is_a_clear_error_unless_commits_are_off(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(InputError, match="git is not installed"):
        require_git("ask")
    with pytest.raises(InputError, match="--git-commit off"):
        require_git("auto")
    require_git("off")


def test_missing_git_skips_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert ensure_repository(tmp_path) == "skipped"


def test_checkpoint_without_a_workspace_reports_nothing(tmp_path):
    assert checkpoint(None, "voice: create x", (tmp_path / "a",)) == {}


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["ask", "off"])
def test_ask_and_off_report_commit_paths_without_committing(make_workspace, mode):
    workspace = make_workspace(mode)
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result == {
        "commit_paths": ["voices/v.json"],
        "git_commit": mode,
        "message": "voice: create v",
    }
    assert "voices/v.json" in git(workspace.root, "status", "--porcelain", "-uall")
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace.root,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.integration
def test_auto_commits_only_the_written_paths_and_leaves_user_changes_alone(make_workspace):
    workspace = make_workspace("auto")
    root = workspace.root
    (root / "notes.txt").write_text("tracked")
    git(root, "add", "notes.txt")
    git(root, "commit", "--quiet", "-m", "base")
    (root / "notes.txt").write_text("edited but unstaged")
    (root / "staged.txt").write_text("staged by the user")
    git(root, "add", "staged.txt")
    (root / "scratch.txt").write_text("untracked")
    target = root / "cases/c/case.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}")
    result = checkpoint(workspace, "case: draft c", (target,))
    assert result["commit_paths"] == ["cases/c/case.json"]
    commit = result["commit"]
    assert commit["state"] == "committed"
    assert commit["message"] == "case: draft c"
    assert git(root, "show", "--name-only", "--format=%s", "HEAD").splitlines() == [
        "case: draft c",
        "",
        "cases/c/case.json",
    ]
    assert git(root, "diff", "--cached", "--name-only").splitlines() == ["staged.txt"]
    status = git(root, "status", "--porcelain").splitlines()
    assert " M notes.txt" in status
    assert "?? scratch.txt" in status


@pytest.mark.integration
def test_auto_with_nothing_new_to_commit_is_a_no_op(make_workspace):
    workspace = make_workspace("auto")
    target = workspace.reports_dir / "r.json"
    target.parent.mkdir()
    target.write_text("{}")
    assert checkpoint(workspace, "report: r", (target,))["commit"]["state"] == "committed"
    head = git(workspace.root, "rev-parse", "HEAD")
    again = checkpoint(workspace, "report: r", (target,))
    assert again["commit"] == {"message": "report: r", "state": "nothing_to_commit"}
    assert git(workspace.root, "rev-parse", "HEAD") == head


@pytest.mark.integration
def test_auto_without_paths_commits_nothing(make_workspace):
    workspace = make_workspace("auto")
    assert checkpoint(workspace, "bench: r", ())["commit"] == {
        "message": "bench: r",
        "state": "nothing_to_commit",
    }


@pytest.mark.integration
def test_a_rejecting_hook_is_reported_and_keeps_the_written_file(make_workspace):
    workspace = make_workspace("auto")
    hook = workspace.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho 'rejected by policy' >&2\nexit 1\n")
    hook.chmod(0o755)
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit_paths"] == ["voices/v.json"]
    assert result["commit"]["state"] == "failed"
    assert "rejected by policy" in result["commit"]["error"]
    assert target.read_text() == "{}"


@pytest.mark.integration
def test_missing_identity_is_reported_as_a_failed_commit(make_workspace, monkeypatch):
    workspace = make_workspace("auto")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "failed"
    assert result["commit"]["error"]


@pytest.mark.integration
def test_auto_in_a_directory_that_lost_its_repository_reports_the_failure(make_workspace, tmp_path):
    workspace = make_workspace("auto")
    shutil.rmtree(workspace.root / ".git")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"] == {
        "message": "voice: create v",
        "state": "failed",
        "error": "The workspace is not a git repository.",
    }


@pytest.mark.integration
def test_case_freeze_commits_the_draft_and_the_files_it_names_but_not_other_files(make_workspace):
    workspace = make_workspace("auto")
    draft = workspace.cases_dir / "c" / "case.json"
    draft.parent.mkdir(parents=True)
    draft.write_text('{"hidden_files": {"check.py": "check.py"}, "reference_patch": "ref.patch"}')
    (draft.parent / "check.py").write_text("check")
    (draft.parent / "ref.patch").write_text("patch")
    (draft.parent / "scratch.py").write_text("wip")
    assert case_paths(draft) == (draft, draft.parent / "check.py", draft.parent / "ref.patch")
    result = checkpoint(workspace, "case: freeze c", case_paths(draft))
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "cases/c/case.json",
        "cases/c/check.py",
        "cases/c/ref.patch",
    ]


class RecordingStore:
    def __init__(self, error=None, existing=None):
        self.error = error
        self.existing = existing or {}
        self.calls = []

    def aliases(self):
        return {name: f"id-{kind}" for name, kind in self.existing.items()}

    def get(self, reference):
        return SimpleNamespace(kind=ObjectKind(reference.removeprefix("id-")))

    def set_alias(self, name, identifier, *, replace=False):
        if self.error:
            raise self.error
        self.calls.append((name, identifier, replace))


def test_case_alias_points_the_case_folder_name_at_the_id_and_reports_failures(make_workspace):
    workspace = make_workspace("off")
    store = RecordingStore()
    draft = workspace.cases_dir / "mine" / "case.json"
    assert case_alias(store, workspace, draft, "a" * 32) == {"alias": "mine"}
    assert store.calls == [("mine", "a" * 32, True)]
    other = [workspace.root / "drafts" / "case.json", workspace.cases_dir / "mine" / "other.json"]
    assert [case_alias(store, workspace, path, "b" * 32) for path in other] == [{}, {}]
    assert case_alias(store, None, draft, "b" * 32) == {}
    assert len(store.calls) == 1
    failing = RecordingStore(ConflictError("alias busy"))
    assert case_alias(failing, workspace, draft, "c" * 32) == {"alias_error": "alias busy"}


def test_case_alias_keeps_aliases_that_point_at_other_kinds_and_replaces_case_aliases(
    make_workspace,
):
    workspace = make_workspace("off")
    draft = workspace.cases_dir / "mine" / "case.json"
    store = RecordingStore(existing={"mine": "profile"})
    assert case_alias(store, workspace, draft, "a" * 32) == {
        "alias_error": "Alias 'mine' already points at a profile object; "
        "rename the case folder or move that alias first."
    }
    assert store.calls == []
    store = RecordingStore(existing={"mine": "case"})
    assert case_alias(store, workspace, draft, "a" * 32) == {"alias": "mine"}
    assert store.calls == [("mine", "a" * 32, True)]


def test_case_alias_matches_the_draft_through_dotdot_and_symlinked_prefixes(
    make_workspace, tmp_path
):
    workspace = make_workspace("off")
    (workspace.cases_dir / "mine").mkdir(parents=True)
    (workspace.cases_dir / "mine" / "sub").mkdir()
    link = tmp_path / "link"
    link.symlink_to(workspace.root)
    spellings = [
        workspace.cases_dir / "mine" / "sub" / ".." / "case.json",
        link / "cases" / "mine" / "case.json",
    ]
    for draft in spellings:
        store = RecordingStore()
        assert case_alias(store, workspace, draft, "a" * 32) == {"alias": "mine"}
        assert store.calls == [("mine", "a" * 32, True)]
    assert (
        case_alias(
            RecordingStore(),
            workspace,
            workspace.cases_dir / "mine" / "sub" / "case.json",
            "a" * 32,
        )
        == {}
    )


def test_case_paths_falls_back_to_the_draft_when_it_cannot_be_read(tmp_path):
    draft = tmp_path / "case.json"
    assert case_paths(draft) == (draft,)
    draft.write_text("not json")
    assert case_paths(draft) == (draft,)


@pytest.mark.integration
def test_paths_are_literal_so_glob_names_do_not_sweep_in_siblings(make_workspace):
    workspace = make_workspace("auto")
    voices = workspace.voices_dir
    voices.mkdir()
    (voices / "a[12].json").write_text("{}")
    (voices / "a1.json").write_text("unrelated")
    result = checkpoint(workspace, "voice: create a", (voices / "a[12].json",))
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "voices/a[12].json"
    ]


@pytest.mark.integration
def test_pathspec_magic_names_do_not_commit_everything(make_workspace):
    workspace = make_workspace("auto")
    (workspace.root / "other.txt").write_text("unrelated")
    target = workspace.voices_dir / ":!x.json"
    target.parent.mkdir()
    target.write_text("{}")
    checkpoint(workspace, "voice: create x", (target,))
    assert git(workspace.root, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "voices/:!x.json"
    ]


@pytest.mark.integration
def test_a_callers_git_dir_does_not_redirect_the_commit(make_workspace, tmp_path, monkeypatch):
    workspace = make_workspace("auto")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    monkeypatch.delenv("GIT_DIR")
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "log", "--format=%s").splitlines() == ["voice: create v"]
    unborn = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=other, capture_output=True, check=False
    )
    assert unborn.returncode != 0


@pytest.mark.integration
def test_auto_reports_a_missing_git_instead_of_raising(make_workspace, monkeypatch):
    workspace = make_workspace("auto")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    monkeypatch.setenv("PATH", "")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"] == {
        "message": "voice: create v",
        "state": "failed",
        "error": "git is not installed or not on PATH",
    }


def reject_commits(workspace):
    hook = workspace.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)


def index_state(root):
    return (
        git(root, "ls-files", "-s"),
        git(root, "diff", "--cached"),
        git(root, "status", "--porcelain"),
    )


def seed_user_index(workspace):
    root = workspace.root
    (root / "tracked.txt").write_text("one\n")
    (root / "partial.txt").write_text("one\n")
    git(root, "add", "tracked.txt", "partial.txt")
    git(root, "commit", "--quiet", "-m", "seed")
    (root / "partial.txt").write_text("one\ntwo\n")
    git(root, "add", "partial.txt")
    (root / "partial.txt").write_text("one\ntwo\nthree\n")
    (root / "ita.txt").write_text("intent\n")
    git(root, "add", "-N", "ita.txt")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    return target


@pytest.mark.integration
def test_a_rejected_checkpoint_leaves_the_real_index_byte_identical(make_workspace):
    workspace = make_workspace("auto")
    target = seed_user_index(workspace)
    before = index_state(workspace.root)
    index_bytes = (workspace.root / ".git/index").read_bytes()
    reject_commits(workspace)
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "failed"
    assert index_state(workspace.root) == before
    assert (workspace.root / ".git/index").read_bytes() == index_bytes
    assert " A ita.txt" in before[2]
    assert git(workspace.root, "show", ":partial.txt") == "one\ntwo\n"


@pytest.mark.integration
def test_a_successful_checkpoint_keeps_unrelated_staging_and_cleans_its_paths(make_workspace):
    workspace = make_workspace("auto")
    target = seed_user_index(workspace)
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "committed"
    assert "index_sync_error" not in result["commit"]
    status = git(workspace.root, "status", "--porcelain").splitlines()
    assert " A ita.txt" in status
    assert "MM partial.txt" in status
    assert all("voices/v.json" not in line for line in status)
    assert git(workspace.root, "show", ":partial.txt") == "one\ntwo\n"
    assert git(workspace.root, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "voices/v.json"
    ]


@pytest.mark.integration
def test_the_first_checkpoint_in_an_unborn_repository_commits(make_workspace):
    workspace = make_workspace("auto")
    (workspace.root / "staged.txt").write_text("user\n")
    git(workspace.root, "add", "staged.txt")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        "voices/v.json"
    ]
    assert git(workspace.root, "diff", "--cached", "--name-only").splitlines() == ["staged.txt"]


@pytest.mark.integration
def test_a_rejected_first_checkpoint_in_an_unborn_repository_leaves_the_index(make_workspace):
    workspace = make_workspace("auto")
    (workspace.root / "staged.txt").write_text("user\n")
    git(workspace.root, "add", "staged.txt")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    before = index_state(workspace.root)
    reject_commits(workspace)
    assert checkpoint(workspace, "voice: create v", (target,))["commit"]["state"] == "failed"
    assert index_state(workspace.root) == before


@pytest.mark.integration
def test_a_caller_index_file_does_not_redirect_the_checkpoint(
    make_workspace, tmp_path, monkeypatch
):
    workspace = make_workspace("auto")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere-index"))
    assert checkpoint(workspace, "voice: create v", (target,))["commit"]["state"] == "committed"
    assert not (tmp_path / "elsewhere-index").exists()
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert git(workspace.root, "status", "--porcelain", "--", "voices").strip() == ""


@pytest.mark.integration
def test_a_merge_in_progress_fails_the_checkpoint_without_touching_the_index(make_workspace):
    workspace = make_workspace("auto")
    root = workspace.root
    (root / "c.txt").write_text("base\n")
    git(root, "add", "c.txt")
    git(root, "commit", "--quiet", "-m", "base")
    git(root, "checkout", "--quiet", "-b", "side")
    (root / "c.txt").write_text("side\n")
    git(root, "commit", "--quiet", "-am", "side")
    git(root, "checkout", "--quiet", "-")
    (root / "c.txt").write_text("main\n")
    git(root, "commit", "--quiet", "-am", "main")
    subprocess.run(["git", "merge", "side"], cwd=root, capture_output=True, check=False)
    before = git(root, "ls-files", "-s")
    result = checkpoint(workspace, "voice: create v", (root / "c.txt",))
    assert result["commit"]["state"] == "failed"
    assert git(root, "ls-files", "-s") == before
    assert "UU c.txt" in git(root, "status", "--porcelain")


@pytest.mark.integration
def test_a_commit_landing_during_the_checkpoint_survives_and_the_checkpoint_fails(
    make_workspace, monkeypatch
):
    workspace = make_workspace("auto")
    root = workspace.root
    (root / "base.txt").write_text("base\n")
    git(root, "add", "base.txt")
    git(root, "commit", "--quiet", "-m", "base")
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    real_run = workspace_git._run

    def racing_run(directory, *argv, **options):
        result = real_run(directory, *argv, **options)
        if argv[0] == "read-tree":
            (root / "user.txt").write_text("user\n")
            git(root, "add", "user.txt")
            git(root, "commit", "--quiet", "-m", "user commit")
        return result

    monkeypatch.setattr(workspace_git, "_run", racing_run)
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "failed"
    assert "HEAD moved" in result["commit"]["error"]
    assert git(root, "log", "--format=%s").splitlines() == ["user commit", "base"]
    assert git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        "base.txt",
        "user.txt",
    ]
    assert "?? voices/" in git(root, "status", "--porcelain").splitlines()
    assert git(root, "diff", "--cached", "--name-only").splitlines() == []


@pytest.mark.integration
def test_commit_message_and_post_commit_hooks_run_on_a_checkpoint(make_workspace):
    workspace = make_workspace("auto")
    hooks = workspace.root / ".git/hooks"
    (hooks / "commit-msg").write_text('#!/bin/sh\nprintf "\\nSigned-off-by: hook\\n" >> "$1"\n')
    (hooks / "post-commit").write_text("#!/bin/sh\ngit rev-parse HEAD > post-commit.out\n")
    for hook in ("commit-msg", "post-commit"):
        (hooks / hook).chmod(0o755)
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "committed"
    assert git(workspace.root, "log", "-1", "--format=%B").strip() == (
        "voice: create v\n\nSigned-off-by: hook"
    )
    assert (workspace.root / "post-commit.out").read_text().strip() == result["commit"]["commit"]
    assert git(workspace.root, "reflog", "-1", "--format=%gs").strip() == (
        "commit (initial): voice: create v"
    )


def old_git(tmp_path, monkeypatch, version="2.30.2"):
    real = shutil.which("git")
    bin_dir = tmp_path / "old-git"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$2" = version ]; then echo "git version {version}"; exit 0; fi\n'
        'if [ "$2" = hook ]; then echo "git: \'hook\' is not a git command." >&2; exit 1; fi\n'
        f'exec "{real}" "$@"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize(
    "text, version",
    [
        ("git version 2.25.1", (2, 25)),
        ("git version 2.39.5 (Apple Git-154)", (2, 39)),
        ("git version 2.36.0.windows.1", (2, 36)),
        ("something else", None),
    ],
)
def test_git_version_reads_major_and_minor(text, version):
    assert workspace_git.git_version(text) == version


@pytest.mark.integration
def test_a_checkpoint_on_git_older_than_2_36_names_the_version(
    make_workspace, tmp_path, monkeypatch
):
    workspace = make_workspace("auto")
    target = seed_user_index(workspace)
    before = index_state(workspace.root)
    old_git(tmp_path, monkeypatch)
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"] == {
        "message": "voice: create v",
        "state": "failed",
        "error": "checkpoint commits need git 2.36 or later (found 2.30.2); "
        "upgrade git or set [git] commit to ask or off",
    }
    assert index_state(workspace.root) == before
    assert git(workspace.root, "log", "--format=%s").splitlines() == ["seed"]


@pytest.mark.integration
def test_post_commit_sees_the_real_index_after_the_checkpoint(make_workspace):
    workspace = make_workspace("auto")
    (workspace.root / "staged.txt").write_text("user\n")
    git(workspace.root, "add", "staged.txt")
    hook = workspace.root / ".git/hooks/post-commit"
    hook.write_text("#!/bin/sh\ngit status --porcelain --untracked-files=no > ../post-status\n")
    hook.chmod(0o755)
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    assert checkpoint(workspace, "voice: create v", (target,))["commit"]["state"] == "committed"
    assert (workspace.root.parent / "post-status").read_text().splitlines() == ["A  staged.txt"]


@pytest.mark.integration
def test_a_commit_msg_hook_that_empties_the_message_aborts_the_checkpoint(make_workspace):
    workspace = make_workspace("auto")
    hook = workspace.root / ".git/hooks/commit-msg"
    hook.write_text('#!/bin/sh\n: > "$1"\n')
    hook.chmod(0o755)
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["error"] == "Aborting commit due to empty commit message."
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=workspace.root, capture_output=True
        ).returncode
        != 0
    )


@pytest.mark.integration
def test_a_pre_commit_hook_that_unstages_everything_leaves_nothing_to_commit(make_workspace):
    workspace = make_workspace("auto")
    target = seed_user_index(workspace)
    hook = workspace.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\ngit read-tree HEAD\n")
    hook.chmod(0o755)
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"] == {"message": "voice: create v", "state": "nothing_to_commit"}
    assert git(workspace.root, "log", "--format=%s").splitlines() == ["seed"]


@pytest.mark.integration
def test_hooks_run_without_an_editor(make_workspace):
    workspace = make_workspace("auto")
    hook = workspace.root / ".git/hooks/prepare-commit-msg"
    hook.write_text('#!/bin/sh\necho "$GIT_EDITOR" > ../hook-editor\n')
    hook.chmod(0o755)
    target = workspace.voices_dir / "v.json"
    target.parent.mkdir()
    target.write_text("{}")
    assert checkpoint(workspace, "voice: create v", (target,))["commit"]["state"] == "committed"
    assert (workspace.root.parent / "hook-editor").read_text() == ":\n"


@pytest.mark.integration
def test_a_held_branch_lock_is_not_reported_as_a_racing_commit(make_workspace):
    workspace = make_workspace("auto")
    target = seed_user_index(workspace)
    branch = git(workspace.root, "symbolic-ref", "HEAD").strip()
    (workspace.root / ".git" / f"{branch}.lock").write_text("")
    result = checkpoint(workspace, "voice: create v", (target,))
    assert result["commit"]["state"] == "failed"
    head = git(workspace.root, "rev-parse", "HEAD").strip()
    assert result["commit"]["error"].startswith(f"HEAD could not be moved from {head}")
