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
does, and pyrtan.com publishes only a static list of *seeds*, hubs to
follow first. An entry is a hint (a hub two follows away cannot verify
who said it), kept until its hub has not been heard of for a week, and
routing never uses it: finding a hub in the directory and following it is
still the one act of trust.

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

DEFAULT_SEEDS = "https://pyrtan.com/rendezvous/seeds.json"
DEFAULT_SNAPSHOT = "https://pyrtan.com/rendezvous/directory.json"
SIGNATURE_WINDOW = 300.0   # seconds a signed message stays valid
MAX_HOPS = 1               # federation is one hop; raise with care
STALE_AFTER = 30.0
DOWN_AFTER = 90.0
ANNOUNCE_EVERY = 60.0      # seconds between announces when nothing changed: the directory's heartbeat
DIRECTORY_TTL = 7 * 86400  # an entry unheard of for this long is dropped
DIRECTORY_MAX = 500
HEARD_UP = 600.0           # a directory entry seen this recently reads "up"
HEARD_STALE = 86400.0
ACTIVITY_TYPES = ("Follow", "Accept", "Reject", "Undo", "Announce")
SIGNED_PATHS = ("/inbox", "/run", "/intent", "/resume", "/capabilities")


# ---------------------------------------------------------------- seeds


def seed_urls() -> list[str]:
    """Hubs to follow first, from ``$PYRTAN_RENDEZVOUS`` (one hub),
    ``$PYRTAN_SEEDS`` (a URL or file holding ``{"hubs": [...]}``), or the
    list at pyrtan.com. Never raises: no seeds is an empty list."""
    single = os.environ.get("PYRTAN_RENDEZVOUS")
    if single:
        return [single.rstrip("/")]
    source = os.environ.get("PYRTAN_SEEDS") or DEFAULT_SEEDS
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
    heard first. What a new hub can try when every seed is down."""
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


def bootstrap_candidates() -> list[str]:
    """Where to look for a first hub: the seeds, then the snapshot's hubs."""
    seeds = seed_urls()
    return seeds + [h for h in snapshot_hubs() if h not in seeds]


def first_live_seed(seeds: list[str] | None = None) -> str | None:
    """The first seed whose ``/actor`` answers as a hub; when none does, the
    first hub from the snapshot that answers; else None."""
    tried = seeds if seeds is not None else seed_urls()
    for batch in ([tried] if seeds is not None else [tried, lambda: [h for h in snapshot_hubs() if h not in tried]]):
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
    kind: str = "hub"

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
        return {"url": self.url, "name": self.name, "kind": "hub", "state": self.state, "link": self.link, "trees": [t["name"] for t in self.trees], "last_ok": self.last_ok, "last_error": self.last_error}

    def to_state(self) -> dict[str, Any]:
        return {"url": self.url, "inbox": self.inbox, "secret": self.secret, "name": self.name, "state": self.state, "since": self.since}

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "Remote":
        return cls(d["url"], d["inbox"], d["secret"], d.get("name", ""), d.get("state", "pending"), since=float(d.get("since", time.time())))


def heard(seen: float, now: float | None = None) -> str:
    """How recently a directory entry's hub announced, as gossip carried it."""
    age = (now if now is not None else time.time()) - seen
    return "up" if age < HEARD_UP else "stale" if age < HEARD_STALE else "down"


