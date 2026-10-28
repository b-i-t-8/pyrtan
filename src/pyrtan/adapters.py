"""The other forms of I/O: a browser, a file, a shell, a serial line, a queue, memory.

HTTP is the form the core spoke first (``transport.py``). Every other form
arrives here by the same rule: a node names a *source* and what it wants
from it, the tree lists what that source may reach, the runtime refuses
anything outside the list, and the adapter moves the bytes. The trace reads
the same whichever source a node used.

    [nodes.df]      observe   shell  = { command = "df", args = ["-P", "/"] }
    [nodes.log]     observe   file   = { path = "/var/log/syslog", tail = 200 }
    [nodes.note]    act       file   = { path = "./notes.txt", append = "..." }
    [nodes.modem]   observe   serial = { device = "/dev/ttyUSB0", baud = 9600, write = "AT\\r\\n", read_until = "\\n" }
    [nodes.sensor]  observe   queue  = { url = "mqtt://broker.local", topic = "home/kitchen/temperature" }
    [nodes.switch]  act       queue  = { url = "mqtt://broker.local", topic = "home/kitchen/light", publish = "on" }
    [nodes.page]    observe   browser = { url = "https://example.com/app", wait_for = "h1", actions = [{ click = "#more" }] }
    [nodes.last]    observe   memory = { key = "disk-space/root" }
    [nodes.keep]    act       memory = { key = "disk-space/root", write = "{{ state.percent }}", ttl = "1 day" }

and at the top of the tree the allow-list of the same kind: ``hosts`` for
``http``, ``browser`` and a queue's broker; ``paths`` for ``file``;
``commands`` for ``shell``; ``devices`` for ``serial``; ``topics`` for
``queue``; ``keys`` for ``memory``. A tree without the list cannot use the
source; ``pyrtan check`` says so.

What each source leaves in state (the node's ``into``):

``file``    ``path``, ``exists``, ``size``, ``modified``, ``body``, ``json``, ``lines``;
            after a write, ``written`` as well.
``shell``   ``command``, ``args``, ``status`` (the exit code), ``ok``, ``stdout``,
            ``stderr``, ``body`` (stdout again, so ``extract`` reads it by
            default), ``json`` (stdout parsed), ``lines``.
``serial``  ``device``, ``baud``, ``sent``, ``body``, ``bytes``, ``complete``, ``json``, ``lines``.
``queue``   ``url``, ``topic``, ``received``, ``message`` (``topic`` and ``body``),
            ``body``, ``json``; after a publish, ``published``.
``browser`` ``url`` (where the page ended up), ``status``, ``ok``, ``title``,
            ``body`` (the rendered document), ``elapsed_ms``.
``memory``  ``key``, ``exists``, ``value``, ``json``, ``body``, ``written``, ``expires``;
            after a ``write``, ``add`` or ``delete``, ``previous`` and ``existed``
            as well (``memory.py``).

Files, shells, serial lines and memory are on the box the service runs on. A
service started with ``--egress public`` refuses them outright, the way it
refuses private addresses: an operator hosting other people's trees does
not lend them the box. A queue's broker and a browser's page go through the
same egress check as an HTTP request.

The browser adapter needs Playwright (``pip install 'pyrtan[browser]'``),
the one optional dependency; everything else here is the standard library.
A queue is MQTT 3.1.1 over a socket, the few packets a tree needs: connect,
subscribe and wait for one message, or publish one; QoS 0.

:class:`LocalAdapters` is the real thing. A recorded fixture (see
``transport.RecordedTransport``) answers for every source as well, keyed by
:func:`adapter_key`, so a tree over any of them is tested offline the same
way an HTTP tree is.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import queue as _queue
import select
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse
from typing import Any, Callable

from .transport import EgressRefused, TransportError, check_egress

SOURCES = ("http", "browser", "file", "shell", "serial", "queue", "memory")
LOCAL_SOURCES = ("file", "shell", "serial", "memory")  # on the box itself
ALLOW_LISTS = {"http": ("hosts",), "browser": ("hosts",), "file": ("paths",), "shell": ("commands",), "serial": ("devices",), "queue": ("hosts", "topics"), "memory": ("keys",)}
LIST_NAMES = ("hosts", "paths", "commands", "devices", "topics", "keys")
MAX_READ = 8 * 1024 * 1024                              # a file or a command's output larger than this is refused
TAIL_WINDOW = 1024 * 1024                               # how far back a tail looks
MQTT_PORTS = {"mqtt": 1883, "mqtts": 8883}
BROWSER_ACTIONS = ("click", "fill", "press", "select", "wait", "wait_for")

Allowed = Callable[[str], bool]


# ---------------------------------------------------------------- allow-lists


def path_allowed(patterns: list[str], path: str) -> bool:
    """``paths`` entries are absolute paths or globs; a directory covers
    everything beneath it; ``*`` covers everything."""
    target = absolute_path(path)
    for pat in patterns:
        if pat == "*":
            return True
        pat = absolute_path(pat)
        if any(c in pat for c in "*?["):
            if fnmatch.fnmatchcase(target, pat):
                return True
        elif target == pat or target.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def absolute_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def command_allowed(commands: list[str], command: str) -> bool:
    """``commands`` entries are program names (matched against the name the
    node gives, or the last part of a path it gives) or full paths."""
    command = str(command)
    for entry in commands:
        if entry == "*":
            return True
        if "/" in entry:
            if absolute_path(command) == absolute_path(entry):
                return True
        elif command == entry or os.path.basename(command) == entry:
            return True
    return False


def device_allowed(devices: list[str], device: str) -> bool:
    device = str(device)
    return any(d == "*" or fnmatch.fnmatchcase(device, d) for d in devices)


def topic_allowed(topics: list[str], topic: str) -> bool:
    """``topics`` entries are MQTT filters: ``+`` matches one level (so does
    ``*``, for people who think in globs), ``#`` the rest."""
    parts = str(topic).split("/")
    for entry in topics:
        if entry in ("*", "#"):
            return True
        pat = entry.split("/")
        ok = True
        for i, seg in enumerate(pat):
            if seg == "#":
                ok = ok and i <= len(parts)
                break
            if i >= len(parts) or (seg not in ("+", "*") and seg != parts[i]):
                ok = False
                break
        else:
            ok = ok and len(pat) == len(parts)
        if ok:
            return True
    return False


