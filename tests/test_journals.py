import json
import os
from pathlib import Path

import pytest

from dryheave.errors import (
    ConflictError,
    InputError,
    IntegrityError,
    LimitError,
    LockBusyError,
    NotFoundError,
)
from dryheave.journals import RunStore
from dryheave.serialization import canonical_json, digest


def test_durable_events_checkpoints_and_results_survive_reopen(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier, experiment_id="a" * 64) as journal:
        assert journal.events == ()
        assert journal.read_checkpoint() is None
        assert journal.read_result() is None
        empty = journal.checkpoint({"status": "reserved"})
        assert empty.sequence == 0
        first = journal.append(
            "preparing", {"stage": "preparing"}, trial_id="trial-1", attempt_id="attempt-1"
        )
        second = journal.append("launch-intent", {"session": "unique-terminal-session"})
        checkpoint = journal.checkpoint({"status": "launching"})
        assert checkpoint.sequence == 2
        assert checkpoint.event_hash == digest(canonical_json(second))
        assert second.previous_hash == digest(canonical_json(first))
        journal.write_result({"status": "interrupted", "usage": None})
    with RunStore(tmp_path).open(identifier) as reopened:
        assert [event.sequence for event in reopened.events] == [1, 2]
        assert reopened.read_checkpoint() == checkpoint
        assert reopened.read_result().state == {"status": "interrupted", "usage": None}
        assert reopened.metadata.experiment_id == "a" * 64
    with pytest.raises(ConflictError, match="closed"):
        journal.append("late-write")


