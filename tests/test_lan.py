"""Hubs on one LAN find each other: beacons, the multicast socket, and
joining a network through a hub heard nearby."""

import json
import random
import time

import pytest

from pyrtan import lan as lanmod
from pyrtan.lan import HEARD_FOR, HEARD_MAX, Lan, reachable
from pyrtan.service import http_json


def beacon(hub, networks=(), query=False, name="x"):
    body = {"pyrtan-lan": 1, "id": hub, "name": name, "networks": list(networks)}
    if query:
        body["query"] = True
    return json.dumps(body).encode()


@pytest.fixture
def group(monkeypatch):
    """A group of the tests' own, so they never hear a real hub, nor each other."""
    port = random.randint(20000, 60000)
    monkeypatch.setenv("PYRTAN_LAN_GROUP", f"239.255.70.84:{port}")
    return ("239.255.70.84", port)


def test_who_counts_as_reachable():
    assert reachable("http://192.168.1.20:8700") and reachable("https://nas.local")
    for url in ("http://127.0.0.1:8700", "http://localhost:8700", "http://0.0.0.0:8700", "http://127.0.1.1", "ftp://nas.local", "nas.local", ""):
        assert not reachable(url), url


def test_beacons_are_heard_kept_and_forgotten():
    me = {"id": "http://192.168.1.10:8700", "name": "home", "networks": ["n1"]}
    lan = Lan(lambda: me)
    sent = []
    lan.send = lambda query=False: sent.append(query) or True
    now = 1000.0
    assert lan.take(beacon("http://192.168.1.20:8700/", ["n1"], name="nas"), "192.168.1.20", now) is True
    assert lan.take(beacon("http://192.168.1.30:8700", ["n2"]), "192.168.1.30", now + 1) is True
    assert lan.take(beacon(me["id"], ["n1"]), "192.168.1.10", now) is False  # its own voice
    for junk in (b"not json", b"[]", json.dumps({"pyrtan-lan": 2, "id": "http://192.168.1.40"}).encode(), beacon("http://127.0.0.1:8700"), beacon("x" * 300)):
        assert lan.take(junk, "192.168.1.40", now) is None
    assert lan.hubs(now=now + 2) == ["http://192.168.1.30:8700", "http://192.168.1.20:8700"]  # most recent first
    assert lan.hubs("n1", now=now + 2) == ["http://192.168.1.20:8700"]
    assert lan.heard["http://192.168.1.20:8700"]["name"] == "nas" and lan.heard["http://192.168.1.20:8700"]["from"] == "192.168.1.20"
    assert lan.hubs(now=now + HEARD_FOR + 5) == []  # not heard for three minutes: gone
    # A question is answered, but not more than every couple of seconds.
    assert sent == []
    lan.take(beacon("http://192.168.1.50:8700", query=True), "192.168.1.50", now + 10)
    lan.take(beacon("http://192.168.1.51:8700", query=True), "192.168.1.51", now + 10.5)
    lan.take(json.dumps({"pyrtan-lan": 1, "query": True}).encode(), "192.168.1.52", now + 13)  # a hub that cannot be reached asks too
    assert sent == [False, False]
    # Kept to a bound.
    for i in range(HEARD_MAX + 10):
        lan.take(beacon(f"http://10.0.0.{i + 1}:8700"), f"10.0.0.{i + 1}", now + 20 + i)
    assert len(lan.heard) == HEARD_MAX and "http://10.0.0.1:8700" not in lan.heard


def test_what_a_hub_says_and_when_it_stays_quiet():
    said = {"id": "http://192.168.1.10:8700/", "name": "home", "networks": ["n1", 7]}
    lan = Lan(lambda: said)
    body = json.loads(lan.beacon())
    assert body == {"pyrtan-lan": 1, "id": "http://192.168.1.10:8700", "name": "home", "networks": ["n1"]}
    assert json.loads(lan.beacon(query=True))["query"] is True
    said["id"] = "http://127.0.0.1:8700"  # nobody else could reach it: it only asks
    assert lan.beacon() is None and json.loads(lan.beacon(query=True)) == {"pyrtan-lan": 1, "query": True}
    assert Lan(lambda: None).beacon() is None


