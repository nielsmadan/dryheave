from dryheave.calibrations import load_calibration
from dryheave.cases import load_frozen_case
from dryheave.controller_models import ControllerRecipe
from dryheave.errors import IntegrityError
from dryheave.experiment_models import ScoringConfig
from dryheave.experiments import load_experiment, load_role
from dryheave.integrity import load_assessment
from dryheave.logs.base import Session
from dryheave.models import ObjectKind
from dryheave.personas import load_frozen_persona
from dryheave.profiles import load_profile
from dryheave.report_models import RunReport
from dryheave.repositories import load_repository
from dryheave.result_models import AssessmentEvidence
from dryheave.runner import load_capture
from dryheave.runner_models import QuarantinedCapture
from dryheave.storage import ObjectStore


def validate_bundle_object(store: ObjectStore, identifier: str, included: tuple[str, ...]) -> None:
    manifest = store.get(identifier)
    required = {
        ObjectKind.CAPTURE: "captures",
        ObjectKind.QUARANTINE: "captures",
        ObjectKind.SESSION: "source-sessions",
        ObjectKind.ASSESSMENT_EVIDENCE: "assessment-evidence",
        ObjectKind.CALIBRATION: "calibration-evidence",
    }.get(manifest.kind)
    if required and required not in included:
        raise IntegrityError("Bundle contains an undeclared sensitive artifact class.")
    loaders = {
        ObjectKind.CASE: load_frozen_case,
        ObjectKind.REPOSITORY: load_repository,
        ObjectKind.PERSONA: load_frozen_persona,
        ObjectKind.PROFILE: load_profile,
        ObjectKind.EXPERIMENT: load_experiment,
        ObjectKind.CAPTURE: load_capture,
        ObjectKind.RESULT: load_assessment,
        ObjectKind.CALIBRATION: load_calibration,
    }
    if loader := loaders.get(manifest.kind):
        loader(store, identifier)
    elif manifest.kind == ObjectKind.SIMULATOR:
        load_role(store, identifier, ControllerRecipe, ObjectKind.SIMULATOR)
    elif manifest.kind == ObjectKind.SCORING:
        load_role(store, identifier, ScoringConfig, ObjectKind.SCORING)
    elif manifest.kind == ObjectKind.SESSION:
        store.load(identifier, Session, kind=ObjectKind.SESSION)
    elif manifest.kind == ObjectKind.QUARANTINE:
        store.load(identifier, QuarantinedCapture, kind=ObjectKind.QUARANTINE)
        if manifest.references:
            raise IntegrityError("Quarantined evidence cannot claim validated input dependencies.")
    elif manifest.kind == ObjectKind.ASSESSMENT_EVIDENCE:
        evidence = store.load(identifier, AssessmentEvidence, kind=ObjectKind.ASSESSMENT_EVIDENCE)
        if manifest.references or manifest.files != evidence.files:
            raise IntegrityError("Assessment evidence map differs from its manifest.")
    elif manifest.kind == ObjectKind.PORTABLE_REPORT:
        report = store.load(identifier, RunReport, kind=ObjectKind.PORTABLE_REPORT)
        if manifest.references or manifest.files or not report.portable:
            raise IntegrityError("Portable summaries cannot carry raw dependencies.")
