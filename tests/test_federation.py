"""Two hubs, each with its own service, following each other over real
sockets: who may ask whom, what is announced, and what is refused."""

import json
import threading
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
    assert [x["type"] for x in box["outbox"]] == ["Follow", "Announce"] and all(x["delivered"] for x in box["outbox"])  # the Follow, then our announce upward
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
    # A follows B, so B takes A's announce for the directory it carries, and nothing else:
    # being followed grants nothing, so B still routes only to its own trees.
    assert status == 200 and out["status"] == "ok" and out["trees"] == 0
    assert [t["name"] for t in b.get("/capabilities")[1]["trees"]] == ["beta"]
    stranger = {"type": "Announce", "actor": "http://stranger", "object": {"type": "Capabilities", "trees": []}}
    assert http_json("POST", f"{b.url}/inbox", stranger, headers=sign(secret, "http://stranger", "POST", "/inbox", stranger))[0] == 403
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
    saved = json.loads(state.read_text())
    assert saved["following"] == [] and saved["followers"] == [] and saved["listed"] is False


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


def test_directory_is_gossiped_in_both_directions_and_routes_to_nobody(two, tmp_path):
    a, b = two
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "gamma.toml").write_text(ECHO.format(name="gamma", word="gamma"))
    c = Swarm("c", tmp_path / "c", listed=True)
    try:
        # B lists itself; A follows B and hears about B in the Accept's Announce.
        b.fed.set_listed(True)
        a.post("/follow", {"url": b.url})
        status, d = http_json("GET", f"{a.url}/directory")  # public
        assert status == 200 and d["hub"] == a.url and [h["id"] for h in d["hubs"]] == [b.url]
        assert d["hubs"][0]["trees"] == ["beta"] and d["hubs"][0]["link"] == "up" and d["hubs"][0]["via"] == b.url
        # C (listed) follows B: it announces upward, so B hears of C; A hears of C from B on B's next announce.
        c.post("/follow", {"url": b.url})
        assert {h["id"] for h in b.fed.directory()["hubs"]} == {b.url, c.url}
        assert c.url not in {h["id"] for h in a.fed.directory()["hubs"]}
        b.hub.refresh_all()  # B announces downward (the directory changed) and A merges
        assert {h["id"] for h in a.fed.directory()["hubs"]} == {b.url, c.url}
        assert next(h for h in a.fed.directory()["hubs"] if h["id"] == c.url)["via"] == b.url
        # And C hears of nobody but B until B announces to it; after that, the same two.
        assert {h["id"] for h in c.fed.directory()["hubs"]} >= {b.url, c.url}
        # A is not listed, so it appears nowhere; listing itself puts it in its own gossip and up to B.
        assert a.url not in {h["id"] for h in b.fed.directory()["hubs"]}
        a.post("/listed", {"listed": True})
        assert a.url in {h["id"] for h in b.fed.directory()["hubs"]}
        assert a.fed.directory()["hubs"][0]["id"] == a.url and a.fed.directory()["hubs"][0]["via"] == "self"
        # Nothing in the directory is routed to: B still answers only for itself.
        assert b.post("/intent", {"text": "alpha hello"})[1]["reason"] == "no-match"
        assert [t["name"] for t in b.get("/capabilities")[1]["trees"]] == ["beta"]
        # The directory survives a restart through the state file.
        state = tmp_path / "state.json"
        a.fed.state_path = state
        a.fed._save()
        again = Federation(a.url, state=state)
        assert again.listed and {h["id"] for h in again.directory()["hubs"]} == {a.url, b.url, c.url}
        # Merge rules: our own entry is never overwritten, the future is not believed, old news is dropped.
        a.fed.merge_directory([{"id": a.url, "name": "impostor", "trees": ["x"], "seen": time.time() + 1e6}], via="x")
        assert a.fed.directory()["hubs"][0]["name"] == "a"
        a.fed.merge_directory([{"id": "http://future", "trees": [], "seen": time.time() + 1e6}], via="x")
        assert abs(a.fed.directory_entries["http://future"]["seen"] - time.time()) < 5
        assert a.fed.merge_directory([{"id": "http://old", "trees": [], "seen": time.time() - 30 * 86400}], via="x") == 0
        assert a.fed.merge_directory([{"id": "not a url"}, "junk", {"id": "http://ok", "seen": "soon"}], via="x") == 0
        assert "/directory" in http_json("GET", f"{a.url}/api")[1]["endpoints"]
    finally:
        c.stop()


