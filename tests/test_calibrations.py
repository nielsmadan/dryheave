import io
import json
import signal
import subprocess
import sys
import tarfile
import threading

import psutil
import pytest

from dryheave.bundles import export_bundle, import_bundle
from dryheave.calibration_models import CaseCalibration
from dryheave.calibration_recovery import (
    CalibrationOperation,
    CalibrationOwnership,
    reconcile_calibrations,
)
from dryheave.calibrations import calibrate_case, load_calibration
from dryheave.cases import RubricCriterion, load_frozen_case
from dryheave.cli import main
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import InputError, IntegrityError, LockBusyError
from dryheave.models import CommandSpec, ObjectKind
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore


def altered_case(store, reference, *, program=None, criteria=None, reference_patch=True, setup=()):
    case = load_frozen_case(store, reference)
    files = store.read_blobs(reference)
    if program is not None:
        files["verifiers/hidden.py"] = program.encode()
    if not reference_patch:
        files.pop("reference.patch", None)
    case = case.model_copy(
        update={
            "criteria": case.criteria if criteria is None else criteria,
            "reference_patch_file": case.reference_patch_file if reference_patch else None,
            "setup": setup,
        }
    )
    return store.put(
        ObjectKind.CASE, case, files=files, references=(case.repository_id, case.persona_id)
    )


def calibration_objects(store):
    return [
        path.name
        for path in (store.root / "objects").iterdir()
        if store.get(path.name).kind == ObjectKind.CALIBRATION
    ]


