"""Federation: hubs that follow each other.

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

A hub may also be a *rendezvous*: a public hub others follow to try
federation and, if they ask to be ``listed``, to be found. ``GET
/directory`` names the listed followers, with the trees they hold (the
rendezvous reads a listed follower's ``/capabilities``, signed, because
listing is asking to be seen). The directory is discovery only: a
rendezvous routes to nobody it did not follow, like any hub.

What this module refuses: open relaying (no hop beyond one), asymmetric
secrets (there is no key infrastructure to lean on in the standard
library, and a pair secret is enough for hubs whose operators chose each
other), and any routing decision that was not an operator's follow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as _secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .service import bearer_ok, http_json

DEFAULT_RENDEZVOUS = "https://hub.pyrtan.com"
SIGNATURE_WINDOW = 300.0   # seconds a signed message stays valid
MAX_HOPS = 1               # federation is one hop; raise with care
STALE_AFTER = 30.0
DOWN_AFTER = 90.0
ACTIVITY_TYPES = ("Follow", "Accept", "Reject", "Undo", "Announce")
SIGNED_PATHS = ("/inbox", "/run", "/intent", "/resume", "/capabilities")


def rendezvous_url() -> str:
    """The rendezvous `pyrtan follow` and `pyrtan directory` use when given
    no URL: ``$PYRTAN_RENDEZVOUS`` or the one at pyrtan.com."""
    import os

    return os.environ.get("PYRTAN_RENDEZVOUS") or DEFAULT_RENDEZVOUS


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
    listed: bool = False        # a follower that asked to appear in our directory

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
        return {"url": self.url, "name": self.name, "kind": "hub", "state": self.state, "link": self.link, "trees": [t["name"] for t in self.trees], "last_ok": self.last_ok, "last_error": self.last_error, "listed": self.listed}

    def to_state(self) -> dict[str, Any]:
        return {"url": self.url, "inbox": self.inbox, "secret": self.secret, "name": self.name, "state": self.state, "since": self.since, "listed": self.listed}

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "Remote":
        return cls(d["url"], d["inbox"], d["secret"], d.get("name", ""), d.get("state", "pending"), since=float(d.get("since", time.time())), listed=bool(d.get("listed", False)))


class Federation:
    def __init__(self, hub_id: str, *, name: str = "hub", secret: str | None = None, state: str | Path | None = None, open_follows: bool = True, capabilities: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.id = hub_id.rstrip("/")
        self.name = name
        self.secret = secret or None          # a shared federation secret: Follows must be signed with it
        self.open_follows = open_follows      # without a secret, whether strangers may follow
        self.state_path = Path(state) if state else None
        self.capabilities = capabilities or (lambda: [])
        self.following: dict[str, Remote] = {}
        self.followers: dict[str, Remote] = {}
        self.outbox: deque[dict[str, Any]] = deque(maxlen=50)
        self.inbox_log: deque[dict[str, Any]] = deque(maxlen=50)
        self._announced: str | None = None
        self._lock = threading.RLock()
        if self.state_path and self.state_path.exists():
            self._load()

    # ------------------------------------------------------------ actor

    @property
    def inbox(self) -> str:
        return f"{self.id}/inbox"

    def actor(self) -> dict[str, Any]:
        return {"type": "Hub", "id": self.id, "name": self.name, "inbox": self.inbox, "capabilities": f"{self.id}/capabilities", "pyrtan": __version__, "follows": "open" if (self.open_follows and not self.secret) else "secret"}

    # ------------------------------------------------------------ state

    def _save(self) -> None:
        if not self.state_path:
            return
        data = {"following": [r.to_state() for r in self.following.values()], "followers": [r.to_state() for r in self.followers.values()]}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _load(self) -> None:
        assert self.state_path
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for d in data.get("following", []):
            self.following[d["url"]] = Remote.from_state(d)
        for d in data.get("followers", []):
            self.followers[d["url"]] = Remote.from_state(d)

    # ------------------------------------------------------------ outgoing

    def _activity(self, type_: str, object_: Any, **extra: Any) -> dict[str, Any]:
        return {"type": type_, "id": f"{self.id}/activities/{uuid.uuid4()}", "actor": self.id, "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "object": object_, **extra}

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
        return ok, None if ok else f"inbox answered {status}: {answer}"

    def follow(self, url: str, listed: bool = False) -> Remote:
        """Follow the hub at ``url``: fetch its actor, make a pair secret,
        send the Follow. The remote is pending until it Accepts. ``listed``
        asks the hub to show this one in its directory."""
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
        activity = self._activity("Follow", url, secret=remote.secret, inbox=self.inbox, name=self.name, listed=bool(listed))
        # Under a shared federation secret the Follow is signed with it; in
        # the open case with the pair secret, which proves nothing yet but
        # keeps every message signed.
        ok, error = self.deliver(remote, activity, self.secret or remote.secret)
        if not ok:
            remote.last_error = error
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

    def announce(self, force: bool = False) -> int:
        """Tell every follower what this hub's own services hold, when it
        changed (or ``force``). Returns how many deliveries succeeded."""
        caps = self.capabilities()
        digest = hashlib.sha256(canonical([{k: t.get(k) for k in ("name", "version", "triggers", "inputs", "language", "hosts")} for t in caps])).hexdigest()
        if digest == self._announced and not force:
            return 0
        self._announced = digest
        activity = self._activity("Announce", {"type": "Capabilities", "name": self.name, "trees": caps})
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

    def refresh_listed(self, remote: Remote) -> None:
        """Read what a listed follower holds, for the directory. Signed with
        the pair secret: the follower accepts a request from the hub it
        follows. Nothing is routed there; this is what it asked to show."""
        if not remote.listed or remote.state != "accepted":
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
        for remote in list(self.followers.values()):
            self.refresh_listed(remote)

    def directory(self) -> dict[str, Any]:
        """The listed followers: where to find hubs to follow."""
        with self._lock:
            hubs = [
                {"id": r.url, "name": r.name, "since": r.since, "link": r.link, "trees": [t.get("name") for t in r.trees if isinstance(t, dict) and t.get("name")]}
                for r in self.followers.values()
                if r.listed and r.state == "accepted"
            ]
        return {"rendezvous": self.id, "name": self.name, "hubs": sorted(hubs, key=lambda h: (h["link"] != "up", h["since"]))}

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
        remote = self.following.get(actor) if kind in ("Accept", "Reject", "Announce") else self.followers.get(actor)
        if remote is None:
            return 403, {"status": "error", "reason": "unknown-actor", "say": f"{actor} is not a hub this one follows or is followed by."}
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
            self._take_capabilities(remote, obj)
            return 200, {"status": "ok", "trees": len(remote.trees)}
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
            remote = Remote(actor, inbox, pair, str(activity.get("name") or doc.get("name") or ""), state="accepted", last_ok=time.time(), listed=activity.get("listed") is True)
            self.followers[actor] = remote
            self._save()
        self.deliver(remote, self._activity("Accept", {"type": "Follow", "actor": actor, "object": self.id}))
        self.deliver(remote, self._activity("Announce", {"type": "Capabilities", "name": self.name, "trees": self.capabilities()}))
        return 202, {"status": "ok", "say": f"{self.id} accepts {actor}."}

    # ------------------------------------------------------------ views

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self.id, "following": [r.to_dict() for r in self.following.values()], "followers": [r.to_dict() for r in self.followers.values()]}
