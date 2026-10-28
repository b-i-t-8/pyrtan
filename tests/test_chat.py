"""The lobby: two strangers on one hub, a bot made of trees, strangers on
two hubs that follow each other, and what the lobby refuses."""

import io
import sys
import time

import pytest

from pyrtan import Registry, RecordedTransport
from pyrtan.chat import interests_of, sample
from pyrtan.cli import main
from pyrtan.hub import Hub
from pyrtan.service import Server, Service, http_json
from conftest import EXAMPLES, FIXTURES

GREETER = """
name = "greeter"
description = "asks for a name"
start = "have-name"

[[triggers]]
pattern = "greet me"

[inputs.name]
type = "str"
required = false

[nodes.have-name]
kind = "decide"
on = "inputs.name"
branches = [ { when = { exists = true }, then = "hello" }, { else = "ask" } ]

[nodes.ask]
kind = "ask"
prompt = "What is your name?"
into = "inputs.name"
next = "hello"

[nodes.hello]
kind = "done"
say = "Hello, {{ inputs.name }}."
"""


def post(hub, path, body):
    return hub.route("POST", path, body)


def start(hub, want="anyone", interests=()):
    status, out = post(hub, "/chat/start", {"want": want, "interests": list(interests)})
    assert status == 200, out
    return out


def until(hub, seat, test, after=0, timeout=5.0):
    """Poll a seat until ``test(events, view)`` holds; every event seen so far."""
    events, deadline = [], time.time() + timeout
    while True:
        status, view = post(hub, "/chat/poll", {"seat": seat, "after": after, "wait": 0.2})
        assert status == 200, view
        events += view["events"]
        after = view["after"]
        if test(events, view):
            return events, view
        if time.time() > deadline:
            raise AssertionError(f"never: {events}")


def texts(events, kind):
    return [e["text"] for e in events if e["kind"] == kind]


@pytest.fixture
def examples_hub():
    svc = Service(Registry.from_paths([EXAMPLES]), name="alpha", transport=RecordedTransport.from_file(FIXTURES / "examples.json"))
    hub = Hub(name="alpha", chat=True, local=svc)
    yield hub
    hub.stop()


def test_two_strangers_meet_talk_and_part():
    hub = Hub(name="h", chat=True)
    a = start(hub, "people", ["Music", "cats"])
    assert a["state"] == "waiting" and a["interests"] == ["music", "cats"]
    assert hub.route("GET", "/chat", None)[1]["waiting"] == 1
    b = start(hub, "anyone", ["music"])
    assert b["state"] == "talking" and b["partner"] == {"kind": "stranger", "hub": None}
    assert "You're now chatting with a random stranger. Say hi!" in texts(b["events"], "system")
    assert "You both like music." in texts(b["events"], "system")
    events, view = until(hub, a["seat"], lambda ev, v: v["state"] == "talking")
    assert "You both like music." in texts(events, "system")

    assert post(hub, "/chat/say", {"seat": a["seat"], "text": "  hi there  "})[0] == 200
    events, _ = until(hub, b["seat"], lambda ev, v: texts(ev, "stranger"), after=b["after"])
    assert texts(events, "stranger") == ["hi there"]
    events, _ = until(hub, a["seat"], lambda ev, v: texts(ev, "you"), after=view["after"])
    assert texts(events, "you") == ["hi there"]  # your own line comes back in the hub's order

    # b moves on: a is told, and cannot talk into the void.
    status, nxt = post(hub, "/chat/next", {"seat": b["seat"]})
    assert status == 200 and nxt["state"] == "waiting"
    events, view = until(hub, a["seat"], lambda ev, v: v["state"] == "ended")
    assert "Stranger has disconnected." in texts(events, "system")
    assert post(hub, "/chat/say", {"seat": a["seat"], "text": "hello?"})[1]["reason"] == "not-talking"

    # Next pairs a with b again: the only other person here.
    status, again = post(hub, "/chat/next", {"seat": a["seat"]})
    assert again["state"] == "talking"
    assert post(hub, "/chat/leave", {"seat": a["seat"]})[1]["left"] is True
    assert post(hub, "/chat/poll", {"seat": a["seat"]})[1]["reason"] == "no-seat"
    events, _ = until(hub, b["seat"], lambda ev, v: v["state"] == "ended", after=nxt["after"])
    assert texts(events, "system")[-1] == "Stranger has disconnected."
    assert hub.route("GET", "/chat", None)[1] == {"status": "ok", "waiting": 0, "talking": 0, "bots": 0, "hubs": 0, "typing": True}