def key_allowed(keys: list[str], key: str) -> bool:
    """``keys`` entries are memory keys or globs (``disk-space/*``); ``*`` is every key."""
    key = str(key)
    return any(k == "*" or fnmatch.fnmatchcase(key, k) for k in keys)


def broker(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of a queue URL, or a :class:`ValueError`."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in MQTT_PORTS or not parsed.hostname:
        raise ValueError(f"{url!r} is not mqtt://host[:port] or mqtts://host[:port]")
    return parsed.scheme, parsed.hostname, parsed.port or MQTT_PORTS[parsed.scheme]


# ---------------------------------------------------------------- keys and words


def adapter_key(source: str, spec: dict[str, Any]) -> str:
    """How a fixture names one request to a source. The JavaScript runtime
    builds the same key, so one fixture serves both."""
    if source == "file":
        mode = "write " if "write" in spec else "append " if "append" in spec else ""
        return f"file {mode}{spec.get('path')}"
    if source == "shell":
        return "shell " + " ".join([str(spec.get("command"))] + [_arg(a) for a in (spec.get("args") or [])])
    if source == "serial":
        write = spec.get("write")
        return f"serial {spec.get('device')}" + (f" {json.dumps(str(write))}" if write not in (None, "") else "")
    if source == "queue":
        mode = "publish " if "publish" in spec else ""
        return f"queue {mode}{spec.get('url')} {spec.get('topic')}"
    if source == "browser":
        return f"browser {spec.get('url')}"
    if source == "memory":
        what = next((m for m in ("write", "add", "delete") if m in spec), None)
        return f"memory {what + ' ' if what else ''}{spec.get('key')}"
    raise ValueError(source)


def _arg(a: Any) -> str:
    return a if isinstance(a, str) else json.dumps(a)


def what(source: str, spec: dict[str, Any]) -> str:
    """The words a trace uses for one request: what was reached."""
    if source == "file":
        return str(spec.get("path"))
    if source == "shell":
        return " ".join([str(spec.get("command"))] + [_arg(a) for a in (spec.get("args") or [])])
    if source == "serial":
        return str(spec.get("device"))
    if source == "queue":
        return f"{spec.get('topic')} at {spec.get('url')}"
    if source == "memory":
        return str(spec.get("key"))
    return str(spec.get("url"))


def brief_result(source: str, observed: dict[str, Any]) -> str:
    """The words a trace uses for what came back."""
    if source in ("http", "browser"):
        return str(observed.get("status"))
    if source == "file":
        if "written" in observed:
            return f"wrote {observed['written']} B"
        return f"{observed.get('size')} B" if observed.get("exists") else "missing"
    if source == "shell":
        return f"exit {observed.get('status')}"
    if source == "serial":
        return f"{observed.get('bytes')} B"
    if source == "queue":
        if "published" in observed:
            return f"published {observed['published']} B"
        return "1 message" if observed.get("received") else "no message"
    if source == "memory":
        if "previous" in observed:
            return f"now {json.dumps(observed.get('value'), ensure_ascii=False, default=str)[:60]}, was {json.dumps(observed.get('previous'), ensure_ascii=False, default=str)[:60]}" if observed.get("exists") else "forgotten"
        return f"{json.dumps(observed.get('value'), ensure_ascii=False, default=str)[:60]}" if observed.get("exists") else "nothing kept"
    return "ok"


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- the real adapters


class LocalAdapters:
    """The sources as this box reaches them. ``egress`` is the service's
    policy; ``browser`` an executable for Playwright's Chromium, if not the
    one Playwright installed; ``memory`` the store ``memory`` nodes keep in
    (a :class:`pyrtan.memory.Memory`, or a file to keep one in; one in this
    process otherwise)."""

    def __init__(self, egress: str = "any", browser: str | None = None, memory: Any = None) -> None:
        from .memory import Memory

        self.egress = egress
        self.memory = memory if isinstance(memory, Memory) else Memory(memory)
        self._browser: Browser | None = None
        self._browser_path = browser or os.environ.get("PYRTAN_BROWSER") or None
        self._lock = threading.Lock()

    def perform(self, source: str, spec: dict[str, Any], timeout: float, allowed: Allowed | None = None) -> dict[str, Any]:
        if source in LOCAL_SOURCES and self.egress != "any":
            raise EgressRefused(f"{source}: this service reaches only public addresses (--egress {self.egress}); files, shells, serial lines and memory are its own box, and stay closed")
        if source == "file":
            return read_or_write_file(spec)
        if source == "shell":
            return run_command(spec, timeout)
        if source == "serial":
            return serial_exchange(spec, timeout)
        if source == "queue":
            return mqtt(spec, timeout, self.egress)
        if source == "memory":
            return self.memory.perform(spec)
        if source == "browser":
            with self._lock:
                if self._browser is None:
                    self._browser = Browser(self._browser_path)
            return self._browser.perform(spec, timeout, allowed or (lambda url: True), self.egress)
        raise TransportError(f"no adapter for {source!r}")

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()


# ---------------------------------------------------------------- file


def read_or_write_file(spec: dict[str, Any]) -> dict[str, Any]:
    path = absolute_path(spec.get("path", ""))
    if "write" in spec or "append" in spec:
        data = spec["write"] if "write" in spec else spec["append"]
        if not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False) + "\n"
        if not os.path.isdir(os.path.dirname(path) or "."):
            raise TransportError(f"{path}: the directory does not exist; a tree writes files, it does not make directories")
        try:
            with open(path, "a" if "append" in spec else "w", encoding="utf-8") as f:
                f.write(data)
        except OSError as e:
            raise TransportError(f"{path}: {e.strerror or e}") from e
        st = os.stat(path)
        return {"path": path, "exists": True, "written": len(data.encode("utf-8")), "size": st.st_size, "modified": _iso(st.st_mtime)}
    if not os.path.exists(path):
        return {"path": path, "exists": False, "size": None, "modified": None, "body": None, "json": None, "lines": []}
    try:
        st = os.stat(path)
        tail = spec.get("tail")
        if tail is not None:
            tail = int(tail)
            with open(path, "rb") as f:
                f.seek(max(0, st.st_size - TAIL_WINDOW))
                raw = f.read()
            lines = raw.decode("utf-8", errors="replace").splitlines()
            if st.st_size > TAIL_WINDOW and lines:
                lines = lines[1:]  # the first line is a fragment
            lines = lines[-tail:] if tail > 0 else []
            text = "\n".join(lines)
        else:
            if st.st_size > MAX_READ:
                raise TransportError(f"{path}: {st.st_size} bytes is more than a tree reads at once ({MAX_READ}); give 'tail'")
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
            lines = text.splitlines()
    except OSError as e:
        raise TransportError(f"{path}: {e.strerror or e}") from e
    return {"path": path, "exists": True, "size": st.st_size, "modified": _iso(st.st_mtime), "body": text, "json": _json(text), "lines": lines}


