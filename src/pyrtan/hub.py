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
``GET  /suggestions`` what agents beside the swarm sent this hub (``POST`` to send)
``POST /build``      the builder: questions and the tree file for a sentence (:mod:`pyrtan.build`)
``GET  /``           the console; ``GET /api`` is the JSON index

With ``--chat``, a hub also keeps a lobby at ``/chat``, where people meet a
stranger here or at a hub it follows, or a bot made of the trees it routes
to; see :mod:`pyrtan.chat`. ``--chat-public`` opens the lobby to callers
without the hub's token, who meet people only: a bot runs trees, and the
token is what guards them.

Federated (``--federate``), a hub also answers ``GET /actor``, ``POST
/inbox``, ``POST /follow`` and ``/unfollow`` (operator), ``GET /following``,
``/followers`` and ``/outbox``, ``GET`` and ``POST /networks``, ``POST
/join`` and ``/leave`` (operator: the networks it is in), and routes to
the hubs it follows; see :mod:`pyrtan.federation`.

A service that holds several trees and a hub that routes between services
answer the same requests, so a client need not know which it is talking to,
and a swarm can start as one process and split later without the callers
changing. A hub given a token requires it of callers (``/register``
included, so a stranger cannot join the swarm) and presents it to peers.

**Identities.** A request signed with a key (``X-Pyrtan-Key`` and its
signature, ``_meta/identity.md`` §7) is checked against the hub's
*audience*: the URL the client called, which it signs for and names in
``X-Pyrtan-Audience``, accepted when it is this hub's own id or, on a hub
nobody named with ``--advertise``, the host the request reached. A hub
cannot have a client sign for another hub by naming that hub's audience
at ``GET /api``: the client signs for the URL it has. On a hub with a token,
it may do what the hub's grants say its root may (``ask``, ``chat``,
``run``, ``operate``; see :mod:`pyrtan.grants`) and nothing else; on a hub
without one it only says who is asking. A member that follows with a key
whose root holds ``run`` is trusted as one that joined with the token,
and is dropped when its grant, its note or its root's document says it
may no longer be. ``GET``/``POST /allow`` and ``POST /disallow`` (operator)
keep the grants; ``POST /identity`` takes a granted root's newer identity
document from anyone, since it is signed; ``GET
/.well-known/pyrtan/id/<56>.json`` serves the documents the hub holds.
With ``--names DIR`` a hub is also its network's name provider: it serves
the claims in DIR (``pyrtan names init`` makes a repository of them) at
``/.well-known/pyrtan/name/<name>.json``, ``names.json``, and their
identity documents, for its own domain.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import threading
from pathlib import Path
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from . import __version__
from .chat import Lobby, is_chat_path
from .lan import Lan
from .federation import MAX_HOPS, Federation, Remote, default_network, first_live_hub, parse_invite, rendezvous_hubs, sign, snapshot_hubs
from . import identity
from .grants import Grants, ability_for, parse_may
from .identity import IdentityError
from .grow import Unanswered, no_match_outcome
from .suggestions import Suggestions
from .intent import Trigger, read
from .page import MANIFEST, PYRTAN_JS, SERVICE_WORKER, default_page
from .registry import Registry
from .runtime import Outcome
from .tree import Tree
from .service import PEER_HEADER, Server, Service, bearer_ok, http_json, outcome_from_dict, set_egress, signed_path, build_answer
from .store import StoreError, check_key
from .transport import EgressRefused, check_egress

STALE_AFTER = 30.0
DOWN_AFTER = 90.0
MAX_PEERS = 200   # services a hub polls; /register past this is refused, not queued


@dataclass
class Peer:
    url: str
    name: str = ""
    trees: list[dict[str, Any]] = field(default_factory=list)
    last_ok: float = 0.0
    last_error: str | None = None
    registered: float = field(default_factory=time.time)
    kind: str = "service"
    dry_run: bool = False  # says, in its /capabilities, that it stops before every act in a dry run

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


class Local:
    """Trees the hub holds itself (``pyrtan hub --trees DIR``): a service in
    the same process, reached without HTTP. A swarm that is one process."""

    url = "local"
    kind = "local"
    link = "up"
    last_ok = 0.0
    last_error: str | None = None

    def __init__(self, service: Service) -> None:
        self.service = service
        self.name = service.name

    @property
    def trees(self) -> list[dict[str, Any]]:
        return self.service.capabilities()["trees"]

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "name": self.name, "kind": "local", "link": "up", "trees": [t["name"] for t in self.trees], "last_ok": time.time(), "last_error": None}