def test_a_bot_is_trees_and_says_how_it_answered(examples_hub):
    hub = examples_hub
    seat = start(hub, "bot", ["disk"])
    assert seat["state"] == "talking" and seat["partner"]["kind"] == "bot" and seat["partner"]["name"] == "alpha"
    intro = texts(seat["events"], "system")[-1]
    assert intro.startswith("You're now chatting with a bot: alpha, this hub's own trees.") and "not a model" in intro
    assert "Try “how full is the disk”." in intro  # a sentence from the tree that shares the interest
    after = seat["after"]

    def bot_says(text):
        nonlocal after
        assert post(hub, "/chat/say", {"seat": seat["seat"], "text": text})[0] == 200
        events, view = until(hub, seat["seat"], lambda ev, v: texts(ev, "bot"), after=after)
        after = view["after"]
        return [e for e in events if e["kind"] == "bot"][-1]

    line = bot_says("what's the weather in Oslo")
    assert line["text"] == "Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h."
    assert line["status"] == "done" and line["tree"] == "weather" and line["peer"] == "local"
    assert line["trace"]["steps"][0]["kind"] == "intent" and line["output"]["sky"] == "raining"

    # A greeting asks the swarm nothing: the bot says who it is, and nothing is recorded.
    line = bot_says("hey!")
    assert line["reason"] == "greeting" and line["text"].startswith("I'm alpha, this hub's own trees.")
    line = bot_says("how are you?")
    assert line["tree"] == "small-talk" and line["text"].startswith("Deterministic")
    assert hub.unanswered.to_list() == []

    # Nobody answers: the bot says so, with the tree that would, and the swarm records it.
    line = bot_says("top 3 stories on lobsters")
    assert line["reason"] == "no-match" and 'pyrtan new top-stories-lobsters --from "top 3 stories on lobsters"' in line["text"]
    assert [u["text"] for u in hub.unanswered.to_list()] == ["top 3 stories on lobsters"]


def test_a_bot_asks_and_the_next_line_is_the_answer(tmp_path):
    (tmp_path / "greeter.toml").write_text(GREETER)
    hub = Hub(name="g", chat=True, local=Service(Registry.from_paths([tmp_path]), name="g"))
    seat = start(hub, "bot")
    assert "Try “greet me”." in texts(seat["events"], "system")[-1]
    post(hub, "/chat/say", {"seat": seat["seat"], "text": "greet me"})
    events, view = until(hub, seat["seat"], lambda ev, v: texts(ev, "bot"), after=seat["after"])
    assert texts(events, "bot") == ["What is your name?"] and events[-1]["status"] == "asked"
    post(hub, "/chat/say", {"seat": seat["seat"], "text": "Ada"})
    events, _ = until(hub, seat["seat"], lambda ev, v: texts(ev, "bot"), after=view["after"])
    assert texts(events, "bot") == ["Hello, Ada."] and events[-1]["status"] == "done"


def test_anyone_meets_a_bot_after_a_while_and_people_never_do(examples_hub):
    hub = examples_hub
    hub.lobby.bot_after = 0.3
    people = start(hub, "people")
    _, view = post(hub, "/chat/poll", {"seat": people["seat"], "after": people["after"], "wait": 0.6})
    assert view["state"] == "waiting" and view["events"] == []
    post(hub, "/chat/leave", {"seat": people["seat"]})

    anyone = start(hub, "anyone")
    assert anyone["state"] == "waiting"
    # The long poll wakes by itself when the bot is due.
    t0 = time.time()
    _, view = post(hub, "/chat/poll", {"seat": anyone["seat"], "after": anyone["after"], "wait": 5})
    assert view["state"] == "talking" and view["partner"]["kind"] == "bot" and time.time() - t0 < 2


