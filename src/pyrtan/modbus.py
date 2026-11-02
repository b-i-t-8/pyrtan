"""Modbus TCP: a device's coils and registers, read or written.

    [nodes.level]  observe  modbus = { url = "modbus://plc.local", unit = 1, read = "holding", address = 100, type = "float32" }
    [nodes.pump]   act      modbus = { url = "modbus://plc.local", unit = 1, write = "coil", address = 3, value = true }

A node reads one of the four tables (``coil``, ``discrete``, ``holding``,
``input``) from ``address``, ``count`` values long, or writes ``value``
(one value or a list) to a coil or holding register. Registers are 16-bit
words; ``type`` reads or writes them as ``uint16`` (the default),
``int16``, ``uint32``, ``int32``, ``float32``, ``uint64``, ``int64`` or
``float64``, most significant word first unless ``word_order = "little"``.
``scale`` multiplies what is read (``0.1`` turns 215 into 21.5) and
divides what is written.

What it leaves in state: ``url``, ``unit``, ``table``, ``address``,
``count``, ``ok``, ``raw`` (the words or bits as they came), ``values``,
``value`` (the first), ``body`` and ``json`` (the values), and
``exception`` (``{code, name}``) when the device answered with a Modbus
exception, which is an answer, not a transport error: a tree decides what
``illegal-data-address`` means. After a write, ``written``: how many
coils or registers.

One request per node, on a fresh connection, the standard library's
sockets: function codes 1-4 to read, 5, 6, 15 and 16 to write. Modbus
RTU over a serial line, and Modbus over TLS, are not here.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
import urllib.parse
from typing import Any

from .transport import TransportError, check_egress

PORT = 502
TABLES = ("coil", "discrete", "holding", "input")
WRITABLE = ("coil", "holding")
READ_CODES = {"coil": 1, "discrete": 2, "holding": 3, "input": 4}
TYPES: dict[str, tuple[int, str]] = {
    "uint16": (1, "H"), "int16": (1, "h"), "uint32": (2, "I"), "int32": (2, "i"), "float32": (2, "f"),
    "uint64": (4, "Q"), "int64": (4, "q"), "float64": (4, "d"),
}
MAX_BITS = 2000
MAX_WORDS = 125
EXCEPTIONS = {
    1: "illegal-function", 2: "illegal-data-address", 3: "illegal-data-value", 4: "server-device-failure",
    5: "acknowledge", 6: "server-device-busy", 8: "memory-parity-error", 10: "gateway-path-unavailable",
    11: "gateway-target-failed-to-respond",
}


def endpoint(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of ``modbus://host[:port]``, or a :class:`ValueError`."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme != "modbus" or not parsed.hostname:
        raise ValueError(f"{url!r} is not modbus://host[:port]")
    return parsed.scheme, parsed.hostname, parsed.port or PORT


def table_of(spec: dict[str, Any]) -> tuple[str, str]:
    """``("read" | "write", table)``."""
    if "write" in spec:
        return "write", str(spec["write"])
    return "read", str(spec.get("read", "holding"))


def _type(spec: dict[str, Any], table: str, value: Any = None) -> str:
    if table in ("coil", "discrete"):
        return "bool"
    t = spec.get("type")
    if t is not None:
        t = str(t)
        if t not in TYPES:
            raise TransportError(f"modbus: type {t!r} is not one of {', '.join(TYPES)}")
        return t
    if value is not None:
        values = value if isinstance(value, list) else [value]
        if any(isinstance(v, float) and not float(v).is_integer() for v in values) and not spec.get("scale"):
            return "float32"
        if any(isinstance(v, (int, float)) and v < 0 for v in values):
            return "int16"
    return "uint16"


def _words(value: Any, typ: str, little: bool) -> list[int]:
    n, code = TYPES[typ]
    if code in "fd":
        packed = struct.pack(">" + code, float(value))
    else:
        packed = struct.pack(">" + code, int(round(value)))
    words = [struct.unpack(">H", packed[i:i + 2])[0] for i in range(0, 2 * n, 2)]
    return words[::-1] if little else words


def _value(words: list[int], typ: str, little: bool) -> Any:
    n, code = TYPES[typ]
    words = words[::-1] if little else words
    v = struct.unpack(">" + code, b"".join(struct.pack(">H", w) for w in words))[0]
    if code == "f":
        v = float(f"{v:.7g}")
    return v


def _scaled(v: Any, scale: Any) -> Any:
    if scale in (None, 1) or isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    out = v * float(scale)
    out = float(f"{out:.10g}")
    return int(out) if out.is_integer() and isinstance(scale, int) else out


def _recv(sock: socket.socket, n: int, deadline: float, url: str) -> bytes:
    out = b""
    while len(out) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError(f"{url}: no answer within the timeout")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(n - len(out))
        except (socket.timeout, TimeoutError):
            raise TransportError(f"{url}: no answer within the timeout") from None
        if not chunk:
            raise TransportError(f"{url}: the device closed the connection")
        out += chunk
    return out


def exchange(url: str, unit: int, pdu: bytes, timeout: float, egress: str = "any") -> bytes:
    """Send one PDU to ``unit`` at ``url``; the PDU that came back."""
    try:
        _, host, port = endpoint(url)
    except ValueError as e:
        raise TransportError(str(e)) from None
    check_egress(f"http://{host}:{port}/", egress)
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    tid = int.from_bytes(os.urandom(2), "big")
    try:
        sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
        while True:
            head = _recv(sock, 7, deadline, url)
            rtid, proto, length, _unit = struct.unpack(">HHHB", head)
            if proto != 0 or length < 2:
                raise TransportError(f"{url}: not a Modbus TCP answer")
            body = _recv(sock, length - 1, deadline, url)
            if rtid == tid:
                return body
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    finally:
        try:
            sock.close()
        except OSError:
            pass


def perform(spec: dict[str, Any], timeout: float, egress: str = "any") -> dict[str, Any]:
    url = str(spec.get("url", ""))
    unit = int(spec.get("unit", 1))
    mode, table = table_of(spec)
    if table not in TABLES:
        raise TransportError(f"modbus: {table!r} is not one of {', '.join(TABLES)}")
    address = int(spec.get("address", 0))
    little = str(spec.get("word_order", "big")) == "little"
    scale = spec.get("scale")
    out: dict[str, Any] = {"url": url, "unit": unit, "table": table, "address": address}

    if mode == "write":
        if table not in WRITABLE:
            raise TransportError(f"modbus: {table} is read-only; write to a coil or a holding register")
        value = spec.get("value")
        if value is None:
            raise TransportError("modbus: a write needs 'value'")
        values = value if isinstance(value, list) else [value]
        typ = _type(spec, table, value)
        if table == "coil":
            bits = [bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "on", "yes") for v in values]
            if len(bits) == 1:
                pdu = struct.pack(">BHH", 5, address, 0xFF00 if bits[0] else 0)
            else:
                packed = bytearray((len(bits) + 7) // 8)
                for i, b in enumerate(bits):
                    if b:
                        packed[i // 8] |= 1 << (i % 8)
                pdu = struct.pack(">BHHB", 15, address, len(bits), len(packed)) + bytes(packed)
            raw, count = [int(b) for b in bits], len(bits)
        else:
            raws = [v / float(scale) if scale not in (None, 1) else v for v in values]
            words = [w for v in raws for w in _words(v, typ, little)]
            if len(words) > MAX_WORDS:
                raise TransportError(f"modbus: {len(words)} registers is more than one write carries ({MAX_WORDS})")
            if len(words) == 1:
                pdu = struct.pack(">BHH", 6, address, words[0])
            else:
                pdu = struct.pack(">BHHB", 16, address, len(words), 2 * len(words)) + b"".join(struct.pack(">H", w) for w in words)
            raw, count = words, len(words)
        answer = exchange(url, unit, pdu, timeout, egress)
        if answer[0] & 0x80:
            code = answer[1] if len(answer) > 1 else 0
            return {**out, "count": len(values), "ok": False, "written": 0, "raw": raw, "values": values, "value": values[0], "exception": {"code": code, "name": EXCEPTIONS.get(code, f"exception-{code}")}, "body": json.dumps(values), "json": values}
        return {**out, "count": len(values), "ok": True, "written": count, "raw": raw, "values": values, "value": values[0], "exception": None, "body": json.dumps(values), "json": values}

    count = int(spec.get("count", 1))
    if count < 1:
        raise TransportError("modbus: 'count' is at least 1")
    typ = _type(spec, table)
    per = 1 if typ == "bool" else TYPES[typ][0]
    quantity = count * per
    if quantity > (MAX_BITS if typ == "bool" else MAX_WORDS):
        raise TransportError(f"modbus: {quantity} {'bits' if typ == 'bool' else 'registers'} is more than one read carries")
    answer = exchange(url, unit, struct.pack(">BHH", READ_CODES[table], address, quantity), timeout, egress)
    if answer[0] & 0x80:
        code = answer[1] if len(answer) > 1 else 0
        return {**out, "count": count, "ok": False, "raw": [], "values": [], "value": None, "exception": {"code": code, "name": EXCEPTIONS.get(code, f"exception-{code}")}, "body": "[]", "json": []}
    data = answer[2:2 + answer[1]] if len(answer) > 1 else b""
    if typ == "bool":
        raw = [(data[i // 8] >> (i % 8)) & 1 if i // 8 < len(data) else 0 for i in range(quantity)]
        values: list[Any] = [bool(b) for b in raw]
    else:
        if len(data) < 2 * quantity:
            raise TransportError(f"{url}: asked for {quantity} registers, {len(data) // 2} came back")
        raw = [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, 2 * quantity, 2)]
        values = [_scaled(_value(raw[i:i + per], typ, little), scale) for i in range(0, quantity, per)]
    return {**out, "count": count, "ok": True, "raw": raw, "values": values, "value": values[0], "exception": None, "body": json.dumps(values), "json": values}
