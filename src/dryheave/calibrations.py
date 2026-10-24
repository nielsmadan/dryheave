import os
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import JsonValue

from dryheave.calibration_models import (
    CalibratedFact,
    CaseCalibration,
    calibration_status,
    execution_verdict,
    fact_flags,
    fact_status,
)
from dryheave.calibration_recovery import CalibrationOwnership
from dryheave.cases import (
    CapturePolicy,
    FrozenCase,
    TaskFact,
    fact_verifier,
    graded_facts,
    judged_facts,
    load_frozen_case,
    verifier_facts,
)
from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.controllers import CallContext
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import CleanupReport
from dryheave.errors import DryheaveError, InputError, IntegrityError
from dryheave.filesystem import file_lock
from dryheave.final_capture import CaptureBuilder, IgnoreSelection, capture_workspace
from dryheave.grading import VerifierContext, check_copy, check_path, materialize_check
from dryheave.judges import (
    FactJudgment,
    fact_judge_recipe,
    fact_projection,
    invoke_fact_judge,
    judge_files,
)
from dryheave.models import CommandSpec, ObjectKind
from dryheave.native_recovery import reconcile_store_ownership
from dryheave.result_models import Calibration, CheckExecution
from dryheave.runner import cancellation_signals
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore

CHECK_CONTENT_DEPTH = 4
JUDGE_CONTENT_DEPTH = 2
JudgedSides = dict[str, dict[str, FactJudgment]]


def _fact_payload(fact: TaskFact) -> JsonValue:
    return fact.model_dump(mode="json", include={"fact_id", "kind", "phase", "check", "verifier"})


def facts_identity(case: FrozenCase) -> str:
    return digest(canonical_json({"facts": [_fact_payload(item) for item in graded_facts(case)]}))


def verifier_identity(case: FrozenCase, fact: TaskFact, blobs: dict[str, bytes]) -> str:
    return digest(
        canonical_json(
            {
                "fact": _fact_payload(fact),
                "hidden_files": {
                    name: digest(blobs[path]) for name, path in case.hidden_files.items()
                },
            }
        )
    )


def _sides(case: FrozenCase) -> tuple[str, ...]:
    return ("baseline", "reference") if case.reference_patch_file else ("baseline",)


def _verified_fact(
    context: VerifierContext, fact: TaskFact, ownership: CalibrationOwnership
) -> CalibratedFact:
    baseline = check_copy(context, fact, "baseline")
    ownership.reconcile()
    reference = None
    if context.case.reference_patch_file:
        reference = check_copy(context, fact, "reference")
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
    verdicts = (
        execution_verdict(baseline),
        None if reference is None else execution_verdict(reference),
    )
    return CalibratedFact(
        fact_id=fact.fact_id,
        kind="optional" if fact.kind == "optional" else "requirement",
        source="verifier",
        verifier_id=verifier_identity(context.case, fact, context.blobs),
        baseline=verdicts[0],
        reference=verdicts[1],
        flags=fact_flags(*verdicts),
        status=fact_status(*verdicts),
        calibration=calibration,
    )


def _judged_fact(context: VerifierContext, fact: TaskFact, judged: JudgedSides) -> CalibratedFact:
    baseline = judged.get("baseline", {}).get(fact.fact_id)
    reference = judged.get("reference", {}).get(fact.fact_id)
    verdicts = (
        baseline.verdict if baseline else "unclear",
        None
        if not context.case.reference_patch_file
        else reference.verdict
        if reference
        else "unclear",
    )
    return CalibratedFact(
        fact_id=fact.fact_id,
        kind="optional" if fact.kind == "optional" else "requirement",
        source="judge",
        verifier_id=verifier_identity(context.case, fact, context.blobs),
        baseline=verdicts[0],
        reference=verdicts[1],
        flags=fact_flags(*verdicts),
        status=fact_status(*verdicts),
        baseline_judgment=baseline,
        reference_judgment=reference,
    )