def test_seeds_and_cli_defaults(two, capsys, monkeypatch, tmp_path):
    a, b = two
    from pyrtan.federation import first_live_seed, seed_urls

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps({"pyrtan-seeds": 1, "hubs": ["http://127.0.0.1:9", b.url, a.url]}))
    monkeypatch.setenv("PYRTAN_SEEDS", str(seeds))
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(tmp_path / "no-snapshot.json"))  # never reach pyrtan.com from a test
    monkeypatch.delenv("PYRTAN_RENDEZVOUS", raising=False)
    assert seed_urls() == ["http://127.0.0.1:9", b.url, a.url]
    assert first_live_seed() == b.url  # the dead one is skipped
    monkeypatch.setenv("PYRTAN_RENDEZVOUS", a.url)
    assert seed_urls() == [a.url]
    monkeypatch.delenv("PYRTAN_RENDEZVOUS")
    b.fed.set_listed(True)
    code, out, _ = run("follow", "--at", a.url, "--token", "token-a", "--listed")  # no URL: the first live seed
    assert code == 0 and f"now follows {b.url}" in out and "listed in the directory" in out
    assert a.fed.listed and a.url in {h["id"] for h in b.fed.directory()["hubs"]}
    monkeypatch.setenv("PYRTAN_AT", "http://127.0.0.1:9")  # the local default is dead: fall through to the seeds
    code, out, _ = run("directory")
    assert code == 0 and f"as b ({b.url})" in out and a.url in out and b.url in out
    code, out, _ = run("directory", "--at", a.url, "--json")
    assert {h["id"] for h in json.loads(out)["hubs"]} == {a.url, b.url}
    assert run("directory", "--at", b.svc_srv.url)[0] == 3
    monkeypatch.setenv("PYRTAN_SEEDS", str(tmp_path / "missing.json"))
    assert seed_urls() == []
    code, _, err = run("follow", "--at", a.url, "--token", "token-a")
    assert code == 3 and "no seed or snapshot hub answers" in err


def test_hub_holds_trees_itself(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "alpha.toml").write_text(ECHO.format(name="alpha", word="alpha"))
    (tmp_path / "t" / "picnic.toml").write_text("""
name = "picnic"
description = "delegates to the hub it lives in"
start = "ask"

[[triggers]]
pattern = "picnic {text}"

[inputs.text]
type = "str"

[nodes.ask]
kind = "delegate"
intent = "alpha {{ inputs.text }}"
into = "a"
next = "say"

[nodes.say]
kind = "done"
say = "picnic says {{ state.a.say }}"
""")
    from pyrtan.hub import serve_hub

    hub, srv = serve_hub(port=0, quiet=True, name="solo", trees=[tmp_path / "t"], federate=True)
    try:
        _, health = http_json("GET", f"{srv.url}/health")
        assert health["local"] == 2 and health["trees"] == 2 and health["peers"] == 0
        _, caps = http_json("GET", f"{srv.url}/capabilities")
        assert {(t["name"], t["peer"]) for t in caps["trees"]} == {("alpha", "local"), ("picnic", "local")}
        _, out = http_json("POST", f"{srv.url}/intent", {"text": "alpha hi"})
        assert out["status"] == "done" and out["say"] == "alpha: hi" and out["peer"] == "local"
        assert out["trace"]["steps"][0]["kind"] == "intent"
        # A local tree's delegate goes back through the hub, in-process.
        _, out = http_json("POST", f"{srv.url}/run", {"tree": "picnic", "inputs": {"text": "now"}})
        assert out["status"] == "done" and out["say"] == "picnic says alpha: now"
        _, peers = http_json("GET", f"{srv.url}/peers")
        assert peers["peers"][0]["kind"] == "local" and peers["peers"][0]["trees"] == ["alpha", "picnic"]
        assert http_json("GET", f"{srv.url}/gaps")[1]["gaps"] == []
        # Hot reload holds for local trees too, and the announce digest follows.
        (tmp_path / "t" / "beta.toml").write_text(ECHO.format(name="beta", word="beta"))
        assert "beta" in {t["name"] for t in hub.local_capabilities()}
    finally:
        hub.stop()
        srv.stop()


