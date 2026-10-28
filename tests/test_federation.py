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
from conftest import EXAMPLES, FIXTURES, ROOT

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


def test_hub_list_and_cli_defaults(two, capsys, monkeypatch, tmp_path):
    a, b = two
    from pyrtan.federation import first_live_hub, rendezvous_hubs

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    listed = tmp_path / "hubs.json"
    listed.write_text(json.dumps({"pyrtan-hubs": 1, "hubs": ["http://127.0.0.1:9", b.url, a.url]}))
    monkeypatch.setenv("PYRTAN_HUB_LIST", str(listed))
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(tmp_path / "no-snapshot.json"))  # never reach pyrtan.com from a test
    monkeypatch.delenv("PYRTAN_RENDEZVOUS", raising=False)
    assert rendezvous_hubs() == ["http://127.0.0.1:9", b.url, a.url]
    assert first_live_hub() == b.url  # the dead one is skipped
    monkeypatch.setenv("PYRTAN_RENDEZVOUS", a.url)
    assert rendezvous_hubs() == [a.url]
    monkeypatch.delenv("PYRTAN_RENDEZVOUS")
    b.fed.set_listed(True)
    code, out, _ = run("follow", "--at", a.url, "--token", "token-a", "--listed")  # no URL: the first listed hub that answers
    assert code == 0 and f"now follows {b.url}" in out and "listed in the directory" in out
    assert a.fed.listed and a.url in {h["id"] for h in b.fed.directory()["hubs"]}
    monkeypatch.setenv("PYRTAN_AT", "http://127.0.0.1:9")  # the local default is dead: fall through to the hub list
    code, out, _ = run("directory")
    assert code == 0 and f"as b ({b.url})" in out and a.url in out and b.url in out
    code, out, _ = run("directory", "--at", a.url, "--json")
    assert {h["id"] for h in json.loads(out)["hubs"]} == {a.url, b.url}
    assert run("directory", "--at", b.svc_srv.url)[0] == 3
    monkeypatch.setenv("PYRTAN_HUB_LIST", str(tmp_path / "missing.json"))
    assert rendezvous_hubs() == []
    code, _, err = run("follow", "--at", a.url, "--token", "token-a")
    assert code == 3 and "no listed or snapshot hub answers" in err


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
    """site/snapshot.py merges the listed hubs' directories with the last snapshot,
    and the command falls back to the snapshot when nothing is live."""
    import subprocess
    import sys as _sys

    a, b = two
    b.fed.set_listed(True)
    a.post("/follow", {"url": b.url})
    listed = tmp_path / "hubs.json"
    listed.write_text(json.dumps({"pyrtan-hubs": 1, "hubs": ["http://127.0.0.1:9", a.url]}))
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"pyrtan-directory": 1, "hubs": [{"id": "http://gone.example", "name": "gone", "trees": ["x"], "seen": time.time() - 3600}, {"id": "http://ancient.example", "trees": [], "seen": time.time() - 30 * 86400}]}))
    out = tmp_path / "directory.json"
    run = subprocess.run([_sys.executable, "site/snapshot.py", str(listed), str(out), "--previous", str(tmp_path / "missing.json"), "--previous", str(old)], capture_output=True, text=True, cwd=str(EXAMPLES.parent))
    assert run.returncode == 0, run.stderr
    snap = json.loads(out.read_text())
    assert snap["pyrtan-directory"] == 1 and snap["listed"]["answered"] == [a.url] and snap["listed"]["silent"][0]["hub"] == "http://127.0.0.1:9"
    assert snap["listed"]["previous"] == str(old)
    ids = [h["id"] for h in snap["hubs"]]
    assert b.url in ids and "http://gone.example" in ids and "http://ancient.example" not in ids  # live from the listed hub, kept from the snapshot, expired
    # B was named by A, so it was asked too, and its own word on itself is the newest.
    assert snap["listed"]["crawled"]["answered"] == [b.url] and next(h for h in snap["hubs"] if h["id"] == b.url)["via"] == b.url

    def cli(*argv):
        code = main(list(argv))
        o = capsys.readouterr()
        return code, o.out, o.err

    from pyrtan.federation import bootstrap_candidates, first_live_hub, snapshot_hubs

    # With every listed hub dead, the snapshot's hubs are the next place to look.
    monkeypatch.setenv("PYRTAN_HUB_LIST", str(tmp_path / "dead-hubs.json"))
    (tmp_path / "dead-hubs.json").write_text(json.dumps({"hubs": ["http://127.0.0.1:9"]}))
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(out))
    monkeypatch.delenv("PYRTAN_RENDEZVOUS", raising=False)
    assert snapshot_hubs()[0] == b.url  # most recently heard first
    assert bootstrap_candidates() == ["http://127.0.0.1:9", b.url, "http://gone.example"]
    assert first_live_hub() == b.url
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
        self.sent = getattr(self, "sent", 0) + 1  # each activity its own id, as a browser's are: alike ones in one second would be a replay
        activity = {"id": f"{self.id}/activities/{self.sent}", "actor": self.id, "published": "now", **activity}
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
    (tmp_path / "h" / "box.toml").write_text('''
name = "box"
commands = ["uptime"]
start = "up"
[nodes.up]
kind = "observe"
shell = { command = "uptime" }
next = "say"
[nodes.say]
kind = "done"
say = "{{ state.last.stdout }}"
''')
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
        assert sorted(phone.trees) == ["alpha", "gamma"] and phone.trees["gamma"]["name"] == "gamma"  # not "box": a phone has no shell
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


