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
``GET /suggestions`` and ``POST /suggestions`` ``{"suggestions": [...]}``
    What agents beside the swarm suggest (:mod:`pyrtan.suggestions`): a
    branch's meaning, a trigger, a tree. Kept, never applied;
    ``pyrtan grow --suggestions`` writes them, by rules, for a person.
``POST /build``    ``{"text": "...", "answers": {...}}``
    The builder (:mod:`pyrtan.build`): the questions that apply to the
    sentence given the answers so far, each with its default, and the
    tree file those answers describe, checked. Nothing is written here;
    the console offers the file to save.
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
import ipaddress
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .gaps import GapStore
from .page import MANIFEST, PYRTAN_JS, SERVICE_WORKER, default_page
from .build import BuildError, build
from .grow import Unanswered
from .suggestions import Suggestions
from .registry import Registry
from .runtime import Outcome, Runner
from .trace import Trace
from .tree import Tree
from .watch import Watcher
from .adapters import LocalAdapters
from .transport import Transport, UrllibTransport, _EgressRedirectHandler, check_egress

MAX_BODY = 1 << 20
# The most a hub reads of another's answer: a directory, a blob or an actor,
# never a stream.
MAX_RESPONSE = 4 << 20
# The egress policy for the hub's own requests to hosts it did not choose:
# followed hubs and their followers, the hub list, identity documents.
# ``serve`` and ``serve_hub`` set it from --egress, the same policy their
# trees get, so a hub told to stay public makes no request into its LAN on
# anyone's behalf (a Follow names the actor to ask). The operator's own
# swarm (``--hub``, ``--peer``, a service that registered with the token)
# is called under ``any``: it usually is the LAN. Every redirect is checked.
_EGRESS = "any"


def set_egress(policy: str) -> None:
    """The egress policy ``http_json`` applies when its caller names none."""
    global _EGRESS
    check_egress("http://example.com/", policy)  # a bad policy name is refused here, not at the first request
    _EGRESS = policy

# What answers without a token: the console, its index, liveness, and a pushed
# identity document (which proves itself by its signature).
PUBLIC_PATHS = frozenset({"/", "/index.html", "/console", "/api", "/health", "/actor", "/directory", "/reports", "/manifest.webmanifest", "/sw.js", "/pyrtan.js", "/identity"})


def public_path(path: str) -> bool:
    """What answers without a token: the paths above, and ``/.well-known/`` (identity documents, signed)."""
    return path in PUBLIC_PATHS or path.startswith("/.well-known/")
# On a federated hub, a request carrying a signature reaches the route,
# which verifies it (see :mod:`pyrtan.federation`).
SIGNED_PATHS = frozenset({"/inbox", "/run", "/intent", "/resume", "/capabilities", "/mailbox"})


def signed_path(path: str) -> bool:
    """Whether a signed (federated) request may reach this path at all."""
    return path in SIGNED_PATHS or path.startswith("/kept/")
# Headers a browser at another origin may send: federation's HMAC and an identity's key.
SIGNATURE_HEADERS = ("X-Pyrtan-Actor", "X-Pyrtan-Date", "X-Pyrtan-Signature", "X-Pyrtan-Key", "X-Pyrtan-Key-Signature", "X-Pyrtan-Device", "X-Pyrtan-Audience")
# What /health says to a caller without a token, when one is required.
PUBLIC_HEALTH_KEYS = ("status", "name", "peers", "up", "stale", "down", "uptime_s")