def test_snapshot_bakes_and_bootstraps(two, tmp_path, monkeypatch, capsys):
    """site/snapshot.py merges the seeds' directories with the last snapshot,
    and the command falls back to the snapshot when nothing is live."""
    import subprocess
    import sys as _sys

    a, b = two
    b.fed.set_listed(True)
    a.post("/follow", {"url": b.url})
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps({"pyrtan-seeds": 1, "hubs": ["http://127.0.0.1:9", a.url]}))
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"pyrtan-directory": 1, "hubs": [{"id": "http://gone.example", "name": "gone", "trees": ["x"], "seen": time.time() - 3600}, {"id": "http://ancient.example", "trees": [], "seen": time.time() - 30 * 86400}]}))
    out = tmp_path / "directory.json"
    run = subprocess.run([_sys.executable, "site/snapshot.py", str(seeds), str(out), "--previous", str(tmp_path / "missing.json"), "--previous", str(old)], capture_output=True, text=True, cwd=str(EXAMPLES.parent))
    assert run.returncode == 0, run.stderr
    snap = json.loads(out.read_text())
    assert snap["pyrtan-directory"] == 1 and snap["seeds"]["answered"] == [a.url] and snap["seeds"]["silent"][0]["seed"] == "http://127.0.0.1:9"
    assert snap["seeds"]["previous"] == str(old)
    ids = [h["id"] for h in snap["hubs"]]
    assert b.url in ids and "http://gone.example" in ids and "http://ancient.example" not in ids  # live from the seed, kept from the snapshot, expired
    assert next(h for h in snap["hubs"] if h["id"] == b.url)["via"] == a.url

    def cli(*argv):
        code = main(list(argv))
        o = capsys.readouterr()
        return code, o.out, o.err

    from pyrtan.federation import bootstrap_candidates, first_live_seed, snapshot_hubs

    # With every seed dead, the snapshot's hubs are the next place to look.
    monkeypatch.setenv("PYRTAN_SEEDS", str(tmp_path / "dead-seeds.json"))
    (tmp_path / "dead-seeds.json").write_text(json.dumps({"hubs": ["http://127.0.0.1:9"]}))
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(out))
    monkeypatch.delenv("PYRTAN_RENDEZVOUS", raising=False)
    assert snapshot_hubs()[0] == b.url  # most recently heard first
    assert bootstrap_candidates() == ["http://127.0.0.1:9", b.url, "http://gone.example"]
    assert first_live_seed() == b.url
    code, o, _ = cli("follow", "--at", a.url, "--token", "token-a")
    assert code == 0 and f"now follows {b.url}" in o
    monkeypatch.setenv("PYRTAN_AT", "http://127.0.0.1:9")
    b.stop()  # nothing live at all now: the snapshot, with its age, and link recomputed
    code, o, _ = cli("directory")
    assert code == 0 and "snapshot as of" in o and b.url in o and "gone" in o
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(tmp_path / "nope.json"))
    assert cli("directory")[0] == 3


# ---------------------------------------------------------------- members and keeping


class Member:
    """A browser or a phone, in Python: no inbox, a mailbox it polls."""

    def __init__(self, hub_url: str, name: str = "phone"):
        import secrets as _secrets

        self.id = f"urn:pyrtan:member:{_secrets.token_hex(16)}"
        self.secret = _secrets.token_hex(32)
        self.hub = hub_url.rstrip("/")
        self.name = name
        self.kept: dict[str, dict] = {}
        self.directory: list[dict] = []

    def post(self, activity: dict, secret: str | None = None):
        activity = {"id": f"{self.id}/activities/{len(self.kept)}", "actor": self.id, "published": "now", **activity}
        return http_json("POST", f"{self.hub}/inbox", activity, headers=sign(secret or self.secret, self.id, "POST", "/inbox", activity))

    def follow(self, secret: str | None = None):
        return self.post({"type": "Follow", "object": self.hub, "secret": self.secret, "inbox": "mailbox", "name": self.name}, secret)

    def poll(self) -> list[dict]:
        status, body = http_json("GET", f"{self.hub}/mailbox", headers=sign(self.secret, self.id, "GET", "/mailbox", None))
        assert status == 200, body
        acts = body["activities"]
        for a in acts:
            if a["type"] == "Keep":
                self.kept[a["object"]["key"]] = a["object"]
                self.post({"type": "Kept", "object": {"key": a["object"]["key"], "sha256": a["object"]["sha256"]}})
            elif a["type"] == "Fetch":
                blob = self.kept.get(a["object"]["key"])
                if blob:
                    self.post({"type": "Kept", "object": {"key": blob["key"], "sha256": blob["sha256"], "data": blob["data"]}})
            elif a["type"] == "Announce":
                self.directory = a["object"].get("directory", [])
        return acts

    def announce(self, listed: bool = True):
        own = [{"id": self.id, "name": self.name, "kind": "member", "trees": [], "seen": time.time()}] if listed else []
        return self.post({"type": "Announce", "object": {"type": "Capabilities", "name": self.name, "trees": [], "directory": own, "kept": sorted(self.kept)}})