# ---------------------------------------------------------------- shell


def run_command(spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One program, by name, with its arguments as a list: no shell reads
    the line, so nothing in an argument is ever a command."""
    argv = [str(spec.get("command", ""))] + [_arg(a) for a in (spec.get("args") or [])]
    stdin = spec.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        stdin = json.dumps(stdin)
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    cwd = spec.get("cwd")
    started = time.monotonic()
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None, env=env, shell=False, errors="replace")
    except FileNotFoundError:
        raise TransportError(f"{argv[0]}: not found on this box") from None
    except subprocess.TimeoutExpired:
        raise TransportError(f"{argv[0]}: no answer within {timeout:g} s") from None
    except OSError as e:
        raise TransportError(f"{argv[0]}: {e.strerror or e}") from e
    out, err = p.stdout[:MAX_READ], p.stderr[:MAX_READ]
    return {"command": argv[0], "args": argv[1:], "status": p.returncode, "ok": p.returncode == 0, "stdout": out, "stderr": err, "body": out, "json": _json(out), "lines": out.splitlines(), "elapsed_ms": int((time.monotonic() - started) * 1000)}


# ---------------------------------------------------------------- serial


def serial_exchange(spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Open a serial device at a baud rate, write what the node says, read
    until a terminator, a byte count or the timeout."""
    try:
        import termios
    except ImportError:  # pragma: no cover - Windows
        raise TransportError("serial lines need a POSIX system") from None
    device = str(spec.get("device", ""))
    baud = int(spec.get("baud", 9600))
    write = spec.get("write")
    until = spec.get("read_until", "\n" if spec.get("read") is None else None)
    count = int(spec.get("read") or 0)
    speed = getattr(termios, f"B{baud}", None)
    if speed is None:
        raise TransportError(f"{device}: {baud} baud is not a rate this system knows")
    try:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        raise TransportError(f"{device}: {e.strerror or e}") from e
    sent = 0
    buf = b""
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR | termios.IGNCR | termios.ISTRIP)  # iflag
        attrs[1] &= ~termios.OPOST                                                                                      # oflag
        attrs[2] = (attrs[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)) | termios.CS8 | termios.CLOCAL | termios.CREAD  # cflag
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG | termios.IEXTEN)                   # lflag
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        if write not in (None, ""):
            data = str(write).encode("utf-8")
            while sent < len(data):
                _, ready, _ = select.select([], [fd], [], timeout)
                if not ready:
                    raise TransportError(f"{device}: could not write within {timeout:g} s")
                sent += os.write(fd, data[sent:])
        deadline = time.monotonic() + timeout
        terminator = str(until).encode("utf-8") if until else None
        complete = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                if buf:
                    break
                raise TransportError(f"{device}: {e.strerror or e}") from e
            if not chunk:
                break
            buf += chunk
            if (terminator and terminator in buf) or (count and len(buf) >= count) or len(buf) >= MAX_READ:
                complete = True
                break
    finally:
        os.close(fd)
    if not buf and (write not in (None, "") or terminator or count):
        raise TransportError(f"{device}: no answer within {timeout:g} s")
    text = buf.decode("utf-8", errors="replace")
    return {"device": device, "baud": baud, "sent": sent, "body": text, "bytes": len(buf), "complete": complete, "json": _json(text.strip()), "lines": text.splitlines()}


