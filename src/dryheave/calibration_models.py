from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from dryheave.controller_models import RoleCall
from dryheave.judges import FactJudgment, FactVerdict
from dryheave.models import Name, ObjectId, RelativePath, StrictModel
from dryheave.result_models import Calibration, CheckExecution, verifier_verdict

CalibrationStatus = Literal["demonstrated", "ineffective", "unavailable", "not_applicable"]
FactCalibrationStatus = Literal["demonstrated", "ineffective", "unavailable"]
FactFlag = Literal["met_on_baseline", "unmet_on_reference"]


def execution_verdict(execution: CheckExecution | None) -> FactVerdict:
    if execution is None or execution.outcome not in {"pass", "fail"}:
        return "unclear"
    return verifier_verdict(execution) or "unclear"


def fact_flags(baseline: FactVerdict, reference: FactVerdict | None) -> tuple[FactFlag, ...]:
    flags: list[FactFlag] = []
    if baseline == "met":
        flags.append("met_on_baseline")
    if reference == "unmet":
        flags.append("unmet_on_reference")
    return tuple(flags)


def fact_status(baseline: FactVerdict, reference: FactVerdict | None) -> FactCalibrationStatus:
    if fact_flags(baseline, reference):
        return "ineffective"
    return "demonstrated" if baseline == "unmet" and reference == "met" else "unavailable"


def _execution_status(calibration: Calibration) -> str:
    baseline, reference = calibration.baseline, calibration.reference
    if baseline and baseline.outcome == "fail" and reference and reference.outcome == "pass":
        return "demonstrated"
    return "ineffective" if baseline and baseline.outcome == "pass" else "unavailable"


class CalibratedFact(StrictModel):
    fact_id: Name
    kind: Literal["requirement", "optional"]
    source: Literal["verifier", "judge"]
    verifier_id: ObjectId
    baseline: FactVerdict
    reference: FactVerdict | None
    flags: tuple[FactFlag, ...]
    status: FactCalibrationStatus
    calibration: Calibration | None = None
    baseline_judgment: FactJudgment | None = None
    reference_judgment: FactJudgment | None = None

    @model_validator(mode="after")
    def observed_verdicts(self) -> Self:
        judged = (self.baseline_judgment, self.reference_judgment)
        if self.source == "verifier":
            if self.calibration is None or any(judged):
                raise ValueError("verifier facts carry executions and no judgments")
            if self.calibration.status != _execution_status(self.calibration):
                raise ValueError("calibration status differs from its observed executions")
            expected = (
                execution_verdict(self.calibration.baseline),
                None
                if self.calibration.reference is None
                else execution_verdict(self.calibration.reference),
            )
        else:
            if self.calibration is not None:
                raise ValueError("judge facts carry judgments and no executions")
            expected = (
                self.baseline_judgment.verdict if self.baseline_judgment else "unclear",
                None
                if self.reference is None
                else self.reference_judgment.verdict
                if self.reference_judgment
                else "unclear",
            )
            if self.reference is None and self.reference_judgment is not None:
                raise ValueError("a reference judgment needs a reference side")
        if (self.baseline, self.reference) != expected:
            raise ValueError("fact verdicts differ from their observed evidence")
        if self.flags != fact_flags(self.baseline, self.reference):
            raise ValueError("fact flags differ from its verdicts")
        if self.status != fact_status(self.baseline, self.reference):
            raise ValueError("fact calibration status differs from its verdicts")
        return self


def calibration_status(facts: tuple[CalibratedFact, ...]) -> CalibrationStatus:
    statuses = [item.status for item in facts]
    if not statuses:
        return "not_applicable"
    if all(status == "demonstrated" for status in statuses):
        return "demonstrated"
    if "ineffective" in statuses:
        return "ineffective"
    return "unavailable"


class CaseCalibration(StrictModel):
    schema_version: Literal[2] = 2
    kind: Literal["case-calibration"] = "case-calibration"
    case_id: ObjectId
    facts_id: ObjectId
    created_at: AwareDatetime
    facts: tuple[CalibratedFact, ...]
    judge_calls: tuple[RoleCall, ...] = ()
    status: CalibrationStatus
    files: dict[RelativePath, ObjectId]
    evidence_complete: bool
    omissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "Judge facts are judged from the files alone, once per side; a model verdict is not an executed check.",
        "Verifier copies do not run case.setup; required tools and declared environment references must already be available.",
        "Native repository copies do not isolate host access; detached process telemetry is partial.",
    )

    @model_validator(mode="after")
    def evidence_contract(self) -> Self:
        if len({item.fact_id for item in self.facts}) != len(self.facts):
            raise ValueError("calibrated facts must be distinct")
        if self.evidence_complete != (not self.omissions):
            raise ValueError("evidence completeness must match omissions")
        expected = calibration_status(self.facts) if self.evidence_complete else "unavailable"
        if self.status != expected:
            raise ValueError("overall calibration status differs from fact evidence")
        return self

    @property
    def flagged(self) -> tuple[CalibratedFact, ...]:
        return tuple(item for item in self.facts if item.flags)
