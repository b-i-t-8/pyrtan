"""The lobby: talk to a stranger, an agent or a bot, Omegle-style; or guess which.

A hub started with ``--chat`` keeps a lobby. Anyone who can reach the hub
(with its token, when it has one) takes a *seat*, says whom they want to
meet (``anyone``, ``people``, an ``agent`` or a ``bot``) and a few
interests, and is paired at random: with someone else waiting here, with
someone waiting at a hub this one follows, with an agent, or with a bot.
Nobody has a name. Either side can end it at any time and take the next
stranger.

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

**Chat links (v0.17).** Following is trust: it routes sentences to the
other hub's trees, so a hub follows few. That made the pool of strangers
small. A hub started ``--chat-reach N`` also keeps up to N *chat links*
with other hubs whose lobbies are open: hubs it heard of on the LAN or
in its directory (public networks only), asked with a signed
``ChatLink`` that carries a pair secret, as a ``Follow`` does. A link is
for chat and nothing else: it is not a follow, so nothing is routed over
it, nothing is announced or kept, and no directory travels on it. Both
ends may send ``Meet`` over it, so the two lobbies share their
strangers. A hub accepts a link only when its operator also gave
``--chat-reach`` and its lobby is open to strangers anyway (``--chat-
public``, or a hub with no token), takes at most twice its reach, and
refuses them all when its federation is closed (``--closed`` or a
federation secret). Links live in memory and are made again after a
restart; one whose hub stops answering is dropped.

**Typing.** While a person types, their client says so (``/chat/typing``)
and the stranger sees "Stranger is typing…"; while a bot's trees run, its
partner sees "Bot is thinking…". It is a state, never an event: it goes out
on its own after five seconds without news, and with the next line. It
is shown both ways or not at all: a seat with the indicator off
(``typing_indicator: false``) neither shares its typing nor sees anyone
else's, and a hub started ``--no-chat-typing`` has it off for everyone.
Across hubs it travels as ``Typing``, at most every two seconds, and the
logs leave it out.

**Agents (v0.31).** An agent, the open-weights model a person starts in
the console, may take a seat too (``as: "agent"``, with its name and
model): its console reads the stranger's lines and says its own, through
the same endpoints a person uses. The hub runs no model and asks none: it
relays the lines and labels them. Outside the game an agent is always
named as one (the stranger reads "You're now chatting with an agent", and
its lines come as ``agent``, not ``stranger``); a person who wants
``people`` never meets one, and an agent never meets an agent. A person
who would meet ``anyone`` meets the people here first, then the
strangers of other hubs, then an agent, and a bot last.

**Person or agent? (v0.31).** A seat started ``game: true`` is paired
only with others playing: a person with a person or an agent, at even
odds when both are waiting, and nobody labelled. Each side may guess
once (``/chat/guess``, ``person`` or ``agent``) while they talk or after;
the answer comes when the conversation ends, with what the other side
guessed and the seat's score. The hub knows what a seat is only by what
its client said when it sat down, so the game is as honest as its
players' consoles; outside the game nothing is unlabelled.

**Public lobbies.** The lobby follows the hub's token: a bot runs trees,
and the token is what guards them. A hub started ``--chat-public`` keeps
its token for everything else and opens ``/chat`` to callers without it,
as *guests*: a guest meets people (here, or on a hub this one follows)
and never a bot. ``want: bot`` from a guest is refused, and ``anyone`` is
read as ``people``; a guest may still ask for an agent or play the game,
since an agent runs no tree here. A hub with no token has no guests:
anyone may meet a bot there, as anyone may ask it at ``/intent``.

Endpoints, all POST with a JSON body except the first:

``GET  /chat``           how many are waiting and talking; whether bots, other hubs and typing are in reach
``POST /chat/start``     ``{"want": "anyone"|"people"|"bot"|"agent", "interests": [...], "typing_indicator": true,
                         "game": false, "as": "person"|"agent", "agent": {"name", "model"}}``: a seat
``POST /chat/poll``      ``{"seat", "after": n, "wait": s, "partner_typing": bool}``: the events after ``n``, waiting
                         up to ``s`` s for one, or for the partner's typing to differ from what the client shows
``POST /chat/say``       ``{"seat", "text"}``
``POST /chat/typing``    ``{"seat", "typing": bool}``: this person is (or stopped) typing
``POST /chat/settings``  ``{"seat", "typing_indicator": bool}``
``POST /chat/guess``     ``{"seat", "guess": "person"|"agent"}``: in the game, what the stranger was
``POST /chat/next``      ``{"seat", "game": bool}``: end this conversation and wait for the next, with the same wishes
``POST /chat/leave``     ``{"seat"}``

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
from typing import TYPE_CHECKING, Any, Callable, Union

from .service import http_json

if TYPE_CHECKING:
    from .federation import Remote
    from .hub import Hub

LINK_TRIES = 2          # links tried per round (a round is the hub's poll)
LINK_REFUSED = 86400.0  # a hub that refused a link is not asked again for a day
LINK_FAILS = 3          # Meets in a row a linked hub may fail before the link is dropped

WANTS = ("anyone", "people", "bot", "agent")
GUESSES = ("person", "agent")
SEAT_IDLE = 60.0      # a seat nobody polls for this long is let go
BOT_AFTER = 8.0       # an "anyone" seat that meets no stranger in this long meets a bot
MEET_EVERY = 5.0      # how often a waiting seat asks the hubs this one follows
MEET_HUBS = 3         # how many of them it asks each time
POLL_MAX = 25.0
LINE_MAX = 1000
EVENTS_MAX = 200      # events a seat holds for a client that has not polled
SEATS_MAX = 500
SEATS_RESERVED = 100  # of those, kept for callers with the token or a key granted chat: strangers cannot fill the lobby on the operator
SEATS_PER_ADDRESS = 8 # seats one address may hold, unless it has the token or a granted key
MEET_CROWD = 20       # past this many waiting seats, each asks the hubs we follow less often: the lobby is not an amplifier
INTERESTS_MAX = 5
BURST, REFILL = 5.0, 1.0   # lines: a burst of five, then one a second
TYPING_TTL = 5.0      # an indicator nobody refreshes goes out after this long
TYPING_EVERY = 2.0    # how often "still typing" travels to another hub
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0 and C1 controls, but tab and newline: a line is text, not terminal commands
REF = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
INTEREST = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def is_chat_path(path: str) -> bool:
    return path == "/chat" or path.startswith("/chat/")


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
    typing: bool = True   # whether its hub takes Typing; one from before it refuses the activity

    def view(self) -> dict[str, Any]:
        return {"kind": "stranger", "hub": self.hub}


@dataclass
class Seat:
    id: str           # what the client holds: knowing it is being this seat
    ref: str          # what another hub calls it; never the id
    want: str
    interests: list[str]
    bots: bool = True        # may meet a bot: false for a guest of a public lobby
    since: float = field(default_factory=time.time)
    seen: float = field(default_factory=time.time)
    state: str = "waiting"   # waiting | talking | ended | left
    partner: Union["Seat", Bot, Far, None] = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=EVENTS_MAX))
    n: int = 0
    asked: float = 0.0       # when it last asked the hubs we follow
    busy: bool = False       # asking them now: nobody else may pair it meanwhile
    botless: bool = False    # told there is no bot to meet
    typing: bool = True      # the indicator: share this person's typing, and see the partner's
    heard: float = 0.0       # until when the partner reads as typing (or thinking) to this seat
    told: tuple[bool, float] = (False, 0.0)   # the typing last sent to a partner on another hub, and when
    allowance: float = BURST
    allowance_at: float = field(default_factory=time.time)
    heard_allowance: float = BURST                              # the same limit on lines from a stranger on another hub
    heard_allowance_at: float = field(default_factory=time.time)
    address: str | None = None   # where it came from, unless the caller had the token or a granted key
    polls: int = 0               # the newest poll's number: an older poll still waiting gives way to it
    agent: dict[str, str] | None = None   # an agent's seat: its name and model, as its console said; None for a person
    game: bool = False       # playing "person or agent?": paired only with others playing, nobody labelled
    round: "Round | None" = None          # this seat's game with its partner, until it is revealed
    score: list[int] = field(default_factory=lambda: [0, 0])   # games guessed right, and guessed: while it sits
    noted: bool = False      # told there is nobody of the kind it wants to meet, yet

    def view(self) -> dict[str, Any]:
        p = self.partner
        if isinstance(p, Seat):
            partner = {"kind": "agent", "hub": None, **p.agent} if p.agent is not None and not self.game else {"kind": "stranger", "hub": None}
        else:
            partner = p.view() if p is not None else None
        out = {"state": self.state, "want": self.want, "interests": self.interests, "partner": partner, "as": "agent" if self.agent is not None else "person", "game": self.game}
        if self.game:
            r = self.round
            out["guess"] = r.guess if r is not None and not r.revealed else None
            out["guessing"] = r is not None and not r.revealed and self.agent is None
            out["score"] = {"right": self.score[0], "played": self.score[1]}
        return out


@dataclass
class Round:
    """One game of "person or agent?", as one seat sees it: who the partner
    was (kept, though they may have moved on), what this seat guessed, and
    the partner's round, which holds what they guessed about this one."""

    partner: Seat
    agent: dict[str, str] | None   # the partner's name and model when it is an agent; None for a person
    guess: str | None = None
    over: bool = False       # the conversation has ended
    revealed: bool = False
    other: "Round | None" = None