def test_member_follows_through_a_mailbox_and_keeps_blobs(two, tmp_path):
    a, b = two
    a.fed.store = __import__("pyrtan.store", fromlist=["BlobStore"]).BlobStore(tmp_path / "store-a")
    m = Member(a.url)
    status, out = m.follow()
    assert status == 202 and out["mailbox"] == f"{a.url}/mailbox"
    assert a.fed.followers[m.id].kind == "member" and a.fed.followers[m.id].inbox == "mailbox"
    # The Accept and the first Announce wait in the mailbox; polling drains it.
    acts = m.poll()
    assert [x["type"] for x in acts] == ["Accept", "Announce"] and m.poll() == []
    _, peers = a.get("/peers")
    assert peers["followers"][0]["kind"] == "member" and peers["followers"][0]["link"] == "up"
    # The member lists itself by announcing upward; the hub's directory carries it as a member.
    status, out = m.announce()
    assert status == 200 and out["directory"] is not None
    entry = next(h for h in a.fed.directory()["hubs"] if h["id"] == m.id)
    assert entry["kind"] == "member" and entry["via"] == m.id
    # A blob of the hub's own goes to the member through its mailbox; the member's Kept confirms it.
    status, out = a.post("/keep", {"key": "notes/today", "data": {"todo": ["water plants"]}})
    assert status == 200 and out["keeping"]["holders"] == []  # nobody has confirmed yet
    acts = m.poll()
    assert [x["type"] for x in acts] == ["Keep"] and m.kept["notes/today"]["data"] == {"todo": ["water plants"]}
    assert a.fed.holders("notes/today") == [m.id]
    _, out = a.get("/keep")
    assert out["keeping"][0]["key"] == "notes/today" and out["keeping"][0]["holders"] == [m.id] and out["replicas"] == 2
    # Fetch: the hub still has its own copy, so no round trip.
    assert a.post("/fetch", {"key": "notes/today"})[1]["source"] == "own"
    # The hub loses its store; the member's Announce says what it keeps, the hub asks, the member answers, the blob is back.
    a.fed.store = __import__("pyrtan.store", fromlist=["BlobStore"]).BlobStore(None)
    assert a.get("/keep/notes/today")[0] == 404
    _, out = a.post("/fetch", {"key": "notes/today"})
    assert out["status"] == "asked" and out["asked"] == [m.id]
    acts = m.poll()
    assert [x["type"] for x in acts] == ["Fetch"]
    _, out = a.get("/keep/notes/today")
    assert out["data"] == {"todo": ["water plants"]} and out["holders"] == [m.id]
    # Or without asking: the member's next upward Announce names the key, the hub notices it has no copy and fetches.
    a.fed.store = __import__("pyrtan.store", fromlist=["BlobStore"]).BlobStore(None)
    m.announce()
    assert [x["type"] for x in m.poll()] == ["Fetch"]
    assert a.get("/keep/notes/today")[1]["data"] == {"todo": ["water plants"]}
    # Leaving takes the mailbox and the kept blobs with it.
    assert m.post({"type": "Undo", "object": {"type": "Follow", "actor": m.id, "object": a.url}})[0] == 200
    assert m.id not in a.fed.followers and m.id not in a.fed.mailboxes
    assert http_json("GET", f"{a.url}/mailbox", headers=sign(m.secret, m.id, "GET", "/mailbox", None))[0] == 401
    # A member's id has a shape; a stranger's signature opens nothing.
    bad = Member(a.url); bad.id = "urn:pyrtan:member:short"
    assert bad.follow()[0] == 400
    assert http_json("GET", f"{a.url}/mailbox", headers=sign("x", "urn:pyrtan:member:" + "0" * 32, "GET", "/mailbox", None))[0] == 401


