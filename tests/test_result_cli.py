import json
import subprocess
import sys
import time

import pytest

from conftest import judged_case_benchmark
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
        "import pathlib,time\n"
        'pathlib.Path("ready").write_text("ready")\n'
        "deadline = time.monotonic() + 25\n"
        'while not pathlib.Path("release").exists() and time.monotonic() < deadline:\n'
        "    time.sleep(0.02)\n"
        'print(\'{"decision":{"action":"stop","reason":"fixture completed"}}\')\n'
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
            [*command, "run", "--status", run_id], capture_output=True, timeout=20, check=False
        )
        assert status.returncode == 0, status.stderr.decode()
        state = json.loads(status.stdout)["data"]
        assert state["attempts"][0]["stage"] == "interacting"
        report = subprocess.run(
            [*command, "report", run_id], capture_output=True, timeout=20, check=False
        )
        assert report.returncode == 0, report.stderr.decode()
        assert json.loads(report.stdout)["data"]["attempts"][0]["stage"] == "interacting"
        (ready[0].parent / "release").write_text("release")
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


def test_assess_case_requires_regrade(store, capsys):
    assert main(["--store", str(store.root), "assess", "a" * 32, "--case", "b" * 64]) == 2
    assert capsys.readouterr().err == "dryheave: --case requires --regrade.\n"