GAPPY = '''
name = "gappy"
version = 2
namespace = "pyrtan"
description = "a tree with a decide that covers one value"
start = "go"

[inputs.v]
type = "int"

[nodes.go]
kind = "set"
set = { x = "{{ inputs.v }}" }
next = "pick"

[nodes.pick]
kind = "decide"
on = "x"
branches = [{ when = { eq = 1 }, then = "one" }]

[nodes.one]
kind = "done"
say = "one"
'''


def test_gap_reports_travel_with_announces_when_a_hub_opts_in(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "gappy.toml").write_text(GAPPY)
    (tmp_path / "g" / "plain.toml").write_text(ECHO.format(name="plain", word="plain"))
    from pyrtan.hub import serve_hub

    a_hub, a_srv = serve_hub(port=0, quiet=True, name="reporter", trees=[tmp_path / "g"], federate=True, report_gaps=True, poll_every=60)
    b_hub, b_srv = serve_hub(port=0, quiet=True, name="carrier", federate=True, poll_every=60, state=str(tmp_path / "b.json"))
    try:
        # Two gaps on the catalog tree (namespace + version), seen twice and once.
        for v in (7, 7, 9):
            out = http_json("POST", f"{a_srv.url}/run", {"tree": "gappy", "inputs": {"v": v}})[1]
            assert out["reason"] == "gap"
        own = sorted(a_hub.gap_reports(), key=lambda r: r["observed"])
        assert [(r["id"], r["node"], r["on"], r["observed"], r["count"]) for r in own] == [("pyrtan/gappy@2", "pick", "state.x", 7, 2), ("pyrtan/gappy@2", "pick", "state.x", 9, 1)]
        _, view = http_json("GET", f"{a_srv.url}/reports")  # public, like /directory
        assert view["reporting"] is True and view["count"] == 2 and view["reports"][0] == {"id": "pyrtan/gappy@2", "node": "pick", "on": "state.x", "observed": 7, "count": 2, "reporters": 1, "last": view["reports"][0]["last"]}
        # B follows A: A's announce carries the reports; B keeps them, says who reported, and passes them on.
        assert http_json("POST", f"{b_srv.url}/follow", {"url": a_srv.url})[0] == 200
        _, bview = http_json("GET", f"{b_srv.url}/reports")
        assert bview["reporting"] is False and bview["count"] == 2 and bview["reports"][0]["reporters"] == 1
        assert {r["reporter"] for r in b_hub.federation.reports.values()} == {a_srv.url}
        assert any(r["id"] == "pyrtan/gappy@2" for r in b_hub.federation.report_gossip())
        # The same gap reported by a second hub counts that hub too; B's own announce (not opted in) carries only what it heard.
        b_hub.federation.merge_reports([{"id": "pyrtan/gappy@2", "node": "pick", "on": "state.x", "observed": 7, "count": 5, "last": time.time()}], via="http://other.example")
        row = next(r for r in http_json("GET", f"{b_srv.url}/reports")[1]["reports"] if r["observed"] == 7)
        assert row["count"] == 7 and row["reporters"] == 2
        # Shaping: never an input, no id outside namespace/name@version, no future, no old news, long values summarised.
        now = time.time()
        assert b_hub.federation.merge_reports([{"id": "gappy", "node": "pick", "on": "x", "observed": 1}, {"id": "pyrtan/gappy@2", "node": "pick", "on": "x", "observed": 1, "last": now - 60 * 86400}, "junk"], via="x") == 0
        assert b_hub.federation.merge_reports([{"id": "a/b@1", "node": "n", "on": "o", "observed": "v" * 200, "last": now + 1e6, "count": "3"}], via="http://x") == 1
        shaped = next(r for r in b_hub.federation.reports.values() if r["id"] == "a/b@1")
        assert shaped["observed"] == "<str>" and shaped["count"] == 3 and shaped["last"] <= time.time() and shaped["reporter"] == "http://x"
        assert b_hub.federation.merge_reports([{"id": "a/b@1", "node": "n", "on": "o", "observed": {"k": 1}, "reporter": "https://r.example/"}], via="http://x") == 1
        assert any(r["reporter"] == "https://r.example" and r["observed"] == "<dict>" for r in b_hub.federation.reports.values())
        # Reports survive a restart through the state file.
        b_hub.federation._save()
        again = Federation(b_srv.url, state=str(tmp_path / "b.json"))
        assert {r["id"] for r in again.reports.values()} == {"pyrtan/gappy@2", "a/b@1"}
        # A tree without a namespace reports nothing.
        assert not any(r["id"].endswith("/plain@1") for r in a_hub.gap_reports())
    finally:
        a_hub.stop(); a_srv.stop(); b_hub.stop(); b_srv.stop()


