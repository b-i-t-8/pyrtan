"""The LAN: hubs on the same network find each other, with nobody to ask.

A hub started ``--lan`` says it is here, on a UDP multicast group that
never leaves the local network (TTL 1): its federation id (the URL other
hubs reach it at), its name, and the public networks it is in. It says
so when it starts, with a question that makes every other hub on the LAN
answer at once, and again every thirty seconds. It keeps what it hears
for three minutes. Joining a network then tries the hubs it heard on the
LAN in that network right after the ones it already follows, before its
directory, the hub list or the snapshot: two boxes in one house find
each other with the internet unplugged.

A beacon is a hint, exactly as a directory entry is: anyone on the LAN
can send one, and nothing is routed to a hub because it said it was
there. Following it is still the one act of trust, and the follow checks
the hub's ``/actor`` and is signed with the network's secret like any
other. Beacons carry no secret and no private network: only public
network ids, so a private network's hubs are not named to the LAN.

A hub announces itself only when its id is an address others can reach:
not ``127.0.0.1``, ``localhost`` or ``0.0.0.0``. Give it one with
``--advertise http://<its LAN address>:8700``. In Docker, multicast
needs ``network_mode: host``.

Standard library only. ``$PYRTAN_LAN_GROUP`` (``ADDRESS:PORT``) moves the
group, which is how the tests keep to themselves.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

GROUP = "239.255.70.84"   # administratively scoped: stays on the site
PORT = 8770
EVERY = 30.0              # seconds between beacons
HEARD_FOR = 180.0         # a hub not heard for this long is forgotten
HEARD_MAX = 64            # hubs kept at most
ANSWER_EVERY = 2.0        # a question is answered at most this often
BEACON_MAX = 2048         # bytes
UNREACHABLE = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}


def group() -> tuple[str, int]:
    """The multicast group, from ``$PYRTAN_LAN_GROUP`` or the default."""
    text = os.environ.get("PYRTAN_LAN_GROUP", "")
    host, _, port = text.rpartition(":")
    try:
        return (host, int(port)) if host and port else (GROUP, PORT)
    except ValueError:
        return GROUP, PORT


def reachable(url: str) -> bool:
    """Whether another box could reach a hub at ``url``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and parts.hostname not in UNREACHABLE and not parts.hostname.startswith("127.")


class Lan:
    """Announce this hub on the LAN and keep what other hubs announce.

    ``me`` returns what to say (``{"id", "name", "networks"}``), or None
    to stay quiet; it is asked each time, so a hub that joins a network
    says so in its next beacon."""

    def __init__(self, me: Callable[[], dict[str, Any] | None], *, every: float = EVERY, address: tuple[str, int] | None = None) -> None:
        self.me = me
        self.every = every
        self.address = address or group()
        self.heard: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        self._answered = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ what is said

    def beacon(self, *, query: bool = False) -> bytes | None:
        """What this hub says; None when it has nothing to say. A hub that
        cannot be reached still asks who is there: a question with no id."""
        me = self.me()
        if not me or not reachable(str(me.get("id", ""))):
            return json.dumps({"pyrtan-lan": 1, "query": True}).encode("utf-8") if query else None
        body = {"pyrtan-lan": 1, "id": str(me["id"]).rstrip("/"), "name": str(me.get("name", ""))[:80], "networks": [n for n in me.get("networks", []) if isinstance(n, str)][:16]}
        if query:
            body["query"] = True
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        return data if len(data) <= BEACON_MAX else None

    def take(self, data: bytes, sender: str, now: float | None = None) -> bool | None:
        """A datagram heard from ``sender``: True when it named a hub (and
        False when it named this one); None when it was not a beacon. A
        question in it is answered, at most every couple of seconds."""
        now = time.time() if now is None else now
        try:
            body = json.loads(data[:BEACON_MAX].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(body, dict) or body.get("pyrtan-lan") != 1:
            return None
        me = self.me() or {}
        hub = body["id"].rstrip("/") if isinstance(body.get("id"), str) else None
        if hub == str(me.get("id", "")).rstrip("/"):
            return False
        if body.get("query") is True and now - self._answered >= ANSWER_EVERY:
            self._answered = now
            self.send()
        if hub is None or not reachable(hub) or len(hub) > 200:
            return None
        networks = [n for n in body.get("networks", []) if isinstance(n, str) and len(n) <= 64][:16] if isinstance(body.get("networks"), list) else []
        with self._lock:
            self.heard[hub] = {"id": hub, "name": str(body.get("name", ""))[:80], "networks": networks, "from": sender, "seen": now}
            if len(self.heard) > HEARD_MAX:
                for old in sorted(self.heard.values(), key=lambda e: e["seen"])[: len(self.heard) - HEARD_MAX]:
                    self.heard.pop(old["id"], None)
        return True

    def hubs(self, network: str | None = None, now: float | None = None) -> list[str]:
        """The hubs heard lately, most recent first; ``network`` keeps the
        ones that said they are in it."""
        now = time.time() if now is None else now
        with self._lock:
            fresh = [e for e in self.heard.values() if now - e["seen"] < HEARD_FOR and (network is None or network in e["networks"])]
        return [e["id"] for e in sorted(fresh, key=lambda e: -e["seen"])]

    def view(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            rows = [{**e, "age_s": int(now - e["seen"])} for e in self.heard.values() if now - e["seen"] < HEARD_FOR]
        me = self.me() or {}
        return {"group": f"{self.address[0]}:{self.address[1]}", "announcing": self.beacon() is not None, "id": me.get("id"), "error": self.error, "hubs": sorted(rows, key=lambda e: e["age_s"])}

    # ------------------------------------------------------------ the socket

    def send(self, *, query: bool = False) -> bool:
        """Say this hub is here (``query``: and ask who else is)."""
        data = self.beacon(query=query)
        if data is None or self._sock is None:
            return False
        try:
            self._sock.sendto(data, self.address)
            return True
        except OSError as e:
            self.error = str(e)
            return False

    def start(self) -> "Lan":
        """Join the group, ask who is there, and keep listening and
        announcing in the background. A LAN that will not have it (no
        multicast route, the port refused) leaves ``error`` set and the
        hub running without it."""
        host, port = self.address
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # several hubs on one box share the port
            s.bind(("", port))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4sl", socket.inet_aton(host), socket.INADDR_ANY))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)  # never past the first router
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)  # hubs on this same box hear it too
            s.settimeout(1.0)
        except OSError as e:
            self.error = f"cannot join {host}:{port}: {e}"
            return self
        self._sock = s
        self._threads = [threading.Thread(target=self._listen, daemon=True), threading.Thread(target=self._announce, daemon=True)]
        for t in self._threads:
            t.start()
        return self

    def wait(self, seconds: float) -> None:
        """Give the answers to the first question a moment to arrive."""
        self._stop.wait(seconds)

    def _listen(self) -> None:
        while not self._stop.is_set() and self._sock is not None:
            try:
                data, (sender, _) = self._sock.recvfrom(BEACON_MAX + 1)
            except socket.timeout:
                continue
            except OSError:
                return
            self.take(data, sender)

    def _announce(self) -> None:
        self.send(query=True)
        while not self._stop.wait(self.every):
            self.send()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
