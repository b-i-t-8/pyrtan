"""Discovery: what is on the network, and what answers where.

    [nodes.matter]  observe  discover = { mdns = "_matter._tcp" }                  # DNS-SD: instances, hosts, addresses, ports, TXT
    [nodes.upnp]    observe  discover = { ssdp = "ssdp:all" }                      # UPnP: who answers an M-SEARCH, and what they call themselves
    [nodes.bms]     observe  discover = { bacnet = "who-is" }                      # BACnet: every device's I-Am
    [nodes.lan]     observe  discover = { scan = "192.168.1.0/24", ports = [22, 80, 443, 502] }   # TCP: which hosts are up, which ports are open

A tree that discovers lists the ``networks`` it may look at (CIDRs such
as ``192.168.1.0/24``): a scan's target must lie inside them, and a device
that answers from outside them is left out of what the node observes.
``wait`` is how long to listen for answers (seconds; 3 by default, 30 at
most); a scan's ``connect`` is how long one connection may take (0.5 s).

What it leaves in state: ``method``, ``query``, ``ok``, ``found`` (how
many), ``devices`` (one table each, below), ``body`` and ``json`` (the
devices).

``mdns``    ``name`` (the instance), ``service``, ``host``, ``address``, ``port``, ``txt``.
            An empty query (``mdns = ""``) lists the service types on the network.
``ssdp``    ``address``, ``location``, ``server``, ``st``, ``usn``, and, from the
            device's description, ``friendly_name``, ``manufacturer``, ``model``.
``bacnet``  ``instance`` (the device object's), ``address``, ``port``, ``network`` and
            ``mac`` (a device behind a router), ``vendor``, ``max_apdu``; ``low`` and
            ``high`` limit the instances asked for.
``scan``    ``address``, ``open`` (the ports that accepted), ``name`` (reverse DNS,
            with ``names = true``). A host that refused a connection is up too.

Discovery looks at the box's own network, so a service with ``--egress
public`` refuses it, as it refuses files and programs. Multicast and
broadcast do not cross a container's NAT: in Docker, discovery by mDNS,
SSDP or BACnet needs the host's network (a scan does not). There is no
ICMP: a ping needs a raw socket, and a container has dropped that
capability; a TCP connection says as much. Standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .transport import TransportError

METHODS = ("mdns", "ssdp", "bacnet", "scan")
DEFAULT_PORTS = [22, 80, 443, 502, 1883, 5540, 8080, 47808]
MAX_HOSTS = 1024
MAX_PORTS = 64
MAX_WAIT = 30.0
MAX_DESCRIPTIONS = 32
MDNS = ("224.0.0.251", 5353)
SSDP = ("239.255.255.250", 1900)
BACNET_BROADCAST = ("255.255.255.255", 47808)

Allowed = Callable[[str], bool]


def method_of(spec: dict[str, Any]) -> str | None:
    found = [m for m in METHODS if m in spec]
    return found[0] if len(found) == 1 else None


def network_allowed(networks: list[str], target: str) -> bool:
    """Whether an address or a CIDR lies inside one of ``networks``; ``*`` is everything."""
    try:
        net = ipaddress.ip_network(str(target).strip(), strict=False)
    except ValueError:
        return False
    for entry in networks:
        if entry == "*":
            return True
        try:
            allowed = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if net.version == allowed.version and net.subnet_of(allowed):
            return True
    return False


def _wait(spec: dict[str, Any]) -> float:
    return max(0.1, min(MAX_WAIT, float(spec.get("wait", 3))))


def perform(spec: dict[str, Any], timeout: float, egress: str = "any", allowed: Allowed | None = None) -> dict[str, Any]:
    method = method_of(spec)
    if method is None:
        raise TransportError(f"discover: exactly one of {', '.join(METHODS)}")
    if egress != "any":
        raise TransportError(f"discover: this service reaches only public addresses (--egress {egress}); its own network is not looked at")
    inside = allowed or (lambda addr: True)
    query = spec[method]
    if method == "mdns":
        devices = mdns(str(query or ""), _wait(spec))
    elif method == "ssdp":
        devices = ssdp(str(query or "ssdp:all"), _wait(spec), inside)
    elif method == "bacnet":
        devices = who_is(_wait(spec), spec.get("low"), spec.get("high"))
    else:
        devices = scan(str(query), spec.get("ports") or DEFAULT_PORTS, float(spec.get("connect", 0.5)), timeout, bool(spec.get("names")), inside)
    devices = [d for d in devices if inside(str(d.get("address", "")))] if method != "scan" else devices
    return {"method": method, "query": query, "ok": True, "found": len(devices), "devices": devices, "body": json.dumps(devices, ensure_ascii=False), "json": devices}


# ---------------------------------------------------------------- mDNS / DNS-SD (RFC 6762, 6763)


def _qname(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _name(data: bytes, i: int) -> tuple[str, int]:
    labels: list[str] = []
    end = None
    seen = 0
    while True:
        n = data[i]
        if n & 0xC0 == 0xC0:
            if end is None:
                end = i + 2
            i = ((n & 0x3F) << 8) | data[i + 1]
            seen += 1
            if seen > 32:
                raise ValueError("a name that points at itself")
            continue
        if n == 0:
            i += 1
            break
        labels.append(data[i + 1:i + 1 + n].decode("utf-8", errors="replace"))
        i += 1 + n
    return ".".join(labels), (end if end is not None else i)


def dns_records(data: bytes) -> list[dict[str, Any]]:
    """Every answer, authority and additional record of a DNS message, as ``{name, type, ...}``."""
    _, _, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    i = 12
    for _ in range(qd):
        _, i = _name(data, i)
        i += 4
    out = []
    for _ in range(an + ns + ar):
        name, i = _name(data, i)
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        rdata_at, i = i, i + rdlen
        rec: dict[str, Any] = {"name": name, "type": rtype}
        if rtype == 12:  # PTR
            rec["target"] = _name(data, rdata_at)[0]
        elif rtype == 33:  # SRV
            _, _, port = struct.unpack(">HHH", data[rdata_at:rdata_at + 6])
            rec["port"], rec["target"] = port, _name(data, rdata_at + 6)[0]
        elif rtype == 16:  # TXT
            txt, k = {}, rdata_at
            while k < i:
                n = data[k]
                entry = data[k + 1:k + 1 + n].decode("utf-8", errors="replace")
                k += 1 + n
                if entry:
                    key, _, value = entry.partition("=")
                    txt[key] = value
            rec["txt"] = txt
        elif rtype == 1 and rdlen == 4:
            rec["address"] = socket.inet_ntoa(data[rdata_at:i])
        elif rtype == 28 and rdlen == 16:
            rec["address"] = socket.inet_ntop(socket.AF_INET6, data[rdata_at:i])
        out.append(rec)
    return out


def mdns_query(name: str) -> bytes:
    """A one-question PTR query, asking for a unicast answer (the QU bit)."""
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", 12, 0x8001)


def mdns(service: str, wait: float) -> list[dict[str, Any]]:
    """Browse ``service`` (``_matter._tcp``; ``.local`` is added) by DNS-SD,
    or list the service types with an empty one. The query goes from an
    ephemeral port, so responders answer it by unicast (RFC 6762 6.7)."""
    browse_types = not service.strip()
    name = "_services._dns-sd._udp.local" if browse_types else (service if service.endswith(".local") else service.rstrip(".") + ".local")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        records: list[dict[str, Any]] = []
        deadline = time.monotonic() + wait
        sock.sendto(mdns_query(name), MDNS)
        resent = False
        while (remaining := deadline - time.monotonic()) > 0:
            if not resent and remaining < wait / 2:
                sock.sendto(mdns_query(name), MDNS)  # once more: a packet on a busy network is lost now and then
                resent = True
            sock.settimeout(min(remaining, wait / 2))
            try:
                data, _ = sock.recvfrom(9000)
            except (socket.timeout, TimeoutError):
                continue
            try:
                records.extend(dns_records(data))
            except (ValueError, IndexError, struct.error):
                continue
    except OSError as e:
        raise TransportError(f"mdns: {e.strerror or e}") from e
    finally:
        sock.close()
    return assemble_mdns(name, records, browse_types)


def assemble_mdns(name: str, records: list[dict[str, Any]], browse_types: bool = False) -> list[dict[str, Any]]:
    """Instances of ``name`` from the records that came back: PTR to SRV to address, with TXT."""
    key = name.lower()
    ptrs = sorted({r["target"] for r in records if r["type"] == 12 and r["name"].lower() == key})
    if browse_types:
        return [{"service": t, "address": ""} for t in ptrs]
    srv = {r["name"].lower(): r for r in records if r["type"] == 33}
    txt = {r["name"].lower(): r["txt"] for r in records if r["type"] == 16}
    addrs: dict[str, list[str]] = {}
    for r in records:
        if r["type"] in (1, 28):
            addrs.setdefault(r["name"].lower(), [])
            if r["address"] not in addrs[r["name"].lower()]:
                addrs[r["name"].lower()].append(r["address"])
    out = []
    for inst in ptrs:
        s = srv.get(inst.lower(), {})
        host = s.get("target", "")
        found = addrs.get(host.lower(), [])
        v4 = [a for a in found if ":" not in a]
        suffix = "." + name
        out.append({
            "name": inst[:-len(suffix)] if inst.lower().endswith(suffix.lower()) else inst,
            "service": name, "host": host, "address": (v4 or found or [""])[0], "addresses": found,
            "port": s.get("port"), "txt": txt.get(inst.lower(), {}),
        })
    return out


# ---------------------------------------------------------------- SSDP / UPnP


def ssdp(target: str, wait: float, inside: Allowed) -> list[dict[str, Any]]:
    """M-SEARCH for ``target`` and read each answer's device description."""
    mx = max(1, min(5, int(wait)))
    msg = f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: {mx}\r\nST: {target}\r\nUSER-AGENT: pyrtan\r\n\r\n'.encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.sendto(msg, SSDP)
        deadline = time.monotonic() + wait
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(9000)
            except (socket.timeout, TimeoutError):
                break
            headers = ssdp_headers(data)
            if headers is None:
                continue
            key = headers.get("usn") or headers.get("location") or addr[0]
            found.setdefault(key, {"address": addr[0], "location": headers.get("location", ""), "server": headers.get("server", ""), "st": headers.get("st", ""), "usn": headers.get("usn", "")})
    except OSError as e:
        raise TransportError(f"ssdp: {e.strerror or e}") from e
    finally:
        sock.close()
    devices = sorted(found.values(), key=lambda d: (d["address"], d["usn"]))
    describe(devices, inside)
    return devices