def test_hubs_keep_for_each_other_and_the_store_has_limits(two, tmp_path):
    from pyrtan.store import MAX_BLOB, BlobStore, StoreError

    a, b = two
    a.fed.store = BlobStore(tmp_path / "a"); b.fed.store = BlobStore(tmp_path / "b")
    a.post("/follow", {"url": b.url})
    # A keeps a blob; B (a hub A follows) is a keeper and confirms in the same round trip.
    status, out = a.post("/keep", {"key": "gaps-archive", "data": [{"tree": "x"}]})
    assert status == 200 and out["keeping"]["holders"] == [b.url]
    assert b.fed.store.list_kept(a.url)[0]["key"] == "gaps-archive"
    assert [k["key"] for k in b.get("/keep")[1]["kept"]] == ["gaps-archive"]
    # A takes it back from B over the kept path, signed; a stranger cannot.
    path = "/kept/gaps-archive"
    status, blob = http_json("GET", f"{b.url}{path}", headers=sign(a.fed.following[b.url].secret, a.url, "GET", path, None))
    assert status == 200 and blob["data"] == [{"tree": "x"}]
    assert http_json("GET", f"{b.url}{path}", headers=sign("nope", a.url, "GET", path, None))[0] == 401
    assert http_json("GET", f"{b.url}/kept/other", headers=sign(a.fed.following[b.url].secret, a.url, "GET", "/kept/other", None))[0] == 404
    # Fetch after A's store is gone: B answers at once.
    a.fed.store = BlobStore(tmp_path / "a2")
    _, out = a.post("/fetch", {"key": "gaps-archive"})
    assert out["status"] == "ok" and out["source"] == b.url and out["blob"]["data"] == [{"tree": "x"}]
    # The automatic blobs appear on the poll and change when the directory does.
    a.hub.refresh_all()
    keys = {k["key"] for k in a.get("/keep")[1]["keeping"]}
    assert {"directory", "gaps", "unanswered", "gaps-archive"} <= keys
    # Limits: a key has a shape, a blob a size, an actor a budget; and the state file remembers who holds what.
    assert a.post("/keep", {"key": "Bad Key", "data": 1})[0] == 400
    assert a.post("/keep", {"key": "big", "data": "x" * (MAX_BLOB + 1)})[0] == 400
    with pytest.raises(StoreError):
        b.fed.store.put_kept(a.url, "k", {"x": 1}, sha256="wrong")
    state = tmp_path / "state.json"
    a.fed.state_path = state; a.fed._save()
    again = Federation(a.url, state=state, store=BlobStore(tmp_path / "a2"))
    assert again.keeping["gaps-archive"]["holders"][b.url]["sha256"] == a.fed.keeping["gaps-archive"]["sha256"]
    # Unfollow drops what B kept for A.
    a.post("/unfollow", {"url": b.url})
    assert b.fed.store.list_kept(a.url) == []


def test_replication_counts_fresh_holders_only(two, tmp_path):
    from pyrtan.store import BlobStore

    a, b = two
    a.fed.store = BlobStore(None); a.fed.replicas = 2
    m1, m2, m3 = Member(a.url, "m1"), Member(a.url, "m2"), Member(a.url, "m3")
    for m in (m1, m2, m3):
        m.follow(); m.poll()
    a.post("/keep", {"key": "k", "data": 1})
    # Two members got a Keep (replicas = 2), the third did not.
    got = [m for m in (m1, m2, m3) if any(x["type"] == "Keep" for x in m.poll())]
    assert len(got) == 2 and a.fed.holders("k") == sorted(m.id for m in got)
    assert a.fed.replicate() == 0  # enough holders: nothing sent
    # A holder goes stale: the next replicate sends to the third.
    a.fed.keeping["k"]["holders"][got[0].id]["at"] -= 2 * 86400
    assert a.fed.replicate() == 1
    third = next(m for m in (m1, m2, m3) if m not in got)
    assert [x["type"] for x in third.poll()] == ["Keep"] and len(a.fed.holders("k")) == 2
    # A new version of the blob needs confirming again.
    a.post("/keep", {"key": "k", "data": 2})
    assert a.fed.holders("k") == []
    for m in (m1, m2, m3):
        m.poll()
    assert len(a.fed.holders("k")) == 2


