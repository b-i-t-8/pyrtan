"""Tokens, CORS, persistent gaps and the egress policy: what an operator
turns on when a swarm leaves the LAN or hosts trees for other people."""

import json
import socket
import urllib.error
import urllib.request

import pytest

from pyrtan import Registry, RecordedTransport
from pyrtan.gaps import GapStore
from pyrtan.hub import Hub
from pyrtan.service import Server, Service, http_json
from pyrtan.transport import EgressRefused, UrllibTransport, check_egress
from conftest import EXAMPLES, FIXTURES

GAPPY = """
name = "gappy"
description = "decides on a value it was given, with no else"
start = "look"

[[triggers]]
pattern = "gappy {value:int}"

[inputs.value]
type = "int"

[nodes.look]
kind = "decide"
on = "inputs.value"
branches = [ { when = { eq = 1 }, then = "one" } ]

[nodes.one]
kind = "done"
say = "one"
"""


def _raw(url: str, headers: dict | None = None, method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def locked_service():
    reg = Registry.from_paths([EXAMPLES])
    svc = Service(reg, name="locked", transport=RecordedTransport.from_file(FIXTURES / "examples.json"), token="s3cret")
    server = Server(svc.route, quiet=True, token="s3cret", cors=["https://pyrtan.com"]).start()
    yield svc, server
    server.stop()


def test_token_required_except_public_paths(locked_service):
    _, server = locked_service
    status, headers, body = _raw(f"{server.url}/capabilities")
    assert status == 401 and headers["WWW-Authenticate"].startswith("Bearer")
    assert json.loads(body)["reason"] == "unauthorised"
    assert _raw(f"{server.url}/intent", {"Content-Type": "application/json"}, "POST", b'{"text":"weather in Oslo"}')[0] == 401
    assert _raw(f"{server.url}/capabilities", {"Authorization": "Bearer wrong"})[0] == 401
    assert _raw(f"{server.url}/capabilities", {"Authorization": "Basic s3cret"})[0] == 401
    status, _, body = _raw(f"{server.url}/capabilities", {"Authorization": "Bearer s3cret"})
    assert status == 200 and {t["name"] for t in json.loads(body)["trees"]} >= {"weather"}
    assert http_json("POST", f"{server.url}/intent", {"text": "weather in Oslo"}, token="s3cret")[1]["status"] == "done"
    # Public: the console, its index, and liveness.
    assert _raw(f"{server.url}/")[0] == 200
    assert _raw(f"{server.url}/console")[0] == 200
    status, _, body = _raw(f"{server.url}/api")
    assert status == 200 and json.loads(body)["auth"] == "bearer"


def test_health_is_trimmed_without_the_token(locked_service):
    _, server = locked_service
    status, _, body = _raw(f"{server.url}/health")
    public = json.loads(body)
    assert status == 200 and public["status"] == "ok" and "trees" not in public and "errors" not in public
    status, _, body = _raw(f"{server.url}/health", {"Authorization": "Bearer s3cret"})
    assert "weather" in json.loads(body)["trees"]


def test_cors_only_for_listed_origins(locked_service):
    _, server = locked_service
    status, headers, _ = _raw(f"{server.url}/intent", {"Origin": "https://pyrtan.com", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}, "OPTIONS")
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "https://pyrtan.com"
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    status, headers, _ = _raw(f"{server.url}/intent", {"Origin": "https://evil.example"}, "OPTIONS")
    assert status == 204 and "Access-Control-Allow-Origin" not in headers
    _, headers, _ = _raw(f"{server.url}/api", {"Origin": "https://pyrtan.com"})
    assert headers["Access-Control-Allow-Origin"] == "https://pyrtan.com" and headers["Vary"] == "Origin"
    _, headers, _ = _raw(f"{server.url}/api")
    assert "Access-Control-Allow-Origin" not in headers


def test_cors_star_echoes_any_origin():
    reg = Registry.from_paths([EXAMPLES])
    server = Server(Service(reg).route, quiet=True, cors=["*"]).start()
    try:
        _, headers, _ = _raw(f"{server.url}/api", {"Origin": "http://localhost:5173"})
        assert headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    finally:
        server.stop()


def test_swarm_shares_one_token(tmp_path):
    token = "swarm-token"
    reg_a = Registry.from_paths([EXAMPLES])
    svc_a = Service(reg_a, name="a", transport=RecordedTransport.from_file(FIXTURES / "examples.json"), token=token)
    srv_a = Server(svc_a.route, quiet=True, token=token).start()
    hub = Hub(poll_every=60, token=token)
    hub_srv = Server(hub.route, quiet=True, token=token).start()
    try:
        # A stranger cannot join the swarm or use it.
        assert http_json("POST", f"{hub_srv.url}/register", {"url": srv_a.url})[0] == 401
        assert http_json("POST", f"{hub_srv.url}/intent", {"text": "weather in Oslo"})[0] == 401
        # A service with the token registers; the hub polls it with the token.
        svc_a.hub = hub_srv.url
        assert svc_a.register(srv_a.url)
        _, peers = http_json("GET", f"{hub_srv.url}/peers", token=token)
        assert peers["peers"][0]["link"] == "up" and "weather" in peers["peers"][0]["trees"]
        # And forwards with it.
        _, out = http_json("POST", f"{hub_srv.url}/intent", {"text": "weather in Oslo"}, token=token)
        assert out["status"] == "done" and out["peer"] == srv_a.url
        # A hub without the token sees the peer as down.
        other = Hub(poll_every=60)
        p = other.add_peer(srv_a.url)
        assert p.link == "down" and "401" in p.last_error
    finally:
        hub.stop()
        hub_srv.stop()
        srv_a.stop()


# ---------------------------------------------------------------- gaps


def test_gaps_persist_and_count(tmp_path):
    (tmp_path / "gappy.toml").write_text(GAPPY)
    path = tmp_path / "gaps.jsonl"
    reg = Registry.from_paths([tmp_path])
    svc = Service(reg, name="g", gaps=path)
    server = Server(svc.route, quiet=True).start()
    try:
        assert http_json("POST", f"{server.url}/intent", {"text": "gappy 1"})[1]["status"] == "done"
        for _ in range(3):
            out = http_json("POST", f"{server.url}/intent", {"text": "gappy 7"})[1]
            assert out["status"] == "error" and out["reason"] == "gap"
        http_json("POST", f"{server.url}/intent", {"text": "gappy 8"})
        _, gaps = http_json("GET", f"{server.url}/gaps")
        assert [(g["node"], g["observed"], g["count"]) for g in gaps["gaps"]] == [("look", 7, 3), ("look", 8, 1)]
        assert http_json("GET", f"{server.url}/health")[1]["gaps"] == 2
    finally:
        server.stop()
    assert len(path.read_text().splitlines()) == 4
    # A new service over the same file starts with what the old one saw.
    again = GapStore(path)
    assert [(g["observed"], g["count"]) for g in again.all()] == [(7, 3), (8, 1)]
    again.add([{"tree": "gappy", "node": "look", "on": "inputs.value", "observed": 8}])
    assert [(g["observed"], g["count"]) for g in again.all()] == [(7, 3), (8, 2)]


def test_gap_store_keeps_going_when_the_file_cannot_be_written(tmp_path, capsys):
    blocked = tmp_path / "dir"
    blocked.mkdir()
    store = GapStore(blocked)  # a directory, not a file: reading and every append fail
    store.add([{"tree": "t", "node": "n", "on": "x", "observed": 1}])
    err = capsys.readouterr().err
    assert len(store) == 1 and store.error and "cannot read gaps" in err
    # A store over a file that cannot be written warns once and keeps the gap.
    missing_dir = tmp_path / "nope"
    missing_dir.write_text("")  # a file where the parent directory should be
    store = GapStore(missing_dir / "gaps.jsonl")
    store.add([{"tree": "t", "node": "n", "on": "x", "observed": 1}] * 2)
    assert store.all()[0]["count"] == 2 and store.error
    assert capsys.readouterr().err.count("cannot write gaps") == 1


def test_gap_store_in_memory_when_no_path():
    store = GapStore()
    store.add([{"tree": "t", "node": "n", "on": "x", "observed": "a"}] * 2)
    assert len(store) == 1 and store.all()[0]["count"] == 2 and store.path is None


# ---------------------------------------------------------------- egress


def _resolver(*answers: str):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers]

    return resolve


