"""The field protocols: Modbus TCP, BACnet/IP, SNMP, CoAP and Matter (through
a controller's WebSocket). Each is spoken against a small fake device on
localhost, held to its allow-list and the egress policy, and answered
from a fixture like every other source."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from pyrtan import Registry, RecordedTransport, Runner, bacnet, coap, matter, modbus, snmp
from pyrtan.adapters import LocalAdapters, adapter_key, brief_result, what, would
from pyrtan.transport import EgressRefused, TransportError
from pyrtan.tree import TreeError, check, load_tree_text


def registry(tmp_path: Path, **trees: str) -> Registry:
    d = tmp_path / "trees"
    d.mkdir(exist_ok=True)
    for name, text in trees.items():
        (d / f"{name}.toml").write_text(text)
    reg = Registry.from_paths([d])
    assert not reg.errors, reg.errors
    return reg


def problems(text: str) -> list[str]:
    """What the checker refuses in a tree, as one line per problem (none: an empty list)."""
    try:
        tree = load_tree_text(text)
    except TreeError as e:
        return str(e).split("; ")
    return [str(p) for p in check(tree) if p.level == "error"]


class UDP:
    """A UDP device on localhost: ``handle(packet) -> reply bytes or None``."""

    def __init__(self, handle) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.handle = handle
        self.got: list[bytes] = []
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
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


class TCP:
    """A TCP device on localhost: ``serve(conn)`` per connection."""

    def __init__(self, serve) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.serve = serve
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            self.serve(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.stop.set()
        self.sock.close()


def recv_exact(conn: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise OSError("closed")
        out += chunk
    return out


# ---------------------------------------------------------------- Modbus TCP


class Plc:
    """Holding registers, input registers, coils; unit 9 is a gateway with nothing behind it."""

    def __init__(self) -> None:
        self.holding = {0: 215, 1: 0xFFFE, 100: 0x4242, 101: 0x0000}  # 100-101: float32 48.5
        self.coils = {0: 1, 1: 0, 2: 1}
        self.writes: list[tuple[int, int, bytes]] = []
        self.server = TCP(self.serve)
        self.url = f"modbus://127.0.0.1:{self.server.port}"

    def serve(self, conn: socket.socket) -> None:
        head = recv_exact(conn, 7)
        tid, _, length, unit = struct.unpack(">HHHB", head)
        pdu = recv_exact(conn, length - 1)
        fc = pdu[0]
        if unit == 9:
            reply = bytes([fc | 0x80, 11])
        elif fc in (3, 4):
            address, count = struct.unpack(">HH", pdu[1:5])
            if address + count > 200:
                reply = bytes([fc | 0x80, 2])
            else:
                words = [self.holding.get(address + i, 0) for i in range(count)]
                reply = bytes([fc, 2 * count]) + b"".join(struct.pack(">H", w) for w in words)
        elif fc in (1, 2):
            address, count = struct.unpack(">HH", pdu[1:5])
            packed = bytearray((count + 7) // 8)
            for i in range(count):
                if self.coils.get(address + i):
                    packed[i // 8] |= 1 << (i % 8)
            reply = bytes([fc, len(packed)]) + bytes(packed)
        elif fc in (5, 6):
            self.writes.append((unit, fc, pdu))
            reply = pdu[:5]
        elif fc in (15, 16):
            self.writes.append((unit, fc, pdu))
            reply = pdu[:5]
        else:
            reply = bytes([fc | 0x80, 1])
        conn.sendall(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)


@pytest.fixture
def plc():
    p = Plc()
    yield p
    p.server.close()


def test_modbus_reads_registers_and_coils_and_types(plc):
    r = modbus.perform({"url": plc.url, "read": "holding", "address": 0}, 2.0)
    assert r["ok"] and r["value"] == 215 and r["raw"] == [215] and r["body"] == "[215]"
    r = modbus.perform({"url": plc.url, "read": "holding", "address": 0, "count": 2, "type": "int16", "scale": 0.1}, 2.0)
    assert r["values"] == [21.5, -0.2]
    r = modbus.perform({"url": plc.url, "read": "input", "address": 100, "type": "float32"}, 2.0)
    assert r["value"] == 48.5 and r["raw"] == [0x4242, 0]
    r = modbus.perform({"url": plc.url, "read": "coil", "address": 0, "count": 3}, 2.0)
    assert r["values"] == [True, False, True]
    # An exception is the device's answer: a tree decides on it.
    r = modbus.perform({"url": plc.url, "read": "holding", "address": 199, "count": 2}, 2.0)
    assert r["ok"] is False and r["exception"] == {"code": 2, "name": "illegal-data-address"}
    r = modbus.perform({"url": plc.url, "unit": 9, "read": "holding", "address": 0}, 2.0)
    assert r["exception"]["name"] == "gateway-target-failed-to-respond"


def test_modbus_writes(plc):
    r = modbus.perform({"url": plc.url, "unit": 2, "write": "coil", "address": 3, "value": True}, 2.0)
    assert r["ok"] and r["written"] == 1 and plc.writes[-1] == (2, 5, struct.pack(">BHH", 5, 3, 0xFF00))
    r = modbus.perform({"url": plc.url, "write": "holding", "address": 10, "value": 21.5, "scale": 0.1}, 2.0)
    assert r["written"] == 1 and plc.writes[-1][2] == struct.pack(">BHH", 6, 10, 215)
    r = modbus.perform({"url": plc.url, "write": "holding", "address": 20, "value": 48.5, "type": "float32"}, 2.0)
    assert r["written"] == 2 and plc.writes[-1][2] == struct.pack(">BHHB", 16, 20, 2, 4) + bytes.fromhex("42420000")
    r = modbus.perform({"url": plc.url, "write": "coil", "address": 0, "value": [1, 0, 1, 1]}, 2.0)
    assert r["written"] == 4 and plc.writes[-1][2] == struct.pack(">BHHB", 15, 0, 4, 1) + bytes([0b1101])
    with pytest.raises(TransportError, match="read-only"):
        modbus.perform({"url": plc.url, "write": "input", "address": 0, "value": 1}, 2.0)


def test_modbus_no_device_is_a_transport_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(TransportError):
        modbus.perform({"url": f"modbus://127.0.0.1:{port}", "read": "holding", "address": 0}, 1.0)


# ---------------------------------------------------------------- BACnet/IP


def bms_handle(packet: bytes) -> list[bytes]:
    """A BACnet device: analog-input:3 is 21.5, binary-output:1 is active,
    device:1234 has a name; anything else is unknown-object."""
    apdu = packet[6:]
    assert packet[:2] == b"\x81\x0a" and packet[4:6] == b"\x01\x04"
    invoke, service = apdu[2], apdu[3]
    obj = apdu[5:9]
    t, inst = struct.unpack(">I", obj)[0] >> 22, struct.unpack(">I", obj)[0] & 0x3FFFFF
    prop = apdu[10]

    def frame(a: bytes) -> bytes:
        body = b"\x01\x00" + a
        return struct.pack(">BBH", 0x81, 0x0A, len(body) + 4) + body

    if service == 15:  # WriteProperty
        if (t, inst) == (2, 7):
            return [frame(bytes([0x20, invoke, 15]))]
        return [frame(bytes([0x50, invoke, 15, 0x91, 2, 0x91, 40]))]  # property / write-access-denied
    if (t, inst, prop) == (0, 3, 85):
        value = b"\x44" + struct.pack(">f", 21.5)
    elif (t, inst, prop) == (4, 1, 85):
        value = b"\x91\x01"
    elif (t, inst, prop) == (8, 1234, 77):
        value = b"\x75\x06\x00" + b"AHU-1"
    elif (t, inst, prop) == (0, 3, 111):
        value = b"\x82\x04\x40"  # status-flags: fault
    else:
        return [frame(bytes([0x50, invoke, 12, 0x91, 1, 0x91, 31]))]  # object / unknown-object
    return [frame(bytes([0x30, invoke, 12]) + apdu[4:11] + b"\x3e" + value + b"\x3f")]


@pytest.fixture
def bms():
    u = UDP(bms_handle)
    yield u
    u.close()


def test_bacnet_encodes_what_the_standard_says():
    assert bacnet.object_id("analog-input:3") == (0, 3) and bacnet.object_id("ai:3") == (0, 3) and bacnet.object_id("8:1234") == (8, 1234)
    assert bacnet.request(12, 1, (0, 3), 85) == bytes.fromhex("0005010c0c00000003" + "1955")
    assert bacnet.request(15, 2, (2, 7), 85, value=bacnet._application(21.5, "real"), priority=8) == bytes.fromhex("0005020f0c00800007195" + "53e4441ac00003f4908")
    assert bacnet.frame(b"\xaa", network=5, mac=12) == bytes.fromhex("810a000c" + "0124" + "000501" + "0c" + "ff" + "aa")
    with pytest.raises(ValueError):
        bacnet.object_id("pump:1")


def test_bacnet_reads_and_writes(bms):
    url = f"bacnet://127.0.0.1:{bms.port}"
    r = bacnet.perform({"url": url, "object": "analog-input:3"}, 2.0)
    assert r["ok"] and r["value"] == 21.5 and r["property"] == "present-value" and r["body"] == "21.5"
    assert bacnet.perform({"url": url, "object": "bo:1"}, 2.0)["value"] == 1
    assert bacnet.perform({"url": url, "object": "device:1234", "property": "object-name"}, 2.0)["value"] == "AHU-1"
    assert bacnet.perform({"url": url, "object": "analog-input:3", "property": "status-flags"}, 2.0)["value"] == [False, True, False, False]
    r = bacnet.perform({"url": url, "object": "analog-input:99"}, 2.0)
    assert r["ok"] is False and r["refused"] == {"kind": "error", "class": "object", "code": "unknown-object"}
    w = bacnet.perform({"url": url, "object": "analog-value:7", "write": 21.5, "priority": 8}, 2.0)
    assert w["ok"] and w["written"] is True
    assert bms.got[-1][6:].hex().endswith("3e4441ac00003f4908")
    w = bacnet.perform({"url": url, "object": "analog-value:8", "write": 21.5}, 2.0)
    assert w["written"] is False and w["refused"]["code"] == "write-access-denied"


def test_bacnet_silence_is_a_transport_error():
    quiet = UDP(lambda p: None)
    try:
        with pytest.raises(TransportError, match="no answer"):
            bacnet.perform({"url": f"bacnet://127.0.0.1:{quiet.port}", "object": "ai:1"}, 0.6)
        assert len(quiet.got) >= 1
    finally:
        quiet.close()


# ---------------------------------------------------------------- SNMP


MIB = {
    "1.3.6.1.2.1.1.5.0": (0x04, b"core-switch"),
    "1.3.6.1.2.1.1.3.0": (0x43, (123456).to_bytes(3, "big")),
    "1.3.6.1.2.1.2.2.1.2.1": (0x04, b"eth0"),
    "1.3.6.1.2.1.2.2.1.2.2": (0x04, b"eth1"),
    "1.3.6.1.2.1.2.2.1.2.3": (0x04, b"lo"),
    "1.3.6.1.2.1.2.2.1.6.1": (0x04, bytes.fromhex("001122aabbcc")),
    "1.3.6.1.2.1.2.2.1.8.1": (0x02, b"\x01"),
}


def _key(o: str) -> tuple[int, ...]:
    return tuple(int(p) for p in o.split("."))


def agent_handle(packet: bytes) -> list[bytes]:
    """An SNMP agent with community "public" (anything else: silence)."""
    _, msg, _ = snmp._read(packet, 0)
    _, ver, i = snmp._read(msg, 0)
    _, community, i = snmp._read(msg, i)
    pdu_tag, pdu, _ = snmp._read(msg, i)
    if community != b"public":
        return []
    _, rid, j = snmp._read(pdu, 0)
    _, a, j = snmp._read(pdu, j)
    _, b, j = snmp._read(pdu, j)
    _, vbs, _ = snmp._read(pdu, j)
    asked = []
    k = 0
    while k < len(vbs):
        _, vb, k = snmp._read(vbs, k)
        _, o, m = snmp._read(vb, 0)
        vtag, vraw, _ = snmp._read(vb, m)
        asked.append((snmp._decode_oid(o), vtag, vraw))
    out = []
    status = 0
    order = sorted(MIB, key=_key)
    for o, vtag, vraw in asked:
        if pdu_tag == snmp.GET:
            tag, raw = MIB.get(o, (0x81, b""))
            out.append((o, tag, raw))
        elif pdu_tag in (snmp.GETNEXT, snmp.GETBULK):
            reps = int.from_bytes(b, "big") if pdu_tag == snmp.GETBULK else 1
            after = [x for x in order if _key(x) > _key(o)][:reps]
            for x in after:
                out.append((x, *MIB[x]))
            if not after:
                out.append((o, 0x82, b""))
        elif pdu_tag == snmp.SET:
            if o == "1.3.6.1.2.1.1.5.0":
                status = 17  # notWritable
            out.append((o, vtag, vraw))
    vbl = b"".join(snmp._tlv(0x30, snmp._oid(o) + snmp._tlv(tag, raw)) for o, tag, raw in out)
    body = snmp._tlv(0x02, rid) + snmp._int(status) + snmp._int(0) + snmp._tlv(0x30, vbl)
    return [snmp._tlv(0x30, snmp._tlv(0x02, ver) + snmp._tlv(0x04, community) + snmp._tlv(snmp.RESPONSE, body))]


@pytest.fixture
def agent():
    u = UDP(agent_handle)
    yield u
    u.close()


def test_snmp_encodes_what_the_standard_says():
    assert snmp.oid("sysName.0") == "1.3.6.1.2.1.1.5.0" and snmp.oid(".1.3.6.1") == "1.3.6.1"
    assert snmp.message(1, "public", snmp.GET, 1, [("1.3.6.1.2.1.1.5.0", b"\x05\x00")]) == bytes.fromhex(
        "3026020101040670756 26c6963a019020101020100020100300e300c06082b060102010105000500".replace(" ", ""))
    assert snmp._oid("1.3.6.1.4.1.311") == bytes.fromhex("06072b060104018237")
    with pytest.raises(ValueError):
        snmp.oid("notAName.0")


def test_snmp_get_walk_and_set(agent):
    url = f"snmp://127.0.0.1:{agent.port}"
    r = snmp.perform({"url": url, "get": ["sysName.0", "sysUpTime.0"]}, 2.0)
    assert r["ok"] and r["status"] == "noError" and r["values"] == {"sysName.0": "core-switch", "sysUpTime.0": 123456} and r["value"] == "core-switch"
    assert r["varbinds"][1] == {"oid": "1.3.6.1.2.1.1.3.0", "type": "timeticks", "value": 123456}
    r = snmp.perform({"url": url, "get": "1.3.6.1.2.1.1.9.0"}, 2.0)
    assert r["ok"] is False and r["values"] == {"1.3.6.1.2.1.1.9.0": None}
    for version in ("2c", "1"):
        r = snmp.perform({"url": url, "walk": "ifDescr", "version": version}, 2.0)
        assert r["ok"] and r["values"] == {"1": "eth0", "2": "eth1", "3": "lo"}, version
    assert snmp.perform({"url": url, "walk": "ifDescr", "max": 2}, 2.0)["values"] == {"1": "eth0", "2": "eth1"}
    assert snmp.perform({"url": url, "get": "ifPhysAddress.1"}, 2.0)["value"] == "00:11:22:aa:bb:cc"
    s = snmp.perform({"url": url, "set": {"oid": "sysLocation.0", "value": "rack 4"}}, 2.0)
    assert s["ok"] and s["written"] is True
    s = snmp.perform({"url": url, "set": {"oid": "sysName.0", "value": "x"}}, 2.0)
    assert s["ok"] is False and s["status"] == "notWritable"
    with pytest.raises(TransportError, match="community"):
        snmp.perform({"url": url, "get": "sysName.0", "community": "wrong"}, 0.6)


# ---------------------------------------------------------------- CoAP


def garden_handle(packet: bytes) -> list[bytes]:
    """/sensors/bed-1/moisture is JSON; /big comes in 16-byte blocks; /slow
    answers separately; /valves/1 takes a PUT."""
    msg = coap.decode(packet)
    path = "/" + "/".join(v.decode() for n, v in msg["options"] if n == coap.URI_PATH)
    block = next((int.from_bytes(v, "big") for n, v in msg["options"] if n == coap.BLOCK2), None)
    ack = lambda code, opts, payload=b"": coap.encode(coap.ACK, code, msg["mid"], msg["token"], opts, payload)  # noqa: E731
    if path == "/sensors/bed-1/moisture" and msg["code"] == 1:
        return [ack(0x45, [(coap.CONTENT_FORMAT, bytes([50]))], b'{"moisture": 22.5}')]
    if path == "/big":
        body = "".join(f"{i:02d}" for i in range(40)).encode()
        num = (block >> 4) if block is not None else 0
        chunk = body[num * 16:(num + 1) * 16]
        more = (num + 1) * 16 < len(body)
        return [ack(0x45, [(coap.BLOCK2, bytes([(num << 4) | (0x08 if more else 0) | 0]))], chunk)]
    if path == "/slow":
        return [coap.encode(coap.ACK, 0, msg["mid"], b"", []), coap.encode(coap.CON, 0x45, 4242, msg["token"], [], b"late")]
    if path == "/valves/1" and msg["code"] == 3:
        return [ack(0x44, [], b"")] if msg["payload"] == b"open" else [ack(0x80, [], b"")]
    return [ack(0x84, [], b"")]


@pytest.fixture
def garden():
    u = UDP(garden_handle)
    yield u
    u.close()


def test_coap_encodes_what_the_rfc_says():
    # CON GET, mid 0x1234, token 0xab, Uri-Path "a", Uri-Path "bcd"
    assert coap.encode(coap.CON, 1, 0x1234, b"\xab", [(11, b"a"), (11, b"bcd")]) == bytes.fromhex("41011234ab" + "b161" + "03626364")
    long = coap.encode(coap.NON, 1, 1, b"", [(11, b"x" * 20)])
    assert coap.decode(long)["options"] == [(11, b"x" * 20)]
    assert coap.status(0x45) == "2.05" and coap.status(0x84) == "4.04"


def test_coap_get_put_blocks_and_separate_responses(garden):
    base = f"coap://127.0.0.1:{garden.port}"
    r = coap.perform({"url": f"{base}/sensors/bed-1/moisture"}, 2.0)
    assert r["status"] == "2.05" and r["ok"] and r["json"] == {"moisture": 22.5} and r["format"] == "json"
    assert coap.perform({"url": f"{base}/nothing"}, 2.0)["status"] == "4.04"
    r = coap.perform({"url": f"{base}/big"}, 2.0)
    assert r["body"] == "".join(f"{i:02d}" for i in range(40))
    assert coap.perform({"url": f"{base}/slow"}, 2.0)["body"] == "late"
    assert coap.perform({"url": f"{base}/valves/1", "method": "PUT", "payload": "open"}, 2.0)["status"] == "2.04"
    with pytest.raises(TransportError, match="DTLS"):
        coap.perform({"url": "coaps://127.0.0.1/x"}, 1.0)


# ---------------------------------------------------------------- Matter, through a controller


class Controller:
    """A python-matter-server stand-in: node 4 is a light (on), node 7 is not reachable."""

    def __init__(self) -> None:
        self.on = True
        self.commands: list[dict] = []
        self.server = TCP(self.serve)
        self.url = f"ws://127.0.0.1:{self.server.port}/ws"

    @staticmethod
    def send(conn: socket.socket, obj: dict) -> None:
        data = json.dumps(obj).encode()
        head = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack(">H", len(data)))
        conn.sendall(head + data)

    @staticmethod
    def recv(conn: socket.socket) -> dict | None:
        b0, b1 = recv_exact(conn, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", recv_exact(conn, 2))[0]
        mask = recv_exact(conn, 4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(recv_exact(conn, n)))
        if b0 & 0x0F == 0x8:
            return None
        return json.loads(data)

    def serve(self, conn: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1024)
        key = next(line.split(":", 1)[1].strip() for line in head.decode().split("\r\n") if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        self.send(conn, {"fabric_id": 1, "schema_version": 11, "sdk_version": "test"})
        while True:
            msg = self.recv(conn)
            if msg is None:
                return
            mid, args = msg["message_id"], msg["args"]
            self.send(conn, {"event": "node_updated", "data": {}})  # an event in between: not the answer
            if args["node_id"] == 7:
                self.send(conn, {"message_id": mid, "error_code": 5, "details": "Node 7 is not (yet) available"})
            elif msg["command"] == "read_attribute":
                path = args["attribute_path"]
                value = {"1/6/0": self.on, "1/1026/0": 2150}.get(path)
                self.send(conn, {"message_id": mid, "result": {path: value}})
            elif msg["command"] == "device_command":
                self.commands.append(args)
                self.on = {"On": True, "Off": False, "Toggle": not self.on}[args["command_name"]]
                self.send(conn, {"message_id": mid, "result": None})
            elif msg["command"] == "write_attribute":
                self.send(conn, {"message_id": mid, "result": [{"Path": {"EndpointId": 1}, "Status": 0}]})


@pytest.fixture
def controller():
    c = Controller()
    yield c
    c.server.close()


def test_matter_paths_by_name_and_number():
    assert matter.attribute_path("1/OnOff/OnOff") == "1/6/0" and matter.attribute_path("1/temperature-measurement/measured-value") == "1/1026/0"
    assert matter.attribute_path("2/0x0300/7") == "2/768/7" and matter.cluster_id("LevelControl") == 8
    with pytest.raises(ValueError):
        matter.attribute_path("1/OnOff")
    with pytest.raises(ValueError):
        matter.attribute_path("1/OnOff/Brightness")


def test_matter_reads_writes_and_commands(controller):
    url = controller.url
    r = matter.perform({"url": url, "node": 4, "read": "1/OnOff/OnOff"}, 2.0)
    assert r["ok"] and r["value"] is True and r["path"] == "1/6/0"
    assert matter.perform({"url": url, "node": 4, "read": "1/TemperatureMeasurement/MeasuredValue", "scale": 0.01}, 2.0)["value"] == 21.5
    c = matter.perform({"url": url, "node": 4, "endpoint": 1, "cluster": "OnOff", "command": "Off"}, 2.0)
    assert c["ok"] and controller.commands[-1] == {"node_id": 4, "endpoint_id": 1, "cluster_id": 6, "command_name": "Off", "payload": {}}
    assert matter.perform({"url": url, "node": 4, "read": "1/6/0"}, 2.0)["value"] is False
    w = matter.perform({"url": url, "node": 4, "write": "1/LevelControl/OnLevel", "value": 128}, 2.0)
    assert w["ok"] and w["written"] is True
    e = matter.perform({"url": url, "node": 7, "read": "1/6/0"}, 2.0)
    assert e["ok"] is False and e["refused"] == {"code": 5, "details": "Node 7 is not (yet) available"}


def test_matter_needs_a_websocket():
    plain = TCP(lambda conn: (conn.recv(1024), conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")))
    try:
        with pytest.raises(TransportError, match="not a WebSocket"):
            matter.perform({"url": f"ws://127.0.0.1:{plain.port}/ws", "node": 1, "read": "1/6/0"}, 2.0)
    finally:
        plain.close()


# ---------------------------------------------------------------- the trees: lists, checks, words, fixtures, egress


FIELD_TREE = """
name = "plant"
description = "every field protocol at once"
hosts = ["127.0.0.1"]
start = "level"