def test_reports_command_and_snapshot(two, tmp_path, capsys, monkeypatch):
    a, b = two
    a.fed.report_gaps = True
    a.fed.own_reports = lambda: [{"id": "pyrtan/weather@1", "node": "code", "on": "state.code", "observed": 99, "count": 4, "last": time.time()}]
    assert main(["reports", "--at", a.url]) == 0
    assert "pyrtan/weather@1 code: state.code = 99  ×4" in capsys.readouterr().out
    assert main(["reports", "nobody/x", "--at", a.url]) == 0 and "no gap reports" in capsys.readouterr().out
    assert main(["reports", "--at", a.url, "--json"]) == 0 and json.loads(capsys.readouterr().out)["reports"][0]["id"] == "pyrtan/weather@1"
    # The site's snapshot asks the listed hubs for /reports and bakes reports.json; the command reads that when no hub is given.
    import subprocess, sys as _sys

    listed = tmp_path / "hubs.json"
    listed.write_text(json.dumps({"hubs": [a.url]}))
    out = subprocess.run([_sys.executable, "site/snapshot.py", str(listed), str(tmp_path / "directory.json"), "--reports", str(tmp_path / "reports.json")], capture_output=True, text=True, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    baked = json.loads((tmp_path / "reports.json").read_text())
    assert baked["count"] == 1 and baked["reports"][0]["id"] == "pyrtan/weather@1" and baked["reports"][0]["count"] == 4
    monkeypatch.setenv("PYRTAN_REPORTS", str(tmp_path / "reports.json"))
    monkeypatch.delenv("PYRTAN_AT", raising=False)
    assert main(["reports"]) == 0 and "pyrtan/weather@1 code" in capsys.readouterr().out


# ---------------------------------------------------------------- networks


def test_invites_and_network_ids():
    from pyrtan.federation import Network, network_id, parse_invite

    net, hub = parse_invite("home:abcdefghijklmnop1234@https://nas.example.ts.net/")
    assert (net.name, net.secret, hub) == ("home", "abcdefghijklmnop1234", "https://nas.example.ts.net")
    assert parse_invite("home:abcdefghijklmnop1234")[1] is None
    assert net.invite("https://x/") == "home:abcdefghijklmnop1234@https://x" and net.invite() == "home:abcdefghijklmnop1234"
    for bad in ("home", "Home:abcdefghijklmnop1234", "home:short", "home:abcdefghijklmnop1234@ftp://x", ""):
        with pytest.raises(ValueError):
            parse_invite(bad)
    # The id comes from name and secret: same name, other secret, another network; and it holds neither.
    assert network_id("home", "a" * 20) != network_id("home", "b" * 20) == Network("home", "b" * 20).id
    assert "a" * 20 not in network_id("home", "a" * 20)


def test_a_hub_starts_a_network_and_others_join_by_invite(tmp_path):
    from pyrtan.federation import Network

    for n in "abc":
        (tmp_path / n).mkdir()
        (tmp_path / n / f"{n}x.toml").write_text(ECHO.format(name=f"{n}x", word=f"{n}x"))
    a = Swarm("a", tmp_path / "a", token="token-a", open_follows=False)  # takes no follows outside its networks
    b = Swarm("b", tmp_path / "b", token="token-b")
    c = Swarm("c", tmp_path / "c")
    try:
        status, made = a.post("/networks", {"name": "home"})
        assert status == 200 and made["network"] == "home" and made["invite"].startswith("home:") and made["invite"].endswith(f"@{a.url}")
        assert a.post("/networks", {"name": "home"})[0] == 400 and a.post("/networks", {"name": "Not OK"})[0] == 400
        assert a.fed.following == {} and a.fed.followers == {}  # a network of one
        # B joins by invite: it is in home, follows A in it, and routes to A's trees.
        status, joined = b.post("/join", {"invite": made["invite"]})
        assert status == 200 and joined["network"] == "home" and joined["following"]["url"] == a.url and joined["following"]["state"] == "accepted"
        assert b.fed.following[a.url].network == a.fed.networks["home"].id == a.fed.followers[b.url].network
        assert b.post("/intent", {"text": "ax hi"})[1]["say"] == "ax: hi"
        # Outside the network A takes nobody (it is --closed); in another network of the same name, nobody either.
        assert c.fed.follow(a.url).state == "pending" and "closed" in c.fed.following[a.url].last_error
        c.fed.add_network(Network("home", "another-secret-entirely"))
        assert c.fed.follow(a.url, network="home").state == "pending"
        assert "not-in-network" in c.fed.following[a.url].last_error
        # A Follow naming home's id but signed with another secret is refused as unsigned.
        forged = {"type": "Follow", "id": "x", "actor": c.url, "object": a.url, "secret": "p" * 32, "inbox": f"{c.url}/inbox", "network": a.fed.networks["home"].id}
        status, answer = http_json("POST", f"{a.url}/inbox", forged, headers=sign("another-secret-entirely", c.url, "POST", "/inbox", forged))
        assert status == 401 and answer["reason"] == "unsigned"
        # The invite lets C in, and joining again changes nothing; a name taken by another network is refused.
        assert c.post("/leave", {"network": "home"})[1]["left"] is True and c.url not in a.fed.followers
        status, joined = c.post("/join", {"invite": made["invite"]})
        assert status == 200 and joined["following"]["state"] == "accepted"
        assert c.post("/join", {"invite": made["invite"]})[1]["following"]["url"] == a.url
        assert c.post("/join", {"invite": "home:yet-another-secret-xyz"})[0] == 400
        assert c.post("/join", {"invite": "nonsense"})[1]["reason"] == "bad-invite"
        # An invite with no hub: in the network, following nobody.
        status, alone = b.post("/join", {"invite": "solo:abcdefghijklmnopqrstu"})
        assert status == 200 and alone["following"] is None and "network of one" in alone["say"]
        _, view = b.get("/networks")
        assert {n["name"]: n["following"] for n in view["networks"]} == {"home": [a.url], "solo": []}
        assert all(n["invite"].endswith(f"@{b.url}") for n in view["networks"])
        assert http_json("GET", f"{b.url}/networks")[0] == 401  # the invites are the operator's
        # Leaving cuts the links made in it, both ways, and forgets it.
        assert b.post("/leave", {"network": "home"})[1]["left"] is True
        assert a.url not in b.fed.following and b.url not in a.fed.followers and "home" not in b.fed.networks
        assert b.post("/leave", {"network": "home"})[1]["left"] is False
    finally:
        for s in (a, b, c):
            s.stop()


def test_a_private_network_never_reaches_the_public_directory(tmp_path):
    from pyrtan.federation import Network

    for n in "abc":
        (tmp_path / n).mkdir()
        (tmp_path / n / f"{n}x.toml").write_text(ECHO.format(name=f"{n}x", word=f"{n}x"))
    public = Network("pyrtan", "public-default-network-of-pyrtan-com", public=True)
    a = Swarm("a", tmp_path / "a", listed=True)
    b = Swarm("b", tmp_path / "b", token="token-b")
    c = Swarm("c", tmp_path / "c", listed=True)
    try:
        # A is in the public network; B joins it through A. C starts "home", and B joins that too.
        a.fed.add_network(public)
        assert b.fed.join(Network(public.name, public.secret, public=True), [a.url]).state == "accepted"
        home = c.fed.create_network("home")
        assert b.fed.join(Network("home", home.secret), [c.url]).state == "accepted"
        b.hub.refresh_all()
        # B hears A in the public network and C in home, and passes each on only within its own network.
        heard = {e["id"]: e["network"] for e in b.fed.directory_entries.values()}
        assert heard == {a.url: public.id, c.url: home.id}
        assert c.url not in {h["id"] for h in a.fed.directory()["hubs"]}
        assert a.url not in {h["id"] for h in c.fed.directory()["hubs"]}
        assert [e["id"] for e in b.fed.gossip(public.id)] == [a.url] and [e["id"] for e in b.fed.gossip(home.id)] == [c.url]
        # B's public /directory shows the public network; the operator also sees home.
        _, shown = http_json("GET", f"{b.url}/directory")
        assert {h["id"] for h in shown["hubs"]} == {a.url} and shown["networks"] == {public.id: "pyrtan"}
        _, full = b.get("/directory")
        assert {h["id"]: h["networks"] for h in full["hubs"]} == {a.url: [public.id], c.url: [home.id]}
        assert full["networks"] == {public.id: "pyrtan", home.id: "home"}
        # The directory blob B's keepers hold is the public part only.
        assert {e["id"] for e in b.fed.directory_backup()} == {a.url}
        # State keeps networks and the tags; an entry of a network since left is not taken back.
        state = tmp_path / "b-state.json"
        b.fed.state_path = state
        b.fed._save()
        again = Federation(b.url, state=state)
        assert set(again.networks) == {"pyrtan", "home"} and again.networks["pyrtan"].public
        assert {e["id"]: e["network"] for e in again.directory_entries.values()} == {a.url: public.id, c.url: home.id}
        stale = json.loads(state.read_text())
        stale["networks"] = [n for n in stale["networks"] if n["name"] != "home"]
        state.write_text(json.dumps(stale))
        assert {e["id"] for e in Federation(b.url, state=state).directory_entries.values()} == {a.url}
    finally:
        for s in (a, b, c):
            s.stop()


def test_the_default_network_from_pyrtan_com(two, tmp_path, monkeypatch, capsys):
    import subprocess
    import sys as _sys

    from pyrtan.federation import default_network
    from pyrtan.hub import serve_hub

    a, b = two

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    network = ROOT / "site" / "rendezvous" / "network.json"
    monkeypatch.setenv("PYRTAN_NETWORK", str(network))
    listed = tmp_path / "hubs.json"
    listed.write_text(json.dumps({"hubs": ["http://127.0.0.1:9", b.url]}))
    monkeypatch.setenv("PYRTAN_HUB_LIST", str(listed))
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(tmp_path / "no-snapshot.json"))
    monkeypatch.delenv("PYRTAN_RENDEZVOUS", raising=False)
    default = default_network()
    assert default is not None and default.name == "pyrtan" and default.public
    # The first hub in the default network is a network of one: nobody to follow yet.
    code, out, _ = run("join", "--at", b.url, "--token", "token-b", "--listed")
    assert code == 0 and "in pyrtan; no hub in it accepted a follow yet" in out and b.fed.following == {}
    # `pyrtan follow` with no URL is `pyrtan join`: A is in the default network, following the listed hub in it.
    code, out, _ = run("follow", "--at", a.url, "--token", "token-a")
    assert code == 0 and f"in pyrtan, following {b.url}" in out and "invite others" not in out  # a public network needs no invite
    assert a.fed.following[b.url].network == default.id
    code, out, _ = run("network", "list", "--at", a.url, "--token", "token-a")
    assert code == 0 and f"pyrtan           {default.id}  public  following 1" in out
    # Its own network, by command: the invite, then another hub joins with it.
    code, out, _ = run("network", "new", "home", "--at", b.url, "--token", "token-b")
    invite = next(line.split("pyrtan join ", 1)[1] for line in out.splitlines() if "pyrtan join " in line)
    assert code == 0 and invite.startswith("home:") and invite.endswith(f"@{b.url}")
    code, out, _ = run("join", invite, "--at", a.url, "--token", "token-a")
    assert code == 0 and "in home" in out and a.fed.following[b.url].network == b.fed.networks["home"].id
    assert run("follow", "--at", a.url, "--token", "token-a", "--network", "home")[0] == 2  # a network needs a hub to follow in it
    code, out, _ = run("network", "leave", "home", "--at", a.url, "--token", "token-a")
    assert code == 0 and "left home" in out and "home" not in a.fed.networks
    assert run("network", "new", "--at", a.url, "--token", "token-a")[0] == 2
    # A hub started with --join default is in it from the start.
    hub, srv = serve_hub(port=0, quiet=True, name="d", join=["default"])
    try:
        assert hub.federation is not None and hub.federation.following[b.url].network == default.id
    finally:
        hub.stop()
        srv.stop()
    # The snapshot keeps the default network's part of each directory and nobody else's.
    b.fed.merge_directory([{"id": "http://private.example", "name": "p", "trees": [], "seen": time.time()}], via="x", network="0123456789abcdef")
    b.fed.merge_directory([{"id": "http://public.example", "name": "q", "trees": [], "seen": time.time()}], via="x", network=default.id)
    b.hub.token = None  # as anyone sees it, the private part included: the snapshot must still drop it
    out = tmp_path / "directory.json"
    done = subprocess.run([_sys.executable, "site/snapshot.py", str(listed), str(out), "--network", str(network)], capture_output=True, text=True, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr
    ids = {h["id"] for h in json.loads(out.read_text())["hubs"]}
    assert "http://public.example" in ids and "http://private.example" not in ids and b.url in ids


def test_the_default_network_is_built_in_and_pyrtan_com_only_mirrors_it(monkeypatch):
    import pyrtan.federation as federation

    monkeypatch.delenv("PYRTAN_NETWORK", raising=False)
    monkeypatch.setattr(federation, "http_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked the network for the default network")))
    net = federation.default_network()
    assert net is not None and net.public and net.name == "pyrtan"
    mirror = json.loads((ROOT / "site" / "rendezvous" / "network.json").read_text())
    assert (net.name, net.secret) == (mirror["name"], mirror["secret"])  # the site publishes the same network, or joining would split it