class Federation:
    def __init__(self, hub_id: str, *, name: str = "hub", secret: str | None = None, state: str | Path | None = None, open_follows: bool = True, capabilities: Callable[[], list[dict[str, Any]]] | None = None, listed: bool = False) -> None:
        self.id = hub_id.rstrip("/")
        self.name = name
        self.secret = secret or None          # a shared federation secret: Follows must be signed with it
        self.open_follows = open_follows      # without a secret, whether strangers may follow
        self.state_path = Path(state) if state else None
        self.capabilities = capabilities or (lambda: [])
        self.listed = listed                  # put ourselves in the directory we gossip
        self.following: dict[str, Remote] = {}
        self.followers: dict[str, Remote] = {}
        self.directory_entries: dict[str, dict[str, Any]] = {}
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
        return {"type": "Hub", "id": self.id, "name": self.name, "inbox": self.inbox, "capabilities": f"{self.id}/capabilities", "directory": f"{self.id}/directory", "pyrtan": __version__, "follows": "open" if (self.open_follows and not self.secret) else "secret", "listed": self.listed}

    # ------------------------------------------------------------ state

    def _save(self) -> None:
        if not self.state_path:
            return
        with self._lock:
            data = {
                "listed": self.listed,
                "following": [r.to_state() for r in self.following.values()],
                "followers": [r.to_state() for r in self.followers.values()],
                "directory": list(self.directory_entries.values()),
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
        for d in data.get("following", []):
            self.following[d["url"]] = Remote.from_state(d)
        for d in data.get("followers", []):
            self.followers[d["url"]] = Remote.from_state(d)
        self.merge_directory(data.get("directory", []), via="state")

    def set_listed(self, listed: bool) -> None:
        with self._lock:
            self.listed = listed
            if not listed:
                self.directory_entries.pop(self.id, None)
            self._save()

    # ------------------------------------------------------------ directory

    def own_entry(self) -> dict[str, Any] | None:
        if not self.listed:
            return None
        return {"id": self.id, "name": self.name, "trees": sorted({t.get("name") for t in self.capabilities() if isinstance(t, dict) and t.get("name")}), "seen": time.time()}

    def gossip(self) -> list[dict[str, Any]]:
        """What this hub tells others about the directory: itself, if
        listed, and every live entry it has heard."""
        now = time.time()
        with self._lock:
            entries = [e for e in self.directory_entries.values() if e["id"] != self.id and now - e["seen"] < DIRECTORY_TTL]
        own = self.own_entry()
        return ([own] if own else []) + sorted(entries, key=lambda e: -e["seen"])

    def merge_directory(self, entries: Any, via: str) -> int:
        """Take entries heard from ``via``: a newer ``seen`` replaces an
        older one, our own entry is never overwritten, nothing from the
        future is believed, and the table stays bounded. Returns how many
        entries changed."""
        if not isinstance(entries, list):
            return 0
        now = time.time()
        changed = 0
        with self._lock:
            for e in entries:
                if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].startswith(("http://", "https://")):
                    continue
                hub_id = e["id"].rstrip("/")
                if hub_id == self.id:
                    continue
                try:
                    seen = min(float(e.get("seen", 0)), now)
                except (TypeError, ValueError):
                    continue
                if now - seen >= DIRECTORY_TTL:
                    continue
                current = self.directory_entries.get(hub_id)
                if current is not None and current["seen"] >= seen:
                    continue
                self.directory_entries[hub_id] = {
                    "id": hub_id,
                    "name": str(e.get("name") or "")[:80],
                    "trees": [str(t)[:80] for t in e.get("trees", []) if isinstance(t, (str, int))][:200],
                    "seen": seen,
                    "via": via,
                }
                changed += 1
            if len(self.directory_entries) > DIRECTORY_MAX:
                for hub_id in sorted(self.directory_entries, key=lambda k: self.directory_entries[k]["seen"])[: len(self.directory_entries) - DIRECTORY_MAX]:
                    del self.directory_entries[hub_id]
        if changed:
            self._save()
        return changed

    def directory(self) -> dict[str, Any]:
        """Every hub this one has heard of that asked to be found, this one
        included if listed. ``link`` is how recently each announced."""
        now = time.time()
        hubs = []
        for e in self.gossip():
            hubs.append({"id": e["id"], "name": e["name"], "trees": e["trees"], "seen": e["seen"], "link": heard(e["seen"], now), "via": e.get("via", "self" if e["id"] == self.id else "")})
        hubs.sort(key=lambda h: (h["id"] != self.id, {"up": 0, "stale": 1, "down": 2}[h["link"]], h["name"], h["id"]))
        return {"hub": self.id, "name": self.name, "listed": self.listed, "count": len(hubs), "hubs": hubs}

    # ------------------------------------------------------------ outgoing

    def _activity(self, type_: str, object_: Any, **extra: Any) -> dict[str, Any]:
        return {"type": type_, "id": f"{self.id}/activities/{uuid.uuid4()}", "actor": self.id, "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "object": object_, **extra}

    def _announcement(self) -> dict[str, Any]:
        return {"type": "Capabilities", "name": self.name, "trees": self.capabilities(), "directory": self.gossip()}

    def deliver(self, remote: Remote, activity: dict[str, Any], secret: str | None = None) -> tuple[bool, str | None]:
        """Post an activity to a remote's inbox, signed with the pair secret
        (or the one given). Returns (delivered, error)."""
        path = "/inbox"
        headers = sign(secret or remote.secret, self.id, "POST", path, activity)
        try:
            status, answer = http_json("POST", remote.inbox, activity, timeout=10.0, headers=headers)
        except Exception as e:
            self.outbox.append({**activity, "to": remote.url, "delivered": False, "error": str(e)})
            return False, str(e)
        ok = status in (200, 202)
        self.outbox.append({**activity, "to": remote.url, "delivered": ok, "error": None if ok else f"{status}: {answer}"})
        if ok and isinstance(answer, dict) and activity.get("type") == "Announce":
            # An inbox answers an Announce with its own gossip: one round trip, both directions.
            self.merge_directory(answer.get("directory"), via=remote.url)
        return ok, None if ok else f"inbox answered {status}: {answer}"

    def follow(self, url: str) -> Remote:
        """Follow the hub at ``url``: fetch its actor, make a pair secret,
        send the Follow. The remote is pending until it Accepts."""
        url = url.rstrip("/")
        if url == self.id:
            raise ValueError("a hub cannot follow itself")
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
            self._save()
        activity = self._activity("Follow", url, secret=remote.secret, inbox=self.inbox, name=self.name)
        # Under a shared federation secret the Follow is signed with it; in
        # the open case with the pair secret, which proves nothing yet but
        # keeps every message signed.
        ok, error = self.deliver(remote, activity, self.secret or remote.secret)
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
        digest = hashlib.sha256(canonical({"trees": [{k: t.get(k) for k in ("name", "version", "triggers", "inputs", "language", "hosts")} for t in announcement["trees"]], "directory": [(e["id"], e["trees"]) for e in announcement["directory"]]})).hexdigest()
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
        activity = self._activity("Announce", announcement)
        delivered = 0
        for remote in list(self.followers.values()):
            ok, error = self.deliver(remote, activity)
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
        activity = self._activity("Announce", announcement)
        delivered = 0
        for remote in accepted:
            ok, _ = self.deliver(remote, activity)
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
        self.announce_up()
        self.announce()

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

    def handle_inbox(self, activity: Any, headers: dict[str, str] | None) -> tuple[int, dict[str, Any]]:
        h = _lower(headers)
        if not isinstance(activity, dict) or activity.get("type") not in ACTIVITY_TYPES or not isinstance(activity.get("actor"), str):
            return 400, {"status": "error", "reason": "bad-activity", "say": f"Send an activity of type {', '.join(ACTIVITY_TYPES)} with an actor."}
        actor = activity["actor"].rstrip("/")
        if h.get("X-Pyrtan-Actor", "").rstrip("/") != actor:
            return 401, {"status": "error", "reason": "unsigned", "say": "The signature's actor must be the activity's actor."}
        kind = activity["type"]
        self.inbox_log.append({**activity, "received": time.time()})
        if kind == "Follow":
            return self._on_follow(actor, activity, h)
        if kind == "Announce":
            # From a hub we follow: its trees are ours to route to. From a
            # follower: only the directory it carries.
            followed = self.following.get(actor)
            follower = self.followers.get(actor)
            remote = followed or follower
        else:
            remote = self.following.get(actor) if kind in ("Accept", "Reject") else self.followers.get(actor)
        if remote is None:
            return 403, {"status": "error", "reason": "unknown-actor", "say": f"{actor} is not a hub this one follows or is followed by."}
        problem = verify(remote.secret, "POST", "/inbox", activity, h)
        if problem and kind == "Announce" and followed and follower and followed is not follower:
            # Mutual follow: the announce may be signed with the other pair.
            remote = follower if remote is followed else followed
            problem = verify(remote.secret, "POST", "/inbox", activity, h)
        if problem:
            return 401, {"status": "error", "reason": "unsigned", "say": problem}
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
            taken = self.merge_directory(obj.get("directory", []), via=actor)
            return 200, {"status": "ok", "trees": len(remote.trees) if remote is followed else 0, "merged": taken, "directory": self.gossip()}
        if kind == "Undo":
            with self._lock:
                self.followers.pop(actor, None)
                self._save()
            return 200, {"status": "ok"}
        return 400, {"status": "error", "reason": "bad-activity"}

    def _on_follow(self, actor: str, activity: dict[str, Any], h: dict[str, str]) -> tuple[int, dict[str, Any]]:
        pair = activity.get("secret")
        inbox = activity.get("inbox")
        if not isinstance(pair, str) or len(pair) < 16 or not isinstance(inbox, str):
            return 400, {"status": "error", "reason": "bad-activity", "say": "A Follow carries a pair secret and the follower's inbox."}
        if self.secret:
            problem = verify(self.secret, "POST", "/inbox", activity, h)
            if problem:
                return 401, {"status": "error", "reason": "unsigned", "say": f"This hub takes follows signed with its federation secret: {problem}."}
        elif not self.open_follows:
            return 403, {"status": "error", "reason": "closed", "say": "This hub does not take follows."}
        else:
            problem = verify(pair, "POST", "/inbox", activity, h)
            if problem:
                return 401, {"status": "error", "reason": "unsigned", "say": problem}
        # The inbox must be the actor's own: ask the actor.
        try:
            status, doc = http_json("GET", f"{actor}/actor", timeout=10.0)
        except Exception as e:
            return 400, {"status": "error", "reason": "no-actor", "say": f"{actor}/actor did not answer: {e}"}
        if status != 200 or not isinstance(doc, dict) or doc.get("id", "").rstrip("/") != actor or doc.get("inbox") != inbox:
            return 400, {"status": "error", "reason": "no-actor", "say": f"{actor}/actor does not describe this follower."}
        with self._lock:
            remote = Remote(actor, inbox, pair, str(activity.get("name") or doc.get("name") or ""), state="accepted", last_ok=time.time())
            self.followers[actor] = remote
            self._save()
        self.deliver(remote, self._activity("Accept", {"type": "Follow", "actor": actor, "object": self.id}))
        self.deliver(remote, self._activity("Announce", self._announcement()))
        return 202, {"status": "ok", "say": f"{self.id} accepts {actor}."}

    # ------------------------------------------------------------ views

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self.id, "listed": self.listed, "following": [r.to_dict() for r in self.following.values()], "followers": [r.to_dict() for r in self.followers.values()], "directory": len(self.directory_entries) + (1 if self.listed else 0)}