def test_regrade_cancellation_resumes_the_same_regrade_command(store, capsys, monkeypatch):
    import shlex

    def cancelled(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("dryheave.result_cli.regrade_run", cancelled)
    run_id, case_id = "a" * 32, "b" * 64
    args = ["--store", str(store.root), "assess", run_id, "--regrade", "--case", case_id]
    assert main(args) == 130
    assert capsys.readouterr().err.endswith(
        "Resume with: " + shlex.join(("dryheave", *args)) + "\n"
    )


@pytest.mark.integration
def test_cli_regrade_returns_its_regrade_id_and_the_chained_report(store, graded_benchmark, capsys):
    prefix = ["--store", str(store.root), "--json"]
    identifier = create_experiment(store, graded_benchmark)
    assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]
    assert main([*prefix, "assess", run_id]) == 0
    (original,) = json.loads(capsys.readouterr().out)["data"]["assessment_ids"]
    assert main([*prefix, "assess", run_id, "--regrade"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["regrade_id"].startswith("r-")
    (regraded,) = data["assessment_ids"]
    attempt = data["report"]["attempts"][0]
    assert (attempt["assessment_id"], attempt["result"]["previous_id"]) == (regraded, original)


@pytest.mark.integration
def test_cli_report_html_writes_self_contained_file_and_refuses_overwrite(
    store, graded_benchmark, tmp_path, capsys
):
    benchmark, case_id = judged_case_benchmark(store, graded_benchmark, tmp_path)
    prefix = ["--store", str(store.root), "--json"]
    identifier = create_experiment(store, benchmark)
    assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]
    assert main([*prefix, "assess", run_id]) == 0
    capsys.readouterr()

    plain = ["--store", str(store.root), "report", run_id, "--html"]
    destination = tmp_path / "report.html"
    assert main([*plain, str(destination)]) == 0
    content = destination.read_text()
    assert "<script" not in content
    assert case_id in content
    assert "reasoning not included" in content
    assert "Judged quality." not in content

    assert main([*plain, str(destination)]) == 1
    assert destination.read_text() == content
    assert main([*plain, str(destination), "--force"]) == 0

    sensitive_destination = tmp_path / "sensitive-report.html"
    assert (
        main([*plain, str(sensitive_destination), "--include-sensitive", "assessment-evidence"])
        == 0
    )
    assert "Judged quality." in sensitive_destination.read_text()


@pytest.mark.integration
def test_cli_report_of_several_runs_returns_each_report_with_its_settings(
    store, graded_benchmark, tmp_path, capsys
):
    benchmark, _case_id = judged_case_benchmark(store, graded_benchmark, tmp_path)
    prefix = ["--store", str(store.root), "--json"]
    identifier = create_experiment(store, benchmark)
    run_ids = []
    for _ in range(2):
        assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
        run_ids.append(json.loads(capsys.readouterr().out)["data"]["run_id"])
        assert main([*prefix, "assess", run_ids[-1]]) == 0
        capsys.readouterr()

    assert main([*prefix, "report", *run_ids]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert [report["run_id"] for report in data["reports"]] == run_ids
    assert [setup["run_id"] for setup in data["setups"]] == run_ids
    assert data["setups"][0]["variants"].keys() == {variant.name for variant in benchmark.variants}

    destination = tmp_path / "combined.html"
    assert main(["--store", str(store.root), "report", *run_ids, "--html", str(destination)]) == 0
    content = destination.read_text()
    assert content.count('<table class="topline">') == 1
    assert all(f'<h2 class="run">Run {run_id}</h2>' in content for run_id in run_ids)

    assert main(["--store", str(store.root), "report", run_ids[0], run_ids[0]]) == 2
    assert capsys.readouterr().err == "dryheave: Each report reference may appear only once.\n"


@pytest.mark.integration
def test_cli_report_html_renders_missing_case_without_aborting(
    store, graded_benchmark, tmp_path, capsys, monkeypatch
):
    from dryheave.errors import NotFoundError
    from dryheave.viewer import ViewerService

    benchmark, case_id = judged_case_benchmark(store, graded_benchmark, tmp_path)
    prefix = ["--store", str(store.root), "--json"]
    identifier = create_experiment(store, benchmark)
    assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]
    assert main([*prefix, "assess", run_id]) == 0
    capsys.readouterr()

    def missing_case(_self, requested_case_id):
        raise NotFoundError(f"Case not found: {requested_case_id}")

    monkeypatch.setattr(ViewerService, "case", missing_case)
    destination = tmp_path / "missing-case-report.html"
    assert main(["--store", str(store.root), "report", run_id, "--html", str(destination)]) == 0
    content = destination.read_text()
    assert "case not available in this store" in content
    assert case_id in content


def test_report_rejects_more_references_than_its_cap(store, capsys):
    references = [f"{index:032x}" for index in range(17)]
    assert main(["--store", str(store.root), "report", *references]) == 2
    assert capsys.readouterr().err == "dryheave: Report at most 16 runs at once.\n"


def test_report_rejects_two_references_to_the_same_run(store, capsys, monkeypatch):
    from dryheave import result_cli
    from dryheave.report_models import RunReport

    monkeypatch.setattr(
        result_cli,
        "report_run",
        lambda _store, _reference: RunReport.model_construct(run_id="a" * 32),
    )
    assert main(["--store", str(store.root), "report", "a" * 32, "portable-copy"]) == 2
    assert capsys.readouterr().err == (
        "dryheave: Each run may appear only once; two references name the same run.\n"
    )


def test_report_rejects_force_or_include_sensitive_without_html(store, capsys):
    run_id = "a" * 32
    assert main(["--store", str(store.root), "report", run_id, "--force"]) == 2
    assert capsys.readouterr().err == "dryheave: --force and --include-sensitive require --html.\n"
    assert (
        main(
            [
                "--store",
                str(store.root),
                "report",
                run_id,
                "--include-sensitive",
                "assessment-evidence",
            ]
        )
        == 2
    )
    assert capsys.readouterr().err == "dryheave: --force and --include-sensitive require --html.\n"


def test_report_html_rejects_sensitive_classes_other_than_assessment_evidence(store, capsys):
    run_id = "a" * 32
    assert (
        main(
            [
                "--store",
                str(store.root),
                "report",
                run_id,
                "--html",
                "out.html",
                "--include-sensitive",
                "captures",
            ]
        )
        == 2
    )
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.integration
def test_cli_report_html_reasoning_does_not_depend_on_the_request_journal_budget(
    store, graded_benchmark, tmp_path, capsys, monkeypatch
):
    from dryheave import viewer as viewer_module

    benchmark, _case_id = judged_case_benchmark(store, graded_benchmark, tmp_path)
    prefix = ["--store", str(store.root), "--json"]
    identifier = create_experiment(store, benchmark)
    assert main([*prefix, "run", identifier, "--mode", "offline-fixture"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]
    assert main([*prefix, "assess", run_id]) == 0
    capsys.readouterr()

    monkeypatch.setattr(viewer_module, "MAX_REQUEST_JOURNAL_BYTES", 1)
    destination = tmp_path / "tight-budget-report.html"
    assert (
        main(
            [
                "--store",
                str(store.root),
                "report",
                run_id,
                "--html",
                str(destination),
                "--include-sensitive",
                "assessment-evidence",
            ]
        )
        == 0
    )
    assert "Judged quality." in destination.read_text()
