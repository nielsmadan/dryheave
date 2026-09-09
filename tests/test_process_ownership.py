import subprocess
import sys
import time
from contextlib import suppress

import psutil
import pytest

from dryheave.errors import LimitError
from dryheave.process_ownership import ProcessOwner


class FakeProcess:
    def __init__(self, pid=42, created=1.0, children=()):
        self.pid = pid
        self.created = created
        self.descendants = children
        self.running = True
        self.signals = []

    def create_time(self):
        return self.created

    def ppid(self):
        return 1

    def children(self):
        return self.descendants

    def is_running(self):
        return self.running

    def status(self):
        return psutil.STATUS_RUNNING

    def terminate(self):
        self.signals.append("terminate")
        self.running = False

    def kill(self):
        self.signals.append("kill")
        self.running = False

    def wait(self, timeout=0):
        return None


def test_identity_mismatch_is_never_signaled(monkeypatch):
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    owner = ProcessOwner()
    old = FakeProcess()
    owner.add(old)
    old.running = False
    report = owner.stop(timeout=0.1, terminal_closed=True)
    assert old.signals == []
    assert report.known_writers_stopped
    assert report.owned[0].created == 1.0


def test_retained_child_is_signaled_after_parent_disappears(monkeypatch):
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    child = FakeProcess(pid=43)
    parent = FakeProcess(children=(child,))
    owner = ProcessOwner()
    owner.add(parent)
    owner.scan()
    parent.running = False
    parent.descendants = ()
    report = owner.stop(timeout=0.1, terminal_closed=True)
    assert parent.signals == []
    assert child.signals == ["terminate"]
    assert {item.pid for item in report.owned} == {42, 43}
    assert report.known_writers_stopped
    assert report.coverage == "partial-native"


def test_process_limit_is_visible_and_known_children_are_stopped(monkeypatch):
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    owner = ProcessOwner(max_processes=1)
    owner.add(FakeProcess(children=(FakeProcess(pid=43),)))
    with pytest.raises(LimitError):
        owner.scan()
    report = owner.stop(timeout=0.1, terminal_closed=False)
    assert report.errors == ("process_limit",)
    assert not report.known_writers_stopped


def test_real_detached_writer_is_retained_and_stopped(tmp_path):
    marker = tmp_path / "heartbeat"
    gate = tmp_path / "release-parent"
    child_code = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nend=time.monotonic()+5\nwhile time.monotonic()<end:\n with p.open('a') as f: f.write('beat\\n')\n time.sleep(.02)\n"
    parent_code = "import pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nprint(p.pid,flush=True)\nend=time.monotonic()+4\nwhile not pathlib.Path(sys.argv[3]).exists() and time.monotonic()<end: time.sleep(.01)\n"
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", parent_code, child_code, str(marker), str(gate)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    owner = ProcessOwner(interval=0.01)
    child = None
    try:
        owner.add(psutil.Process(process.pid))
        deadline = time.monotonic() + 3
        while not marker.exists():
            if time.monotonic() >= deadline:
                pytest.fail("Fixture writer did not start within its bound.")
            time.sleep(0.01)
        owner.scan()
        gate.touch()
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr
        child = psutil.Process(int(stdout))
        assert child.is_running()
        assert child.pid in {item.pid for item in owner.identities.values()}
        report = owner.stop(timeout=1, terminal_closed=True)
        assert report.known_writers_stopped
        at_close = marker.read_bytes()
        time.sleep(0.08)
        assert marker.read_bytes() == at_close
    finally:
        owner.stop(timeout=1, terminal_closed=False)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=1)
        if child is not None:
            with suppress(psutil.NoSuchProcess):
                child.kill()
