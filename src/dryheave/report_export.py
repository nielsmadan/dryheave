from pathlib import Path

from dryheave.errors import ConflictError, DryheaveError
from dryheave.filesystem import atomic_write
from dryheave.report_html import render_report_html
from dryheave.report_models import RunReport, RunSetup
from dryheave.storage import ObjectStore
from dryheave.viewer import AttemptIdentity, ViewerService
from dryheave.viewer_models import CaseView, JudgeReasoningView


def _safe_case(service: ViewerService, case_id: str) -> CaseView | None:
    try:
        return service.case(case_id)
    except DryheaveError:
        return None


def _report_cases(service: ViewerService, reports: list[RunReport]) -> dict[str, CaseView | None]:
    case_ids = sorted(
        {
            case_id
            for report in reports
            for attempt in report.attempts
            if attempt.result
            for case_id in (attempt.result.case_id, attempt.result.graded_case_id)
            if case_id is not None
        }
    )
    return {case_id: _safe_case(service, case_id) for case_id in case_ids}


def _reasoning(
    service: ViewerService, reports: list[RunReport]
) -> dict[str, dict[str, JudgeReasoningView]]:
    return {
        report.run_id: {
            attempt.attempt_id: service.reasoning_for_identity(
                report.run_id,
                attempt.attempt_id,
                AttemptIdentity(attempt.capture_id, attempt.quarantine_id, attempt.assessment_id),
            )
            for attempt in report.attempts
        }
        for report in reports
    }


def write_report_html(
    store: ObjectStore,
    entries: list[tuple[RunReport, RunSetup | None]],
    destination: Path,
    *,
    include_reasoning: bool,
    replace: bool,
) -> Path:
    destination = destination.absolute()
    if not replace and (destination.exists() or destination.is_symlink()):
        raise ConflictError(
            f"Report destination already exists: {destination}. Use --force to overwrite."
        )
    service = ViewerService(store)
    reports = [report for report, _setup in entries]
    reasoning = _reasoning(service, reports) if include_reasoning else {}
    content = render_report_html(entries, _report_cases(service, reports), reasoning=reasoning)
    try:
        atomic_write(destination, content.encode("utf-8"), replace=replace)
    except FileExistsError as error:
        raise ConflictError(
            f"Report destination already exists: {destination}. Use --force to overwrite."
        ) from error
    return destination