def ssdp_headers(data: bytes) -> dict[str, str] | None:
    text = data.decode("utf-8", errors="replace")
    head, _, rest = text.partition("\r\n")
    if not head.startswith("HTTP/1.1 200") and not head.startswith("NOTIFY"):
        return None
    out = {}
    for line in rest.split("\r\n"):
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip().lower()] = v.strip()
    return out


def describe(devices: list[dict[str, Any]], inside: Allowed) -> None:
    """Fill in ``friendly_name``, ``manufacturer`` and ``model`` from each
    device's description, when its LOCATION is on a network the tree may see."""
    locations = []
    for d in devices:
        d.setdefault("friendly_name", None)
        d.setdefault("manufacturer", None)
        d.setdefault("model", None)
        loc = d.get("location") or ""
        host = urllib.parse.urlsplit(loc).hostname if loc.startswith("http://") else None
        if host and inside(host) and loc not in locations:
            locations.append(loc)
    described: dict[str, dict[str, Any]] = {}

    def fetch(loc: str) -> None:
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(loc, timeout=2) as resp:
                described[loc] = description(resp.read(256 * 1024))
        except Exception:  # a device that does not describe itself is still found
            pass

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(fetch, locations[:MAX_DESCRIPTIONS]))
    for d in devices:
        d.update(described.get(d.get("location") or "", {}))


