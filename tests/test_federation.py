"""Two hubs, each with its own service, following each other over real
sockets: who may ask whom, what is announced, and what is refused."""

import json
import time

import pytest

from pyrtan import Registry, RecordedTransport
from pyrtan.cli import main
from pyrtan.federation import Federation, Remote, sign, verify
from pyrtan.hub import Hub
from pyrtan.service import Server, Service, http_json
from conftest import EXAMPLES, FIXTURES

ECHO = """
name = "{name}"
description = "echoes"
start = "say"

[[triggers]]
pattern = "{word} {{text}}"

[inputs.text]
type = "str"

[nodes.say]
kind = "done"
say = "{name}: {{{{ inputs.text }}}}"
output = {{ text = "{{{{ inputs.text }}}}" }}
"""


class Swarm:
    """A hub with a token, federated, holding one service with the given trees."""

    def __init__(self, name: str, tree_dir, token: str | None = None, **fed):
        self.name = name
        self.token = token
        self.hub = Hub(name=name, poll_every=60, token=token)
        self.hub_srv = Server(self.hub.route, quiet=True, token=token, federated=True).start()
        self.fed = self.hub.federate(self.hub_srv.url, **fed)
        self.svc = Service(Registry.from_paths([tree_dir]), name=f"{name}-svc", transport=RecordedTransport.from_file(FIXTURES / "examples.json"), token=token, hub=self.hub_srv.url)
        self.svc_srv = Server(self.svc.route, quiet=True, token=token).start()
        assert self.svc.register(self.svc_srv.url)

    @property
    def url(self):
        return self.hub_srv.url

    def get(self, path):
        return http_json("GET", f"{self.url}{path}", token=self.token)

    def post(self, path, body):
        return http_json("POST", f"{self.url}{path}", body, token=self.token)

    def stop(self):
        self.hub.stop()
        self.hub_srv.stop()
        self.svc_srv.stop()


@pytest.fixture
def two(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "alpha.toml").write_text(ECHO.format(name="alpha", word="alpha"))
    (tmp_path / "b" / "beta.toml").write_text(ECHO.format(name="beta", word="beta"))
    a = Swarm("a", tmp_path / "a", token="token-a")
    b = Swarm("b", tmp_path / "b", token="token-b")
    yield a, b
    a.stop()
    b.stop()


def test_follow_accept_announce_and_route(two):
    a, b = two
    status, actor = http_json("GET", f"{b.url}/actor")  # public, no token
    assert status == 200 and actor["type"] == "Hub" and actor["inbox"] == f"{b.url}/inbox" and actor["follows"] == "open"

    status, out = a.post("/follow", {"url": b.url})
    assert status == 200 and out["following"]["url"] == b.url
    # The handshake is synchronous: B fetched A's actor, accepted, announced.
    assert a.fed.following[b.url].state == "accepted" and a.fed.following[b.url].link == "up"
    assert b.fed.followers[a.url].state == "accepted"
    assert [t["name"] for t in a.fed.following[b.url].trees] == ["beta"]

    _, caps = a.get("/capabilities")
    beta = next(t for t in caps["trees"] if t["name"] == "beta")
    assert beta["via"] == "federation" and beta["peer"] == b.url and beta["link"] == "up"
    _, peers = a.get("/peers")
    assert peers["id"] == a.url and peers["following"][0]["url"] == b.url and peers["followers"] == []
    _, peers = b.get("/peers")
    assert peers["followers"][0]["url"] == a.url and peers["following"] == []

    # A asks for B's tree: routed over federation, signed, one hop, and the answer says so.
    _, out = a.post("/intent", {"text": "beta hello"})
    assert out["status"] == "done" and out["say"] == "beta: hello"
    assert out["peer"] == b.svc_srv.url and out["via"] == [b.url]
    assert out["trace"]["steps"][0]["kind"] == "intent"
    _, out = a.post("/run", {"tree": "beta", "inputs": {"text": "x"}})
    assert out["status"] == "done" and out["via"] == [b.url]
    # Its own service still comes first for its own trees.
    _, out = a.post("/intent", {"text": "alpha hi"})
    assert out["status"] == "done" and "via" not in out

    # Being followed grants nothing: B does not route to A.
    _, caps = b.get("/capabilities")
    assert [t["name"] for t in caps["trees"]] == ["beta"]
    _, out = b.post("/intent", {"text": "alpha hi"})
    assert out["reason"] == "no-match"

    _, box = a.get("/outbox")
    assert [x["type"] for x in box["outbox"]] == ["Follow"] and box["outbox"][0]["delivered"]
    assert [x["type"] for x in box["inbox"]] == ["Accept", "Announce"]


