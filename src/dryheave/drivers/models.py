from typing import Literal

from pydantic import Field, JsonValue

from dryheave.models import ObjectId, StrictModel, TokenUsage


class DriverLimits(StrictModel):
    duration_seconds: float = Field(default=180, gt=0, le=86400)
    command_seconds: float = Field(default=5, gt=0, le=60)
    poll_seconds: float = Field(default=0.1, ge=0.01, le=5)
    cleanup_seconds: float = Field(default=3, gt=0, le=30)
    max_polls: int = Field(default=4000, gt=0, le=100000)
    max_log_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=128 * 1024 * 1024)
    max_artifact_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=512 * 1024 * 1024)
    max_events: int = Field(default=20000, gt=0, le=100000)
    max_files: int = Field(default=256, gt=0, le=10000)
    max_depth: int = Field(default=12, gt=0, le=32)
    max_input_bytes: int = Field(default=32000, gt=0, le=65536)
    max_command_bytes: int = Field(default=1024 * 1024, gt=0, le=8 * 1024 * 1024)
    max_processes: int = Field(default=256, gt=0, le=4096)


class Screen(StrictModel):
    text: str
    exited: int | None
    cols: int = Field(default=120, gt=0, le=1000)
    rows: int = Field(default=40, gt=0, le=1000)
    ready: bool = False
    cwd: str | None = None
    cursor: dict[str, JsonValue] = Field(default_factory=dict)


class FileCursor(StrictModel):
    device: int
    inode: int
    offset: int
    line: int


class EventCursor(StrictModel):
    sequence: int = 0
    files: dict[str, FileCursor] = Field(default_factory=dict)


class NativeEvent(StrictModel):
    sequence: int = 0
    source: str = ""
    line: int = 0
    kind: Literal[
        "metadata",
        "accepted",
        "started",
        "assistant",
        "completed",
        "aborted",
        "usage",
        "tool",
        "hook",
        "unsupported",
    ]
    session_id: str | None = None
    parent_session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    root_turn_id: str | None = None
    native_id: str | None = None
    parent_id: str | None = None
    text: str = ""
    data: dict[str, JsonValue] = Field(default_factory=dict)


class NativeUsage(StrictModel):
    identity: str
    session_id: str | None
    thread_id: str | None
    turn_id: str | None
    root_turn_id: str | None
    response_id: str | None
    source: str
    line: int
    accounting: Literal["response", "cumulative", "superseded", "unknown"]
    usage: TokenUsage | None
    raw: dict[str, JsonValue]


class Submission(StrictModel):
    submission_id: str
    prompt_sha256: ObjectId
    cursor: EventCursor
    root_session_id: str | None
    policy: Literal["conversation", "curated-native-command"] = "conversation"


class UsageObservation(StrictModel):
    records: tuple[NativeUsage, ...]
    unattributed: tuple[NativeUsage, ...]
    session_graph: dict[str, str | None]
    missing_session_usage: tuple[str, ...]
    coverage: Literal["partial"] = "partial"
    reason: str = "Native logs do not establish an exhaustive descendant or usage inventory; unknown usage remains unknown."


class Observation(StrictModel):
    state: Literal[
        "starting",
        "ready",
        "working",
        "accepted",
        "completed",
        "question",
        "structured_input",
        "approval",
        "unsupported",
        "cancelled",
        "timeout",
        "exited",
        "delivery_uncertain",
        "partial_input",
        "error",
    ]
    reason: str
    submission_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    accepted_id: str | None = None
    text: str = ""
    evidence: tuple[int, ...] = ()


class ProcessIdentity(StrictModel):
    pid: int
    created: float
    parent_pid: int | None = None


class CleanupReport(StrictModel):
    terminal_closed: bool
    known_writers_stopped: bool
    logs_drained: bool = False
    owned: tuple[ProcessIdentity, ...] = ()
    survivors: tuple[ProcessIdentity, ...] = ()
    errors: tuple[str, ...] = ()
    coverage: Literal["partial-native", "fixture"] = "partial-native"
    limitation: str = "Unobserved descendants that detach between process scans remain outside cleanup visibility."


class LaunchObservation(StrictModel):
    transport: str
    transport_version: str
    session: str
    runtime: str
    requested_profile_id: ObjectId
    requested_argv: tuple[str, ...]
    requested_cwd: str
    observed_session_id: str | None = None
    observed_version: str | None = None
    observed_model: str | None = None
    observed_effort: str | None = None
    observed_cwd: str | None = None
    effective_permissions: str | None = None
    daemon: ProcessIdentity | None = None
