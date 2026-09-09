import os
import signal
import sys
import time
from pathlib import Path

import pytest

from dryheave.errors import InputError
from dryheave.models import CommandSpec
from dryheave.processes import run_command


def test_full_duplex_and_nonzero(tmp_path: Path) -> None:
    result = run_command(
        CommandSpec(
            argv=(
                sys.executable,
                "-c",
                'import sys; data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);sys.stderr.write("failure");sys.exit(7)',
            ),
            max_output_bytes=500000,
        ),
        tmp_path,
        input_bytes=b"a" * 200000,
    )
    assert (result.returncode, result.outcome) == (7, "exited")
    assert result.stdout == b"a" * 200000
    assert result.stderr == b"failure"


def test_output_and_deadline_limits(tmp_path: Path) -> None:
    result = run_command(
        CommandSpec(
            argv=(sys.executable, "-c", 'while True: print("a"*10000)'), max_output_bytes=100
        ),
        tmp_path,
    )
    assert (result.outcome, len(result.stdout)) == ("output_limit", 100)
    timed = run_command(
        CommandSpec(argv=(sys.executable, "-c", "import time; time.sleep(10)"), timeout_seconds=1),
        tmp_path,
    )
    assert timed.outcome == "timeout"
    assert timed.returncode == -signal.SIGKILL


def test_inherited_pipe_child_is_stopped(tmp_path: Path) -> None:
    code = 'import os,time; child=os.fork(); time.sleep(5) if child==0 else None; open("survived","w").write("bad") if child==0 else None'
    result = run_command(
        CommandSpec(argv=(sys.executable, "-c", code), timeout_seconds=1), tmp_path
    )
    assert result.outcome == "timeout"
    assert not (tmp_path / "survived").exists()


def test_closed_pipe_descendant_coverage_is_explicit(tmp_path: Path) -> None:
    code = 'import os,time; from pathlib import Path; child=os.fork(); [(os.close(i)) for i in (0,1,2)] if child==0 else None; time.sleep(0.1) if child==0 else None; Path("child-pending").write_text("done") if child==0 else None; os.rename("child-pending", "child-finished") if child==0 else None'
    result = run_command(CommandSpec(argv=(sys.executable, "-c", code)), tmp_path)
    assert result.outcome == "exited"
    assert "pipe-closing descendants are unobserved" in result.cleanup_coverage
    deadline = time.monotonic() + 2
    while not (tmp_path / "child-finished").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "child-finished").read_text() == "done"


def test_reaped_leader_is_never_signaled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(os, "killpg", lambda *args: calls.append(args))
    result = run_command(CommandSpec(argv=(sys.executable, "-c", 'print("done")')), tmp_path)
    assert result.stdout == b"done\n"
    assert calls == []


def test_symlink_cwd_rejected(tmp_path: Path) -> None:
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(InputError, match="escapes"):
        run_command(CommandSpec(argv=("true",), cwd="escape"), tmp_path)


def test_parent_cancellation_interrupts_a_child_with_closed_pipes(tmp_path):
    import threading
    import time

    from dryheave.processes import CommandControl

    cancelled = threading.Event()
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_command(
            CommandSpec(
                argv=(
                    sys.executable,
                    "-c",
                    "import os,time; os.close(0); os.close(1); os.close(2); time.sleep(10)",
                ),
                timeout_seconds=10,
            ),
            tmp_path,
            control=CommandControl(cancelled=cancelled),
        )
    finally:
        timer.cancel()
    assert result.outcome == "timeout"
    assert time.monotonic() - started < 2