def test_signed_requests_replace_the_token_only_for_hubs_we_know(two):
    a, b = two
    a.post("/follow", {"url": b.url})
    secret = a.fed.following[b.url].secret
    body = {"tree": "beta", "inputs": {"text": "x"}}
    # No token, no signature: refused.
    assert http_json("POST", f"{b.url}/run", body)[0] == 401
    # Signed by A with the pair secret: served.
    status, out = http_json("POST", f"{b.url}/run", body, headers=sign(secret, a.url, "POST", "/run", body))
    assert status == 200 and out["status"] == "done"
    # Wrong secret, wrong actor, stale date: refused.
    assert http_json("POST", f"{b.url}/run", body, headers=sign("nope", a.url, "POST", "/run", body))[0] == 401
    assert http_json("POST", f"{b.url}/run", body, headers=sign(secret, "http://stranger", "POST", "/run", body))[0] == 401
    assert http_json("POST", f"{b.url}/run", body, headers=sign(secret, a.url, "POST", "/run", body, date=time.time() - 3600))[0] == 401
    # A signature does not open the operator's paths.
    assert http_json("GET", f"{b.url}/peers", headers=sign(secret, a.url, "GET", "/peers", None))[0] == 401
    assert http_json("POST", f"{b.url}/follow", {"url": a.url}, headers=sign(secret, a.url, "POST", "/follow", {"url": a.url}))[0] == 401
    # The inbox checks the activity's actor against the signature's.
    announce = {"type": "Announce", "actor": a.url, "object": {"type": "Capabilities", "trees": []}}
    status, out = http_json("POST", f"{b.url}/inbox", announce, headers=sign(secret, a.url, "POST", "/inbox", announce))
    assert status == 403 and out["reason"] == "unknown-actor"  # B does not follow A, so A cannot announce to it
    bad = {"type": "Announce", "actor": b.url, "object": {"type": "Capabilities", "trees": []}}
    assert http_json("POST", f"{a.url}/inbox", bad, headers=sign("nope", b.url, "POST", "/inbox", bad))[0] == 401
    assert http_json("POST", f"{a.url}/inbox", {"type": "Dance", "actor": b.url}, headers=sign(secret, b.url, "POST", "/inbox", {"type": "Dance", "actor": b.url}))[0] == 400


def test_federation_is_one_hop(two, tmp_path):
    a, b = two
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "gamma.toml").write_text(ECHO.format(name="gamma", word="gamma"))
    c = Swarm("c", tmp_path / "c")
    try:
        c.post("/follow", {"url": a.url})
        a.post("/follow", {"url": b.url})
        # C knows A's own trees, not what A learned from B.
        _, caps = c.get("/capabilities")
        assert {t["name"] for t in caps["trees"]} == {"gamma", "alpha"}
        assert c.post("/intent", {"text": "beta hello"})[1]["reason"] == "no-match"
        assert c.post("/run", {"tree": "beta", "inputs": {"text": "x"}})[1]["reason"] == "no-such-tree"
        # A request that already crossed a hub is not forwarded again.
        secret = c.fed.following[a.url].secret
        body = {"tree": "beta", "inputs": {"text": "x"}, "hops": [c.url]}
        status, out = http_json("POST", f"{a.url}/run", body, headers=sign(secret, c.url, "POST", "/run", body))
        assert status == 200 and out["reason"] == "hop-limit"
        # A mutual follow does not loop: a sentence nobody answers is simply unanswered.
        b.post("/follow", {"url": a.url})
        assert a.post("/intent", {"text": "nobody answers this"})[1]["reason"] == "no-match"
        assert b.post("/intent", {"text": "alpha hi"})[1]["status"] == "done"
    finally:
        c.stop()


def test_follow_policy_secret_and_closed(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "alpha.toml").write_text(ECHO.format(name="alpha", word="alpha"))
    guarded = Swarm("guarded", tmp_path / "t", secret="federation-secret")
    stranger = Swarm("stranger", tmp_path / "t")
    insider = Swarm("insider", tmp_path / "t", secret="federation-secret")
    closed = Swarm("closed", tmp_path / "t", open_follows=False)
    try:
        assert http_json("GET", f"{guarded.url}/actor")[1]["follows"] == "secret"
        stranger.post("/follow", {"url": guarded.url})
        assert stranger.fed.following[guarded.url].state == "pending"
        assert "federation secret" in stranger.fed.following[guarded.url].last_error
        assert stranger.url not in guarded.fed.followers
        insider.post("/follow", {"url": guarded.url})
        assert insider.fed.following[guarded.url].state == "accepted"
        stranger.post("/follow", {"url": closed.url})
        assert stranger.fed.following[closed.url].state == "pending" and "does not take follows" in stranger.fed.following[closed.url].last_error
        # A closed hub can still follow.
        closed.post("/follow", {"url": stranger.url})
        assert closed.fed.following[stranger.url].state == "accepted"
        status, out = stranger.post("/follow", {"url": stranger.url})
        assert status == 400 and "itself" in out["say"]
        status, out = stranger.post("/follow", {"url": stranger.svc_srv.url})
        assert status == 400 and "does not answer /actor" in out["say"]
    finally:
        for s in (guarded, stranger, insider, closed):
            s.stop()