def test_two_hubs_hear_each_other_over_multicast(group):
    a = Lan(lambda: {"id": "http://192.0.2.101:8700", "name": "a", "networks": ["n1"]}, address=group)
    b = Lan(lambda: {"id": "http://192.0.2.102:8700", "name": "b", "networks": []}, address=group)
    try:
        a.start()
        if a.error:
            pytest.skip(f"no multicast here: {a.error}")
        b.start()  # b asks who is there; a answers at once, and hears b's question too
        deadline = time.time() + 5
        while time.time() < deadline and not (a.hubs() and b.hubs()):
            time.sleep(0.05)
        assert b.hubs("n1") == ["http://192.0.2.101:8700"] and a.hubs() == ["http://192.0.2.102:8700"]
        assert a.view()["announcing"] is True and a.view()["hubs"][0]["name"] == "b"
    finally:
        a.stop()
        b.stop()


def test_joining_takes_a_hub_heard_on_the_lan_before_anyone_else(monkeypatch, tmp_path):
    import pyrtan.hub as hubmod
    from pyrtan.federation import default_network
    from pyrtan.hub import Hub
    from pyrtan.service import Server

    monkeypatch.delenv("PYRTAN_NETWORK", raising=False)  # the built-in default network
    hubs = []
    for name in ("a", "b"):
        hub = Hub(name=name, poll_every=60)
        srv = Server(hub.route, quiet=True, federated=True).start()
        hub.federate(srv.url)
        hubs.append((hub, srv))
    (a, a_srv), (b, b_srv) = hubs
    try:
        default = default_network()
        hubmod.join_network(b.federation, "default")  # b is the network so far
        # a heard b on the LAN say it is in the default network (as a socket would hand it over).
        a.lan = Lan(lambda: {"id": a.federation.id, "name": "a", "networks": []})
        a.federation.lan = a.lan
        a.lan.heard[b.federation.id] = {"id": b.federation.id, "name": "b", "networks": [default.id], "from": "192.0.2.2", "seen": time.time()}
        a.federation.directory_entries["http://gossiped.example"] = {"id": "http://gossiped.example", "name": "g", "trees": [], "seen": time.time(), "network": default.id}
        assert a.federation.known_hubs(default.id) == [b.federation.id, "http://gossiped.example"]  # the LAN before the directory

        def nobody(*_):
            raise AssertionError("asked beyond the LAN although a hub there answered")

        monkeypatch.setattr(hubmod, "rendezvous_hubs", nobody)
        monkeypatch.setattr(hubmod, "snapshot_hubs", nobody)
        status, out = hubmod.join_network(a.federation, "default")
        assert status == 200 and out["say"] == f"in pyrtan, following {b.federation.id}"
    finally:
        for hub, srv in hubs:
            hub.stop()
            srv.stop()


def test_a_hub_on_the_lan_without_a_lan_address_listens_only(group, monkeypatch, tmp_path):
    from pyrtan.hub import serve_hub

    monkeypatch.setenv("PYRTAN_HUB_LIST", str(tmp_path / "none.json"))
    monkeypatch.setenv("PYRTAN_SNAPSHOT", str(tmp_path / "none.json"))
    hub, srv = serve_hub(port=0, quiet=True, name="solo", lan=True, token="t")
    try:
        assert hub.lan is not None and hub.federation is not None and hub.federation.lan is hub.lan
        status, view = http_json("GET", f"{srv.url}/lan", token="t")
        assert status == 200 and view["announcing"] is False and view["group"] == f"{group[0]}:{group[1]}"  # 127.0.0.1 is nobody else's address
        assert http_json("GET", f"{srv.url}/lan")[0] == 401  # what the LAN said is the operator's
    finally:
        hub.stop()
        srv.stop()
    assert lanmod.group() == group