class _HubResolver:
    """What a local tree's ``delegate`` reaches: the hub itself, in-process."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    def run_tree(self, name: str, inputs: dict[str, Any], depth: int) -> Outcome:
        _, body = self.hub.run({"tree": name, "inputs": inputs, "depth": depth})
        return outcome_from_dict(body, name)

    def run_intent(self, text: str, depth: int) -> Outcome:
        _, body = self.hub.intent({"text": text, "depth": depth})
        return outcome_from_dict(body, "")


class Hub:
    def __init__(self, *, name: str = "hub", poll_every: float = 10.0, token: str | None = None, local: Service | None = None, handout: Registry | None = None, unanswered: str | Path | None = None, chat: bool = False, chat_typing: bool = True, chat_public: bool = False, grants: Grants | None = None, audience: str | None = None, chat_reach: int = 0, suggestions: str | Path | None = None, names: str | Path | None = None, names_domain: str | None = None) -> None:
        self.name = name
        self.names_dir = Path(names) if names else None
        self._names_domain = names_domain
        self._names_cache: tuple[tuple, Any] | None = None
        self.grants = grants if grants is not None else Grants()
        self.replays = identity.Replays()  # key signatures taken: each POST once
        self.grants.on_change = self._grant_changed
        self._audience = audience
        self.advertised = False   # named with --advertise: then only that name is an audience here
        self.egress = "any"       # the policy for hosts this hub did not choose; on an open hub, what /register may name
        self.lobby = Lobby(self, typing=chat_typing, reach=chat_reach) if chat or chat_public else None
        self.chat_public = chat_public  # callers without the token may chat, with people only
        self.lan: Lan | None = None     # hubs heard on the local network (--lan)
        self.handout_registry = handout  # trees for trusted members only: this hub hands them out and does not run them
        self.poll_every = poll_every
        self.token = token or None
        self.peers: dict[str, Peer] = {}
        self.local: Local | None = None
        if local is not None:
            self.hold(local)
        self.federation: Federation | None = None
        self.unanswered = Unanswered(path=unanswered)
        self.suggestions = Suggestions(path=suggestions)  # what agents sent this hub; a hub keeps its own, it does not collect its peers'
        self._reports: list[dict[str, Any]] = []
        self._reports_at = 0.0
        self.started = time.time()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ federation

    def federate(self, advertise: str, *, secret: str | None = None, state: str | None = None, open_follows: bool = True, listed: bool = False, store: str | None = None, replicas: int = 2, report_gaps: bool = False) -> Federation:
        """Give this hub an identity other hubs can follow. ``advertise`` is
        the URL they reach it at. ``store`` is where it keeps blobs, its own
        and others'; ``replicas`` how many keepers each of its own should have."""
        auto = {
            "directory": lambda: self.federation.directory_backup() if self.federation else [],
            "gaps": lambda: self.gaps()["gaps"],
            "unanswered": lambda: self.unanswered_all()["unanswered"],
        }
        self.federation = Federation(advertise, name=self.name, secret=secret, state=state, open_follows=open_follows, capabilities=self.announced_capabilities, listed=listed, store=store, replicas=replicas, auto_keys=auto, report_gaps=report_gaps, own_reports=self.gap_reports)
        self.federation.handout = self.handout_trees
        self.federation.chat = self.lobby
        return self.federation

    # ------------------------------------------------------------ identities

    @property
    def audience(self) -> str | None:
        """The id a key-signed request to this hub must be signed for."""
        return self.federation.id if self.federation is not None else self._audience

    @property
    def names_domain(self) -> str | None:
        """The domain this hub's names are for: --names-domain, else its own host."""
        if self._names_domain:
            return self._names_domain
        from urllib.parse import urlsplit
        return urlsplit(self.audience).hostname if self.audience else None

    def names(self) -> "Any":
        """The directory of names this hub serves, read again whenever a file in it changes."""
        from .names import load_directory
        if self.names_dir is None or not self.names_domain:
            return None
        try:
            stamp = tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size) for e in self.names_dir.iterdir()))
        except OSError:
            return None
        if self._names_cache is None or self._names_cache[0] != stamp:
            self._names_cache = (stamp, load_directory(self.names_dir, self.names_domain))
        return self._names_cache[1]

    def _signer(self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[identity.Signer | None, set[str], tuple[int, Any] | None]:
        """For a key-signed request: who signed it, what they may do here, or the refusal."""
        if not identity.has_key_signature(headers):
            return None, set(), None
        if self.audience is None:
            return None, set(), (401, {"status": "error", "reason": "unsigned", "say": "This hub has no id to check a key signature against; start it with --advertise."})
        audience = self._audience_for(headers)
        if audience is None:
            asked = next((v for k, v in headers.items() if k.lower() == identity.AUDIENCE_HEADER.lower()), "?")
            return None, set(), (401, {"status": "error", "reason": "unsigned", "say": f"The request is signed for {asked}, which is not this hub ({self.audience})."})
        try:
            signer = identity.verify_request(audience, method, path, body if method == "POST" else None, headers)
        except IdentityError as e:
            return None, set(), (401, {"status": "error", "reason": "unsigned", "say": f"The key signature does not hold: {e}."})
        may = self.grants.may(signer)
        needed = ability_for(method, path)
        if needed == "chat" and self.chat_public:
            needed = None  # a public lobby takes a key it grants nothing as a guest, as it takes a caller with no key
        if self.token and needed is not None and needed not in may:
            granted = f"it may {', '.join(sorted(may))}" if may else "this hub grants it nothing"
            return signer, may, (403, {"status": "error", "reason": "not-granted", "say": f"{identity.short(signer.root)} may not {needed} here: {granted}."})
        if method == "POST":
            # Taken once: a signature seen again within its window is someone replaying it (a refused one may be sent again).
            h = {k.lower(): v for k, v in headers.items()}
            if not self.replays.fresh(h.get("x-pyrtan-key-signature", ""), h.get("x-pyrtan-date")):
                return None, set(), (401, {"status": "error", "reason": "replayed", "say": "This signed request was taken already; sign it again to send it again."})
        return signer, may, None

    def _audience_for(self, headers: dict[str, str]) -> str | None:
        """The audience to check a key signature against. A client signs for the URL it
        called and names it in ``X-Pyrtan-Audience``; that is taken when it is this hub's
        own id or, on a hub nobody named with ``--advertise``, the host this request
        reached (``Host``). A request naming no audience is checked against the hub's id,
        as older clients sign. None when the request is signed for some other hub."""
        h = {k.lower(): v for k, v in headers.items()}
        asked = h.get(identity.AUDIENCE_HEADER.lower())
        own = self.audience or ""
        if asked is None:
            return own
        normal = identity.audience_of(asked)
        if normal is None:
            return None
        if normal == identity.audience_of(own):
            return asked
        if not self.advertised and h.get("host") and normal in {identity.audience_of(f"{scheme}://{h['host']}") for scheme in ("http", "https")}:
            return asked
        return None

    def _grant_changed(self, root: str) -> None:
        """Drop the members that followed as ``root`` and may no longer be what they are."""
        fed = self.federation
        if fed is None:
            return
        for remote in [r for r in list(fed.followers.values()) if r.kind == "member" and r.identity == root]:
            note_ok = remote.note is None or identity.check_device(remote.note, root=root) is None
            revoked = remote.note is not None and (doc := self.grants.document(root)) is not None and identity.device_status(doc, remote.device or "") == "revoked"
            lost_run = remote.trusted_by == "key" and not self.grants.still(root, remote.device or "", remote.note, "run")
            if not note_ok or revoked or lost_run:
                fed.drop_follower(remote.url)

    def handout_trees(self) -> dict[str, Tree]:
        """The trees a trusted member may run: the ones this hub holds itself,
        and the ones given it to hand out (``--handout``), by name. A member
        is a browser: a tree that reads a file, runs a program, opens a
        serial line, talks to a broker or drives a browser stays here."""
        out: dict[str, Tree] = {}
        for reg in (self.local.service.registry if self.local is not None else None, self.handout_registry):
            if reg is None:
                continue
            reg.reload()
            for t in reg.trees():
                if set(t.sources()) <= {"http"}:
                    out.setdefault(t.name, t)
        return out

    def member_capabilities(self) -> list[dict[str, Any]]:
        """The trees this hub's trusted members have announced they run."""
        if self.federation is None:
            return []
        return [{**t, "peer": r.url, "link": r.link, "via": "member"} for r in list(self.federation.followers.values()) if r.kind == "member" and r.trusted for t in r.trees]

    def announced_capabilities(self) -> list[dict[str, Any]]:
        """What this hub answers for: its own trees, its services', and its members'."""
        return self.local_capabilities() + self.member_capabilities()

    def hold(self, service: Service) -> Local:
        """Hold trees in this process. The service's delegations reach the
        hub directly, so a local tree can call anything the hub routes to."""
        service.runner.resolver = _HubResolver(self)
        self.local = Local(service)
        return self.local

    def local_capabilities(self) -> list[dict[str, Any]]:
        """What this hub holds itself and through its own services: what it announces."""
        trees: list[dict[str, Any]] = []
        if self.local is not None:
            for t in self.local.trees:
                trees.append({**t, "peer": "local", "link": "up"})
        with self._lock:
            for peer in self.peers.values():
                for t in peer.trees:
                    trees.append({**t, "peer": peer.url, "link": peer.link})
        return trees

    # ------------------------------------------------------------ peers

    def add_peer(self, url: str, name: str = "") -> Peer:
        url = url.rstrip("/")
        with self._lock:
            peer = self.peers.get(url)
            if peer is None:
                if len(self.peers) >= MAX_PEERS:
                    raise ValueError(f"this hub polls at most {MAX_PEERS} peers")
                peer = Peer(url, name)
                self.peers[url] = peer
            elif name:
                peer.name = name
        self.refresh(peer)
        return peer

    def refresh(self, peer: Peer) -> None:
        try:
            status, caps = http_json("GET", f"{peer.url}/capabilities", timeout=5.0, token=self.token, egress="any")  # a peer is the operator's, or passed /register's check
        except Exception as e:  # urllib raises URLError subclasses of OSError
            status, caps = 0, None
            peer.last_error = str(e)
        if status == 200 and isinstance(caps, dict):
            with self._lock:
                peer.trees = list(caps.get("trees", []))
                peer.dry_run = caps.get("dry_run") is True
                peer.name = caps.get("name") or peer.name
                peer.last_ok = time.time()
                peer.last_error = None
        elif status:
            peer.last_error = f"capabilities answered {status}"

    def refresh_all(self) -> None:
        for peer in list(self.peers.values()):
            self.refresh(peer)
        self.grants.refresh()
        if self.federation is not None:
            # A device note runs out with no grant changing: look at every member that has one.
            for root in {r.identity for r in list(self.federation.followers.values()) if r.kind == "member" and r.identity and r.note is not None}:
                self._grant_changed(root)
            self.federation.refresh_all()
            self.federation.announce()  # only when what we hold changed
            if self.lobby is not None:
                self.lobby.reach_out()  # chat links, for chat only (--chat-reach)

    def start_polling(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.poll_every):
                self.refresh_all()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def on_lan(self) -> Lan:
        """Announce this hub on the LAN and listen for others (``--lan``)."""
        fed = self.federation
        assert fed is not None, "a hub is federated before it goes on the LAN"
        self.lan = Lan(lambda: {"id": fed.id, "name": self.name, "networks": sorted(n.id for n in fed.networks.values() if n.public)}).start()
        fed.lan = self.lan
        return self.lan

    def stop(self) -> None:
        self._stop.set()
        if self.lan is not None:
            self.lan.stop()
        if self.local is not None:
            self.local.service.stop()

    def watches(self) -> dict[str, Any]:
        """This hub's own trees' watches, then each service's."""
        out: list[dict[str, Any]] = []
        if self.local is not None and self.local.service.watcher is not None:
            out.extend({**w, "peer": "local"} for w in self.local.service.watcher.status()["watches"])
        for peer in list(self.peers.values()):
            if peer.link == "down":
                continue
            try:
                status, body = http_json("GET", f"{peer.url}/watches", timeout=5.0, token=self.token, egress="any")
            except Exception:
                continue
            if status == 200 and isinstance(body, dict):
                out.extend({**w, "peer": peer.url} for w in body.get("watches", []))
        return {"count": len(out), "watches": out}

    # ------------------------------------------------------------ lookup

    def holders(self) -> list[Local | Peer | Remote]:
        """Everyone this hub may route to: its own trees, its services, then the hubs it follows."""
        with self._lock:
            out: list[Local | Peer | Remote] = [self.local] if self.local is not None else []
            out.extend(self.peers.values())
            if self.federation is not None:
                # Trusted members run our own trees; a hub we follow answers for its.
                out.extend(r for r in self.federation.followers.values() if r.kind == "member" and r.trusted and r.trees)
                out.extend(self.federation.following.values())
            return out

    def candidates(self, only: str | None = None) -> list[tuple[tuple[str, str], Trigger]]:
        """Every trigger this hub may route by, as ((holder url, tree), trigger);
        ``only`` keeps one holder's."""
        out: list[tuple[tuple[str, str], Trigger]] = []
        for holder in self.holders():
            if holder.link == "down" or (only is not None and holder.url != only):
                continue
            for tree in holder.trees:
                fixed = tree.get("trigger_inputs") if isinstance(tree.get("trigger_inputs"), dict) else {}
                for pattern in tree.get("triggers", []):
                    try:
                        out.append(((holder.url, tree["name"]), Trigger(pattern, dict(fixed.get(pattern) or {}), language=tree.get("language") or {})))
                    except Exception:
                        continue
        return out

    def holder(self, tree_name: str) -> Local | Peer | Remote | None:
        """Who to send a tree to: our own trees, then a service of ours, then
        a hub we follow, and the healthiest of each."""
        best: Local | Peer | Remote | None = None
        for h in self.holders():
            if any(t.get("name") == tree_name for t in h.trees):
                if best is None or _rank(h) < _rank(best):
                    best = h
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
            "trees": sum(len(p.trees) for p in peers) + (len(self.local.trees) if self.local else 0),
            "local": len(self.local.trees) if self.local else 0,
            "unanswered": len(self.unanswered),
            "suggestions": len(self.suggestions),
            "uptime_s": int(time.time() - self.started),
        }

    def capabilities(self) -> dict[str, Any]:
        trees = self.announced_capabilities()
        if self.federation is not None:
            for remote in list(self.federation.following.values()):
                for t in remote.trees:
                    trees.append({**t, "peer": remote.url, "link": remote.link, "via": "federation"})
        return {"name": self.name, "trees": trees}

    def intent(self, body: dict[str, Any], only: str | None = None) -> tuple[int, dict[str, Any]]:
        """Read a sentence and route it. ``only`` (a holder's url) reads it
        against that holder's trees alone, as a chat bot does; a sentence
        they do not answer is then not recorded, since another holder may."""
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 200, {"status": "error", "reason": "bad-request", "say": "Send {\"text\": \"...\"}."}
        reading = read(text, self.candidates(only), _dt.datetime.now(_dt.timezone.utc))
        matches = reading.matches
        if not matches and only is not None:
            return 200, {"status": "error", "reason": "no-match", "say": f"{only} holds no tree that answers {text!r}.", "output": {"reading": {"cleaned": reading.cleaned, "steps": reading.steps}}}
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
        peer = next((h for h in self.holders() if h.url == only), None) if only is not None else self.holder(tree_name) or self.peers.get(peer_url)
        status, answer = self.forward(peer, "/run", {"tree": tree_name, "inputs": matches[0].inputs, "depth": body.get("depth", 0), "trace": body.get("trace", True), "hops": body.get("hops", []), **({"dry_run": True} if body.get("dry_run") else {})})
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

    def forward(self, peer: Local | Peer | Remote | None, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if peer is None:
            return 200, {"status": "error", "reason": "no-peer"}
        dry = body.get("dry_run") or (isinstance(body.get("snapshot"), dict) and body["snapshot"].get("dry_run"))
        if dry and not (isinstance(peer, Local) or (isinstance(peer, Peer) and peer.dry_run)):
            # A dry run must not act anywhere. A member, a followed hub or a service
            # that does not say it stops before its acts might run it for real.
            where = getattr(peer, "name", "") or getattr(peer, "url", "a peer")
            return 200, {"status": "error", "reason": "dry-run", "tree": str(body.get("tree") or ""), "say": f"Dry run: this would run on {where}, which does not say it stops before its acts, so it stops here."}
        if isinstance(peer, Local):
            handler = {"/run": peer.service.run, "/intent": peer.service.intent, "/resume": peer.service.resume}[path]
            answer = handler({k: v for k, v in body.items() if k != "hops"})
            answer.setdefault("peer", "local")
            return 200, answer
        hops = list(body.get("hops") or [])
        if isinstance(peer, Remote) and peer.kind == "member":
            # One of our own devices runs it: an Ask in its mailbox, its Answer back.
            if path != "/run":
                return 200, {"status": "error", "reason": "not-on-member", "say": "A member runs trees; it cannot be asked a sentence or resumed."}
            assert self.federation is not None
            answer = self.federation.ask(peer, str(body.get("tree")), dict(body.get("inputs") or {}), int(body.get("depth", 0)))
            answer.setdefault("peer", peer.url)
            answer["via"] = ["member"]
            if not body.get("trace", True):
                answer.pop("trace", None)
            return 200, answer
        if isinstance(peer, Remote):
            # Over federation: one hop, signed, and the request says where it has been.
            if len(hops) >= MAX_HOPS or (self.federation and self.federation.id in hops):
                return 200, {"status": "error", "reason": "hop-limit", "say": f"{peer.name or peer.url} holds this tree, but the request has already crossed a hub ({hops}); federation is {MAX_HOPS} hop.", "output": {"hops": hops, "peer": peer.url}}
            assert self.federation is not None
            body = {**body, "hops": hops + [self.federation.id]}
            headers = sign(peer.secret, self.federation.id, "POST", path, body)
        else:
            body = {k: v for k, v in body.items() if k != "hops"}
            headers = None
        try:
            status, answer = http_json("POST", f"{peer.url}{path}", body, timeout=120.0, token=None if headers else self.token, headers=headers, egress="any")
        except Exception as e:
            peer.last_error = str(e)
            return 200, {"status": "error", "reason": "peer-unreachable", "say": f"{peer.name or peer.url} did not answer: {e}", "output": {"peer": peer.url}}
        if status != 200 or not isinstance(answer, dict):
            return 200, {"status": "error", "reason": "peer-error", "say": f"{peer.name or peer.url} answered {status}", "output": {"peer": peer.url}}
        peer.last_ok = time.time()
        answer.setdefault("peer", peer.url)
        if isinstance(peer, Remote):
            answer["via"] = [*hops, peer.url]
        return 200, answer

    def gaps(self) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        if self.local is not None:
            out.extend({**g, "peer": "local"} for g in self.local.service.gaps.all())
        for peer in list(self.peers.values()):
            try:
                status, body = http_json("GET", f"{peer.url}/gaps", timeout=5.0, token=self.token, egress="any")
            except Exception:
                continue
            if status == 200 and isinstance(body, dict):
                out.extend({**g, "peer": peer.url} for g in body.get("gaps", []))
        return {"gaps": out}

    def gap_reports(self) -> list[dict[str, Any]]:
        """This hub's gaps for catalog trees (the ones with a namespace),
        as reports: id, node, on, observed, count, last. Cached a minute;
        the announce heartbeat reads it."""
        now = time.time()
        if now - self._reports_at < 60:
            return self._reports
        ids = {t["name"]: f"{t['namespace']}/{t['name']}@{t.get('version', 1)}" for t in self.announced_capabilities() if t.get("namespace") and t.get("name")}
        rows: dict[str, dict[str, Any]] = {}
        for g in self.gaps()["gaps"]:
            tree_id = ids.get(str(g.get("tree")))
            if tree_id is None:
                continue
            key = json.dumps([tree_id, g.get("node"), g.get("on"), g.get("observed")], sort_keys=True, ensure_ascii=False, default=str)
            row = rows.get(key)
            if row is None:
                rows[key] = {"id": tree_id, "node": g.get("node"), "on": g.get("on"), "observed": g.get("observed"), "count": int(g.get("count", 1)), "last": float(g.get("last_seen", now))}
            else:
                row["count"] += int(g.get("count", 1))
                row["last"] = max(row["last"], float(g.get("last_seen", now)))
        self._reports, self._reports_at = list(rows.values()), now
        return self._reports

    def unanswered_all(self) -> dict[str, Any]:
        """This hub's unanswered sentences, then each peer's own (a peer
        without a hub keeps its own record; one with a hub leaves it here)."""
        out = self.unanswered.to_list()
        if self.local is not None:
            out.extend({**u, "peer": "local"} for u in self.local.service.unanswered.to_list())
        for peer in list(self.peers.values()):
            if peer.link == "down":
                continue
            try:
                status, body = http_json("GET", f"{peer.url}/unanswered", timeout=5.0, token=self.token, egress="any")
            except Exception:
                continue
            if status == 200 and isinstance(body, dict):
                out.extend({**u, "peer": peer.url} for u in body.get("unanswered", []))
        return {"unanswered": out}

    def _trusted(self, headers: dict[str, str], may: set[str] | frozenset[str] = frozenset()) -> bool:
        """Whether the caller holds this hub's token (always, when it has none), or a key it grants chat: who meets bots in the lobby."""
        return not self.token or bearer_ok(headers.get("Authorization") or headers.get("authorization"), self.token) or "chat" in may

    def route(self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        body = body or {}
        headers = headers or {}
        fed = self.federation
        signer, may, refused = self._signer(method, path, body, headers)
        if refused:
            return refused
        # The operator: the token, or a key whose root this hub grants operate.
        operator = (bool(self.token) and bearer_ok(headers.get("Authorization") or headers.get("authorization"), self.token)) or "operate" in may
        signed = any(k.lower() == "x-pyrtan-signature" for k in headers)
        if signed and not operator:
            # A hub we federate with is asking. It may ask the routed paths
            # and the inbox; anything else needs the operator's token.
            if fed is None or not signed_path(path):
                return 401, {"status": "error", "reason": "unauthorised", "say": "A signed request may only reach the inbox, the mailbox, the kept blobs and the routed paths."}
            actor = fed.verify_request(method, path, body if method == "POST" else None, headers) if path != "/inbox" else None
            if path != "/inbox" and actor is None:
                return 401, {"status": "error", "reason": "unsigned", "say": "The signature does not hold, or this hub does not know the actor."}
            if method == "GET" and path == "/mailbox":
                return 200, {"activities": fed.mailbox(actor or "")}
            if method == "GET" and path.startswith("/kept/"):
                blob = fed.kept_for(actor or "", path[len("/kept/"):])
                return (200, blob) if blob else (404, {"status": "error", "reason": "not-kept", "say": f"Nothing kept here for you under {path[len('/kept/'):]!r}."})
        if method == "GET" and path == "/health":
            return 200, self.health()
        if method == "GET" and path == "/peers":
            with self._lock:
                out: dict[str, Any] = {"peers": ([self.local.to_dict()] if self.local else []) + [p.to_dict() for p in self.peers.values()]}
            if fed is not None:
                out.update(fed.to_dict())
            if self.lobby is not None and self.lobby.reach:
                out["chat_links"] = self.lobby.links_view()
            return 200, out
        if fed is not None:
            if method == "GET" and path == "/actor":
                return 200, fed.actor()
            if method == "GET" and path == "/lan":
                return (200, self.lan.view()) if self.lan is not None else (404, {"status": "error", "reason": "no-lan", "say": "This hub is not on the LAN; start it with --lan."})
            if method == "GET" and path == "/directory":
                # A private network's part only to the operator: on a hub with no token, a caller on this machine.
                return 200, fed.directory(private=operator or (not self.token and _loopback(headers.get(PEER_HEADER))))
            if method == "GET" and path == "/reports":
                return 200, fed.reports_view()
            if method == "POST" and path == "/inbox":
                by_token = bool(self.token) and bearer_ok(headers.get("Authorization") or headers.get("authorization"), self.token)
                by_key = signer is not None and "run" in may
                return fed.handle_inbox(body, headers, trusted=by_token or by_key, trusted_by="token" if by_token else "key" if by_key else None, signer=signer)
            if method == "GET" and path in ("/following", "/followers"):
                return 200, {path[1:]: fed.to_dict()[path[1:]]}
            if method == "GET" and path == "/outbox":
                return 200, {"outbox": list(fed.outbox), "inbox": list(fed.inbox_log)}
            if method == "POST" and path == "/follow":
                url = body.get("url")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"url\": \"https://hub.example\"}."}
                network = body.get("network")
                if network is not None and not isinstance(network, str):
                    return 400, {"status": "error", "reason": "bad-request", "say": "A network is named by a string."}
                if body.get("listed") is True and not fed.listed:
                    fed.set_listed(True)
                try:
                    remote = fed.follow(url, network=network)
                except ValueError as e:
                    return 400, {"status": "error", "reason": "cannot-follow", "say": str(e)}
                return 200, {"status": "ok", "following": remote.to_dict(), "listed": fed.listed}
            if method == "GET" and path == "/networks":
                return 200, {"networks": fed.networks_view()}
            if method == "POST" and path == "/networks":
                try:
                    net = fed.create_network(body.get("name"), public=body.get("public") is True)
                except ValueError as e:
                    return 400, {"status": "error", "reason": "bad-network", "say": str(e)}
                return 200, {"status": "ok", "network": net.name, "id": net.id, "invite": net.invite(fed.id)}
            if method == "POST" and path == "/join":
                if body.get("listed") is True and not fed.listed:
                    fed.set_listed(True)
                return join_network(fed, body.get("invite"))
            if method == "POST" and path == "/leave":
                name = body.get("network")
                if not isinstance(name, str):
                    return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"network\": \"name\"}."}
                return 200, {"status": "ok", "left": fed.leave_network(name)}
            if method == "POST" and path == "/listed":
                fed.set_listed(body.get("listed") is not False)
                if fed.listed:
                    fed.announce_up(force=True)
                    fed.announce(force=True)
                return 200, {"status": "ok", "listed": fed.listed}
            if method == "GET" and path == "/keep":
                return 200, {"keeping": fed.keeping_view(), "kept": fed.store.list_kept(), "store": fed.store.usage(), "replicas": fed.replicas}
            if method == "GET" and path.startswith("/keep/"):
                blob = fed.store.get_own(path[len("/keep/"):])
                return (200, {**blob, "holders": fed.holders(blob["key"])}) if blob else (404, {"status": "error", "reason": "no-blob", "say": f"No blob of ours under {path[len('/keep/'):]!r}; POST /fetch asks the holders."})
            if method == "POST" and path == "/keep":
                try:
                    return 200, {"status": "ok", "keeping": fed.keep(check_key(body.get("key")), body.get("data"))}
                except StoreError as e:
                    return 400, {"status": "error", "reason": "bad-blob", "say": str(e)}
            if method == "POST" and path == "/fetch":
                key = body.get("key")
                if not isinstance(key, str):
                    return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"key\": \"...\"}."}
                return 200, fed.fetch(key)
            if method == "POST" and path == "/drop":
                # A follower the operator lets go: a lost phone, a hub no longer wanted.
                # Its mailbox, what was handed to it and what is kept for it go too, and
                # anything it signs afterwards is from nobody this hub knows.
                who = body.get("follower")
                if not isinstance(who, str) or not who.strip():
                    return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"follower\": \"<its id, from /peers>\"}."}
                return 200, {"status": "ok", "dropped": fed.drop_follower(who.strip().rstrip("/"))}
            if method == "POST" and path == "/unfollow":
                url = body.get("url")
                if not isinstance(url, str):
                    return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"url\": \"https://hub.example\"}."}
                return 200, {"status": "ok", "removed": fed.unfollow(url)}
        if method == "GET" and path == "/allow":
            return 200, {"grants": self.grants.view(), "audience": self.audience}
        if method == "POST" and path in ("/allow", "/disallow"):
            if not operator:
                # Even on a hub with no token: a grant of run makes a trusted member, which an open hub never had.
                return 403, {"status": "error", "reason": "not-granted", "say": "Grants change only with the operator's token or a key granted operate; a hub with no token takes them from --allow at start."}
            who = body.get("who")
            if not isinstance(who, str):
                return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"who\": \"pyr:…\" or \"name@domain\"" + (", \"may\": [\"ask\", ...]}." if path == "/allow" else "}.")}
            try:
                if path == "/disallow":
                    return 200, {"status": "ok", "removed": self.grants.disallow(who)}
                grant = self.grants.allow(who, parse_may(body.get("may")))
            except IdentityError as e:
                return 400, {"status": "error", "reason": "bad-grant", "say": str(e)}
            return 200, {"status": "ok", "grant": grant.view()}
        if method == "POST" and path == "/identity":
            problem = self.grants.take_document(body.get("document"))
            if problem:
                return 400, {"status": "error", "reason": "bad-identity", "say": problem}
            return 200, {"status": "ok", "id": body["document"]["id"]}
        if method == "GET" and path.startswith("/.well-known/pyrtan/id/") and path.endswith(".json"):
            ident = identity.PREFIX + path[len("/.well-known/pyrtan/id/"):-len(".json")]
            doc = self.grants.document(ident) if len(ident) == len(identity.PREFIX) + identity.ID_LENGTH else None
            named = self.names()
            if named is not None:
                held = [d for d in named.docs.values() if d["id"] == ident and (doc is None or d["updated"] > doc["updated"])]
                doc = max(held, key=lambda d: d["updated"]) if held else doc
            return (200, doc) if doc else (404, {"status": "error", "reason": "no-identity", "say": "This hub holds no document for that identity."})
        if method == "GET" and (path == "/.well-known/pyrtan/names.json" or (path.startswith("/.well-known/pyrtan/name/") and path.endswith(".json"))):
            named = self.names()
            if named is None:
                return 404, {"status": "error", "reason": "no-names", "say": "This hub serves no names; start it with --names DIR."}
            if path.endswith("/names.json"):
                return 200, {"pyrtan-names": 1, "domain": self.names_domain, "names": named.claims}
            claim = named.claims.get(path[len("/.well-known/pyrtan/name/"):-len(".json")])
            return (200, claim) if claim else (404, {"status": "error", "reason": "no-name", "say": f"No such name at {self.names_domain}."})
        if method == "GET" and path == "/capabilities":
            return 200, self.capabilities()
        if method == "GET" and path == "/gaps":
            return 200, self.gaps()
        if method == "GET" and path == "/unanswered":
            return 200, self.unanswered_all()
        if method in ("GET", "POST") and path == "/suggestions":
            return self.suggestions.route(method, body)
        if method == "POST" and path == "/build":
            return build_answer(body)
        if method == "GET" and path == "/watches":
            return 200, self.watches()
        if is_chat_path(path):
            if self.lobby is None:
                return 404, {"status": "error", "reason": "no-chat", "say": "This hub keeps no lobby; start it with --chat."}
            holder = (bool(self.token) and bearer_ok(headers.get("Authorization") or headers.get("authorization"), self.token)) or "chat" in may
            address = None if holder else (headers.get(PEER_HEADER) or "?")
            return self.lobby.route(method, path, body, guest=not self._trusted(headers, may), address=address)
        if method == "GET" and path in ("/", "/index.html", "/console"):
            return 200, default_page()
        if method == "GET" and path == "/manifest.webmanifest":
            return 200, ("application/manifest+json", MANIFEST)
        if method == "GET" and path == "/sw.js":
            return 200, ("text/javascript; charset=utf-8", SERVICE_WORKER)
        if method == "GET" and path == "/pyrtan.js":
            return 200, ("text/javascript; charset=utf-8", PYRTAN_JS)
        if method == "GET" and path == "/api":
            endpoints = ["/health", "/peers", "/capabilities", "/intent", "/run", "/resume", "/gaps", "/unanswered", "/build", "/suggestions", "/watches", "/register", "/allow", "/disallow", "/identity", "/.well-known/pyrtan/id/{id}.json", "/.well-known/pyrtan/name/{name}.json", "/.well-known/pyrtan/names.json"]
            if self.lobby is not None:
                endpoints += ["/chat", "/chat/start", "/chat/poll", "/chat/say", "/chat/typing", "/chat/settings", "/chat/next", "/chat/leave"]
            if fed is not None:
                endpoints += ["/actor", "/inbox", "/mailbox", "/directory", "/lan", "/reports", "/follow", "/unfollow", "/drop", "/networks", "/join", "/leave", "/listed", "/following", "/followers", "/outbox", "/keep", "/fetch", "/kept/{key}"]
            return 200, {"pyrtan": "hub", "name": self.name, "version": __version__, "auth": "bearer" if self.token else "none", "federation": fed.id if fed else None, "chat": self.lobby is not None, "chat_public": self.lobby is not None and self.chat_public, "audience": self.audience, "grants": len(self.grants), "chat_bots": self.lobby is not None and self._trusted(headers, may), "endpoints": endpoints}
        if method == "POST" and path == "/register":
            url = body.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"url\": \"http://host:port\"}."}
            if not self.token:
                # Anyone may register here: the egress policy says where this hub may be sent polling.
                try:
                    check_egress(url, self.egress)
                except EgressRefused as e:
                    return 403, {"status": "error", "reason": "egress", "say": f"{e}."}
            try:
                peer = self.add_peer(url, str(body.get("name", "")))
            except ValueError as e:
                return 503, {"status": "error", "reason": "full", "say": f"{e}."}
            return 200, {"status": "ok", "peer": peer.to_dict()}
        if method == "POST" and path == "/intent":
            return self.intent(body)
        if method == "POST" and path == "/run":
            return self.run(body)
        if method == "POST" and path == "/resume":
            return self.resume(body)
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


