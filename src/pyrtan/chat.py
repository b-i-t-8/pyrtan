"""The lobby: talk to a stranger or a bot, Omegle-style.

A hub started with ``--chat`` keeps a lobby. Anyone who can reach the hub
(with its token, when it has one) takes a *seat*, says whom they want to
meet (``anyone``, ``people`` or a ``bot``) and a few interests, and is
paired at random: with someone else waiting here, with someone waiting at
a hub this one follows, or with a bot. Nobody has a name. Either side can
end it at any time and take the next stranger.

**A bot is not a model.** It is one holder of trees on the network, picked
at random (this hub's own trees, one of its services, a trusted member, a
hub it follows), preferring one whose trees share a word with the seat's
interests. What the stranger says to it is read against that holder's
triggers; when none answers, it is asked of the whole swarm; when nobody
answers, the bot says so and names the tree that would, exactly as
``/intent`` does (and the sentence is recorded as unanswered, like any
other). An ``ask`` node's question is the bot's next line, and what the
stranger says next is the answer. A sentence that is nothing but greeting
or politeness (the reading drops all of it) gets the bot's introduction and
asks the swarm nothing. Every bot line carries its trace.

**Strangers across hubs.** A seat that finds nobody waiting here asks the
hubs this one follows, a signed ``Meet`` each, until one pairs it with a
seat waiting there. From then on each side's lines travel as ``Say`` and
the end as ``Part``, both ways, signed with the pair's secret like every
activity. A hub answers a ``Meet`` only from a hub that follows it, pairs
it only with its own seats, and never passes one on: one hop, as
everywhere in the federation.

Endpoints, all POST with a JSON body except the first:

``GET  /chat``        how many are waiting and talking; whether bots and other hubs are in reach
``POST /chat/start``  ``{"want": "anyone"|"people"|"bot", "interests": [...]}``: a seat
``POST /chat/poll``   ``{"seat", "after": n, "wait": s}``: the events after ``n``, waiting up to ``s`` s for one
``POST /chat/say``    ``{"seat", "text"}``
``POST /chat/next``   ``{"seat"}``: end this conversation and wait for the next, with the same wishes
``POST /chat/leave``  ``{"seat"}``

What it refuses: keeping anything. A conversation lives in memory while
someone is in it; a seat nobody polls for a minute is let go and its
partner told. A federated ``Say`` appears in the hub's outbox and inbox
logs without its text. There are no names, no history, no accounts and
no moderation but leaving: a seat says at most five lines in a burst and
one a second after that, each at most a thousand characters.
"""

from __future__ import annotations

import random
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    from .federation import Remote
    from .hub import Hub

WANTS = ("anyone", "people", "bot")
SEAT_IDLE = 60.0      # a seat nobody polls for this long is let go
BOT_AFTER = 8.0       # an "anyone" seat that meets no stranger in this long meets a bot
MEET_EVERY = 5.0      # how often a waiting seat asks the hubs this one follows
MEET_HUBS = 3         # how many of them it asks each time
POLL_MAX = 25.0
LINE_MAX = 1000
EVENTS_MAX = 200      # events a seat holds for a client that has not polled
SEATS_MAX = 500
INTERESTS_MAX = 5
BURST, REFILL = 5.0, 1.0   # lines: a burst of five, then one a second
REF = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
INTEREST = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def interests_of(value: Any) -> list[str]:
    """At most five lowercase words, from a list or a comma-separated string."""
    words = value.replace(",", " ").split() if isinstance(value, str) else value if isinstance(value, list) else []
    out: list[str] = []
    for w in words:
        w = str(w).strip().lower().lstrip("#")
        if INTEREST.match(w) and w not in out:
            out.append(w)
    return out[:INTERESTS_MAX]