def http_json(method: str, url: str, payload: Any = None, timeout: float = 15.0, token: str | None = None, headers: dict[str, str] | None = None, *, egress: str | None = None) -> tuple[int, Any]:
    """A JSON request on the hub's own behalf, under the egress policy
    (``egress``, else the one ``set_egress`` chose), on the first URL and on
    every redirect; an answer over ``MAX_RESPONSE`` bytes is an error."""
    policy = egress or _EGRESS
    check_egress(url, policy)  # EgressRefused, before any connection
    # Canonical JSON on the wire, so a signature over the body (federation)
    # can be checked from the parsed body on the other side.
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    opener = urllib.request.build_opener(_EgressRedirectHandler(policy))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(_bounded(resp, url).decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(_bounded(e, url).decode("utf-8") or "null")
        except Exception:
            return e.code, None


def _bounded(resp: Any, url: str) -> bytes:
    raw = resp.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise urllib.error.URLError(f"{url} answered with more than {MAX_RESPONSE} bytes")
    return raw


class RemoteResolver:
    """Sends what a local runner cannot answer to a hub or another service."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def run_tree(self, name: str, inputs: dict[str, Any], depth: int) -> Outcome:
        status, body = http_json("POST", f"{self.base_url}/run", {"tree": name, "inputs": inputs, "depth": depth}, token=self.token, egress="any")
        return outcome_from_dict(body, name) if isinstance(body, dict) else Outcome("error", name, reason="remote", say=f"{self.base_url} answered {status}")

    def run_intent(self, text: str, depth: int) -> Outcome:
        status, body = http_json("POST", f"{self.base_url}/intent", {"text": text, "depth": depth}, token=self.token, egress="any")
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
        unanswered: str | Path | None = None,
        watch: bool = False,
        suggestions: str | Path | None = None,
        memory: Any = None,
    ) -> None:
        self.registry = registry
        self.name = name
        self.hub = hub.rstrip("/") if hub else None
        self.advertise = advertise
        self.token = token or None
        self.egress = egress
        self.started = time.time()
        adapters = transport if transport is not None and hasattr(transport, "perform") else LocalAdapters(egress=egress, memory=memory)
        self.runner = Runner(registry, transport or UrllibTransport(egress=egress), resolver=RemoteResolver(self.hub, self.token) if self.hub else None, env=env or {}, adapters=adapters)
        self.gaps = gaps if isinstance(gaps, GapStore) else GapStore(gaps)
        self.unanswered = Unanswered(path=unanswered)
        self.suggestions = Suggestions(path=suggestions)
        self.runs = 0
        self._lock = threading.Lock()
        self.watcher: Watcher | None = None
        if watch:
            self.watcher = Watcher(registry, self.run_watched, env=env or {}, egress=egress, after=None, note=lambda text: print(f"pyrtan: {text}", file=sys.stderr)).start()

    def run_watched(self, tree: Tree, inputs: dict[str, Any], trace: Trace) -> Outcome:
        """How a watch runs a tree: through the same runner and the same
        bookkeeping as a request, with the watch step already on the trace."""
        outcome = self.runner.run(tree, inputs, trace=trace)
        self._finish(outcome, {})
        return outcome

    def stop(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()

    # ------------------------------------------------------------ handlers

    def health(self) -> dict[str, Any]:
        self.registry.reload()
        health = {"status": "ok", "name": self.name, "trees": self.registry.names(), "errors": self.registry.errors, "runs": self.runs, "gaps": len(self.gaps), "unanswered": len(self.unanswered), "suggestions": len(self.suggestions), "egress": self.egress, "uptime_s": int(time.time() - self.started), "watches": len(self.watcher) if self.watcher else 0}
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

    def build(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return build_answer(body)

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
            status, _ = http_json("POST", f"{self.hub}/register", {"url": my_url, "name": self.name}, token=self.token, egress="any")  # the operator named the hub
            return status == 200
        except Exception:
            return False

    # ------------------------------------------------------------ routing

    def route(self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        body = body or {}
        if method == "GET" and path == "/health":
            return 200, self.health()
        if method == "GET" and path == "/capabilities":
            return 200, self.capabilities()
        if method == "GET" and path == "/gaps":
            return 200, {"gaps": self.gaps.all()}
        if method == "GET" and path == "/unanswered":
            return 200, {"unanswered": self.unanswered.to_list()}
        if method in ("GET", "POST") and path == "/suggestions":
            return self.suggestions.route(method, body)
        if method == "GET" and path == "/watches":
            return 200, self.watcher.status() if self.watcher else {"count": 0, "watches": [], "watching": False}
        if method == "GET" and path in ("/", "/index.html", "/console"):
            return 200, default_page()
        if method == "GET" and path == "/manifest.webmanifest":
            return 200, ("application/manifest+json", MANIFEST)
        if method == "GET" and path == "/sw.js":
            return 200, ("text/javascript; charset=utf-8", SERVICE_WORKER)
        if method == "GET" and path == "/pyrtan.js":
            return 200, ("text/javascript; charset=utf-8", PYRTAN_JS)
        if method == "GET" and path == "/api":
            return 200, {"pyrtan": "service", "name": self.name, "version": __version__, "auth": "bearer" if self.token else "none", "endpoints": ["/health", "/capabilities", "/intent", "/run", "/resume", "/gaps", "/unanswered", "/build", "/suggestions", "/watches"]}
        if method == "POST" and path == "/intent":
            return 200, self.intent(body)
        if method == "POST" and path == "/run":
            return 200, self.run(body)
        if method == "POST" and path == "/resume":
            return 200, self.resume(body)
        if method == "POST" and path == "/build":
            return self.build(body)
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


Route = Callable[..., tuple[int, Any]]  # (method, path, body, headers) -> (status, payload)
PEER_HEADER = "X-Pyrtan-Peer"  # the caller's address, set here and never taken from the request


def peer_address(client: str, headers: Any) -> str:
    """Where a request came from: the socket's address, or, when that is this
    machine (a proxy in front, for TLS), the last address the proxy put in
    ``X-Forwarded-For``. Only a proxy on this machine is believed."""
    try:
        local = ipaddress.ip_address(client).is_loopback
    except ValueError:
        local = False
    forwarded = (headers.get("X-Forwarded-For") or "").split(",")[-1].strip() if local else ""
    return forwarded or client


def _with_peer(headers: Any, client: str) -> dict[str, str]:
    out = {k: v for k, v in headers.items() if k.lower() != PEER_HEADER.lower()}
    out[PEER_HEADER] = peer_address(client, headers)
    return out


def bearer_ok(header: str | None, token: str) -> bool:
    """Whether an ``Authorization`` header carries ``token`` (compared in constant time).
    No token means nothing matches: an empty ``Bearer`` is not the operator of an open hub."""
    if not header or not token:
        return False
    scheme, _, value = header.strip().partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def make_handler(route: Route, quiet: bool = False, token: str | None = None, cors: list[str] | None = None, federated: bool = False, public: Callable[[str], bool] | None = None, keyed: bool = False):
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
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, " + ", ".join(SIGNATURE_HEADERS))
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
            if token is None or public_path(path) or bearer_ok(self.headers.get("Authorization"), token) or (public is not None and public(path)):
                return True
            if federated and signed_path(path) and self.headers.get("X-Pyrtan-Signature"):
                return True  # the route verifies the signature
            if keyed and self.headers.get("X-Pyrtan-Key-Signature"):
                return True  # the route verifies the key and asks its grants
            self._send(401, {"status": "error", "reason": "unauthorised", "say": "This Pyrtan needs a token: send Authorization: Bearer <token>."}, extra={"WWW-Authenticate": 'Bearer realm="pyrtan"'})
            return False

        def _public_view(self, path: str, payload: Any) -> Any:
            """Trim /health for a caller with no token, when one is required."""
            if token is not None and path == "/health" and isinstance(payload, dict) and not bearer_ok(self.headers.get("Authorization"), token):
                return {k: v for k, v in payload.items() if k in PUBLIC_HEALTH_KEYS}
            return payload

        # -------------------------------------------------------- i/o

        def _send(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], str):
                ctype, text = payload  # (content type, text): the manifest, the service worker
                data = text.encode("utf-8")
            elif isinstance(payload, str):
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
            status, payload = route("GET", path, None, _with_peer(self.headers, self.client_address[0]))
            self._send(status, self._public_view(path, payload))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._authorised(path):
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"status": "error", "reason": "too-large"})
                return
            if length and (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
                # A page on another site may POST text/plain with no preflight; only JSON is read,
                # so a hub with no token cannot be driven by a form or a simple request.
                self._send(415, {"status": "error", "reason": "not-json", "say": "Send the body as Content-Type: application/json."})
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
            try:
                json.dumps(body, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError:
                # "\ud800" is valid JSON and no text: signatures and logs are over UTF-8.
                self._send(400, {"status": "error", "reason": "bad-json", "say": "The body holds a lone surrogate (\\ud800-\\udfff), which is not text."})
                return
            status, payload = route("POST", path, body, _with_peer(self.headers, self.client_address[0]))
            self._send(status, payload)

    return Handler


class Server:
    """A running HTTP server around a route function, for a service or a hub.

    ``token`` requires ``Authorization: Bearer`` on everything but the public
    paths, and any path ``public`` says yes to (a hub's open lobby); ``cors``
    lists the browser origins allowed to call (``*`` for any)."""

    def __init__(self, route: Route, host: str = "127.0.0.1", port: int = 0, quiet: bool = False, *, token: str | None = None, cors: list[str] | None = None, federated: bool = False, public: Callable[[str], bool] | None = None, keyed: bool = False) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(route, quiet, token, cors, federated, public, keyed))
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


def build_answer(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``POST /build``: the builder's questions and the file for a sentence
    and the answers so far. A bad answer comes back as ``bad-answer`` with
    the question's key, so a form can point at it."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"text\": \"...\", \"answers\": {...}}."}
    answers = body.get("answers") or {}
    if not isinstance(answers, dict):
        return 400, {"status": "error", "reason": "bad-request", "say": "answers is a table of question key to answer."}
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return 400, {"status": "error", "reason": "bad-request", "say": "name is a string."}
    try:
        built = build(text, answers, name or None)
    except BuildError as e:
        return 200, {"status": "error", "reason": "bad-answer", "key": e.key, "say": str(e)}
    return 200, {"status": "ok", **built.to_dict()}


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
    unanswered: str | Path | None = None,
    watch: bool = False,
    suggestions: str | Path | None = None,
    memory: str | Path | None = None,
) -> tuple[Service, Server]:
    registry = Registry.from_paths(paths)
    set_egress(egress)  # the service's own requests (its hub) under the same policy as its trees
    service = Service(registry, name=name, hub=hub, env=env, advertise=advertise, token=token, gaps=gaps, egress=egress, unanswered=unanswered, watch=watch, suggestions=suggestions, memory=memory)
    server = Server(service.route, host, port, quiet, token=token, cors=cors).start()
    # The server is already answering when it registers: the hub checks a new
    # peer's /capabilities inside /register, and would otherwise wait on a
    # service that is itself waiting for the hub's answer.
    if hub:
        service.register(advertise or server.url)
    return service, server