def test_cli_keep_kept_fetch(two, tmp_path, capsys):
    from pyrtan.store import BlobStore

    a, b = two
    a.fed.store = BlobStore(None)
    a.post("/follow", {"url": b.url})

    def run(*argv):
        code = main(list(argv))
        o = capsys.readouterr()
        return code, o.out, o.err

    f = tmp_path / "blob.json"; f.write_text(json.dumps({"hello": "world"}))
    code, out, _ = run("keep", "recipes/soup", str(f), "--at", a.url, "--token", "token-a")
    assert code == 0 and "keeps recipes/soup" in out and "held by 1 of 2" in out
    code, out, _ = run("kept", "--at", a.url, "--token", "token-a")
    assert code == 0 and "recipes/soup" in out and b.url in out
    code, out, _ = run("kept", "--at", b.url, "--token", "token-b")
    assert f"for {a.url}: recipes/soup" in out
    a.fed.store = BlobStore(None)
    code, out, _ = run("fetch", "recipes/soup", "--at", a.url, "--token", "token-a", "--out", str(tmp_path / "back.json"))
    assert code == 0 and f"from {b.url}" in out and json.loads((tmp_path / "back.json").read_text()) == {"hello": "world"}
    assert run("fetch", "nothing", "--at", a.url, "--token", "token-a")[0] == 1
    assert run("keep", "bad key", str(f), "--at", a.url, "--token", "token-a")[0] == 1


class TrustedMember(Member):
    """A member that runs trees: it holds what the hub hands it and answers Asks."""

    def __init__(self, hub_url: str, name: str = "phone", answers: dict | None = None):
        super().__init__(hub_url, name)
        self.trees: dict[str, dict] = {}
        self.asks: list[dict] = []
        self.answers = answers or {}

    def follow(self, secret: str | None = None, token: str | None = None):
        activity = {"id": f"{self.id}/activities/follow", "actor": self.id, "published": "now", "type": "Follow", "object": self.hub, "secret": self.secret, "inbox": "mailbox", "name": self.name}
        headers = sign(secret or self.secret, self.id, "POST", "/inbox", activity)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return http_json("POST", f"{self.hub}/inbox", activity, headers=headers)

    def poll(self) -> list[dict]:
        acts = super().poll()
        for a in acts:
            if a["type"] == "Keep" and a["object"]["key"].startswith("trees/"):
                self.trees[a["object"]["data"]["name"]] = a["object"]["data"]
            elif a["type"] == "Ask":
                self.asks.append(a["object"])
                outcome = self.answers.get(a["object"]["tree"])
                if outcome is not None:
                    self.post({"type": "Answer", "object": {"run": a["object"]["run"], "outcome": {**outcome, "output": {"inputs": a["object"]["inputs"]}}}})
        return acts

    def announce(self, listed: bool = True, trees: list | None = None):
        caps = trees if trees is not None else [{"name": n, "triggers": ["whatever the member says"]} for n in self.trees]
        return self.post({"type": "Announce", "object": {"type": "Capabilities", "name": self.name, "trees": caps, "directory": [], "kept": sorted(self.kept)}})


