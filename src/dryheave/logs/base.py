import os
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from dryheave.errors import InputError, LimitError
from dryheave.filesystem import read_bytes
from dryheave.models import AgentKind, Name, ObjectId, StrictModel, TokenUsage
from dryheave.serialization import digest, parse_json


class ImportLimits(StrictModel):
    max_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=64 * 1024 * 1024)
    max_events: int = Field(default=20000, gt=0, le=100000)
    max_files: int = Field(default=10000, gt=0, le=100000)
    max_depth: int = Field(default=12, gt=0, le=32)


class Diagnostic(StrictModel):
    code: str
    message: str
    line: int | None = None


class LogEvent(StrictModel):
    event_id: Name
    source_line: int
    kind: Literal["user", "assistant", "tool_call", "tool_result", "lifecycle", "metadata"]
    text: str = ""
    timestamp: str | None = None
    native_id: str | None = None
    turn_id: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class UsageRecord(StrictModel):
    identity: str
    source_line: int
    usage: TokenUsage
    raw: dict[str, JsonValue]
    accounting: Literal["response", "cumulative", "superseded"] = "response"


class Session(StrictModel):
    schema_version: Literal[1] = 1
    agent: AgentKind
    source_id: str | None
    source_path_hash: ObjectId
    source_content_hash: ObjectId
    parser_version: str = "1"
    source_version: str | None = None
    parent_session_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    events: tuple[LogEvent, ...]
    usage: tuple[UsageRecord, ...] = ()
    warnings: tuple[Diagnostic, ...] = ()
    limits: ImportLimits = Field(default_factory=ImportLimits)


class EvidenceExcerpt(StrictModel):
    session_id: ObjectId
    event_id: Name
    visibility: Literal["subject", "curator", "judge"]
    excerpt: str = Field(min_length=1, max_length=16000)


class Candidate(StrictModel):
    start_event: Name
    end_event: Name
    prompt: str
    signals: tuple[str, ...]
    issues: tuple[str, ...]


def mapping(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def text_content(value: JsonValue | None) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "\n".join(
        str(item["text"])
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and item.get("type") in {"Text", "text", "input_text", "output_text"}
    )


def read_records(
    path: Path, limits: ImportLimits
) -> tuple[bytes, list[tuple[int, dict[str, JsonValue]]], list[Diagnostic]]:
    content = read_bytes(path, limit=limits.max_bytes)
    records: list[tuple[int, dict[str, JsonValue]]] = []
    warnings: list[Diagnostic] = []
    lines = content.splitlines(keepends=True)
    if len(lines) > limits.max_events:
        raise LimitError("Log exceeds the event limit; select a smaller source.")
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            records.append((index, parse_json(line)))
        except InputError:
            warnings.append(
                Diagnostic(
                    code="malformed_line", message="Invalid JSON record was skipped.", line=index
                )
            )
        if not line.endswith(b"\n"):
            warnings.append(
                Diagnostic(
                    code="truncated_tail",
                    message="Final record has no terminating newline; source may still be writing.",
                    line=index,
                )
            )
    return content, records, warnings


def discover(root: Path, limits: ImportLimits) -> tuple[Path, ...]:
    if root.is_symlink() or not root.is_dir():
        raise InputError("Log root must be a real directory.")
    found: list[Path] = []
    count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        count += len(dirs) + len(files)
        if count > limits.max_files:
            raise LimitError("Log scan exceeds the entry limit; choose a narrower root.")
        depth = len(Path(current).relative_to(root).parts)
        dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink())
        if depth >= limits.max_depth and dirs:
            raise LimitError("Log scan exceeds the depth limit; choose a narrower root.")
        found.extend(
            Path(current) / name
            for name in sorted(files)
            if name.endswith(".jsonl") and not (Path(current) / name).is_symlink()
        )
    return tuple(sorted(found))


def candidates(session: Session) -> tuple[Candidate, ...]:
    result: list[Candidate] = []
    for index, event in enumerate(session.events):
        if event.kind != "user" or not event.text.strip():
            continue
        following = session.events[index + 1 :]
        end = next((item for item in following if item.kind == "user"), None)
        relevant = following[: following.index(end)] if end else following
        signals = ["Nonempty user message starts a possible task boundary."]
        if any(item.kind == "tool_call" for item in relevant):
            signals.append("Tool activity follows this message.")
        if any(item.kind == "assistant" and "?" in item.text for item in relevant):
            signals.append("Assistant question suggests a clarification exchange.")
        result.append(
            Candidate(
                start_event=event.event_id,
                end_event=relevant[-1].event_id if relevant else event.event_id,
                prompt=event.text[:2000],
                signals=tuple(signals),
                issues=(
                    "Heuristic suggestion; curator must confirm task intent and boundaries.",
                    "Explicit starting SHA and grading criteria are required.",
                ),
            )
        )
    return tuple(result)


def recorded_cwd(session: Session) -> str | None:
    value = session.metadata.get("cwd")
    return value.strip() if isinstance(value, str) and value.strip() else None


def recorded_baseline(session: Session) -> str | None:
    value = mapping(session.metadata.get("git")).get("commit_hash")
    return value.strip() if isinstance(value, str) and value.strip() else None


def source_hash(path: Path) -> str:
    return digest(str(path.absolute()).encode())
