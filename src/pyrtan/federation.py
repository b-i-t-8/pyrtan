"""Federation: hubs that follow each other, and a directory they all keep.

A hub on its own routes sentences to the services that registered with
it. A federated hub also *follows* other hubs, the way one account follows
another on the fediverse, and answers for their trees too. The shape is
ActivityPub's (an actor document, an inbox, ``Follow`` / ``Accept`` /
``Undo`` / ``Announce`` activities) without being an implementation of it:
the activities are Pyrtan's own, the signatures are HMAC on a secret the
two hubs share, and nothing here federates with Mastodon.

The one rule everything rests on: **following is trust, in one direction.**

- A hub routes sentences only to hubs it follows. Its operator chose them.
- A hub that follows another may ask it to run trees; the followed hub
  serves its followers. Nobody else can ask over federation.
- Being followed grants nothing: a follower receives announcements and may
  ask, but the followed hub never routes anything to it.
- A hub announces what its own services hold, never what it learned from
  the hubs it follows, and does not relay: federation is one hop. A
  request that has already crossed a hub is refused rather than forwarded
  again, so there are no loops to detect.

How it works. Each hub has an *actor* document at ``GET /actor`` (its id,
its inbox, its name). To follow, hub A fetches B's actor, makes a secret
for the pair, records B as pending, and posts a ``Follow`` carrying the
secret to B's inbox. B checks the policy (open, or a shared federation
secret the Follow must be signed with), fetches A's actor to confirm the
inbox is A's, records A as a follower, and posts back an ``Accept`` and an
``Announce`` of its capabilities, both signed with the pair secret. From
then on B announces whenever its capabilities change and A polls B's
``/capabilities`` (signed) as a fallback, so B's ``link`` is up, stale or
down like any peer's. ``Undo`` of the Follow ends it.

Every message between two hubs is signed: ``X-Pyrtan-Actor`` names the
sender, ``X-Pyrtan-Date`` carries a unix timestamp within five minutes,
and ``X-Pyrtan-Signature`` is HMAC-SHA256 over the method, path, date and
the SHA-256 of the canonical JSON body. The body sent *is* that canonical
JSON, so the receiver can verify from the body it parsed.

**The directory** is how hubs find each other without a server. A hub
started ``--listed`` puts an entry for itself (id, name, the trees it
holds, when) in every ``Announce`` it sends; every hub keeps the entries
it hears and passes them on in its own announces, in both directions of
every follow. Nothing central holds the directory: every federated hub
does, and pyrtan.com publishes only static files on GitHub Pages: the
default network, the *hub list* (hubs operators added by pull request,
which pyrtan.com asks every hour), and an hourly snapshot of what they
know. No hub is run for it, and none has to be up for it. An
entry is a hint (a hub two follows away cannot verify who said it), kept
until its hub has not been heard of for a week, and routing never uses
it: finding a hub in the directory and following it is still the one act
of trust.

**Networks** are names and secrets. A hub can be in any number and start
one (``create_network``, a network of one); another joins with its
invite, ``name:secret@hub`` (``join``). A Follow made in a network names
it by ``network_id`` (from the name and the secret, so it gives neither
away) and is signed with its secret; the followed hub takes it only if
it is in that network. The directory heard over a link is kept with the
link's network and passed on only over links in the same one, so a
private network's hubs never reach the public directory, and ``GET
/directory`` shows them only to the operator. The default network
(``default_network``, published at pyrtan.com) has a public secret: it
names the network, it does not guard it, and nothing here needs it to.
A follow outside any network (and every member's) works as before.

**Chat.** A hub with a lobby (``--chat``) asks the hubs it follows for a
stranger with ``Meet``, and the two seats talk in ``Say`` and end with
``Part``, with ``Typing`` while someone types, both ways between any two
hubs that share a secret; a ``Say`` is logged without its text and a
``Typing`` not at all. See :mod:`pyrtan.chat`.

What this module refuses: open relaying (no hop beyond one), asymmetric
secrets (there is no key infrastructure to lean on in the standard
library, and a pair secret is enough for hubs whose operators chose each
other), and any routing decision that was not an operator's follow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .service import http_json
from .store import BlobStore, StoreError, check_key, digest as blob_digest

DEFAULT_HUB_LIST = "https://pyrtan.com/rendezvous/hubs.json"
DEFAULT_SNAPSHOT = "https://pyrtan.com/rendezvous/directory.json"
DEFAULT_NETWORK = "https://pyrtan.com/rendezvous/network.json"
NETWORK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
INVITE = re.compile(r"^([a-z0-9][a-z0-9-]{0,39}):([A-Za-z0-9_-]{16,128})(?:@(https?://\S+))?$")
SIGNATURE_WINDOW = 300.0   # seconds a signed message stays valid
MAX_HOPS = 1               # federation is one hop; raise with care
STALE_AFTER = 30.0
DOWN_AFTER = 90.0
ANNOUNCE_EVERY = 60.0      # seconds between announces when nothing changed: the directory's heartbeat
DIRECTORY_TTL = 7 * 86400  # an entry unheard of for this long is dropped
DIRECTORY_MAX = 500
HEARD_UP = 600.0           # a directory entry seen this recently reads "up"
HEARD_STALE = 86400.0
ACTIVITY_TYPES = ("Follow", "Accept", "Reject", "Undo", "Announce", "Keep", "Kept", "Fetch", "Ask", "Answer", "Meet", "Say", "Typing", "Part")
CHAT_TYPES = ("Meet", "Say", "Typing", "Part")   # a lobby's: two strangers on two hubs
UNLOGGED = ("Typing",)                 # said every couple of seconds while someone types: not worth a log line
ASK_TIMEOUT = 30.0                     # seconds a hub waits for a member to answer an Ask
MEMBER_PREFIX = "urn:pyrtan:member:"   # an actor with no inbox: a browser or a phone, which polls a mailbox
MAILBOX_MAX = 200                      # activities a hub holds for a member that has not polled
REPORT_TTL = 30 * 86400                # a gap report unheard of for this long is dropped
REPORT_MAX = 2000
REPORT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*@\d+$")  # namespace/name@version: a catalog tree
HOLDER_FRESH = 86400.0                 # a holder confirmed within this counts toward the replicas
ASK_WINDOW = 600.0                     # a member asked to keep within this counts as covered until it confirms
SIGNED_PATHS = ("/inbox", "/run", "/intent", "/resume", "/capabilities")


# ---------------------------------------------------------------- the hub list


def rendezvous_hubs() -> list[str]:
    """Hubs to try first, from ``$PYRTAN_RENDEZVOUS`` (one hub),
    ``$PYRTAN_HUB_LIST`` (a URL or file holding ``{"hubs": [...]}``), or
    the hub list at pyrtan.com. Never raises: no list is an empty list."""
    single = os.environ.get("PYRTAN_RENDEZVOUS")
    if single:
        return [single.rstrip("/")]
    source = os.environ.get("PYRTAN_HUB_LIST") or DEFAULT_HUB_LIST
    try:
        if source.startswith(("http://", "https://")):
            status, body = http_json("GET", source, timeout=10.0)
            if status != 200:
                return []
        else:
            body = json.loads(Path(source).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(body, dict) or not isinstance(body.get("hubs"), list):
        return []
    return [h.rstrip("/") for h in body["hubs"] if isinstance(h, str) and h.startswith(("http://", "https://"))]


def snapshot_hubs() -> list[str]:
    """Hubs from the directory snapshot pyrtan.com bakes hourly
    (``$PYRTAN_SNAPSHOT`` overrides the URL or names a file), most recently
    heard first. What a new hub can try when no listed hub answers."""
    source = os.environ.get("PYRTAN_SNAPSHOT") or DEFAULT_SNAPSHOT
    try:
        if source.startswith(("http://", "https://")):
            status, body = http_json("GET", source, timeout=10.0)
            if status != 200:
                return []
        else:
            body = json.loads(Path(source).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(body, dict) or not isinstance(body.get("hubs"), list):
        return []
    hubs = [h for h in body["hubs"] if isinstance(h, dict) and isinstance(h.get("id"), str) and h["id"].startswith(("http://", "https://"))]
    hubs.sort(key=lambda h: -float(h.get("seen", 0) or 0))
    return [h["id"].rstrip("/") for h in hubs]


# ---------------------------------------------------------------- networks


def network_id(name: str, secret: str) -> str:
    """What a Follow names a network by: derived from its name and secret,
    so two networks that share a name never meet, and the id gives the
    secret away to nobody."""
    return hashlib.sha256(f"pyrtan-network\n{name}\n{secret}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Network:
    """A set of hubs that sign their follows with one secret. ``public``
    marks one whose secret is published (the default network at
    pyrtan.com): its directory is shown to anyone, a private one's only
    to the operator."""

    name: str
    secret: str
    public: bool = False

    @property
    def id(self) -> str:
        return network_id(self.name, self.secret)

    def invite(self, hub: str | None = None) -> str:
        return f"{self.name}:{self.secret}" + (f"@{hub.rstrip('/')}" if hub else "")

    def to_state(self) -> dict[str, Any]:
        return {"name": self.name, "secret": self.secret, "public": self.public}

    @classmethod
    def from_state(cls, d: Any) -> "Network | None":
        if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not isinstance(d.get("secret"), str):
            return None
        if not NETWORK_NAME.match(d["name"]) or len(d["secret"]) < 16:
            return None
        return cls(d["name"], d["secret"], bool(d.get("public", False)))


def parse_invite(text: str) -> tuple[Network, str | None]:
    """``name:secret`` or ``name:secret@https://hub``: a network, and the
    hub to follow into it. Raises ValueError on anything else."""
    m = INVITE.match(text.strip()) if isinstance(text, str) else None
    if not m:
        raise ValueError("an invite is NAME:SECRET or NAME:SECRET@https://hub (a name of lowercase letters, digits and dashes; a secret of at least 16 letters, digits, dashes or underscores)")
    return Network(m.group(1), m.group(2)), (m.group(3).rstrip("/") if m.group(3) else None)


def default_network() -> Network | None:
    """The public network pyrtan.com publishes (``$PYRTAN_NETWORK``
    overrides the URL or names a file). Its secret is public on purpose:
    it names the network rather than guarding it. None when unreadable."""
    source = os.environ.get("PYRTAN_NETWORK") or DEFAULT_NETWORK
    try:
        if source.startswith(("http://", "https://")):
            status, body = http_json("GET", source, timeout=10.0)
            if status != 200:
                return None
        else:
            body = json.loads(Path(source).read_text(encoding="utf-8"))
    except Exception:
        return None
    net = Network.from_state(body)
    if net is not None:
        net.public = True
    return net


def bootstrap_candidates() -> list[str]:
    """Where to look for a first hub: the hub list, then the snapshot's hubs."""
    listed = rendezvous_hubs()
    return listed + [h for h in snapshot_hubs() if h not in listed]


