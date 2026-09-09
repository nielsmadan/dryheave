import threading
from contextlib import nullcontext

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