# ---------------------------------------------------------------- queue (MQTT 3.1.1)


def _mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _mqtt_packet(head: int, body: bytes) -> bytes:
    n = len(body)
    length = b""
    while True:
        byte = n % 128
        n //= 128
        length += bytes([byte | (0x80 if n else 0)])
        if not n:
            break
    return bytes([head]) + length + body


def _mqtt_read(sock: socket.socket, deadline: float) -> tuple[int, int, bytes] | None:
    """One packet: ``(type, flags, body)``, or None at the deadline."""

    def take(n: int) -> bytes | None:
        out = b""
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(n - len(out))
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                raise TransportError("the broker closed the connection")
            out += chunk
        return out

    head = take(1)
    if head is None:
        return None
    n = 0
    shift = 1
    while True:
        b = take(1)
        if b is None:
            return None
        n += (b[0] & 0x7F) * shift
        shift *= 128
        if not b[0] & 0x80:
            break
    body = take(n) if n else b""
    if body is None:
        return None
    return head[0] >> 4, head[0] & 0x0F, body


def mqtt(spec: dict[str, Any], timeout: float, egress: str = "any") -> dict[str, Any]:
    """Connect, then either publish one message (``publish``) or subscribe
    and wait for one. QoS 0, clean session, no retained state of our own."""
    url = str(spec.get("url", ""))
    topic = str(spec.get("topic", ""))
    try:
        scheme, host, port = broker(url)
    except ValueError as e:
        raise TransportError(str(e)) from None
    check_egress(f"http://{host}:{port}/", egress)
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    try:
        if scheme == "mqtts":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        client_id = str(spec.get("client_id") or f"pyrtan-{os.getpid()}-{int(time.time() * 1000) % 100000}")
        user, password = spec.get("username"), spec.get("password")
        flags = 0x02 | (0x80 if user else 0) | (0x40 if password else 0)
        keepalive = min(65535, max(60, int(timeout) + 30))
        body = _mqtt_str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive) + _mqtt_str(client_id)
        if user:
            body += _mqtt_str(str(user))
        if password:
            body += _mqtt_str(str(password))
        sock.sendall(_mqtt_packet(0x10, body))
        ack = _mqtt_read(sock, deadline)
        if ack is None or ack[0] != 2 or len(ack[2]) < 2:
            raise TransportError(f"{url}: no CONNACK within {timeout:g} s")
        if ack[2][1] != 0:
            raise TransportError(f"{url}: connection refused (code {ack[2][1]})")
        if "publish" in spec:
            payload = spec["publish"]
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False)
            data = payload.encode("utf-8")
            sock.sendall(_mqtt_packet(0x30, _mqtt_str(topic) + data))
            sock.sendall(_mqtt_packet(0xE0, b""))
            return {"url": url, "topic": topic, "published": len(data), "body": payload}
        sock.sendall(_mqtt_packet(0x82, struct.pack(">H", 1) + _mqtt_str(topic) + b"\x00"))
        message = None
        while True:
            pkt = _mqtt_read(sock, deadline)
            if pkt is None:
                break
            ptype, pflags, pbody = pkt
            if ptype == 9:  # SUBACK
                if len(pbody) >= 3 and pbody[2] == 0x80:
                    raise TransportError(f"{url}: the broker refused the subscription to {topic!r}")
                continue
            if ptype == 3:  # PUBLISH
                tlen = struct.unpack(">H", pbody[:2])[0]
                mtopic = pbody[2:2 + tlen].decode("utf-8", errors="replace")
                rest = pbody[2 + tlen:]
                qos = (pflags >> 1) & 3
                if qos:
                    pid, rest = rest[:2], rest[2:]
                    if qos == 1:
                        sock.sendall(_mqtt_packet(0x40, pid))
                text = rest.decode("utf-8", errors="replace")
                message = {"topic": mtopic, "body": text, "json": _json(text)}
                break
        try:
            sock.sendall(_mqtt_packet(0xE0, b""))
        except OSError:
            pass
        return {"url": url, "topic": topic, "received": message is not None, "message": message, "body": message["body"] if message else None, "json": message["json"] if message else None}
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- browser (Playwright)


