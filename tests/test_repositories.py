import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from dryheave import repositories
from dryheave.errors import InputError, LimitError
from dryheave.models import ObjectKind
from dryheave.repositories import (
    Git,
    RepositorySnapshot,
    SnapshotLimits,
    capture_repository,
    materialize_repository,
)
from dryheave.storage import ObjectStore


def fingerprint(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.integration
def test_exact_ancestry_and_tree_without_source_changes(
    historical_repo: tuple[Path, str, str, str],
    tmp_path: Path,
    store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, baseline, future, unreachable = historical_repo
    git = Git(source, SnapshotLimits())
    canary = tmp_path / "filter-ran"
    git.run("config", "filter.hostile.smudge", f"touch {canary}")
    git.run("config", "filter.hostile.required", "true")
    git.run("config", "core.hooksPath", str(tmp_path / "hooks"))
    git.run("replace", baseline, future)
    (source / "greet.py").write_text("Uncommitted source content must be ignored\n")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        f'[filter "hostile"]\nsmudge = touch {canary}\nrequired = true\n[init]\ntemplateDir = {source}\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    before = fingerprint(source)
    identifier = capture_repository(store, source, baseline)
    assert fingerprint(source) == before
    snapshot = store.load(identifier, RepositorySnapshot, kind=ObjectKind.REPOSITORY)
    assert snapshot.known_disallowed_commits == tuple(sorted((future, unreachable)))
    assert snapshot.audit_coverage == "complete_at_capture"
    target = tmp_path / "workspace"
    materialize_repository(store, identifier, target)
    isolated = Git(target, SnapshotLimits())
    assert isolated.run("log", "--format=%s").stdout.decode().splitlines() == [
        "baseline",
        "initial",
    ]
    assert "initial" in isolated.run("blame", "--line-porcelain", "greet.py").stdout.decode()
    assert (target / "history.txt").read_bytes() == b"Historical context\n"
    assert (target / "subst.txt").read_bytes() == b"$Format:%H$\r\n"
    assert (target / "greet.py").read_text() == 'def greet(name):\n    return "Hello " + name\n'
    assert isolated.run("status", "--porcelain").stdout == b""
    assert isolated.run("for-each-ref", "--format=%(refname)").stdout == b"refs/heads/baseline\n"
    assert isolated.run("remote").stdout == b""
    for sha in (future, unreachable):
        assert isolated.run("cat-file", "-e", sha, check=False).returncode != 0
    assert not (target / ".git/logs").exists()
    assert not (target / ".git/objects/info/alternates").exists()
    assert not canary.exists()
    assert str(source).encode() not in (target / ".git/config").read_bytes()
    assert fingerprint(source) == before


@pytest.mark.integration
def test_initial_patch_applied_explicitly(
    historical_repo: tuple[Path, str, str, str], tmp_path: Path, store: ObjectStore
) -> None:
    source, baseline, future, unreachable = historical_repo
    patch = b'diff --git a/greet.py b/greet.py\n--- a/greet.py\n+++ b/greet.py\n@@ -1,2 +1,2 @@\n def greet(name):\n-    return "Hello " + name\n+    return "Starting dirty work " + name\n'
    identifier = capture_repository(store, source, baseline, initial_patch=patch)
    target = tmp_path / "workspace"
    materialize_repository(store, identifier, target)
    assert "Starting dirty work" in (target / "greet.py").read_text()
    git = Git(target, SnapshotLimits())
    assert git.run("status", "--porcelain").stdout == b" M greet.py\n"
    assert git.run("diff", "--cached", "--no-ext-diff").stdout == b""
    diff = git.run("diff", "--no-ext-diff", "--", "greet.py").stdout
    assert b'-    return "Hello " + name\n+    return "Starting dirty work " + name\n' in diff
    assert git.run("rev-parse", "HEAD").stdout.decode().strip() == baseline
    for sha in (future, unreachable):
        assert git.run("cat-file", "-e", sha, check=False).returncode != 0


@pytest.mark.integration
def test_invalid_patch_and_baseline_fail(
    historical_repo: tuple[Path, str, str, str], store: ObjectStore
) -> None:
    source, baseline, _, _ = historical_repo
    with pytest.raises(InputError, match="explicit full"):
        capture_repository(store, source, "HEAD")
    with pytest.raises(InputError, match="failed"):
        capture_repository(store, source, "f" * 40)
    with pytest.raises(InputError, match="failed"):
        capture_repository(store, source, baseline, initial_patch=b"not a patch")


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["lfs", "symlink", "submodule", "shallow", "alternates"])
def test_unsupported_dependencies_rejected(
    kind: str, historical_repo: tuple[Path, str, str, str], tmp_path: Path, store: ObjectStore
) -> None:
    source, baseline, _, _ = historical_repo
    git = Git(source, SnapshotLimits())
    git.environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "f@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "f@example.invalid",
        }
    )
    if kind == "shallow":
        (source / ".git/shallow").write_text(baseline + "\n")
    elif kind == "alternates":
        (source / ".git/objects/info/alternates").write_text(str(tmp_path) + "\n")
    else:
        if kind == "lfs":
            (source / "asset").write_text(
                "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 3\n"
            )
            git.run("add", "asset")
        elif kind == "symlink":
            (source / "escape").symlink_to(tmp_path / "outside")
            git.run("add", "escape")
        else:
            git.run("update-index", "--add", "--cacheinfo", f"160000,{baseline},dependency")
        git.run("commit", "--quiet", "-m", "unsupported")
        baseline = git.run("rev-parse", "HEAD").stdout.decode().strip()
    with pytest.raises(InputError):
        capture_repository(store, source, baseline)