def first_live_hub(hubs: list[str] | None = None) -> str | None:
    """The first listed hub whose ``/actor`` answers as a hub; when none
    does, the first hub from the snapshot that answers; else None."""
    tried = hubs if hubs is not None else rendezvous_hubs()
    for batch in ([tried] if hubs is not None else [tried, lambda: [h for h in snapshot_hubs() if h not in tried]]):
        for url in (batch() if callable(batch) else batch):
            try:
                status, actor = http_json("GET", f"{url}/actor", timeout=5.0)
            except Exception:
                continue
            if status == 200 and isinstance(actor, dict) and actor.get("type") == "Hub":
                return url
    return None


# ---------------------------------------------------------------- signing


def canonical(body: Any) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret: str, actor: str, method: str, path: str, body: Any, date: float | None = None) -> dict[str, str]:
    date_text = str(int(date if date is not None else time.time()))
    digest = hashlib.sha256(canonical(body) if body is not None else b"").hexdigest()
    message = "\n".join([method.upper(), path, date_text, digest]).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {"X-Pyrtan-Actor": actor, "X-Pyrtan-Date": date_text, "X-Pyrtan-Signature": mac}


def verify(secret: str, method: str, path: str, body: Any, headers: dict[str, str], now: float | None = None) -> str | None:
    """None if the signature holds; otherwise what is wrong with it."""
    date_text = headers.get("X-Pyrtan-Date", "")
    mac = headers.get("X-Pyrtan-Signature", "")
    if not date_text.isdigit() or not mac:
        return "no signature"
    if abs((now if now is not None else time.time()) - int(date_text)) > SIGNATURE_WINDOW:
        return "signature too old"
    expected = sign(secret, headers.get("X-Pyrtan-Actor", ""), method, path, body, int(date_text))["X-Pyrtan-Signature"]
    return None if hmac.compare_digest(expected, mac) else "bad signature"


def _lower(headers: dict[str, str] | None) -> dict[str, str]:
    """Header lookup is case-insensitive; keep the X-Pyrtan-* names as written."""
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        key = "-".join(p.capitalize() if p.lower() != "pyrtan" else "Pyrtan" for p in k.split("-"))
        out[key] = v
    return out


# ---------------------------------------------------------------- remotes


@dataclass
class Remote:
    """A hub we follow, or one that follows us."""

    url: str
    inbox: str
    secret: str
    name: str = ""
    state: str = "pending"      # pending | accepted | rejected
    trees: list[dict[str, Any]] = field(default_factory=list)
    last_ok: float = 0.0
    last_error: str | None = None
    since: float = field(default_factory=time.time)
    kind: str = "hub"           # hub | member
    trusted: bool = False       # a member that joined with the operator's token: it may run the hub's trees
    network: str | None = None  # the id of the network the follow was made in; None for a follow outside any

    @property
    def link(self) -> str:
        if self.state != "accepted":
            return "down" if self.state == "rejected" else "pending"
        if self.last_ok == 0.0:
            return "down" if self.last_error else "pending"
        age = time.time() - self.last_ok
        if age < STALE_AFTER:
            return "up"
        if age < DOWN_AFTER:
            return "stale"
        return "down"

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "name": self.name, "kind": self.kind, "state": self.state, "link": self.link, "trees": [t["name"] for t in self.trees], "last_ok": self.last_ok, "last_error": self.last_error, "trusted": self.trusted, "network": self.network}

    def to_state(self) -> dict[str, Any]:
        return {"url": self.url, "inbox": self.inbox, "secret": self.secret, "name": self.name, "state": self.state, "since": self.since, "kind": self.kind, "trusted": self.trusted, "network": self.network}

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "Remote":
        return cls(d["url"], d["inbox"], d["secret"], d.get("name", ""), d.get("state", "pending"), since=float(d.get("since", time.time())), kind=d.get("kind", "hub"), trusted=bool(d.get("trusted", False)), network=d.get("network") if isinstance(d.get("network"), str) else None)