def description(xml: bytes) -> dict[str, Any]:
    root = ET.fromstring(xml)
    dev = next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "device"), None)
    if dev is None:
        return {}

    def field(name: str) -> str | None:
        child = next((c for c in dev if c.tag.rsplit("}", 1)[-1] == name), None)
        return child.text.strip() if child is not None and child.text else None

    return {"friendly_name": field("friendlyName"), "manufacturer": field("manufacturer"), "model": field("modelName")}


# ---------------------------------------------------------------- BACnet Who-Is / I-Am


def who_is_packet(low: Any = None, high: Any = None) -> bytes:
    from . import bacnet

    apdu = b"\x10\x08"
    if low is not None and high is not None:
        apdu += bacnet._tag(0, True, bacnet._unsigned(low)) + bacnet._tag(1, True, bacnet._unsigned(high))
    npdu = b"\x01\x20\xff\xff\x00\xff"  # to every network, through routers too
    data = npdu + apdu
    return struct.pack(">BBH", 0x81, 0x0B, len(data) + 4) + data


def i_am(data: bytes, addr: tuple[str, int]) -> dict[str, Any] | None:
    """The device in a BACnet/IP I-Am, or None."""
    from . import bacnet

    if len(data) < 8 or data[0] != 0x81 or data[1] not in (0x0A, 0x0B, 0x04):
        return None
    i = 4
    if data[1] == 0x04:  # forwarded: the original sender's address comes first
        addr = (socket.inet_ntoa(data[4:8]), struct.unpack(">H", data[8:10])[0])
        i = 10
    control = data[i + 1]
    i += 2
    if control & 0x80:
        return None
    network = mac = None
    if control & 0x20:
        i += 3 + data[i + 2]
    if control & 0x08:
        network = struct.unpack(">H", data[i:i + 2])[0]
        slen = data[i + 2]
        mac = data[i + 3:i + 3 + slen].hex()
        i += 3 + slen
    if control & 0x20:
        i += 1
    if data[i:i + 2] != b"\x10\x00":
        return None
    values, _ = bacnet.decode_values(data, i + 2)
    if len(values) < 4 or not isinstance(values[0], str) or not values[0].startswith("device:"):
        return None
    return {"instance": int(values[0].split(":", 1)[1]), "address": addr[0], "port": addr[1], "network": network, "mac": mac, "max_apdu": values[1], "segmentation": values[2], "vendor": values[3]}


