"""SNMP v1 and v2c: get a few values, walk a table, or set one value.

    [nodes.ups]    observe  snmp = { url = "snmp://ups.local", get = ["upsBatteryStatus.0", "upsEstimatedChargeRemaining.0"] }
    [nodes.ports]  observe  snmp = { url = "snmp://switch.local", walk = "ifOperStatus", max = 48 }
    [nodes.name]   act      snmp = { url = "snmp://switch.local", set = { oid = "sysLocation.0", value = "rack 4" } }

An OID is dotted numbers (``1.3.6.1.2.1.1.5.0``) or one of the names in
:data:`NAMES` with its index (``sysName.0``). ``community`` is ``public``
unless the node says otherwise (``{{ env.SNMP_COMMUNITY }}`` keeps it out
of the tree), ``version`` is ``2c`` unless it is ``1``.

What it leaves in state: ``url``, ``ok``, ``status`` (the agent's
error-status by name: ``noError``, ``noSuchName``, ``notWritable``, …), ``values`` (for a
``get``, keyed by the OID as the node wrote it; for a ``walk``, by the
index under the walked OID: ``"1"``, ``"2"``, …), ``value`` (the first),
``varbinds`` (``{oid, type, value}`` in order, numeric OIDs), ``body``
and ``json`` (the values). An OctetString that is text reads as text,
otherwise as ``aa:bb:cc`` hex; TimeTicks are hundredths of a second;
``noSuchObject`` and ``noSuchInstance`` read as None and make ``ok``
false. After a set, ``written``.

An agent that is asked with the wrong community says nothing at all, so
that reads as no answer within the timeout. SNMPv3 (users, auth and
privacy) is not here: it needs ciphers the standard library does not
carry.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.parse
from typing import Any

from .transport import TransportError, check_egress

PORT = 161

NAMES = {
    "sysDescr": "1.3.6.1.2.1.1.1", "sysObjectID": "1.3.6.1.2.1.1.2", "sysUpTime": "1.3.6.1.2.1.1.3",
    "sysContact": "1.3.6.1.2.1.1.4", "sysName": "1.3.6.1.2.1.1.5", "sysLocation": "1.3.6.1.2.1.1.6",
    "sysServices": "1.3.6.1.2.1.1.7", "ifNumber": "1.3.6.1.2.1.2.1",
    "ifIndex": "1.3.6.1.2.1.2.2.1.1", "ifDescr": "1.3.6.1.2.1.2.2.1.2", "ifType": "1.3.6.1.2.1.2.2.1.3",
    "ifMtu": "1.3.6.1.2.1.2.2.1.4", "ifSpeed": "1.3.6.1.2.1.2.2.1.5", "ifPhysAddress": "1.3.6.1.2.1.2.2.1.6",
    "ifAdminStatus": "1.3.6.1.2.1.2.2.1.7", "ifOperStatus": "1.3.6.1.2.1.2.2.1.8", "ifLastChange": "1.3.6.1.2.1.2.2.1.9",
    "ifInOctets": "1.3.6.1.2.1.2.2.1.10", "ifInErrors": "1.3.6.1.2.1.2.2.1.14", "ifOutOctets": "1.3.6.1.2.1.2.2.1.16",
    "ifOutErrors": "1.3.6.1.2.1.2.2.1.20", "ifName": "1.3.6.1.2.1.31.1.1.1.1", "ifHCInOctets": "1.3.6.1.2.1.31.1.1.1.6",
    "ifHCOutOctets": "1.3.6.1.2.1.31.1.1.1.10", "ifHighSpeed": "1.3.6.1.2.1.31.1.1.1.15", "ifAlias": "1.3.6.1.2.1.31.1.1.1.18",
    "hrSystemUptime": "1.3.6.1.2.1.25.1.1", "hrStorageDescr": "1.3.6.1.2.1.25.2.3.1.3",
    "hrStorageAllocationUnits": "1.3.6.1.2.1.25.2.3.1.4", "hrStorageSize": "1.3.6.1.2.1.25.2.3.1.5",
    "hrStorageUsed": "1.3.6.1.2.1.25.2.3.1.6", "hrProcessorLoad": "1.3.6.1.2.1.25.3.3.1.2",
    "upsBatteryStatus": "1.3.6.1.2.1.33.1.2.1", "upsSecondsOnBattery": "1.3.6.1.2.1.33.1.2.2",
    "upsEstimatedMinutesRemaining": "1.3.6.1.2.1.33.1.2.3", "upsEstimatedChargeRemaining": "1.3.6.1.2.1.33.1.2.4",
    "upsOutputSource": "1.3.6.1.2.1.33.1.4.1", "upsOutputPercentLoad": "1.3.6.1.2.1.33.1.4.4.1.5",
    "prtMarkerSuppliesDescription": "1.3.6.1.2.1.43.11.1.1.6", "prtMarkerSuppliesMaxCapacity": "1.3.6.1.2.1.43.11.1.1.8",
    "prtMarkerSuppliesLevel": "1.3.6.1.2.1.43.11.1.1.9",
}
ERRORS = {
    1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess", 7: "wrongType",
    8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation", 12: "inconsistentValue",
    13: "resourceUnavailable", 14: "commitFailed", 15: "undoFailed", 16: "authorizationError", 17: "notWritable",
    18: "inconsistentName",
}
SET_TYPES = {"integer": 0x02, "string": 0x04, "oid": 0x06, "ipaddress": 0x40, "counter": 0x41, "gauge": 0x42, "unsigned": 0x42, "timeticks": 0x43}
TYPE_NAMES = {0x02: "integer", 0x04: "string", 0x05: "null", 0x06: "oid", 0x40: "ipaddress", 0x41: "counter32", 0x42: "gauge32", 0x43: "timeticks", 0x44: "opaque", 0x46: "counter64", 0x80: "noSuchObject", 0x81: "noSuchInstance", 0x82: "endOfMibView"}
GET, GETNEXT, RESPONSE, SET, GETBULK = 0xA0, 0xA1, 0xA2, 0xA3, 0xA5
MAX_WALK = 1000


def endpoint(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of ``snmp://host[:port]``, or a :class:`ValueError`."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme != "snmp" or not parsed.hostname:
        raise ValueError(f"{url!r} is not snmp://host[:port]")
    return parsed.scheme, parsed.hostname, parsed.port or PORT


def mode_of(spec: dict[str, Any]) -> str:
    return next((m for m in ("set", "walk", "get") if m in spec), "get")


def oid(text: Any) -> str:
    """Numeric dotted form of an OID or a known name (``sysName.0``), or a :class:`ValueError`."""
    s = str(text).strip().lstrip(".")
    head, _, rest = s.partition(".")
    if head in NAMES:
        s = NAMES[head] + ("." + rest if rest else "")
    parts = s.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"{text!r} is not an OID (1.3.6.1.2.1.1.5.0) or a name this knows (sysName.0)")
    return s


# ---------------------------------------------------------------- BER


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, data: bytes) -> bytes:
    return bytes([tag]) + _len(len(data)) + data


def _int(n: int, tag: int = 0x02) -> bytes:
    n = int(n)
    size = max(1, (n.bit_length() + 8) // 8)
    if tag != 0x02 and n >= 0:
        b = n.to_bytes(size, "big", signed=False)
    else:
        b = n.to_bytes(size, "big", signed=True)
    return _tlv(tag, b)


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, out)


def _set_value(value: Any, kind: Any) -> bytes:
    kind = str(kind) if kind is not None else ("integer" if isinstance(value, int) and not isinstance(value, bool) else "string")
    if kind not in SET_TYPES:
        raise TransportError(f"snmp: type {kind!r} is not one of {', '.join(SET_TYPES)}")
    tag = SET_TYPES[kind]
    if tag == 0x04:
        return _tlv(0x04, str(value).encode("utf-8"))
    if tag == 0x06:
        return _oid(oid(value))
    if tag == 0x40:
        return _tlv(0x40, socket.inet_aton(str(value)))
    return _int(int(value), tag)


def message(version: int, community: str, pdu: int, request_id: int, binds: list[tuple[str, bytes]], a: int = 0, b: int = 0) -> bytes:
    vbs = b"".join(_tlv(0x30, _oid(o) + v) for o, v in binds)
    body = _int(request_id) + _int(a) + _int(b) + _tlv(0x30, vbs)
    return _tlv(0x30, _int(version) + _tlv(0x04, community.encode("utf-8")) + _tlv(pdu, body))


def _read(data: bytes, i: int) -> tuple[int, bytes, int]:
    tag = data[i]
    n = data[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(data[i:i + k], "big")
        i += k
    return tag, data[i:i + n], i + n


def _decode_oid(raw: bytes) -> str:
    parts = [raw[0] // 40, raw[0] % 40] if raw else []
    n = 0
    for byte in raw[1:]:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(n)
            n = 0
    return ".".join(str(p) for p in parts)


def _text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
        if all(c.isprintable() or c in "\r\n\t" for c in text.rstrip("\x00")):
            return text.rstrip("\x00")
    except UnicodeDecodeError:
        pass
    return ":".join(f"{b:02x}" for b in raw)


def _value(tag: int, raw: bytes) -> Any:
    if tag == 0x02:
        return int.from_bytes(raw, "big", signed=True)
    if tag in (0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(raw, "big")
    if tag == 0x04:
        return _text(raw)
    if tag == 0x06:
        return _decode_oid(raw)
    if tag == 0x40:
        return ".".join(str(b) for b in raw)
    if tag == 0x44:
        return raw.hex()
    return None


def response(data: bytes) -> tuple[int, int, int, list[dict[str, Any]]]:
    """``(request id, error status, error index, varbinds)`` of a GetResponse."""
    _, msg, _ = _read(data, 0)
    _, _, i = _read(msg, 0)          # version
    _, _, i = _read(msg, i)          # community
    tag, pdu, _ = _read(msg, i)
    if tag != RESPONSE:
        raise ValueError("not a response")
    _, rid, j = _read(pdu, 0)
    _, status, j = _read(pdu, j)
    _, index, j = _read(pdu, j)
    _, vbs, _ = _read(pdu, j)
    binds = []
    k = 0
    while k < len(vbs):
        _, vb, k = _read(vbs, k)
        _, o, m = _read(vb, 0)
        vtag, vraw, _ = _read(vb, m)
        binds.append({"oid": _decode_oid(o), "type": TYPE_NAMES.get(vtag, f"0x{vtag:02x}"), "value": _value(vtag, vraw)})
    return int.from_bytes(rid, "big", signed=True), int.from_bytes(status, "big"), int.from_bytes(index, "big"), binds


# ---------------------------------------------------------------- the exchange


class _Agent:
    def __init__(self, url: str, timeout: float, egress: str) -> None:
        try:
            _, host, port = endpoint(url)
        except ValueError as e:
            raise TransportError(str(e)) from None
        check_egress(f"http://{host}:{port}/", egress)
        self.url = url
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        try:
            self.addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
        except OSError as e:
            raise TransportError(f"{url}: {e.strerror or e}") from e
        self.sock = socket.socket(self.addr[0], socket.SOCK_DGRAM)

    def ask(self, build) -> tuple[int, int, list[dict[str, Any]]]:
        rid = int.from_bytes(os.urandom(3), "big")
        packet = build(rid)
        resend = 0.0
        while True:
            now = time.monotonic()
            if now >= self.deadline:
                raise TransportError(f"{self.url}: no answer within {self.timeout:g} s (an agent asked with the wrong community says nothing)")
            if now >= resend:
                self.sock.sendto(packet, self.addr[4])
                resend = now + max(0.5, min(2.0, self.timeout / 3))
            self.sock.settimeout(max(0.01, min(resend, self.deadline) - now))
            try:
                data, _ = self.sock.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as e:
                raise TransportError(f"{self.url}: {e.strerror or e}") from e
            try:
                got_id, status, index, binds = response(data)
            except (ValueError, IndexError):
                continue
            if got_id == rid:
                return status, index, binds

    def close(self) -> None:
        self.sock.close()


def perform(spec: dict[str, Any], timeout: float, egress: str = "any") -> dict[str, Any]:
    url = str(spec.get("url", ""))
    community = str(spec.get("community", "public"))
    version = 0 if str(spec.get("version", "2c")) == "1" else 1
    mode = mode_of(spec)
    out: dict[str, Any] = {"url": url}
    try:
        if mode == "get":
            asked = spec.get("get")
            asked = asked if isinstance(asked, list) else [asked]
            oids = [oid(a) for a in asked]
        elif mode == "walk":
            base = oid(spec.get("walk"))
        else:
            target = spec.get("set") or {}
            if not isinstance(target, dict) or "oid" not in target or "value" not in target:
                raise ValueError("'set' is a table with 'oid', 'value' and optionally 'type'")
            set_oid = oid(target["oid"])
    except ValueError as e:
        raise TransportError(f"snmp: {e}") from None
    agent = _Agent(url, timeout, egress)
    try:
        if mode == "set":
            status, index, binds = agent.ask(lambda rid: message(version, community, SET, rid, [(set_oid, _set_value(target["value"], target.get("type")))]))
            error = ERRORS.get(status, str(status)) if status else None
            return {**out, "ok": error is None, "status": error or "noError", "written": error is None, "values": {str(target["oid"]): target["value"]}, "value": target["value"], "varbinds": binds, "body": json.dumps(target["value"]), "json": target["value"]}
        if mode == "get":
            status, index, binds = agent.ask(lambda rid: message(version, community, GET, rid, [(o, b"\x05\x00") for o in oids]))
            error = ERRORS.get(status, str(status)) if status else None
            values = {str(a): (binds[k]["value"] if k < len(binds) and not error else None) for k, a in enumerate(asked)}
            missing = any(b["type"] in ("noSuchObject", "noSuchInstance", "endOfMibView") for b in binds)
            first = next(iter(values.values()), None)
            return {**out, "ok": error is None and not missing, "status": error or "noError", "values": values, "value": first, "varbinds": binds, "body": json.dumps(values, ensure_ascii=False), "json": values}
        limit = min(MAX_WALK, int(spec.get("max", 100)))
        rows: list[dict[str, Any]] = []
        current = base
        error = None
        while len(rows) < limit:
            if version == 1:
                at = current
                status, index, binds = agent.ask(lambda rid: message(version, community, GETBULK, rid, [(at, b"\x05\x00")], 0, min(25, limit - len(rows))))
            else:
                at = current
                status, index, binds = agent.ask(lambda rid: message(version, community, GETNEXT, rid, [(at, b"\x05\x00")]))
            if status:
                if status != 2:  # noSuchName is how v1 says the walk is over
                    error = ERRORS.get(status, str(status))
                break
            done = False
            for b in binds:
                if b["type"] == "endOfMibView" or not b["oid"].startswith(base + "."):
                    done = True
                    break
                rows.append(b)
                if len(rows) >= limit:
                    break
            if done or not binds:
                break
            current = binds[-1]["oid"]
        values = {b["oid"][len(base) + 1:]: b["value"] for b in rows}
        first = rows[0]["value"] if rows else None
        return {**out, "ok": error is None, "status": error or "noError", "values": values, "value": first, "varbinds": rows, "body": json.dumps(values, ensure_ascii=False), "json": values}
    finally:
        agent.close()