def test_no_trees_no_bots():
    hub = Hub(name="empty", chat=True)
    hub.lobby.bot_after = 0
    bot = start(hub, "bot")
    assert bot["state"] == "ended" and "No bots here" in texts(bot["events"], "system")[-1]
    anyone = start(hub, "anyone")
    events, view = until(hub, anyone["seat"], lambda ev, v: "either" in " ".join(texts(ev, "system")))
    assert view["state"] == "waiting" and texts(events, "system").count("No bots here either; still looking for a person.") == 1


def test_lines_are_short_and_not_too_fast():
    hub = Hub(name="h", chat=True)
    a, b = start(hub, "people"), start(hub, "people")
    say = lambda text: post(hub, "/chat/say", {"seat": a["seat"], "text": text})
    assert say("x" * 1001)[1]["reason"] == "too-long"
    assert say("   ")[0] == 400
    assert [say(f"line {i}")[0] for i in range(6)] == [200] * 5 + [429]
    assert post(hub, "/chat/start", {"want": "everyone"})[0] == 400
    assert post(hub, "/chat/say", {"seat": "nobody", "text": "hi"})[1]["reason"] == "no-seat"


def test_a_seat_nobody_polls_is_let_go():
    hub = Hub(name="h", chat=True)
    hub.lobby.seat_idle = 0.3
    a, b = start(hub, "people"), start(hub, "people")
    events, view = until(hub, b["seat"], lambda ev, v: v["state"] == "ended", after=b["after"])
    assert texts(events, "system") == ["Stranger has disconnected."]
    assert post(hub, "/chat/poll", {"seat": a["seat"]})[1]["reason"] == "no-seat"


def test_interests_choose_whom_you_meet():
    hub = Hub(name="h", chat=True)
    cats = start(hub, "people", ["cats"])
    hub.lobby.seats[cats["seat"]].busy = True  # as if asking another hub: two can wait at once
    dogs = start(hub, "people", ["dogs"])
    hub.lobby.seats[cats["seat"]].busy = False
    assert cats["state"] == dogs["state"] == "waiting"
    me = start(hub, "people", ["dogs", "chess"])  # at random among equals, but sharing a word goes first
    assert me["state"] == "talking" and "You both like dogs." in texts(me["events"], "system")
    assert post(hub, "/chat/poll", {"seat": cats["seat"]})[1]["state"] == "waiting"
    assert interests_of("Dogs, #chess,, a b c d e f") == ["dogs", "chess", "a", "b", "c"]
    assert sample({"triggers": ["[what's the] weather in {place}"], "inputs": {"place": {"default": "Oslo"}}}) == "what's the weather in Oslo"
    assert sample({"triggers": ["(who|what) are you [doing|today]"]}) == "who are you doing"
    assert sample({"triggers": ["is {host:host} up"], "inputs": {"host": {}}}) is None


def test_trigger_inputs_travel_with_capabilities(examples_hub):
    # small-talk fixes its topic in each trigger; a hub must route with it.
    caps = examples_hub.capabilities()["trees"]
    talk = next(t for t in caps if t["name"] == "small-talk")
    assert talk["trigger_inputs"]["how are you [doing|today]"] == {"topic": "how"}
    status, out = examples_hub.intent({"text": "goodbye"})
    assert out["status"] == "done" and out["output"] == {"topic": "bye"}


def test_the_lobby_is_opt_in_and_in_the_api():
    plain = Hub(name="p")
    assert plain.route("GET", "/chat", None)[1]["reason"] == "no-chat"
    assert plain.route("GET", "/api", None)[1]["chat"] is False
    _, api = Hub(name="c", chat=True).route("GET", "/api", None)
    assert api["chat"] is True and "/chat/start" in api["endpoints"]