def test_torn_final_record_is_preserved_then_recovered(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        journal.append("reserved")
        journal.checkpoint({"status": "reserved"})
        path = journal.path
    with (path / "events.jsonl").open("ab") as stream:
        stream.write(b'{"sequence":2,"incomplete')
    with runs.open(identifier) as recovered:
        assert recovered.recovered_tail == b'{"sequence":2,"incomplete'
        assert len(recovered.events) == 1
        assert next(path.glob("torn-*.bin")).read_bytes() == recovered.recovered_tail
        assert recovered.append("interrupted").sequence == 2
    with runs.open(identifier) as reopened:
        assert [event.event for event in reopened.events] == ["reserved", "interrupted"]
        assert reopened.recovered_tail == b""


@pytest.mark.parametrize("mutation", ["bad-line", "sequence-gap", "hash-chain", "noncanonical"])
def test_complete_journal_corruption_is_never_recovered(tmp_path: Path, mutation: str) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        journal.append("reserved")
        journal.append("preparing")
        path = journal.path / "events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    if mutation == "bad-line":
        lines[1] = b'{"incomplete":\n'
    else:
        data = json.loads(lines[1])
        if mutation == "sequence-gap":
            data["sequence"] = 3
        elif mutation == "hash-chain":
            data["previous_hash"] = "b" * 64
        if mutation == "noncanonical":
            lines[1] = json.dumps(data).encode() + b"\n"
        else:
            lines[1] = canonical_json(data) + b"\n"
    corrupted = b"".join(lines)
    path.write_bytes(corrupted)
    with pytest.raises(IntegrityError), runs.open(identifier):
        pytest.fail("corruption accepted")
    assert path.read_bytes() == corrupted


def test_checkpoint_can_lag_but_not_lead_journal(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        journal.append("reserved")
        journal.checkpoint({"step": 1})
        journal.append("preparing")
        path = journal.path / "checkpoint.json"
    with runs.open(identifier) as journal:
        assert journal.read_checkpoint().sequence == 1
        assert len(journal.events) == 2
    checkpoint = json.loads(path.read_bytes())
    checkpoint["sequence"] = 3
    path.write_bytes(canonical_json(checkpoint))
    with pytest.raises(IntegrityError, match="durable"), runs.open(identifier):
        pytest.fail("checkpoint exceeded journal")


@pytest.mark.parametrize(
    "field,value", [("event_hash", "b" * 64), ("run_id", "b" * 32), ("schema_version", 2)]
)
def test_checkpoint_identity_hash_and_schema_are_verified(
    tmp_path: Path, field: str, value: object
) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        journal.append("reserved")
        journal.checkpoint({})
        path = journal.path / "checkpoint.json"
    checkpoint = json.loads(path.read_bytes())
    checkpoint[field] = value
    path.write_bytes(canonical_json(checkpoint))
    with pytest.raises(IntegrityError), runs.open(identifier):
        pytest.fail("checkpoint corruption accepted")


@pytest.mark.parametrize("name", ["checkpoint.json", "result.json"])
@pytest.mark.parametrize("number", [b"1e400", b"-1e400"])
def test_snapshot_reads_reject_nested_overflow(tmp_path: Path, name: str, number: bytes) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        journal.append("reserved")
        snapshot = journal.checkpoint({"sensitive-key": [{"value": "overflow"}]})
        content = canonical_json(snapshot).replace(b'"overflow"', number)
        path = journal.path / name
        path.write_bytes(content)
        read = journal.read_checkpoint if name == "checkpoint.json" else journal.read_result
        with pytest.raises(IntegrityError) as caught:
            read()
        assert str(caught.value) == f"Invalid {name}. JSON contains a non-finite numeric value."
        assert path.read_bytes() == content


def test_run_lock_and_pinned_experiment_prevent_duplicate_resume(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier), pytest.raises(LockBusyError), RunStore(tmp_path).open(identifier):
        pytest.fail("second resume acquired run")
    with (
        pytest.raises(ConflictError, match="pinned experiment"),
        runs.open(identifier, experiment_id="b" * 64),
    ):
        pytest.fail("resumed different experiment")


def test_event_data_cannot_mutate_the_durable_sequence(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        returned = journal.append("reserved", {"nested": {"value": "original"}})
        returned.data["nested"]["value"] = "mutated"
        journal.events[0].data["nested"] = "changed"
        assert journal.events[0].data == {"nested": {"value": "original"}}
        journal.append("preparing")
        journal.checkpoint({})
    with runs.open(identifier) as journal:
        assert len(journal.events) == 2


def test_append_failure_invalidates_handle_and_allows_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dryheave import journals

    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)

    def partial(descriptor: int, _content: bytes) -> None:
        os.write(descriptor, b'{"torn')
        raise OSError("disk write failed")

    with runs.open(identifier) as journal:
        journal.append("reserved")
        with monkeypatch.context() as patch:
            patch.setattr(journals, "write_all", partial)
            with pytest.raises(OSError, match="disk"):
                journal.append("launching")
        with pytest.raises(ConflictError, match="closed"):
            journal.append("interacting")
    with runs.open(identifier) as journal:
        assert [event.event for event in journal.events] == ["reserved"]
        assert journal.append("interrupted").sequence == 2


def test_append_limits_and_invalid_event_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dryheave import journals

    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        with pytest.raises(InputError):
            journal.append("invalid/event")
        with monkeypatch.context() as patch:
            patch.setattr(journals, "MAX_EVENT_BYTES", 10)
            with pytest.raises(LimitError):
                journal.append("reserved")
        with monkeypatch.context() as patch:
            patch.setattr(journals, "MAX_JOURNAL_BYTES", 10)
            with pytest.raises(LimitError):
                journal.append("reserved")
        assert journal.events == ()


def test_run_identity_validation(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    with pytest.raises(InputError):
        runs.create("alias")
    with pytest.raises(InputError), runs.open("../escape"):
        pytest.fail("accepted path traversal")
    with pytest.raises(NotFoundError), runs.open("a" * 32):
        pytest.fail("accepted absent run")


def test_oversized_checkpoint_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dryheave import journals

    runs = RunStore(tmp_path)
    identifier = runs.create("a" * 64)
    with runs.open(identifier) as journal:
        journal.checkpoint({"status": "reserved"})
        with monkeypatch.context() as patch:
            patch.setattr(journals, "MAX_MANIFEST_BYTES", 10)
            with pytest.raises(LimitError):
                journal.checkpoint({"status": "preparing"})
        assert journal.read_checkpoint().state == {"status": "reserved"}


def test_read_only_inspection_does_not_lock_or_repair_active_tail(tmp_path):
    from dryheave.journals import RunStore

    runs = RunStore(tmp_path / "store")
    run_id = runs.create("a" * 64)
    with runs.open(run_id) as writer:
        writer.append("observed", {"stage": "working"})
        path = writer.path / "events.jsonl"
        original = path.read_bytes()
        with path.open("ab") as stream:
            stream.write(b'{"partial":')
        view = runs.inspect(run_id)
        assert len(view.events) == 1
        assert view.events[0].data == {"stage": "working"}
        assert path.read_bytes() == original + b'{"partial":'
        assert list(writer.path.glob("torn-*")) == []
