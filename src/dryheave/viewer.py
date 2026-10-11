import base64
import re
from dataclasses import dataclass, field
from typing import Literal

from dryheave.calibration_models import CaseCalibration
from dryheave.calibrations import load_calibration
from dryheave.cases import DEFAULT_RUBRIC_IDS, DeterministicCriterion, FrozenCase
from dryheave.errors import DryheaveError, InputError, IntegrityError, LimitError, NotFoundError
from dryheave.experiment_models import FrozenExperiment
from dryheave.filesystem import directory_names
from dryheave.journals import RunStore
from dryheave.judges import JudgeResponse
from dryheave.models import (
    JournalEvent,
    ObjectKind,
    RunCheckpoint,
    TrialStage,
    validate_relative_path,
)
from dryheave.report_models import Comparison, RunReport
from dryheave.reports import compare_runs, report_run
from dryheave.repositories import load_repository
from dryheave.result_models import Assessment, AssessmentEvidence
from dryheave.rubrics import effective_rubrics
from dryheave.runner_models import (
    AttemptCheckpoint,
    AttemptState,
    CapturedAttempt,
    QuarantinedCapture,
    RunOptions,
)
from dryheave.runner_state import ExecutionJournal
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore
from dryheave.viewer_models import (
    AttemptProgress,
    CalibrationEntry,
    CalibrationScan,
    CalibrationView,
    CaptureOmissionView,
    CaseCalibrationView,
    CaseCheckView,
    CaseDeterministicView,
    CaseFactView,
    CaseMetricsView,
    CaseRubricView,
    CaseView,
    CheckReasoning,
    ContentEncoding,
    CriterionReasoning,
    DetailStatus,
    DialogueLine,
    DialogueView,
    EvidenceFileView,
    EvidenceListing,
    JudgeReasoningView,
    LabelIndex,
    PatchView,
    RunListing,
    RunProgress,
    RunRow,
    SessionView,
    ViewerTarget,
)

RUN_ID_PATTERN = r"[0-9a-f]{32}"
OBJECT_ID_PATTERN = r"[0-9a-f]{64}"
JUDGE_RESPONSE_FILE = re.compile(r"/judge/([^/]+)/\d{6}-response\.json$")
MAX_LIST_LIMIT = 100
MAX_DETAIL_BYTES = 4 * 1024 * 1024
MAX_ROW_JOURNAL_BYTES = 256 * 1024
MAX_REQUEST_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_SCAN_OBJECTS = 4096
MAX_VERIFIED_CALIBRATIONS = 32

REASONS = {
    "not_found": "The referenced object is not retained in this store.",
    "integrity_error": "The retained object failed verification.",
    "invalid_input": "The retained object was rejected as invalid.",
    "limit_exceeded": "The retained object exceeds a read limit.",
    "conflict": "The retained object could not be read while it is locked.",
}
UNREADABLE = "The retained object could not be read."


def _failure(error: DryheaveError) -> tuple[str, str]:
    return error.code, REASONS.get(error.code, UNREADABLE)


@dataclass(frozen=True)
class AttemptIdentity:
    capture_id: str | None
    quarantine_id: str | None
    assessment_id: str | None


@dataclass(frozen=True)
class CaptureView:
    capture: CapturedAttempt | None
    capture_id: str | None
    quarantine_id: str | None
    quarantined: bool
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class AssessmentRecord:
    assessment: Assessment | None
    assessment_id: str | None
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class EvidenceView:
    assessment: Assessment | None
    evidence: AssessmentEvidence | None
    evidence_id: str | None
    assessment_id: str | None
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class DetailContent:
    status: DetailStatus
    reason: str | None = None
    reason_code: str | None = None
    limit_bytes: int | None = None
    size: int | None = None
    sha256: str | None = None
    encoding: ContentEncoding | None = None
    content: str | None = None
    omissions: tuple[CaptureOmissionView, ...] = ()


@dataclass(frozen=True)
class CalibrationCandidates:
    entries: list[tuple[str, str]] = field(default_factory=list)
    scanned: int = 0
    unreadable: int = 0
    truncated: bool = False
    cursor: str | None = None


