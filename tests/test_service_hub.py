"""A service, a hub, and a swarm of two services behind one hub, over real
sockets on localhost."""

import json
import time
import urllib.request

import pytest

from pyrtan import Registry, RecordedTransport, __version__
from pyrtan.hub import Hub
from pyrtan.service import Server, Service, http_json
from pyrtan.tree import load_tree_text
from conftest import EXAMPLES, FIXTURES

ECHO = """
name = "echo"
description = "echoes"
start = "say"

[[triggers]]
pattern = "echo {text}"

[inputs.text]
type = "str"

[nodes.say]
kind = "done"
say = "{{ inputs.text }}"
output = { text = "{{ inputs.text }}" }
"""

NEEDS_WEATHER = """
name = "picnic"
description = "decides on a picnic by asking the swarm for weather"
start = "weather"

[[triggers]]
pattern = "picnic in {place}"

[inputs.place]
type = "str"

[nodes.weather]
kind = "delegate"
intent = "weather in {{ inputs.place }}"
into = "w"
next = "ok"

[nodes.ok]
kind = "decide"
on = "w.status"
branches = [ { when = { eq = "done" }, then = "sky" }, { else = "unknown" } ]

[nodes.sky]
kind = "decide"
on = "w.output.sky"
branches = [ { when = { in = ["clear", "cloudy"] }, then = "yes" }, { else = "no" } ]

[nodes.yes]
kind = "done"
say = "Picnic: yes, it is {{ state.w.output.sky }}."

[nodes.no]
kind = "done"
say = "Picnic: no, it is {{ state.w.output.sky }}."

[nodes.unknown]
kind = "fail"
reason = "no-weather"
say = "Could not get the weather: {{ state.w.say }}"
"""


@pytest.fixture
def example_service():
    reg = Registry.from_paths([EXAMPLES])
    svc = Service(reg, name="examples", transport=RecordedTransport.from_file(FIXTURES / "examples.json"))
    server = Server(svc.route, quiet=True).start()
    yield svc, server
    server.stop()


def test_service_endpoints(example_service):
    svc, server = example_service
    status, health = http_json("GET", f"{server.url}/health")
    assert status == 200 and health["status"] == "ok" and "weather" in health["trees"]
    _, caps = http_json("GET", f"{server.url}/capabilities")
    assert {t["name"] for t in caps["trees"]} >= {"weather", "hacker-news"}
    _, out = http_json("POST", f"{server.url}/intent", {"text": "weather in Oslo"})
    assert out["status"] == "done" and out["output"]["sky"] == "raining"
    assert out["trace"]["steps"][0]["kind"] == "intent" and out["trace"]["steps"][1]["node"] == "geocode"
    _, out = http_json("POST", f"{server.url}/run", {"tree": "hacker-news-item", "inputs": {"id": 101}, "trace": False})
    assert out["status"] == "done" and "trace" not in out
    _, out = http_json("POST", f"{server.url}/intent", {"text": "nothing matches this"})
    assert out["reason"] == "no-match"
    assert out["output"]["proposal"]["trigger"] == "nothing matches this"
    assert "pyrtan new nothing-matches-this --from" in out["say"]
    _, un = http_json("GET", f"{server.url}/unanswered")
    assert [(u["text"], u["count"]) for u in un["unanswered"]] == [("nothing matches this", 1)]
    _, health = http_json("GET", f"{server.url}/health")
    assert health["unanswered"] == 1
    status, out = http_json("GET", f"{server.url}/nowhere")
    assert status == 404
    _, out = http_json("POST", f"{server.url}/run", {"tree": "site-status", "inputs": {"host": "teapot.example"}})
    assert out["status"] == "done"
    _, gaps = http_json("GET", f"{server.url}/gaps")
    assert gaps["gaps"] == []