class Lobbyist:
    """A federated hub with a lobby and no trees, over a real socket."""

    def __init__(self, name, **hub):
        self.hub = Hub(name=name, poll_every=60, chat=True, **hub)
        self.srv = Server(self.hub.route, quiet=True, federated=True, token=hub.get("token")).start()
        self.fed = self.hub.federate(self.srv.url)
        self.url = self.srv.url

    def call(self, path, body=None):
        return http_json("POST" if body is not None else "GET", f"{self.url}{path}", body)

    def stop(self):
        self.hub.stop()
        self.srv.stop()


@pytest.fixture
def lobbies():
    a, b = Lobbyist("a"), Lobbyist("b")
    yield a, b
    a.stop()
    b.stop()


def test_strangers_meet_across_a_follow(lobbies):
    a, b = lobbies
    there = start(b.hub, "people", ["chess"])
    assert there["state"] == "waiting"
    a.fed.follow(b.url)
    assert a.fed.following[b.url].state == "accepted"
    assert a.call("/chat")[1]["hubs"] == 1

    _, here = a.call("/chat/start", {"want": "people", "interests": ["chess"]})
    assert here["state"] == "talking" and here["partner"] == {"kind": "stranger", "hub": "b"}
    assert "You're now chatting with a random stranger on b. Say hi!" in texts(here["events"], "system")
    assert "You both like chess." in texts(here["events"], "system")
    events, view = until(b.hub, there["seat"], lambda ev, v: v["state"] == "talking")
    assert "You're now chatting with a random stranger on a. Say hi!" in texts(events, "system")

    assert a.call("/chat/say", {"seat": here["seat"], "text": "hello from a"})[0] == 200
    events, view = until(b.hub, there["seat"], lambda ev, v: texts(ev, "stranger"), after=view["after"])
    assert texts(events, "stranger") == ["hello from a"]
    assert post(b.hub, "/chat/say", {"seat": there["seat"], "text": "and from b"})[0] == 200
    events, _ = until(a.hub, here["seat"], lambda ev, v: texts(ev, "stranger"), after=here["after"])
    assert texts(events, "stranger") == ["and from b"]

    # The logs keep that a line went, never what it said.
    said = [x for x in a.fed.outbox if x["type"] == "Say"]
    assert said and all(x["object"]["text"] == "<12 characters>" for x in said)
    assert "hello from a" not in str(list(b.fed.inbox_log)) and "and from b" not in str(list(a.fed.outbox) + list(a.fed.inbox_log))

    # Being followed lets b answer a's Meet, not send its own to a.
    ok, error, answer = b.fed.deliver(b.fed.followers[a.url], b.fed._activity("Meet", {"ref": "x" * 16}))
    assert not ok and answer["reason"] == "not-a-follower"
    # A Say for a conversation that is not that hub's goes nowhere.
    ok, error, answer = b.fed.deliver(b.fed.followers[a.url], b.fed._activity("Say", {"to": "y" * 16, "from": "x" * 16, "text": "boo"}))
    assert not ok and answer["reason"] == "no-such-seat"

    a.call("/chat/leave", {"seat": here["seat"]})
    events, view = until(b.hub, there["seat"], lambda ev, v: v["state"] == "ended", after=view["after"])
    assert texts(events, "system")[-1] == "Stranger has disconnected."


def test_cli_chat_with_a_bot(examples_hub, capsys, monkeypatch):
    srv = Server(examples_hub.route, quiet=True).start()
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO("how are you\n"))
        assert main(["chat", "--bot", "--at", srv.url]) == 0
        out = capsys.readouterr().out
        assert "You're now chatting with a bot: alpha" in out
        assert "Bot: Deterministic, thanks: the same question takes the same path every time." in out
        assert examples_hub.lobby.seats == {}  # it left on the way out
        assert main(["chat", "--at", "http://127.0.0.1:9"]) == 3
    finally:
        srv.stop()


