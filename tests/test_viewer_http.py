import base64
import http.client
import json
import re
import socket
import threading
import time
import tomllib
import zipfile
from contextlib import suppress
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from conftest import HOSTILE
from dryheave import journals, viewer_http
from dryheave.assessments import assess_run
from dryheave.calibrations import calibrate_case
from dryheave.constants import VERSION
from dryheave.errors import ConflictError
from dryheave.experiments import create_experiment
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore
from dryheave.viewer import ViewerService
from dryheave.viewer_http import ServerLimits, ViewerServer
from dryheave.viewer_models import ViewerTarget


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
def served(store, assessed):
    server = ViewerServer(ViewerService(store))
    server.start()
    yield server, assessed
    server.stop()


def _port(server: ViewerServer) -> int:
    port = urlsplit(server.url).port
    assert port is not None
    return port


def _get(server: ViewerServer, path: str, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", _port(server), timeout=5)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def _raw(server: ViewerServer, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", _port(server)), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def test_index_assets_and_security_headers(served):
    server, _ = served
    response, body = _get(server, "/")
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert int(response.getheader("Content-Length")) == len(body)
    assert body == files("dryheave").joinpath("resources/viewer/index.html").read_bytes()
    assert b"Dryheave results" in body
    policy = response.getheader("Content-Security-Policy")
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert response.getheader("Server") == f"dryheave/{VERSION}"
    assert response.getheader("Cross-Origin-Resource-Policy") == "same-origin"
    assert response.getheader("Connection") == "close"
    assert {name for name, _ in response.getheaders()} == {
        "Server",
        "Date",
        "Content-Type",
        "Content-Length",
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "Cache-Control",
        "Referrer-Policy",
        "Cross-Origin-Resource-Policy",
        "Connection",
    }
    assert response.getheader("Access-Control-Allow-Origin") is None
    for path, mime, asset in (
        ("/viewer.css", "text/css; charset=utf-8", "viewer.css"),
        ("/viewer.js", "text/javascript; charset=utf-8", "viewer.js"),
    ):
        response, body = _get(server, path)
        assert response.status == 200
        assert response.getheader("Content-Type") == mime
        assert body == files("dryheave").joinpath("resources/viewer", asset).read_bytes()
    for path in ("/missing", "/viewer2.css", "/../store", "/api/unknown", "/api"):
        response, body = _get(server, path)
        assert response.status == 404
        assert json.loads(body)["error"]["code"] == "not_found"


def test_api_runs_report_and_progress(served):
    server, summary = served
    response, body = _get(server, "/api/runs")
    assert response.status == 200
    listing = json.loads(body)
    assert listing["total"] == 1
    assert listing["runs"][0]["run_id"] == summary.run_id
    assert listing["runs"][0]["finished"] == 1
    response, body = _get(server, f"/api/runs/{summary.run_id}")
    report = json.loads(body)
    assert report["run_id"] == summary.run_id
    assert report["groups"][0]["eligible"] == 1
    response, body = _get(server, f"/api/runs/{summary.run_id}/progress")
    progress = json.loads(body)
    assert progress["attempts"][0]["stage"] == "finished"
    response, body = _get(server, "/api/target")
    assert json.loads(body) == {"target": None}
    response, body = _get(server, "/api/runs?limit=0")
    assert response.status == 400
    assert json.loads(body)["error"]["code"] == "invalid_input"
    response, body = _get(server, "/api/runs?offset=bogus")
    assert response.status == 400
    response, body = _get(server, f"/api/runs/{'0' * 32}")
    assert response.status == 404
    assert json.loads(body)["error"]["code"] == "not_found"


def test_labels_route_serves_the_alias_index_for_immutable_ids(served, store):
    server, assessed = served
    response, body = _get(server, "/api/labels")
    assert response.status == 200
    assert assessed.experiment_id not in json.loads(body)["labels"]
    store.set_alias("nightly", assessed.experiment_id)
    response, body = _get(server, "/api/labels")
    assert response.status == 200
    assert json.loads(body)["labels"][assessed.experiment_id] == ["nightly"]
    response, body = _get(server, "/api/labels/extra")
    assert response.status == 404
    assert json.loads(body)["error"]["code"] == "not_found"


