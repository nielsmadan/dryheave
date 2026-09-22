import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dryheave.errors import LimitError, LockBusyError, PathError
from dryheave.filesystem import (
    atomic_write,
    directory_fd,
    file_lock,
    publish_directory,
    read_bytes,
    read_chunks,
    read_prefix,
    write_all,
)


def _descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def test_atomic_files_have_private_permissions_and_create_only_behavior(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "file.json"
    atomic_write(path, b"first", replace=False)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        atomic_write(path, b"second", replace=False)
    assert read_bytes(path, limit=10) == b"first"
    assert set(path.parent.iterdir()) == {path}
    atomic_write(path, b"third")
    assert read_bytes(path, limit=10) == b"third"
    assert set(path.parent.iterdir()) == {path}


@pytest.mark.parametrize("replace", [False, True])
def test_atomic_write_syncs_content_then_final_directory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    path = tmp_path / "file"
    synced: list[tuple[str, bytes | set[str]]] = []
    original = os.fsync

    def record_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced.append(("directory", set(os.listdir(descriptor))))
        else:
            synced.append(("file", next(tmp_path.iterdir()).read_bytes()))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_sync)
    atomic_write(path, b"durable", replace=replace)
    assert synced == [("file", b"durable"), ("directory", {"file"})]
    assert path.read_bytes() == b"durable"


def test_interrupted_replace_preserves_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file"
    atomic_write(path, b"old")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("ancestor", [False, True])
def test_no_symlink_following(tmp_path: Path, ancestor: bool) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "value").write_bytes(b"protected")
    link = tmp_path / "link"
    link.symlink_to(target if ancestor else target / "value")
    path = link / "value" if ancestor else link
    with pytest.raises(PathError):
        read_bytes(path, limit=100)
    with pytest.raises(PathError):
        atomic_write(path, b"changed")
    assert (target / "value").read_bytes() == b"protected"


def test_special_files_rejected_without_opening_for_blocking_io(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(PathError, match="regular"):
        read_bytes(fifo, limit=100)
    with pytest.raises(PathError, match="regular"):
        atomic_write(fifo, b"content")


def test_read_limit_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"12345")
    with pytest.raises(LimitError):
        read_bytes(path, limit=4)
    assert read_bytes(path, limit=5) == b"12345"


def test_read_prefix_reports_truncation_at_the_limit_boundary(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"12345")
    assert read_prefix(path, limit=5) == (b"12345", False)
    assert read_prefix(path, limit=6) == (b"12345", False)
    assert read_prefix(path, limit=4) == (b"1234", True)
    assert read_prefix(path, limit=0) == (b"", True)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert read_prefix(empty, limit=0) == (b"", False)


def test_read_prefix_refuses_irregular_and_missing_files(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(PathError, match="regular"):
        read_prefix(fifo, limit=100)
    with pytest.raises(FileNotFoundError):
        read_prefix(tmp_path / "absent", limit=100)


def test_read_chunks_streams_exactly_up_to_its_limit(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"0123456789")
    assert list(read_chunks(path, limit=10, size=4)) == [b"0123", b"4567", b"89"]
    assert b"".join(read_chunks(path, limit=11, size=4)) == b"0123456789"
    with pytest.raises(LimitError, match="9-byte limit"):
        list(read_chunks(path, limit=9, size=4))
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert list(read_chunks(empty, limit=0)) == []


def test_read_chunks_refuses_a_file_that_grows_past_its_limit(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"a" * 8)
    chunks = read_chunks(path, limit=8, size=4)
    assert next(chunks) == b"aaaa"
    with path.open("ab") as stream:
        stream.write(b"b" * 8)
    with pytest.raises(LimitError, match="8-byte limit"):
        list(chunks)


def test_read_chunks_refuses_irregular_and_missing_files(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(PathError, match="regular"):
        list(read_chunks(fifo, limit=100))
    with pytest.raises(FileNotFoundError):
        list(read_chunks(tmp_path / "absent", limit=100))


def test_partially_consumed_read_chunks_releases_its_descriptors(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"0123456789")
    baseline = _descriptors()
    chunks = read_chunks(path, limit=10, size=4)
    assert next(chunks) == b"0123"
    assert _descriptors() > baseline
    chunks.close()
    assert _descriptors() == baseline


def test_locks_are_exclusive_and_released_on_error(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with pytest.raises(ValueError, match="stop"), file_lock(path):
        with pytest.raises(LockBusyError), file_lock(path):
            pytest.fail("a second owner acquired the lock")
        raise ValueError("stop")
    with file_lock(path):
        assert path.is_file()


@pytest.mark.integration
def test_lock_excludes_another_process(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    program = """import sys
from pathlib import Path
from dryheave.filesystem import file_lock
from dryheave.errors import LockBusyError
try:
    with file_lock(Path(sys.argv[1])):
        print('acquired')
except LockBusyError:
    print('busy')
"""
    with file_lock(path):
        result = subprocess.run(
            [sys.executable, "-c", program, str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.stdout == "busy\n"
    result = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout == "acquired\n"


def test_write_all_handles_partial_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.write
    monkeypatch.setattr(os, "write", lambda descriptor, content: original(descriptor, content[:2]))
    atomic_write(tmp_path / "file", b"abcdefg")
    assert (tmp_path / "file").read_bytes() == b"abcdefg"


def test_write_all_rejects_zero_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "write", lambda _descriptor, _content: 0)
    with (tmp_path / "file").open("wb") as stream, pytest.raises(OSError, match="no progress"):
        write_all(stream.fileno(), b"data")


def test_directory_paths_and_publication_collision(tmp_path: Path) -> None:
    with pytest.raises(PathError), directory_fd(tmp_path / ".."):
        pytest.fail("accepted parent component")
    staged, destination = tmp_path / "pending", tmp_path / "published"
    staged.mkdir()
    destination.mkdir()
    with pytest.raises(FileExistsError):
        publish_directory(staged, destination)
    with pytest.raises(PathError):
        publish_directory(staged, destination / "nested")