def test_stranger_is_typing_and_stops():
    hub = Hub(name="h", chat=True)
    hub.lobby.typing_ttl = 0.4
    a, b = start(hub, "people"), start(hub, "people")
    assert b["typing_indicator"] is True and b["partner_typing"] is False
    assert hub.route("GET", "/chat", None)[1]["typing"] is True

    assert post(hub, "/chat/typing", {"seat": a["seat"], "typing": True})[1] == {"status": "ok", "shared": True}
    # A poll that says what it shows is answered as soon as that changes.
    t0 = time.time()
    _, view = post(hub, "/chat/poll", {"seat": b["seat"], "after": b["after"], "wait": 5, "partner_typing": False})
    assert view["partner_typing"] is True and view["events"] == [] and time.time() - t0 < 1
    # Nobody refreshes it: it goes out on its own, and the poll wakes for that too.
    t0 = time.time()
    _, view = post(hub, "/chat/poll", {"seat": b["seat"], "after": view["after"], "wait": 5, "partner_typing": True})
    assert view["partner_typing"] is False and 0.2 < time.time() - t0 < 2

    # Typing, then the line: the line ends it.
    post(hub, "/chat/typing", {"seat": a["seat"], "typing": True})
    assert post(hub, "/chat/poll", {"seat": b["seat"], "after": view["after"]})[1]["partner_typing"] is True
    post(hub, "/chat/say", {"seat": a["seat"], "text": "hello"})
    _, view = post(hub, "/chat/poll", {"seat": b["seat"], "after": view["after"]})
    assert texts(view["events"], "stranger") == ["hello"] and view["partner_typing"] is False
    # And stopping says so at once.
    post(hub, "/chat/typing", {"seat": a["seat"], "typing": True})
    post(hub, "/chat/typing", {"seat": a["seat"], "typing": False})
    assert post(hub, "/chat/poll", {"seat": b["seat"], "after": view["after"]})[1]["partner_typing"] is False

    # A client that does not say what it shows is not woken by typing (it would spin).
    post(hub, "/chat/typing", {"seat": a["seat"], "typing": True})
    t0 = time.time()
    _, view = post(hub, "/chat/poll", {"seat": b["seat"], "after": view["after"], "wait": 0.3})
    assert time.time() - t0 >= 0.3 and view["events"] == []

    # The stranger leaves mid-word: the indicator goes with them.
    post(hub, "/chat/leave", {"seat": a["seat"]})
    assert post(hub, "/chat/poll", {"seat": b["seat"], "after": view["after"]})[1]["partner_typing"] is False


def test_the_indicator_is_both_ways_or_not_at_all():
    hub = Hub(name="h", chat=True)
    a = start(hub, "people")
    b = post(hub, "/chat/start", {"want": "people", "typing_indicator": False})[1]
    assert b["typing_indicator"] is False
    # b shares nothing, and sees nothing.
    assert post(hub, "/chat/typing", {"seat": b["seat"], "typing": True})[1]["shared"] is False
    assert post(hub, "/chat/poll", {"seat": a["seat"]})[1]["partner_typing"] is False
    post(hub, "/chat/typing", {"seat": a["seat"], "typing": True})
    assert post(hub, "/chat/poll", {"seat": b["seat"]})[1]["partner_typing"] is False
    # Turned on mid-conversation, b sees a, and a sees b.
    _, view = post(hub, "/chat/settings", {"seat": b["seat"], "typing_indicator": True})
    assert view["typing_indicator"] is True and view["partner_typing"] is True
    post(hub, "/chat/typing", {"seat": b["seat"], "typing": True})
    assert post(hub, "/chat/poll", {"seat": a["seat"]})[1]["partner_typing"] is True
    # Turned off again, what a was shown of b's typing goes out at once.
    post(hub, "/chat/settings", {"seat": b["seat"], "typing_indicator": False})
    assert post(hub, "/chat/poll", {"seat": a["seat"]})[1]["partner_typing"] is False
    assert post(hub, "/chat/settings", {"seat": b["seat"], "typing_indicator": "yes"})[0] == 400
    # The setting holds across Next, unless Next says otherwise.
    assert post(hub, "/chat/next", {"seat": b["seat"]})[1]["typing_indicator"] is False
    assert post(hub, "/chat/next", {"seat": b["seat"], "typing_indicator": True})[1]["typing_indicator"] is True