[inputs.host]
type = "str"
default = "127.0.0.1"

[nodes.level]
kind = "observe"
modbus = {{ url = "{modbus}", unit = 1, read = "input", address = 100, type = "float32" }}
into = "level"
next = "zone"

[nodes.zone]
kind = "observe"
bacnet = {{ url = "{bacnet}", object = "analog-input:3" }}
into = "zone"
next = "name"

[nodes.name]
kind = "observe"
snmp = {{ url = "{snmp}", get = "sysName.0" }}
into = "sw"
next = "soil"

[nodes.soil]
kind = "observe"
coap = {{ url = "{coap}/sensors/bed-1/moisture" }}
into = "soil"
next = "light"

[nodes.light]
kind = "act"
matter = {{ url = "{matter}", node = 4, endpoint = 1, cluster = "OnOff", command = "On" }}
into = "light"
next = "done"

[nodes.done]
kind = "done"
say = "level {{{{ state.level.value }}}}, zone {{{{ state.zone.value }}}}, {{{{ state.sw.value }}}}, soil {{{{ state.soil.json.moisture }}}}, light {{{{ state.light.ok }}}}"
"""


def test_a_tree_over_every_field_protocol_runs_and_records(tmp_path, plc, bms, agent, garden, controller):
    text = FIELD_TREE.format(modbus=plc.url, bacnet=f"bacnet://127.0.0.1:{bms.port}", snmp=f"snmp://127.0.0.1:{agent.port}", coap=f"coap://127.0.0.1:{garden.port}", matter=controller.url)
    reg = registry(tmp_path, plant=text)
    from pyrtan.transport import RecordingTransport

    rec = RecordingTransport(None, LocalAdapters())  # type: ignore[arg-type]
    out = Runner(reg, rec).run_tree("plant", {})
    assert out.status == "done", out.say
    assert out.say == "level 48.5, zone 21.5, core-switch, soil 22.5, light True"
    steps = [s.summary for s in out.trace.steps]
    assert steps[0] == f"modbus input 100 on unit 1 at {plc.url} -> 48.5"
    assert steps[1] == f"bacnet analog-input:3 present-value at bacnet://127.0.0.1:{bms.port} -> 21.5"
    assert steps[2] == f'snmp sysName.0 at snmp://127.0.0.1:{agent.port} -> "core-switch"'
    assert steps[3] == f"coap GET coap://127.0.0.1:{garden.port}/sensors/bed-1/moisture -> 2.05"
    assert steps[4] == f"matter node 4 OnOff On at {controller.url} -> sent"
    # Replayed from what was recorded, with nothing listening.
    replay = Runner(reg, transport=RecordedTransport(json.loads(json.dumps(rec.recorded)))).run_tree("plant", {})
    assert replay.say == out.say
    assert set(rec.recorded) == {
        f"modbus {plc.url} unit 1 input 100", f"bacnet bacnet://127.0.0.1:{bms.port} analog-input:3 present-value",
        f"snmp snmp://127.0.0.1:{agent.port} sysName.0", f"coap GET coap://127.0.0.1:{garden.port}/sensors/bed-1/moisture",
        f"matter command {controller.url} node 4 1/OnOff On",
    }
    # A hosted service reaches public addresses only: a device on the box's network is refused.
    with pytest.raises(EgressRefused):
        LocalAdapters(egress="public").perform("modbus", {"url": plc.url, "read": "holding", "address": 0}, 1.0)
    for source, spec in (("bacnet", {"url": "bacnet://127.0.0.1", "object": "ai:1"}), ("snmp", {"url": "snmp://127.0.0.1", "get": "sysName.0"}),
                         ("coap", {"url": "coap://127.0.0.1/x"}), ("matter", {"url": "ws://127.0.0.1/ws", "node": 1, "read": "1/6/0"})):
        with pytest.raises(EgressRefused):
            LocalAdapters(egress="public").perform(source, spec, 1.0)


HOSTED = """
name = "zone"
description = "a zone's temperature"
hosts = ["bms.local"]
start = "read"