def test_service_rejects_bad_json(example_service):
    _, server = example_service
    req = urllib.request.Request(f"{server.url}/intent", data=b"not json", method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        pytest.fail("expected 400")


def test_hot_reload_picks_up_a_new_tree(tmp_path):
    reg = Registry.from_paths([tmp_path])
    svc = Service(reg, name="hot")
    server = Server(svc.route, quiet=True).start()
    try:
        _, out = http_json("POST", f"{server.url}/intent", {"text": "echo hi"})
        assert out["reason"] == "no-match"
        (tmp_path / "echo.toml").write_text(ECHO)
        _, out = http_json("POST", f"{server.url}/intent", {"text": "echo hi"})
        assert out["status"] == "done" and out["say"] == "hi"
        (tmp_path / "echo.toml").unlink()
        _, health = http_json("GET", f"{server.url}/health")
        assert health["trees"] == []
    finally:
        server.stop()


def test_swarm_of_two_behind_a_hub(tmp_path):
    # Service A: the examples (holds `weather`). Service B: `picnic`, which
    # delegates "weather in X" to the hub, which routes it to A.
    reg_a = Registry.from_paths([EXAMPLES])
    svc_a = Service(reg_a, name="a", transport=RecordedTransport.from_file(FIXTURES / "examples.json"))
    srv_a = Server(svc_a.route, quiet=True).start()

    hub = Hub(poll_every=0.2)
    hub_srv = Server(hub.route, quiet=True).start()

    (tmp_path / "picnic.toml").write_text(NEEDS_WEATHER)
    reg_b = Registry.from_paths([tmp_path])
    svc_b = Service(reg_b, name="b", hub=hub_srv.url)
    srv_b = Server(svc_b.route, quiet=True).start()
    try:
        assert svc_b.register(srv_b.url)
        status, reg = http_json("POST", f"{hub_srv.url}/register", {"url": srv_a.url})
        assert status == 200 and reg["peer"]["link"] == "up"

        _, health = http_json("GET", f"{hub_srv.url}/health")
        assert health["peers"] == 2 and health["up"] == 2 and health["trees"] == 12

        _, caps = http_json("GET", f"{hub_srv.url}/capabilities")
        assert {t["name"] for t in caps["trees"]} == {"weather", "hacker-news", "hacker-news-item", "site-status", "github-release", "picnic", "disk-space", "log-errors", "room-temperature", "rendered-headline", "modem-check", "small-talk"}

        # Routed by the hub to B, which delegates back through the hub to A.
        _, out = http_json("POST", f"{hub_srv.url}/intent", {"text": "picnic in Oslo"})
        assert out["status"] == "done", out
        assert out["say"] == "Picnic: no, it is raining."
        assert out["peer"] == srv_b.url
        nodes = [s["node"] for s in out["trace"]["steps"]]
        assert "geocode" in nodes and "sky" in nodes  # A's steps carried back inside B's trace

        # Direct run through the hub to whoever holds the tree.
        _, out = http_json("POST", f"{hub_srv.url}/run", {"tree": "hacker-news-item", "inputs": {"id": 101}})
        assert out["status"] == "done" and out["peer"] == srv_a.url

        _, out = http_json("POST", f"{hub_srv.url}/run", {"tree": "nope"})
        assert out["reason"] == "no-such-tree"

        # A sentence nobody answers: the hub says so, proposes the tree, and
        # remembers. B, which has a hub, leaves the record to the hub.
        _, out = http_json("POST", f"{hub_srv.url}/intent", {"text": "trace route to 10.0.0.1"})
        assert out["reason"] == "no-match" and out["output"]["seen"] == 1
        assert out["say"].startswith("No service in the swarm answers 'trace route to 10.0.0.1'. To write the tree that would: pyrtan new trace-route --from")
        _, out = http_json("POST", f"{srv_b.url}/intent", {"text": "trace route to 10.0.0.1"})
        assert out["reason"] == "no-match"
        assert http_json("GET", f"{srv_b.url}/unanswered")[1]["unanswered"] == []
        _, un = http_json("GET", f"{hub_srv.url}/unanswered")
        assert [(u["text"], u["count"]) for u in un["unanswered"]] == [("trace route to 10.0.0.1", 2)]
        assert un["unanswered"][0]["proposal"]["inputs"]["host"]["type"] == "str"

        # A peer that dies is marked and skipped.
        srv_a.stop()
        hub.refresh_all()
        _, peers = http_json("GET", f"{hub_srv.url}/peers")
        a = next(p for p in peers["peers"] if p["url"] == srv_a.url)
        assert a["last_error"] is not None
        _, out = http_json("POST", f"{hub_srv.url}/run", {"tree": "weather", "inputs": {"place": "Oslo"}})
        assert out["reason"] in ("peer-unreachable", "no-such-tree")
    finally:
        hub.stop()
        hub_srv.stop()
        srv_b.stop()
        try:
            srv_a.stop()
        except Exception:
            pass


def test_hub_health_link_states():
    hub = Hub()
    p = hub.add_peer("http://127.0.0.1:9")  # nothing listens there
    assert p.link == "down" and p.last_error
    assert hub.health()["down"] == 1
    assert hub.route("POST", "/register", {"url": "ftp://x"})[0] == 400


def test_demo_page_is_served_by_service_and_hub(example_service):
    _, server = example_service
    with urllib.request.urlopen(f"{server.url}/") as resp:
        assert resp.headers.get_content_type() == "text/html"
        page = resp.read()
        assert b"<title>Pyrtan</title>" in page and f"console v{__version__}".encode() in page
        # The wordmark's two halves pick their own colours; the picker is inline, not another request.
        assert b'data-theme-part="pyr">pyr<' in page and b'data-theme-part="tan">tan<' in page
        assert b"__THEME__" not in page and b'localStorage.getItem(KEY)' in page
        assert b'role="combobox"' in page and b'id="suggestions"' in page and b'pyrtan.console.asked' in page  # the ask box suggests
    status, api = http_json("GET", f"{server.url}/api")
    assert status == 200 and api["pyrtan"] == "service"
    hub = Hub()
    hub_srv = Server(hub.route, quiet=True).start()
    try:
        with urllib.request.urlopen(f"{hub_srv.url}/index.html") as resp:
            assert resp.headers.get_content_type() == "text/html"
        assert http_json("GET", f"{hub_srv.url}/api")[1]["pyrtan"] == "hub"
    finally:
        hub_srv.stop()


def test_hub_reads_sentences_with_the_tree_language_and_puts_the_reading_in_the_trace():
    reg = Registry.from_paths([EXAMPLES])
    svc = Service(reg, name="examples", transport=RecordedTransport.from_file(FIXTURES / "examples.json"))
    srv = Server(svc.route, quiet=True).start()
    hub = Hub(poll_every=60)
    hub_srv = Server(hub.route, quiet=True).start()
    try:
        hub.add_peer(srv.url)
        _, caps = http_json("GET", f"{hub_srv.url}/capabilities")
        weather = next(t for t in caps["trees"] if t["name"] == "weather")
        assert weather["language"] == {"synonyms": {"weather": ["forecast", "temperature"]}}
        # The synonym lives in the tree; the hub applies it when it routes.
        _, out = http_json("POST", f"{hub_srv.url}/intent", {"text": "Hey, what's the forecast in Oslo?"})
        assert out["status"] == "done" and out["output"]["sky"] == "raining"
        first = out["trace"]["steps"][0]
        assert first["kind"] == "intent" and first["next"] == "geocode"
        assert first["observed"]["cleaned"] == "what is the forecast in Oslo"
        assert out["trace"]["steps"][1]["node"] == "geocode"
        _, out = http_json("POST", f"{hub_srv.url}/intent", {"text": "please do nothing"})
        assert out["reason"] == "no-match" and out["output"]["reading"]["cleaned"] == "do nothing"
    finally:
        hub.stop()
        hub_srv.stop()
        srv.stop()


def test_the_hubs_own_requests_keep_to_the_egress_policy(monkeypatch):
    """http_json, which every request a hub makes for itself goes through (peers,
    followed hubs, the hub list, identity documents), refuses what the egress
    policy refuses, before any connection; and a hub polls only so many peers."""
    import io
    import urllib.error
    from pyrtan import service
    from pyrtan.transport import EgressRefused
    monkeypatch.setattr(service, "_EGRESS", "any")
    with pytest.raises(EgressRefused):
        http_json("GET", "http://127.0.0.1:9/actor", egress="public")
    with pytest.raises(EgressRefused):
        http_json("GET", "http://169.254.169.254/latest/meta-data/", egress="public")
    with pytest.raises(EgressRefused):
        http_json("GET", "ftp://example.com/", egress="any")
    service.set_egress("public")
    with pytest.raises(EgressRefused):
        http_json("GET", "http://localhost:9/actor")
    with pytest.raises(ValueError):
        service.set_egress("everything")
    assert service._EGRESS == "public"
    service.set_egress("any")
    # An answer past MAX_RESPONSE is an error, not a read of all of it.
    class Big:
        def __init__(self, n): self.f = io.BytesIO(b"x" * n)
        def read(self, n=-1): return self.f.read(n)
    assert service._bounded(Big(10), "u") == b"x" * 10
    with pytest.raises(urllib.error.URLError):
        service._bounded(Big(service.MAX_RESPONSE + 1), "u")
    hub = Hub(name="h", poll_every=60)
    monkeypatch.setattr(hub, "refresh", lambda peer: None)
    from pyrtan.hub import MAX_PEERS
    for i in range(MAX_PEERS):
        hub.add_peer(f"http://10.0.0.{i // 250}.{i % 250}:1")
    with pytest.raises(ValueError, match="at most"):
        hub.add_peer("http://10.9.9.9:1")
    status, body = hub.route("POST", "/register", {"url": "http://10.9.9.9:1"}, {})
    assert status == 503 and body["reason"] == "full"
    hub.stop()
    # An open hub (no token) takes a registration from anyone, so the policy says where it may be sent polling; the operator's own swarm is never refused.
    hub = Hub(name="open", poll_every=60)
    monkeypatch.setattr(hub, "refresh", lambda peer: None)
    hub.egress = "public"
    status, body = hub.route("POST", "/register", {"url": "http://127.0.0.1:1"}, {})
    assert status == 403 and body["reason"] == "egress" and not hub.peers
    hub.stop()
    hub = Hub(name="closed", poll_every=60, token="t")
    monkeypatch.setattr(hub, "refresh", lambda peer: None)
    hub.egress = "public"
    assert hub.route("POST", "/register", {"url": "http://127.0.0.1:1"}, {"Authorization": "Bearer t"})[0] == 200
    hub.stop()