@pytest.mark.integration
def test_finite_tree_and_audit_limits(
    historical_repo: tuple[Path, str, str, str], store: ObjectStore
) -> None:
    source, baseline, _, _ = historical_repo
    with pytest.raises(LimitError, match="file limit"):
        capture_repository(store, source, baseline, limits=SnapshotLimits(max_files=1))
    with pytest.raises(LimitError):
        capture_repository(store, source, baseline, limits=SnapshotLimits(max_bytes=1))
    identifier = capture_repository(
        store, source, baseline, limits=SnapshotLimits(max_disallowed_commits=1)
    )
    snapshot = store.load(identifier, RepositorySnapshot, kind=ObjectKind.REPOSITORY)
    assert snapshot.audit_coverage == "partial"
    assert len(snapshot.known_disallowed_commits) == 1


@pytest.mark.integration
def test_internal_symlink_and_executable_preserved(
    historical_repo: tuple[Path, str, str, str], tmp_path: Path, store: ObjectStore
) -> None:
    source, _, _, _ = historical_repo
    git = Git(source, SnapshotLimits())
    git.environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "f@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "f@example.invalid",
        }
    )
    (source / "link").symlink_to("history.txt")
    (source / "executable").write_text("hello\n")
    (source / "executable").chmod(0o755)
    git.run("add", "link", "executable")
    git.run("commit", "--quiet", "-m", "links")
    baseline = git.run("rev-parse", "HEAD").stdout.decode().strip()
    identifier = capture_repository(store, source, baseline)
    target = tmp_path / "workspace"
    materialize_repository(store, identifier, target)
    assert os.readlink(target / "link") == "history.txt"
    assert (target / "executable").stat().st_mode & 0o111 == 0o111
    with pytest.raises(InputError, match="already exist"):
        materialize_repository(store, identifier, target)


@pytest.mark.integration
def test_hash_valid_pack_with_future_objects_rejected(
    historical_repo: tuple[Path, str, str, str], tmp_path: Path, store: ObjectStore
) -> None:
    from dryheave.errors import IntegrityError

    source, baseline, future, _ = historical_repo
    identifier = capture_repository(store, source, baseline)
    snapshot = store.load(identifier, RepositorySnapshot, kind=ObjectKind.REPOSITORY)
    future_pack = (
        Git(source, SnapshotLimits())
        .run("pack-objects", "--stdout", "--revs", input_bytes=f"{future}\n".encode())
        .stdout
    )
    injected = store.put(
        ObjectKind.REPOSITORY,
        snapshot,
        files={"tree.tar": store.read_blob(identifier, "tree.tar"), "ancestry.pack": future_pack},
    )
    with pytest.raises(IntegrityError, match="outside the exact"):
        materialize_repository(store, injected, tmp_path / "injected-workspace")
    assert not (tmp_path / "injected-workspace").exists()


def symlink_input(
    historical_repo: tuple[Path, str, str, str], links: dict[str, str], mode: str
) -> tuple[str, bytes | None]:
    source, baseline, _, _ = historical_repo
    git = Git(source, SnapshotLimits())
    git.environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "f@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "f@example.invalid",
        }
    )
    git.run("read-tree", baseline)
    (source / "dir").mkdir()
    (source / "dir/file").write_bytes(b"inside directory\n")
    (source / "outside").write_bytes(b"inside root\n")
    git.run("add", "dir/file", "outside")
    for path, target in links.items():
        oid = (
            git.run("hash-object", "-w", "--stdin", input_bytes=target.encode())
            .stdout.decode()
            .strip()
        )
        git.run("update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")
    if mode == "initial_patch":
        return baseline, git.run("diff", "--cached", "--binary", "--no-ext-diff", baseline).stdout
    tree = git.run("write-tree").stdout.decode().strip()
    commit = (
        git.run("commit-tree", tree, "-p", baseline, input_bytes=b"links\n").stdout.decode().strip()
    )
    return commit, None


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["baseline", "initial_patch"])
@pytest.mark.parametrize(
    ("links", "error"),
    [
        ({"dir/up": ".", "escape": "dir/up/../../outside"}, "External"),
        ({"first": "second", "second": "first"}, "Cyclic"),
        ({"link": "missing/../outside"}, "Dangling"),
        ({"link": "dir/file/../outside"}, "non-directory"),
        ({"link": "dir/file/"}, "non-directory"),
        ({"file-link": "dir/file", "link": "file-link/../outside"}, "non-directory"),
    ],
)
def test_unsafe_symlinks_rejected_before_materialization(
    mode: str,
    links: dict[str, str],
    error: str,
    historical_repo: tuple[Path, str, str, str],
    store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit, patch = symlink_input(historical_repo, links, mode)
    write_tree = Mock(wraps=repositories._write_tree)
    monkeypatch.setattr(repositories, "_write_tree", write_tree)
    with pytest.raises(InputError, match=error):
        capture_repository(store, historical_repo[0], commit, initial_patch=patch)
    write_tree.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["baseline", "initial_patch"])
def test_symlinks_resolve_components_in_filesystem_order(
    mode: str,
    historical_repo: tuple[Path, str, str, str],
    tmp_path: Path,
    store: ObjectStore,
) -> None:
    links = {
        "dir/up": ".",
        "root": ".",
        "link": "dir/up/../outside",
        "repeated": "dir/up/../dir/up/../outside",
    }
    commit, patch = symlink_input(historical_repo, links, mode)
    identifier = capture_repository(store, historical_repo[0], commit, initial_patch=patch)
    target = tmp_path / "workspace"
    materialize_repository(store, identifier, target)
    assert (target / "link").read_bytes() == b"inside root\n"
    assert (target / "repeated").read_bytes() == b"inside root\n"
    assert (target / "root").resolve() == target.resolve()
    for path, destination in links.items():
        assert os.readlink(target / path) == destination
