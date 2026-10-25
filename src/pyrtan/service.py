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
``GET /``
    The demo page (:mod:`pyrtan.page`); ``GET /api`` is the JSON index.

A service may be given a hub (``--hub``). Then it registers itself on start,
and a ``delegate`` to a tree it does not hold is forwarded to the hub.

The server is the standard library's threading HTTP server. It is meant for
a set of small processes on a trusted network, behind whatever ingress
the operator already runs; it does not do TLS or authentication itself.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .page import PAGE
from .grow import Unanswered
from .registry import Registry
from .runtime import Outcome, Runner
from .trace import Trace
from .transport import Transport, UrllibTransport

MAX_BODY = 1 << 20


def http_json(method: str, url: str, payload: Any = None, timeout: float = 15.0) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
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

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def run_tree(self, name: str, inputs: dict[str, Any], depth: int) -> Outcome:
        status, body = http_json("POST", f"{self.base_url}/run", {"tree": name, "inputs": inputs, "depth": depth})
        return outcome_from_dict(body, name) if isinstance(body, dict) else Outcome("error", name, reason="remote", say=f"{self.base_url} answered {status}")

    def run_intent(self, text: str, depth: int) -> Outcome:
        status, body = http_json("POST", f"{self.base_url}/intent", {"text": text, "depth": depth})
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
    def __init__(self, registry: Registry, *, name: str = "pyrtan", transport: Transport | None = None, hub: str | None = None, env: dict[str, Any] | None = None, advertise: str | None = None) -> None:
        self.registry = registry
        self.name = name
        self.hub = hub.rstrip("/") if hub else None
        self.advertise = advertise
        self.started = time.time()
        self.runner = Runner(registry, transport or UrllibTransport(), resolver=RemoteResolver(self.hub) if self.hub else None, env=env or {})
        self.gaps: list[dict[str, Any]] = []
        self.unanswered = Unanswered()
        self.runs = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ handlers

    def health(self) -> dict[str, Any]:
        self.registry.reload()
        return {"status": "ok", "name": self.name, "trees": self.registry.names(), "errors": self.registry.errors, "runs": self.runs, "gaps": len(self.gaps), "unanswered": len(self.unanswered), "uptime_s": int(time.time() - self.started)}

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
            if outcome.trace is not None:
                self.gaps.extend(outcome.trace.gaps)
        return outcome.to_dict(with_trace=bool(body.get("trace", True)))

    # ------------------------------------------------------------ hub

    def register(self, my_url: str) -> bool:
        if not self.hub:
            return False
        try:
            status, _ = http_json("POST", f"{self.hub}/register", {"url": my_url, "name": self.name})
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
            return 200, {"gaps": self.gaps}
        if method == "GET" and path == "/unanswered":
            return 200, {"unanswered": self.unanswered.to_list()}
        if method == "GET" and path in ("/", "/index.html"):
            return 200, PAGE
        if method == "GET" and path == "/api":
            return 200, {"pyrtan": "service", "name": self.name, "endpoints": ["/health", "/capabilities", "/intent", "/run", "/resume", "/gaps", "/unanswered"]}
        if method == "POST" and path == "/intent":
            return 200, self.intent(body)
        if method == "POST" and path == "/run":
            return 200, self.run(body)
        if method == "POST" and path == "/resume":
            return 200, self.resume(body)
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


def make_handler(route: Callable[[str, str, dict[str, Any] | None], tuple[int, Any]], quiet: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pyrtan/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            if not quiet:
                super().log_message(fmt, *args)

        def _send(self, status: int, payload: Any) -> None:
            if isinstance(payload, str):
                data = payload.encode("utf-8")
                ctype = "text/html; charset=utf-8"
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            status, payload = route("GET", self.path.split("?", 1)[0], None)
            self._send(status, payload)

        def do_POST(self) -> None:
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
            status, payload = route("POST", self.path.split("?", 1)[0], body)
            self._send(status, payload)

    return Handler


class Server:
    """A running HTTP server around a route function, for a service or a hub."""

    def __init__(self, route: Callable[[str, str, dict[str, Any] | None], tuple[int, Any]], host: str = "127.0.0.1", port: int = 0, quiet: bool = False) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(route, quiet))
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

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def serve(paths: list[str | Path], host: str = "127.0.0.1", port: int = 8701, *, name: str = "pyrtan", hub: str | None = None, env: dict[str, Any] | None = None, advertise: str | None = None, quiet: bool = False) -> tuple[Service, Server]:
    registry = Registry.from_paths(paths)
    service = Service(registry, name=name, hub=hub, env=env, advertise=advertise)
    server = Server(service.route, host, port, quiet)
    if hub:
        service.register(advertise or server.url)
    return service, server
