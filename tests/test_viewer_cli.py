import json
import socket
import subprocess
from urllib.parse import urlsplit

import pytest

from dryheave import viewer_cli
from dryheave.assessments import assess_run
from dryheave.bundles import export_bundle, import_bundle
from dryheave.cli import main
from dryheave.errors import InputError, NotFoundError
from dryheave.experiments import create_experiment
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore
from dryheave.viewer import ViewerService, resolve_target
from dryheave.viewer_http import ViewerServer

STUB_URL = "http://127.0.0.1:65500/"


@pytest.fixture
def assessed(store, graded_benchmark):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    assess_run(store, summary.run_id)
    return summary


@pytest.fixture
def stub_server(monkeypatch):
    record = {"served": 0, "services": []}

    class _Server:
        def __init__(self, service):
            record["services"].append(service)

        @property
        def url(self):
            return STUB_URL

        def serve(self):
            record["served"] += 1

    monkeypatch.setattr(viewer_cli, "ViewerServer", _Server)
    return record


def _view(store, *arguments):
    return main(["--store", str(store.root), "--json", "view", *arguments])


def test_view_prints_the_url_before_it_opens_a_browser(
    store, assessed, stub_server, monkeypatch, capsys
):
    opened = []
    monkeypatch.setattr(
        viewer_cli.webbrowser, "open", lambda url: opened.append((url, capsys.readouterr().err))
    )
    assert _view(store, assessed.run_id, "--open") == 0
    assert opened == [(STUB_URL, f"Serving {STUB_URL} (read-only; Ctrl-C to stop).\n")]
    assert stub_server["served"] == 1
    data = json.loads(capsys.readouterr().out)["data"]
    assert data == {"url": STUB_URL, "target": {"kind": "run", "id": assessed.run_id}}


def test_view_opens_a_browser_only_when_requested(
    store, assessed, stub_server, monkeypatch, capsys
):
    opened = []
    monkeypatch.setattr(viewer_cli.webbrowser, "open", opened.append)
    assert _view(store) == 0
    output = capsys.readouterr()
    assert opened == []
    assert output.err == f"Serving {STUB_URL} (read-only; Ctrl-C to stop).\n"
    assert json.loads(output.out)["data"] == {"url": STUB_URL, "target": None}
    assert stub_server["served"] == 1
    assert isinstance(stub_server["services"][0], ViewerService)
    assert stub_server["services"][0].target is None


def test_resolve_target_accepts_runs_reports_and_aliases(store, assessed, tmp_path):
    target = viewer_cli.resolve_target(store, assessed.run_id)
    assert (target.kind, target.id) == ("run", assessed.run_id)
    bundle = tmp_path / "summary.tar"
    export_bundle(store, assessed.run_id, bundle)
    imported = ObjectStore(tmp_path / "imported")
    report_id = import_bundle(imported, bundle).roots[0]
    report_target = resolve_target(imported, report_id)
    assert (report_target.kind, report_target.id) == ("report", report_id)
    imported.set_alias("latest", report_id)
    alias_target = resolve_target(imported, "latest")
    assert (alias_target.kind, alias_target.id) == ("report", report_id)


def test_resolve_target_rejects_references_that_are_not_reports(store, assessed):
    with pytest.raises(InputError, match="Expected portable-report object"):
        resolve_target(store, assessed.experiment_id)
    store.set_alias("experiment", assessed.experiment_id)
    with pytest.raises(InputError, match="Expected portable-report object"):
        resolve_target(store, "experiment")
    with pytest.raises(NotFoundError, match="Alias does not exist"):
        resolve_target(store, "absent")
    with pytest.raises(NotFoundError, match="Run does not exist"):
        resolve_target(store, "0" * 32)


def test_view_reports_a_missing_reference_without_serving(store, assessed, stub_server, capsys):
    assert _view(store, "0" * 32) == 1
    assert stub_server["served"] == 0
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


def test_serve_returns_on_keyboard_interrupt_and_closes_the_socket(store, monkeypatch):
    server = ViewerServer(ViewerService(store))
    port = urlsplit(server.url).port

    def interrupted(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(server._httpd, "serve_forever", interrupted)
    server.serve()
    with pytest.raises((ConnectionRefusedError, OSError)):
        socket.create_connection(("127.0.0.1", port), timeout=2)


def test_view_never_triggers_assessment_or_model_calls(
    store, graded_benchmark, stub_server, monkeypatch, capsys
):
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    capsys.readouterr()

    def refuse(*_args, **_kwargs):
        raise AssertionError("Viewing must never launch a process.")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(viewer_cli.webbrowser, "open", refuse)
    monkeypatch.setattr("dryheave.assessments.assess_run", refuse)
    monkeypatch.setattr("dryheave.assessments._assess_one", refuse)
    monkeypatch.setattr("dryheave.grading.grade_criterion", refuse)
    assert _view(store, summary.run_id) == 0
    assert stub_server["served"] == 1
    capsys.readouterr()
    service = ViewerService(store)
    report = service.run_report(summary.run_id)
    assert report.attempts[0].result is None
    assert report.attempts[0].stage == "captured"
    assert service.dialogue("run", summary.run_id, report.attempts[0].attempt_id).status == "ok"
    assert service.evidence("run", summary.run_id, report.attempts[0].attempt_id).status == (
        "unavailable"
    )