@pytest.mark.integration
def test_public_calibration_retains_baseline_failure_reference_success_without_run(
    store, graded_benchmark, historical_repo, capsys
):
    case_id = graded_benchmark.cases[0]
    source = historical_repo[0]
    original = (source / "greet.py").read_bytes()
    common = ["--store", str(store.root), "--json"]
    store.set_alias("calibrate-me", case_id)
    assert main([*common, "case", "calibrate", "calibrate-me"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    identifier = data["id"]
    record = load_calibration(store, identifier)
    assert record.case_id == case_id
    assert record.status == "demonstrated"
    calibration = record.criteria[0].calibration
    assert calibration.baseline.outcome == "fail"
    assert calibration.reference.outcome == "pass"
    assert calibration.baseline.execution_observed
    assert calibration.reference.execution_observed
    assert (source / "greet.py").read_bytes() == original
    assert not (store.root / "runs").exists()
    path = next(
        name for name in record.files if "/baseline/" in name and name.endswith("stdout.bin")
    )
    assert (
        main([*common, "case", "calibration", identifier, "--evidence", path, "--limit", "8"]) == 0
    )
    evidence = json.loads(capsys.readouterr().out)["data"]["evidence"]
    assert evidence["text"] == "GREETING"
    assert evidence["next_offset"] == 8
    assert evidence["truncated"] is True
    assert store.get(identifier).references == (case_id,)
    assert record.files == store.get(identifier).files


@pytest.mark.integration
@pytest.mark.parametrize(
    "program",
    [
        "raise RuntimeError('broken runtime')\n",
        "import nonexistent_calibration_dependency\n",
        "invalid python syntax\n",
        "print('no actual checks')\n",
        "print('PRIVATE_VERIFIER_SENTINEL'); raise SystemExit(1)\n",
    ],
)
def test_runtime_and_unobserved_execution_errors_do_not_demonstrate_failure(
    store, graded_benchmark, program
):
    case_id = altered_case(store, graded_benchmark.cases[0], program=program)
    record = load_calibration(store, calibrate_case(store, case_id))
    assert record.status == "unavailable"
    result = record.criteria[0].calibration
    assert result.baseline.outcome == "error"
    assert result.reference.outcome == "error"
    assert result.baseline.error == "expected_execution_evidence_missing"


@pytest.mark.integration
def test_passing_baseline_is_ineffective(store, graded_benchmark):
    case_id = altered_case(
        store, graded_benchmark.cases[0], program="print('PRIVATE_VERIFIER_SENTINEL')\n"
    )
    record = load_calibration(store, calibrate_case(store, case_id))
    assert record.status == "ineffective"
    assert record.criteria[0].calibration.baseline.outcome == "pass"
    assert record.criteria[0].calibration.reference.outcome == "pass"


@pytest.mark.integration
def test_direct_executable_hidden_entrypoint_is_trusted(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    program = (
        f"#!{sys.executable}\n"
        + store.read_blob(graded_benchmark.cases[0], "verifiers/hidden.py").decode()
    )
    criterion = case.criteria[0].model_copy(
        update={"command": CommandSpec(argv=("{verifier}/hidden.py",))}
    )
    case_id = altered_case(store, graded_benchmark.cases[0], program=program, criteria=(criterion,))
    assert load_calibration(store, calibrate_case(store, case_id)).status == "demonstrated"


@pytest.mark.integration
def test_one_byte_verifier_output_calibrates_and_assesses_with_retained_metadata(
    store, graded_benchmark
):
    from dryheave.assessments import assess_run
    from dryheave.experiments import create_experiment
    from dryheave.integrity import load_assessment
    from dryheave.runner import run_experiment
    from dryheave.runner_models import RunOptions

    case = load_frozen_case(store, graded_benchmark.cases[0])
    criterion = case.criteria[0].model_copy(
        update={
            "command": case.criteria[0].command.model_copy(update={"max_output_bytes": 1}),
            "expected_stdout": "P",
            "expected_failure_stdout": "F",
        }
    )
    case_id = altered_case(
        store,
        graded_benchmark.cases[0],
        program=(
            "import runpy, sys\n"
            "greet = runpy.run_path('greet.py')['greet']\n"
            "passed = greet('Niels') == 'Hello, Niels' and greet('') == 'Hello, '\n"
            "sys.stdout.write('P' if passed else 'F')\n"
            "raise SystemExit(0 if passed else 1)\n"
        ),
        criteria=(criterion,),
    )
    identifier = calibrate_case(store, case_id)
    record = load_calibration(store, identifier)
    assert record.status == "demonstrated"
    for side, marker, outcome in (("baseline", b"F", "fail"), ("reference", b"P", "pass")):
        execution = getattr(record.criteria[0].calibration, side)
        assert execution.outcome == outcome
        assert execution.process_outcome == "exited"
        assert execution.execution_observed
        assert execution.error is None
        stdout = next(
            name for name in record.files if f"/{side}/" in name and name.endswith("stdout.bin")
        )
        assert store.read_blob(identifier, stdout) == marker
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark.model_copy(update={"cases": (case_id,)})),
        options=RunOptions(mode="offline-fixture"),
    )
    result = load_assessment(store, assess_run(store, summary.run_id)[0])
    assert result.completion == "pass"
    assert result.eligible
    check = result.criteria[0]
    assert check.execution.outcome == "pass"
    assert check.execution.stdout_sha256 == digest(b"P")
    assert check.calibration.status == "demonstrated"
    assert check.calibration.baseline.outcome == "fail"
    assert check.calibration.reference.outcome == "pass"


@pytest.mark.integration
def test_timeout_is_a_retained_error_and_stops_verifier(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    criterion = case.criteria[0].model_copy(
        update={"command": case.criteria[0].command.model_copy(update={"timeout_seconds": 1})}
    )
    case_id = altered_case(
        store,
        graded_benchmark.cases[0],
        program="import time\nprint('PRIVATE_VERIFIER_SENTINEL', flush=True)\ntime.sleep(30)\n",
        criteria=(criterion,),
    )
    record = load_calibration(store, calibrate_case(store, case_id))
    assert record.status == "unavailable"
    assert record.criteria[0].calibration.baseline.process_outcome == "timeout"
    assert record.criteria[0].calibration.reference.outcome == "error"


@pytest.mark.integration
def test_completed_verifier_stops_observed_detached_child_before_snapshot(store, graded_benchmark):
    program = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(0.3)\nprint('PRIVATE_VERIFIER_SENTINEL', flush=True)\n"
    )
    case_id = altered_case(store, graded_benchmark.cases[0], program=program, reference_patch=False)
    record = load_calibration(store, calibrate_case(store, case_id))
    assert record.status == "ineffective"
    state = parse_model(
        next((store.root / "calibrations").glob("*/operation.json")).read_bytes(),
        CalibrationOperation,
    )
    assert len(state.owned) >= 2
    assert state.cleanup.known_writers_stopped
    for identity in state.owned:
        try:
            process = psutil.Process(identity.pid)
            assert (
                process.create_time() != identity.created
                or process.status() == psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            pass


@pytest.mark.integration
def test_missing_reference_and_judge_only_remain_honest(store, graded_benchmark):
    missing = altered_case(store, graded_benchmark.cases[0], reference_patch=False)
    record = load_calibration(store, calibrate_case(store, missing))
    assert record.status == "unavailable"
    assert record.criteria[0].calibration.baseline.outcome == "fail"
    assert record.criteria[0].calibration.reference is None
    judge = altered_case(
        store,
        missing,
        reference_patch=False,
        criteria=(
            RubricCriterion(
                criterion_id="quality",
                rubric="Judge the clarity of the resulting greeting implementation.",
            ),
        ),
    )
    record = load_calibration(store, calibrate_case(store, judge))
    assert record.status == "not_applicable"
    assert record.criteria[0].kind == "judge"
    assert record.criteria[0].calibration is None
    assert set(record.files) == {"cleanup.json"}


@pytest.mark.integration
def test_shared_suites_keep_side_order_and_independent_copies(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    program = (
        "from pathlib import Path\nimport sys, runpy\n"
        "greet = runpy.run_path('greet.py')['greet']\n"
        "side = 'reference' if greet('Niels') == 'Hello, Niels' else 'baseline'\n"
        "state = Path('state')\n"
        "if sys.argv[1] == 'first':\n"
        "    assert not state.exists()\n"
        "    state.write_text(side)\n"
        "else:\n"
        "    assert state.read_text() == side\n"
        "print('PRIVATE_VERIFIER_SENTINEL' if side == 'reference' else 'GREETING_ASSERTION_FAILED')\n"
        "raise SystemExit(0 if side == 'reference' else 1)\n"
    )
    first, second = (
        case.criteria[0].model_copy(
            update={
                "criterion_id": name,
                "suite_id": "ordered",
                "command": CommandSpec(argv=(sys.executable, "{verifier}/hidden.py", name)),
            }
        )
        for name in ("first", "second")
    )
    shared = altered_case(
        store, graded_benchmark.cases[0], program=program, criteria=(first, second)
    )
    record = load_calibration(store, calibrate_case(store, shared))
    assert record.status == "demonstrated"
    assert [item.criterion_id for item in record.criteria] == ["first", "second"]
    isolated = altered_case(
        store,
        shared,
        criteria=tuple(item.model_copy(update={"suite_id": None}) for item in (first, second)),
    )
    record = load_calibration(store, calibrate_case(store, isolated))
    assert record.status == "unavailable"
    assert record.criteria[1].calibration.baseline.outcome == "error"
    assert record.criteria[1].calibration.reference.outcome == "error"


@pytest.mark.integration
def test_verifier_environment_and_setup_match_assessment_contract(
    store, graded_benchmark, tmp_path
):
    marker = tmp_path / "setup-ran"
    setup = CommandSpec(
        argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()")
    )
    case_id = altered_case(store, graded_benchmark.cases[0], setup=(setup,))
    assert load_calibration(store, calibrate_case(store, case_id)).status == "demonstrated"
    assert not marker.exists()


@pytest.mark.parametrize(
    "mutation",
    ["case", "criteria", "verifier", "files", "stdout", "result", "missing-result", "command"],
)
def test_calibration_validates_exact_identity_and_retained_evidence(store, calibrated, mutation):
    identifier = calibrated
    record = load_calibration(store, identifier)
    files = store.read_blobs(identifier)
    if mutation == "case":
        changed = altered_case(
            store, record.case_id, program="print('PRIVATE_VERIFIER_SENTINEL')\n"
        )
        record = record.model_copy(update={"case_id": changed})
    elif mutation == "criteria":
        record = record.model_copy(update={"criteria_id": "a" * 64})
    elif mutation == "verifier":
        record = record.model_copy(
            update={"criteria": (record.criteria[0].model_copy(update={"verifier_id": "a" * 64}),)}
        )
    elif mutation == "files":
        record = record.model_copy(update={"files": {}})
    else:
        suffix = (
            "-command.json"
            if mutation == "command"
            else "stdout.bin"
            if mutation == "stdout"
            else "result.json"
        )
        path = next(name for name in files if "/baseline/" in name and name.endswith(suffix))
        if mutation == "missing-result":
            del files[path]
        elif mutation == "stdout":
            files[path] = b"different observed output\n"
        elif mutation == "command":
            files[path] = canonical_json(
                parse_model(files[path], CommandSpec).model_copy(update={"timeout_seconds": 1})
            )
        else:
            files[path] = canonical_json(record.criteria[0].calibration.reference)
        record = record.model_copy(
            update={"files": {name: digest(content) for name, content in files.items()}}
        )
    changed = store.put(ObjectKind.CALIBRATION, record, files=files, references=(record.case_id,))
    with pytest.raises((InputError, IntegrityError)):
        load_calibration(store, changed)
    assert load_calibration(store, identifier).status == "demonstrated"


@pytest.mark.integration
def test_changed_case_alias_never_reuses_old_calibration(store, graded_benchmark):
    original = graded_benchmark.cases[0]
    store.set_alias("selected", original)
    identifier = calibrate_case(store, "selected")
    changed = altered_case(store, original, program="print('PRIVATE_VERIFIER_SENTINEL')\n")
    store.set_alias("selected", changed, replace=True)
    assert load_calibration(store, identifier).case_id == original
    assert load_calibration(store, calibrate_case(store, "selected")).case_id == changed
    assert load_calibration(store, identifier).status == "demonstrated"


@pytest.mark.integration
def test_standalone_bundle_roundtrip_requires_declared_sensitive_evidence(
    store, graded_benchmark, tmp_path
):
    identifier = calibrate_case(store, graded_benchmark.cases[0])
    with pytest.raises(InputError, match="calibration-evidence"):
        export_bundle(store, identifier, tmp_path / "undeclared.tar")
    path = tmp_path / "calibration.tar"
    exported = export_bundle(store, identifier, path, include_sensitive=("calibration-evidence",))
    assert "calibration-evidence" in exported.included
    assert "standalone calibration verifier outputs" not in exported.omitted
    target = ObjectStore(tmp_path / "imported")
    assert import_bundle(target, path) == exported
    assert load_calibration(target, identifier) == load_calibration(store, identifier)
    assert target.read_blobs(identifier) == store.read_blobs(identifier)
    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(path) as source, tarfile.open(unsafe, "w") as output:
        for member in source:
            content = source.extractfile(member).read()
            if member.name == "bundle.json":
                manifest = json.loads(content)
                manifest["included"].remove("calibration-evidence")
                content = canonical_json(manifest)
                member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    destination = ObjectStore(tmp_path / "rejected")
    with pytest.raises(IntegrityError, match="undeclared sensitive"):
        import_bundle(destination, unsafe)
    assert not (destination.root / "objects").exists()


def test_native_lock_prevents_calibration_execution(store, graded_benchmark):
    with store.native_lock(), pytest.raises(LockBusyError):
        calibrate_case(store, graded_benchmark.cases[0])
    assert calibration_objects(store) == []


@pytest.mark.integration
@pytest.mark.parametrize("action", ["calibrate", "run", "assess"])
def test_hard_interrupted_initial_publication_allows_later_native_progress(
    store, graded_benchmark, action
):
    from dryheave.assessments import assess_run
    from dryheave.experiments import create_experiment
    from dryheave.integrity import load_assessment
    from dryheave.runner import run_experiment
    from dryheave.runner_models import RunOptions

    case_id = graded_benchmark.cases[0]
    experiment = create_experiment(store, graded_benchmark)
    summary = (
        run_experiment(store, experiment, options=RunOptions(mode="offline-fixture"))
        if action == "assess"
        else None
    )
    program = (
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "from dryheave import filesystem\n"
        "from dryheave.calibrations import calibrate_case\n"
        "from dryheave.storage import ObjectStore\n"
        "def interrupt(*args, **kwargs):\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "filesystem.write_all = interrupt\n"
        "calibrate_case(ObjectStore(Path(sys.argv[1])), sys.argv[2])\n"
    )
    interrupted = subprocess.run(
        [sys.executable, "-c", program, str(store.root), case_id],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert interrupted.returncode == -signal.SIGKILL, interrupted.stderr.decode()
    entries = list((store.root / "calibrations").iterdir())
    assert len(entries) == 1
    staged = entries[0]
    assert staged.name.startswith(".pending-")
    original = {path.name: path.read_bytes() for path in staged.iterdir()}
    assert len(original) == 1
    assert list(original.values()) == [b""]
    if action == "calibrate":
        assert load_calibration(store, calibrate_case(store, case_id)).status == "demonstrated"
    elif action == "run":
        result = run_experiment(store, experiment, options=RunOptions(mode="offline-fixture"))
        assert len(result.pending_assessment) == 1
    else:
        result = load_assessment(store, assess_run(store, summary.run_id)[0])
        assert result.completion == "pass"
        assert result.eligible
    assert {path.name: path.read_bytes() for path in staged.iterdir()} == original


@pytest.mark.integration
def test_interrupted_verifier_stops_owned_writers_and_retains_recovery_evidence(
    store, graded_benchmark, monkeypatch
):
    from dryheave import calibrations

    case_id = altered_case(
        store,
        graded_benchmark.cases[0],
        program="import time\nprint('started', flush=True)\ntime.sleep(30)\n",
    )
    check = calibrations.check_copy
    timers = []

    def cancel(context, criterion, side):
        own = context.on_identity

        def cancel_after_launch(identity):
            own(identity)
            timer = threading.Timer(0.2, context.cancelled.set)
            timers.append(timer)
            timer.start()

        context.on_identity = cancel_after_launch
        return check(context, criterion, side)

    monkeypatch.setattr(calibrations, "check_copy", cancel)
    with pytest.raises(KeyboardInterrupt):
        calibrate_case(store, case_id)
    for timer in timers:
        timer.join()
    state_path = next((store.root / "calibrations").glob("*/operation.json"))
    state = parse_model(state_path.read_bytes(), CalibrationOperation)
    assert state.owned
    assert state.cleanup.known_writers_stopped
    assert state.result_id is None
    assert calibration_objects(store) == []
    assert list(state_path.parent.glob("checks/**/*-cleanup.json"))
    assert all(
        not psutil.pid_exists(item.pid) or psutil.Process(item.pid).create_time() != item.created
        for item in state.owned
    )


@pytest.mark.integration
def test_unresolved_owned_writers_block_evidence_capture_and_publication(
    store, graded_benchmark, monkeypatch
):
    from dryheave import calibrations

    def unresolved(_self):
        raise InputError("unresolved owned writers")

    def capture_forbidden(*_args):
        pytest.fail("Captured evidence while writers were unresolved")

    monkeypatch.setattr(CalibrationOwnership, "reconcile", unresolved)
    monkeypatch.setattr(calibrations, "_evidence", capture_forbidden)
    with pytest.raises(InputError, match="unresolved"):
        calibrate_case(store, graded_benchmark.cases[0])
    assert calibration_objects(store) == []
    assert list((store.root / "calibrations").glob("*/operation.json"))


@pytest.mark.integration
@pytest.mark.parametrize("matching", [True, False])
def test_recovery_stops_only_matching_recorded_process_identities(
    store, graded_benchmark, matching
):
    root = store.root / "calibrations" / ("a" * 32)
    ownership = CalibrationOwnership.create(root, graded_benchmark.cases[0])
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            identity = ProcessIdentity(
                pid=process.pid,
                created=psutil.Process(process.pid).create_time() + (0 if matching else 1),
            )
            ownership.own(identity)
            with store.native_lock():
                reconcile_calibrations(store)
            if matching:
                assert process.poll() is not None
            else:
                assert process.poll() is None
            state = parse_model((root / "operation.json").read_bytes(), CalibrationOperation)
            assert state.cleanup.known_writers_stopped
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


@pytest.mark.integration
def test_schema_version_is_strict(store, graded_benchmark):
    record = load_calibration(store, calibrate_case(store, graded_benchmark.cases[0]))
    for version in (2, "1", True):
        payload = record.model_dump(mode="json") | {"schema_version": version}
        with pytest.raises(InputError):
            parse_model(canonical_json(payload), CaseCalibration)


@pytest.mark.integration
@pytest.mark.parametrize("suite", [None, "workspace"])
def test_criterion_names_do_not_hide_required_evidence(store, graded_benchmark, suite):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    criterion = case.criteria[0].model_copy(update={"criterion_id": "hidden", "suite_id": suite})
    case_id = altered_case(store, graded_benchmark.cases[0], criteria=(criterion,))
    record = load_calibration(store, calibrate_case(store, case_id))
    assert record.status == "demonstrated"
    assert any(name.endswith("hidden-evidence/stdout.bin") for name in record.files)


@pytest.mark.integration
@pytest.mark.parametrize("action", ["calibrate", "run", "assess"])
def test_unresolved_calibration_recovery_guards_all_native_entrypoints(
    store, graded_benchmark, monkeypatch, action
):
    from dryheave.assessments import assess_run
    from dryheave.experiments import create_experiment
    from dryheave.runner import run_experiment
    from dryheave.runner_models import RunOptions

    experiment = create_experiment(store, graded_benchmark)
    summary = (
        run_experiment(store, experiment, options=RunOptions(mode="offline-fixture"))
        if action == "assess"
        else None
    )
    ownership = CalibrationOwnership.create(
        store.root / "calibrations" / ("b" * 32), graded_benchmark.cases[0]
    )
    current = psutil.Process()
    ownership.own(ProcessIdentity(pid=current.pid, created=current.create_time()))
    process = psutil.Process

    def denied(pid=None):
        if pid == current.pid:
            raise psutil.AccessDenied(pid)
        return process(pid)

    monkeypatch.setattr("dryheave.calibration_recovery.psutil.Process", denied)
    with pytest.raises(InputError, match="unresolved owned writers"):
        if action == "calibrate":
            calibrate_case(store, graded_benchmark.cases[0])
        elif action == "run":
            run_experiment(store, experiment, options=RunOptions(mode="offline-fixture"))
        else:
            assess_run(store, summary.run_id)
    state = parse_model((ownership.root / "operation.json").read_bytes(), CalibrationOperation)
    assert state.cleanup.errors == ("recovery_process_visibility_denied",)
    assert not state.cleanup.known_writers_stopped


def test_public_cancellation_returns_retry_instruction(
    store, graded_benchmark, monkeypatch, capsys
):
    def interrupted(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.authoring_cli.calibrate_case", interrupted)
    assert (
        main(["--store", str(store.root), "case", "calibrate", graded_benchmark.cases[0], "--json"])
        == 130
    )
    response = capsys.readouterr()
    assert json.loads(response.err)["error"]["code"] == "cancelled"
    assert "case calibrate" in json.loads(response.err)["error"]["message"]


@pytest.mark.integration
def test_run_export_discloses_unlinked_calibration_evidence(store, graded_benchmark, tmp_path):
    from dryheave.experiments import create_experiment
    from dryheave.runner import run_experiment
    from dryheave.runner_models import RunOptions

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    manifest = export_bundle(
        store, summary.run_id, tmp_path / "run.tar", include_sensitive=("calibration-evidence",)
    )
    assert "standalone calibration verifier outputs" in manifest.omitted
    assert manifest.included == ("curated-inputs", "structured-results")
