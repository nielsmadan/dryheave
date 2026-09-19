import json
from pathlib import Path

import pytest

from dryheave.cli import main
from dryheave.errors import InputError
from dryheave.experiments import create_experiment
from dryheave.runner import load_capture, run_experiment
from dryheave.runner_models import RunOptions
from dryheave.workspaces import Workspace, WorkspaceConfig, resolve_transport


def test_assess_convenience_completes_existing_pipeline(store, graded_benchmark, capsys):
    identifier = create_experiment(store, graded_benchmark)
    common = ["--store", str(store.root), "--json"]
    assert main([*common, "run", identifier, "--mode", "offline-fixture", "--assess"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert len(data["assessments"]) == 1
    assert data["attempts"][0]["assessment_id"] == data["assessments"][0]
    assert data["pending_assessment"] == []
    assert main([*common, "run", "--resume", data["run_id"], "--assess"]) == 0
    resumed = json.loads(capsys.readouterr().out)["data"]
    assert resumed["assessments"] == data["assessments"]


def test_status_rejects_assessment(store, benchmark, capsys):
    result = run_experiment(
        store, create_experiment(store, benchmark), options=RunOptions(mode="offline-fixture")
    )
    assert capsys.readouterr().err == f"dryheave run reserved: {result.run_id}\n"
    assert (
        main(["--store", str(store.root), "--json", "run", "--status", result.run_id, "--assess"])
        == 2
    )
    assert "status cannot" in json.loads(capsys.readouterr().err)["error"]["message"]


@pytest.mark.parametrize(
    "failure",
    ["unstarted", "missing-capture", "capture-error", "input-error", "cleanup", "cancelled"],
)
def test_assess_guard_preserves_run_identity_without_spending(
    store, benchmark, capsys, monkeypatch, failure
):
    identifier = create_experiment(store, benchmark)
    summary = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    assert capsys.readouterr().err == f"dryheave run reserved: {summary.run_id}\n"
    capture = load_capture(store, summary.attempts[0].capture_id)
    if failure == "unstarted":
        summary = summary.model_copy(update={"unstarted_trials": ("pending-trial",)})
    elif failure == "input-error":
        summary = summary.model_copy(update={"input_error": "corrupt"})
    elif failure in {"missing-capture", "capture-error"}:
        state = summary.attempts[0].model_copy(
            update={"capture_id": None}
            if failure == "missing-capture"
            else {"capture_error": "failure"}
        )
        summary = summary.model_copy(update={"attempts": (state,)})
    elif failure == "cleanup":
        capture = capture.model_copy(
            update={"cleanup": capture.cleanup.model_copy(update={"errors": ("unresolved",)})}
        )

    def launch(*_args, **kwargs):
        if failure == "cancelled":
            kwargs["cancelled"].set()
        return summary

    def assess(*_args, **_kwargs):
        pytest.fail("Guarded incomplete execution must not start assessment.")

    monkeypatch.setattr("dryheave.runner_cli.run_experiment", launch)
    monkeypatch.setattr("dryheave.runner_cli.load_capture", lambda *_args: capture)
    monkeypatch.setattr("dryheave.runner_cli.assess_run", assess)
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "run",
                identifier,
                "--mode",
                "offline-fixture",
                "--assess",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)["error"]["message"]
    assert f"Run {summary.run_id} retains its evidence" in error
    assert f"dryheave assess {summary.run_id}" in error


@pytest.mark.parametrize("error", [InputError("synthetic assessment failure"), KeyboardInterrupt()])
def test_assessment_failure_keeps_run_id_and_recovery_command(
    store, benchmark, capsys, monkeypatch, error
):
    identifier = create_experiment(store, benchmark)
    summary = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    assert capsys.readouterr().err == f"dryheave run reserved: {summary.run_id}\n"
    monkeypatch.setattr("dryheave.runner_cli.run_experiment", lambda *_args, **_kwargs: summary)

    def assess(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("dryheave.runner_cli.assess_run", assess)
    assert (
        main(
            [
                "--store",
                str(store.root),
                "--json",
                "run",
                identifier,
                "--mode",
                "offline-fixture",
                "--assess",
            ]
        )
        == 2
    )
    response = json.loads(capsys.readouterr().err)
    assert f"run --resume {summary.run_id}" in response["error"]["message"]


def test_transport_resolution_precedence_is_explicit_then_config_then_path(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, WorkspaceConfig(tui_test="tools/tui-test"))
    monkeypatch.setattr("dryheave.workspaces.shutil.which", lambda _name: "/installed/tui-test")
    assert resolve_transport(Path("/explicit/tui-test"), workspace) == Path("/explicit/tui-test")
    assert resolve_transport(None, workspace) == tmp_path / "tools/tui-test"
    assert resolve_transport(None, Workspace(tmp_path, WorkspaceConfig())) == Path(
        "/installed/tui-test"
    )
    monkeypatch.setattr("dryheave.workspaces.shutil.which", lambda _name: None)
    assert resolve_transport(None, None) is None


def test_new_cli_run_freezes_transport_and_resume_does_not_resolve_defaults(
    store, benchmark, tmp_path, monkeypatch, capsys
):
    identifier = create_experiment(store, benchmark)
    summary = run_experiment(store, identifier, options=RunOptions(mode="offline-fixture"))
    runtime = (Path(".cache") / "rt").absolute()
    (tmp_path / "dryheave.toml").write_text(
        f"store = {json.dumps(str(store.root))}\nruntime = {json.dumps(str(runtime))}\ntui_test = 'tools/tui-test'\n"
    )
    seen = []

    def launch(*_args, **kwargs):
        seen.append(kwargs["options"])
        return summary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dryheave.runner_cli.run_experiment", launch)
    assert main(["run", identifier, "--json"]) == 0
    capsys.readouterr()
    assert seen[-1].transport == str(tmp_path / "tools/tui-test")
    assert main(["run", identifier, "--tui-test", "override", "--json"]) == 0
    capsys.readouterr()
    assert seen[-1].transport == str(tmp_path / "override")

    def resolve(*_args):
        pytest.fail("Resume must not discover or materialize new defaults.")

    monkeypatch.setattr("dryheave.runner_cli.resolve_transport", resolve)
    assert main(["run", "--resume", summary.run_id, "--json"]) == 0
    capsys.readouterr()
    assert seen[-1] is None
