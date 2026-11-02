"""BACnet/IP: one property of one object, read or written.

    [nodes.zone]   observe  bacnet = { url = "bacnet://bms.local", object = "analog-input:3", property = "present-value" }
    [nodes.setp]   act      bacnet = { url = "bacnet://bms.local", object = "analog-value:12", write = 21.5, priority = 8 }

``object`` is ``type:instance``: the type by its name (``analog-input``,
``binary-output``, ``device``, …), a short form (``ai``, ``bo``, ``msv``)
or its number. ``property`` (``present-value`` when left out) is a name or
a number; ``index`` reads one element of an array (``priority-array``,
``object-list``). A write sends ``write`` at ``priority`` (1-16, optional)
encoded as the object's type expects (real for analog, enumerated for
binary, unsigned for multi-state) unless ``type`` names the application
tag (``real``, ``unsigned``, ``signed``, ``enumerated``, ``boolean``,
``string``, ``double``, ``null``); ``write = "null"`` with ``type = "null"``
relinquishes a priority.

``network`` and ``mac`` reach a device behind a BACnet router (an MS/TP
trunk): the remote network number and the device's address on it.

What it leaves in state: ``url``, ``object``, ``property``, ``ok``,
``value`` (one value, or a list when the property is a list), ``values``,
``body`` and ``json``, and ``refused`` when the device answered with a
BACnet Error, Reject or Abort (``{kind, class, code}``), which is an
answer a tree decides on (``unknown-object`` is "no such zone"), not a
transport error. After a write, ``written``.

ReadProperty and WriteProperty, confirmed, over UDP from an ephemeral
port, resent until the timeout; no segmentation (a property too large for
one packet comes back as ``segmentation-not-supported``: read it by
``index``), no Who-Is, no COV. The standard library only.
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

PORT = 47808  # 0xBAC0

OBJECT_TYPES = {
    "analog-input": 0, "analog-output": 1, "analog-value": 2, "binary-input": 3, "binary-output": 4,
    "binary-value": 5, "calendar": 6, "command": 7, "device": 8, "event-enrollment": 9, "file": 10,
    "group": 11, "loop": 12, "multi-state-input": 13, "multi-state-output": 14, "notification-class": 15,
    "program": 16, "schedule": 17, "averaging": 18, "multi-state-value": 19, "trend-log": 20,
    "life-safety-point": 21, "life-safety-zone": 22, "accumulator": 23, "pulse-converter": 24,
    "event-log": 25, "global-group": 26, "trend-log-multiple": 27, "load-control": 28,
    "structured-view": 29, "access-door": 30, "character-string-value": 40, "integer-value": 45,
    "large-analog-value": 46, "positive-integer-value": 48,
}
SHORT_TYPES = {"ai": 0, "ao": 1, "av": 2, "bi": 3, "bo": 4, "bv": 5, "dev": 8, "msi": 13, "mso": 14, "msv": 19, "tl": 20, "iv": 45}
TYPE_NAMES = {v: k for k, v in OBJECT_TYPES.items()}

PROPERTIES = {
    "active-text": 4, "application-software-version": 12, "cov-increment": 22, "deadband": 25, "description": 28,
    "device-type": 31, "event-state": 36, "firmware-revision": 44, "high-limit": 45, "inactive-text": 46,
    "local-date": 56, "local-time": 57, "location": 58, "low-limit": 59, "max-apdu-length-accepted": 62,
    "max-pres-value": 65, "min-pres-value": 69, "model-name": 70, "number-of-states": 74, "object-identifier": 75,
    "object-list": 76, "object-name": 77, "object-type": 79, "out-of-service": 81, "polarity": 84,
    "present-value": 85, "priority-array": 87, "protocol-version": 98, "reliability": 103,
    "relinquish-default": 104, "resolution": 106, "setpoint": 108, "state-text": 110, "status-flags": 111,
    "system-status": 112, "units": 117, "vendor-identifier": 120, "vendor-name": 121,
    "protocol-revision": 139, "database-revision": 155,
}
APP_TAGS = {"null": 0, "boolean": 1, "unsigned": 2, "signed": 3, "real": 4, "double": 5, "string": 7, "enumerated": 9}

ERROR_CLASSES = {0: "device", 1: "object", 2: "property", 3: "resources", 4: "security", 5: "services", 6: "vt", 7: "communication"}
ERROR_CODES = {
    0: "other", 2: "configuration-in-progress", 3: "device-busy", 9: "invalid-data-type", 25: "operational-problem",
    27: "read-access-denied", 29: "service-request-denied", 30: "timeout", 31: "unknown-object",
    32: "unknown-property", 36: "unsupported-object-type", 37: "value-out-of-range", 40: "write-access-denied",
    42: "invalid-array-index",
}
REJECT_REASONS = {
    0: "other", 1: "buffer-overflow", 2: "inconsistent-parameters", 3: "invalid-parameter-data-type", 4: "invalid-tag",
    5: "missing-required-parameter", 6: "parameter-out-of-range", 7: "too-many-arguments", 8: "undefined-enumeration",
    9: "unrecognized-service",
}
ABORT_REASONS = {0: "other", 1: "buffer-overflow", 2: "invalid-apdu-in-this-state", 3: "preempted-by-higher-priority-task", 4: "segmentation-not-supported"}

READ_PROPERTY, WRITE_PROPERTY = 12, 15


def endpoint(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of ``bacnet://host[:port]``, or a :class:`ValueError`."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme != "bacnet" or not parsed.hostname:
        raise ValueError(f"{url!r} is not bacnet://host[:port]")
    return parsed.scheme, parsed.hostname, parsed.port or PORT


def object_id(text: Any) -> tuple[int, int]:
    """``(type, instance)`` of ``"analog-input:3"``, ``"ai:3"`` or ``"0:3"``, or a :class:`ValueError`."""
    kind, sep, inst = str(text).strip().replace(" ", ":").rpartition(":")
    kind = kind.strip().lower().replace("_", "-")
    if not sep or not inst.strip().isdigit():
        raise ValueError(f"{text!r} is not an object such as analog-input:3")
    if kind.isdigit():
        t = int(kind)
    elif kind in OBJECT_TYPES:
        t = OBJECT_TYPES[kind]
    elif kind in SHORT_TYPES:
        t = SHORT_TYPES[kind]
    else:
        raise ValueError(f"{kind!r} is not a BACnet object type this knows; give its number")
    i = int(inst)
    if not 0 <= t < 1024 or not 0 <= i < 4194303:
        raise ValueError(f"{text!r} is out of range")
    return t, i


def property_id(text: Any) -> int:
    if isinstance(text, int) and not isinstance(text, bool):
        return text
    name = str(text).strip().lower().replace("_", "-")
    if name.isdigit():
        return int(name)
    if name not in PROPERTIES:
        raise ValueError(f"{text!r} is not a BACnet property this knows; give its number")
    return PROPERTIES[name]


# ---------------------------------------------------------------- encoding


def _unsigned(n: int) -> bytes:
    n = int(n)
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


def _signed(n: int) -> bytes:
    n = int(n)
    for size in (1, 2, 3, 4):
        try:
            return n.to_bytes(size, "big", signed=True)
        except OverflowError:
            continue
    raise TransportError(f"bacnet: {n} does not fit a signed value")


def _tag(number: int, context: bool, data: bytes) -> bytes:
    n = len(data)
    head = (number << 4) | (0x08 if context else 0)
    if n <= 4:
        return bytes([head | n]) + data
    if n <= 253:
        return bytes([head | 5, n]) + data
    return bytes([head | 5, 254]) + struct.pack(">H", n) + data


def _application(value: Any, tag: str) -> bytes:
    if tag == "null":
        return b"\x00"
    if tag == "boolean":
        on = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "on", "active", "yes")
        return bytes([0x10 | (1 if on else 0)])
    if tag == "unsigned":
        return _tag(2, False, _unsigned(value))
    if tag == "signed":
        return _tag(3, False, _signed(value))
    if tag == "real":
        return _tag(4, False, struct.pack(">f", float(value)))
    if tag == "double":
        return _tag(5, False, struct.pack(">d", float(value)))
    if tag == "string":
        return _tag(7, False, b"\x00" + str(value).encode("utf-8"))
    if tag == "enumerated":
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, str) and not value.strip().isdigit():
            value = {"inactive": 0, "active": 1, "off": 0, "on": 1, "false": 0, "true": 1}.get(value.strip().lower())
            if value is None:
                raise TransportError("bacnet: an enumerated value is a number (or active / inactive)")
        return _tag(9, False, _unsigned(value))
    raise TransportError(f"bacnet: type {tag!r} is not one of {', '.join(APP_TAGS)}")


def _write_tag(obj_type: int, value: Any, given: Any) -> str:
    if given is not None:
        return str(given)
    if value is None:
        return "null"
    if obj_type in (0, 1, 2, 46):
        return "real"
    if obj_type in (3, 4, 5):
        return "enumerated"
    if obj_type in (13, 14, 19, 48):
        return "unsigned"
    if obj_type == 45:
        return "signed"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "unsigned" if value >= 0 else "signed"
    if isinstance(value, float):
        return "real"
    return "string"


def request(service: int, invoke: int, obj: tuple[int, int], prop: int, index: Any = None, value: bytes | None = None, priority: Any = None) -> bytes:
    """A confirmed ReadProperty or WriteProperty APDU."""
    body = _tag(0, True, struct.pack(">I", (obj[0] << 22) | obj[1])) + _tag(1, True, _unsigned(prop))
    if index is not None:
        body += _tag(2, True, _unsigned(index))
    if value is not None:
        body += b"\x3e" + value + b"\x3f"
        if priority is not None:
            body += _tag(4, True, _unsigned(priority))
    return bytes([0x00, 0x05, invoke, service]) + body


def frame(apdu: bytes, network: Any = None, mac: Any = None) -> bytes:
    """BVLC (original unicast) + NPDU (expecting a reply) around an APDU."""
    if network is not None:
        dadr = _mac(mac)
        npdu = bytes([0x01, 0x24]) + struct.pack(">HB", int(network), len(dadr)) + dadr + b"\xff"
    else:
        npdu = b"\x01\x04"
    data = npdu + apdu
    return struct.pack(">BBH", 0x81, 0x0A, len(data) + 4) + data


def _mac(mac: Any) -> bytes:
    if mac is None or mac == "":
        return b""
    if isinstance(mac, int):
        return _unsigned(mac)
    text = str(mac).strip().lower()
    if text.isdigit():
        return _unsigned(int(text))
    text = text.removeprefix("0x").replace(":", "").replace("-", "")
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise TransportError(f"bacnet: mac {mac!r} is a number or hex bytes") from None


# ---------------------------------------------------------------- decoding


def _read_tag(data: bytes, i: int) -> tuple[int, bool, int, int]:
    """``(number, context, length-or-value, offset of the content)``; length -1 opens, -2 closes."""
    b = data[i]
    i += 1
    number = b >> 4
    if number == 0x0F:
        number = data[i]
        i += 1
    context = bool(b & 0x08)
    lvt = b & 0x07
    if context and lvt == 6:
        return number, True, -1, i
    if context and lvt == 7:
        return number, True, -2, i
    if not context and number == 1:  # boolean: the value is in the tag
        return number, False, lvt, i
    if lvt == 5:
        lvt = data[i]
        i += 1
        if lvt == 254:
            lvt = struct.unpack(">H", data[i:i + 2])[0]
            i += 2
        elif lvt == 255:
            lvt = struct.unpack(">I", data[i:i + 4])[0]
            i += 4
    return number, context, lvt, i


def _app_value(tag: int, lvt: int, raw: bytes) -> Any:
    if tag == 0:
        return None
    if tag == 1:
        return bool(lvt)
    if tag in (2, 9):
        return int.from_bytes(raw, "big")
    if tag == 3:
        return int.from_bytes(raw, "big", signed=True)
    if tag == 4:
        return float(f"{struct.unpack('>f', raw)[0]:.7g}")
    if tag == 5:
        return struct.unpack(">d", raw)[0]
    if tag == 6:
        return raw.hex()
    if tag == 7:
        charset, text = (raw[0], raw[1:]) if raw else (0, b"")
        enc = {0: "utf-8", 4: "utf-16-be", 5: "latin-1"}.get(charset, "utf-8")
        return text.decode(enc, errors="replace")
    if tag == 8:
        unused, bits = (raw[0], raw[1:]) if raw else (0, b"")
        out = [bool(byte >> (7 - k) & 1) for byte in bits for k in range(8)]
        return out[:len(out) - unused] if unused else out
    if tag == 10:
        y, m, d, _ = raw
        return f"{'*' if y == 255 else y + 1900}-{'*' if m == 255 else f'{m:02d}'}-{'*' if d == 255 else f'{d:02d}'}"
    if tag == 11:
        h, m, s, hs = raw
        return ":".join("*" if x == 255 else f"{x:02d}" for x in (h, m, s)) + ("" if hs == 255 else f".{hs:02d}")
    if tag == 12:
        n = struct.unpack(">I", raw)[0]
        t, inst = n >> 22, n & 0x3FFFFF
        return f"{TYPE_NAMES.get(t, t)}:{inst}"
    return raw.hex()


def decode_values(data: bytes, i: int = 0, until_close: int | None = None) -> tuple[list[Any], int]:
    """Values from ``data[i:]`` up to the closing tag ``until_close``:
    application values as themselves, a context value as hex, a constructed
    one as a list."""
    out: list[Any] = []
    while i < len(data):
        number, context, lvt, i = _read_tag(data, i)
        if lvt == -2:
            if until_close is None or number == until_close:
                return out, i
            continue
        if lvt == -1:
            inner, i = decode_values(data, i, number)
            out.append(inner)
            continue
        raw = b"" if (not context and number == 1) else data[i:i + lvt]
        i += len(raw)
        out.append(raw.hex() if context else _app_value(number, lvt, raw))
    return out, i


def answer(data: bytes) -> tuple[int, int, bytes] | None:
    """``(apdu type, invoke id, apdu)`` of a BACnet/IP packet, or None for one that is not an answer."""
    if len(data) < 6 or data[0] != 0x81:
        return None
    i = 4 + (6 if data[1] == 0x04 else 0)  # Forwarded-NPDU carries the original address
    if data[1] not in (0x0A, 0x0B, 0x04) or len(data) < i + 2:
        return None
    control = data[i + 1]
    i += 2
    if control & 0x80:
        return None  # a network-layer message
    if control & 0x20:
        i += 3 + data[i + 2]
    if control & 0x08:
        i += 3 + data[i + 2]
    if control & 0x20:
        i += 1
    apdu = data[i:]
    if len(apdu) < 2:
        return None
    kind = apdu[0] >> 4
    if kind not in (2, 3, 5, 6, 7):
        return None
    return kind, apdu[1], apdu


def _refusal(kind: int, apdu: bytes) -> dict[str, Any]:
    if kind == 5:
        values, _ = decode_values(apdu, 3)
        cls, code = (values + [None, None])[:2]
        return {"kind": "error", "class": ERROR_CLASSES.get(cls, cls), "code": ERROR_CODES.get(code, code)}
    reason = apdu[2] if len(apdu) > 2 else 0
    if kind == 6:
        return {"kind": "reject", "class": None, "code": REJECT_REASONS.get(reason, reason)}
    return {"kind": "abort", "class": None, "code": ABORT_REASONS.get(reason, reason)}


# ---------------------------------------------------------------- the exchange


def perform(spec: dict[str, Any], timeout: float, egress: str = "any") -> dict[str, Any]:
    url = str(spec.get("url", ""))
    try:
        _, host, port = endpoint(url)
        obj = object_id(spec.get("object"))
        prop = property_id(spec.get("property", "present-value"))
    except ValueError as e:
        raise TransportError(f"bacnet: {e}") from None
    check_egress(f"http://{host}:{port}/", egress)
    index = spec.get("index")
    writing = "write" in spec
    invoke = os.urandom(1)[0]
    if writing:
        value = spec.get("write")
        tag = _write_tag(obj[0], value, spec.get("type"))
        priority = spec.get("priority")
        if priority is not None and not 1 <= int(priority) <= 16:
            raise TransportError("bacnet: 'priority' is 1 to 16")
        apdu = request(WRITE_PROPERTY, invoke, obj, prop, index, _application(value, tag), priority)
    else:
        apdu = request(READ_PROPERTY, invoke, obj, prop, index)
    packet = frame(apdu, spec.get("network"), spec.get("mac"))
    out: dict[str, Any] = {"url": url, "object": str(spec.get("object")), "property": str(spec.get("property", "present-value"))}
    if index is not None:
        out["index"] = int(index)
    try:
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    sock = socket.socket(addr[0], socket.SOCK_DGRAM)
    deadline = time.monotonic() + timeout
    try:
        resend = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TransportError(f"{url}: no answer from {out['object']} within {timeout:g} s")
            if now >= resend:
                sock.sendto(packet, addr[4])
                resend = now + max(0.5, min(2.0, timeout / 3))
            sock.settimeout(max(0.01, min(resend, deadline) - now))
            try:
                data, _ = sock.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                continue
            got = answer(data)
            if got is None or got[1] != invoke:
                continue
            kind, _, reply = got
            if kind in (5, 6, 7):
                refusal = _refusal(kind, reply)
                return {**out, "ok": False, "refused": refusal, "value": None, "values": [], "body": "", "json": None, **({"written": False} if writing else {})}
            if writing:
                return {**out, "ok": kind == 2, "refused": None, "written": kind == 2, "value": spec.get("write"), "values": [spec.get("write")], "body": json.dumps(spec.get("write")), "json": spec.get("write")}
            if kind != 3:
                continue
            if reply[0] & 0x08:
                return {**out, "ok": False, "refused": {"kind": "abort", "class": None, "code": "segmentation-not-supported"}, "value": None, "values": [], "body": "", "json": None}
            values = _property_value(reply)
            value = values[0] if len(values) == 1 else values
            return {**out, "ok": True, "refused": None, "value": value, "values": values, "body": json.dumps(value, ensure_ascii=False), "json": value}
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    finally:
        sock.close()


def _property_value(apdu: bytes) -> list[Any]:
    """The values inside a ReadProperty-ACK's ``[3]`` opening and closing tags."""
    i = 3
    while i < len(apdu):
        number, context, lvt, i = _read_tag(apdu, i)
        if context and lvt == -1 and number == 3:
            values, _ = decode_values(apdu, i, 3)
            return values
        if lvt >= 0:
            i += lvt
    return []
