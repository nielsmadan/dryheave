import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from dryheave.errors import InputError
from dryheave.models import CommandSpec
from dryheave.processes import CommandControl, run_command


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_inherited_pipe_child_is_stopped(tmp_path: Path) -> None:
    code = 'import os,time; child=os.fork(); time.sleep(5) if child==0 else None; open("survived","w").write("bad") if child==0 else None'
    result = run_command(
        CommandSpec(argv=(sys.executable, "-c", code), timeout_seconds=1), tmp_path
    )
    assert result.outcome == "timeout"
    assert not (tmp_path / "survived").exists()


@pytest.mark.integration
def test_closed_pipe_descendant_coverage_is_explicit(tmp_path: Path) -> None:
    code = 'import os,time; from pathlib import Path; child=os.fork(); [(os.close(i)) for i in (0,1,2)] if child==0 else None; time.sleep(0.1) if child==0 else None; Path("child-pending").write_text("done") if child==0 else None; os.rename("child-pending", "child-finished") if child==0 else None'
    result = run_command(CommandSpec(argv=(sys.executable, "-c", code)), tmp_path)
    assert result.outcome == "exited"
    assert "pipe-closing descendants are unobserved" in result.cleanup_coverage
    deadline = time.monotonic() + 2
    while not (tmp_path / "child-finished").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "child-finished").read_text() == "done"


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_output_written_before_cancellation_is_retained(tmp_path: Path) -> None:
    started = tmp_path / "started"
    cancelled = threading.Event()

    def cancel_after_child_writes() -> None:
        if started.exists() and not cancelled.is_set():
            time.sleep(1.0)
            cancelled.set()

    result = run_command(
        CommandSpec(
            argv=(
                sys.executable,
                "-c",
                "import pathlib,sys,time\n"
                f"pathlib.Path({str(started)!r}).write_text('1')\n"
                "time.sleep(0.5)\n"
                'sys.stdout.write("usage=12\\n")\n'
                "sys.stdout.flush()\n"
                "time.sleep(30)\n",
            ),
            max_output_bytes=100000,
        ),
        tmp_path,
        control=CommandControl(cancelled=cancelled, on_poll=cancel_after_child_writes),
    )
    assert result.outcome == "timeout"
    assert result.stdout == b"usage=12\n"


@pytest.mark.integration
def test_output_clipped_while_draining_reports_the_output_limit(tmp_path: Path) -> None:
    started = tmp_path / "started"
    cancelled = threading.Event()

    def cancel_after_child_writes() -> None:
        if started.exists() and not cancelled.is_set():
            time.sleep(1.0)
            cancelled.set()

    result = run_command(
        CommandSpec(
            argv=(
                sys.executable,
                "-c",
                "import pathlib,sys,time\n"
                f"pathlib.Path({str(started)!r}).write_text('1')\n"
                "time.sleep(0.5)\n"
                'sys.stdout.write("x" * 100000)\n'
                "sys.stdout.flush()\n"
                "time.sleep(30)\n",
            ),
            max_output_bytes=100,
        ),
        tmp_path,
        control=CommandControl(cancelled=cancelled, on_poll=cancel_after_child_writes),
    )
    assert result.outcome == "output_limit"
    assert result.stdout == b"x" * 100


@pytest.mark.integration
def test_drain_does_not_block_when_the_start_callback_fails(tmp_path: Path) -> None:
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import pathlib, sys, time\n"
        "stop = pathlib.Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 60\n"
        "while not stop.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, subprocess, sys, time\n"
        'sys.stdout.write("usage=12\\n")\n'
        "sys.stdout.flush()\n"
        "subprocess.Popen(\n"
        "    [sys.executable, sys.argv[1], sys.argv[2]], start_new_session=True\n"
        ")\n"
        "pathlib.Path(sys.argv[3]).write_text('1')\n"
        "time.sleep(60)\n"
    )
    stop = tmp_path / "stop"
    ready = tmp_path / "ready"
    refusal = InputError("The start callback refused this attempt.")

    def on_start(pid: int) -> None:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        raise refusal

    raised: list[InputError] = []
    finished = threading.Event()

    def call() -> None:
        try:
            run_command(
                CommandSpec(
                    argv=(sys.executable, str(child), str(holder), str(stop), str(ready)),
                    max_output_bytes=100000,
                ),
                tmp_path,
                on_start=on_start,
            )
        except InputError as error:
            raised.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    try:
        returned = finished.wait(15)
    finally:
        stop.write_text("1")
        worker.join(60)
    assert ready.exists()
    assert returned
    assert raised == [refusal]
