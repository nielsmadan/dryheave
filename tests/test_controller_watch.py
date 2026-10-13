import os
import threading
from contextlib import nullcontext

import pytest

from dryheave.controller_watch import MAX_CONTROLLER_DEPTH, MAX_RUNTIME_FILES, ControllerWatch


def test_vanishing_atomic_temporary_file_does_not_fail_observation(tmp_path, monkeypatch):
    class VanishingEntry:
        name = "temporary"

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            raise FileNotFoundError("An atomic publication removed its temporary name.")

    monkeypatch.setattr(
        "dryheave.controller_watch.os.scandir",
        lambda _descriptor: nullcontext(iter([VanishingEntry()])),
    )
    watch = ControllerWatch(tmp_path, threading.Event(), max_bytes=1000)
    watch.close()
    assert watch.error is None
    assert watch.cancelled.is_set() is False


@pytest.mark.parametrize("limit", ["bytes", "count"])
def test_watch_combines_evidence_and_runtime_limits(tmp_path, limit):
    evidence, runtime = tmp_path / "evidence", tmp_path / "runtime"
    evidence.mkdir()
    runtime.mkdir()
    (evidence / "output").write_bytes(b"e" * 6)
    (runtime / "output").write_bytes(b"r" * 6)
    watch = ControllerWatch(
        (evidence, runtime),
        threading.Event(),
        max_bytes=10 if limit == "bytes" else 100,
        max_files=1 if limit == "count" else 10,
    )
    watch.close()
    assert watch.error == "controller_file_limit"
    assert watch.cancelled.is_set()


def nested_tree(root, depth):
    directory = root
    for level in range(depth):
        directory = directory / f"level-{level}"
        directory.mkdir(parents=True)
        (directory / "data").write_bytes(b"x" * 3)
    return 2 * depth, 3 * depth


@pytest.mark.parametrize(
    ("spare_files", "spare_bytes", "error"),
    [(0, 0, None), (-1, 0, "controller_file_limit"), (0, -1, "controller_file_limit")],
)
def test_a_nested_tree_is_measured_exactly(tmp_path, spare_files, spare_bytes, error):
    entries, size = nested_tree(tmp_path / "evidence", MAX_CONTROLLER_DEPTH)
    watch = ControllerWatch(
        tmp_path / "evidence",
        threading.Event(),
        max_bytes=size + spare_bytes,
        max_files=entries + spare_files,
    )
    watch.close()
    assert watch.error == error


def test_a_nested_tree_opens_each_directory_once(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    entries, _size = nested_tree(root, MAX_CONTROLLER_DEPTH)
    watch = ControllerWatch(root, threading.Event(), max_bytes=1000, max_files=entries)
    watch.close()
    opened = []
    real_open = os.open

    def counting_open(path, flags, *args, **kwargs):
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("dryheave.controller_watch.os.open", counting_open)
    watch.close()
    assert (watch.error, len(opened)) == (
        None,
        len(root.absolute().parts) + 1 + MAX_CONTROLLER_DEPTH,
    )


def test_a_tree_deeper_than_the_limit_is_refused(tmp_path):
    nested_tree(tmp_path / "evidence", MAX_CONTROLLER_DEPTH + 1)
    watch = ControllerWatch(tmp_path / "evidence", threading.Event(), max_bytes=1000)
    watch.close()
    assert watch.error == "controller_file_depth"


@pytest.mark.parametrize("evidence_files", [0, 1])
def test_the_runtime_file_limit_admits_a_full_runtime_and_refuses_one_more(
    tmp_path, evidence_files
):
    evidence, runtime = tmp_path / "evidence", tmp_path / "runtime"
    evidence.mkdir()
    runtime.mkdir()
    for index in range(evidence_files):
        (evidence / f"e{index}").touch()
    for index in range(MAX_RUNTIME_FILES - evidence_files):
        (runtime / f"r{index}").touch()
    full = ControllerWatch(
        (evidence, runtime), threading.Event(), max_bytes=1000, max_files=MAX_RUNTIME_FILES
    )
    full.close()
    (runtime / "extra").touch()
    over = ControllerWatch(
        (evidence, runtime), threading.Event(), max_bytes=1000, max_files=MAX_RUNTIME_FILES
    )
    over.close()
    assert (full.error, over.error) == (None, "controller_file_limit")


@pytest.mark.parametrize("change", ["none", "replacement", "extra"])
def test_watch_allows_only_exact_owned_auth_symlink_without_following(tmp_path, change):
    evidence, runtime = tmp_path / "evidence", tmp_path / "runtime"
    evidence.mkdir()
    (runtime / "config").mkdir(parents=True)
    link = runtime / "config/auth.json"
    link.symlink_to(tmp_path / "nonexistent-opaque-source")
    info = link.lstat()
    owned = {link: (info.st_dev, info.st_ino, info.st_ctime_ns)}
    if change == "replacement":
        link.unlink()
        link.symlink_to(evidence)
    elif change == "extra":
        (runtime / "config/extra.json").symlink_to(link)
    watch = ControllerWatch(
        (evidence, runtime), threading.Event(), max_bytes=1000, owned_symlinks=owned
    )
    watch.close()
    assert watch.error == (None if change == "none" else "controller_special_file")
    assert watch.cancelled.is_set() == (change != "none")