def test_egress_any_allows_everything_http():
    check_egress("http://127.0.0.1:8701/health", "any")
    check_egress("http://10.0.0.5/", "any", resolve=_resolver("10.0.0.5"))
    with pytest.raises(EgressRefused):
        check_egress("ftp://example.com/", "any")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.20", "169.254.169.254", "0.0.0.0", "::1", "fd00::1", "fe80::1", "::ffff:10.0.0.1", "224.0.0.1"])
def test_egress_public_refuses_private_addresses(address):
    with pytest.raises(EgressRefused):
        check_egress("https://api.example.com/", "public", resolve=_resolver(address))


def test_egress_public_allows_public_addresses():
    check_egress("https://api.example.com/", "public", resolve=_resolver("93.184.216.34"))
    check_egress("https://api.example.com/", "public", resolve=_resolver("2606:2800:220:1:248:1893:25c8:1946"))


def test_egress_public_refuses_localhost_names_and_mixed_answers():
    with pytest.raises(EgressRefused):
        check_egress("http://localhost:8700/", "public")
    with pytest.raises(EgressRefused):
        check_egress("http://thing.localhost/", "public")
    # One private answer among public ones is enough to refuse.
    with pytest.raises(EgressRefused):
        check_egress("https://api.example.com/", "public", resolve=_resolver("93.184.216.34", "10.0.0.1"))
    with pytest.raises(EgressRefused):
        check_egress("https://nope.invalid/", "public", resolve=lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no such host")))


def test_egress_public_transport_refuses_before_connecting():
    with pytest.raises(ValueError):
        UrllibTransport(egress="strict")
    with pytest.raises(EgressRefused):
        UrllibTransport(egress="public").request("GET", "http://127.0.0.1:9/")


def test_service_with_public_egress_reports_it_and_refuses_a_lan_host(tmp_path):
    (tmp_path / "status.toml").write_text((EXAMPLES / "site-status" / "site-status.toml").read_text())
    svc = Service(Registry.from_paths([tmp_path]), name="sealed", egress="public")
    server = Server(svc.route, quiet=True).start()
    try:
        assert http_json("GET", f"{server.url}/health")[1]["egress"] == "public"
        _, out = http_json("POST", f"{server.url}/run", {"tree": "site-status", "inputs": {"host": "127.0.0.1"}})
        assert out["status"] == "error" and out["reason"] == "transport" and "not a public address" in out["say"]
        # Refused once, not retried: one step in the trace.
        assert [s["node"] for s in out["trace"]["steps"]] == ["probe"]
    finally:
        server.stop()


def test_a_body_must_say_it_is_json():
    """A page on another site may POST text/plain with no preflight: a hub with no token
    reads no body that does not say it is JSON, so such a page cannot drive it."""
    svc = Service(Registry.from_paths([EXAMPLES]), transport=RecordedTransport.from_file(FIXTURES / "examples.json"))
    server = Server(svc.route, port=0, quiet=True).start()
    try:
        body = b'{"text":"weather in Oslo"}'
        status, _, out = _raw(f"{server.url}/intent", {"Content-Type": "text/plain"}, "POST", body)
        assert status == 415 and json.loads(out)["reason"] == "not-json"
        assert _raw(f"{server.url}/intent", {}, "POST", body)[0] == 415   # no type at all is not JSON either
        assert _raw(f"{server.url}/intent", {"Content-Type": "application/json; charset=utf-8"}, "POST", body)[0] == 200
    finally:
        server.stop()


def test_odd_dates_and_lone_surrogates_are_refused_not_fatal():
    hub = Hub(name="solo", token="tok")
    server = Server(hub.route, port=0, quiet=True, token="tok", keyed=True).start()
    try:
        # "²³" (Latin-1, as headers are read) is digits to str.isdigit(), not to int(): a refusal, not a dead handler.
        host, port = server.url.split("//")[1].split(":")
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(b"POST /intent HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 13\r\n"
                         b"X-Pyrtan-Key: pyr:x\r\nX-Pyrtan-Key-Signature: AA\r\nX-Pyrtan-Date: \xb2\xb3\r\nConnection: close\r\n\r\n{\"text\":\"hi\"}")
            reply = sock.recv(4096)
        assert reply.startswith(b"HTTP/1.0 401") or reply.startswith(b"HTTP/1.1 401"), reply[:80]
        status, _, out = _raw(f"{server.url}/intent", {"Content-Type": "application/json", "Authorization": "Bearer tok"}, "POST", b'{"text":"\\ud800"}')
        assert status == 400 and json.loads(out)["reason"] == "bad-json"
        assert http_json("GET", f"{server.url}/health")[0] == 200   # still serving
    finally:
        server.stop()
        hub.stop()