def _shape_report(e: dict[str, Any], reporter: str, now: float) -> dict[str, Any] | None:
    """A report as carried: valid or nothing. ``reporter`` is who said it
    first (the entry's own, else who we heard it from)."""
    tree_id, node, on = e.get("id"), e.get("node"), e.get("on")
    if not (isinstance(tree_id, str) and REPORT_ID.match(tree_id) and isinstance(node, str) and isinstance(on, str)):
        return None
    who = e.get("reporter") if isinstance(e.get("reporter"), str) and e.get("reporter", "").startswith(("http://", "https://")) else reporter
    try:
        count = max(1, int(e.get("count", 1)))
        last = min(float(e.get("last", now)), now)
    except (TypeError, ValueError):
        return None
    if now - last >= REPORT_TTL:
        return None
    observed = e.get("observed")
    if isinstance(observed, str) and len(observed) > 80:
        observed = "<str>"
    elif not isinstance(observed, (str, int, float, bool)) and observed is not None:
        observed = f"<{type(observed).__name__}>"
    return {"reporter": who.rstrip("/"), "id": tree_id, "node": node[:80], "on": on[:120], "observed": observed, "count": count, "last": last}


def _logged(activity: dict[str, Any]) -> dict[str, Any]:
    """An activity as the outbox and inbox logs keep it: a chat line without its text."""
    obj = activity.get("object")
    if activity.get("type") == "Say" and isinstance(obj, dict) and "text" in obj:
        return {**activity, "object": {**obj, "text": f"<{len(str(obj['text']))} characters>"}}
    return activity


def heard(seen: float, now: float | None = None) -> str:
    """How recently a directory entry's hub announced, as gossip carried it."""
    age = (now if now is not None else time.time()) - seen
    return "up" if age < HEARD_UP else "stale" if age < HEARD_STALE else "down"