def who_is(wait: float, low: Any = None, high: Any = None) -> list[dict[str, Any]]:
    """Broadcast a Who-Is and gather the I-Ams. Devices often broadcast
    their I-Am to port 47808, so the socket listens there when it can."""
    from . import bacnet

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", bacnet.PORT))
        except OSError:
            sock.bind(("", 0))  # something else holds 47808: unicast answers still come
        sock.sendto(who_is_packet(low, high), BACNET_BROADCAST)
        deadline = time.monotonic() + wait
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1500)
            except (socket.timeout, TimeoutError):
                break
            try:
                dev = i_am(data, addr)
            except (IndexError, struct.error, ValueError):
                continue
            if dev is not None:
                found.setdefault((dev["instance"], dev["address"], dev["network"]), dev)
    except OSError as e:
        raise TransportError(f"bacnet who-is: {e.strerror or e}") from e
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: d["instance"])


# ---------------------------------------------------------------- TCP scan


def scan(target: str, ports: Any, connect: float, timeout: float, names: bool, inside: Allowed) -> list[dict[str, Any]]:
    """Which hosts of ``target`` are up and which of ``ports`` accept a
    connection; a host that refuses one is up too. Within the node's timeout."""
    try:
        net = ipaddress.ip_network(target.strip(), strict=False)
    except ValueError:
        raise TransportError(f"scan: {target!r} is not an address or a network such as 192.168.1.0/24") from None
    if not inside(str(net)):
        raise TransportError(f"scan: {net} is outside this tree's networks")
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
    if len(hosts) > MAX_HOSTS:
        raise TransportError(f"scan: {len(hosts)} hosts is more than a tree scans at once ({MAX_HOSTS}, a /22)")
    ports = [int(p) for p in (ports if isinstance(ports, list) else [ports])]
    if not ports or len(ports) > MAX_PORTS or not all(0 < p < 65536 for p in ports):
        raise TransportError(f"scan: 'ports' is 1 to {MAX_PORTS} port numbers")
    connect = max(0.05, min(5.0, connect))
    deadline = time.monotonic() + max(0.5, timeout - 0.5)
    up: dict[str, set[int]] = {}
    lock = threading.Lock()

    def probe(job: tuple[str, int]) -> None:
        host, port = job
        if time.monotonic() > deadline:
            return
        try:
            with socket.create_connection((host, port), timeout=connect):
                with lock:
                    up.setdefault(host, set()).add(port)
        except ConnectionRefusedError:
            with lock:
                up.setdefault(host, set())
        except OSError:
            pass

    with ThreadPoolExecutor(min(128, len(hosts) * len(ports))) as pool:
        list(pool.map(probe, [(h, p) for h in hosts for p in ports]))
    out = []
    for host in sorted(up, key=lambda a: ipaddress.ip_address(a)):
        dev: dict[str, Any] = {"address": host, "open": sorted(up[host])}
        if names:
            try:
                dev["name"] = socket.gethostbyaddr(host)[0]
            except OSError:
                dev["name"] = None
        out.append(dev)
    return out
