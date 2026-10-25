"""The hub: where a swarm's services find each other.

A hub holds no trees. It keeps a list of peers, polls each one's
``/capabilities`` and ``/health``, and answers the same ``/intent`` and
``/run`` a service does by forwarding to the peer that holds the matching
tree. Each peer carries a ``link`` of ``up``, ``stale`` or ``down``: the one
field an operator reads to know whether the swarm is whole.

Endpoints:

``POST /register``   ``{"url": "http://host:port", "name": "..."}``
``GET  /peers``      every peer with its link and trees
``GET  /capabilities`` the union of every peer's trees
``GET  /health``     ``{"status": "ok", "peers": n, "up": n, "trees": n}``
``POST /intent``     forwarded to the peer whose tree matches best
``POST /run``        forwarded to the peer holding ``tree``
``POST /resume``     forwarded to the peer named in the snapshot
``GET  /gaps``       every peer's gaps, with the peer that saw them
``GET  /unanswered`` sentences no peer answered, each with a proposed tree
``GET  /``           the console; ``GET /api`` is the JSON index

A service that holds several trees and a hub that routes between services
answer the same requests, so a client need not know which it is talking to,
and a swarm can start as one process and split later without the callers
changing. A hub given a token requires it of callers (``/register``
included, so a stranger cannot join the swarm) and presents it to peers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .grow import Unanswered, no_match_outcome
import datetime as _dt

from .intent import Trigger, read
from .page import PAGE
from .service import Server, http_json

STALE_AFTER = 30.0
DOWN_AFTER = 90.0


@dataclass
class Peer:
    url: str
    name: str = ""
    trees: list[dict[str, Any]] = field(default_factory=list)
    last_ok: float = 0.0
    last_error: str | None = None
    registered: float = field(default_factory=time.time)

    @property
    def link(self) -> str:
        if self.last_ok == 0.0:
            return "down" if self.last_error else "pending"
        age = time.time() - self.last_ok
        if age < STALE_AFTER:
            return "up"
        if age < DOWN_AFTER:
            return "stale"
        return "down"

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "name": self.name, "link": self.link, "trees": [t["name"] for t in self.trees], "last_ok": self.last_ok, "last_error": self.last_error}


class Hub:
    def __init__(self, *, name: str = "hub", poll_every: float = 10.0, token: str | None = None) -> None:
        self.name = name
        self.poll_every = poll_every
        self.token = token or None
        self.peers: dict[str, Peer] = {}
        self.unanswered = Unanswered()
        self.started = time.time()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ peers

    def add_peer(self, url: str, name: str = "") -> Peer:
        url = url.rstrip("/")
        with self._lock:
            peer = self.peers.get(url)
            if peer is None:
                peer = Peer(url, name)
                self.peers[url] = peer
            elif name:
                peer.name = name
        self.refresh(peer)
        return peer

    def refresh(self, peer: Peer) -> None:
        try:
            status, caps = http_json("GET", f"{peer.url}/capabilities", timeout=5.0, token=self.token)
        except Exception as e:  # urllib raises URLError subclasses of OSError
            status, caps = 0, None
            peer.last_error = str(e)
        if status == 200 and isinstance(caps, dict):
            with self._lock:
                peer.trees = list(caps.get("trees", []))
                peer.name = caps.get("name") or peer.name
                peer.last_ok = time.time()
                peer.last_error = None
        elif status:
            peer.last_error = f"capabilities answered {status}"

    def refresh_all(self) -> None:
        for peer in list(self.peers.values()):
            self.refresh(peer)

    def start_polling(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.poll_every):
                self.refresh_all()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ lookup

    def candidates(self) -> list[tuple[tuple[str, str], Trigger]]:
        out: list[tuple[tuple[str, str], Trigger]] = []
        with self._lock:
            for peer in self.peers.values():
                if peer.link == "down":
                    continue
                for tree in peer.trees:
                    for pattern in tree.get("triggers", []):
                        try:
                            out.append(((peer.url, tree["name"]), Trigger(pattern, language=tree.get("language") or {})))
                        except Exception:
                            continue
        return out

    def holder(self, tree_name: str) -> Peer | None:
        with self._lock:
            best: Peer | None = None
            for peer in self.peers.values():
                if any(t.get("name") == tree_name for t in peer.trees):
                    if best is None or _rank(peer) < _rank(best):
                        best = peer
            return best

    # ------------------------------------------------------------ handlers

    def health(self) -> dict[str, Any]:
        with self._lock:
            peers = list(self.peers.values())
        return {
            "status": "ok",
            "name": self.name,
            "peers": len(peers),
            "up": sum(1 for p in peers if p.link == "up"),
            "stale": sum(1 for p in peers if p.link == "stale"),
            "down": sum(1 for p in peers if p.link == "down"),
            "trees": sum(len(p.trees) for p in peers),
            "unanswered": len(self.unanswered),
            "uptime_s": int(time.time() - self.started),
        }

    def capabilities(self) -> dict[str, Any]:
        trees: list[dict[str, Any]] = []
        with self._lock:
            for peer in self.peers.values():
                for t in peer.trees:
                    trees.append({**t, "peer": peer.url, "link": peer.link})
        return {"name": self.name, "trees": trees}

    def intent(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 200, {"status": "error", "reason": "bad-request", "say": "Send {\"text\": \"...\"}."}
        reading = read(text, self.candidates(), _dt.datetime.now(_dt.timezone.utc))
        matches = reading.matches
        if not matches:
            entry = self.unanswered.record(text)
            answer = no_match_outcome(text, "No service in the swarm")
            answer["output"]["seen"] = entry["count"]
            answer["output"]["reading"] = {"cleaned": reading.cleaned, "steps": reading.steps}
            return 200, {"status": "error", "reason": "no-match", **answer}
        tree_names = {m.tree[1] for m in matches}  # type: ignore[index]
        if len(tree_names) > 1:
            return 200, {"status": "error", "reason": "ambiguous", "say": f"{text!r} matches more than one tree: {sorted(tree_names)}", "output": {"candidates": sorted(tree_names), "reading": {"cleaned": reading.cleaned, "steps": reading.steps}}}
        peer_url, tree_name = matches[0].tree  # type: ignore[misc]
        for m in matches:
            m.tree = tree_name  # the step names the tree, not the (peer, tree) pair
        peer = self.holder(tree_name) or self.peers.get(peer_url)
        status, answer = self.forward(peer, "/run", {"tree": tree_name, "inputs": matches[0].inputs, "depth": body.get("depth", 0), "trace": body.get("trace", True)})
        if isinstance(answer.get("trace"), dict) and isinstance(answer["trace"].get("steps"), list):
            # The hub read the sentence; the peer only ran the tree. Put the
            # reading where a caller expects it, at the top of the trace.
            answer["trace"]["steps"].insert(0, {**reading.step(), "next": answer["trace"]["steps"][0]["node"] if answer["trace"]["steps"] else None, "elapsed_ms": 0, "depth": 0})
        return status, answer

    def run(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tree = body.get("tree")
        if not isinstance(tree, str):
            return 200, {"status": "error", "reason": "bad-request", "say": "Send {\"tree\": \"...\", \"inputs\": {...}}."}
        peer = self.holder(tree)
        if peer is None:
            return 200, {"status": "error", "reason": "no-such-tree", "tree": tree, "say": f"No service in the swarm holds {tree!r}."}
        return self.forward(peer, "/run", body)

    def resume(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        snapshot = body.get("snapshot") or {}
        peer = self.holder(str(snapshot.get("tree", "")))
        if peer is None:
            return 200, {"status": "error", "reason": "no-such-tree", "say": "No service holds the tree this snapshot came from."}
        return self.forward(peer, "/resume", body)

    def forward(self, peer: Peer | None, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if peer is None:
            return 200, {"status": "error", "reason": "no-peer"}
        try:
            status, answer = http_json("POST", f"{peer.url}{path}", body, timeout=120.0, token=self.token)
        except Exception as e:
            peer.last_error = str(e)
            return 200, {"status": "error", "reason": "peer-unreachable", "say": f"{peer.name or peer.url} did not answer: {e}", "output": {"peer": peer.url}}
        if status != 200 or not isinstance(answer, dict):
            return 200, {"status": "error", "reason": "peer-error", "say": f"{peer.name or peer.url} answered {status}", "output": {"peer": peer.url}}
        peer.last_ok = time.time()
        answer.setdefault("peer", peer.url)
        return 200, answer

    def gaps(self) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for peer in list(self.peers.values()):
            try:
                status, body = http_json("GET", f"{peer.url}/gaps", timeout=5.0, token=self.token)
            except Exception:
                continue
            if status == 200 and isinstance(body, dict):
                out.extend({**g, "peer": peer.url} for g in body.get("gaps", []))
        return {"gaps": out}

    def unanswered_all(self) -> dict[str, Any]:
        """This hub's unanswered sentences, then each peer's own (a peer
        without a hub keeps its own record; one with a hub leaves it here)."""
        out = self.unanswered.to_list()
        for peer in list(self.peers.values()):
            if peer.link == "down":
                continue
            try:
                status, body = http_json("GET", f"{peer.url}/unanswered", timeout=5.0, token=self.token)
            except Exception:
                continue
            if status == 200 and isinstance(body, dict):
                out.extend({**u, "peer": peer.url} for u in body.get("unanswered", []))
        return {"unanswered": out}

    def route(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        body = body or {}
        if method == "GET" and path == "/health":
            return 200, self.health()
        if method == "GET" and path == "/peers":
            with self._lock:
                return 200, {"peers": [p.to_dict() for p in self.peers.values()]}
        if method == "GET" and path == "/capabilities":
            return 200, self.capabilities()
        if method == "GET" and path == "/gaps":
            return 200, self.gaps()
        if method == "GET" and path == "/unanswered":
            return 200, self.unanswered_all()
        if method == "GET" and path in ("/", "/index.html", "/console"):
            return 200, PAGE
        if method == "GET" and path == "/api":
            return 200, {"pyrtan": "hub", "name": self.name, "version": __version__, "auth": "bearer" if self.token else "none", "endpoints": ["/health", "/peers", "/capabilities", "/intent", "/run", "/resume", "/gaps", "/unanswered", "/register"]}
        if method == "POST" and path == "/register":
            url = body.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"url\": \"http://host:port\"}."}
            peer = self.add_peer(url, str(body.get("name", "")))
            return 200, {"status": "ok", "peer": peer.to_dict()}
        if method == "POST" and path == "/intent":
            return self.intent(body)
        if method == "POST" and path == "/run":
            return self.run(body)
        if method == "POST" and path == "/resume":
            return self.resume(body)
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


def _rank(peer: Peer) -> int:
    return {"up": 0, "pending": 1, "stale": 2, "down": 3}[peer.link]


def serve_hub(host: str = "127.0.0.1", port: int = 8700, *, name: str = "hub", peers: list[str] | None = None, poll_every: float = 10.0, quiet: bool = False, token: str | None = None, cors: list[str] | None = None) -> tuple[Hub, Server]:
    hub = Hub(name=name, poll_every=poll_every, token=token)
    server = Server(hub.route, host, port, quiet, token=token, cors=cors)
    for url in peers or []:
        hub.add_peer(url)
    hub.start_polling()
    return hub, server
