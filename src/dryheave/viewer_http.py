import json
import re
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, cast
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import JsonValue

from dryheave.constants import VERSION
from dryheave.errors import ConflictError, DryheaveError, InputError, NotFoundError
from dryheave.models import StrictModel
from dryheave.viewer import ViewerService

JSON_MIME = "application/json; charset=utf-8"
MAX_CONCURRENT_REQUESTS = 16
MAX_OPEN_CONNECTIONS = 64
HANDLER_SLOT_SECONDS = 10.0
SOCKET_TIMEOUT_SECONDS = 30.0
SHUTDOWN_JOIN_SECONDS = 10.0
REFUSAL_DRAIN_SECONDS = 1.0
MAX_DRAIN_BYTES = 1024 * 1024
LISTEN_BACKLOG = 32
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
METHOD_NOT_ALLOWED = 405
NOT_IMPLEMENTED = 501
SERVICE_UNAVAILABLE = 503
SUPPORTED_VERSION = re.compile(r"HTTP/1\.[01]")
ABSOLUTE_PATH = re.compile(r"(?<![\w/])(?:/[^\s'\"/]*(?:[ '\"][^\s'\"/]+)*)+")

SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)

_SOURCE = r"(runs/([0-9a-f]{32})|reports/([0-9a-f]{64}))"
_ATTEMPT = r"/attempts/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})"
_DETAIL_ROUTE = re.compile("^/api/" + _SOURCE + _ATTEMPT + r"/(dialogue|patch|evidence)$")
_EVIDENCE_ROUTE = re.compile("^/api/" + _SOURCE + _ATTEMPT + r"/evidence/(.+)$")
_REPORT_ROUTE = re.compile(r"^/api/reports/([0-9a-f]{64})$")
_RUN_ROUTE = re.compile(r"^/api/runs/([0-9a-f]{32})$")
_PROGRESS_ROUTE = re.compile(r"^/api/runs/([0-9a-f]{32})/progress$")
_CALIBRATIONS_ROUTE = re.compile(r"^/api/runs/([0-9a-f]{32})/calibrations$")
_CASE_CALIBRATIONS_ROUTE = re.compile(r"^/api/cases/([0-9a-f]{64})/calibrations$")

type RequestType = socket.socket | tuple[bytes, socket.socket]


@dataclass(frozen=True)
class ServerLimits:
    max_concurrent: int = MAX_CONCURRENT_REQUESTS
    max_connections: int = MAX_OPEN_CONNECTIONS
    slot_seconds: float = HANDLER_SLOT_SECONDS
    socket_timeout: float = SOCKET_TIMEOUT_SECONDS
    join_seconds: float = SHUTDOWN_JOIN_SECONDS
    drain_seconds: float = REFUSAL_DRAIN_SECONDS


SCRIPT_MIME = "text/javascript; charset=utf-8"
VIEWER_ASSETS = (
    ("/", "index.html", "text/html; charset=utf-8"),
    ("/viewer.css", "viewer.css", "text/css; charset=utf-8"),
    ("/viewer.js", "viewer.js", SCRIPT_MIME),
    ("/api.mjs", "api.mjs", SCRIPT_MIME),
    ("/dom.mjs", "dom.mjs", SCRIPT_MIME),
    ("/filters.mjs", "filters.mjs", SCRIPT_MIME),
    ("/format.mjs", "format.mjs", SCRIPT_MIME),
    ("/views.mjs", "views.mjs", SCRIPT_MIME),
)


def _assets() -> dict[str, tuple[bytes, str]]:
    root = files("dryheave").joinpath("resources", "viewer")
    return {route: (root.joinpath(name).read_bytes(), mime) for route, name, mime in VIEWER_ASSETS}


def _json(payload: JsonValue) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _error(code: str, message: str) -> bytes:
    return _json({"error": {"code": code, "message": ABSOLUTE_PATH.sub("<path>", message)}})


def _bounded_json(payload: JsonValue, limit: int) -> bytes | None:
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False)
    chunks: list[bytes] = []
    total = 0
    for piece in encoder.iterencode(payload):
        encoded = piece.encode("utf-8")
        total += len(encoded)
        if total > limit:
            return None
        chunks.append(encoded)
    return b"".join(chunks)