class Federation:
    def __init__(self, hub_id: str, *, name: str = "hub", secret: str | None = None, state: str | Path | None = None, open_follows: bool = True, capabilities: Callable[[], list[dict[str, Any]]] | None = None, listed: bool = False, store: BlobStore | str | Path | None = None, replicas: int = 2, auto_keys: dict[str, Callable[[], Any]] | None = None, report_gaps: bool = False, own_reports: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.id = hub_id.rstrip("/")
        self.report_gaps = report_gaps        # opt in: our own catalog trees' gaps travel with our announces
        self.own_reports = own_reports or (lambda: [])  # the hub's gaps for catalog trees, shaped for a report
        self.reports: dict[str, dict[str, Any]] = {}    # heard gap reports: (reporter, id, node, on, observed) -> entry
        self.name = name
        self.secret = secret or None          # a shared federation secret: Follows must be signed with it
        self.open_follows = open_follows      # without a secret, whether strangers may follow
        self.state_path = Path(state) if state else None
        self.capabilities = capabilities or (lambda: [])
        self.listed = listed                  # put ourselves in the directory we gossip
        self.store = store if isinstance(store, BlobStore) else BlobStore(store)
        self.replicas = max(0, int(replicas))  # how many keepers each of our blobs should have
        self.auto_keys = dict(auto_keys or {})  # blobs kept automatically: key -> what to keep
        self._auto_digest: dict[str, str] = {}
        self.keeping: dict[str, dict[str, Any]] = {}        # our blobs: key -> {sha256, size, updated, holders: {actor: {at, sha256}}}
        self.mailboxes: dict[str, deque[dict[str, Any]]] = {}  # activities waiting for members
        self.handout: Callable[[], dict[str, Any]] = lambda: {}  # the trees trusted members run: name -> the tree as JSON
        self._handed: dict[str, dict[str, dict[str, Any]]] = {}  # member -> key -> {sha256, at, confirmed}
        self._asks: dict[str, dict[str, Any]] = {}            # run id -> {member, event, answer}
        self.chat: Any = None                                  # the hub's lobby, when it keeps one (pyrtan.chat.Lobby)
        self.ask_timeout = ASK_TIMEOUT
        self.following: dict[str, Remote] = {}
        self.followers: dict[str, Remote] = {}
        self.networks: dict[str, Network] = {}               # name -> network this hub is in
        self.directory_entries: dict[str, dict[str, Any]] = {}  # (network id, hub id) -> entry; see _dkey
        self.outbox: deque[dict[str, Any]] = deque(maxlen=50)
        self.inbox_log: deque[dict[str, Any]] = deque(maxlen=50)
        self._announced: tuple[str, float] = ("", 0.0)       # digest and time of the last downward announce
        self._announced_up: tuple[str, float] = ("", 0.0)    # likewise upward
        self._lock = threading.RLock()
        if self.state_path and self.state_path.exists():
            self._load()
            if listed:
                self.listed = True

    # ------------------------------------------------------------ actor

    @property
    def inbox(self) -> str:
        return f"{self.id}/inbox"

    def actor(self) -> dict[str, Any]:
        return {"type": "Hub", "id": self.id, "name": self.name, "inbox": self.inbox, "mailbox": f"{self.id}/mailbox", "capabilities": f"{self.id}/capabilities", "directory": f"{self.id}/directory", "pyrtan": __version__, "follows": "open" if (self.open_follows and not self.secret) else "secret", "listed": self.listed, "replicas": self.replicas}

    # ------------------------------------------------------------ state

    def _save(self) -> None:
        if not self.state_path:
            return
        with self._lock:
            data = {
                "listed": self.listed,
                "networks": [n.to_state() for n in self.networks.values()],
                "following": [r.to_state() for r in self.following.values()],
                "followers": [r.to_state() for r in self.followers.values()],
                "directory": list(self.directory_entries.values()),
                "keeping": self.keeping,
                "reports": list(self.reports.values()),
            }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _load(self) -> None:
        assert self.state_path
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.listed = bool(data.get("listed", False))
        for d in data.get("networks", []):
            net = Network.from_state(d)
            if net is not None:
                self.networks[net.name] = net
        for d in data.get("following", []):
            self.following[d["url"]] = Remote.from_state(d)
        for d in data.get("followers", []):
            self.followers[d["url"]] = Remote.from_state(d)
        self.merge_directory(data.get("directory", []), via="state", keep_tags=True)
        self.merge_reports(data.get("reports", []), via="state")
        keeping = data.get("keeping")
        if isinstance(keeping, dict):
            self.keeping = {k: v for k, v in keeping.items() if isinstance(v, dict) and isinstance(v.get("holders"), dict)}

    def set_listed(self, listed: bool) -> None:
        with self._lock:
            self.listed = listed
            if not listed:
                self.directory_entries.pop(self.id, None)
            self._save()

    # ------------------------------------------------------------ networks
    # A network is a name and a secret its hubs sign their follows with. A
    # hub may be in any number, starts one with create_network, and joins
    # one with an invite (join). Every follow made in a network says so,
    # and the directory heard over it is kept apart and passed on only
    # within it, so a private network's hubs never reach the public one.
    # A follow outside any network (follow(url), a member) works as before.

    def network(self, name: str) -> Network:
        net = self.networks.get(name)
        if net is None:
            raise ValueError(f"this hub is in no network named {name!r}")
        return net

    def network_by_id(self, nid: Any) -> Network | None:
        return next((n for n in self.networks.values() if n.id == nid), None) if isinstance(nid, str) else None

    def add_network(self, net: Network) -> Network:
        """Be in ``net``. The same network again is a no-op; another one
        under a name already taken is refused."""
        with self._lock:
            current = self.networks.get(net.name)
            if current is not None:
                if current.secret != net.secret:
                    raise ValueError(f"this hub is already in a different network named {net.name!r}; leave it first")
                current.public = current.public or net.public
                return current
            self.networks[net.name] = net
            self._save()
        return net

    def create_network(self, name: str, public: bool = False) -> Network:
        if not isinstance(name, str) or not NETWORK_NAME.match(name):
            raise ValueError("a network's name is lowercase letters, digits and dashes, at most 40")
        if name in self.networks:
            raise ValueError(f"this hub is already in a network named {name!r}")
        return self.add_network(Network(name, _secrets.token_urlsafe(24), public))

    def join(self, net: Network, hubs: list[str]) -> Remote | None:
        """Be in ``net`` and follow the first of ``hubs`` that accepts a
        follow in it. None when none does: the hub is then a network of
        one, which others can join by following it."""
        net = self.add_network(net)
        for url in hubs:
            url = url.rstrip("/")
            if url == self.id:
                continue
            current = self.following.get(url)
            if current is not None and current.network == net.id and current.state == "accepted":
                return current
            before = (current.network, current.state) if current is not None else None
            try:
                remote = self.follow(url, network=net.name)
            except ValueError:
                continue
            if remote.state == "accepted":
                return remote
            with self._lock:
                # Not accepted in this network: leave things as they were.
                if before is None:
                    self.following.pop(url, None)
                else:
                    remote.network, remote.state = before
                self._save()
        return None

    def leave_network(self, name: str) -> bool:
        """Leave a network: unfollow the hubs followed in it, drop the hubs
        that followed us in it, and forget its directory."""
        net = self.networks.get(name)
        if net is None:
            return False
        for url in [u for u, r in self.following.items() if r.network == net.id]:
            self.unfollow(url)
        with self._lock:
            for url in [u for u, r in self.followers.items() if r.network == net.id]:
                del self.followers[url]
            for key in [k for k, e in self.directory_entries.items() if e.get("network") == net.id]:
                del self.directory_entries[key]
            del self.networks[name]
            self._save()
        return True

    def networks_view(self) -> list[dict[str, Any]]:
        """The operator's view: each network with its invite."""
        with self._lock:
            return [{"name": n.name, "id": n.id, "public": n.public, "invite": n.invite(self.id), "following": sorted(u for u, r in self.following.items() if r.network == n.id), "followers": sorted(u for u, r in self.followers.items() if r.network == n.id)} for n in sorted(self.networks.values(), key=lambda n: n.name)]

    # ------------------------------------------------------------ directory

    @staticmethod
    def _dkey(network: str | None, hub_id: str) -> str:
        return hub_id if network is None else f"{network} {hub_id}"

    def own_entry(self) -> dict[str, Any] | None:
        if not self.listed:
            return None
        return {"id": self.id, "name": self.name, "kind": "hub", "trees": sorted({t.get("name") for t in self.capabilities() if isinstance(t, dict) and t.get("name")}), "seen": time.time()}

    def gossip(self, network: str | None = None) -> list[dict[str, Any]]:
        """What this hub tells a hub it is linked to in ``network`` (an id;
        None for a link outside any) about the directory: itself, if
        listed, and every live entry it heard in that same network."""
        now = time.time()
        with self._lock:
            entries = [e for e in self.directory_entries.values() if e["id"] != self.id and e.get("network") == network and now - e["seen"] < DIRECTORY_TTL]
        own = self.own_entry()
        return ([own] if own else []) + sorted(({k: v for k, v in e.items() if k != "network"} for e in entries), key=lambda e: -e["seen"])

    def _public_networks(self) -> set[str | None]:
        return {None} | {n.id for n in self.networks.values() if n.public}

    def directory_backup(self) -> list[dict[str, Any]]:
        """The directory as a blob for keepers: the public part, tagged,
        so a private network's hubs are never handed to a keeper."""
        now = time.time()
        public = self._public_networks()
        with self._lock:
            entries = [dict(e) for e in self.directory_entries.values() if e.get("network") in public and now - e["seen"] < DIRECTORY_TTL]
        own = self.own_entry()
        return ([own] if own else []) + sorted(entries, key=lambda e: -e["seen"])

    def merge_directory(self, entries: Any, via: str, network: str | None = None, keep_tags: bool = False) -> int:
        """Take entries heard from ``via`` over a link in ``network``: a
        newer ``seen`` replaces an older one, our own entry is never
        overwritten, nothing from the future is believed, and the table
        stays bounded. ``keep_tags`` is for our own copies (the state file,
        a blob handed back): each entry keeps the network it carries, if we
        are still in it. Returns how many entries changed."""
        if not isinstance(entries, list):
            return 0
        now = time.time()
        changed = 0
        with self._lock:
            known = {n.id for n in self.networks.values()}
            for e in entries:
                if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].startswith(("http://", "https://", MEMBER_PREFIX)):
                    continue
                hub_id = e["id"].rstrip("/")
                if hub_id == self.id:
                    continue
                net = network
                if keep_tags:
                    net = e.get("network") if isinstance(e.get("network"), str) else None
                    if net is not None and net not in known:
                        continue
                try:
                    seen = min(float(e.get("seen", 0)), now)
                except (TypeError, ValueError):
                    continue
                if now - seen >= DIRECTORY_TTL:
                    continue
                key = self._dkey(net, hub_id)
                current = self.directory_entries.get(key)
                if current is not None and current["seen"] >= seen:
                    continue
                self.directory_entries[key] = {
                    "id": hub_id,
                    "name": str(e.get("name") or "")[:80],
                    "kind": "member" if hub_id.startswith(MEMBER_PREFIX) else "hub",
                    "trees": [str(t)[:80] for t in e.get("trees", []) if isinstance(t, (str, int))][:200],
                    "seen": seen,
                    "via": via,
                    "network": net,
                }
                changed += 1
            if len(self.directory_entries) > DIRECTORY_MAX:
                for key in sorted(self.directory_entries, key=lambda k: self.directory_entries[k]["seen"])[: len(self.directory_entries) - DIRECTORY_MAX]:
                    del self.directory_entries[key]
        if changed:
            self._save()
        return changed

    def directory(self, private: bool = True) -> dict[str, Any]:
        """Every hub this one has heard of that asked to be found, this one
        included if listed: one row per hub, naming the networks it was
        heard in. ``link`` is how recently each announced. Without
        ``private`` (a caller without the operator's token), only what was
        heard outside any network or in a public one."""
        now = time.time()
        visible = None if private else self._public_networks()
        names = {n.id: n.name for n in self.networks.values()}
        rows: dict[str, dict[str, Any]] = {}
        own = self.own_entry()
        if own:
            rows[self.id] = {**own, "via": "self", "networks": sorted(n.id for n in self.networks.values() if private or n.public)}
        with self._lock:
            entries = [dict(e) for e in self.directory_entries.values() if now - e["seen"] < DIRECTORY_TTL and (visible is None or e.get("network") in visible)]
        for e in sorted(entries, key=lambda e: -e["seen"]):
            row = rows.get(e["id"])
            if row is None:
                rows[e["id"]] = row = {**{k: v for k, v in e.items() if k != "network"}, "networks": []}
            if e.get("network") and e["network"] not in row["networks"]:
                row["networks"].append(e["network"])
        hubs = [{"id": r["id"], "name": r["name"], "kind": r.get("kind", "hub"), "trees": r["trees"], "seen": r["seen"], "link": heard(r["seen"], now), "via": r.get("via", ""), "networks": sorted(r["networks"])} for r in rows.values()]
        hubs.sort(key=lambda h: (h["id"] != self.id, {"up": 0, "stale": 1, "down": 2}[h["link"]], h["name"], h["id"]))
        shown = {nid for h in hubs for nid in h["networks"]}
        return {"hub": self.id, "name": self.name, "listed": self.listed, "count": len(hubs), "hubs": hubs, "networks": {nid: names[nid] for nid in sorted(shown) if nid in names}}

    # ------------------------------------------------------------ gap reports
    # A hub that opted in (--report-gaps) puts the gaps its catalog trees met
    # into its announces: the tree's id (namespace/name@version), the node,
    # the path decided on and the value no branch covered, with a count;
    # never the inputs or the rest of the state. Every federated hub keeps
    # what it hears and passes it on, like the directory, so the reports
    # reach the listed hubs and the snapshot pyrtan.com bakes, where an author
    # reads them beside the tree. Nothing is reported for a tree without a
    # namespace: there is nobody to report to.

    def report_gossip(self) -> list[dict[str, Any]]:
        now = time.time()
        own = []
        if self.report_gaps:
            for r in self.own_reports():
                shaped = _shape_report(r, self.id, now)
                if shaped:
                    own.append(shaped)
        with self._lock:
            heard = [r for r in self.reports.values() if r["reporter"] != self.id and now - r["last"] < REPORT_TTL]
        return own + sorted(heard, key=lambda r: -r["last"])

    def merge_reports(self, entries: Any, via: str) -> int:
        """Take reports heard from ``via``: one row per reporter and gap, a
        newer ``last`` replaces an older one, nothing from the future is
        believed, old news is dropped, the table stays bounded."""
        if not isinstance(entries, list):
            return 0
        now = time.time()
        changed = 0
        with self._lock:
            for e in entries:
                shaped = _shape_report(e, via, now) if isinstance(e, dict) else None
                if shaped is None or shaped["reporter"] == self.id:
                    continue
                key = json.dumps([shaped["reporter"], shaped["id"], shaped["node"], shaped["on"], shaped["observed"]], sort_keys=True, ensure_ascii=False)
                current = self.reports.get(key)
                if current is not None and (current["last"], current["count"]) >= (shaped["last"], shaped["count"]):
                    continue
                self.reports[key] = shaped
                changed += 1
            if len(self.reports) > REPORT_MAX:
                for key in sorted(self.reports, key=lambda k: self.reports[k]["last"])[: len(self.reports) - REPORT_MAX]:
                    del self.reports[key]
        if changed:
            self._save()
        return changed

    def reports_view(self) -> dict[str, Any]:
        """Every gap report this hub carries, one row per gap with the
        counts and reporters summed, most seen first."""
        rows: dict[str, dict[str, Any]] = {}
        for r in self.report_gossip():
            key = json.dumps([r["id"], r["node"], r["on"], r["observed"]], sort_keys=True, ensure_ascii=False)
            row = rows.get(key)
            if row is None:
                rows[key] = {"id": r["id"], "node": r["node"], "on": r["on"], "observed": r["observed"], "count": r["count"], "reporters": 1, "last": r["last"]}
            else:
                row["count"] += r["count"]
                row["reporters"] += 1
                row["last"] = max(row["last"], r["last"])
        out = sorted(rows.values(), key=lambda r: (-r["count"], -r["last"], r["id"]))
        return {"hub": self.id, "reporting": self.report_gaps, "count": len(out), "reports": out}

    # ------------------------------------------------------------ outgoing

    def _activity(self, type_: str, object_: Any, **extra: Any) -> dict[str, Any]:
        return {"type": type_, "id": f"{self.id}/activities/{uuid.uuid4()}", "actor": self.id, "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "object": object_, **extra}

    def _announcement(self, for_remote: Remote | None = None) -> dict[str, Any]:
        out = {"type": "Capabilities", "name": self.name, "trees": self.capabilities(), "directory": self.gossip(for_remote.network if for_remote is not None else None)}
        reports = self.report_gossip()
        if reports:
            out["reports"] = reports
        if for_remote is not None:
            out["kept"] = [b["key"] for b in self.store.list_kept(for_remote.url)]  # what we keep for them
        return out

    def deliver(self, remote: Remote, activity: dict[str, Any], secret: str | None = None) -> tuple[bool, str | None, Any]:
        """Post an activity to a remote's inbox, signed with the pair secret
        (or the one given); for a member, leave it in its mailbox. Returns
        (delivered, error, the inbox's answer)."""
        if remote.inbox == "mailbox":
            box = self.mailboxes.setdefault(remote.url, deque(maxlen=MAILBOX_MAX))
            box.append(activity)
            self.outbox.append({**activity, "to": remote.url, "delivered": True, "error": None, "mailbox": True})
            return True, None, None
        path = "/inbox"
        headers = sign(secret or remote.secret, self.id, "POST", path, activity)
        try:
            status, answer = http_json("POST", remote.inbox, activity, timeout=10.0, headers=headers)
        except Exception as e:
            if activity.get("type") not in UNLOGGED:
                self.outbox.append({**_logged(activity), "to": remote.url, "delivered": False, "error": str(e)})
            return False, str(e), None
        ok = status in (200, 202)
        if activity.get("type") not in UNLOGGED:
            self.outbox.append({**_logged(activity), "to": remote.url, "delivered": ok, "error": None if ok else f"{status}: {answer}"})
        if ok and isinstance(answer, dict):
            if activity.get("type") == "Announce":
                # An inbox answers an Announce with its own gossip: one round trip, both directions.
                self.merge_directory(answer.get("directory"), via=remote.url, network=remote.network)
                self.merge_reports(answer.get("reports"), via=remote.url)
            if activity.get("type") == "Keep" and answer.get("kept") == activity["object"].get("key"):
                self.confirm(remote.url, activity["object"]["key"], activity["object"]["sha256"])
        return ok, None if ok else f"inbox answered {status}: {answer}", answer

    def mailbox(self, actor: str) -> list[dict[str, Any]]:
        """Hand a member what has been waiting for it, and forget it."""
        box = self.mailboxes.get(actor.rstrip("/"))
        if not box:
            return []
        out = list(box)
        box.clear()
        remote = self.followers.get(actor.rstrip("/"))
        if remote is not None:
            remote.last_ok = time.time()
            remote.last_error = None
        return out

    def follow(self, url: str, network: str | None = None) -> Remote:
        """Follow the hub at ``url``: fetch its actor, make a pair secret,
        send the Follow. The remote is pending until it Accepts. In a
        ``network`` (a name this hub is in), the Follow names it and is
        signed with its secret, and what the two hear of the directory over
        this link stays in it."""
        url = url.rstrip("/")
        if url == self.id:
            raise ValueError("a hub cannot follow itself")
        net = self.network(network) if network is not None else None
        try:
            status, actor = http_json("GET", f"{url}/actor", timeout=10.0)
        except Exception as e:
            raise ValueError(f"{url} does not answer /actor: {e}") from e
        if status != 200 or not isinstance(actor, dict) or actor.get("type") != "Hub" or not isinstance(actor.get("inbox"), str):
            raise ValueError(f"{url} does not answer /actor as a Pyrtan hub ({status})")
        with self._lock:
            remote = self.following.get(url)
            if remote is None:
                remote = Remote(url, actor["inbox"], _secrets.token_hex(32), actor.get("name", ""))
                self.following[url] = remote
            else:
                remote.inbox = actor["inbox"]
                remote.name = actor.get("name", remote.name)
                remote.state = "pending"
            remote.network = net.id if net else None
            self._save()
        extra = {"network": net.id} if net else {}
        activity = self._activity("Follow", url, secret=remote.secret, inbox=self.inbox, name=self.name, **extra)
        # In a network the Follow is signed with its secret; under a shared
        # federation secret with that; in the open case with the pair
        # secret, which proves nothing yet but keeps every message signed.
        ok, error, _ = self.deliver(remote, activity, net.secret if net else (self.secret or remote.secret))
        if not ok:
            remote.last_error = error
        elif remote.state == "accepted":
            self.announce_up(force=True)  # tell it about us, and hear the directory, now
        return remote

    def unfollow(self, url: str) -> bool:
        url = url.rstrip("/")
        with self._lock:
            remote = self.following.pop(url, None)
            self._save()
        if remote is None:
            return False
        self.deliver(remote, self._activity("Undo", {"type": "Follow", "actor": self.id, "object": url}))
        return True

    def _due(self, last: tuple[str, float], announcement: dict[str, Any], force: bool) -> tuple[bool, str]:
        with self._lock:
            # Every network's directory: a change in any is news for the links in it.
            heard = sorted((e.get("network") or "", e["id"], e["trees"]) for e in self.directory_entries.values())
        digest = hashlib.sha256(canonical({"trees": [{k: t.get(k) for k in ("name", "version", "triggers", "inputs", "language", "hosts")} for t in announcement["trees"]], "directory": [(e["id"], e["trees"]) for e in announcement["directory"]], "heard": heard})).hexdigest()
        due = force or digest != last[0] or time.time() - last[1] >= ANNOUNCE_EVERY
        return due, digest

    def announce(self, force: bool = False) -> int:
        """Tell every follower what this hub's own services hold and what
        the directory says: when either changed, on a heartbeat, or
        ``force``. Returns how many deliveries succeeded."""
        announcement = self._announcement()
        due, digest = self._due(self._announced, announcement, force)
        if not due or not self.followers:
            return 0
        self._announced = (digest, time.time())
        delivered = 0
        for remote in list(self.followers.values()):
            ok, error, _ = self.deliver(remote, self._activity("Announce", self._announcement(remote)))
            if ok:
                delivered += 1
                remote.last_ok = time.time()
                remote.last_error = None
            else:
                remote.last_error = error
        return delivered

    def announce_up(self, force: bool = False) -> int:
        """Tell every hub this one follows what it holds and what the
        directory says. The followed hub routes none of it (being followed
        grants nothing); it keeps the directory and, if this hub is
        listed, finds it there."""
        announcement = self._announcement()
        due, digest = self._due(self._announced_up, announcement, force)
        accepted = [r for r in self.following.values() if r.state == "accepted"]
        if not due or not accepted:
            return 0
        self._announced_up = (digest, time.time())
        delivered = 0
        for remote in accepted:
            ok, _, _ = self.deliver(remote, self._activity("Announce", self._announcement(remote)))
            delivered += ok
        return delivered

    def refresh(self, remote: Remote) -> None:
        """Poll a hub we follow, signed: the fallback when its announces
        do not arrive, and what keeps its link honest."""
        if remote.state != "accepted":
            return
        path = "/capabilities"
        try:
            status, caps = http_json("GET", f"{remote.url}{path}", timeout=5.0, headers=sign(remote.secret, self.id, "GET", path, None))
        except Exception as e:
            remote.last_error = str(e)
            return
        if status == 200 and isinstance(caps, dict):
            self._take_capabilities(remote, caps)
        else:
            remote.last_error = f"capabilities answered {status}"

    def refresh_all(self) -> None:
        for remote in list(self.following.values()):
            self.refresh(remote)
        self.auto()
        self.announce_up()
        self.announce()
        self.replicate()
        self.hand_out()

    # ------------------------------------------------------------ keeping

    def keep(self, key: str, data: Any) -> dict[str, Any]:
        """Keep a blob of our own and see that enough others keep it too."""
        meta = self.store.put_own(key, data)
        with self._lock:
            entry = self.keeping.setdefault(key, {"holders": {}})
            if entry.get("sha256") != meta["sha256"]:
                entry["asked"] = {}
            entry.update({"sha256": meta["sha256"], "size": meta["size"], "updated": meta["updated"]})
            self._save()
        self.replicate(only=key)
        return self.keeping_view(key)

    def confirm(self, actor: str, key: str, sha256: str) -> None:
        with self._lock:
            entry = self.keeping.get(key)
            if entry is None:
                return
            entry["holders"][actor.rstrip("/")] = {"at": time.time(), "sha256": sha256}
            entry.get("asked", {}).pop(actor.rstrip("/"), None)
            self._save()

    def holders(self, key: str) -> list[str]:
        """Who holds the current version of a blob, confirmed recently."""
        entry = self.keeping.get(key)
        if not entry:
            return []
        now = time.time()
        return sorted(a for a, h in entry["holders"].items() if h.get("sha256") == entry.get("sha256") and now - float(h.get("at", 0)) < HOLDER_FRESH)

    def keeping_view(self, key: str | None = None) -> Any:
        def view(k: str, e: dict[str, Any]) -> dict[str, Any]:
            return {"key": k, "sha256": e.get("sha256"), "size": e.get("size"), "updated": e.get("updated"), "holders": self.holders(k), "wanted": self.replicas, "known": sorted(e["holders"])}

        with self._lock:
            if key is not None:
                e = self.keeping.get(key)
                return view(key, e) if e else None
            return [view(k, e) for k, e in sorted(self.keeping.items())]

    def keepers(self) -> list[Remote]:
        """Who we may ask to keep a blob: everyone we share a secret with, members first."""
        remotes = [r for r in self.followers.values() if r.state == "accepted"] + [r for r in self.following.values() if r.state == "accepted"]
        remotes.sort(key=lambda r: (r.kind != "member", r.link != "up", r.url))
        return remotes

    def replicate(self, only: str | None = None, limit: int = 20) -> int:
        """Send ``Keep`` for every blob with fewer fresh holders than
        ``replicas``, to keepers that do not hold it, at most ``limit`` at a
        time. Returns how many were sent."""
        sent = 0
        now = time.time()
        for key in ([only] if only else list(self.keeping)):
            entry = self.keeping.get(key)
            blob = self.store.get_own(key)
            if not entry or not blob:
                continue
            asked = entry.setdefault("asked", {})
            # A member confirms later, through its mailbox: count it as covered
            # for a while rather than asking every member at once.
            have = set(self.holders(key)) | {actor for actor, t in asked.items() if now - float(t) < ASK_WINDOW}
            # Someone new before a holder gone stale: the stale one may be gone for good.
            for remote in sorted(self.keepers(), key=lambda r: r.url in entry["holders"]):
                if len(have) >= self.replicas or sent >= limit:
                    break
                if remote.url in have:
                    continue
                ok, _, _ = self.deliver(remote, self._activity("Keep", {"key": key, "sha256": blob["sha256"], "size": blob["size"], "updated": blob["updated"], "data": blob["data"]}))
                if ok:
                    sent += 1
                    have.add(remote.url)
                    if remote.inbox == "mailbox":
                        asked[remote.url] = now
        return sent

    def auto(self) -> None:
        """Keep the automatic blobs (directory, gaps, ...) when they changed."""
        for key, fn in self.auto_keys.items():
            try:
                data = fn()
            except Exception:
                continue
            sha, size = blob_digest(data)
            if size > 256 * 1024 or self._auto_digest.get(key) == sha:
                continue
            self._auto_digest[key] = sha
            try:
                self.keep(key, data)
            except StoreError:
                continue

    def hand_out(self, limit: int = 20) -> int:
        """Give every trusted member the trees it should run (``trees/<name>``
        blobs), each once per version until the member confirms. Returns
        how many were sent."""
        trees = self.handout()
        if not trees:
            return 0
        now = time.time()
        sent = 0
        for remote in list(self.followers.values()):
            if remote.kind != "member" or not remote.trusted or remote.state != "accepted":
                continue
            handed = self._handed.setdefault(remote.url, {})
            for name, tree in trees.items():
                key = f"trees/{name}"
                raw = tree.raw
                sha, size = blob_digest(raw)
                h = handed.get(key)
                if h and h["sha256"] == sha and (h["confirmed"] or now - h["at"] < ASK_WINDOW):
                    continue
                if sent >= limit:
                    return sent
                ok, _, _ = self.deliver(remote, self._activity("Keep", {"key": key, "sha256": sha, "size": size, "updated": now, "data": raw}))
                if ok:
                    handed[key] = {"sha256": sha, "at": now, "confirmed": False}
                    sent += 1
        return sent

    def ask(self, remote: Remote, tree: str, inputs: dict[str, Any], depth: int = 0, timeout: float | None = None) -> dict[str, Any]:
        """Have a trusted member run a tree: an Ask in its mailbox, then wait
        for its Answer (at most ``timeout`` seconds, default ``ask_timeout``;
        a member polls)."""
        timeout = self.ask_timeout if timeout is None else timeout
        run = uuid.uuid4().hex
        pending = {"member": remote.url, "event": threading.Event(), "answer": None}
        self._asks[run] = pending
        try:
            self.deliver(remote, self._activity("Ask", {"run": run, "tree": tree, "inputs": inputs, "depth": depth}))
            if not pending["event"].wait(timeout):
                return {"status": "error", "reason": "member-timeout", "tree": tree, "say": f"{remote.name or remote.url} did not answer within {int(timeout)} s; a member answers when it next polls."}
            answer = dict(pending["answer"] or {})
            answer.setdefault("tree", tree)
            return answer
        finally:
            self._asks.pop(run, None)

    def kept_for(self, actor: str, key: str) -> dict[str, Any] | None:
        """A blob we keep for ``actor``, for it to take back."""
        return self.store.get_kept(actor, key)

    def fetch(self, key: str) -> dict[str, Any]:
        """Our own copy of a blob, or the copy a holder keeps: a hub answers
        at once, a member is asked through its mailbox and answers later."""
        own = self.store.get_own(key)
        if own:
            return {"status": "ok", "source": "own", "blob": own}
        asked: list[str] = []
        entry = self.keeping.get(key, {})
        for actor in sorted(entry.get("holders", {})):
            remote = self.followers.get(actor) or self.following.get(actor)
            if remote is None:
                continue
            if remote.inbox == "mailbox":
                self.deliver(remote, self._activity("Fetch", {"key": key}))
                asked.append(actor)
                continue
            path = f"/kept/{key}"
            try:
                status, blob = http_json("GET", f"{remote.url}{path}", timeout=10.0, headers=sign(remote.secret, self.id, "GET", path, None))
            except Exception:
                continue
            if status == 200 and isinstance(blob, dict) and "data" in blob:
                try:
                    self.store.put_own(key, blob["data"])
                except StoreError:
                    continue
                return {"status": "ok", "source": actor, "blob": self.store.get_own(key)}
        if asked:
            return {"status": "asked", "say": f"{len(asked)} member(s) asked through their mailbox; try again once they have answered.", "asked": asked}
        return {"status": "error", "reason": "not-held", "say": f"Nobody holds {key!r}."}

    def _adopt(self, key: str, blob: Any, who: str) -> bool:
        """Take a blob a keeper hands back, when we have no copy of our own."""
        if not isinstance(blob, dict) or "data" not in blob or self.store.get_own(key):
            return False
        try:
            self.store.put_own(key, blob["data"])
        except StoreError:
            return False
        with self._lock:
            entry = self.keeping.setdefault(key, {"holders": {}})
            meta = self.store.get_own(key) or {}
            entry.update({"sha256": meta.get("sha256"), "size": meta.get("size"), "updated": meta.get("updated")})
            entry["holders"][who] = {"at": time.time(), "sha256": meta.get("sha256")}
            self._save()
        if key == "directory":
            self.merge_directory(blob.get("data"), via=who, keep_tags=True)
        return True

    def _take_capabilities(self, remote: Remote, caps: dict[str, Any]) -> None:
        with self._lock:
            # Only what the hub holds itself: a tree it learned from a hub it
            # follows is two hops away and not ours to route to.
            remote.trees = [t for t in caps.get("trees", []) if isinstance(t, dict) and not t.get("via")]
            remote.name = caps.get("name") or remote.name
            remote.last_ok = time.time()
            remote.last_error = None

    # ------------------------------------------------------------ incoming

    def secret_for(self, actor: str) -> str | None:
        """The secret a request from ``actor`` is signed with. A hub asks the
        hubs it follows, so the pair in which the actor follows us comes
        first; with a mutual follow there are two pairs and this is the one
        the actor signs requests to us with."""
        actor = actor.rstrip("/")
        remote = self.followers.get(actor) or self.following.get(actor)
        return remote.secret if remote else None

    def verify_request(self, method: str, path: str, body: Any, headers: dict[str, str] | None) -> str | None:
        """For a signed request to a routed path: the actor it is from, or
        None (and the caller answers 401)."""
        h = _lower(headers)
        actor = h.get("X-Pyrtan-Actor", "").rstrip("/")
        secret = self.secret_for(actor)
        if not actor or secret is None:
            return None
        return actor if verify(secret, method, path, body, h) is None else None

    def handle_inbox(self, activity: Any, headers: dict[str, str] | None, trusted: bool = False) -> tuple[int, dict[str, Any]]:
        """``trusted`` says the request also carried the operator's token: a
        member that follows that way may run the hub's trees."""
        h = _lower(headers)
        if not isinstance(activity, dict) or activity.get("type") not in ACTIVITY_TYPES or not isinstance(activity.get("actor"), str):
            return 400, {"status": "error", "reason": "bad-activity", "say": f"Send an activity of type {', '.join(ACTIVITY_TYPES)} with an actor."}
        actor = activity["actor"].rstrip("/")
        if h.get("X-Pyrtan-Actor", "").rstrip("/") != actor:
            return 401, {"status": "error", "reason": "unsigned", "say": "The signature's actor must be the activity's actor."}
        kind = activity["type"]
        if activity.get("type") not in UNLOGGED:
            self.inbox_log.append({**_logged(activity), "received": time.time()})
        if kind == "Follow":
            return self._on_follow(actor, activity, h, trusted)
        followed = self.following.get(actor)
        follower = self.followers.get(actor)
        if kind in ("Announce", "Keep", "Kept", "Fetch", "Answer", *CHAT_TYPES):
            # Announce from a hub we follow: its trees are ours to route to;
            # from a follower: only the directory it carries. Keeping and
            # chatting go both ways between any two that share a secret.
            remote = followed or follower
        else:
            remote = followed if kind in ("Accept", "Reject") else follower
        if remote is None:
            return 403, {"status": "error", "reason": "unknown-actor", "say": f"{actor} is not a hub this one follows or is followed by."}
        problem = verify(remote.secret, "POST", "/inbox", activity, h)
        if problem and kind in ("Announce", "Keep", "Kept", "Fetch", "Answer", *CHAT_TYPES) and followed and follower and followed is not follower:
            # Mutual follow: the announce may be signed with the other pair.
            remote = follower if remote is followed else followed
            problem = verify(remote.secret, "POST", "/inbox", activity, h)
        if problem:
            return 401, {"status": "error", "reason": "unsigned", "say": problem}
        if kind in CHAT_TYPES:
            if self.chat is None:
                return 404, {"status": "error", "reason": "no-chat", "say": f"{self.id} keeps no lobby."}
            if remote.kind != "hub":
                return 403, {"status": "error", "reason": "not-a-hub", "say": "Strangers meet hub to hub; a member chats through its hub's lobby."}
            remote.last_ok = time.time()
            return self.chat.on_activity(kind, remote, activity.get("object"), follower=remote is follower)
        if kind == "Accept":
            with self._lock:
                remote.state = "accepted"
                remote.last_ok = time.time()
                remote.last_error = None
                self._save()
            return 200, {"status": "ok"}
        if kind == "Reject":
            with self._lock:
                remote.state = "rejected"
                remote.last_error = str(activity.get("summary") or "the hub refused the follow")
                self._save()
            return 200, {"status": "ok"}
        if kind == "Announce":
            obj = activity.get("object")
            if not isinstance(obj, dict) or not isinstance(obj.get("trees"), list):
                return 400, {"status": "error", "reason": "bad-activity", "say": "An Announce carries {\"trees\": [...]}."}
            if remote is followed:
                self._take_capabilities(remote, obj)
            else:
                with self._lock:
                    remote.name = obj.get("name") or remote.name
                    remote.last_ok = time.time()
                    remote.last_error = None
                    if remote.kind == "member" and remote.trusted:
                        # A trusted member runs the trees we handed it, and only those;
                        # what each tree answers to is our copy's word, not the member's.
                        handed = self.handout()
                        names = {t.get("name") for t in obj.get("trees", []) if isinstance(t, dict)}
                        remote.trees = [handed[n].capability() for n in sorted(names & set(handed))]
            taken = self.merge_directory(obj.get("directory", []), via=actor, network=remote.network)
            self.merge_reports(obj.get("reports", []), via=actor)
            # What it keeps for us that we no longer have: ask for it back.
            wanted = [k for k in obj.get("kept", []) if isinstance(k, str) and not self.store.get_own(k)]
            for k in wanted[:20]:
                self.deliver(remote, self._activity("Fetch", {"key": k}))
            return 200, {"status": "ok", "trees": len(remote.trees) if remote is followed else 0, "merged": taken, "directory": self.gossip(remote.network), "reports": self.report_gossip(), "kept": [b["key"] for b in self.store.list_kept(actor)]}
        if kind == "Keep":
            obj = activity.get("object")
            if not isinstance(obj, dict) or "data" not in obj:
                return 400, {"status": "error", "reason": "bad-activity", "say": "A Keep carries {\"key\", \"sha256\", \"data\"}."}
            try:
                meta = self.store.put_kept(actor, obj.get("key"), obj["data"], sha256=obj.get("sha256"), updated=obj.get("updated"))
            except StoreError as e:
                return 413, {"status": "error", "reason": "not-kept", "say": str(e)}
            remote.last_ok = time.time()
            return 200, {"status": "ok", "kept": meta["key"], "sha256": meta["sha256"]}
        if kind == "Kept":
            obj = activity.get("object")
            if not isinstance(obj, dict) or not isinstance(obj.get("key"), str):
                return 400, {"status": "error", "reason": "bad-activity", "say": "A Kept names a key."}
            if obj["key"].startswith("trees/"):
                with self._lock:
                    handed = self._handed.setdefault(actor, {}).get(obj["key"])
                    if handed and handed.get("sha256") == obj.get("sha256"):
                        handed["confirmed"] = True
            elif "data" in obj:
                self._adopt(obj["key"], obj, actor)
            elif isinstance(obj.get("sha256"), str):
                self.confirm(actor, obj["key"], obj["sha256"])
            remote.last_ok = time.time()
            return 200, {"status": "ok"}
        if kind == "Answer":
            obj = activity.get("object")
            run = obj.get("run") if isinstance(obj, dict) else None
            pending = self._asks.get(run) if isinstance(run, str) else None
            if pending is None or pending["member"] != actor:
                return 404, {"status": "error", "reason": "no-such-ask", "say": "No run waits for this answer from this member."}
            pending["answer"] = obj.get("outcome") if isinstance(obj.get("outcome"), dict) else {"status": "error", "reason": "bad-answer", "say": "The member's answer was not an outcome."}
            pending["event"].set()
            remote.last_ok = time.time()
            return 200, {"status": "ok"}
        if kind == "Fetch":
            obj = activity.get("object")
            key = obj.get("key") if isinstance(obj, dict) else None
            blob = self.store.get_kept(actor, key) if isinstance(key, str) else None
            if blob is None:
                return 404, {"status": "error", "reason": "not-kept", "say": f"Nothing kept here for {actor} under {key!r}."}
            return 200, {"status": "ok", "blob": blob}
        if kind == "Undo":
            with self._lock:
                self.followers.pop(actor, None)
                self.mailboxes.pop(actor, None)
                self._handed.pop(actor, None)
                self.store.drop_kept(actor)
                self._save()
            return 200, {"status": "ok"}
        return 400, {"status": "error", "reason": "bad-activity"}

    def _on_follow(self, actor: str, activity: dict[str, Any], h: dict[str, str], trusted: bool = False) -> tuple[int, dict[str, Any]]:
        pair = activity.get("secret")
        inbox = activity.get("inbox")
        if not isinstance(pair, str) or len(pair) < 16 or not isinstance(inbox, str):
            return 400, {"status": "error", "reason": "bad-activity", "say": "A Follow carries a pair secret and the follower's inbox."}
        nid = activity.get("network")
        net = self.network_by_id(nid)
        if nid is not None:
            # A follow in a network: signed with its secret, and only into a network this hub is in.
            if net is None:
                return 403, {"status": "error", "reason": "not-in-network", "say": "This hub is not in the network this follow names."}
            problem = verify(net.secret, "POST", "/inbox", activity, h)
            if problem:
                return 401, {"status": "error", "reason": "unsigned", "say": f"A follow in {net.name} is signed with its secret: {problem}."}
        elif self.secret:
            problem = verify(self.secret, "POST", "/inbox", activity, h)
            if problem:
                return 401, {"status": "error", "reason": "unsigned", "say": f"This hub takes follows signed with its federation secret: {problem}."}
        elif not self.open_follows:
            return 403, {"status": "error", "reason": "closed", "say": "This hub does not take follows."}
        else:
            problem = verify(pair, "POST", "/inbox", activity, h)
            if problem:
                return 401, {"status": "error", "reason": "unsigned", "say": problem}
        if inbox == "mailbox":
            # A member: a browser or a phone, with no address of its own. It
            # polls its mailbox here, signed, instead of being posted to.
            if not actor.startswith(MEMBER_PREFIX) or len(actor) < len(MEMBER_PREFIX) + 16:
                return 400, {"status": "error", "reason": "bad-activity", "say": f"A member's id is {MEMBER_PREFIX}<at least 16 characters>."}
            name = str(activity.get("name") or "")[:80]
            kind = "member"
        else:
            # The inbox must be the actor's own: ask the actor.
            try:
                status, doc = http_json("GET", f"{actor}/actor", timeout=10.0)
            except Exception as e:
                return 400, {"status": "error", "reason": "no-actor", "say": f"{actor}/actor did not answer: {e}"}
            if status != 200 or not isinstance(doc, dict) or doc.get("id", "").rstrip("/") != actor or doc.get("inbox") != inbox:
                return 400, {"status": "error", "reason": "no-actor", "say": f"{actor}/actor does not describe this follower."}
            name = str(activity.get("name") or doc.get("name") or "")
            kind = "hub"
        with self._lock:
            remote = Remote(actor, inbox, pair, name, state="accepted", last_ok=time.time(), kind=kind, trusted=bool(trusted and kind == "member"), network=net.id if net else None)
            self.followers[actor] = remote
            self._handed.pop(actor, None)
            self._save()
        self.deliver(remote, self._activity("Accept", {"type": "Follow", "actor": actor, "object": self.id}))
        self.deliver(remote, self._activity("Announce", self._announcement(remote)))
        if remote.trusted:
            self.hand_out()
        return 202, {"status": "ok", "say": f"{self.id} accepts {actor}.", "mailbox": f"{self.id}/mailbox" if kind == "member" else None, "trusted": remote.trusted}

    # ------------------------------------------------------------ views

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self.id, "listed": self.listed, "following": [r.to_dict() for r in self.following.values()], "followers": [r.to_dict() for r in self.followers.values()], "directory": len({e["id"] for e in self.directory_entries.values()}) + (1 if self.listed else 0), "keeping": len(self.keeping), "store": self.store.usage(), "replicas": self.replicas, "networks": [{"name": n.name, "id": n.id, "public": n.public} for n in self.networks.values()]}
