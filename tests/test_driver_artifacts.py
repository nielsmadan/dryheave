import pytest

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.errors import LimitError, PathError


def test_artifacts_persist_exact_bytes_and_refuse_symlink_parent(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 100)
    writer.append("raw.jsonl", b"one\n")
    writer.append("raw.jsonl", b"two\n")
    assert (writer.root / "raw.jsonl").read_bytes() == b"one\ntwo\n"
    original = tmp_path / "preserved"
    writer.root.rename(original)
    writer.root.symlink_to(original, target_is_directory=True)
    with pytest.raises(PathError):
        writer.append("raw.jsonl", b"unexpected\n")
    assert (original / "raw.jsonl").read_bytes() == b"one\ntwo\n"


def test_artifact_bounds_preserve_prior_evidence(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 4)
    writer.append("raw.jsonl", b"one\n")
    with pytest.raises(LimitError):
        writer.append("raw.jsonl", b"two\n")
    assert (writer.root / "raw.jsonl").read_bytes() == b"one\n"