def sample(tree: dict[str, Any]) -> str | None:
    """A sentence a tree answers to, from its first trigger: the first of
    each choice, optional words included, a slot's default where it has
    one. None when a slot has no default (there is nothing sensible to put)."""
    pattern = (tree.get("triggers") or [None])[0]
    if not isinstance(pattern, str):
        return None
    inputs = tree.get("inputs") or {}
    missing = False

    def slot(m: re.Match[str]) -> str:
        nonlocal missing
        default = (inputs.get(m.group(1)) or {}).get("default")
        if default is None:
            missing = True
            return ""
        return str(default)

    text = re.sub(r"[\[(]([^|\])]*)(?:\|[^\])]*)?[\])]", r"\1", pattern)
    text = re.sub(r"\{(\w+)(?::\w+)?\}", slot, text)
    text = re.sub(r"\s+", " ", text).strip()
    return None if missing or not text else text


def _words(tree: dict[str, Any]) -> set[str]:
    """The words a tree is about: its name, description, tags and triggers."""
    text = " ".join([str(tree.get("name", "")), str(tree.get("description", "")), *map(str, tree.get("tags") or []), *map(str, tree.get("triggers") or [])])
    return set(re.findall(r"[a-z0-9]+", text.lower()))


@dataclass
class Bot:
    """A holder of trees, as a partner: who it is, and the question it is waiting on."""

    url: str
    name: str
    where: str
    trees: list[dict[str, Any]]
    liked: list[dict[str, Any]] = field(default_factory=list)   # its trees that share a word with the seat's interests
    snapshot: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def samples(self, prefer: list[dict[str, Any]] | None = None) -> list[str]:
        """What to suggest saying: from the preferred trees when any has a sample."""
        for trees in (prefer or [], self.trees):
            found = [s for s in (sample(t) for t in trees) if s]
            if found:
                return found
        return []

    def view(self) -> dict[str, Any]:
        return {"kind": "bot", "name": self.name, "where": self.where, "trees": [t.get("name") for t in self.trees]}


@dataclass
class Far:
    """A stranger at another hub: that hub, and what it calls their seat."""

    remote: "Remote"
    ref: str
    hub: str

    def view(self) -> dict[str, Any]:
        return {"kind": "stranger", "hub": self.hub}


@dataclass
class Seat:
    id: str           # what the client holds: knowing it is being this seat
    ref: str          # what another hub calls it; never the id
    want: str
    interests: list[str]
    since: float = field(default_factory=time.time)
    seen: float = field(default_factory=time.time)
    state: str = "waiting"   # waiting | talking | ended | left
    partner: Union["Seat", Bot, Far, None] = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=EVENTS_MAX))
    n: int = 0
    asked: float = 0.0       # when it last asked the hubs we follow
    busy: bool = False       # asking them now: nobody else may pair it meanwhile
    botless: bool = False    # told there is no bot to meet
    allowance: float = BURST
    allowance_at: float = field(default_factory=time.time)

    def view(self) -> dict[str, Any]:
        p = self.partner
        return {"state": self.state, "want": self.want, "interests": self.interests, "partner": {"kind": "stranger", "hub": None} if isinstance(p, Seat) else p.view() if p is not None else None}


