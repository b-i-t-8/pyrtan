"""One member of the swarm: an HTTP service hosting a registry of trees.

Endpoints, all JSON:

``GET /health``
    ``{"status": "ok", "name": ..., "trees": [...], "errors": {...}}``
``GET /capabilities``
    What this service can do: every tree's name, triggers and inputs.
``POST /intent``   ``{"text": "..."}``
    Match a sentence to a tree and run it.
``POST /run``      ``{"tree": "...", "inputs": {...}}``
    Run a named tree.
``POST /resume``   ``{"snapshot": {...}, "answer": ...}``
    Continue a run that asked a question.
``GET /gaps``
    Decisions that met a value no branch covered, since the service started.
    Each is a branch waiting to be written.
``GET /unanswered``
    Sentences no tree answered, counted, each with a proposed tree. Each is
    a tree waiting to be written; ``pyrtan unanswered --at URL --scaffold
    DIR`` writes the files.
``GET /`` and ``GET /console``
    The console (:mod:`pyrtan.page`); ``GET /api`` is the JSON index.

A service may be given a hub (``--hub``). Then it registers itself on start,
and a ``delegate`` to a tree it does not hold is forwarded to the hub.

**Access.** With no token, everything is open: the swarm is assumed to sit
on a trusted network behind the operator's ingress. With a token
(``--token`` or ``$PYRTAN_TOKEN``), every request except ``GET /health``,
the console and ``/api`` must carry ``Authorization: Bearer <token>``;
``/health`` then answers with status and counts only. A swarm shares one
token: a service sends it to its hub when it registers and the hub sends it
to peers when it forwards. **CORS** is off unless ``--cors`` names the
origins (or ``*``) a browser may call from; the console at pyrtan.com is
one such origin. The server does not do TLS; put it behind a proxy that
does when it leaves the LAN.

The server is the standard library's threading HTTP server.
"""

from __future__ import annotations

import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .gaps import GapStore
from .page import PAGE
from .grow import Unanswered
from .registry import Registry
from .runtime import Outcome, Runner
from .trace import Trace
from .transport import Transport, UrllibTransport

MAX_BODY = 1 << 20

# What answers without a token: the console, its index, and liveness.
PUBLIC_PATHS = frozenset({"/", "/index.html", "/console", "/api", "/health"})
# What /health says to a caller without a token, when one is required.
PUBLIC_HEALTH_KEYS = ("status", "name", "peers", "up", "stale", "down", "uptime_s")


def http_json(method: str, url: str, payload: Any = None, timeout: float = 15.0, token: str | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "null")
        except Exception:
            return e.code, None