[[triggers]]
pattern = "temperature at {{host:word}}"

[inputs.host]
type = "str"

[nodes.read]
kind = "observe"
bacnet = {{ url = "bacnet://{{{{ inputs.host }}}}", object = "ai:1" }}
into = "t"
next = "done"

[nodes.done]
kind = "done"
say = "{{{{ state.t.value }}}}"
"""


def test_hosts_hold_for_a_templated_device(tmp_path):
    reg = registry(tmp_path, zone=HOSTED.format())
    fixture = RecordedTransport({"bacnet bacnet://bms.local ai:1 present-value": {"ok": True, "value": 20.5, "refused": None}})
    runner = Runner(reg, transport=fixture)
    assert runner.run_intent("temperature at bms.local").say == "20.5"
    out = runner.run_intent("temperature at 10.0.0.9")
    assert out.status == "error" and out.reason == "host-not-allowed" and "10.0.0.9 is outside this tree's hosts" in out.say


def test_checker_holds_each_field_source_to_its_shape():
    head = 'name = "t"\ndescription = "t"\nhosts = ["plc.local"]\nstart = "a"\n[nodes.b]\nkind = "done"\nsay = "x"\n[nodes.a]\nnext = "b"\n'
    assert problems(head + 'kind = "observe"\nmodbus = { url = "modbus://plc.local", read = "holding", address = 0 }') == []
    assert any("not in this tree's hosts" in p for p in problems(head + 'kind = "observe"\nmodbus = { url = "modbus://other.local", read = "holding", address = 0 }'))
    assert any("make this an act node" in p for p in problems(head + 'kind = "observe"\nmodbus = { url = "modbus://plc.local", write = "coil", address = 0, value = true }'))
    assert any("read-only" in p for p in problems(head + 'kind = "act"\nmodbus = { url = "modbus://plc.local", write = "input", address = 0, value = 1 }'))
    assert any("is not modbus://" in p for p in problems(head + 'kind = "observe"\nmodbus = { url = "tcp://plc.local", read = "holding", address = 0 }'))
    assert any("not a BACnet object type" in p for p in problems(head + 'kind = "observe"\nbacnet = { url = "bacnet://plc.local", object = "pump:1" }'))
    assert any("'priority' is 1 to 16" in p for p in problems(head + 'kind = "act"\nbacnet = { url = "bacnet://plc.local", object = "av:1", write = 1, priority = 17 }'))
    assert any("not an OID" in p for p in problems(head + 'kind = "observe"\nsnmp = { url = "snmp://plc.local", get = "nope.0" }'))
    assert any("exactly one of 'get', 'walk' or 'set'" in p for p in problems(head + 'kind = "observe"\nsnmp = { url = "snmp://plc.local" }'))
    assert any("PUT changes things" in p for p in problems(head + 'kind = "observe"\ncoap = { url = "coap://plc.local/x", method = "PUT" }'))
    assert any("DTLS" in p for p in problems(head + 'kind = "observe"\ncoap = { url = "coaps://plc.local/x" }'))
    assert any("not an attribute" in p for p in problems(head + 'kind = "observe"\nmatter = { url = "ws://plc.local/ws", node = 1, read = "1/OnOff/Nope" }'))
    assert any("a command changes things" in p for p in problems(head + 'kind = "observe"\nmatter = { url = "ws://plc.local/ws", node = 1, cluster = "OnOff", command = "On" }'))
    no_hosts = head.replace('hosts = ["plc.local"]\n', "")
    assert any("must list the hosts" in p for p in problems(no_hosts + 'kind = "observe"\ncoap = { url = "coap://plc.local/x" }'))


def test_words_for_traces_and_confirmations():
    spec = {"url": "modbus://plc.local", "unit": 2, "write": "holding", "address": 10, "value": 215}
    assert adapter_key("modbus", spec) == "modbus write modbus://plc.local unit 2 holding 10"
    assert would("modbus", spec) == "write 215 to holding 10 on unit 2 at modbus://plc.local"
    assert adapter_key("modbus", {"url": "modbus://p", "read": "coil", "address": 0, "count": 8}) == "modbus modbus://p unit 1 coil 0 x8"
    assert would("bacnet", {"url": "bacnet://b", "object": "av:1", "write": 21.5, "priority": 8}) == "write 21.5 to av:1 present-value at bacnet://b at priority 8"
    assert would("snmp", {"url": "snmp://s", "set": {"oid": "sysLocation.0", "value": "rack 4"}}) == 'set sysLocation.0 to "rack 4" at snmp://s'
    assert would("coap", {"url": "coap://g/v/1", "method": "put", "payload": "open"}) == 'PUT coap://g/v/1 with "open"'
    assert would("matter", {"url": "ws://m/ws", "node": 4, "cluster": "LevelControl", "command": "MoveToLevel", "payload": {"level": 128}}) == 'send MoveToLevel to node 4 (LevelControl) at ws://m/ws with {"level": 128}'
    assert what("snmp", {"url": "snmp://s", "walk": "ifDescr"}) == "walk ifDescr at snmp://s"
    assert brief_result("snmp", {"ok": True, "status": "noError", "values": {"1": "a", "2": "b"}}) == "2 values"
    assert brief_result("bacnet", {"ok": False, "refused": {"kind": "error", "code": "unknown-object"}}) == "unknown-object"
    assert brief_result("modbus", {"ok": False, "exception": {"code": 2, "name": "illegal-data-address"}}) == "exception illegal-data-address"
    assert brief_result("matter", {"ok": False, "refused": {"code": 5}}) == "refused (5)"