def _rank(peer: Local | Peer | Remote) -> int:
    # Our own trees, then a service of ours, then a hub we follow; the healthier of each.
    # Our own trees, then a service of ours, then our trusted members, then a hub we follow; the healthier of each.
    return {"up": 0, "pending": 1, "stale": 2, "down": 3}[peer.link] + (5 if isinstance(peer, Remote) and peer.kind == "member" else 10 if isinstance(peer, Remote) else 0) - (1 if isinstance(peer, Local) else 0)


def _loopback(address: str | None) -> bool:
    try:
        return address is not None and ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def join_network(fed: Federation, invite: Any) -> tuple[int, dict[str, Any]]:
    """Join a network from an invite (``name:secret[@hub]``), or the default
    network (built in) when ``invite`` is empty or ``default``, and follow a
    hub in it: for the default network, the hubs this one already follows
    or heard on its LAN first, then the hub list, then the snapshot, each
    fetched only when the ones before it did not do. A hub that only
    directory gossip named is never followed: any member may gossip an
    entry. When ``$PYRTAN_NETWORK`` names a default network that cannot be
    read, the first listed hub that answers is followed outside any
    network, as before networks."""
    tried: list[str] = []
    if invite in (None, "", "default"):
        net = default_network()
        if net is not None:
            known = net.id

            def candidates() -> Iterator[str]:
                for source in (lambda: fed.known_hubs(known, heard=False), rendezvous_hubs, snapshot_hubs):  # never a hub only gossip named
                    for url in source():
                        if url not in tried and url != fed.id:
                            tried.append(url)
                            yield url

            hubs: Iterable[str] = candidates()
        if net is None:
            url = first_live_hub()
            if url is None:
                return 200, {"status": "error", "reason": "no-network", "say": "Cannot read the default network, and no listed or snapshot hub answers; give an invite or a hub URL."}
            try:
                remote = fed.follow(url)
            except ValueError as e:
                return 200, {"status": "error", "reason": "cannot-follow", "say": str(e)}
            return 200, {"status": "ok", "network": None, "following": remote.to_dict(), "say": f"now follows {url}, outside any network (the default network could not be read)"}
    else:
        try:
            net, url = parse_invite(str(invite))
        except ValueError as e:
            return 400, {"status": "error", "reason": "bad-invite", "say": str(e)}
        hubs = [url] if url else []
        tried.extend(h for h in hubs if h.rstrip("/") != fed.id)
    try:
        remote = fed.join(net, hubs)
    except ValueError as e:
        return 400, {"status": "error", "reason": "bad-network", "say": str(e)}
    net = fed.networks[net.name]
    hubs = tried
    if remote is not None:
        say = f"in {net.name}, following {remote.url}"
    elif hubs:
        say = f"in {net.name}; no hub in it accepted a follow yet (tried {', '.join(hubs)}), so this hub is a network of one until one does or another hub joins through it"
    else:
        say = f"in {net.name}, a network of one until another hub joins through this one"
    return 200, {"status": "ok", "network": net.name, "id": net.id, "public": net.public, "following": remote.to_dict() if remote else None, "invite": net.invite(fed.id), "say": say}