class Lobby:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.seats: dict[str, Seat] = {}
        self.refs: dict[str, Seat] = {}
        self.cond = threading.Condition(threading.RLock())
        self.bot_after = BOT_AFTER
        self.meet_every = MEET_EVERY
        self.seat_idle = SEAT_IDLE
        self._parts: list[tuple["Remote", str, str]] = []   # Parts to send once the lock is let go: (remote, to, from)

    # ------------------------------------------------------------ views

    def stats(self) -> dict[str, Any]:
        with self.cond:
            self._sweep()
            seats = list(self.seats.values())
        fed = self.hub.federation
        return {
            "status": "ok",
            "waiting": sum(1 for s in seats if s.state == "waiting"),
            "talking": sum(1 for s in seats if s.state == "talking"),
            "bots": sum(1 for h in self.hub.holders() if h.link != "down" and h.trees),
            "hubs": sum(1 for r in fed.following.values() if r.link != "down") if fed is not None else 0,
        }

    def _view(self, seat: Seat, after: int) -> dict[str, Any]:
        events = [e for e in seat.events if e["n"] > after]
        return {"status": "ok", "seat": seat.id, **seat.view(), "events": events, "after": seat.n}

    # ------------------------------------------------------------ events

    def _add(self, seat: Seat, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        seat.n += 1
        event = {"n": seat.n, "at": time.time(), "kind": kind, "text": text, **extra}
        seat.events.append(event)
        self.cond.notify_all()
        return event

    def _say_paired(self, a: Seat, where: str | None, common: list[str]) -> None:
        self._add(a, "system", f"You're now chatting with a random stranger{' on ' + where if where else ''}. Say hi!")
        if common:
            self._add(a, "system", f"You both like {', '.join(common)}.")

    # ------------------------------------------------------------ pairing

    def _pair(self, a: Seat, b: Seat) -> None:
        common = [w for w in a.interests if w in b.interests]
        for x, y in ((a, b), (b, a)):
            x.partner, x.state = y, "talking"
            self._say_paired(x, None, common)

    def _waiting(self, interests: list[str], exclude: Seat | None = None) -> Seat | None:
        """Someone waiting here to pair with: one who wants people, the most
        interests in common first, at random among equals."""
        found = [s for s in self.seats.values() if s is not exclude and s.state == "waiting" and not s.busy and s.want != "bot"]
        if not found:
            return None
        best = max(len(set(s.interests) & set(interests)) for s in found)
        return random.choice([s for s in found if len(set(s.interests) & set(interests)) == best])

    def _pair_bot(self, seat: Seat) -> bool:
        holders = [h for h in self.hub.holders() if h.link != "down" and h.trees]
        if not holders:
            if seat.want == "bot":
                seat.state = "ended"
                self._add(seat, "system", "No bots here: this hub holds no trees and reaches nobody who does.")
            elif not seat.botless:
                seat.botless = True
                self._add(seat, "system", "No bots here either; still looking for a person.")
            return False
        likes = set(seat.interests)

        def score(h: Any) -> int:
            return len(set().union(*(_words(t) for t in h.trees)) & likes)

        best = max(score(h) for h in holders)
        holder = random.choice([h for h in holders if score(h) == best])
        kind = getattr(holder, "kind", "service")
        where = {"local": "this hub's own trees", "member": "a device that is a member of this hub", "hub": "a hub this one follows"}.get(kind, "a service of this hub")
        name = self.hub.name if kind == "local" else (holder.name or holder.url)
        bot = Bot(holder.url, name, where, list(holder.trees), liked=[t for t in holder.trees if _words(t) & likes])
        seat.partner, seat.state = bot, "talking"
        self._add(seat, "system", self._introduction(bot, first=True))
        return True

    def _introduction(self, bot: Bot, *, first: bool = False) -> str:
        n = len(bot.trees)
        head = f"You're now chatting with a bot: {bot.name}, {bot.where}." if first else f"I'm {bot.name}, {bot.where}."
        tries = bot.samples(bot.liked)
        try_line = f" Try “{random.choice(tries)}”." if tries else ""
        return f"{head} It is {n} tree{'s' if n != 1 else ''}, not a person and not a model, and every line it says comes with the path that produced it.{try_line}"

    def _advance(self, seat: Seat) -> None:
        """What a waiting seat does next: meet someone here, ask the hubs we
        follow, or, when it wants anyone and has waited long enough, a bot."""
        fed = self.hub.federation
        with self.cond:
            self._sweep()
            if seat.state != "waiting" or seat.busy or seat.id not in self.seats:
                return
            if seat.want == "bot":
                self._pair_bot(seat)
                return
            other = self._waiting(seat.interests, exclude=seat)
            if other is not None:
                self._pair(seat, other)
                return
            now = time.time()
            far = fed is not None and bool(fed.following) and now - seat.asked >= self.meet_every
            if far:
                seat.busy, seat.asked = True, now
        if far:
            try:
                if self._meet_far(seat):
                    return
            finally:
                with self.cond:
                    seat.busy = False
                    self.cond.notify_all()
        with self.cond:
            if seat.state == "waiting" and not seat.busy and seat.want == "anyone" and time.time() - seat.since >= self.bot_after:
                self._pair_bot(seat)

    def _meet_far(self, seat: Seat) -> bool:
        fed = self.hub.federation
        assert fed is not None
        hubs = [r for r in fed.following.values() if r.kind == "hub" and r.state == "accepted" and r.link != "down"]
        random.shuffle(hubs)
        ref = seat.ref
        for remote in hubs[:MEET_HUBS]:
            ok, _, answer = fed.deliver(remote, fed._activity("Meet", {"ref": ref, "interests": seat.interests}))
            if not (ok and isinstance(answer, dict) and answer.get("met") is True and isinstance(answer.get("ref"), str) and REF.match(answer["ref"])):
                continue
            with self.cond:
                if seat.state == "waiting" and seat.id in self.seats and seat.ref == ref:
                    seat.partner, seat.state = Far(remote, answer["ref"], remote.name or remote.url), "talking"
                    self._say_paired(seat, remote.name or remote.url, interests_of(answer.get("interests")))
                    return True
            # It left, or moved on, while we asked: the stranger over there is told at once.
            self._send_part(remote, answer["ref"], ref)
            return True
        return False

    # ------------------------------------------------------------ ending

    def _end(self, seat: Seat) -> None:
        """End the seat's conversation, and tell its partner."""
        p = seat.partner
        if isinstance(p, Seat):
            p.partner, p.state = None, "ended"
            self._add(p, "system", "Stranger has disconnected.")
        elif isinstance(p, Far):
            self._parts.append((p.remote, p.ref, seat.ref))
        seat.partner = None
        if seat.state == "talking":
            seat.state = "ended"
            self._add(seat, "system", "You have disconnected.")

    def _sweep(self) -> None:
        cutoff = time.time() - self.seat_idle
        for seat in [s for s in self.seats.values() if s.seen < cutoff]:
            self._end(seat)
            self._drop(seat)

    def _drop(self, seat: Seat) -> None:
        self.seats.pop(seat.id, None)
        self.refs.pop(seat.ref, None)
        seat.state = "left"
        self.cond.notify_all()

    def _send_part(self, remote: "Remote", to: str, frm: str) -> None:
        fed = self.hub.federation
        if fed is not None:
            fed.deliver(remote, fed._activity("Part", {"to": to, "from": frm}))

    def _flush(self) -> None:
        with self.cond:
            parts, self._parts = self._parts, []
        for remote, to, frm in parts:
            self._send_part(remote, to, frm)

    # ------------------------------------------------------------ requests

    def _seat(self, body: dict[str, Any]) -> Seat | None:
        with self.cond:
            seat = self.seats.get(str(body.get("seat", "")))
            if seat is not None:
                seat.seen = time.time()
            return seat

    def start(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        want = body.get("want", "anyone")
        if want not in WANTS:
            return 400, {"status": "error", "reason": "bad-request", "say": f"want is one of {', '.join(WANTS)}."}
        with self.cond:
            self._sweep()
            if len(self.seats) >= SEATS_MAX:
                return 503, {"status": "error", "reason": "lobby-full", "say": f"The lobby is full ({SEATS_MAX} seats); try again in a minute."}
            seat = Seat(secrets.token_urlsafe(18), secrets.token_urlsafe(12), want, interests_of(body.get("interests")))
            self.seats[seat.id] = seat
            self.refs[seat.ref] = seat
            self._add(seat, "system", "Looking for a bot…" if want == "bot" else "Looking for someone you can chat with…")
        self._advance(seat)
        with self.cond:
            return 200, self._view(seat, 0)

    def poll(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seat = self._seat(body)
        if seat is None:
            return 404, _gone()
        after = _int(body.get("after"))
        try:
            wait = min(max(float(body.get("wait") or 0), 0.0), POLL_MAX)
        except (TypeError, ValueError):
            return 400, {"status": "error", "reason": "bad-request", "say": "wait is a number of seconds."}
        deadline = time.time() + wait
        while True:
            with self.cond:
                seat.seen = time.time()  # polling is being here, before anyone is swept
            self._advance(seat)
            self._flush()
            with self.cond:
                now = time.time()
                # A seat that left while this poll waited gets its last view, state "left".
                if seat.id not in self.seats or any(e["n"] > after for e in seat.events) or now >= deadline:
                    return 200, self._view(seat, after)
                nap = deadline - now
                if seat.state == "waiting":
                    # Wake to ask the hubs we follow again, or to meet a bot.
                    if self.hub.federation is not None and self.hub.federation.following:
                        nap = min(nap, seat.asked + self.meet_every - now)
                    if seat.want == "anyone":
                        nap = min(nap, seat.since + self.bot_after - now)
                self.cond.wait(max(0.05, nap))

    def say(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seat = self._seat(body)
        if seat is None:
            return 404, _gone()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"seat\": \"…\", \"text\": \"…\"}."}
        text = text.strip()
        if len(text) > LINE_MAX:
            return 413, {"status": "error", "reason": "too-long", "say": f"A line is at most {LINE_MAX} characters."}
        with self.cond:
            if seat.state != "talking" or seat.partner is None:
                return 409, {"status": "error", "reason": "not-talking", "say": "Nobody is listening: wait for a stranger, or press New."}
            now = time.time()
            seat.allowance = min(BURST, seat.allowance + (now - seat.allowance_at) * REFILL)
            seat.allowance_at = now
            if seat.allowance < 1:
                return 429, {"status": "error", "reason": "too-fast", "say": "Slow down: five lines at once, then one a second."}
            seat.allowance -= 1
            event = self._add(seat, "you", text)
            partner = seat.partner
            if isinstance(partner, Seat):
                self._add(partner, "stranger", text)
        if isinstance(partner, Far):
            fed = self.hub.federation
            assert fed is not None
            ok, error, _ = fed.deliver(partner.remote, fed._activity("Say", {"to": partner.ref, "from": seat.ref, "text": text}))
            if not ok:
                with self.cond:
                    if seat.partner is partner:
                        seat.partner, seat.state = None, "ended"
                        self._add(seat, "system", f"The stranger's hub did not take that line ({error}); the conversation is over.")
        elif isinstance(partner, Bot):
            threading.Thread(target=self._bot_reply, args=(seat, partner, text), daemon=True).start()
        return 200, {"status": "ok", "n": event["n"]}

    def next(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seat = self._seat(body)
        if seat is None:
            return 404, _gone()
        with self.cond:
            self._end(seat)
            self.refs.pop(seat.ref, None)
            seat.ref = secrets.token_urlsafe(12)   # the last stranger cannot reach the next conversation
            self.refs[seat.ref] = seat
            seat.state, seat.since, seat.asked = "waiting", time.time(), 0.0
            seat.botless = False
            self._add(seat, "system", "Looking for a bot…" if seat.want == "bot" else "Looking for someone you can chat with…")
        self._flush()
        self._advance(seat)
        with self.cond:
            return 200, self._view(seat, _int(body.get("after")))

    def leave(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seat = self._seat(body)
        if seat is None:
            return 200, {"status": "ok", "left": False}
        with self.cond:
            self._end(seat)
            self._drop(seat)
        return 200, {"status": "ok", "left": True}

    # ------------------------------------------------------------ bots

    def _bot_reply(self, seat: Seat, bot: Bot, text: str) -> None:
        try:
            self._bot_answer(seat, bot, text)
        except Exception as e:  # a bot that breaks says so, rather than going quiet
            self._bot_line(seat, bot, f"(error: {e})", {"status": "error", "reason": "bot-error"})

    def _bot_answer(self, seat: Seat, bot: Bot, text: str) -> None:
        with bot.lock:  # one line at a time, in order: a question waits for its answer
            if bot.snapshot is not None:
                snapshot, bot.snapshot = bot.snapshot, None
                _, answer = self.hub.resume({"snapshot": snapshot, "answer": text})
            else:
                _, answer = self.hub.intent({"text": text}, only=bot.url)
                if answer.get("reason") == "no-match":
                    reading = (answer.get("output") or {}).get("reading") or {}
                    if reading.get("cleaned") == "":
                        # Nothing but greeting or politeness: the bot says who it is.
                        self._bot_line(seat, bot, self._introduction(bot), {"status": "done", "reason": "greeting"})
                        return
                    _, answer = self.hub.intent({"text": text})  # not its trees: ask the swarm
            if answer.get("status") == "asked" and isinstance(answer.get("snapshot"), dict):
                bot.snapshot = answer["snapshot"]
            line = answer.get("say") or answer.get("question")
            if not line:
                line = f"({answer.get('status')}{': ' + str(answer['reason']) if answer.get('reason') else ''})"
            self._bot_line(seat, bot, str(line), answer)

    def _bot_line(self, seat: Seat, bot: Bot, text: str, answer: dict[str, Any]) -> None:
        with self.cond:
            if seat.partner is not bot:
                return  # the stranger moved on while the bot was thinking
            extra = {k: answer[k] for k in ("status", "reason", "tree", "peer", "via", "output", "trace") if answer.get(k) is not None}
            self._add(seat, "bot", text, **extra)

    # ------------------------------------------------------------ federation

    def on_activity(self, kind: str, remote: "Remote", obj: Any, *, follower: bool) -> tuple[int, dict[str, Any]]:
        """A ``Meet``, ``Say`` or ``Part`` from a hub we share a secret with.
        A ``Meet`` only from one that follows us: being followed is what lets
        a hub ask."""
        try:
            return self._on_activity(kind, remote, obj, follower)
        finally:
            self._flush()

    def _on_activity(self, kind: str, remote: "Remote", obj: Any, follower: bool) -> tuple[int, dict[str, Any]]:
        if not isinstance(obj, dict):
            return 400, {"status": "error", "reason": "bad-activity", "say": f"A {kind} carries an object."}
        if kind == "Meet":
            ref = obj.get("ref")
            if not follower:
                return 403, {"status": "error", "reason": "not-a-follower", "say": "Only a hub that follows this one may ask it for a stranger."}
            if not isinstance(ref, str) or not REF.match(ref):
                return 400, {"status": "error", "reason": "bad-activity", "say": "A Meet carries the asking seat's ref."}
            interests = interests_of(obj.get("interests"))
            with self.cond:
                self._sweep()
                seat = self._waiting(interests)
                if seat is None:
                    return 200, {"status": "ok", "met": False}
                seat.partner, seat.state = Far(remote, ref, remote.name or remote.url), "talking"
                common = [w for w in seat.interests if w in interests]
                self._say_paired(seat, remote.name or remote.url, common)
                return 200, {"status": "ok", "met": True, "ref": seat.ref, "interests": common}
        to, frm = obj.get("to"), obj.get("from")
        with self.cond:
            seat = self.refs.get(to) if isinstance(to, str) else None
            p = seat.partner if seat is not None else None
            if seat is None or not isinstance(p, Far) or p.remote.url != remote.url or p.ref != frm:
                return 404, {"status": "error", "reason": "no-such-seat", "say": "No conversation here with that stranger."}
            if kind == "Part":
                seat.partner, seat.state = None, "ended"
                self._add(seat, "system", "Stranger has disconnected.")
                return 200, {"status": "ok"}
            text = obj.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > LINE_MAX:
                return 400, {"status": "error", "reason": "bad-activity", "say": f"A Say carries a line of at most {LINE_MAX} characters."}
            self._add(seat, "stranger", text.strip())
            return 200, {"status": "ok"}

    # ------------------------------------------------------------ routing

    def route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/chat":
            try:
                return 200, self.stats()
            finally:
                self._flush()
        handlers = {"/chat/start": self.start, "/chat/poll": self.poll, "/chat/say": self.say, "/chat/next": self.next, "/chat/leave": self.leave}
        if method == "POST" and path in handlers:
            try:
                return handlers[path](body)
            finally:
                self._flush()  # the Parts an ending owes another hub, sent once no lock is held
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gone() -> dict[str, Any]:
    return {"status": "error", "reason": "no-seat", "say": "This seat is gone (it left, or nobody polled it for a minute): start again."}