def test_the_hub_list_reads_many_sources_of_either_shape(tmp_path, monkeypatch):
    from pyrtan.federation import hub_urls, rendezvous_hubs

    listed = tmp_path / "hubs.json"
    listed.write_text(json.dumps({"hubs": ["https://a.example/", "https://b.example"]}))
    directory = tmp_path / "directory.json"  # a hub's /directory, or a snapshot: newest first
    directory.write_text(json.dumps({"hubs": [{"id": "https://old.example", "seen": 1}, {"id": "https://b.example", "seen": 3}, {"id": "https://new.example", "seen": 5}]}))
    monkeypatch.delenv("PYRTAN_RENDEZVOUS", raising=False)
    monkeypatch.setenv("PYRTAN_HUB_LIST", f"{listed}, {tmp_path / 'missing.json'} {directory}")
    assert rendezvous_hubs() == ["https://a.example", "https://b.example", "https://new.example", "https://old.example"]
    assert hub_urls({"hubs": ["ftp://x", 3, None]}) == [] and hub_urls(None) == []


def test_joining_tries_the_hubs_it_knows_before_asking_anyone(two, monkeypatch):
    import pyrtan.hub as hubmod
    from pyrtan.federation import default_network

    a, b = two
    monkeypatch.delenv("PYRTAN_NETWORK", raising=False)  # the built-in default network
    default = default_network()
    assert hubmod.join_network(b.fed, "default")[1]["say"].startswith("in pyrtan")  # b is in it, a network of one

    # a has heard of b in the default network, from gossip: a hint, never a follow.
    a.fed.add_network(default)
    a.fed.merge_directory([{"id": b.url, "name": "b", "trees": [], "seen": time.time()}], via="http://someone.example", network=default.id)
    assert a.fed.known_hubs(default.id) == [b.url] and a.fed.known_hubs(default.id, heard=False) == []
    asked: list[str] = []

    def hub_list():
        asked.append("hub list")
        return [b.url]

    def nobody(*_):
        raise AssertionError("asked for the snapshot although a listed hub answered")

    monkeypatch.setattr(hubmod, "rendezvous_hubs", hub_list)
    monkeypatch.setattr(hubmod, "snapshot_hubs", nobody)
    status, out = hubmod.join_network(a.fed, "default")
    assert status == 200 and out["say"] == f"in pyrtan, following {b.url}" and a.fed.following[b.url].network == default.id
    assert asked == ["hub list"]  # gossip named b; the hub list is what a follow trusts
    # Joining again (a restart with --join default) is the follow it has, and asks nobody.
    monkeypatch.setattr(hubmod, "rendezvous_hubs", nobody)
    assert hubmod.join_network(a.fed, "default")[1]["following"]["url"] == b.url
    assert a.fed.known_hubs(default.id, heard=False)[0] == b.url


