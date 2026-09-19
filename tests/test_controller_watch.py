import threading
from contextlib import nullcontext

import pytest

from dryheave.controller_watch import ControllerWatch


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