def _judge_side(
    context: VerifierContext,
    recipe: ControllerRecipe,
    side: str,
    ownership: CalibrationOwnership,
) -> tuple[RoleCall, dict[str, FactJudgment]]:
    recipe = fact_judge_recipe(recipe, len(judged_facts(context.case)))
    root = context.root.parent / "judge" / side
    workspace = root / "workspace"
    materialize_check(context, side, workspace)
    snapshot, blobs = capture_workspace(
        context.store, context.case.repository_id, workspace, context.case.capture_policy, root
    )
    request = fact_projection(
        context.case, judged_facts(context.case), judge_files(snapshot.files, blobs)
    )
    call_id = "j-" + uuid4().hex
    call, judgments = invoke_fact_judge(
        recipe,
        request,
        ArtifactWriter(
            root / call_id, recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes
        ),
        CallContext(
            call_id=call_id,
            index=0 if side == "baseline" else 1,
            deadline=time.monotonic() + recipe.budget.call_seconds,
            cancelled=context.cancelled,
            inherited=os.environ,
            on_identities=ownership.own,
            runtime_root=root / "runtime" / call_id,
        ),
    )
    ownership.reconcile()
    return call, {item.fact_id: item for item in judgments}


def _judge_sides(
    context: VerifierContext, recipe: ControllerRecipe | None, ownership: CalibrationOwnership
) -> tuple[tuple[RoleCall, ...], JudgedSides]:
    if recipe is None or not judged_facts(context.case):
        return (), {}
    calls: list[RoleCall] = []
    judged: JudgedSides = {}
    for side in _sides(context.case):
        if context.cancelled.is_set():
            raise KeyboardInterrupt
        try:
            call, judged[side] = _judge_side(context, recipe, side, ownership)
        except (DryheaveError, OSError) as error:
            call = RoleCall(
                call_id="j-" + uuid4().hex,
                role="judge",
                status="failed",
                error=type(error).__name__,
            )
            judged[side] = {}
        calls.append(call)
    return tuple(calls), judged


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
    judge_root = root.parent / "judge"

    def select_judge(paths: dict[str, int]) -> IgnoreSelection:
        return IgnoreSelection(
            omitted={
                path
                for path in paths
                if len(Path(path).parts) == JUDGE_CONTENT_DEPTH
                and Path(path).name in {"workspace", "runtime"}
            }
        )

    if judge_root.exists():
        for path, mode in builder.walk(judge_root, ignored=select_judge).items():
            builder.take(judge_root, path, mode, prefix="judge")
    files = builder.blobs | {"cleanup.json": canonical_json(cleanup)}
    return files, tuple(item.reason for item in builder.omissions if not item.intentional)


def _require_judge(case: FrozenCase, judge: ControllerRecipe | None) -> None:
    judged = judged_facts(case)
    if judged and (
        judge is None or judge.kind == "scripted" or judge.budget.max_calls < len(_sides(case))
    ):
        raise InputError(
            "Facts without a verifier need a judge to calibrate ("
            + ", ".join(fact.fact_id for fact in judged)
            + "); pass --judge MODEL[@EFFORT]."
        )