def _stages(
    events: tuple[JournalEvent, ...], checkpoint: RunCheckpoint | None
) -> tuple[str | None, dict[str, TrialStage]]:
    mode: str | None = None
    stages = (
        dict(parse_model(canonical_json(checkpoint.state), AttemptCheckpoint).attempts)
        if checkpoint is not None
        else {}
    )
    after = checkpoint.sequence if checkpoint is not None else 0
    for event in events:
        if event.event == "run-options":
            mode = parse_model(canonical_json(event.data), RunOptions).mode
        elif (
            event.event == "attempt-state"
            and event.attempt_id is not None
            and event.sequence > after
        ):
            stages[event.attempt_id] = parse_model(canonical_json(event.data), AttemptState).stage
    return mode, stages


def _rubric_views(case: FrozenCase) -> tuple[CaseRubricView, ...]:
    declared = {item.criterion_id for item in case.criteria}
    views = []
    for item in effective_rubrics(case):
        origin: Literal["case", "default", "overridden"]
        if item.criterion_id not in DEFAULT_RUBRIC_IDS:
            origin = "case"
        elif item.criterion_id in declared:
            origin = "overridden"
        else:
            origin = "default"
        views.append(
            CaseRubricView(
                criterion_id=item.criterion_id,
                required=item.required,
                rubric=item.rubric,
                origin=origin,
            )
        )
    return tuple(views)


def resolve_target(store: ObjectStore, reference: str) -> ViewerTarget:
    if re.fullmatch(RUN_ID_PATTERN, reference):
        try:
            RunStore(store.root).inspect(reference, limit=MAX_REQUEST_JOURNAL_BYTES)
        except NotFoundError as missing:
            try:
                identifier = store.resolve(reference)
            except DryheaveError:
                raise missing from None
            report_run(store, identifier, journal_limit=MAX_REQUEST_JOURNAL_BYTES)
            return ViewerTarget(kind="report", id=identifier)
        return ViewerTarget(kind="run", id=reference)
    identifier = store.resolve(reference)
    report_run(store, identifier, journal_limit=MAX_REQUEST_JOURNAL_BYTES)
    return ViewerTarget(kind="report", id=identifier)