def test_a_hub_can_turn_typing_off_for_everyone():
    hub = Hub(name="quiet", chat=True, chat_typing=False)
    assert hub.route("GET", "/chat", None)[1]["typing"] is False
    a, b = start(hub, "people"), start(hub, "people")
    assert a["typing_indicator"] is False and b["typing_indicator"] is False
    assert post(hub, "/chat/typing", {"seat": a["seat"], "typing": True})[1]["shared"] is False
    assert post(hub, "/chat/poll", {"seat": b["seat"]})[1]["partner_typing"] is False


def test_a_bot_is_thinking_while_its_trees_run(examples_hub, monkeypatch):
    hub = examples_hub
    slow = hub.intent

    def thinking(body, only=None):
        time.sleep(0.5)
        return slow(body, only=only)

    monkeypatch.setattr(hub, "intent", thinking)
    seat = start(hub, "bot")
    post(hub, "/chat/say", {"seat": seat["seat"], "text": "how are you"})
    _, view = post(hub, "/chat/poll", {"seat": seat["seat"], "after": seat["after"] + 1, "wait": 2, "partner_typing": False})
    assert view["partner_typing"] is True and texts(view["events"], "bot") == []
    events, view = until(hub, seat["seat"], lambda ev, v: texts(ev, "bot"), after=view["after"])
    assert view["partner_typing"] is False and texts(events, "bot")[0].startswith("Deterministic")


def test_typing_travels_between_hubs_and_stays_out_of_the_logs(lobbies):
    a, b = lobbies
    there = start(b.hub, "people")
    a.fed.follow(b.url)
    _, here = a.call("/chat/start", {"want": "people"})
    assert here["state"] == "talking"
    assert a.call("/chat/typing", {"seat": here["seat"], "typing": True})[1]["shared"] is True
    _, view = until(b.hub, there["seat"], lambda ev, v: v["partner_typing"])
    # Said again within two seconds, it does not travel again.
    a.call("/chat/typing", {"seat": here["seat"], "typing": True})
    assert a.hub.lobby.seats[here["seat"]].told[0] is True
    a.call("/chat/typing", {"seat": here["seat"], "typing": False})
    until(b.hub, there["seat"], lambda ev, v: not v["partner_typing"])
    post(b.hub, "/chat/typing", {"seat": there["seat"], "typing": True})
    until(a.hub, here["seat"], lambda ev, v: v["partner_typing"])
    assert not [x for x in list(a.fed.outbox) + list(a.fed.inbox_log) + list(b.fed.outbox) + list(b.fed.inbox_log) if x["type"] == "Typing"]


def test_cli_shows_the_indicator_unless_told_not_to(examples_hub, capsys, monkeypatch):
    slow = examples_hub.intent
    monkeypatch.setattr(examples_hub, "intent", lambda body, only=None: (time.sleep(0.6), slow(body, only=only))[1])
    srv = Server(examples_hub.route, quiet=True).start()
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO("how are you\n"))
        assert main(["chat", "--bot", "--at", srv.url]) == 0
        out = capsys.readouterr().out
        assert out.index("  Bot is thinking…") < out.index("Bot: Deterministic")
        monkeypatch.setattr(sys, "stdin", io.StringIO("how are you\n"))
        assert main(["chat", "--bot", "--no-typing", "--at", srv.url]) == 0
        out = capsys.readouterr().out
        assert "thinking" not in out and "Bot: Deterministic" in out
    finally:
        srv.stop()