def serve_hub(
    host: str = "127.0.0.1",
    port: int = 8700,
    *,
    name: str = "hub",
    peers: list[str] | None = None,
    poll_every: float = 10.0,
    quiet: bool = False,
    token: str | None = None,
    cors: list[str] | None = None,
    federate: bool = False,
    advertise: str | None = None,
    federation_secret: str | None = None,
    state: str | None = None,
    open_follows: bool = True,
    follow: list[str] | None = None,
    join: list[str] | None = None,
    listed: bool = False,
    trees: list[str | Path] | None = None,
    handout: list[str | Path] | None = None,
    env: dict[str, Any] | None = None,
    gaps: str | None = None,
    egress: str = "any",
    store: str | None = None,
    replicas: int = 2,
    report_gaps: bool = False,
    unanswered: str | Path | None = None,
    watch: bool = False,
    memory: str | Path | None = None,
    lan: bool = False,
    chat_reach: int = 0,
    chat: bool = False,
    suggestions: str | Path | None = None,
    allow: list[str] | None = None,
    grants: str | None = None,
    names: str | Path | None = None,
    names_domain: str | None = None,
    chat_typing: bool = True,
    chat_public: bool = False,
) -> tuple[Hub, Server]:
    """Start a hub. ``allow`` holds ``WHO=ABILITIES`` grants, made before it answers
    anything (a name is resolved now; IdentityError if it cannot be); ``grants`` is
    where the list is kept (default: beside ``state``, else in memory)."""
    from .grants import parse_spec
    specs = [parse_spec(a) for a in allow or []]
    if specs:
        identity.require_signatures()  # a grant nobody could prove is refused at start, not at the first request
    keep = grants or (str(Path(state).with_name("grants.json")) if state else None)
    granted = Grants(keep)
    for who, may in specs:
        granted.allow(who, may)
    set_egress(egress)  # the hub's own requests to hosts it did not choose (followed hubs, the hub list, identity documents) under the same policy as its trees
    local = Service(Registry.from_paths(trees), name=f"{name}", env=env, gaps=gaps, egress=egress, watch=watch, memory=memory) if trees else None
    hub = Hub(name=name, poll_every=poll_every, token=token, local=local, handout=Registry.from_paths(handout) if handout else None, unanswered=unanswered, chat=chat, chat_typing=chat_typing, chat_public=chat_public, grants=granted, chat_reach=chat_reach, suggestions=suggestions, names=names, names_domain=names_domain)
    server = Server(hub.route, host, port, quiet, token=token, cors=cors, federated=federate or bool(advertise), public=is_chat_path if chat_public else None, keyed=True).start()
    hub._audience = (advertise or server.url).rstrip("/")
    hub.advertised = bool(advertise)
    hub.egress = egress
    if federate or advertise or join or lan or chat_reach:
        hub.federate(advertise or server.url, secret=federation_secret, state=state, open_follows=open_follows, listed=listed, store=store or (str(Path(state).with_name("store")) if state else None), replicas=replicas, report_gaps=report_gaps)
    for url in peers or []:
        hub.add_peer(url)
    for url in follow or []:
        try:
            hub.federation.follow(url)  # type: ignore[union-attr]
        except ValueError as e:
            print(f"pyrtan hub: cannot follow {url}: {e}", flush=True)
    if lan:
        hub.on_lan()
        if join and hub.lan is not None and hub.lan.error is None:
            hub.lan.wait(1.5)  # the hubs nearby answer the first question in that time: join one of them
    for invite in join or []:
        _, answer = join_network(hub.federation, invite)  # type: ignore[arg-type]
        print(f"pyrtan hub: join {answer.get('network') or invite.split(':', 1)[0]}: {answer.get('say')}", flush=True)
    if join and hub.lan is not None:
        hub.lan.send()  # now in its networks: say so at once, not at the next beacon
    hub.start_polling()
    return hub, server