class Lobby:
    def __init__(self, hub: "Hub", *, typing: bool = True, reach: int = 0) -> None:
        self.hub = hub
        self.reach = max(0, int(reach))    # chat links to keep with other open lobbies (--chat-reach)
        self.links: dict[str, "Remote"] = {}   # hub id -> the link: chat only, never a follow
        self._refused: dict[str, tuple[float, str]] = {}  # hub id -> (when it may be asked again, why it said no)
        self._fails: dict[str, int] = {}
        self.typing = typing   # the hub's switch: off, nobody's typing is shared
        self.seats: dict[str, Seat] = {}
        self.refs: dict[str, Seat] = {}
        self.cond = threading.Condition(threading.RLock())
        self.bot_after = BOT_AFTER
        self.meet_every = MEET_EVERY
        self.seat_idle = SEAT_IDLE
        self.typing_ttl = TYPING_TTL
        self._parts: list[tuple["Remote", str, str]] = []   # Parts to send once the lock is let go: (remote, to, from)

    # ------------------------------------------------------------ views

    def stats(self, guest: bool = False) -> dict[str, Any]:
        with self.cond:
            self._sweep()
            seats = list(self.seats.values())
        fed = self.hub.federation
        return {
            "status": "ok",
            "waiting": sum(1 for s in seats if s.state == "waiting"),
            "talking": sum(1 for s in seats if s.state == "talking"),
            "bots": 0 if guest else sum(1 for h in self.hub.holders() if h.link != "down" and h.trees),
            "hubs": (sum(1 for r in fed.following.values() if r.link != "down") if fed is not None else 0) + len(self.links),
            "agents": sum(1 for s in seats if s.agent is not None),
            "playing": sum(1 for s in seats if s.game),
            "typing": self.typing,
        }

    def _view(self, seat: Seat, after: int) -> dict[str, Any]:
        events = [e for e in seat.events if e["n"] > after]
        return {"status": "ok", "seat": seat.id, **seat.view(), "bots": seat.bots, "typing_indicator": self._shows(seat), "partner_typing": self._partner_typing(seat), "events": events, "after": seat.n}

    # ------------------------------------------------------------ typing

    def _shows(self, seat: Seat) -> bool:
        return self.typing and seat.typing

    def _partner_typing(self, seat: Seat) -> bool:
        return self._shows(seat) and seat.state == "talking" and seat.heard > time.time()

    def _hear(self, seat: Seat, until: float) -> None:
        seat.heard = until
        self.cond.notify_all()

    def _tell_far(self, seat: Seat, typing: bool, *, force: bool = False) -> tuple[Far, dict[str, Any]] | None:
        """The Typing to send the stranger on another hub, if one is due:
        on a change, or every couple of seconds while it holds."""
        p = seat.partner
        if not isinstance(p, Far) or not p.typing:
            return None
        now = time.time()
        last, at = seat.told
        if not force and typing == last and (not typing or now - at < TYPING_EVERY):
            return None
        seat.told = (typing, now)
        return p, {"to": p.ref, "from": seat.ref, "typing": typing}

    def _send_typing(self, due: tuple[Far, dict[str, Any]] | None) -> None:
        fed = self.hub.federation
        if due is None or fed is None:
            return
        far, obj = due
        ok, _, _ = fed.deliver(far.remote, fed._activity("Typing", obj))
        if not ok:
            far.typing = False  # a hub that will not take it is not asked again in this conversation

    # ------------------------------------------------------------ events

    def _add(self, seat: Seat, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        seat.n += 1
        event = {"n": seat.n, "at": time.time(), "kind": kind, "text": text, **extra}
        seat.events.append(event)
        self.cond.notify_all()
        return event

    def _say_paired(self, a: Seat, where: str | None, common: list[str], partner: Seat | None = None) -> None:
        if a.game and a.agent is not None:
            text = "You're now playing “person or agent?” with a random stranger: they will guess which you are."
        elif a.game:
            text = "You're now chatting with someone: a person or an agent. Talk, then guess which (/guess person or /guess agent); the answer comes when the conversation ends."
        elif partner is not None and partner.agent is not None:
            model = partner.agent.get("model")
            text = f"You're now chatting with an agent: {partner.agent['name']}{' (' + model + ')' if model else ''}. It is a model, not a person, and not a tree: what it says is its own. Say hi!"
        else:
            told = " They were told you are an agent." if a.agent is not None else ""
            text = f"You're now chatting with a random stranger{' on ' + where if where else ''}. Say hi!{told}"
        self._add(a, "system", text)
        if common:
            self._add(a, "system", f"You both like {', '.join(common)}.")

    # ------------------------------------------------------------ pairing

    def _pair(self, a: Seat, b: Seat) -> None:
        common = [w for w in a.interests if w in b.interests]
        for x, y in ((a, b), (b, a)):
            x.partner, x.state, x.noted = y, "talking", False
            x.round = Round(y, y.agent) if x.game else None
            self._say_paired(x, None, common, partner=y)
        if a.round is not None and b.round is not None:
            a.round.other, b.round.other = b.round, a.round

    @staticmethod
    def _takes(x: Seat, y: Seat) -> bool:
        """Whether ``x`` would be paired with ``y``, as ``x`` asked. The
        game is played among those playing; an agent meets people, never
        another agent; a person meets whom they said they would."""
        if x.game != y.game or (x.agent is not None and y.agent is not None):
            return False
        if x.game or x.agent is not None:
            return True
        return x.want in (("anyone", "agent") if y.agent is not None else ("anyone", "people"))

    @staticmethod
    def _best(found: list[Seat], interests: list[str]) -> Seat | None:
        """The most interests in common first, at random among equals."""
        if not found:
            return None
        best = max(len(set(s.interests) & set(interests)) for s in found)
        return random.choice([s for s in found if len(set(s.interests) & set(interests)) == best])

    def _waiting(self, ok: Callable[[Seat], bool]) -> list[Seat]:
        return [s for s in self.seats.values() if s.state == "waiting" and not s.busy and s.want != "bot" and ok(s)]

    def _match(self, seat: Seat, *, agents: bool) -> Seat | None:
        """Someone waiting here to pair ``seat`` with; agents among them only
        when ``agents``. In the game a person meets a person or an agent at
        even odds, whichever are waiting; anyone else meets people first."""
        found = self._waiting(lambda s: s is not seat and (agents or s.agent is None) and self._takes(seat, s) and self._takes(s, seat))
        people, bots = [s for s in found if s.agent is None], [s for s in found if s.agent is not None]
        if people and bots and seat.agent is None:
            found = random.choice((people, bots)) if seat.game else people
        return self._best(found, seat.interests)

    def _far_ok(self, seat: Seat) -> bool:
        """Whether a seat meets strangers on other hubs: a person, outside the game, who wants people."""
        return seat.agent is None and not seat.game and seat.want in ("anyone", "people")

    def _note_alone(self, seat: Seat) -> None:
        if seat.noted:
            return
        seat.noted = True
        if seat.game and seat.agent is None:
            self._add(seat, "system", "Nobody else is playing right now; still looking. (An agent started in a console's Agents tab can play too.)")
        elif seat.want == "agent":
            self._add(seat, "system", "No agent is in the lobby right now; still looking. (Anyone can seat one, from a console's Chat tab.)")

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
            # A person who could meet strangers on other hubs meets them before an agent here.
            other = self._match(seat, agents=not self._far_ok(seat))
            if other is not None:
                self._pair(seat, other)
                return
            now = time.time()
            far = self._far_ok(seat) and fed is not None and bool(fed.following or self.links) and now - seat.asked >= self._meet_every()
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
            if seat.state != "waiting" or seat.busy or seat.id not in self.seats:
                return
            if seat.want == "anyone" and self._far_ok(seat):
                other = self._match(seat, agents=True)
                if other is not None:
                    self._pair(seat, other)
                elif time.time() - seat.since >= self.bot_after:
                    self._pair_bot(seat)
                return
            self._note_alone(seat)

    def _meet_every(self) -> float:
        """How often a waiting seat asks the hubs we follow: less often as more wait."""
        waiting = sum(1 for s in self.seats.values() if s.state == "waiting")
        return self.meet_every * max(1.0, waiting / MEET_CROWD)

    def _meet_far(self, seat: Seat) -> bool:
        fed = self.hub.federation
        assert fed is not None
        followed = [r for r in fed.following.values() if r.kind == "hub" and r.state == "accepted" and r.link != "down"]
        hubs = followed + [r for url, r in list(self.links.items()) if url not in {f.url for f in followed}]
        random.shuffle(hubs)
        ref = seat.ref
        for remote in hubs[:MEET_HUBS]:
            ok, _, answer = fed.deliver(remote, fed._activity("Meet", {"ref": ref, "interests": seat.interests}))
            if remote.url in self.links:
                self._link_answered(remote.url, ok)
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
        seat.heard, seat.told = 0.0, (False, 0.0)
        if isinstance(p, Seat):
            p.partner, p.state, p.heard = None, "ended", 0.0
            self._add(p, "system", "The agent has disconnected." if seat.agent is not None and not seat.game else "Stranger has disconnected.")
            self._over(p)
        elif isinstance(p, Far):
            self._parts.append((p.remote, p.ref, seat.ref))
        seat.partner = None
        if seat.state == "talking":
            seat.state = "ended"
            self._add(seat, "system", "You have disconnected.")
        self._over(seat)

    # ------------------------------------------------------------ the game

    def _over(self, seat: Seat) -> None:
        """A game's conversation ended: a seat that guessed (or an agent,
        which does not) learns the answer now; a person who did not is
        asked to, and learns it on guessing or on moving on."""
        r = seat.round
        if r is None or r.over:
            return
        r.over = True
        if seat.agent is not None or r.guess is not None:
            self._reveal(seat)
        else:
            self._add(seat, "system", "Now guess: was that a person or an agent? (/guess person or /guess agent)")

    def _reveal(self, seat: Seat) -> None:
        r = seat.round
        assert r is not None
        r.revealed = True
        their = r.other.guess if r.other is not None else None   # an agent does not guess
        if seat.agent is not None:
            text = f"They guessed you were {_a(their)}." if their else "They did not guess (yet)."
            self._add(seat, "reveal", text, partner="person", their_guess=their)
            return
        truth = "agent" if r.agent is not None else "person"
        right = r.guess == truth if r.guess is not None else None
        if r.guess is not None:
            seat.score[1] += 1
            seat.score[0] += 1 if right else 0
        model = (r.agent or {}).get("model")
        parts = [f"It was {_a(truth)}" + (f": {r.agent['name']}{' (' + model + ')' if model else ''}." if r.agent is not None else ".")]
        parts.append(f"You guessed {r.guess}: {'right' if right else 'wrong'}. {seat.score[0]} of {seat.score[1]} right so far." if r.guess is not None else "You did not guess.")
        if their:
            parts.append(f"They guessed you were {_a(their)}.")
        self._add(seat, "reveal", " ".join(parts), partner=truth, agent=r.agent, guess=r.guess, right=right, their_guess=their, score={"right": seat.score[0], "played": seat.score[1]})

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

    def start(self, body: dict[str, Any], guest: bool = False, *, address: str | None = None) -> tuple[int, dict[str, Any]]:
        """``address``: where a caller without the token or a granted key came from; it
        may hold a few seats, and strangers together all but the reserved ones."""
        want = body.get("want", "anyone")
        if want not in WANTS:
            return 400, {"status": "error", "reason": "bad-request", "say": f"want is one of {', '.join(WANTS)}."}
        sit = body.get("as", "person")
        if sit not in ("person", "agent"):
            return 400, {"status": "error", "reason": "bad-request", "say": "as is person or agent."}
        game = body.get("game") is True
        if want == "bot" and (game or sit == "agent"):
            return 400, {"status": "error", "reason": "bad-request", "say": "A bot says what it is: the game is between people and agents, and an agent meets people."}
        if guest and want == "bot":
            return 403, {"status": "error", "reason": "bots-need-token", "say": "Without this hub's token you meet people only: a bot runs the hub's trees."}
        if guest and want == "anyone":
            want = "people"   # never a bot; an agent only when asked for, or in the game
        agent = _agent_of(body.get("agent")) if sit == "agent" else None
        if agent is not None:
            want = "people"   # what an agent meets; in the game, whom a seat meets is the game's
        with self.cond:
            self._sweep()
            strangers = [s for s in self.seats.values() if s.address is not None]
            if len(self.seats) >= SEATS_MAX or (address is not None and len(strangers) >= SEATS_MAX - SEATS_RESERVED):
                return 503, {"status": "error", "reason": "lobby-full", "say": f"The lobby is full ({SEATS_MAX} seats); try again in a minute."}
            if address is not None and sum(1 for s in strangers if s.address == address) >= SEATS_PER_ADDRESS:
                return 429, {"status": "error", "reason": "too-many-seats", "say": f"{SEATS_PER_ADDRESS} seats from one place at once is the most; leave one first."}
            seat = Seat(secrets.token_urlsafe(18), secrets.token_urlsafe(12), want, interests_of(body.get("interests")), typing=body.get("typing_indicator") is not False, bots=not guest, address=address, agent=agent, game=game)
            self.seats[seat.id] = seat
            self.refs[seat.ref] = seat
            self._add(seat, "system", _looking(seat))
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
        # Only a client that says what it shows is woken by typing; any other
        # would be answered at once, again and again, while the partner types.
        shown = bool(body["partner_typing"]) if "partner_typing" in body else None
        deadline = time.time() + wait
        with self.cond:
            seat.polls += 1  # one poll waits per seat: an older one answers now and lets its thread go
            mine = seat.polls
            self.cond.notify_all()
        while True:
            with self.cond:
                seat.seen = time.time()  # polling is being here, before anyone is swept
                if seat.polls != mine:
                    return 200, self._view(seat, after)
            self._advance(seat)
            self._flush()
            with self.cond:
                now = time.time()
                # A seat that left while this poll waited gets its last view, state "left".
                if seat.polls != mine or seat.id not in self.seats or any(e["n"] > after for e in seat.events) or now >= deadline or (shown is not None and self._partner_typing(seat) != shown):
                    return 200, self._view(seat, after)
                nap = deadline - now
                if shown and now < seat.heard < float("inf"):
                    nap = min(nap, seat.heard - now)  # wake when the indicator goes out
                if seat.state == "waiting" and self._far_ok(seat):
                    # Wake to ask the hubs we follow again, or to meet a bot.
                    if self.hub.federation is not None and (self.hub.federation.following or self.links):
                        nap = min(nap, seat.asked + self._meet_every() - now)
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
        text = clean(text)
        if not text:
            return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"seat\": \"…\", \"text\": \"…\"}."}
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
            seat.told = (False, now)  # the line ends the typing, here and on the other hub
            if isinstance(partner, Seat):
                partner.heard = 0.0
                if seat.agent is not None and not seat.game:
                    self._add(partner, "agent", text, name=seat.agent["name"])
                else:
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
            if seat.round is not None:
                seat.round.over = True   # moving on: the answer now, not a request to guess
            self._end(seat)
            if seat.round is not None and not seat.round.revealed:
                self._reveal(seat)   # without a guess, it scores nothing
            seat.round = None
            self.refs.pop(seat.ref, None)
            seat.ref = secrets.token_urlsafe(12)   # the last stranger cannot reach the next conversation
            self.refs[seat.ref] = seat
            seat.state, seat.since, seat.asked = "waiting", time.time(), 0.0
            seat.botless = seat.noted = False
            if isinstance(body.get("typing_indicator"), bool):
                seat.typing = body["typing_indicator"]
            if isinstance(body.get("game"), bool) and seat.want != "bot":
                seat.game = body["game"]
            self._add(seat, "system", _looking(seat))
        self._flush()
        self._advance(seat)
        with self.cond:
            return 200, self._view(seat, _int(body.get("after")))

    def guess(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """In the game: whether the stranger is (or was) a person or an agent.
        Once per conversation; the answer comes when it is over."""
        seat = self._seat(body)
        if seat is None:
            return 404, _gone()
        guess = body.get("guess")
        if guess not in GUESSES:
            return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"seat\": \"…\", \"guess\": \"person\"|\"agent\"}."}
        with self.cond:
            r = seat.round
            if r is None or r.revealed or seat.agent is not None:
                return 409, {"status": "error", "reason": "no-round", "say": "There is nothing to guess: play the game, and guess about the stranger you are talking to, or the one who just left."}
            if r.guess is not None:
                return 409, {"status": "error", "reason": "guessed", "say": f"You guessed {r.guess} already."}
            r.guess = guess
            if not r.over:
                self._add(seat, "system", f"You guessed {_a(guess)}. The answer comes when the conversation ends.")
            else:
                self._reveal(seat)
                p = r.partner
                if r.other is not None and r.other.revealed and p.id in self.seats:
                    self._add(p, "system", f"Your last stranger guessed you were {_a(guess)}.")
            return 200, self._view(seat, _int(body.get("after")))

    def typed(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """This person is typing (``typing: true``, sent again every couple
        of seconds while they are) or stopped. Shared only when both the hub
        and this seat have the indicator on; ``shared`` says whether it was."""
        seat = self._seat(body)
        if seat is None:
            return 404, _gone()
        typing = body.get("typing") is True
        with self.cond:
            if not self._shows(seat) or seat.state != "talking":
                return 200, {"status": "ok", "shared": False}
            p = seat.partner
            if isinstance(p, Seat):
                self._hear(p, time.time() + self.typing_ttl if typing else 0.0)
            due = self._tell_far(seat, typing)
        self._send_typing(due)
        return 200, {"status": "ok", "shared": True}

    def settings(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seat = self._seat(body)
        if seat is None:
            return 404, _gone()
        if not isinstance(body.get("typing_indicator"), bool):
            return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"seat\": \"…\", \"typing_indicator\": true|false}."}
        due = None
        with self.cond:
            seat.typing = body["typing_indicator"]
            if not seat.typing:
                # Off now: whatever the partner was shown of this person's typing goes out.
                if isinstance(seat.partner, Seat):
                    self._hear(seat.partner, 0.0)
                if seat.told[0]:
                    due = self._tell_far(seat, False, force=True)
            self.cond.notify_all()
            view = self._view(seat, _int(body.get("after")))
        self._send_typing(due)
        return 200, view

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
            with self.cond:
                if seat.partner is bot:
                    self._hear(seat, float("inf"))  # thinking, until its line
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
            seat.heard = 0.0
            extra = {k: answer[k] for k in ("status", "reason", "tree", "peer", "via", "output", "trace") if answer.get(k) is not None}
            self._add(seat, "bot", text, **extra)

    # ------------------------------------------------------------ chat links

    def open(self) -> bool:
        """Whether strangers may chat here anyway: a public lobby, or a hub with no token."""
        return bool(self.hub.chat_public or not self.hub.token)

    def _chosen(self, remote: "Remote") -> bool:
        """Whether the operator chose this hub: this one follows it (a follow back counts)."""
        fed = self.hub.federation
        return fed is not None and remote.url in fed.following

    def link_for(self, hub: str) -> "Remote | None":
        return self.links.get(hub.rstrip("/"))

    def links_view(self) -> dict[str, Any]:
        now = time.time()
        return {"reach": self.reach, "open": self.open(), "links": [{"url": r.url, "name": r.name, "since": r.since} for r in list(self.links.values())],
                "refused": [{"url": u, "reason": why, "until": until} for u, (until, why) in list(self._refused.items()) if until > now]}

    def accept_link(self, remote: "Remote") -> tuple[int, dict[str, Any]]:
        """Another hub asks for a chat link (it signed with the pair secret it sent)."""
        if self.reach <= 0:
            return 403, {"status": "error", "reason": "not-linking", "say": "This hub keeps no chat links (its operator did not give --chat-reach)."}
        if not self.open():
            return 403, {"status": "error", "reason": "private-lobby", "say": "This hub's lobby is not open to strangers, so it links with nobody for chat."}
        with self.cond:
            if remote.url not in self.links and len(self.links) >= 2 * self.reach:
                return 503, {"status": "error", "reason": "links-full", "say": f"This hub keeps {len(self.links)} chat links already."}
            self.links[remote.url] = remote
            self._fails.pop(remote.url, None)
        return 202, {"status": "ok", "linked": True}

    def link(self, url: str) -> bool:
        """Ask the hub at ``url`` for a chat link: a ``ChatLink`` carrying a
        fresh pair secret, signed with it. True when it took it."""
        from .federation import Remote

        fed = self.hub.federation
        if fed is None:
            return False
        url = url.rstrip("/")
        try:
            status, actor = http_json("GET", f"{url}/actor", timeout=5.0)
        except Exception:
            status, actor = 0, None
        if status != 200 or not isinstance(actor, dict) or actor.get("type") != "Hub" or not isinstance(actor.get("inbox"), str) or str(actor.get("id", "")).rstrip("/") != url:
            self._refused[url] = (time.time() + LINK_REFUSED, "no-actor")
            return False
        remote = Remote(url, actor["inbox"], secrets.token_hex(32), str(actor.get("name") or ""), state="accepted", kind="hub")
        ok, _, answer = fed.deliver(remote, fed._activity("ChatLink", {"type": "Lobby", "id": fed.id}, secret=remote.secret, inbox=fed.inbox, name=self.hub.name))
        if not (ok and isinstance(answer, dict) and answer.get("linked") is True):
            self._refused[url] = (time.time() + LINK_REFUSED, str((answer.get("reason") if isinstance(answer, dict) else None) or "no-answer"))
            return False
        remote.last_ok = time.time()
        with self.cond:
            self.links[url] = remote
            self._fails.pop(url, None)
        return True

    def reach_out(self) -> int:
        """Keep up to ``reach`` links: try a hub or two it heard of on the
        LAN, then in its directory (public networks only), that it neither
        follows nor is followed by nor links with already. Called on the
        hub's poll; returns the links made."""
        fed = self.hub.federation
        if fed is None or self.reach <= 0 or not self.open() or len(self.links) >= self.reach:
            return 0
        now = time.time()
        public = fed._public_networks()
        with fed._lock:
            heard = sorted((e for e in fed.directory_entries.values() if e.get("network") in public and e.get("kind", "hub") == "hub"), key=lambda e: -e["seen"])
            taken = set(fed.following) | {u for u, r in fed.followers.items() if r.kind == "hub"}
        nearby = fed.lan.hubs() if fed.lan is not None else []
        made = tried = 0
        for url in dict.fromkeys(nearby + [e["id"].rstrip("/") for e in heard]):
            if tried >= LINK_TRIES or len(self.links) >= self.reach:
                break
            if url == fed.id or url in taken or url in self.links or self._refused.get(url, (0.0, ""))[0] > now:
                continue
            tried += 1
            made += 1 if self.link(url) else 0
        return made

    def _link_answered(self, url: str, ok: bool) -> None:
        with self.cond:
            if ok:
                self._fails.pop(url, None)
                return
            self._fails[url] = self._fails.get(url, 0) + 1
            if self._fails[url] >= LINK_FAILS:
                self.links.pop(url, None)  # it stopped answering: make another in its place
                self._fails.pop(url, None)

    # ------------------------------------------------------------ federation

    def on_activity(self, kind: str, remote: "Remote", obj: Any, *, follower: bool) -> tuple[int, dict[str, Any]]:
        """A ``Meet``, ``Say``, ``Typing`` or ``Part`` from a hub we share a secret with.
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
            if not self.open() and not self._chosen(remote):
                # A lobby behind a token meets strangers only from hubs its operator chose to follow.
                return 403, {"status": "error", "reason": "private-lobby", "say": "This hub's lobby is not open to strangers, so it meets none from hubs it does not follow."}
            if not isinstance(ref, str) or not REF.match(ref):
                return 400, {"status": "error", "reason": "bad-activity", "say": "A Meet carries the asking seat's ref."}
            interests = interests_of(obj.get("interests"))
            with self.cond:
                self._sweep()
                seat = self._best(self._waiting(lambda s: self._far_ok(s)), interests)
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
                seat.partner, seat.state, seat.heard = None, "ended", 0.0
                self._add(seat, "system", "Stranger has disconnected.")
                return 200, {"status": "ok"}
            if kind == "Typing":
                # Their hub sends it only when their indicator is on; ours shows it only when this seat's is.
                self._hear(seat, time.time() + self.typing_ttl if obj.get("typing") is True else 0.0)
                return 200, {"status": "ok"}
            text = obj.get("text")
            if not isinstance(text, str) or not clean(text) or len(text) > LINE_MAX:
                return 400, {"status": "error", "reason": "bad-activity", "say": f"A Say carries a line of at most {LINE_MAX} characters."}
            now = time.time()
            seat.heard_allowance = min(BURST, seat.heard_allowance + (now - seat.heard_allowance_at) * REFILL)
            seat.heard_allowance_at = now
            if seat.heard_allowance < 1:
                return 429, {"status": "error", "reason": "too-fast", "say": "Slow down: five lines at once, then one a second."}
            seat.heard_allowance -= 1
            seat.heard = 0.0
            self._add(seat, "stranger", clean(text))
            return 200, {"status": "ok"}

    # ------------------------------------------------------------ routing

    def route(self, method: str, path: str, body: dict[str, Any], *, guest: bool = False, address: str | None = None) -> tuple[int, dict[str, Any]]:
        """``guest``: the caller lacks the hub's token, and came in because the lobby is public.
        ``address``: where the caller came from, when it has neither the token nor a granted key."""
        if method == "GET" and path == "/chat":
            try:
                return 200, self.stats(guest)
            finally:
                self._flush()
        handlers = {"/chat/start": self.start, "/chat/poll": self.poll, "/chat/say": self.say, "/chat/typing": self.typed, "/chat/settings": self.settings, "/chat/guess": self.guess, "/chat/next": self.next, "/chat/leave": self.leave}
        if method == "POST" and path in handlers:
            try:
                return self.start(body, guest, address=address) if path == "/chat/start" else handlers[path](body)
            finally:
                self._flush()  # the Parts an ending owes another hub, sent once no lock is held
        return 404, {"status": "error", "reason": "not-found", "say": f"No {method} {path} here."}


def clean(text: str) -> str:
    """A chat line without control characters, so that no line steers a terminal."""
    return CONTROLS.sub("", text).strip()


def _agent_of(value: Any) -> dict[str, str]:
    """An agent's name and model, as its console said, made safe to show:
    the name a slug (as agents.js makes it), the model a short plain line."""
    v = value if isinstance(value, dict) else {}
    name = re.sub(r"[^a-z0-9]+", "-", clean(str(v.get("name") or "")).lower()).strip("-")[:32].strip("-") or "agent"
    model = re.sub(r"[^A-Za-z0-9._:/@+ -]+", "", clean(str(v.get("model") or "")))[:80].strip()
    return {"name": name, "model": model}


def _a(kind: str) -> str:
    return "an agent" if kind == "agent" else "a person"


def _looking(seat: Seat) -> str:
    if seat.agent is not None:
        return "Waiting for a stranger…"
    if seat.game:
        return "Looking for someone to play “person or agent?” with…"
    return {"bot": "Looking for a bot…", "agent": "Looking for an agent…"}.get(seat.want, "Looking for someone you can chat with…")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gone() -> dict[str, Any]:
    return {"status": "error", "reason": "no-seat", "say": "This seat is gone (it left, or nobody polled it for a minute): start again."}
