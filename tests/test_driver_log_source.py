import json
import os

import pytest

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.fake import FakeTerminal
from dryheave.drivers.log_source import NativeLogSource, native_log_root
from dryheave.drivers.models import DriverLimits
from dryheave.drivers.session import DriverSession
from dryheave.errors import InputError, LimitError
from dryheave.models import AgentKind


def source(tmp_path, **limits):
    root = tmp_path / "logs"
    root.mkdir()
    return NativeLogSource(
        root,
        AgentKind.CODEX,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        DriverLimits(**limits),
    )


def record(kind="session_meta", **payload):
    return json.dumps({"type": kind, "payload": payload}).encode() + b"\n"


@pytest.mark.parametrize(
    "agent,directory", [(AgentKind.CODEX, "sessions"), (AgentKind.CLAUDE, "projects")]
)
def test_native_roots_exclude_plugin_files_and_unrelated_jsonl(tmp_path, agent, directory):
    config = tmp_path / "config"
    plugin = config / "plugins"
    plugin.mkdir(parents=True)
    for index in range(260):
        (plugin / f"asset-{index}.jsonl").write_bytes(b"not a native record\n")
    root = native_log_root(config, agent)
    assert root == config / directory
    reader = NativeLogSource(root, agent, ArtifactWriter(tmp_path / "evidence", 100000))
    assert reader.poll() == ()
    root.mkdir()
    content = (
        record(id="root", cwd="/fixture")
        if agent == AgentKind.CODEX
        else b'{"type":"user","sessionId":"root","uuid":"u","message":{"content":"hello"}}\n'
    )
    (root / "root.jsonl").write_bytes(content)
    events = reader.poll()
    assert events[0].session_id == "root"
    assert reader.cursor().files["root.jsonl"].offset == len(content)
    assert reader.drained()


def test_scoped_log_directory_keeps_entry_limit(tmp_path):
    reader = source(tmp_path, max_files=1)
    (reader.root / "one.jsonl").write_bytes(record(id="one"))
    (reader.root / "two.jsonl").write_bytes(record(id="two"))
    with pytest.raises(LimitError, match="entry limit"):
        reader.poll()


def test_reads_incrementally_preserves_partial_tail_and_raw_bytes(tmp_path):
    reader = source(tmp_path)
    path = reader.root / "root.jsonl"
    metadata = record(id="root", cwd="/fixture")
    accepted = record("event_msg", type="user_message", message="hello", turn_id="t")
    path.write_bytes(metadata + accepted[:20])
    assert [event.kind for event in reader.poll()] == ["metadata"]
    cursor = reader.cursor()
    assert cursor.files["root.jsonl"].offset == len(metadata) + 20
    assert not reader.drained()
    with path.open("ab") as stream:
        stream.write(accepted[20:])
    event = reader.poll()[0]
    assert (event.kind, event.sequence, event.line, event.text) == ("accepted", 2, 2, "hello")
    assert reader.poll() == ()
    assert reader.bytes_read == len(metadata + accepted)
    assert next(reader.artifacts.root.glob("raw-*")).read_bytes() == metadata + accepted
    assert reader.drained()


def test_preexisting_logs_never_become_new_root_evidence(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    path = root / "old.jsonl"
    path.write_bytes(record(id="old"))
    reader = NativeLogSource(root, AgentKind.CODEX, ArtifactWriter(tmp_path / "evidence", 100000))
    with path.open("ab") as stream:
        stream.write(record("event_msg", type="user_message", message="hello", turn_id="old"))
    assert reader.poll() == ()
    assert reader.cursor().files["old.jsonl"].offset == path.stat().st_size


def test_full_pre_submit_cursor_drains_more_than_one_chunk(tmp_path):
    reader = source(tmp_path)
    path = reader.root / "root.jsonl"
    path.write_bytes(
        record(id="root", cwd="/fixture")
        + record("event_msg", type="agent_message", message="x" * 70000, turn_id="old")
    )
    assert len(reader.poll()) == 2
    assert reader.cursor().files["root.jsonl"].offset == path.stat().st_size
    assert reader.poll() == ()


@pytest.mark.parametrize("change", ["truncate", "replace"])
def test_replacement_and_truncation_are_errors(tmp_path, change):
    reader = source(tmp_path)
    path = reader.root / "root.jsonl"
    path.write_bytes(record(id="root"))
    reader.poll()
    if change == "replace":
        replacement = path.with_suffix(".new")
        replacement.write_bytes(record(id="new"))
        os.replace(replacement, path)
    else:
        path.write_bytes(b"")
    with pytest.raises(InputError, match="replaced or truncated"):
        reader.poll()


@pytest.mark.parametrize("limits", [{"max_log_bytes": 8}, {"max_events": 1}, {"max_polls": 1}])
def test_native_read_bounds(tmp_path, limits):
    reader = source(tmp_path, **limits)
    (reader.root / "root.jsonl").write_bytes(record(id="root") * 2)
    with pytest.raises(LimitError):
        reader.poll()
        reader.poll()


@pytest.mark.parametrize(
    "content,limits,message",
    [
        (record(id="root") + b"{malformed}\n", {}, "JSON"),
        (record(id="root") + record(id="changed"), {}, "session identity"),
        (record(id="root") * 2, {"max_events": 1}, "record limit"),
    ],
)
def test_failed_poll_and_session_cleanup_retain_raw_bytes_once(
    tmp_path, driver_plan, content, limits, message
):
    reader = source(tmp_path, **limits)
    terminal = FakeTerminal([])
    session = DriverSession(terminal, reader, reader.artifacts, agent=AgentKind.CODEX)
    with pytest.raises(InputError, match=message), session:
        assert session.start(driver_plan, {}).state == "ready"
        (reader.root / "root.jsonl").write_bytes(content)
        session.prepare("Hello")
    assert terminal.closed
    assert session.cleanup.errors == ("native_log_drain_failed",)
    assert session.cleanup.logs_drained is False
    assert reader.drained() is False
    assert reader.bytes_read == len(content)
    assert next(reader.artifacts.root.glob("raw-*")).read_bytes() == content
    with pytest.raises(InputError, match="cannot resume"):
        reader.poll()
    assert next(reader.artifacts.root.glob("raw-*")).read_bytes() == content