def test_attempt_detail_routes_validate_paths(served):
    server, summary = served
    attempt_id = summary.attempts[0].attempt_id
    base = f"/api/runs/{summary.run_id}/attempts/{attempt_id}"
    response, body = _get(server, base + "/dialogue")
    assert response.status == 200
    dialogue = json.loads(body)
    assert dialogue["status"] == "ok"
    assert dialogue["messages"][-1]["text"] == "Done."
    response, body = _get(server, base + "/patch")
    assert json.loads(body)["status"] == "ok"
    response, body = _get(server, base + "/evidence")
    listing = json.loads(body)
    assert listing["status"] == "ok"
    assert listing["files"]
    response, body = _get(server, base + "/evidence/" + listing["files"][0])
    assert json.loads(body)["status"] == "ok"
    response, body = _get(server, base + "/evidence/..%2F..%2Fmanifest.json")
    assert response.status == 400
    assert json.loads(body)["error"]["code"] == "invalid_input"
    response, body = _get(server, base + "/evidence/checks/absent.json")
    assert response.status == 404
    response, body = _get(server, f"/api/runs/{summary.run_id}/attempts/a-unknown/dialogue")
    assert response.status == 404
    response, body = _get(server, base + "/terminal")
    assert response.status == 404


def test_host_origin_and_method_validation(served):
    server, _ = served
    response, body = _get(server, "/api/runs", headers={"Host": "evil.example"})
    assert response.status == 400
    assert json.loads(body)["error"]["code"] == "invalid_host"
    connection = http.client.HTTPConnection("127.0.0.1", _port(server), timeout=5)
    connection.putrequest("GET", "/api/runs", skip_host=True)
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 400
    connection.close()
    reply = _raw(
        server,
        (
            f"GET /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{_port(server)}\r\n"
            f"Host: 127.0.0.1:{_port(server)}\r\n\r\n"
        ).encode(),
    )
    assert reply.startswith(b"HTTP/1.1 400")
    response, body = _get(server, "/api/runs", headers={"Origin": "http://evil.example"})
    assert response.status == 403
    assert json.loads(body)["error"]["code"] == "invalid_origin"
    response, _ = _get(server, "/api/runs", headers={"Origin": server.url.rstrip("/")})
    assert response.status == 200
    connection = http.client.HTTPConnection("127.0.0.1", _port(server), timeout=5)
    connection.request("POST", "/api/runs", body=b"{}")
    response = connection.getresponse()
    body = response.read()
    assert response.status == 405
    assert response.getheader("Allow") == "GET"
    assert json.loads(body)["error"]["code"] == "method_not_allowed"
    connection.request("DELETE", "/api/runs")
    assert connection.getresponse().status == 405
    connection.close()
    reply = _raw(
        server,
        f"GET http://127.0.0.1:{_port(server)}/api/runs HTTP/1.1\r\nHost: 127.0.0.1:{_port(server)}\r\n\r\n".encode(),
    )
    assert reply.startswith(b"HTTP/1.1 400")


def test_lifecycle_port_reuse_and_clean_shutdown(store, assessed):
    service = ViewerService(store, target=ViewerTarget(kind="run", id=assessed.run_id))
    server = ViewerServer(service)
    with pytest.raises(ConflictError, match="not running"):
        server.stop()
    server.start()
    port = _port(server)
    response, body = _get(server, "/api/target")
    assert json.loads(body) == {"target": {"kind": "run", "id": assessed.run_id}}
    server.stop()
    with pytest.raises((ConnectionRefusedError, OSError)):
        socket.create_connection(("127.0.0.1", port), timeout=2)
    replacement = ViewerServer(service)
    replacement.start()
    response, body = _get(replacement, "/api/runs")
    assert response.status == 200
    assert json.loads(body)["total"] == 1
    replacement.stop()


