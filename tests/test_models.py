import json

import pytest
from pydantic import ValidationError

from dryheave.models import (
    CommandSpec,
    EnvironmentReference,
    EvidenceReference,
    Manifest,
    ObjectKind,
    RunCheckpoint,
    TokenUsage,
)


@pytest.mark.parametrize("version", [0, 2, "1", True, 1.0, None])
def test_manifest_rejects_unknown_or_coerced_versions(version: object) -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate_json(
            json.dumps({"schema_version": version, "kind": "case", "payload": {}})
        )


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "a/../b",
        "a/./b",
        "/tmp/file",
        "a//b",
        "a/",
        "a\\b",
        "C:root",
        "a\x00b",
        "a\nb",
        "x" * 4097,
    ],
)
def test_manifest_rejects_unsafe_file_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        Manifest(kind=ObjectKind.PROFILE, payload={}, files={path: "a" * 64})


def test_manifest_rejects_overlapping_file_paths() -> None:
    with pytest.raises(ValidationError, match="ancestors"):
        Manifest(kind=ObjectKind.PROFILE, payload={}, files={"a": "a" * 64, "a/b": "b" * 64})


@pytest.mark.parametrize(
    "references", [("a" * 64, "a" * 64), ("b" * 64, "a" * 64), ("../outside",)]
)
def test_manifest_rejects_noncanonical_references(references: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        Manifest(kind=ObjectKind.CASE, payload={}, references=references)


def test_manifest_rejects_unknown_fields_and_kinds() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Manifest.model_validate_json('{"kind":"case","payload":{},"surprise":1}')
    with pytest.raises(ValidationError):
        Manifest.model_validate_json('{"kind":"unsupported","payload":{}}')


def test_command_keeps_argv_literal_and_credentials_as_references() -> None:
    command = CommandSpec(
        argv=("python", "-c", "print('$HOME; $(whoami)')"),
        environment=(EnvironmentReference(name="MODEL_API_KEY"),),
    )
    restored = CommandSpec.model_validate_json(command.model_dump_json())
    assert restored.argv == ("python", "-c", "print('$HOME; $(whoami)')")
    assert restored.environment[0].model_dump() == {"name": "MODEL_API_KEY", "source": "inherited"}


@pytest.mark.parametrize(
    "data",
    [
        {"argv": []},
        {"argv": [""]},
        {"argv": ["x", "bad\x00argument"]},
        {"argv": "echo hello"},
        {"argv": ["echo"], "timeout_seconds": 0},
        {"argv": ["echo"], "timeout_seconds": "60"},
        {"argv": ["echo"], "cwd": "../parent"},
        {"argv": ["echo"], "max_output_bytes": -1},
        {"argv": ["echo"], "environment": [{"name": "KEY", "value": "credential"}]},
        {"argv": ["echo"], "environment": [{"name": "KEY"}, {"name": "KEY"}]},
    ],
)
def test_command_rejects_invalid_boundaries(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommandSpec.model_validate_json(json.dumps(data))


def test_usage_preserves_unknowns_and_validates_reasoning_subset() -> None:
    usage = TokenUsage(provenance="native-response-1", output=10, reasoning=4)
    assert usage.uncached_input is None
    assert usage.output == 10
    with pytest.raises(ValidationError, match="exceed"):
        TokenUsage(provenance="native-response-1", output=3, reasoning=4)
    with pytest.raises(ValidationError):
        TokenUsage(provenance="native-response-1", cache_read=-1)


def test_evidence_requires_visibility_and_immutable_session_id() -> None:
    evidence = EvidenceReference(session_id="a" * 64, event_id="message-1", visibility="curator")
    assert evidence.visibility == "curator"
    with pytest.raises(ValidationError):
        EvidenceReference.model_validate_json(
            '{"session_id":"alias","event_id":"x","visibility":"subject"}'
        )


def test_checkpoint_sequence_requires_hash() -> None:
    with pytest.raises(ValidationError, match="event hash"):
        RunCheckpoint(
            run_id="a" * 32, experiment_id="b" * 64, sequence=1, event_hash=None, state={}
        )