class RemoteResolver:
    """Sends what a local runner cannot answer to a hub or another service."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def run_tree(self, name: str, inputs: dict[str, Any], depth: int) -> Outcome:
        status, body = http_json("POST", f"{self.base_url}/run", {"tree": name, "inputs": inputs, "depth": depth}, token=self.token)
        return outcome_from_dict(body, name) if isinstance(body, dict) else Outcome("error", name, reason="remote", say=f"{self.base_url} answered {status}")

    def run_intent(self, text: str, depth: int) -> Outcome:
        status, body = http_json("POST", f"{self.base_url}/intent", {"text": text, "depth": depth}, token=self.token)
        return outcome_from_dict(body, "") if isinstance(body, dict) else Outcome("error", "", reason="remote", say=f"{self.base_url} answered {status}")


def outcome_from_dict(d: dict[str, Any], fallback_tree: str = "") -> Outcome:
    trace = None
    if isinstance(d.get("trace"), dict):
        from .trace import Step

        trace = Trace(d["trace"].get("tree", fallback_tree))
        trace.steps = [Step(**{k: v for k, v in s.items() if k in Step.__dataclass_fields__}) for s in d["trace"].get("steps", [])]
        trace.gaps = list(d["trace"].get("gaps", []))
    return Outcome(
        status=d.get("status", "error"),
        tree=d.get("tree") or fallback_tree,
        say=d.get("say"),
        output=d.get("output"),
        reason=d.get("reason"),
        question=d.get("question"),
        snapshot=d.get("snapshot"),
        trace=trace,
        inputs=d.get("inputs") or {},
    )


class Service:
    def __init__(
        self,
        registry: Registry,
        *,
        name: str = "pyrtan",
        transport: Transport | None = None,
        hub: str | None = None,
        env: dict[str, Any] | None = None,
        advertise: str | None = None,
        token: str | None = None,
        gaps: str | Path | GapStore | None = None,
        egress: str = "any",
    ) -> None:
        self.registry = registry
        self.name = name
        self.hub = hub.rstrip("/") if hub else None
        self.advertise = advertise
        self.token = token or None
        self.egress = egress
        self.started = time.time()
        self.runner = Runner(registry, transport or UrllibTransport(egress=egress), resolver=RemoteResolver(self.hub, self.token) if self.hub else None, env=env or {})
        self.gaps = gaps if isinstance(gaps, GapStore) else GapStore(gaps)
        self.unanswered = Unanswered()
        self.runs = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ handlers

    def health(self) -> dict[str, Any]:
        self.registry.reload()
        health = {"status": "ok", "name": self.name, "trees": self.registry.names(), "errors": self.registry.errors, "runs": self.runs, "gaps": len(self.gaps), "unanswered": len(self.unanswered), "egress": self.egress, "uptime_s": int(time.time() - self.started)}
        if self.gaps.error:
            health["gaps_file_error"] = self.gaps.error
        return health

    def capabilities(self) -> dict[str, Any]:
        self.registry.reload()
        return {"name": self.name, "trees": self.registry.capabilities()}

    def intent(self, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"status": "error", "reason": "bad-request", "say": "Send {\"text\": \"...\"}."}
        self.registry.reload()
        outcome = self.runner.run_intent(text, int(body.get("depth", 0)))
        if outcome.reason == "no-match" and self.hub is None:
            # With a hub, the hub saw the sentence and keeps the record.
            self.unanswered.record(text)
        return self._finish(outcome, body)

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        tree = body.get("tree")
        if not isinstance(tree, str):
            return {"status": "error", "reason": "bad-request", "say": "Send {\"tree\": \"...\", \"inputs\": {...}}."}
        self.registry.reload()
        return self._finish(self.runner.run_tree(tree, body.get("inputs") or {}, int(body.get("depth", 0))), body)

    def resume(self, body: dict[str, Any]) -> dict[str, Any]:
        snapshot = body.get("snapshot")
        if not isinstance(snapshot, dict) or "tree" not in snapshot:
            return {"status": "error", "reason": "bad-request", "say": "Send {\"snapshot\": {...}, \"answer\": ...}."}
        return self._finish(self.runner.resume(snapshot, body.get("answer")), body)

    def _finish(self, outcome: Outcome, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.runs += 1
        if outcome.trace is not None and outcome.trace.gaps:
            self.gaps.add(outcome.trace.gaps)
        return outcome.to_dict(with_trace=bool(body.get("trace", True)))

    # ------------------------------------------------------------ hub

    def register(self, my_url: str) -> bool:
        if not self.hub:
            return False
        try:
            status, _ = http_json("POST", f"{self.hub}/register", {"url": my_url, "name": self.name}, token=self.token)
            return status == 200
        except Exception:
            return False

    # ------------------------------------------------------------ routing

    def route(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        body = body or {}
        if method == "GET" and path == "/health":
            return 200, self.health()
        if method == "GET" and path == "/capabilities":
            return 200, self.capabilities()
        if method == "GET" and path == "/gaps":
            return 200, {"gaps": self.gaps.all()}
        if method == "GET" and path == "/unanswered":
            return 200, {"unanswered": self.unanswered.to_list()}
        if method == "GET" and path in ("/", "/index.html", "/console"):
            return 200, PAGE
        if method == "GET" and path == "/api":
            return 200, {"pyrtan": "service", "name": self.name, "version": __version__, "auth": "bearer" if self.token else "none", "endpoints": ["/health", "/capabilities", "/intent", "/run", "/resume", "/gaps", "/unanswered"]}
        if method == "POST" and path == "/intent":
            return 200, self.intent(body)
        if method == "POST" and path == "/run":
            return 200, self.run(body)
        if method == "POST" and path == "/resume":
            return 200, self.resume(body)
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


Route = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


def bearer_ok(header: str | None, token: str) -> bool:
    """Whether an ``Authorization`` header carries ``token`` (compared in constant time)."""
    if not header:
        return False
    scheme, _, value = header.strip().partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def make_handler(route: Route, quiet: bool = False, token: str | None = None, cors: list[str] | None = None):
    token = token or None
    origins = [o.rstrip("/") for o in (cors or []) if o]
    any_origin = "*" in origins

    class Handler(BaseHTTPRequestHandler):
        server_version = f"pyrtan/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:
            if not quiet:
                super().log_message(fmt, *args)

        # -------------------------------------------------------- cors

        def _origin_allowed(self) -> str | None:
            origin = self.headers.get("Origin")
            if not origin or not origins:
                return None
            return origin if any_origin or origin.rstrip("/") in origins else None

        def _cors_headers(self) -> None:
            origin = self._origin_allowed()
            if origin is None:
                return
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -------------------------------------------------------- auth

        def _authorised(self, path: str) -> bool:
            """True if the request may proceed; otherwise a 401 has been sent."""
            if token is None or path in PUBLIC_PATHS or bearer_ok(self.headers.get("Authorization"), token):
                return True
            self._send(401, {"status": "error", "reason": "unauthorised", "say": "This Pyrtan needs a token: send Authorization: Bearer <token>."}, extra={"WWW-Authenticate": 'Bearer realm="pyrtan"'})
            return False

        def _public_view(self, path: str, payload: Any) -> Any:
            """Trim /health for a caller with no token, when one is required."""
            if token is not None and path == "/health" and isinstance(payload, dict) and not bearer_ok(self.headers.get("Authorization"), token):
                return {k: v for k, v in payload.items() if k in PUBLIC_HEALTH_KEYS}
            return payload

        # -------------------------------------------------------- i/o

        def _send(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            if isinstance(payload, str):
                data = payload.encode("utf-8")
                ctype = "text/html; charset=utf-8"
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._cors_headers()
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the caller went away; nothing to do about it

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._authorised(path):
                return
            status, payload = route("GET", path, None)
            self._send(status, self._public_view(path, payload))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._authorised(path):
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"status": "error", "reason": "too-large"})
                return
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"status": "error", "reason": "bad-json"})
                return
            if not isinstance(body, dict):
                self._send(400, {"status": "error", "reason": "bad-json", "say": "The body must be a JSON object."})
                return
            status, payload = route("POST", path, body)
            self._send(status, payload)

    return Handler


class Server:
    """A running HTTP server around a route function, for a service or a hub.

    ``token`` requires ``Authorization: Bearer`` on everything but the public
    paths; ``cors`` lists the browser origins allowed to call (``*`` for any)."""

    def __init__(self, route: Route, host: str = "127.0.0.1", port: int = 0, quiet: bool = False, *, token: str | None = None, cors: list[str] | None = None) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(route, quiet, token, cors))
        self.httpd.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        host = self.httpd.server_address[0]
        return f"http://{host}:{self.port}"

    def start(self) -> "Server":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def wait(self) -> None:
        """Block until the server started with :meth:`start` stops."""
        if self.thread is None:
            self.serve_forever()
            return
        while self.thread.is_alive():
            self.thread.join(0.5)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def serve(
    paths: list[str | Path],
    host: str = "127.0.0.1",
    port: int = 8701,
    *,
    name: str = "pyrtan",
    hub: str | None = None,
    env: dict[str, Any] | None = None,
    advertise: str | None = None,
    quiet: bool = False,
    token: str | None = None,
    cors: list[str] | None = None,
    gaps: str | Path | None = None,
    egress: str = "any",
) -> tuple[Service, Server]:
    registry = Registry.from_paths(paths)
    service = Service(registry, name=name, hub=hub, env=env, advertise=advertise, token=token, gaps=gaps, egress=egress)
    server = Server(service.route, host, port, quiet, token=token, cors=cors).start()
    # The server is already answering when it registers: the hub checks a new
    # peer's /capabilities inside /register, and would otherwise wait on a
    # service that is itself waiting for the hub's answer.
    if hub:
        service.register(advertise or server.url)
    return service, server