def test_a_public_lobby_lets_strangers_in_without_the_token_and_without_bots(tmp_path):
    from pyrtan.hub import serve_hub

    (tmp_path / "greeter.toml").write_text(GREETER)
    hub, srv = serve_hub(port=0, quiet=True, name="pub", token="sekrit", trees=[tmp_path], chat_public=True)
    call = lambda method, path, body=None, token=None: http_json(method, f"{srv.url}{path}", body, token=token)
    try:
        # The lobby answers without the token; nothing else does.
        status, stats = call("GET", "/chat")
        assert status == 200 and stats["bots"] == 0
        assert call("GET", "/chat", token="sekrit")[1]["bots"] == 1
        assert call("POST", "/intent", {"text": "greet me"})[0] == 401
        assert call("GET", "/gaps")[0] == 401
        _, api = call("GET", "/api")
        assert api["chat"] is True and api["chat_public"] is True and api["chat_bots"] is False
        assert call("GET", "/api", token="sekrit")[1]["chat_bots"] is True

        # A guest meets people only: no bot on asking, and none after waiting.
        status, out = call("POST", "/chat/start", {"want": "bot"})
        assert status == 403 and out["reason"] == "bots-need-token"
        hub.lobby.bot_after = 0
        _, guest = call("POST", "/chat/start", {"want": "anyone"})
        assert guest["want"] == "people" and guest["bots"] is False
        _, view = call("POST", "/chat/poll", {"seat": guest["seat"], "after": guest["after"], "wait": 0.3})
        assert view["state"] == "waiting"
        _, other = call("POST", "/chat/start", {"want": "people"})
        assert other["state"] == "talking"
        assert call("POST", "/chat/say", {"seat": other["seat"], "text": "hi from outside"})[0] == 200
        _, view = call("POST", "/chat/poll", {"seat": guest["seat"], "after": view["after"]})
        assert texts(view["events"], "stranger") == ["hi from outside"]

        # With the token, the same lobby has bots.
        _, member = call("POST", "/chat/start", {"want": "bot"}, token="sekrit")
        assert member["state"] == "talking" and member["partner"]["kind"] == "bot" and member["bots"] is True
    finally:
        hub.stop()
        srv.stop()


def test_a_lobby_that_is_not_public_follows_the_token(tmp_path):
    from pyrtan.hub import serve_hub

    hub, srv = serve_hub(port=0, quiet=True, name="closed", token="sekrit", chat=True)
    try:
        assert http_json("GET", f"{srv.url}/chat")[0] == 401
        assert http_json("POST", f"{srv.url}/chat/start", {"want": "people"})[0] == 401
        _, api = http_json("GET", f"{srv.url}/api")
        assert api["chat_public"] is False and api["chat_bots"] is False
        assert http_json("GET", f"{srv.url}/chat", token="sekrit")[0] == 200
    finally:
        hub.stop()
        srv.stop()


def heard_of(a, b):
    """``a`` heard of ``b`` in its directory, outside any network (as gossip would bring it)."""
    a.fed.directory_entries[b.url] = {"id": b.url, "name": b.hub.name, "trees": [], "seen": time.time()}