def test_an_activity_is_taken_once(two):
    a, b = two
    m = Member(a.url)
    assert m.follow()[0] == 202
    activity = {"id": f"{m.id}/activities/once", "actor": m.id, "published": "now", "type": "Announce", "object": {"type": "Capabilities", "name": "phone", "trees": [], "directory": [], "kept": []}}
    headers = sign(m.secret, m.id, "POST", "/inbox", activity)
    assert http_json("POST", f"{a.url}/inbox", activity, headers=headers)[0] == 200
    status, out = http_json("POST", f"{a.url}/inbox", activity, headers=headers)   # the same bytes again, from someone on the path
    assert status == 401 and out["reason"] == "replayed"


def test_an_open_hub_takes_so_many_strangers_and_lets_idle_ones_go(two, monkeypatch):
    import pyrtan.federation as fedmod

    a, b = two
    monkeypatch.setattr(fedmod, "FOLLOWS_PER_MINUTE", 2)
    members = [Member(a.url, name=f"m{i}") for i in range(3)]
    assert [m.follow()[0] for m in members[:2]] == [202, 202]
    status, out = members[2].follow()
    assert status == 429 and out["reason"] == "too-fast"
    assert members[0].follow()[0] == 202   # one it has already is not a new stranger
    monkeypatch.setattr(fedmod, "FOLLOWERS_MAX", 2)
    a.fed._follows.clear()
    status, out = members[2].follow()
    assert status == 503 and out["reason"] == "followers-full"
    # A member that never polls is let go after a while; the store and mailbox with it.
    a.fed.followers[members[1].id].last_ok = time.time() - fedmod.MEMBER_IDLE - 1
    assert a.fed.expire_members() == 1 and members[1].id not in a.fed.followers and members[0].id in a.fed.followers
