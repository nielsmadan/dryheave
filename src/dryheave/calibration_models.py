from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from dryheave.models import Name, ObjectId, RelativePath, StrictModel
from dryheave.result_models import Calibration

CalibrationStatus = Literal["demonstrated", "ineffective", "unavailable", "not_applicable"]


class CalibratedCriterion(StrictModel):
    criterion_id: Name
    kind: Literal["deterministic", "judge"]
    required: bool
    verifier_id: ObjectId
    calibration: Calibration | None

    @model_validator(mode="after")
    def applicable_execution(self) -> Self:
        if (self.kind == "deterministic") != (self.calibration is not None):
            raise ValueError("only deterministic criteria have calibration executions")
        if self.calibration is not None:
            baseline, reference = self.calibration.baseline, self.calibration.reference
            status = (
                "demonstrated"
                if baseline
                and baseline.outcome == "fail"
                and reference
                and reference.outcome == "pass"
                else "ineffective"
                if baseline and baseline.outcome == "pass"
                else "unavailable"
            )
            if self.calibration.status != status:
                raise ValueError("calibration status differs from its observed executions")
        return self


def calibration_status(criteria: tuple[CalibratedCriterion, ...]) -> CalibrationStatus:
    statuses = [item.calibration.status for item in criteria if item.calibration is not None]
    if not statuses:
        return "not_applicable"
    if all(status == "demonstrated" for status in statuses):
        return "demonstrated"
    if "ineffective" in statuses:
        return "ineffective"
    return "unavailable"


class CaseCalibration(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["case-calibration"] = "case-calibration"
    case_id: ObjectId
    criteria_id: ObjectId
    created_at: AwareDatetime
    criteria: tuple[CalibratedCriterion, ...]
    status: CalibrationStatus
    files: dict[RelativePath, ObjectId]
    evidence_complete: bool
    omissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "Only deterministic criteria are calibrated; judge rubrics require separate assessment.",
        "Verifier copies do not run case.setup; required tools and declared environment references must already be available.",
        "Native repository copies do not isolate host access; detached process telemetry is partial.",
    )

    @model_validator(mode="after")
    def evidence_contract(self) -> Self:
        if len({item.criterion_id for item in self.criteria}) != len(self.criteria):
            raise ValueError("calibrated criteria must be distinct")
        if self.evidence_complete != (not self.omissions):
            raise ValueError("evidence completeness must match omissions")
        expected = calibration_status(self.criteria) if self.evidence_complete else "unavailable"
        if self.status != expected:
            raise ValueError("overall calibration status differs from criterion evidence")
        return self