def _busy() -> bytes:
    body = _error("busy", "The viewer is already serving its maximum number of requests.")
    head = [
        f"HTTP/1.1 {SERVICE_UNAVAILABLE} Service Unavailable",
        f"Server: dryheave/{VERSION}",
        f"Date: {formatdate(usegmt=True)}",
        f"Content-Type: {JSON_MIME}",
        f"Content-Length: {len(body)}",
        *(f"{name}: {value}" for name, value in SECURITY_HEADERS),
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(head).encode("utf-8") + body


def _integer(params: dict[str, str], name: str, default: int) -> int:
    raw = params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise InputError(f"Query parameter {name} must be an integer.") from error


def _optional_integer(params: dict[str, str], name: str) -> int | None:
    return None if params.get(name) in {None, ""} else _integer(params, name, 0)


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(
        self,
        service: ViewerService,
        server_address: tuple[str, int],
        *,
        limits: ServerLimits,
    ) -> None:
        self.service = service
        self.assets = _assets()
        self._slots = threading.BoundedSemaphore(limits.max_concurrent)
        self._limits = limits
        self._handlers: set[threading.Thread] = set()
        self._accepted = 0
        self._handler_lock = threading.Lock()
        super().__init__(server_address, _ViewerHandler)
        host, port = self.server_address[0], self.server_address[1]
        self.authority = f"{host.decode() if isinstance(host, bytes) else host}:{port}"
        self.origin = f"http://{self.authority}"

    def get_request(self) -> tuple[socket.socket, Any]:
        request, address = super().get_request()
        request.settimeout(self._limits.socket_timeout)
        return request, address

    def _drain(self, request: socket.socket) -> None:
        deadline = time.monotonic() + self._limits.drain_seconds
        drained = 0
        while drained < MAX_DRAIN_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            request.settimeout(remaining)
            chunk = request.recv(65536)
            if not chunk:
                return
            drained += len(chunk)

    def _refuse(self, request: RequestType) -> None:
        if isinstance(request, socket.socket):
            with suppress(OSError):
                request.sendall(_busy())
                request.shutdown(socket.SHUT_WR)
                self._drain(request)
        self.shutdown_request(request)

    def process_request(self, request: RequestType, client_address: Any) -> None:
        with self._handler_lock:
            saturated = self._accepted >= self._limits.max_connections
            self._accepted += 0 if saturated else 1
        if saturated:
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._handler_lock:
                self._accepted -= 1
            raise

    def process_request_thread(self, request: RequestType, client_address: Any) -> None:
        thread = threading.current_thread()
        with self._handler_lock:
            self._handlers.add(thread)
        try:
            if not self._slots.acquire(timeout=self._limits.slot_seconds):
                self._refuse(request)
                return
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()
        finally:
            with self._handler_lock:
                self._handlers.discard(thread)
                self._accepted -= 1

    def join_handlers(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._handler_lock:
                running = next((item for item in self._handlers if item.is_alive()), None)
                accepted = self._accepted
            if running is None and accepted == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if running is None:
                time.sleep(min(0.01, remaining))
            else:
                running.join(timeout=remaining)


class _ViewerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dryheave"
    sys_version = ""

    def version_string(self) -> str:
        return f"dryheave/{VERSION}"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (TimeoutError, OSError):
            self.close_connection = True

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if self._refused_version():
            return
        if code == NOT_IMPLEMENTED and not self._authorized():
            return
        text = self.responses.get(code, ("Error",))[0]
        self._respond(code, _error("http_error", f"{code} {text}"))

    def do_GET(self) -> None:
        if self._refused_version() or not self._authorized():
            return
        try:
            status, body, content_type = self._route()
        except NotFoundError as error:
            status, body, content_type = 404, _error("not_found", str(error)), JSON_MIME
        except InputError as error:
            status, body, content_type = 400, _error(error.code, str(error)), JSON_MIME
        except DryheaveError as error:
            status, body, content_type = 500, _error(error.code, str(error)), JSON_MIME
        except (OSError, ValueError, KeyError):
            status, body, content_type = (
                500,
                _error("viewer_error", "The viewer failed to serve this request."),
                JSON_MIME,
            )
        self._respond(status, body, content_type)

    def _reject_method(self) -> None:
        if self._refused_version() or not self._authorized():
            return
        self._respond(
            METHOD_NOT_ALLOWED,
            _error("method_not_allowed", "The viewer is read-only; use GET."),
        )

    do_HEAD = _reject_method
    do_POST = _reject_method
    do_PUT = _reject_method
    do_DELETE = _reject_method
    do_PATCH = _reject_method
    do_OPTIONS = _reject_method
    do_TRACE = _reject_method
    do_CONNECT = _reject_method

    def _refused_version(self) -> bool:
        if SUPPORTED_VERSION.fullmatch(self.request_version):
            return False
        self.request_version = "HTTP/1.1"
        self._respond(400, _error("invalid_version", "Requests must use HTTP/1.0 or HTTP/1.1."))
        return True

    def _authorized(self) -> bool:
        server = cast(_BoundedHTTPServer, self.server)
        if self.headers.get_all("Host") != [server.authority]:
            self._respond(
                400,
                _error("invalid_host", "Requests require exactly the viewer loopback Host."),
            )
            return False
        origins = self.headers.get_all("Origin")
        if origins is not None and origins != [server.origin]:
            self._respond(403, _error("invalid_origin", "Origin must match the viewer origin."))
            return False
        return True

    def _route(self) -> tuple[int, bytes, str]:
        server = cast(_BoundedHTTPServer, self.server)
        target = urlsplit(self.path)
        if target.scheme or target.netloc:
            raise InputError("Absolute request targets are not accepted.")
        asset = server.assets.get(target.path)
        if asset is not None:
            return 200, asset[0], asset[1]
        payload = self._api(server.service, target.path, dict(parse_qsl(target.query)))
        body = _bounded_json(payload.model_dump(mode="json"), MAX_RESPONSE_BYTES)
        if body is None:
            return (
                500,
                _error("response_too_large", "Viewer response exceeds its byte limit."),
                JSON_MIME,
            )
        return 200, body, JSON_MIME

    def _api(self, service: ViewerService, path: str, params: dict[str, str]) -> StrictModel:
        if path == "/api/target":
            return service.session()
        if path == "/api/labels":
            return service.labels()
        if path == "/api/runs":
            return service.runs(
                offset=_integer(params, "offset", 0), limit=_integer(params, "limit", 25)
            )
        if path == "/api/compare":
            before, after = params.get("before"), params.get("after")
            if not before or not after:
                raise InputError("Comparison requires before and after references.")
            return service.compare(
                before,
                after,
                before_variant=params.get("before_variant") or None,
                after_variant=params.get("after_variant") or None,
            )
        return self._api_object(service, path, params)

    def _api_object(self, service: ViewerService, path: str, params: dict[str, str]) -> StrictModel:
        if match := _PROGRESS_ROUTE.fullmatch(path):
            return service.progress(match.group(1))
        if match := _CALIBRATIONS_ROUTE.fullmatch(path):
            return service.calibrations(
                match.group(1),
                after=params.get("after") or None,
                limit=_optional_integer(params, "limit"),
            )
        if match := _CASE_CALIBRATIONS_ROUTE.fullmatch(path):
            return service.case_calibrations(
                match.group(1),
                after=params.get("after") or None,
                limit=_optional_integer(params, "limit"),
            )
        if match := _RUN_ROUTE.fullmatch(path):
            return service.run_report(match.group(1))
        if match := _REPORT_ROUTE.fullmatch(path):
            return service.portable_report(match.group(1))
        if match := _EVIDENCE_ROUTE.fullmatch(path):
            kind, source = ("run", match.group(2)) if match.group(2) else ("report", match.group(3))
            return service.evidence_file(kind, source, match.group(4), unquote(match.group(5)))
        if match := _DETAIL_ROUTE.fullmatch(path):
            kind, source = ("run", match.group(2)) if match.group(2) else ("report", match.group(3))
            return self._detail(service, kind, source, match.group(4), match.group(5))
        raise NotFoundError("Unknown viewer route.")

    def _detail(
        self, service: ViewerService, kind: str, source: str, attempt_id: str, detail: str
    ) -> StrictModel:
        if detail == "dialogue":
            return service.dialogue(kind, source, attempt_id)
        if detail == "patch":
            return service.patch(kind, source, attempt_id)
        return service.evidence(kind, source, attempt_id)

    def _respond(self, status: int, body: bytes, content_type: str = JSON_MIME) -> None:
        self.close_connection = True
        if not SUPPORTED_VERSION.fullmatch(self.request_version):
            self.request_version = "HTTP/1.1"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        if status == METHOD_NOT_ALLOWED:
            self.send_header("Allow", "GET")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class ViewerServer:
    def __init__(self, service: ViewerService, *, limits: ServerLimits | None = None) -> None:
        self._limits = limits or ServerLimits()
        self._httpd = _BoundedHTTPServer(service, ("127.0.0.1", 0), limits=self._limits)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return self._httpd.origin + "/"

    def serve(self) -> None:
        try:
            self._httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self._httpd.server_close()
            joined = self._httpd.join_handlers(self._limits.join_seconds)
        if not joined:
            raise ConflictError("Viewer server did not stop within its shutdown timeout.")

    def start(self) -> None:
        if self._thread is not None:
            raise ConflictError("Viewer server is already running.")
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            raise ConflictError("Viewer server is not running in the background.")
        thread, self._thread = self._thread, None
        self._httpd.shutdown()
        thread.join(timeout=self._limits.join_seconds)
        self._httpd.server_close()
        if thread.is_alive() or not self._httpd.join_handlers(self._limits.join_seconds):
            raise ConflictError("Viewer server did not stop within its shutdown timeout.")
