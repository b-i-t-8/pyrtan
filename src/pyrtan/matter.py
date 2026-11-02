"""Matter, through a Matter controller: read an attribute, write one, send a command.

A Matter device is reached over a fabric: commissioning, certificates and
secure sessions that belong to a controller, not to a tree. So a node
talks to a controller that already holds the fabric, the open-source
python-matter-server (the one Home Assistant runs), over its WebSocket
API, and the controller talks to the device.

    [nodes.light]  observe  matter = { url = "ws://matter.local:5580/ws", node = 4, read = "1/OnOff/OnOff" }
    [nodes.on]     act      matter = { url = "ws://matter.local:5580/ws", node = 4, endpoint = 1, cluster = "OnOff", command = "On" }
    [nodes.dim]    act      matter = { url = "ws://matter.local:5580/ws", node = 4, write = "1/LevelControl/OnLevel", value = 128 }

A path is ``endpoint/cluster/attribute``: numbers, or the names in
:data:`CLUSTERS` and :data:`ATTRIBUTES` (``1/TemperatureMeasurement/
MeasuredValue`` is ``1/1026/0``). A command names its cluster, its
command (``On``, ``Off``, ``Toggle``, ``MoveToLevel``, …) and its
``payload`` (``{ level = 128, transitionTime = 0 }``). ``scale``
multiplies a number read (Matter keeps temperatures in hundredths:
``scale = 0.01``).

What it leaves in state: ``url``, ``node``, ``ok``, ``refused``
(``{code, details}`` when the controller refused, such as a node that is
not reachable: an answer, not a transport error), and ``value``,
``body``, ``json`` for a read (``path`` the numeric path); ``result`` for
a command or a write.

The WebSocket client is the standard library's sockets, ``ws://`` or
``wss://``; one connection per node.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
from typing import Any

from .transport import TransportError, check_egress

PORTS = {"ws": 80, "wss": 443}
MAX_MESSAGE = 16 * 1024 * 1024

CLUSTERS = {
    "Identify": 3, "Groups": 4, "OnOff": 6, "LevelControl": 8, "Descriptor": 29, "BasicInformation": 40,
    "PowerSource": 47, "Switch": 59, "BooleanState": 69, "ModeSelect": 80, "AirQuality": 91, "SmokeCoAlarm": 92,
    "ElectricalPowerMeasurement": 144, "ElectricalEnergyMeasurement": 145, "DoorLock": 257, "WindowCovering": 258,
    "Thermostat": 513, "FanControl": 514, "ColorControl": 768, "IlluminanceMeasurement": 1024,
    "TemperatureMeasurement": 1026, "PressureMeasurement": 1027, "FlowMeasurement": 1028,
    "RelativeHumidityMeasurement": 1029, "OccupancySensing": 1030,
}
ATTRIBUTES: dict[int, dict[str, int]] = {
    6: {"OnOff": 0},
    8: {"CurrentLevel": 0, "MinLevel": 2, "MaxLevel": 3, "OnLevel": 17},
    29: {"DeviceTypeList": 0, "ServerList": 1, "ClientList": 2, "PartsList": 3},
    40: {"VendorName": 1, "VendorID": 2, "ProductName": 3, "ProductID": 4, "NodeLabel": 5, "Location": 6,
         "HardwareVersion": 7, "HardwareVersionString": 8, "SoftwareVersion": 9, "SoftwareVersionString": 10,
         "SerialNumber": 15, "Reachable": 17, "UniqueID": 18},
    47: {"Status": 0, "Order": 1, "Description": 2, "BatPercentRemaining": 12, "BatChargeLevel": 14},
    69: {"StateValue": 0},
    257: {"LockState": 0, "LockType": 1, "ActuatorEnabled": 2, "DoorState": 3},
    258: {"CurrentPositionLiftPercentage": 8, "OperationalStatus": 10, "CurrentPositionLiftPercent100ths": 14},
    513: {"LocalTemperature": 0, "OccupiedCoolingSetpoint": 17, "OccupiedHeatingSetpoint": 18, "SystemMode": 28},
    768: {"CurrentHue": 0, "CurrentSaturation": 1, "ColorTemperatureMireds": 7, "ColorMode": 8},
    1024: {"MeasuredValue": 0, "MinMeasuredValue": 1, "MaxMeasuredValue": 2},
    1026: {"MeasuredValue": 0, "MinMeasuredValue": 1, "MaxMeasuredValue": 2},
    1027: {"MeasuredValue": 0, "MinMeasuredValue": 1, "MaxMeasuredValue": 2},
    1028: {"MeasuredValue": 0, "MinMeasuredValue": 1, "MaxMeasuredValue": 2},
    1029: {"MeasuredValue": 0, "MinMeasuredValue": 1, "MaxMeasuredValue": 2},
    1030: {"Occupancy": 0},
}


def _norm(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


_CLUSTER_BY = {_norm(k): v for k, v in CLUSTERS.items()}


def endpoint(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of ``ws://host[:port]/path``, or a :class:`ValueError`."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in PORTS or not parsed.hostname:
        raise ValueError(f"{url!r} is not ws://host[:port]/path or wss://…, a Matter controller's WebSocket")
    return parsed.scheme, parsed.hostname, parsed.port or PORTS[parsed.scheme]


def mode_of(spec: dict[str, Any]) -> str:
    return "command" if "command" in spec else "write" if "write" in spec else "read"


def cluster_id(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text.lower().startswith("0x"):
        return int(text, 16)
    if _norm(text) not in _CLUSTER_BY:
        raise ValueError(f"{value!r} is not a Matter cluster this knows; give its number")
    return _CLUSTER_BY[_norm(text)]


def attribute_path(path: Any) -> str:
    """``"1/6/0"`` from ``"1/OnOff/OnOff"``, or a :class:`ValueError`."""
    parts = str(path).strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"{path!r} is not endpoint/cluster/attribute")
    ep, cl, at = (p.strip() for p in parts)
    if not ep.isdigit():
        raise ValueError(f"{path!r}: the endpoint is a number")
    cid = cluster_id(cl)
    if at.isdigit():
        aid = int(at)
    elif at.lower().startswith("0x"):
        aid = int(at, 16)
    else:
        names = {_norm(k): v for k, v in ATTRIBUTES.get(cid, {}).items()}
        if _norm(at) not in names:
            raise ValueError(f"{path!r}: {at!r} is not an attribute of cluster {cid} this knows; give its number")
        aid = names[_norm(at)]
    return f"{int(ep)}/{cid}/{aid}"


# ---------------------------------------------------------------- a WebSocket, just enough of one


class WebSocket:
    """A client WebSocket (RFC 6455): text messages, masked, with pings
    answered; the standard library only."""

    def __init__(self, url: str, timeout: float, egress: str = "any") -> None:
        try:
            scheme, host, port = endpoint(url)
        except ValueError as e:
            raise TransportError(str(e)) from None
        check_egress(f"http://{host}:{port}/", egress)
        self.url = url
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            if scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except OSError as e:
            raise TransportError(f"{url}: {e.strerror or e}") from e
        self.sock = sock
        self._buf = b""
        parsed = urllib.parse.urlsplit(url)
        key = base64.b64encode(os.urandom(16)).decode()
        target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        hostport = host if port == PORTS[scheme] else f"{host}:{port}"
        self._send_raw(
            f"GET {target} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: pyrtan\r\n\r\n".encode()
        )
        head = self._until(b"\r\n\r\n").decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            self.close()
            raise TransportError(f"{url}: not a WebSocket ({status.strip()})")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in head.split("\r\n")[1:]) if k}
        if headers.get("sec-websocket-accept") != accept:
            self.close()
            raise TransportError(f"{url}: the server did not complete the WebSocket handshake")

    def _send_raw(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise TransportError(f"{self.url}: {e.strerror or e}") from e

    def _fill(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError(f"{self.url}: no answer within {self.timeout:g} s")
        self.sock.settimeout(remaining)
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            raise TransportError(f"{self.url}: no answer within {self.timeout:g} s") from None
        except OSError as e:
            raise TransportError(f"{self.url}: {e.strerror or e}") from e
        if not chunk:
            raise TransportError(f"{self.url}: the controller closed the connection")
        self._buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            if len(self._buf) > 65536:
                raise TransportError(f"{self.url}: not a WebSocket")
            self._fill()
        out, _, self._buf = self._buf.partition(marker)
        return out

    def _frame(self, opcode: int, data: bytes) -> None:
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self._send_raw(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        message = b""
        while True:
            b0, b1 = self._take(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            if n + len(message) > MAX_MESSAGE:
                raise TransportError(f"{self.url}: a message larger than {MAX_MESSAGE} bytes")
            mask = self._take(4) if b1 & 0x80 else b""
            data = self._take(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise TransportError(f"{self.url}: the controller closed the connection")
            if opcode == 0x9:
                self._frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            message += data
            if b0 & 0x80:
                return message.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._frame(0x8, b"\x03\xe8")
        except TransportError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- the controller


def call(ws: WebSocket, command: str, args: dict[str, Any]) -> dict[str, Any]:
    """Send one command and wait for its answer: ``{"result": …}`` or ``{"error_code", "details"}``."""
    mid = os.urandom(4).hex()
    ws.send(json.dumps({"message_id": mid, "command": command, "args": args}))
    while True:
        try:
            msg = json.loads(ws.recv())
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("message_id") == mid:
            return msg


def _scaled(v: Any, scale: Any) -> Any:
    if scale in (None, 1) or isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return float(f"{v * float(scale):.10g}")


def perform(spec: dict[str, Any], timeout: float, egress: str = "any") -> dict[str, Any]:
    url = str(spec.get("url", ""))
    try:
        node = int(spec.get("node"))
    except (TypeError, ValueError):
        raise TransportError(f"matter: 'node' is the node id, a number, not {spec.get('node')!r}") from None
    mode = mode_of(spec)
    try:
        if mode == "read":
            path = attribute_path(spec.get("read"))
        elif mode == "write":
            path = attribute_path(spec.get("write"))
        else:
            cluster = cluster_id(spec.get("cluster"))
            ep = int(spec.get("endpoint", 1))
    except ValueError as e:
        raise TransportError(f"matter: {e}") from None
    out: dict[str, Any] = {"url": url, "node": node}
    ws = WebSocket(url, timeout, egress)
    try:
        # The controller says who it is first, and may send events; call() waits past them for its answer.
        if mode == "read":
            msg = call(ws, "read_attribute", {"node_id": node, "attribute_path": path})
        elif mode == "write":
            msg = call(ws, "write_attribute", {"node_id": node, "attribute_path": path, "value": spec.get("value")})
        else:
            msg = call(ws, "device_command", {"node_id": node, "endpoint_id": ep, "cluster_id": cluster, "command_name": str(spec.get("command")), "payload": spec.get("payload") or {}})
    finally:
        ws.close()
    if "result" not in msg or msg.get("error_code") is not None:
        refused = {"code": msg.get("error_code"), "details": msg.get("details")}
        base = {"path": path} if mode != "command" else {"endpoint": ep, "cluster": cluster, "command": str(spec.get("command"))}
        return {**out, **base, "ok": False, "refused": refused, "value": None, "result": None, "body": "", "json": None, **({"written": False} if mode == "write" else {})}
    result = msg.get("result")
    if mode == "read":
        value = result.get(path, next(iter(result.values()), None)) if isinstance(result, dict) and result and all(isinstance(k, str) and k.count("/") == 2 for k in result) else result
        value = _scaled(value, spec.get("scale"))
        return {**out, "path": path, "ok": True, "refused": None, "value": value, "result": result, "body": json.dumps(value, ensure_ascii=False), "json": value}
    if mode == "write":
        statuses = result if isinstance(result, list) else [result]
        ok = all(not isinstance(s, dict) or s.get("Status", 0) == 0 for s in statuses)
        return {**out, "path": path, "ok": ok, "written": ok, "refused": None if ok else {"code": next((s.get("Status") for s in statuses if isinstance(s, dict) and s.get("Status")), None), "details": "the device refused the write"}, "value": spec.get("value"), "result": result, "body": json.dumps(result, ensure_ascii=False), "json": result}
    return {**out, "endpoint": ep, "cluster": cluster, "command": str(spec.get("command")), "ok": True, "refused": None, "value": None, "result": result, "body": json.dumps(result, ensure_ascii=False), "json": result}