class ViewerService:
    def __init__(
        self,
        store: ObjectStore,
        *,
        target: ViewerTarget | None = None,
        max_detail_bytes: int = MAX_DETAIL_BYTES,
        max_scan_objects: int = MAX_SCAN_OBJECTS,
        max_verified_calibrations: int = MAX_VERIFIED_CALIBRATIONS,
        max_request_journal_bytes: int = MAX_REQUEST_JOURNAL_BYTES,
    ) -> None:
        self.store = store
        self.target = target
        self.max_detail_bytes = max_detail_bytes
        self.max_scan_objects = max_scan_objects
        self.max_verified_calibrations = max_verified_calibrations
        self.max_request_journal_bytes = max_request_journal_bytes

    def session(self) -> SessionView:
        return SessionView(target=self.target)

    def labels(self) -> LabelIndex:
        names: dict[str, list[str]] = {}
        for name, identifier in self.store.aliases().items():
            names.setdefault(identifier, []).append(name)
        return LabelIndex(
            labels={identifier: tuple(sorted(names[identifier])) for identifier in sorted(names)}
        )

    def runs(self, *, offset: int = 0, limit: int = 25) -> RunListing:
        if offset < 0 or not 1 <= limit <= MAX_LIST_LIMIT:
            raise InputError(f"Listing requires offset >= 0 and 1 <= limit <= {MAX_LIST_LIMIT}.")
        runs = RunStore(self.store.root)
        identifiers = runs.list_runs()
        budget = self.max_request_journal_bytes
        rows = []
        for run_id in identifiers[offset : offset + limit]:
            row, used = self._row(runs, run_id, max(0, min(MAX_ROW_JOURNAL_BYTES, budget)))
            budget -= used
            rows.append(row)
        return RunListing(runs=tuple(rows), offset=offset, limit=limit, total=len(identifiers))

    def _row(self, runs: RunStore, run_id: str, allowance: int) -> tuple[RunRow, int]:
        try:
            prefix = runs.summarize(run_id, limit=allowance)
            mode, stages = _stages(prefix.events, prefix.checkpoint)
        except DryheaveError as error:
            return RunRow(run_id=run_id, error=_failure(error)[0]), 0
        except OSError:
            return RunRow(run_id=run_id, error="unreadable"), 0
        return RunRow(
            run_id=run_id,
            experiment_id=prefix.metadata.experiment_id,
            created_at=prefix.metadata.created_at,
            durable_sequence=None if prefix.truncated else len(prefix.events),
            observed_events=len(prefix.events),
            truncated=prefix.truncated,
            counted_through=None
            if prefix.checkpoint is None
            else max(prefix.checkpoint.sequence, len(prefix.events)),
            mode=mode,
            attempts=len(stages),
            finished=sum(stage == TrialStage.FINISHED for stage in stages.values()),
        ), prefix.bytes_read

    def progress(self, run_id: str) -> RunProgress:
        if not re.fullmatch(RUN_ID_PATTERN, run_id):
            raise InputError("Expected a 32-character lowercase run ID.")
        journal = RunStore(self.store.root).inspect(run_id, limit=self.max_request_journal_bytes)
        execution = ExecutionJournal(journal)
        return RunProgress(
            run_id=run_id,
            experiment_id=journal.metadata.experiment_id,
            created_at=journal.metadata.created_at,
            durable_sequence=journal.sequence,
            mode=execution.options.mode if execution.options else None,
            attempts=tuple(
                AttemptProgress(
                    attempt_id=state.attempt_id,
                    trial_id=state.trial_id,
                    stage=state.stage.value,
                    capture_id=state.capture_id,
                    quarantine_id=state.quarantine_id,
                    assessment_id=state.assessment_id,
                )
                for state in execution.attempts.values()
            ),
        )

    def run_report(self, run_id: str) -> RunReport:
        if not re.fullmatch(RUN_ID_PATTERN, run_id):
            raise InputError("Expected a 32-character lowercase run ID.")
        return report_run(self.store, run_id, journal_limit=self.max_request_journal_bytes)

    def portable_report(self, report_id: str) -> RunReport:
        if not re.fullmatch(OBJECT_ID_PATTERN, report_id):
            raise InputError("Expected a lowercase SHA-256 object ID.")
        return report_run(self.store, report_id, journal_limit=self.max_request_journal_bytes)

    def compare(
        self,
        before: str,
        after: str,
        *,
        before_variant: str | None = None,
        after_variant: str | None = None,
    ) -> Comparison:
        return compare_runs(
            self.store,
            before,
            after,
            before_variant=before_variant,
            after_variant=after_variant,
            journal_limit=self.max_request_journal_bytes,
        )

    def _attempt(self, kind: str, source_id: str, attempt_id: str) -> tuple[str, AttemptIdentity]:
        if kind == "run":
            if not re.fullmatch(RUN_ID_PATTERN, source_id):
                raise InputError("Expected a 32-character lowercase run ID.")
            journal = RunStore(self.store.root).inspect(
                source_id, limit=self.max_request_journal_bytes
            )
            execution = ExecutionJournal(journal)
            state = execution.attempts.get(attempt_id)
            if state is None:
                raise NotFoundError(f"Run has no retained attempt: {attempt_id}")
            return journal.metadata.run_id, AttemptIdentity(
                state.capture_id, state.quarantine_id, state.assessment_id
            )
        if not re.fullmatch(OBJECT_ID_PATTERN, source_id):
            raise InputError("Expected a lowercase SHA-256 object ID.")
        report = report_run(self.store, source_id, journal_limit=self.max_request_journal_bytes)
        attempt = next((item for item in report.attempts if item.attempt_id == attempt_id), None)
        if attempt is None:
            raise NotFoundError(f"Report has no retained attempt: {attempt_id}")
        return report.run_id, AttemptIdentity(
            attempt.capture_id, attempt.quarantine_id, attempt.assessment_id
        )

    def _retained_capture(
        self, run_id: str, attempt_id: str, identity: AttemptIdentity
    ) -> CaptureView:
        identifier = identity.capture_id or identity.quarantine_id
        quarantined = identity.capture_id is None and identity.quarantine_id is not None
        if identifier is None:
            return CaptureView(
                capture=None,
                capture_id=None,
                quarantine_id=None,
                quarantined=False,
                status="unavailable",
                reason="No capture was retained for this attempt.",
                reason_code="not_retained",
            )
        try:
            capture, quarantined = self._capture_payload(identifier)
            if (capture.run_id, capture.attempt_id) != (run_id, attempt_id):
                raise IntegrityError("Captured payload identity differs from the retained attempt.")
        except DryheaveError as error:
            code, reason = _failure(error)
            return CaptureView(
                capture=None,
                capture_id=identity.capture_id,
                quarantine_id=identity.quarantine_id,
                quarantined=quarantined,
                status="unavailable" if isinstance(error, NotFoundError) else "invalid",
                reason=reason,
                reason_code=code,
            )
        return CaptureView(
            capture=capture,
            capture_id=identity.capture_id,
            quarantine_id=identity.quarantine_id,
            quarantined=quarantined,
            status="ok",
        )

    def _capture_payload(self, identifier: str) -> tuple[CapturedAttempt, bool]:
        envelope = self.store.read_envelope(identifier)
        if envelope.kind == ObjectKind.CAPTURE:
            return parse_model(canonical_json(envelope.payload), CapturedAttempt), False
        if envelope.kind == ObjectKind.QUARANTINE:
            record = parse_model(canonical_json(envelope.payload), QuarantinedCapture)
            return record.capture, True
        raise IntegrityError("Retained attempt evidence has an unexpected object kind.")

    def _read_detail(self, identifier: str, name: str) -> DetailContent:
        try:
            content = self.store.read_blob_bounded(identifier, name, limit=self.max_detail_bytes)
        except LimitError:
            return DetailContent(
                status="refused",
                reason="The retained bytes exceed the viewer's per-response read limit.",
                reason_code="serve_limit",
                limit_bytes=self.max_detail_bytes,
            )
        except DryheaveError as error:
            code, reason = _failure(error)
            return DetailContent(
                status="unavailable" if isinstance(error, NotFoundError) else "invalid",
                reason=reason,
                reason_code=code,
            )
        encoding: ContentEncoding = "utf-8"
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "base64"
            text = base64.b64encode(content).decode("ascii")
        return DetailContent(
            status="ok",
            size=len(content),
            sha256=digest(content),
            encoding=encoding,
            content=text,
        )

    def dialogue(self, kind: str, source_id: str, attempt_id: str) -> DialogueView:
        run_id, identity = self._attempt(kind, source_id, attempt_id)
        view = self._retained_capture(run_id, attempt_id, identity)
        capture = view.capture
        if capture is None:
            return DialogueView(
                attempt_id=attempt_id,
                capture_id=view.capture_id,
                quarantine_id=view.quarantine_id,
                quarantined=view.quarantined,
                status=view.status,
                reason=view.reason,
                reason_code=view.reason_code,
            )
        return DialogueView(
            attempt_id=attempt_id,
            capture_id=view.capture_id,
            quarantine_id=view.quarantine_id,
            quarantined=view.quarantined,
            status="ok",
            interaction_coverage=capture.interaction_coverage,
            capture_errors=capture.errors,
            evidence_omissions=capture.evidence_omissions,
            messages=tuple(
                DialogueLine(role=message.role, text=message.text) for message in capture.dialogue
            ),
        )

    def patch(self, kind: str, source_id: str, attempt_id: str) -> PatchView:
        run_id, identity = self._attempt(kind, source_id, attempt_id)
        view = self._retained_capture(run_id, attempt_id, identity)
        capture = view.capture
        name = capture.workspace.patch_file if capture is not None else None
        detail = self._patch_detail(view, name)
        return PatchView(
            attempt_id=attempt_id,
            capture_id=view.capture_id,
            quarantine_id=view.quarantine_id,
            quarantined=view.quarantined,
            status=detail.status,
            reason=detail.reason,
            reason_code=detail.reason_code,
            name=name,
            workspace_complete=capture.workspace.complete if capture is not None else None,
            omissions=detail.omissions,
            limit_bytes=detail.limit_bytes,
            bytes=detail.size,
            sha256=detail.sha256,
            encoding=detail.encoding,
            content=detail.content,
        )

    def _patch_detail(self, view: CaptureView, name: str | None) -> DetailContent:
        capture = view.capture
        if capture is None:
            return DetailContent(
                status=view.status, reason=view.reason, reason_code=view.reason_code
            )
        if name is None:
            omissions = tuple(
                CaptureOmissionView(
                    path=item.path, reason=item.reason, intentional=item.intentional
                )
                for item in capture.workspace.omissions
                if item.path == "."
            )
            if omissions:
                return DetailContent(
                    status="omitted",
                    reason="The capture recorded why no final patch was retained.",
                    reason_code="capture_omission",
                    omissions=omissions,
                )
            return DetailContent(
                status="unavailable",
                reason="No final patch was retained.",
                reason_code="not_retained",
            )
        identifier = view.capture_id or view.quarantine_id
        if identifier is None:
            return DetailContent(
                status="unavailable",
                reason="No capture was retained for this attempt.",
                reason_code="not_retained",
            )
        return self._read_detail(identifier, name)

    def _assessment_record(
        self, run_id: str, attempt_id: str, identity: AttemptIdentity
    ) -> AssessmentRecord:
        if identity.assessment_id is None:
            return AssessmentRecord(
                assessment=None,
                assessment_id=None,
                status="unavailable",
                reason="No finished assessment.",
                reason_code="not_retained",
            )
        try:
            assessment = self.store.read_payload(
                identity.assessment_id, Assessment, kind=ObjectKind.RESULT
            )
            if (assessment.run_id, assessment.attempt_id) != (run_id, attempt_id):
                raise IntegrityError("Assessment identity differs from the retained attempt.")
        except DryheaveError as error:
            code, reason = _failure(error)
            return AssessmentRecord(
                assessment=None,
                assessment_id=identity.assessment_id,
                status="unavailable" if isinstance(error, NotFoundError) else "invalid",
                reason=reason,
                reason_code=code,
            )
        return AssessmentRecord(
            assessment=assessment, assessment_id=identity.assessment_id, status="ok"
        )

    def _evidence(self, run_id: str, attempt_id: str, identity: AttemptIdentity) -> EvidenceView:
        record = self._assessment_record(run_id, attempt_id, identity)
        if record.assessment is None:
            return EvidenceView(
                assessment=None,
                evidence=None,
                evidence_id=None,
                assessment_id=record.assessment_id,
                status=record.status,
                reason=record.reason,
                reason_code=record.reason_code,
            )
        assessment = record.assessment
        if assessment.evidence_id is None:
            return EvidenceView(
                assessment=assessment,
                evidence=None,
                evidence_id=None,
                assessment_id=record.assessment_id,
                status="unavailable",
                reason="The assessment retained no verifier evidence.",
                reason_code="not_retained",
            )
        try:
            evidence = self.store.read_payload(
                assessment.evidence_id, AssessmentEvidence, kind=ObjectKind.ASSESSMENT_EVIDENCE
            )
            if (evidence.run_id, evidence.attempt_id) != (run_id, attempt_id):
                raise IntegrityError("Evidence identity differs from the retained attempt.")
        except DryheaveError as error:
            code, reason = _failure(error)
            return EvidenceView(
                assessment=assessment,
                evidence=None,
                evidence_id=assessment.evidence_id,
                assessment_id=record.assessment_id,
                status="unavailable" if isinstance(error, NotFoundError) else "invalid",
                reason=reason,
                reason_code=code,
            )
        return EvidenceView(
            assessment=assessment,
            evidence=evidence,
            evidence_id=assessment.evidence_id,
            assessment_id=record.assessment_id,
            status="ok",
        )

    def _latest_judge_response(
        self, evidence_id: str, evidence: AssessmentEvidence, judge_calls: list[str]
    ) -> JudgeResponse | None:
        by_call: dict[str, str] = {}
        for name in evidence.files:
            match = JUDGE_RESPONSE_FILE.search(name)
            if match:
                by_call[match.group(1)] = name
        for call_id in reversed(judge_calls):
            response_name = by_call.get(call_id)
            if response_name is None:
                continue
            try:
                content = self.store.read_blob_bounded(
                    evidence_id, response_name, limit=self.max_detail_bytes
                )
                return parse_model(content, JudgeResponse)
            except DryheaveError:
                continue
        return None

    def reasoning(self, kind: str, source_id: str, attempt_id: str) -> JudgeReasoningView:
        run_id, identity = self._attempt(kind, source_id, attempt_id)
        return self.reasoning_for_identity(run_id, attempt_id, identity)

    def reasoning_for_identity(
        self, run_id: str, attempt_id: str, identity: AttemptIdentity
    ) -> JudgeReasoningView:
        view = self._evidence(run_id, attempt_id, identity)
        if view.assessment is None or view.evidence is None or view.evidence_id is None:
            return JudgeReasoningView(
                attempt_id=attempt_id,
                assessment_id=view.assessment_id,
                status=view.status,
                reason=view.reason,
                reason_code=view.reason_code,
            )
        assessment = view.assessment
        judge_calls = [
            charge.identity
            for role in assessment.roles
            if role.role == "judge"
            for charge in role.records
        ]
        response = self._latest_judge_response(view.evidence_id, view.evidence, judge_calls)
        if response is None:
            return JudgeReasoningView(
                attempt_id=attempt_id,
                assessment_id=view.assessment_id,
                status="unavailable",
                reason="No judge reasoning was retained for this assessment.",
                reason_code="not_retained",
            )
        criterion_rationale = {item.criterion_id: item.rationale for item in response.judgments}
        check_rationale = {item.check_id: item.rationale for item in response.checks}
        return JudgeReasoningView(
            attempt_id=attempt_id,
            assessment_id=view.assessment_id,
            status="ok",
            criteria=tuple(
                CriterionReasoning(
                    criterion_id=item.criterion_id,
                    rationale=criterion_rationale.get(item.criterion_id),
                )
                for item in assessment.criteria
            ),
            checks=tuple(
                CheckReasoning(check_id=item.check_id, rationale=check_rationale.get(item.check_id))
                for item in assessment.checks
            ),
        )

    def _frozen_case_for_view(self, store: ObjectStore, case_id: str) -> FrozenCase:
        case = store.load(case_id, FrozenCase, kind=ObjectKind.CASE)
        manifest = store.get(case_id, kind=ObjectKind.CASE)
        if manifest.references != tuple(sorted({case.repository_id, case.persona_id})):
            raise InputError("Frozen case reference closure does not match its runtime inputs.")
        expected = set(case.hidden_files.values())
        if case.reference_patch_file:
            expected.add(case.reference_patch_file)
        if set(manifest.files) != expected:
            raise InputError("Frozen case blob map does not match its declared hidden inputs.")
        return case

    def case(self, case_id: str) -> CaseView:
        if not re.fullmatch(OBJECT_ID_PATTERN, case_id):
            raise InputError("Expected a lowercase SHA-256 case ID.")
        bounded = ObjectStore(self.store.root, read_budget=self.max_detail_bytes)
        case = self._frozen_case_for_view(bounded, case_id)
        try:
            baseline_commit = load_repository(bounded, case.repository_id).commit
        except DryheaveError:
            baseline_commit = None
        return CaseView(
            case_id=case_id,
            title=case.title,
            initial_prompt=case.initial_prompt,
            facts=tuple(
                CaseFactView(fact_id=item.fact_id, text=item.text, disclosure=item.disclosure)
                for item in case.allowed_facts
            ),
            rubrics=_rubric_views(case),
            disabled_rubrics=case.disabled_rubrics,
            deterministic=tuple(
                CaseDeterministicView(
                    criterion_id=item.criterion_id,
                    description=item.description,
                    required=item.required,
                    command=item.command,
                )
                for item in case.criteria
                if isinstance(item, DeterministicCriterion)
            ),
            checks=tuple(
                CaseCheckView(
                    check_id=item.check_id,
                    question=item.question,
                    expect=item.expect,
                    only_if=item.only_if,
                )
                for item in case.checks
            ),
            metrics=tuple(
                CaseMetricsView(
                    metrics_id=item.metrics_id, description=item.description, command=item.command
                )
                for item in case.metrics
            ),
            budgets=case.budgets,
            tool_requirements=case.tool_requirements,
            repository_id=case.repository_id,
            persona_id=case.persona_id,
            baseline_commit=baseline_commit,
            reference_patch=case.reference_patch_file is not None,
        )

    def evidence(self, kind: str, source_id: str, attempt_id: str) -> EvidenceListing:
        run_id, identity = self._attempt(kind, source_id, attempt_id)
        view = self._evidence(run_id, attempt_id, identity)
        if view.evidence is None:
            return EvidenceListing(
                attempt_id=attempt_id,
                assessment_id=view.assessment_id,
                evidence_id=view.evidence_id,
                status=view.status,
                reason=view.reason,
                reason_code=view.reason_code,
            )
        return EvidenceListing(
            attempt_id=attempt_id,
            assessment_id=view.assessment_id,
            evidence_id=view.evidence_id,
            status="ok",
            files=tuple(sorted(view.evidence.files)),
            omissions=view.evidence.omissions,
            complete=not view.evidence.omissions,
        )

    def evidence_file(
        self, kind: str, source_id: str, attempt_id: str, name: str
    ) -> EvidenceFileView:
        try:
            validate_relative_path(name)
        except ValueError as error:
            raise InputError("Evidence file name must be a normalized relative path.") from error
        run_id, identity = self._attempt(kind, source_id, attempt_id)
        view = self._evidence(run_id, attempt_id, identity)
        detail = self._evidence_detail(view, name)
        return EvidenceFileView(
            attempt_id=attempt_id,
            assessment_id=view.assessment_id,
            evidence_id=view.evidence_id,
            name=name,
            status=detail.status,
            reason=detail.reason,
            reason_code=detail.reason_code,
            limit_bytes=detail.limit_bytes,
            bytes=detail.size,
            sha256=detail.sha256,
            encoding=detail.encoding,
            content=detail.content,
        )

    def _evidence_detail(self, view: EvidenceView, name: str) -> DetailContent:
        if view.evidence is None or view.evidence_id is None:
            return DetailContent(
                status=view.status, reason=view.reason, reason_code=view.reason_code
            )
        if name not in view.evidence.files:
            raise NotFoundError(f"Evidence has no file named {name!r}.")
        return self._read_detail(view.evidence_id, name)

    def calibrations(
        self, run_id: str, *, after: str | None = None, limit: int | None = None
    ) -> CalibrationView:
        if not re.fullmatch(RUN_ID_PATTERN, run_id):
            raise InputError("Expected a 32-character lowercase run ID.")
        metadata = RunStore(self.store.root).read_metadata(run_id)
        case_ids: tuple[str, ...] = ()
        input_error = None
        try:
            experiment = self.store.read_payload(
                metadata.experiment_id, FrozenExperiment, kind=ObjectKind.EXPERIMENT
            )
            case_ids = experiment.case_ids
        except DryheaveError as error:
            input_error = _failure(error)[1]
        candidates = (
            self._scan_calibrations(set(case_ids), after=after, limit=self._scan_limit(limit))
            if case_ids
            else CalibrationCandidates()
        )
        matches, counts = self._entries(candidates, case_ids)
        return CalibrationView(
            run_id=run_id,
            experiment_id=metadata.experiment_id,
            input_error=input_error,
            calibrations={case_id: tuple(entries) for case_id, entries in matches.items()},
            scan=self._scan_state(candidates, counts, complete=input_error is None),
        )

    def case_calibrations(
        self, case_id: str, *, after: str | None = None, limit: int | None = None
    ) -> CaseCalibrationView:
        if not re.fullmatch(OBJECT_ID_PATTERN, case_id):
            raise InputError("Expected a lowercase SHA-256 case ID.")
        candidates = self._scan_calibrations({case_id}, after=after, limit=self._scan_limit(limit))
        matches, counts = self._entries(candidates, (case_id,))
        return CaseCalibrationView(
            case_id=case_id,
            calibrations=tuple(matches[case_id]),
            scan=self._scan_state(candidates, counts, complete=True),
        )

    def _scan_limit(self, limit: int | None) -> int:
        if limit is None:
            return self.max_scan_objects
        if not 1 <= limit <= self.max_scan_objects:
            raise InputError(f"Scan limit must be between 1 and {self.max_scan_objects}.")
        return limit

    def _scan_state(
        self, candidates: CalibrationCandidates, counts: tuple[int, int], *, complete: bool
    ) -> CalibrationScan:
        attempted, verified = counts
        return CalibrationScan(
            complete=complete and not candidates.truncated,
            scanned=candidates.scanned,
            unreadable=candidates.unreadable,
            attempted=attempted,
            verified=verified,
            next_cursor=candidates.cursor,
        )

    def _entries(
        self, candidates: CalibrationCandidates, case_ids: tuple[str, ...]
    ) -> tuple[dict[str, list[CalibrationEntry]], tuple[int, int]]:
        matches: dict[str, list[CalibrationEntry]] = {case_id: [] for case_id in case_ids}
        bounded = ObjectStore(self.store.root, read_budget=self.max_detail_bytes)
        attempted = verified = 0
        for case_id, name in candidates.entries:
            verify = attempted < self.max_verified_calibrations
            attempted += int(verify)
            entry = self._calibration_entry(bounded, case_id, name, verify=verify)
            verified += int(entry.verification == "verified")
            matches[case_id].append(entry)
        return matches, (attempted, verified)

    def _scan_calibrations(
        self, wanted: set[str], *, after: str | None, limit: int
    ) -> CalibrationCandidates:
        if after is not None and not re.fullmatch(OBJECT_ID_PATTERN, after):
            raise InputError("Expected a lowercase SHA-256 scan cursor.")
        try:
            names = sorted(directory_names(self.store.root / "objects"))
        except (OSError, DryheaveError):
            names = []
        entries: list[tuple[str, str]] = []
        scanned = unreadable = 0
        truncated = False
        cursor: str | None = None
        for name in names:
            if not re.fullmatch(OBJECT_ID_PATTERN, name) or (after is not None and name <= after):
                continue
            if scanned >= limit:
                truncated = True
                break
            scanned += 1
            cursor = name
            try:
                envelope = self.store.read_envelope(name)
            except DryheaveError:
                unreadable += 1
                continue
            if envelope.kind != ObjectKind.CALIBRATION or len(envelope.references) != 1:
                continue
            if envelope.references[0] in wanted:
                entries.append((envelope.references[0], name))
        return CalibrationCandidates(
            entries, scanned, unreadable, truncated, cursor if truncated else None
        )

    def _calibration_entry(
        self, store: ObjectStore, case_id: str, name: str, *, verify: bool
    ) -> CalibrationEntry:
        try:
            record = (
                load_calibration(store, name)
                if verify
                else store.read_payload(name, CaseCalibration, kind=ObjectKind.CALIBRATION)
            )
            if record.case_id != case_id:
                raise IntegrityError("Calibration payload differs from its referenced case.")
        except LimitError:
            return CalibrationEntry(
                calibration_id=name,
                case_id=case_id,
                verification="unverified",
                reason="Retained evidence exceeds the viewer's per-response read limit.",
                reason_code="serve_limit",
            )
        except DryheaveError as error:
            code, reason = _failure(error)
            return CalibrationEntry(
                calibration_id=name,
                case_id=case_id,
                verification="failed",
                reason=reason,
                reason_code=code,
            )
        return CalibrationEntry(
            calibration_id=name,
            case_id=case_id,
            verification="verified" if verify else "unverified",
            reason=None
            if verify
            else "Verification was not attempted within the viewer's per-request budget.",
            reason_code=None if verify else "verification_budget",
            status=record.status,
            created_at=record.created_at,
            evidence_complete=record.evidence_complete,
            omissions=record.omissions,
        )
