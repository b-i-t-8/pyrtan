"""CoAP (RFC 7252) over UDP: the small-device web.

    [nodes.soil]   observe  coap = { url = "coap://garden.local/sensors/bed-1/moisture" }
    [nodes.valve]  act      coap = { url = "coap://garden.local/valves/1", method = "PUT", payload = "open" }

``method`` is ``GET`` (the default; an observe), ``POST``, ``PUT`` or
``DELETE`` (an act). ``payload`` is text, or a value sent as JSON;
``format`` names its Content-Format (``text``, ``json``, ``link``,
``cbor``, ``octets`` or a number) and ``accept`` the one wanted back.

What it leaves in state: ``url``, ``status`` (``"2.05"``), ``code`` (the
same as a number, 69), ``ok`` (a 2.xx), ``format``, ``body``, ``json``,
``elapsed_ms``. The request is confirmable and resent on the RFC's
schedule until the node's timeout; a separate response is waited for and
acknowledged; a response that comes in blocks (Block2) is read to the
end. ``coaps://`` (DTLS) and Observe are not here.
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

PORT = 5683
METHODS = {"GET": 1, "POST": 2, "PUT": 3, "DELETE": 4}
READ_METHODS = ("GET",)
FORMATS = {"text": 0, "link": 40, "xml": 41, "octets": 42, "exi": 47, "json": 50, "cbor": 60}
FORMAT_NAMES = {v: k for k, v in FORMATS.items()}
URI_HOST, URI_PORT, URI_PATH, CONTENT_FORMAT, URI_QUERY, ACCEPT, BLOCK2 = 3, 7, 11, 12, 15, 17, 23
CON, NON, ACK, RST = 0, 1, 2, 3
MAX_BODY = 8 * 1024 * 1024


def endpoint(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of ``coap://host[:port]/path``, or a :class:`ValueError`."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme == "coaps":
        raise ValueError(f"{url!r}: coaps:// needs DTLS, which this does not speak; use coap://")
    if parsed.scheme != "coap" or not parsed.hostname:
        raise ValueError(f"{url!r} is not coap://host[:port]/path")
    return parsed.scheme, parsed.hostname, parsed.port or PORT


def _format(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    name = str(value).strip().lower()
    if name.isdigit():
        return int(name)
    if name not in FORMATS:
        raise TransportError(f"coap: format {value!r} is not one of {', '.join(FORMATS)} or a number")
    return FORMATS[name]


def _uint(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, "big")


def _nibble(n: int) -> tuple[int, bytes]:
    if n < 13:
        return n, b""
    if n < 269:
        return 13, bytes([n - 13])
    return 14, struct.pack(">H", n - 269)


def encode(mtype: int, code: int, mid: int, token: bytes, options: list[tuple[int, bytes]], payload: bytes = b"") -> bytes:
    out = bytes([0x40 | (mtype << 4) | len(token), code]) + struct.pack(">H", mid) + token
    last = 0
    for number, value in sorted(options, key=lambda o: o[0]):
        d, dext = _nibble(number - last)
        n, next_ = _nibble(len(value))
        out += bytes([(d << 4) | n]) + dext + next_ + value
        last = number
    if payload:
        out += b"\xff" + payload
    return out


def decode(data: bytes) -> dict[str, Any]:
    if len(data) < 4 or data[0] >> 6 != 1:
        raise ValueError("not CoAP")
    mtype = (data[0] >> 4) & 3
    tkl = data[0] & 0x0F
    code = data[1]
    mid = struct.unpack(">H", data[2:4])[0]
    token = data[4:4 + tkl]
    i = 4 + tkl
    options: list[tuple[int, bytes]] = []
    number = 0
    payload = b""
    while i < len(data):
        if data[i] == 0xFF:
            payload = data[i + 1:]
            break
        d, n = data[i] >> 4, data[i] & 0x0F
        i += 1
        if d == 13:
            d = data[i] + 13
            i += 1
        elif d == 14:
            d = struct.unpack(">H", data[i:i + 2])[0] + 269
            i += 2
        if n == 13:
            n = data[i] + 13
            i += 1
        elif n == 14:
            n = struct.unpack(">H", data[i:i + 2])[0] + 269
            i += 2
        number += d
        options.append((number, data[i:i + n]))
        i += n
    return {"type": mtype, "code": code, "mid": mid, "token": token, "options": options, "payload": payload}


def status(code: int) -> str:
    return f"{code >> 5}.{code & 0x1F:02d}"


def perform(spec: dict[str, Any], timeout: float, egress: str = "any") -> dict[str, Any]:
    url = str(spec.get("url", ""))
    try:
        _, host, port = endpoint(url)
    except ValueError as e:
        raise TransportError(str(e)) from None
    check_egress(f"http://{host}:{port}/", egress)
    method = str(spec.get("method", "GET")).upper()
    if method not in METHODS:
        raise TransportError(f"coap: method {method!r} is not one of {', '.join(METHODS)}")
    parsed = urllib.parse.urlsplit(url)
    base: list[tuple[int, bytes]] = []
    if not _is_address(host):
        base.append((URI_HOST, host.encode("idna")))
    if parsed.port and parsed.port != PORT:
        base.append((URI_PORT, _uint(parsed.port)))
    for seg in [s for s in parsed.path.split("/") if s]:
        base.append((URI_PATH, urllib.parse.unquote(seg).encode("utf-8")))
    for q in [q for q in parsed.query.split("&") if q]:
        base.append((URI_QUERY, urllib.parse.unquote(q).encode("utf-8")))
    payload = spec.get("payload")
    fmt = _format(spec.get("format"))
    if payload is not None and not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)
        fmt = FORMATS["json"] if fmt is None else fmt
    body_out = payload.encode("utf-8") if payload is not None else b""
    if fmt is not None and body_out:
        base.append((CONTENT_FORMAT, _uint(fmt)))
    accept = _format(spec.get("accept"))
    if accept is not None:
        base.append((ACCEPT, _uint(accept)))

    try:
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    sock = socket.socket(addr[0], socket.SOCK_DGRAM)
    started = time.monotonic()
    deadline = started + timeout
    collected = b""
    block: int | None = None
    try:
        while True:
            options = list(base)
            if block is not None:
                options.append((BLOCK2, _uint(block)))  # the next block, at the size the device chose
            reply = _exchange(sock, addr[4], METHODS[method], options, body_out if block is None else b"", deadline, url, timeout)
            collected += reply["payload"]
            if len(collected) > MAX_BODY:
                raise TransportError(f"{url}: more than {MAX_BODY} bytes")
            more = next((v for n, v in reply["options"] if n == BLOCK2), None)
            if more is not None and int.from_bytes(more, "big") & 0x08 and reply["code"] >> 5 == 2:
                value = int.from_bytes(more, "big")
                block = (((value >> 4) + 1) << 4) | (value & 0x07)
                continue
            break
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    finally:
        sock.close()
    code = reply["code"]
    fmt_in = next((int.from_bytes(v, "big") for n, v in reply["options"] if n == CONTENT_FORMAT), None)
    if fmt_in in (42, 60):
        text = collected.hex()
    else:
        text = collected.decode("utf-8", errors="replace")
    return {"url": url, "status": status(code), "code": code, "ok": code >> 5 == 2, "format": FORMAT_NAMES.get(fmt_in, fmt_in), "body": text, "json": _json(text) if fmt_in not in (42, 60) else None, "elapsed_ms": int((time.monotonic() - started) * 1000)}


def _is_address(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        return True
    except OSError:
        return False


def _json(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except (json.JSONDecodeError, TypeError):
        return None


def _exchange(sock: socket.socket, addr: Any, code: int, options: list[tuple[int, bytes]], payload: bytes, deadline: float, url: str, timeout: float) -> dict[str, Any]:
    """One confirmable request and its response, piggybacked or separate."""
    mid = int.from_bytes(os.urandom(2), "big")
    token = os.urandom(4)
    packet = encode(CON, code, mid, token, options, payload)
    wait = 2.0
    resend = 0.0
    acked = False
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise TransportError(f"{url}: no answer within {timeout:g} s")
        if not acked and now >= resend:
            sock.sendto(packet, addr)
            resend = now + wait
            wait *= 2
        sock.settimeout(max(0.01, min(resend if not acked else deadline, deadline) - now))
        try:
            data, _ = sock.recvfrom(65535)
        except (socket.timeout, TimeoutError):
            continue
        try:
            msg = decode(data)
        except (ValueError, IndexError, struct.error):
            continue
        if msg["type"] == RST and msg["mid"] == mid:
            raise TransportError(f"{url}: the device reset the exchange")
        if msg["type"] == ACK and msg["mid"] == mid and msg["code"] == 0:
            acked = True  # the response comes separately
            continue
        if msg["token"] != token:
            continue
        if msg["type"] == CON:
            sock.sendto(encode(ACK, 0, msg["mid"], b"", []), addr)
        return msg
