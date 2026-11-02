"""Discovery: mDNS / DNS-SD, SSDP, BACnet Who-Is and a TCP scan, each
against a fake on localhost (the multicast and broadcast addresses pointed
at it), held to a tree's networks and the egress policy, and answered from
a fixture like every other source."""

from __future__ import annotations

import http.server
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from pyrtan import Registry, RecordedTransport, Runner, discover
from pyrtan.adapters import LocalAdapters, adapter_key, brief_result, what
from pyrtan.cli import main
from pyrtan.transport import EgressRefused, RecordingTransport, TransportError
from pyrtan.tree import TreeError, load_tree_text


class UDP:
    def __init__(self, handle) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.addr = self.sock.getsockname()
        self.handle = handle
        self.got: list[bytes] = []
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(9000)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            self.got.append(data)
            for reply in self.handle(data) or []:
                self.sock.sendto(reply, addr)

    def close(self) -> None:
        self.stop.set()
        self.sock.close()


def rr(name: str, rtype: int, rdata: bytes) -> bytes:
    return discover._qname(name) + struct.pack(">HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


def matter_answer(query: bytes) -> list[bytes]:
    """A Matter device answering a PTR query for _matter._tcp, the way a
    responder does: the PTR, with SRV, TXT and A as additional records."""
    if b"_matter" not in query:
        return []
    inst = "ABCD1234-0000000000000004._matter._tcp.local"
    answers = [rr("_matter._tcp.local", 12, discover._qname(inst))]
    extra = [
        rr(inst, 33, struct.pack(">HHH", 0, 0, 5540) + discover._qname("kettle-plug.local")),
        rr(inst, 16, b"\x05SII=5" + b"\x06SAI=30"),
        rr("kettle-plug.local", 1, socket.inet_aton("127.0.0.1")),
    ]
    return [struct.pack(">HHHHHH", 0, 0x8400, 0, len(answers), 0, len(extra)) + b"".join(answers + extra)]


def test_dns_records_follow_compression():
    # A PTR whose target points back into the question's name: c0 0c.
    msg = struct.pack(">HHHHHH", 0, 0x8400, 1, 1, 0, 0) + discover._qname("_hap._tcp.local") + struct.pack(">HH", 12, 1)
    msg += b"\xc0\x0c" + struct.pack(">HHIH", 12, 1, 120, 9) + b"\x06Lamp-1\xc0\x0c"
    assert discover.dns_records(msg) == [{"name": "_hap._tcp.local", "type": 12, "target": "Lamp-1._hap._tcp.local"}]
    assert discover.mdns_query("_hap._tcp.local")[-4:] == struct.pack(">HH", 12, 0x8001)


def test_mdns_browses_a_service(monkeypatch):
    fake = UDP(matter_answer)
    monkeypatch.setattr(discover, "MDNS", fake.addr)
    try:
        found = discover.mdns("_matter._tcp", 0.6)
    finally:
        fake.close()
    assert found == [{"name": "ABCD1234-0000000000000004", "service": "_matter._tcp.local", "host": "kettle-plug.local", "address": "127.0.0.1", "addresses": ["127.0.0.1"], "port": 5540, "txt": {"SII": "5", "SAI": "30"}}]


class Description(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<friendlyName>Living room TV</friendlyName><manufacturer>Acme</manufacturer><modelName>TV-9</modelName></device></root>"""
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_ssdp_finds_devices_and_reads_their_descriptions(monkeypatch):
    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Description)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    loc = f"http://127.0.0.1:{web.server_address[1]}/desc.xml"

    def answer(data: bytes) -> list[bytes]:
        assert data.startswith(b"M-SEARCH * HTTP/1.1") and b"ST: ssdp:all" in data
        return [f"HTTP/1.1 200 OK\r\nLOCATION: {loc}\r\nSERVER: Linux UPnP/1.0\r\nST: upnp:rootdevice\r\nUSN: uuid:tv::upnp:rootdevice\r\n\r\n".encode()] * 2
    fake = UDP(answer)
    monkeypatch.setattr(discover, "SSDP", fake.addr)
    try:
        found = discover.ssdp("ssdp:all", 0.6, lambda a: True)
        hidden = discover.ssdp("ssdp:all", 0.6, lambda a: False)  # a location outside the networks is not fetched
    finally:
        fake.close()
        web.shutdown()
    assert found == [{"address": "127.0.0.1", "location": loc, "server": "Linux UPnP/1.0", "st": "upnp:rootdevice", "usn": "uuid:tv::upnp:rootdevice", "friendly_name": "Living room TV", "manufacturer": "Acme", "model": "TV-9"}]
    assert hidden[0]["friendly_name"] is None


def i_am_packet(instance: int, vendor: int, routed: bool = False) -> bytes:
    objid = struct.pack(">I", (8 << 22) | instance)
    apdu = b"\x10\x00" + b"\xc4" + objid + b"\x22\x05\xc4" + b"\x91\x03" + b"\x21" + bytes([vendor])
    npdu = b"\x01\x08" + struct.pack(">HB", 5, 1) + b"\x0c" if routed else b"\x01\x00"
    data = npdu + apdu
    return struct.pack(">BBH", 0x81, 0x0B, len(data) + 4) + data


def test_bacnet_who_is_gathers_i_ams(monkeypatch):
    assert discover.who_is_packet() == bytes.fromhex("810b000c" + "0120ffff00ff" + "1008")
    assert discover.who_is_packet(100, 200).endswith(bytes.fromhex("1008" + "0964" + "19c8"))
    fake = UDP(lambda data: [i_am_packet(1234, 5), i_am_packet(77, 24, routed=True), i_am_packet(1234, 5)] if data[-2:] == b"\x10\x08" else [])
    monkeypatch.setattr(discover, "BACNET_BROADCAST", fake.addr)
    try:
        found = discover.who_is(0.6)
    finally:
        fake.close()
    assert [(d["instance"], d["vendor"], d["network"], d["mac"], d["max_apdu"]) for d in found] == [(77, 24, 5, "0c", 1476), (1234, 5, None, None, 1476)]
    assert all(d["address"] == "127.0.0.1" for d in found)


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    yield s.getsockname()[1]
    s.close()


def test_scan_finds_open_ports_and_hosts_that_refuse(listener):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    shut = closed.getsockname()[1]
    closed.close()
    found = discover.scan("127.0.0.1/32", [listener, shut], 0.5, 5.0, False, lambda a: True)
    assert found == [{"address": "127.0.0.1", "open": [listener]}]
    with pytest.raises(TransportError, match="outside this tree's networks"):
        discover.scan("10.0.0.0/24", [22], 0.5, 5.0, False, lambda a: False)
    with pytest.raises(TransportError, match="more than a tree scans"):
        discover.scan("10.0.0.0/16", [22], 0.5, 5.0, False, lambda a: True)


def test_networks_hold_addresses_and_subnets():
    nets = ["192.168.1.0/24", "10.0.0.0/8"]
    assert discover.network_allowed(nets, "192.168.1.20") and discover.network_allowed(nets, "192.168.1.128/25") and discover.network_allowed(nets, "10.9.8.7")
    assert not discover.network_allowed(nets, "192.168.2.1") and not discover.network_allowed(nets, "192.168.0.0/16") and not discover.network_allowed(nets, "kettle.local")
    assert discover.network_allowed(["*"], "8.8.8.8") and not discover.network_allowed([], "10.0.0.1")


TREE = """
name = "lan"
description = "what is on the LAN"
networks = ["127.0.0.0/8"]
start = "look"

[inputs.net]
type = "str"
default = "127.0.0.1/32"

[nodes.look]
kind = "observe"
discover = {{ scan = "{{{{ inputs.net }}}}", ports = [{port}], wait = 1 }}
into = "lan"
next = "say"

[nodes.say]
kind = "done"
say = "{{{{ state.lan.found }}}} up: {{{{ state.lan.devices | json }}}}"
"""


def test_a_tree_discovers_within_its_networks_records_and_replays(tmp_path, listener):
    d = tmp_path / "trees"
    d.mkdir()
    (d / "lan.toml").write_text(TREE.format(port=listener))
    reg = Registry.from_paths([d])
    assert not reg.errors, reg.errors
    rec = RecordingTransport(None, LocalAdapters())  # type: ignore[arg-type]
    out = Runner(reg, rec).run_tree("lan", {})
    assert out.status == "done" and out.say == f'1 up: [{{"address": "127.0.0.1", "open": [{listener}]}}]'
    assert out.trace.steps[0].summary == f"discover scan 127.0.0.1/32 ports {listener} -> 1 found"
    assert list(rec.recorded) == [f"discover scan 127.0.0.1/32 ports {listener}"]
    assert Runner(reg, RecordedTransport(json.loads(json.dumps(rec.recorded)))).run_tree("lan", {}).say == out.say
    refused = Runner(reg, rec).run_tree("lan", {"net": "192.168.1.0/24"})
    assert refused.reason == "network-not-allowed" and "192.168.1.0/24 is outside this tree's networks" in refused.say
    with pytest.raises(EgressRefused):
        LocalAdapters(egress="public").perform("discover", {"scan": "127.0.0.1/32"}, 1.0, lambda a: True)


def test_checker_and_words():
    head = 'name = "t"\ndescription = "t"\nnetworks = ["192.168.1.0/24"]\nstart = "a"\n[nodes.b]\nkind = "done"\nsay = "x"\n[nodes.a]\nkind = "observe"\nnext = "b"\n'
    load_tree_text(head + 'discover = { mdns = "_matter._tcp" }')
    load_tree_text(head + 'discover = { scan = "192.168.1.0/28", ports = [22, 502] }')
    for bad, why in (('discover = { scan = "192.168.2.0/24" }', "not inside this tree's networks"), ('discover = { mdns = "x", ssdp = "y" }', "exactly one of"),
                     ('discover = { bacnet = "who-has" }', "'bacnet' is"), ('discover = { mdns = "x", ports = [22] }', "go with scan"),
                     ('discover = { scan = "192.168.1.0/24", ports = [0] }', "port numbers"), ('discover = { scan = "nope" }', "not an address or a network")):
        with pytest.raises(TreeError, match=why):
            load_tree_text(head + bad)
    with pytest.raises(TreeError, match="must list the networks"):
        load_tree_text(head.replace('networks = ["192.168.1.0/24"]\n', "") + 'discover = { mdns = "_hap._tcp" }')
    with pytest.raises(TreeError, match="not a network"):
        load_tree_text(head.replace("192.168.1.0/24", "kitchen") + 'discover = { mdns = "_hap._tcp" }')
    assert load_tree_text(head + 'discover = { ssdp = "ssdp:all" }').capability()["networks"] == ["192.168.1.0/24"]
    assert adapter_key("discover", {"mdns": ""}) == "discover mdns" and adapter_key("discover", {"bacnet": "who-is", "wait": 2}) == "discover bacnet who-is"
    assert what("discover", {"scan": "10.0.0.0/24", "ports": [22, 80]}) == "scan 10.0.0.0/24 ports 22,80"
    assert brief_result("discover", {"found": 3}) == "3 found"


def test_the_command_scans(capsys, listener):
    assert main(["discover", "scan", "127.0.0.1/32", "--ports", str(listener), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"address": "127.0.0.1", "open": [listener]}]
    assert main(["discover", "scan", "127.0.0.1", "--ports", str(listener)]) == 0
    assert capsys.readouterr().out.startswith("127.0.0.1")
