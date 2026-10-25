"""How a tree reaches the outside: the HTTP form of I/O.

A :class:`Transport` turns a request into a :class:`Response`. Further forms
of I/O (files, shells, serial lines, queues) arrive as further transports
behind the same seam, each with a recorded and a recording wrapper so trees
over them are testable offline the same way. The real one is
:class:`UrllibTransport`, on the standard library. :class:`RecordedTransport`
replays responses from a fixture, so a tree can be tested end to end with no
network, and :class:`RecordingTransport` wraps a real transport to write such
a fixture.

The runtime, not the transport, enforces a tree's host allow-list; a
transport only moves bytes.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: str
    url: str
    elapsed_ms: int = 0

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, TypeError):
            return None

    def to_state(self) -> dict[str, Any]:
        """The shape a tree sees after an ``http`` observation."""
        return {
            "status": self.status,
            "ok": 200 <= self.status < 300,
            "headers": self.headers,
            "body": self.body,
            "json": self.json,
            "url": self.url,
            "elapsed_ms": self.elapsed_ms,
        }


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None, timeout: float = 10.0) -> Response: ...


class TransportError(RuntimeError):
    pass


class UrllibTransport:
    """The standard library's HTTP client, with a fixed user agent and no
    cookie jar: every request is stateless unless a tree sends state itself."""

    def __init__(self, user_agent: str = "pyrtan/0.1 (+https://github.com/b-i-t-8/pyrtan)") -> None:
        self.user_agent = user_agent

    def request(self, method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None, timeout: float = 10.0) -> Response:
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
        hdrs.update(headers or {})
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, _decode(raw, resp.headers.get_content_charset()), resp.geturl(), int((time.monotonic() - started) * 1000))
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, _decode(raw, None), url, int((time.monotonic() - started) * 1000))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TransportError(f"{method} {url}: {e}") from e


def _decode(raw: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _key(method: str, url: str) -> str:
    return f"{method.upper()} {url}"


@dataclass
class RecordedTransport:
    """Replay responses from a fixture: ``{"GET https://...": {"status": 200,
    "body": "..."}}``. A ``body`` may be a JSON value instead of a string, in
    which case it is serialised. Unknown requests raise, so a test fails
    loudly when a tree reaches somewhere its fixture did not foresee."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> "RecordedTransport":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def add(self, method: str, url: str, status: int = 200, body: Any = "", headers: dict[str, str] | None = None) -> None:
        self.responses[_key(method, url)] = {"status": status, "body": body, "headers": headers or {}}

    def request(self, method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None, timeout: float = 10.0) -> Response:
        self.calls.append((method.upper(), url))
        rec = self.responses.get(_key(method, url))
        if rec is None:
            raise TransportError(f"no recorded response for {_key(method, url)}")
        if isinstance(rec, list):
            # A sequence: consume in order, repeating the last.
            rec = rec.pop(0) if len(rec) > 1 else rec[0]
        if "error" in rec:
            raise TransportError(str(rec["error"]))
        text = rec.get("body", "")
        if not isinstance(text, str):
            text = json.dumps(text)
        return Response(int(rec.get("status", 200)), {k.lower(): v for k, v in rec.get("headers", {}).items()}, text, url, 0)


@dataclass
class RecordingTransport:
    """Pass through to ``inner`` and keep what came back, to be saved as a
    fixture for :class:`RecordedTransport`."""

    inner: Transport
    recorded: dict[str, Any] = field(default_factory=dict)

    def request(self, method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None, timeout: float = 10.0) -> Response:
        resp = self.inner.request(method, url, headers, body, timeout)
        self.recorded[_key(method, url)] = {"status": resp.status, "headers": resp.headers, "body": resp.body}
        return resp

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.recorded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