def test_concurrent_requests_are_bounded(store, assessed, monkeypatch):
    service = ViewerService(store)
    server = ViewerServer(service, limits=ServerLimits(max_concurrent=2, slot_seconds=10))
    running = 0
    peak = 0
    lock = threading.Lock()
    original = service.runs

    def tracked(*, offset: int = 0, limit: int = 25):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            time.sleep(0.3)
            return original(offset=offset, limit=limit)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(service, "runs", tracked)
    server.start()
    try:
        outcomes = []
        threads = [
            threading.Thread(target=lambda: outcomes.append(_get(server, "/api/runs")[0].status))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert peak == 2
        assert outcomes == [200] * 6
    finally:
        server.stop()


def test_accepted_socket_times_out_when_idle(store, assessed):
    server = ViewerServer(ViewerService(store), limits=ServerLimits(socket_timeout=0.2))
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", _port(server)), timeout=5) as sock:
            received = b"x"
            deadline = time.monotonic() + 5
            while received and time.monotonic() < deadline:
                received = sock.recv(1024)
            assert received == b""
    finally:
        server.stop()


def test_oversized_responses_fail_explicitly(store, assessed, monkeypatch):
    monkeypatch.setattr(viewer_http, "MAX_RESPONSE_BYTES", 10)
    server = ViewerServer(ViewerService(store))
    server.start()
    try:
        response, body = _get(server, "/api/runs")
        assert response.status == 500
        assert json.loads(body)["error"]["code"] == "response_too_large"
    finally:
        server.stop()


def test_malformed_request_versions_are_refused_with_full_headers(served):
    server, _ = served
    host = f"127.0.0.1:{_port(server)}"
    for request in (
        f"GET /api/runs\r\nHost: {host}\r\n\r\n".encode(),
        f"GET /api/runs BOGUS\r\nHost: {host}\r\n\r\n".encode(),
        f"GET /api/runs HTTP/2.0\r\nHost: {host}\r\n\r\n".encode(),
    ):
        reply = _raw(server, request)
        head, _, body = reply.partition(b"\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 400")
        assert b"X-Content-Type-Options: nosniff" in head
        assert b"Cache-Control: no-store" in head
        assert b"Referrer-Policy: no-referrer" in head
        assert b"frame-ancestors 'none'" in head
        assert json.loads(body)["error"]["code"] == "invalid_version"


def test_head_sends_headers_without_a_body(served):
    server, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", _port(server), timeout=5)
    connection.request("HEAD", "/api/runs")
    response = connection.getresponse()
    body = response.read()
    assert response.status == 405
    assert response.getheader("Allow") == "GET"
    assert int(response.getheader("Content-Length")) > 0
    assert body == b""
    connection.close()


def test_unknown_methods_are_validated_before_they_are_refused(served):
    server, _ = served
    host = f"127.0.0.1:{_port(server)}"
    reply = _raw(server, f"FROB /api/runs HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    head, _, body = reply.partition(b"\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 501")
    assert b"X-Content-Type-Options: nosniff" in head
    assert json.loads(body)["error"]["code"] == "http_error"
    reply = _raw(server, b"FROB /api/runs HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 400")
    assert json.loads(reply.partition(b"\r\n\r\n")[2])["error"]["code"] == "invalid_host"


def test_error_messages_never_disclose_store_paths(store, assessed, monkeypatch):
    monkeypatch.setattr(journals, "MAX_JOURNAL_BYTES", 1)
    server = ViewerServer(ViewerService(store))
    server.start()
    try:
        response, body = _get(server, f"/api/runs/{assessed.run_id}/progress")
        assert response.status == 400
        envelope = json.loads(body)["error"]
        assert envelope["code"] == "limit_exceeded"
        assert "1-byte limit" in envelope["message"]
        assert str(store.root) not in envelope["message"]
        assert "<path>" in envelope["message"]
    finally:
        server.stop()


def test_host_authority_must_match_exactly(served):
    server, _ = served
    host = f"127.0.0.1:{_port(server)}"
    for header in (
        "127.0.0.1:1",
        f"localhost:{_port(server)}",
        "127.0.0.1",
        f"[::1]:{_port(server)}",
    ):
        response, body = _get(server, "/api/runs", headers={"Host": header})
        assert response.status == 400
        assert json.loads(body)["error"]["code"] == "invalid_host"
    response, body = _get(server, "/api/runs", headers={"Host": host})
    assert response.status == 200
    assert json.loads(body)["total"] == 1


def test_duplicate_and_null_origins_are_rejected(served):
    server, _ = served
    host = f"127.0.0.1:{_port(server)}"
    reply = _raw(
        server,
        f"GET /api/runs HTTP/1.1\r\nHost: {host}\r\nOrigin: http://{host}\r\n"
        f"Origin: http://{host}\r\n\r\n".encode(),
    )
    assert reply.startswith(b"HTTP/1.1 403")
    assert json.loads(reply.partition(b"\r\n\r\n")[2])["error"]["code"] == "invalid_origin"
    response, body = _get(server, "/api/runs", headers={"Origin": "null"})
    assert response.status == 403
    assert json.loads(body)["error"]["code"] == "invalid_origin"
    response, body = _get(server, "/api/runs", headers={"Origin": f"http://{host}"})
    assert response.status == 200
    assert json.loads(body)["total"] == 1


def test_percent_encoded_paths_never_reach_assets_or_the_filesystem(served):
    server, _ = served
    for path in (
        "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/viewer%2Ecss",
        "/%2e%2e/%2e%2e/aliases.json",
        "/..%2f..%2fstore",
    ):
        response, body = _get(server, path)
        assert response.status == 404
        assert json.loads(body)["error"]["code"] == "not_found"
    response, body = _get(server, "/viewer.css")
    assert response.status == 200
    assert body == files("dryheave").joinpath("resources/viewer/viewer.css").read_bytes()


def test_get_with_a_body_is_answered_once_and_closed(served):
    server, _ = served
    host = f"127.0.0.1:{_port(server)}"
    reply = _raw(
        server,
        f"GET /api/runs HTTP/1.1\r\nHost: {host}\r\nContent-Length: 5\r\n\r\nhello".encode(),
    )
    head, _, body = reply.partition(b"\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 200")
    assert head.count(b"HTTP/1.1") == 1
    assert b"Connection: close" in head
    assert json.loads(body)["total"] == 1


def test_compare_route_serves_typed_comparisons(served):
    server, summary = served
    run = summary.run_id
    response, body = _get(server, f"/api/compare?before={run}&after={run}")
    assert response.status == 200
    comparison = json.loads(body)
    assert comparison["paired_count"] == 1
    assert comparison["before_variant"] is None
    response, body = _get(
        server, f"/api/compare?before={run}&after={run}&before_variant=&after_variant="
    )
    assert response.status == 200
    assert json.loads(body)["after_variant"] is None
    response, body = _get(
        server, f"/api/compare?before={run}&after={run}&before_variant=base&after_variant=base"
    )
    assert response.status == 200
    assert json.loads(body)["before_variant"] == "base"
    response, body = _get(server, f"/api/compare?before={run}")
    assert response.status == 400
    assert json.loads(body)["error"]["code"] == "invalid_input"
    response, body = _get(server, f"/api/compare?before={run}&after={run}&before_variant=base")
    assert response.status == 400
    assert "both comparison variants" in json.loads(body)["error"]["message"]


def test_calibration_routes_serve_run_and_case_scopes(store, graded_benchmark):
    case_id = store.resolve(graded_benchmark.cases[0])
    calibration_id = calibrate_case(store, case_id)
    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    server = ViewerServer(ViewerService(store))
    server.start()
    try:
        response, body = _get(server, f"/api/runs/{summary.run_id}/calibrations")
        assert response.status == 200
        run_view = json.loads(body)
        assert [item["calibration_id"] for item in run_view["calibrations"][case_id]] == [
            calibration_id
        ]
        assert run_view["scan"]["complete"] is True
        assert run_view["scan"]["next_cursor"] is None
        response, body = _get(server, f"/api/cases/{case_id}/calibrations")
        assert response.status == 200
        case_view = json.loads(body)
        assert case_view["case_id"] == case_id
        assert [item["calibration_id"] for item in case_view["calibrations"]] == [calibration_id]
        assert case_view["calibrations"][0]["verification"] == "verified"
        response, body = _get(server, f"/api/cases/{case_id}/calibrations?limit=1")
        page = json.loads(body)
        assert page["scan"]["scanned"] == 1
        assert page["scan"]["complete"] is False
        assert len(page["scan"]["next_cursor"]) == 64
        response, body = _get(server, f"/api/cases/{case_id}/calibrations?limit=0")
        assert response.status == 400
        assert json.loads(body)["error"]["code"] == "invalid_input"
        response, body = _get(server, f"/api/cases/{'0' * 64}/calibrations")
        assert response.status == 200
        assert json.loads(body)["calibrations"] == []
        response, body = _get(server, "/api/cases/not-a-case/calibrations")
        assert response.status == 404
    finally:
        server.stop()


def test_hostile_transcript_bytes_round_trip_verbatim_over_http(store, hostile_run):
    run_id, attempt_id = hostile_run
    server = ViewerServer(ViewerService(store))
    server.start()
    try:
        base = f"/api/runs/{run_id}/attempts/{attempt_id}"
        response, body = _get(server, base + "/dialogue")
        assert response.status == 200
        assert response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert json.dumps(HOSTILE, ensure_ascii=False).encode() in body
        assert b"<script>alert(1)</script><img src=x onerror=alert(1)>" in body
        dialogue = json.loads(body)
        assert [item["text"] for item in dialogue["messages"][:2]] == [HOSTILE, HOSTILE]
        response, body = _get(server, base + "/patch")
        assert response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert HOSTILE in json.loads(body)["content"]
        response, body = _get(server, base + "/evidence")
        name = next(
            item
            for item in json.loads(body)["files"]
            if item.endswith("final/greeting-evidence/stdout.bin")
        )
        response, body = _get(server, base + "/evidence/" + name)
        served = json.loads(body)
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert served["encoding"] == "base64"
        raw = base64.b64decode(served["content"])
        assert HOSTILE.encode() in raw
        assert b"\xed\xa0\x80" in raw
        assert served["bytes"] == len(raw)
    finally:
        server.stop()


def test_saturated_handler_slots_answer_503_with_security_headers(store, assessed, monkeypatch):
    service = ViewerService(store)
    entered = threading.Event()
    release = threading.Event()
    original = service.runs

    def blocking(*, offset: int = 0, limit: int = 25):
        entered.set()
        release.wait(10)
        return original(offset=offset, limit=limit)

    monkeypatch.setattr(service, "runs", blocking)
    server = ViewerServer(service, limits=ServerLimits(max_concurrent=1, slot_seconds=0.05))
    server.start()
    holder = threading.Thread(target=lambda: _get(server, "/api/runs"))
    holder.start()
    try:
        assert entered.wait(10)
        host = f"127.0.0.1:{_port(server)}"
        reply = _raw(server, f"GET /api/runs HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        head, _, body = reply.partition(b"\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 503 Service Unavailable")
        assert b"Content-Type: application/json; charset=utf-8" in head
        assert b"X-Content-Type-Options: nosniff" in head
        assert b"Cache-Control: no-store" in head
        assert b"Referrer-Policy: no-referrer" in head
        assert b"Cross-Origin-Resource-Policy: same-origin" in head
        assert b"frame-ancestors 'none'" in head
        assert f"Server: dryheave/{VERSION}".encode() in head
        assert int(re.search(rb"Content-Length: (\d+)", head).group(1)) == len(body)
        assert json.loads(body)["error"]["code"] == "busy"
    finally:
        release.set()
        holder.join(timeout=15)
        server.stop()


def test_connection_cap_refuses_without_blocking_the_accept_loop(store, assessed):
    server = ViewerServer(
        ViewerService(store),
        limits=ServerLimits(max_connections=1, slot_seconds=30, socket_timeout=10),
    )
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", _port(server)), timeout=5):
            deadline = time.monotonic() + 10
            while server._httpd._accepted < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server._httpd._accepted == 1
            host = f"127.0.0.1:{_port(server)}"
            started = time.monotonic()
            reply = _raw(server, f"GET /api/runs HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            elapsed = time.monotonic() - started
            assert reply.startswith(b"HTTP/1.1 503 Service Unavailable")
            assert json.loads(reply.partition(b"\r\n\r\n")[2])["error"]["code"] == "busy"
            assert elapsed < 5
    finally:
        server.stop()


def test_a_trickling_refused_client_does_not_stall_the_accept_loop(store, assessed):
    server = ViewerServer(
        ViewerService(store),
        limits=ServerLimits(
            max_connections=1, socket_timeout=20, drain_seconds=0.5, join_seconds=5
        ),
    )
    server.start()
    host = f"127.0.0.1:{_port(server)}"
    stopped = threading.Event()
    try:
        with socket.create_connection(("127.0.0.1", _port(server)), timeout=5):
            deadline = time.monotonic() + 10
            while server._httpd._accepted < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server._httpd._accepted == 1
            with socket.create_connection(("127.0.0.1", _port(server)), timeout=10) as trickle:

                def feed():
                    with suppress(OSError):
                        while not stopped.wait(0.05):
                            trickle.sendall(b"A")

                feeder = threading.Thread(target=feed)
                feeder.start()
                try:
                    assert trickle.recv(65536).startswith(b"HTTP/1.1 503 Service Unavailable")
                    started = time.monotonic()
                    reply = _raw(server, f"GET /api/runs HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                    elapsed = time.monotonic() - started
                    assert reply.startswith(b"HTTP/1.1 503 Service Unavailable")
                    assert json.loads(reply.partition(b"\r\n\r\n")[2])["error"]["code"] == "busy"
                    assert elapsed < 3
                finally:
                    stopped.set()
                    feeder.join(timeout=10)
    finally:
        server.stop()


def test_error_messages_scrub_store_paths_holding_spaces_and_quotes(tmp_path, monkeypatch):
    root = tmp_path / "My Bench's cases" / "store"
    runs = journals.RunStore(root)
    run_id = runs.create("a" * 64)
    with runs.open(run_id) as writer:
        writer.append("observed", {"stage": "working"})
    monkeypatch.setattr(journals, "MAX_JOURNAL_BYTES", 1)
    server = ViewerServer(ViewerService(ObjectStore(root)))
    server.start()
    try:
        response, body = _get(server, f"/api/runs/{run_id}/progress")
        assert response.status == 400
        envelope = json.loads(body)["error"]
        assert envelope["code"] == "limit_exceeded"
        assert envelope["message"] == "File exceeds the 1-byte limit: <path>"
    finally:
        server.stop()


def test_serve_reports_handlers_that_outlive_the_shutdown_timeout(store, assessed, monkeypatch):
    service = ViewerService(store)
    entered = threading.Event()
    release = threading.Event()
    original = service.runs

    def blocking(*, offset: int = 0, limit: int = 25):
        entered.set()
        release.wait(20)
        return original(offset=offset, limit=limit)

    monkeypatch.setattr(service, "runs", blocking)
    server = ViewerServer(service, limits=ServerLimits(join_seconds=0.1))
    port = _port(server)
    refused: list[ConflictError] = []

    def foreground():
        try:
            server.serve()
        except ConflictError as error:
            refused.append(error)

    served = threading.Thread(target=foreground)
    served.start()
    holder = threading.Thread(target=lambda: _get(server, "/api/runs"))
    holder.start()
    try:
        assert entered.wait(10)
        server._httpd.shutdown()
        served.join(timeout=10)
        assert not served.is_alive()
        assert [str(error) for error in refused] == [
            "Viewer server did not stop within its shutdown timeout."
        ]
        with pytest.raises((ConnectionRefusedError, OSError)):
            socket.create_connection(("127.0.0.1", port), timeout=2)
    finally:
        release.set()
        holder.join(timeout=25)


def test_stop_reports_handlers_that_outlive_the_shutdown_timeout(store, assessed, monkeypatch):
    service = ViewerService(store)
    entered = threading.Event()
    release = threading.Event()
    original = service.runs

    def blocking(*, offset: int = 0, limit: int = 25):
        entered.set()
        release.wait(10)
        return original(offset=offset, limit=limit)

    monkeypatch.setattr(service, "runs", blocking)
    server = ViewerServer(service, limits=ServerLimits(join_seconds=0.1))
    server.start()
    port = _port(server)
    holder = threading.Thread(target=lambda: _get(server, "/api/runs"))
    holder.start()
    try:
        assert entered.wait(10)
        with pytest.raises(ConflictError, match="did not stop"):
            server.stop()
        with pytest.raises((ConnectionRefusedError, OSError)):
            socket.create_connection(("127.0.0.1", port), timeout=2)
    finally:
        release.set()
        holder.join(timeout=15)


def test_a_full_slot_table_does_not_stall_the_accept_loop(store, assessed, monkeypatch):
    service = ViewerService(store)
    entered = threading.Event()
    release = threading.Event()
    original = service.runs

    def blocking(*, offset: int = 0, limit: int = 25):
        entered.set()
        release.wait(20)
        return original(offset=offset, limit=limit)

    monkeypatch.setattr(service, "runs", blocking)
    server = ViewerServer(
        service, limits=ServerLimits(max_concurrent=1, slot_seconds=30, join_seconds=0.1)
    )
    server.start()
    host = f"127.0.0.1:{_port(server)}"
    holder = threading.Thread(target=lambda: _get(server, "/api/runs"))
    holder.start()
    try:
        assert entered.wait(10)
        assert server._httpd._accepted == 1
        waiting = socket.create_connection(("127.0.0.1", _port(server)), timeout=5)
        try:
            waiting.sendall(f"GET /api/runs HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            deadline = time.monotonic() + 10
            while server._httpd._accepted < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server._httpd._accepted == 2
            started = time.monotonic()
            with pytest.raises(ConflictError, match="did not stop"):
                server.stop()
            assert time.monotonic() - started < 5
        finally:
            waiting.close()
    finally:
        release.set()
        holder.join(timeout=25)


def _viewer_root():
    return files("dryheave").joinpath("resources", "viewer")


def test_every_packaged_viewer_asset_is_allowlisted_and_served(served):
    server, _ = served
    root = _viewer_root()
    packaged = {item.name for item in root.iterdir() if item.is_file()}
    assert packaged == {name for _, name, _ in viewer_http.VIEWER_ASSETS}
    for route, name, mime in viewer_http.VIEWER_ASSETS:
        expected = root.joinpath(name).read_bytes()
        assert expected
        response, body = _get(server, route)
        assert response.status == 200
        assert response.getheader("Content-Type") == mime
        assert int(response.getheader("Content-Length")) == len(expected)
        assert body == expected


def test_viewer_page_and_modules_only_reference_allowlisted_routes():
    root = _viewer_root()
    routes = {route for route, _, _ in viewer_http.VIEWER_ASSETS}
    page = root.joinpath("index.html").read_text(encoding="utf-8")
    referenced = {
        value for value in re.findall(r'(?:href|src)="([^"]+)"', page) if not value.startswith("#")
    }
    assert referenced == {"/viewer.css", "/viewer.js"}
    assert referenced <= routes
    imported = set()
    for _, name, mime in viewer_http.VIEWER_ASSETS:
        if mime != viewer_http.SCRIPT_MIME:
            continue
        source = root.joinpath(name).read_text(encoding="utf-8")
        imported |= {
            f"/{target.removeprefix('./')}" for target in re.findall(r'from\s+"([^"]+)"', source)
        }
    assert imported
    assert imported <= routes


_INJECTION_SINKS = (
    r"\binnerHTML\b",
    r"\bouterHTML\b",
    r"\binsertAdjacentHTML\b",
    r"\bdocument\s*\.\s*write\b",
    r"\beval\s*\(",
    r"\bsrcdoc\b",
    r"\bnew\s+Function\b",
    r"\bFunction\s*\(",
    r"\bcreateContextualFragment\b",
    r"\bsetAttribute\s*\(\s*[\"\'`]?\s*on",
    r"\.(?:href|src|action|formAction|outerText)\s*=[^=]",
    r"(?<![\w-])(?:href|src|srcdoc|formaction|background|xlink:href)[\"\'`]?\s*:",
    r"\bjavascript\s*:",
    r"\bdata\s*:\s*text/html",
)


def _viewer_scripts():
    root = _viewer_root()
    for _, name, mime in viewer_http.VIEWER_ASSETS:
        if mime == viewer_http.SCRIPT_MIME:
            yield name, root.joinpath(name).read_text(encoding="utf-8")


def test_viewer_modules_never_use_html_injection_sinks():
    scripts = dict(_viewer_scripts())
    assert scripts
    for name, source in scripts.items():
        joined = re.sub(r"[\"\'`]\s*\+\s*[\"\'`]", "", source)
        for pattern in _INJECTION_SINKS:
            assert not re.search(pattern, source), (name, pattern)
            assert not re.search(pattern, joined), (name, pattern)


def test_the_injection_sink_lint_rejects_every_sink_it_claims_to_cover():
    samples = (
        "node.innerHTML = value;",
        "node.outerHTML = value;",
        "node.insertAdjacentHTML('beforeend', value);",
        "document.write(value);",
        "eval(value);",
        "frame.srcdoc = value;",
        "const run = new Function(value);",
        "const run = Function(value);",
        "range.createContextualFragment(value);",
        'node.setAttribute("onclick", value);',
        "node.href = value;",
        "link.src = value;",
        'el("a", { attrs: { href: value } });',
        'node.setAttribute(name, "javascript:" + value);',
        'node.setAttribute(name, "data:text/html," + value);',
        'node["inner" + "HTML"] = value;',
    )
    for sample in samples:
        joined = re.sub(r"[\"\'`]\s*\+\s*[\"\'`]", "", sample)
        assert any(
            re.search(pattern, sample) or re.search(pattern, joined) for pattern in _INJECTION_SINKS
        ), sample


def test_wheel_packaging_configuration_carries_every_viewer_asset():
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert set(wheel) == {"packages"}
    assert wheel["packages"] == ["src/dryheave"]
    for _, name, _ in viewer_http.VIEWER_ASSETS:
        packaged = project / "src" / "dryheave" / "resources" / "viewer" / name
        assert packaged.is_file()
        assert packaged.read_bytes() == _viewer_root().joinpath(name).read_bytes()


@pytest.mark.packaging
def test_built_wheel_contains_every_viewer_asset():
    wheels = sorted((Path(__file__).resolve().parents[1] / "dist").glob("*.whl"))
    assert wheels, "run just build before the packaging tests"
    newest = max(wheels, key=lambda path: path.stat().st_mtime)
    with zipfile.ZipFile(newest) as archive:
        members = set(archive.namelist())
        for _, name, _ in viewer_http.VIEWER_ASSETS:
            member = f"dryheave/resources/viewer/{name}"
            assert member in members
            assert archive.read(member) == _viewer_root().joinpath(name).read_bytes()


def test_hidden_panels_are_not_re_shown_by_a_display_rule() -> None:
    assets = Path(viewer_http.__file__).parent / "resources/viewer"
    markup = (assets / "index.html").read_text()
    stylesheet = (assets / "viewer.css").read_text()
    hidden_classes = {
        name
        for element in re.findall(r"<section[^>]*\bhidden\b[^>]*>", markup)
        for name in re.findall(r'class="([^"]+)"', element)
        for name in name.split()
    }
    assert hidden_classes
    styled = {
        name
        for name in hidden_classes
        if re.search(rf"\.{re.escape(name)}\b[^{{]*{{[^}}]*\bdisplay\s*:", stylesheet)
    }
    assert styled
    override = re.search(r"\[hidden\][^{]*{[^}]*display\s*:\s*none\s*!important", stylesheet)
    assert override is not None, (
        f"classes {sorted(styled)} set display on elements carrying the hidden attribute; "
        "without a !important [hidden] override every tab panel stays visible at once"
    )