def test_chat_links_share_strangers_and_nothing_else():
    a, b = Lobbyist("a", chat_reach=4), Lobbyist("b", chat_reach=4)
    try:
        heard_of(a, b)
        assert a.hub.lobby.reach_out() == 1
        assert list(a.hub.lobby.links) == [b.url] and list(b.hub.lobby.links) == [a.url]
        # A link is not a follow: nothing is routed, announced or kept over it.
        assert a.fed.following == {} and a.fed.followers == {} and b.fed.following == {} and b.fed.followers == {}
        assert all(h.url != b.url for h in a.hub.holders())
        assert a.call("/chat")[1]["hubs"] == 1 and a.call("/peers")[1]["chat_links"]["links"][0]["url"] == b.url

        # a's stranger meets b's, over the link, and they talk both ways.
        there = start(b.hub, "people")
        _, here = a.call("/chat/start", {"want": "people"})
        assert here["state"] == "talking" and "random stranger on b" in " ".join(texts(here["events"], "system"))
        a.call("/chat/say", {"seat": here["seat"], "text": "hi from a"})
        events, view = until(b.hub, there["seat"], lambda ev, v: texts(ev, "stranger"), after=there["after"])
        assert texts(events, "stranger") == ["hi from a"]
        post(b.hub, "/chat/say", {"seat": there["seat"], "text": "hi from b"})
        until(a.hub, here["seat"], lambda ev, v: texts(ev, "stranger"), after=here["after"])
        # Either end may ask: the link goes both ways.
        a.call("/chat/leave", {"seat": here["seat"]})
        post(b.hub, "/chat/leave", {"seat": there["seat"]})
        waiting = start(a.hub, "people")
        met = start(b.hub, "people")
        assert met["state"] == "talking" and "random stranger on a" in " ".join(texts(met["events"], "system"))
        # The secret a link carries stays out of the logs.
        sent = [x for x in a.fed.outbox if x["type"] == "ChatLink"]
        assert sent and sent[0]["secret"] == "<secret>" and all(x.get("secret") in (None, "<secret>") for x in list(b.fed.inbox_log) if x["type"] == "ChatLink")
        # Already linked: nothing more to do.
        assert a.hub.lobby.reach_out() == 0
        # b follows a later: a's lines over the link are still taken, signed with the link's secret.
        b.fed.follow(a.url)
        post(b.hub, "/chat/leave", {"seat": met["seat"]})
        post(a.hub, "/chat/leave", {"seat": waiting["seat"]})
        there = start(b.hub, "people")
        _, here = a.call("/chat/start", {"want": "people"})
        assert here["state"] == "talking" and a.call("/chat/say", {"seat": here["seat"], "text": "still here"})[0] == 200
        events, _ = until(b.hub, there["seat"], lambda ev, v: texts(ev, "stranger"), after=there["after"])
        assert texts(events, "stranger") == ["still here"]
    finally:
        a.stop()
        b.stop()


def test_who_takes_a_chat_link():
    a = Lobbyist("a", chat_reach=4)
    unwilling = Lobbyist("unwilling")                                            # no --chat-reach
    private = Lobbyist("private", chat_reach=4, token="t")                       # a token, and a lobby that is not public
    closed = Lobbyist("closed", chat_reach=4)
    closed.fed.open_follows = False                                              # takes no follows: no links either
    try:
        for other in (unwilling, private, closed):
            heard_of(a, other)
        assert a.hub.lobby.reach_out() == 0 and len(a.hub.lobby._refused) == 2  # two tried per round, both refused
        assert a.hub.lobby.reach_out() == 0 and len(a.hub.lobby._refused) == 3  # then the third, refused too
        assert a.hub.lobby.links == {} and all(o.hub.lobby.links == {} for o in (unwilling, private, closed))
        assert {u: why for u, (_, why) in a.hub.lobby._refused.items()} == {unwilling.url: "not-linking", private.url: "private-lobby", closed.url: "closed"}
        assert {r["reason"] for r in a.hub.lobby.links_view()["refused"]} == {"not-linking", "private-lobby", "closed"}
        assert a.hub.lobby.reach_out() == 0  # a hub that refused is not asked again today
        # A Meet from a hub it neither follows, is followed by, nor links with, goes nowhere.
        from pyrtan.federation import Remote

        stranger = Remote(a.url, a.fed.inbox, "x" * 32, "a", state="accepted", kind="hub")
        ok, _, answer = unwilling.fed.deliver(stranger, unwilling.fed._activity("Meet", {"ref": "r" * 16}))
        assert not ok and answer["reason"] == "unknown-actor"
        # A public lobby with a token links: its strangers are welcome anyway.
        public = Lobbyist("public", chat_reach=4, token="t", chat_public=True)
        try:
            assert a.hub.lobby.link(public.url) is True and a.url in public.hub.lobby.links
        finally:
            public.stop()
    finally:
        for h in (a, unwilling, private, closed):
            h.stop()


def test_a_link_that_stops_answering_is_dropped():
    a, b = Lobbyist("a", chat_reach=1), Lobbyist("b", chat_reach=1)
    try:
        assert a.hub.lobby.link(b.url)
        lobby = a.hub.lobby
        lobby._link_answered(b.url, False)
        lobby._link_answered(b.url, True)  # an answer resets the count
        for _ in range(3):
            lobby._link_answered(b.url, False)
        assert lobby.links == {}
    finally:
        a.stop()
        b.stop()