class Browser:
    """A page rendered by Chromium, through Playwright. Playwright's sync
    API lives on one thread, so one worker thread owns the browser and the
    service's request threads queue jobs for it. The page may fetch what
    it likes; it may not take the tree to another host: a top-level
    navigation outside the tree's hosts is aborted."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable
        self._jobs: _queue.Queue[tuple[Any, ...] | None] = _queue.Queue()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def _start(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="pyrtan-browser", daemon=True)
                self._thread.start()
        self._ready.wait(120)
        if self._error:
            raise TransportError(self._error)

    def _loop(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._error = "the browser adapter needs Playwright: pip install 'pyrtan[browser]' (and 'playwright install chromium', or set PYRTAN_BROWSER to a Chromium)"
            self._ready.set()
            return
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(executable_path=self.executable, headless=True)
        except Exception as e:
            self._error = f"the browser could not start: {str(e).splitlines()[0]}"
            self._ready.set()
            return
        self._ready.set()
        while True:
            job = self._jobs.get()
            if job is None:
                break
            spec, timeout, allowed, done, box = job
            try:
                box["result"] = self._render(browser, spec, timeout, allowed)
            except TransportError as e:
                box["error"] = str(e)
            except Exception as e:  # Playwright's own errors
                box["error"] = str(e).splitlines()[0]
            finally:
                done.set()
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass

    def _render(self, browser: Any, spec: dict[str, Any], timeout: float, allowed: Allowed) -> dict[str, Any]:
        url = str(spec.get("url", ""))
        started = time.monotonic()
        context = browser.new_context(user_agent=f"pyrtan/{_version()} (+https://github.com/b-i-t-8/pyrtan)")
        try:
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)
            blocked: list[str] = []

            def route(r: Any) -> None:
                req = r.request
                if req.resource_type == "document" and req.is_navigation_request() and not allowed(req.url):
                    blocked.append(req.url)
                    r.abort()
                else:
                    r.continue_()

            page.route("**/*", route)
            try:
                resp = page.goto(url, wait_until=str(spec.get("wait_until", "load")))
            except Exception as e:
                if blocked:
                    raise TransportError(f"{url} led to {blocked[-1]}, outside this tree's hosts; the navigation was refused") from None
                raise TransportError(f"{url}: {str(e).splitlines()[0]}") from None
            if blocked:
                raise TransportError(f"{url} led to {blocked[-1]}, outside this tree's hosts; the navigation was refused")
            wait_for = spec.get("wait_for")
            if wait_for:
                page.wait_for_selector(str(wait_for))
            for action in spec.get("actions") or []:
                self._act(page, action)
                if blocked:
                    raise TransportError(f"{page.url} led to {blocked[-1]}, outside this tree's hosts; the navigation was refused")
            status = resp.status if resp is not None else None
            return {"url": page.url, "status": status, "ok": status is not None and 200 <= status < 300, "title": page.title(), "body": page.content(), "elapsed_ms": int((time.monotonic() - started) * 1000)}
        finally:
            context.close()

    @staticmethod
    def _act(page: Any, action: dict[str, Any]) -> None:
        if "click" in action:
            page.click(str(action["click"]))
        elif "fill" in action:
            page.fill(str(action["fill"]["selector"]), str(action["fill"]["value"]))
        elif "press" in action:
            page.press(str(action["press"]["selector"]), str(action["press"]["key"]))
        elif "select" in action:
            page.select_option(str(action["select"]["selector"]), str(action["select"]["value"]))
        elif "wait_for" in action:
            page.wait_for_selector(str(action["wait_for"]))
        elif "wait" in action:
            page.wait_for_timeout(float(action["wait"]))
        else:
            raise TransportError(f"a browser action is one of {', '.join(BROWSER_ACTIONS)}, not {sorted(action)}")

    def perform(self, spec: dict[str, Any], timeout: float, allowed: Allowed, egress: str = "any") -> dict[str, Any]:
        url = str(spec.get("url", ""))
        check_egress(url, egress)
        self._start()
        done = threading.Event()
        box: dict[str, Any] = {}
        self._jobs.put((spec, timeout, allowed, done, box))
        if not done.wait(timeout + 30):
            raise TransportError(f"{url}: the browser did not answer within {timeout + 30:g} s")
        if "error" in box:
            raise TransportError(box["error"])
        return box["result"]

    def close(self) -> None:
        if self._thread is not None:
            self._jobs.put(None)
            self._thread.join(10)
            self._thread = None


def _version() -> str:
    from . import __version__

    return __version__


# ---------------------------------------------------------------- streams, for watches
# A watch holds a line open and hands each message or line to a callback
# until told to stop. Errors are reported through the callback's twin and
# the caller reconnects; nothing here retries on its own.


def mqtt_stream(spec: dict[str, Any], on_message: Callable[[dict[str, Any]], None], stop: threading.Event, egress: str = "any", timeout: float = 10.0) -> None:
    """Subscribe to ``spec['topic']`` at ``spec['url']`` and call
    ``on_message({"topic", "body", "json"})`` for each PUBLISH until
    ``stop`` is set. Pings the broker at half the keepalive. Raises
    :class:`TransportError` when the connection is lost."""
    url = str(spec.get("url", ""))
    topic = str(spec.get("topic", ""))
    try:
        scheme, host, port = broker(url)
    except ValueError as e:
        raise TransportError(str(e)) from None
    check_egress(f"http://{host}:{port}/", egress)
    keepalive = 60
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    try:
        if scheme == "mqtts":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        client_id = str(spec.get("client_id") or f"pyrtan-watch-{os.getpid()}-{int(time.time() * 1000) % 100000}")
        user, password = spec.get("username"), spec.get("password")
        flags = 0x02 | (0x80 if user else 0) | (0x40 if password else 0)
        body = _mqtt_str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive) + _mqtt_str(client_id)
        if user:
            body += _mqtt_str(str(user))
        if password:
            body += _mqtt_str(str(password))
        sock.sendall(_mqtt_packet(0x10, body))
        ack = _mqtt_read(sock, time.monotonic() + timeout)
        if ack is None or ack[0] != 2 or len(ack[2]) < 2 or ack[2][1] != 0:
            raise TransportError(f"{url}: the broker did not accept the connection")
        sock.sendall(_mqtt_packet(0x82, struct.pack(">H", 1) + _mqtt_str(topic) + b"\x00"))
        last_ping = time.monotonic()
        while not stop.is_set():
            pkt = _mqtt_read(sock, time.monotonic() + 1.0)
            if pkt is not None:
                ptype, pflags, pbody = pkt
                if ptype == 9 and len(pbody) >= 3 and pbody[2] == 0x80:
                    raise TransportError(f"{url}: the broker refused the subscription to {topic!r}")
                if ptype == 3:
                    tlen = struct.unpack(">H", pbody[:2])[0]
                    mtopic = pbody[2:2 + tlen].decode("utf-8", errors="replace")
                    rest = pbody[2 + tlen:]
                    qos = (pflags >> 1) & 3
                    if qos:
                        pid, rest = rest[:2], rest[2:]
                        if qos == 1:
                            sock.sendall(_mqtt_packet(0x40, pid))
                    text = rest.decode("utf-8", errors="replace")
                    on_message({"topic": mtopic, "body": text, "json": _json(text)})
            if time.monotonic() - last_ping > keepalive / 2:
                sock.sendall(_mqtt_packet(0xC0, b""))
                last_ping = time.monotonic()
        try:
            sock.sendall(_mqtt_packet(0xE0, b""))
        except OSError:
            pass
    except OSError as e:
        raise TransportError(f"{url}: {e.strerror or e}") from e
    finally:
        try:
            sock.close()
        except OSError:
            pass


def serial_lines(spec: dict[str, Any], on_line: Callable[[str], None], stop: threading.Event) -> None:
    """Hold ``spec['device']`` open and call ``on_line`` for each unit
    ending in ``read_until`` (default newline) until ``stop`` is set.
    Raises :class:`TransportError` when the device fails."""
    try:
        import termios
    except ImportError:  # pragma: no cover
        raise TransportError("serial lines need a POSIX system") from None
    device = str(spec.get("device", ""))
    baud = int(spec.get("baud", 9600))
    terminator = str(spec.get("read_until", "\n")).encode("utf-8") or b"\n"
    speed = getattr(termios, f"B{baud}", None)
    if speed is None:
        raise TransportError(f"{device}: {baud} baud is not a rate this system knows")
    try:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        raise TransportError(f"{device}: {e.strerror or e}") from e
    buf = b""
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR | termios.IGNCR | termios.ISTRIP)
        attrs[1] &= ~termios.OPOST
        attrs[2] = (attrs[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)) | termios.CS8 | termios.CLOCAL | termios.CREAD
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG | termios.IEXTEN)
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        while not stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                raise TransportError(f"{device}: {e.strerror or e}") from e
            if not chunk:
                raise TransportError(f"{device}: closed")
            buf += chunk
            while terminator in buf:
                line, buf = buf.split(terminator, 1)
                on_line(line.decode("utf-8", errors="replace"))
            if len(buf) > MAX_READ:
                buf = b""
    finally:
        os.close(fd)