def calibrate_case(
    store: ObjectStore, reference: str, *, judge: ControllerRecipe | None = None
) -> str:
    case_id = store.resolve(reference)
    case = load_frozen_case(store, case_id)
    _require_judge(case, judge)
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
                verified = {}
                for fact in verifier_facts(case):
                    if cancelled.is_set():
                        raise KeyboardInterrupt
                    verified[fact.fact_id] = _verified_fact(context, fact, ownership)
                calls, judged = _judge_sides(context, judge, ownership)
            finally:
                ownership.reconcile()
            if cancelled.is_set():
                raise KeyboardInterrupt
            facts = tuple(
                verified.get(fact.fact_id) or _judged_fact(context, fact, judged)
                for fact in graded_facts(case)
            )
            store.verify(case_id)
            files, omissions = _evidence(context.root, ownership)
            record = CaseCalibration(
                case_id=case_id,
                facts_id=facts_identity(case),
                created_at=datetime.now(UTC),
                facts=facts,
                judge_calls=calls,
                status=calibration_status(facts) if not omissions else "unavailable",
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


def _evidence_prefix(fact: TaskFact, side: str) -> str:
    return (Path("checks") / check_path(fact, side) / (fact.fact_id + "-evidence")).as_posix() + "/"


def _command_evidence(fact: TaskFact, retained: dict[str, bytes], *, complete: bool) -> None:
    verifier = fact_verifier(fact)
    commands = [
        parse_model(content, CommandSpec)
        for name, content in retained.items()
        if name.endswith("-command.json")
    ]
    if complete and len(commands) != 1:
        raise IntegrityError("Calibration omits its executed verifier command.")
    for command in commands:
        index = verifier.command.argv.index("{verifier}/" + verifier.entrypoint)
        if len(command.argv) != len(verifier.command.argv):
            raise IntegrityError("Calibration command differs from its frozen verifier.")
        entrypoint = command.argv[index]
        suffix = "/" + verifier.entrypoint
        if not Path(entrypoint).is_absolute() or not entrypoint.endswith(suffix):
            raise IntegrityError("Calibration command has an invalid hidden entrypoint.")
        hidden = entrypoint.removesuffix(suffix)
        expected = tuple(
            argument.replace("{verifier}", hidden) for argument in verifier.command.argv
        )
        if index > 0:
            expected = (command.argv[0], *expected[1:])
        if command != verifier.command.model_copy(update={"argv": expected}):
            raise IntegrityError("Calibration command differs from its frozen verifier.")


def _execution_evidence(
    fact: TaskFact,
    side: str,
    execution: CheckExecution,
    files: dict[str, bytes],
    *,
    complete: bool,
) -> None:
    verifier = fact_verifier(fact)
    prefix = _evidence_prefix(fact, side)
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
    _command_evidence(fact, retained, complete=complete)
    stdout = retained.get("stdout.bin")
    expected = (
        verifier.expected_stdout
        if execution.outcome == "pass"
        else verifier.expected_failure_stdout or verifier.expected_stdout
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


def _judgment_consistency(result: CalibratedFact, failed: set[str]) -> None:
    for side, judgment in (
        ("baseline", result.baseline_judgment),
        ("reference", result.reference_judgment),
    ):
        if judgment is None:
            continue
        if judgment.fact_id != result.fact_id:
            raise IntegrityError("Calibration judgment names a different fact than its result.")
        if side in failed:
            raise IntegrityError("Calibration carries a judgment from a failed judge call.")
        if judgment.verdict != "unclear" and not judgment.evidence.strip():
            raise IntegrityError("Calibration judgment has a verdict without evidence.")


def _validate_calibration(
    record: CaseCalibration, case: FrozenCase, hidden: dict[str, bytes], files: dict[str, bytes]
) -> None:
    prefixes = tuple(
        _evidence_prefix(fact, side) for fact in verifier_facts(case) for side in _sides(case)
    )
    judge_prefixes = tuple(f"judge/{side}/" for side in _sides(case))
    if any(
        name != "cleanup.json" and not name.startswith((*prefixes, *judge_prefixes))
        for name in files
    ):
        raise IntegrityError("Calibration contains evidence outside its declared executions.")
    facts = graded_facts(case)
    if record.facts_id != facts_identity(case) or len(record.facts) != len(facts):
        raise IntegrityError("Calibration fact identity differs from its frozen case.")
    expected_calls = len(_sides(case)) if judged_facts(case) else 0
    if len(record.judge_calls) != expected_calls:
        raise IntegrityError("Calibration judge calls differ from one call per calibrated side.")
    judge_failed = {
        side
        for side, call in zip(_sides(case), record.judge_calls, strict=False)
        if call.status == "failed"
    }
    for result, fact in zip(record.facts, facts, strict=True):
        _judgment_consistency(result, judge_failed)
        source = "judge" if fact.verifier is None else "verifier"
        if (result.fact_id, result.kind, result.source, result.verifier_id) != (
            fact.fact_id,
            fact.kind,
            source,
            verifier_identity(case, fact, hidden),
        ):
            raise IntegrityError("Calibration verifier identity differs from its frozen fact.")
        if (result.reference is None) != (case.reference_patch_file is None):
            raise IntegrityError("Calibration sides differ from the available baseline/reference.")
        calibration = result.calibration
        if calibration is not None:
            for side, execution in (
                ("baseline", calibration.baseline),
                ("reference", calibration.reference),
            ):
                if execution is not None:
                    _execution_evidence(
                        fact, side, execution, files, complete=record.evidence_complete
                    )
    cleanup = parse_model(files.get("cleanup.json", b"{}"), CleanupReport)
    if not cleanup.terminal_closed or not cleanup.known_writers_stopped or cleanup.errors:
        raise IntegrityError("Calibration requires stopped owned writers.")
