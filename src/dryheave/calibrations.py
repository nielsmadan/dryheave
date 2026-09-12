from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from dryheave.calibration_models import CalibratedCriterion, CaseCalibration, calibration_status
from dryheave.calibration_recovery import CalibrationOwnership
from dryheave.cases import (
    CapturePolicy,
    DeterministicCriterion,
    FrozenCase,
    RubricCriterion,
    load_frozen_case,
)
from dryheave.drivers.models import CleanupReport
from dryheave.errors import InputError, IntegrityError
from dryheave.filesystem import file_lock
from dryheave.final_capture import CaptureBuilder, IgnoreSelection
from dryheave.grading import VerifierContext, check_copy, check_path
from dryheave.models import CommandSpec, ObjectKind
from dryheave.native_recovery import reconcile_store_ownership
from dryheave.result_models import Calibration, CheckExecution
from dryheave.runner import cancellation_signals
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore

CHECK_CONTENT_DEPTH = 4


def criteria_identity(case: FrozenCase) -> str:
    return digest(
        canonical_json({"criteria": [item.model_dump(mode="json") for item in case.criteria]})
    )


def verifier_identity(
    case: FrozenCase, criterion: DeterministicCriterion | RubricCriterion, blobs: dict[str, bytes]
) -> str:
    return digest(
        canonical_json(
            {
                "criterion": criterion.model_dump(mode="json"),
                "hidden_files": {
                    name: digest(blobs[path]) for name, path in case.hidden_files.items()
                },
            }
        )
    )


def _criterion(
    context: VerifierContext,
    criterion: DeterministicCriterion | RubricCriterion,
    ownership: CalibrationOwnership,
) -> CalibratedCriterion:
    calibration = None
    if isinstance(criterion, DeterministicCriterion):
        baseline = check_copy(context, criterion, "baseline")
        ownership.reconcile()
        reference = None
        if context.case.reference_patch_file:
            reference = check_copy(context, criterion, "reference")
            ownership.reconcile()
        calibration = Calibration(
            status="demonstrated"
            if baseline.outcome == "fail" and reference and reference.outcome == "pass"
            else "ineffective"
            if baseline.outcome == "pass"
            else "unavailable",
            baseline=baseline,
            reference=reference,
        )
    return CalibratedCriterion(
        criterion_id=criterion.criterion_id,
        kind=criterion.kind,
        required=criterion.required,
        verifier_id=verifier_identity(context.case, criterion, context.blobs),
        calibration=calibration,
    )


