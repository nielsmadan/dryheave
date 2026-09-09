import os
import re
from pathlib import Path

from dryheave.cases import FrozenCase
from dryheave.drivers.models import NativeEvent
from dryheave.errors import DryheaveError, InputError
from dryheave.logs.base import mapping, string
from dryheave.models import ObjectKind
from dryheave.repositories import load_repository
from dryheave.result_models import Assessment, AuditFinding, AuditReport, AuditReview
from dryheave.runner_models import CapturedAttempt
from dryheave.serialization import canonical_json, digest, parse_json, parse_model
from dryheave.storage import ObjectStore


def finding(rule: str, confidence: str, description: str, evidence: bytes) -> AuditFinding:
    return parse_model(
        canonical_json(
            {
                "finding_id": rule + "-" + digest(evidence)[:16],
                "rule_id": rule,
                "confidence": confidence,
                "description": description,
                "evidence_hash": digest(evidence),
            }
        ),
        AuditFinding,
    )


def exclusions(audit: AuditReport, reviews: tuple[AuditReview, ...] = ()) -> tuple[str, ...]:
    reasons = []
    if audit.input_integrity != "verified":
        reasons.append("immutable_inputs_" + audit.input_integrity)
    if audit.capture_integrity != "verified":
        reasons.append("capture_" + audit.capture_integrity)
    if audit.cleanup != "stopped":
        reasons.append("cleanup_unresolved")
    if not audit.capture_complete:
        reasons.append("capture_incomplete")
    decisions = {item.finding_id: item.decision for item in reviews}
    for item in audit.findings:
        if item.confidence == "confirmed" or decisions.get(item.finding_id) != "dismiss":
            reasons.append(item.rule_id)
    return tuple(dict.fromkeys(reasons))


def native_events(files: dict[str, bytes]) -> tuple[NativeEvent, ...]:
    events = []
    for name, content in sorted(files.items()):
        if name.startswith("evidence/") and name.endswith("-native-event.json"):
            events.append(parse_model(content, NativeEvent))
    return tuple(events)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _paths(event: NativeEvent) -> tuple[tuple[str, ...], bool]:
    data = event.data
    inputs = mapping(data.get("input"))
    arguments = data.get("arguments")
    if isinstance(arguments, str):
        try:
            inputs = parse_json(arguments.encode())
        except DryheaveError:
            inputs = {}
    elif isinstance(arguments, dict):
        inputs = arguments
    name = (string(data.get("name")) or string(data.get("tool_name")) or "").lower()
    explicit = tuple(
        value
        for key in ("path", "file_path", "file", "filename")
        if isinstance(value := inputs.get(key, data.get(key)), str)
    )
    read = name in {"read", "read_file", "view_file", "open_file", "view_image"}
    if explicit:
        return explicit, read
    command = inputs.get("command", inputs.get("cmd", data.get("command")))
    if isinstance(command, list):
        command = " ".join(item for item in command if isinstance(item, str))
    if not isinstance(command, str):
        return (), False
    read = bool(
        re.search(r"(?:^|[\s;|&])(cat|head|tail|less|sed|rg|grep|cp|python\S*|git)\s", command)
    )
    return tuple(re.findall(r"/[^\s\"'<>|;]+", command)), read


def _access_findings(
    events: tuple[NativeEvent, ...], roots: tuple[str, ...], workspace: str, transcript_hash: str
) -> tuple[tuple[AuditFinding, ...], int]:
    findings = []
    mentions = 0
    for event in events:
        if event.kind != "tool":
            continue
        paths, observed_read = _paths(event)
        for raw in paths:
            path = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(workspace, raw))
            if _within(path, workspace):
                continue
            if digest(path.encode()) != transcript_hash and not any(
                _within(path, root) for root in roots
            ):
                continue
            if observed_read:
                findings.append(
                    finding(
                        "forbidden-read-observed",
                        "suspected",
                        "Recorded tool invocation requests curator-only input; native telemetry does not prove returned content.",
                        canonical_json(event),
                    )
                )
            else:
                mentions += 1
    return tuple({item.finding_id: item for item in findings}.values()), mentions


def audit_capture(
    store: ObjectStore,
    capture: CapturedAttempt,
    case: FrozenCase,
    files: dict[str, bytes],
    workspace: Path,
) -> AuditReport:
    store.verify(capture.experiment_id)
    repository = load_repository(store, case.repository_id)
    findings = []
    inventory_name = capture.workspace.git_inventory_file
    if inventory_name:
        inventory = {
            row.split()[0]
            for row in files[inventory_name].decode("ascii").splitlines()
            if re.fullmatch(r"[0-9a-f]+ (?:commit|blob|tree|tag)", row)
        }
        for identifier in sorted(inventory & set(repository.known_disallowed_commits)):
            findings.append(
                finding(
                    "forbidden-git-object",
                    "confirmed",
                    "The captured Git object database contains a known disallowed future commit.",
                    identifier.encode(),
                )
            )
    events = native_events(files)
    access, mentions = _access_findings(
        events,
        (
            repository.source_path,
            repository.source_git_path,
            str(store.object_path(capture.case_id)),
            str(store.object_path(case.repository_id)),
            str(store.object_path(case.source_session_id)),
        ),
        str(workspace),
        case.source_path_hash,
    )
    findings.extend(access)
    stopped = capture.cleanup.terminal_closed and capture.cleanup.known_writers_stopped
    return AuditReport(
        input_integrity="verified",
        capture_integrity="verified",
        cleanup="stopped" if stopped else "unresolved",
        capture_complete=(
            capture.workspace.complete
            and capture.workspace.git_complete
            and capture.cleanup.logs_drained
            and not capture.cleanup.errors
            and not capture.evidence_omissions
        ),
        findings=tuple(findings),
        coverage=(
            "Immutable experiment closure and hidden verifier hashes verified after subject stop.",
            "Known Git inventory: "
            + repository.audit_coverage
            + "; unknown future history remains partial.",
            "Recorded tool access coverage is partial; missing telemetry is not proof of no access.",
            f"Tool path mentions without observed read intent: {mentions} (not access findings).",
            f"Parsed native events: {len(events)}.",
            capture.cleanup.limitation,
        ),
    )


def invalid_audit(error: str, capture: CapturedAttempt | None = None) -> AuditReport:
    return AuditReport(
        input_integrity="invalid",
        capture_integrity="unavailable" if capture is None else "invalid",
        cleanup="stopped"
        if capture is not None
        and capture.cleanup.terminal_closed
        and capture.cleanup.known_writers_stopped
        else "unresolved",
        capture_complete=False,
        findings=(
            finding(
                "immutable-integrity",
                "confirmed",
                "Immutable input or capture validation failed; grading was not authorized.",
                error.encode(),
            ),
        ),
        coverage=(
            "Only locally hash-verified evidence and the durable journal are available; referenced input closure is invalid.",
        ),
    )


def load_assessment(store: ObjectStore, reference: str) -> Assessment:

    result = store.load(reference, Assessment, kind=ObjectKind.RESULT)
    manifest = store.get(reference, kind=ObjectKind.RESULT)
    if manifest.references or manifest.files:
        raise InputError(
            "Structured assessments contain provenance identities, without raw blob dependencies."
        )
    return result