def test_trusted_member_runs_the_trees_its_hub_hands_out(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "h").mkdir()
    (tmp_path / "t" / "alpha.toml").write_text(ECHO.format(name="alpha", word="alpha"))
    (tmp_path / "h" / "gamma.toml").write_text(ECHO.format(name="gamma", word="gamma"))
    from pyrtan.hub import serve_hub

    hub, srv = serve_hub(port=0, quiet=True, name="solo", trees=[tmp_path / "t"], handout=[tmp_path / "h"], federate=True, token="tok", poll_every=60)
    fed = hub.federation
    try:
        # Joining with the hub's token makes a member trusted; without it, not.
        stranger = TrustedMember(srv.url, "stranger")
        assert stranger.follow()[1]["trusted"] is False
        assert [a["type"] for a in stranger.poll()] == ["Accept", "Announce"]
        stranger.announce(trees=[{"name": "alpha", "triggers": ["alpha {text}"]}])
        assert fed.followers[stranger.id].trees == [] and "alpha" not in stranger.trees
        phone = TrustedMember(srv.url, "phone", answers={"gamma": {"status": "done", "say": "gamma, from the phone", "trace": {"tree": "gamma", "steps": [{"node": "say", "kind": "done", "depth": 0}], "gaps": []}}})
        status, out = phone.follow(token="tok")
        assert status == 202 and out["trusted"] is True and fed.followers[phone.id].trusted
        # The hub hands out its own trees and the --handout ones at once, in the mailbox after the Accept and the Announce.
        acts = phone.poll()
        assert [a["type"] for a in acts] == ["Accept", "Announce", "Keep", "Keep"]
        assert sorted(phone.trees) == ["alpha", "gamma"] and phone.trees["gamma"]["name"] == "gamma"
        assert all(h["confirmed"] for h in fed._handed[phone.id].values())
        assert fed.hand_out() == 0  # nothing new to give
        # What the member says it runs is filtered to what it was handed, with the hub's own triggers.
        status, out = phone.announce(trees=[{"name": "gamma", "triggers": ["nonsense"]}, {"name": "alpha", "triggers": ["alpha {text}"]}, {"name": "zeta", "triggers": ["zeta"]}])
        assert status == 200
        caps = {t["name"]: t for t in fed.followers[phone.id].trees}
        assert sorted(caps) == ["alpha", "gamma"] and caps["gamma"]["triggers"] == ["gamma {text}"]
        _, out = http_json("GET", f"{srv.url}/capabilities", token="tok")
        assert {(t["name"], t["peer"], t.get("via")) for t in out["trees"]} == {("alpha", "local", None), ("alpha", phone.id, "member"), ("gamma", phone.id, "member")}
        # A sentence for gamma goes to the phone: an Ask in its mailbox, its Answer back to the caller.
        got: dict = {}

        def call():
            got["out"] = http_json("POST", f"{srv.url}/intent", {"text": "gamma hi there"}, token="tok")[1]

        t = threading.Thread(target=call)
        t.start()
        deadline = time.time() + 5
        while not phone.asks and time.time() < deadline:
            phone.poll()
            time.sleep(0.05)
        t.join(5)
        assert phone.asks and phone.asks[0]["tree"] == "gamma" and phone.asks[0]["inputs"] == {"text": "hi there"}
        out = got["out"]
        assert out["status"] == "done" and out["say"] == "gamma, from the phone"
        assert out["peer"] == phone.id and out["via"] == ["member"] and out["output"] == {"inputs": {"text": "hi there"}}
        assert [s["kind"] for s in out["trace"]["steps"]] == ["intent", "done"] and out["trace"]["steps"][0]["next"] == "say"
        # A tree the hub runs itself stays on the hub, even though the phone holds it too.
        _, out = http_json("POST", f"{srv.url}/intent", {"text": "alpha hi"}, token="tok")
        assert out["status"] == "done" and out["peer"] == "local"
        # An answer nobody waits for, or from the wrong member, lands nowhere.
        assert phone.post({"type": "Answer", "object": {"run": "nope", "outcome": {"status": "done"}}})[0] == 404
        fed.ask_timeout = 0.2
        pending = fed.ask(fed.followers[phone.id], "gamma", {"text": "x"}, timeout=0.5)  # the phone is not polling
        assert pending["status"] == "error" and pending["reason"] == "member-timeout" and pending["tree"] == "gamma"
        phone.answers = {}
        _, out = http_json("POST", f"{srv.url}/run", {"tree": "gamma", "inputs": {"text": "x"}}, token="tok")
        assert out["reason"] == "member-timeout" and out["peer"] == phone.id
        # The Ask is still in the mailbox; an Answer arriving late is refused, not misfiled.
        acts = phone.poll()
        asks = [a for a in acts if a["type"] == "Ask"]
        assert len(asks) == 2
        assert phone.post({"type": "Answer", "object": {"run": asks[-1]["object"]["run"], "outcome": {"status": "done"}}})[0] == 404
        # Leaving forgets what was handed out; joining again hands it out again.
        assert phone.post({"type": "Undo", "object": {"type": "Follow", "actor": phone.id, "object": srv.url}})[0] == 200
        assert phone.id not in fed._handed
        assert phone.follow(token="tok")[0] == 202 and [a["type"] for a in phone.poll()].count("Keep") == 2
        # Trust survives a restart of the hub's state.
        assert fed.followers[phone.id].to_dict()["trusted"] is True
    finally:
        hub.stop()
        srv.stop()