def _evidence(
    root: Path, ownership: CalibrationOwnership
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    cleanup = ownership.state.cleanup
    if cleanup is None or not cleanup.known_writers_stopped or cleanup.errors:
        raise InputError("Calibration evidence capture requires stopped owned writers.")
    builder = CaptureBuilder(
        CapturePolicy(max_bytes=128 * 1024 * 1024, max_files=30000, max_depth=32)
    )

    def select(paths: dict[str, int]) -> IgnoreSelection:
        return IgnoreSelection(
            omitted={
                path
                for path in paths
                if len(Path(path).parts) == CHECK_CONTENT_DEPTH
                and Path(path).name in {"workspace", "hidden"}
            }
        )

    if root.exists():
        for path, mode in builder.walk(root, ignored=select).items():
            if "-evidence/" in path:
                builder.take(root, path, mode, prefix="checks")
    files = builder.blobs | {"cleanup.json": canonical_json(cleanup)}
    return files, tuple(item.reason for item in builder.omissions if not item.intentional)


def calibrate_case(store: ObjectStore, reference: str) -> str:
    case_id = store.resolve(reference)
    case = load_frozen_case(store, case_id)
    blobs = store.read_blobs(case_id)
    operation_id = uuid4().hex
    root = store.root / "calibrations" / operation_id
    cancelled = Event()
    with (
        store.native_lock(),
        cancellation_signals(cancelled),
    ):
        reconcile_store_ownership(store)
        with file_lock(store.root / "locks" / ("calibration-" + operation_id + ".lock")):
            ownership = CalibrationOwnership.create(root, case_id)
            context = VerifierContext(
                store, case, blobs, root / "checks", None, ownership.own, cancelled
            )
            try:
                criteria = []
                for criterion in case.criteria:
                    if cancelled.is_set():
                        raise KeyboardInterrupt
                    criteria.append(_criterion(context, criterion, ownership))
            finally:
                ownership.reconcile()
            if cancelled.is_set():
                raise KeyboardInterrupt
            store.verify(case_id)
            files, omissions = _evidence(context.root, ownership)
            record = CaseCalibration(
                case_id=case_id,
                criteria_id=criteria_identity(case),
                created_at=datetime.now(UTC),
                criteria=tuple(criteria),
                status=calibration_status(tuple(criteria)) if not omissions else "unavailable",
                files={name: digest(content) for name, content in files.items()},
                evidence_complete=not omissions,
                omissions=omissions,
            )
            _validate_calibration(record, case, blobs, files)
            if cancelled.is_set():
                raise KeyboardInterrupt
            identifier = store.put(
                ObjectKind.CALIBRATION, record, files=files, references=(case_id,)
            )
            load_calibration(store, identifier)
            ownership.state = ownership.state.model_copy(update={"result_id": identifier})
            ownership.save()
            return identifier


def _evidence_prefix(criterion: DeterministicCriterion, side: str) -> str:
    return (
        Path("checks") / check_path(criterion, side) / (criterion.criterion_id + "-evidence")
    ).as_posix() + "/"


def _command_evidence(
    criterion: DeterministicCriterion, retained: dict[str, bytes], *, complete: bool
) -> None:
    commands = [
        parse_model(content, CommandSpec)
        for name, content in retained.items()
        if name.endswith("-command.json")
    ]
    if complete and len(commands) != 1:
        raise IntegrityError("Calibration omits its executed verifier command.")
    for command in commands:
        index = criterion.command.argv.index("{verifier}/" + criterion.entrypoint)
        if len(command.argv) != len(criterion.command.argv):
            raise IntegrityError("Calibration command differs from its frozen verifier.")
        entrypoint = command.argv[index]
        suffix = "/" + criterion.entrypoint
        if not Path(entrypoint).is_absolute() or not entrypoint.endswith(suffix):
            raise IntegrityError("Calibration command has an invalid hidden entrypoint.")
        hidden = entrypoint.removesuffix(suffix)
        expected = tuple(
            argument.replace("{verifier}", hidden) for argument in criterion.command.argv
        )
        if index > 0:
            expected = (command.argv[0], *expected[1:])
        if command != criterion.command.model_copy(update={"argv": expected}):
            raise IntegrityError("Calibration command differs from its frozen verifier.")


def _execution_evidence(
    criterion: DeterministicCriterion,
    side: str,
    execution: CheckExecution,
    files: dict[str, bytes],
    *,
    complete: bool,
) -> None:
    prefix = _evidence_prefix(criterion, side)
    retained = {
        name.removeprefix(prefix): content
        for name, content in files.items()
        if name.startswith(prefix)
    }
    result = retained.get("result.json")
    if result is None:
        if complete:
            raise IntegrityError("Calibration omits a retained execution result.")
        return
    if parse_model(result, CheckExecution) != execution:
        raise IntegrityError("Calibration execution differs from its retained evidence.")
    for stream, sha in (("stdout", execution.stdout_sha256), ("stderr", execution.stderr_sha256)):
        content = retained.get(stream + ".bin")
        if (content is None and complete and sha is not None) or (
            content is not None and digest(content) != sha
        ):
            raise IntegrityError("Calibration stream digest differs from execution evidence.")
    if execution.outcome not in {"pass", "fail"}:
        return
    _command_evidence(criterion, retained, complete=complete)
    stdout = retained.get("stdout.bin")
    expected = (
        criterion.expected_stdout
        if execution.outcome == "pass"
        else criterion.expected_failure_stdout or criterion.expected_stdout
    )
    if (
        not execution.execution_observed
        or execution.stdout_sha256 is None
        or execution.stderr_sha256 is None
        or execution.process_outcome != "exited"
        or execution.returncode is None
        or (execution.returncode != 0 if execution.outcome == "pass" else execution.returncode <= 0)
        or execution.error is not None
        or (stdout is not None and expected.encode() not in stdout)
    ):
        raise IntegrityError("Calibration pass/fail lacks observed verifier execution evidence.")
    cleanup = [
        parse_model(content, CleanupReport)
        for name, content in retained.items()
        if name.endswith("-cleanup.json")
    ]
    if (complete and len(cleanup) != 1) or any(
        not item.known_writers_stopped or item.errors for item in cleanup
    ):
        raise IntegrityError("Calibration verifier cleanup evidence is unresolved or missing.")


def load_calibration(store: ObjectStore, reference: str) -> CaseCalibration:
    record = store.load(reference, CaseCalibration, kind=ObjectKind.CALIBRATION)
    manifest = store.get(reference, kind=ObjectKind.CALIBRATION)
    if manifest.references != (record.case_id,) or manifest.files != record.files:
        raise IntegrityError("Calibration manifest differs from its case or evidence map.")
    case = load_frozen_case(store, record.case_id)
    hidden = store.read_blobs(record.case_id)
    files = store.read_blobs(reference)
    _validate_calibration(record, case, hidden, files)
    return record


def _validate_calibration(
    record: CaseCalibration, case: FrozenCase, hidden: dict[str, bytes], files: dict[str, bytes]
) -> None:
    prefixes = tuple(
        _evidence_prefix(criterion, side)
        for criterion in case.criteria
        if isinstance(criterion, DeterministicCriterion)
        for side in ("baseline", "reference")
        if side == "baseline" or case.reference_patch_file
    )
    if any(name != "cleanup.json" and not name.startswith(prefixes) for name in files):
        raise IntegrityError("Calibration contains evidence outside its declared executions.")
    if record.criteria_id != criteria_identity(case) or len(record.criteria) != len(case.criteria):
        raise IntegrityError("Calibration criteria identity differs from its frozen case.")
    for result, criterion in zip(record.criteria, case.criteria, strict=True):
        if (result.criterion_id, result.kind, result.required, result.verifier_id) != (
            criterion.criterion_id,
            criterion.kind,
            criterion.required,
            verifier_identity(case, criterion, hidden),
        ):
            raise IntegrityError("Calibration verifier identity differs from its frozen criterion.")
        if isinstance(criterion, DeterministicCriterion) and result.calibration:
            calibration = result.calibration
            if calibration.baseline is None or bool(calibration.reference) != bool(
                case.reference_patch_file
            ):
                raise IntegrityError(
                    "Calibration executions differ from the available baseline/reference."
                )
            for side, execution in (
                ("baseline", calibration.baseline),
                ("reference", calibration.reference),
            ):
                if execution is not None:
                    _execution_evidence(
                        criterion, side, execution, files, complete=record.evidence_complete
                    )
    cleanup = parse_model(files.get("cleanup.json", b"{}"), CleanupReport)
    if not cleanup.terminal_closed or not cleanup.known_writers_stopped or cleanup.errors:
        raise IntegrityError("Calibration requires stopped owned writers.")
