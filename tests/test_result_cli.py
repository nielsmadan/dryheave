import json
import subprocess
import sys
import time

import pytest

from dryheave.cli import main
from dryheave.controller_models import ControllerRecipe
from dryheave.experiments import create_experiment
from dryheave.models import CommandSpec


@pytest.mark.integration
def test_public_loop_and_portable_result_comparison(store, graded_benchmark, tmp_path, capsys):
    identifier = create_experiment(store, graded_benchmark)
    prefix = ["--store", str(store.root), "--json"]
    assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
    output = capsys.readouterr()
    run_id = json.loads(output.out)["data"]["run_id"]
    assert run_id in output.err
    assert main([*prefix, "assess", run_id]) == 0
    result = json.loads(capsys.readouterr().out)["data"]
    assert result["report"]["attempts"][0]["result"]["eligible"]
    assert main([*prefix, "report", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["groups"][0]["eligible"] == 1
    bundle = tmp_path / "summary.tar"
    assert main([*prefix, "export", run_id, "--output", str(bundle)]) == 0
    root = json.loads(capsys.readouterr().out)["data"]["bundle"]["roots"][0]
    imported = ["--store", str(tmp_path / "imported"), "--json"]
    assert main([*imported, "import", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["roots"][0] == root
    assert main([*imported, "report", root]) == 0
    report = json.loads(capsys.readouterr().out)["data"]
    assert report["portable"]
    assert report["attempts"][0]["raw_evidence"] == "unavailable"
    assert main([*imported, "compare", root, root]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["paired_count"] == 1


@pytest.mark.integration
def test_real_second_cli_status_reads_progress_while_writer_is_active(
    store, graded_benchmark, tmp_path
):
    script = tmp_path / "controller.py"
    script.write_text(
        'import pathlib,time\npathlib.Path("ready").write_text("ready")\ntime.sleep(3)\nprint(\'{"decision":{"action":"stop","reason":"fixture completed"}}\')\n'
    )
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, str(script))),
    )
    identifier = create_experiment(store, graded_benchmark.model_copy(update={"simulator": recipe}))
    command = [sys.executable, "-m", "dryheave", "--store", str(store.root), "--json"]
    process = subprocess.Popen(
        [*command, "run", identifier, "--mode", "offline-fixture"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        ready = []
        while time.monotonic() < deadline and process.poll() is None:
            ready = (
                list((store.root / "runs").glob("*/attempts/*/controller/*/ready"))
                if (store.root / "runs").exists()
                else []
            )
            if ready:
                break
            time.sleep(0.02)
        assert ready
        run_id = ready[0].relative_to(store.root / "runs").parts[0]
        status = subprocess.run(
            [*command, "run", "--status", run_id], capture_output=True, timeout=2, check=False
        )
        assert status.returncode == 0, status.stderr.decode()
        state = json.loads(status.stdout)["data"]
        assert state["attempts"][0]["stage"] == "interacting"
        report = subprocess.run(
            [*command, "report", run_id], capture_output=True, timeout=2, check=False
        )
        assert report.returncode == 0, report.stderr.decode()
        assert json.loads(report.stdout)["data"]["attempts"][0]["stage"] == "interacting"
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout)["data"]["run_id"] == run_id
        assert run_id in stderr.decode()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


@pytest.mark.parametrize("structured", [False, True])
def test_assessment_cancellation_has_concise_resume_guidance(
    store, capsys, monkeypatch, structured
):
    import shlex

    def cancelled(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.result_cli.assess_run", cancelled)
    run_id = "a" * 32
    args = ["--store", str(store.root), "assess", run_id]
    assert main([*args, *(["--json"] if structured else [])]) == 130
    captured = capsys.readouterr()
    message = (
        "Assessment interrupted; captured output and grading progress are retained. Resume with: "
        + shlex.join(("dryheave", "--store", str(store.root), "assess", run_id))
    )
    assert captured.out == ""
    assert captured.err == (
        json.dumps({"ok": False, "error": {"code": "cancelled", "message": message}}) + "\n"
        if structured
        else "dryheave: " + message + "\n"
    )