def test_unfollow_announce_on_change_and_state(two, tmp_path):
    a, b = two
    state = tmp_path / "a-state.json"
    a.fed.state_path = state
    a.post("/follow", {"url": b.url})
    assert json.loads(state.read_text())["following"][0]["url"] == b.url
    # B gains a tree; its next poll notices and announces; A learns without polling B.
    (tmp_path / "b" / "beta2.toml").write_text(ECHO.format(name="beta-two", word="betatwo"))
    b.hub.refresh_all()
    assert {t["name"] for t in a.fed.following[b.url].trees} == {"beta", "beta-two"}
    assert b.fed.announce() == 0  # nothing changed since: no delivery
    # A fresh federation over the same file remembers the pair.
    again = Federation(a.url, state=state)
    assert again.following[b.url].secret == a.fed.following[b.url].secret and again.following[b.url].state == "accepted"
    # Unfollow: both sides forget, and A stops routing there.
    status, out = a.post("/unfollow", {"url": b.url})
    assert status == 200 and out["removed"] is True
    assert b.url not in a.fed.following and a.url not in b.fed.followers
    assert a.post("/intent", {"text": "beta hello"})[1]["reason"] == "no-match"
    assert a.post("/unfollow", {"url": b.url})[1]["removed"] is False
    assert json.loads(state.read_text()) == {"following": [], "followers": []}


def test_unfederated_hub_has_no_inbox(tmp_path):
    hub = Hub()
    srv = Server(hub.route, quiet=True).start()
    try:
        assert http_json("GET", f"{srv.url}/actor")[0] == 404
        assert http_json("POST", f"{srv.url}/follow", {"url": "http://x"})[0] == 404
        assert http_json("GET", f"{srv.url}/api")[1]["federation"] is None
    finally:
        srv.stop()


def test_cli_follow_peers_unfollow(two, capsys):
    a, b = two

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    code, out, _ = run("follow", b.url, "--at", a.url, "--token", "token-a")
    assert code == 0 and "now follows" in out and "accepted" in out
    code, out, _ = run("peers", "--at", a.url, "--token", "token-a")
    assert code == 0 and "follows" in out and "beta" in out and "a-svc" in out
    code, out, _ = run("peers", "--at", b.url, "--token", "token-b")
    assert "follower" in out
    assert run("follow", b.url, "--at", a.url)[0] == 3  # no token
    code, out, _ = run("unfollow", b.url, "--at", a.url, "--token", "token-a")
    assert code == 0 and "no longer follows" in out
    code, _, err = run("follow", "http://127.0.0.1:9", "--at", a.url, "--token", "token-a")
    assert code == 1 and "does not answer /actor" in err


def test_signing_primitives():
    headers = sign("s", "http://a", "POST", "/inbox", {"b": 1, "a": [1, 2]})
    assert verify("s", "POST", "/inbox", {"a": [1, 2], "b": 1}, headers) is None  # key order does not matter
    assert verify("s", "POST", "/inbox", {"a": [1, 2], "b": 2}, headers) == "bad signature"
    assert verify("t", "POST", "/inbox", {"a": [1, 2], "b": 1}, headers) == "bad signature"
    assert verify("s", "POST", "/other", {"a": [1, 2], "b": 1}, headers) == "bad signature"
    assert verify("s", "POST", "/inbox", {"a": [1, 2], "b": 1}, {}) == "no signature"
    assert verify("s", "POST", "/inbox", {"a": [1, 2], "b": 1}, headers, now=time.time() + 1000) == "signature too old"
    assert Remote("http://b", "http://b/inbox", "s").link == "pending"
    assert Remote("http://b", "http://b/inbox", "s", state="accepted").link == "pending"  # accepted, not heard from yet
    assert Remote("http://b", "http://b/inbox", "s", state="accepted", last_error="x").link == "down"
    assert Remote("http://b", "http://b/inbox", "s", state="rejected").link == "down"
