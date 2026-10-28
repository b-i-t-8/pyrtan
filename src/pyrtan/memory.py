"""Memory across runs: a few values a tree keeps on the box it runs on.

A tree that runs on a clock wants to know what it saw last time: whether
the disk was already full an hour ago, whether it already said so, how
many checks in a row a sensor has been over the line. That is memory,
and it is one more source, with one more allow-list:

    keys = ["disk-space/*"]

    [nodes.last]
    kind = "observe"
    memory = { key = "disk-space/{{ inputs.mount }}" }           # key, exists, value, json, body, written
    into = "last"

    [nodes.count]
    kind = "act"
    memory = { key = "disk-space/{{ inputs.mount }}/over", add = 1, ttl = "1 day" }
    into = "over"                                                 # ... and previous, existed

An ``observe`` reads one key. An ``act`` writes it (``write``, any value),
counts on it (``add``, a number; a missing key counts from 0), or forgets
it (``delete = true``); ``ttl`` makes a written value disappear after a
duration. What an act leaves says what was there before (``previous``,
``existed``), so one node can remember a value and say whether it changed.

Nothing here weakens the invariants. The keys a tree may touch are listed
at its top, checked at load time when literal and at run time when
templated (``Tree.allowed``). A read is an observation like any other: a
recording keeps it (``pyrtan run --record``) and a fixture answers it,
under :func:`pyrtan.adapters.adapter_key`, so a run is still deterministic
given its observations and ``pyrtan test`` replays it. A write is an act,
never retried.

Memory is the box's own. A service keeps it in the file ``--memory``
names (in the process without one, gone when it stops), nobody reads it
over HTTP, it is never a blob, never gossiped and never handed to a
member, and a service started with ``--egress public`` refuses it the
way it refuses files: an operator hosting other people's trees does not
let them read each other's notes. Limits are fixed and small: a key is
at most 200 characters, a value at most 64 KB as JSON, a store at most
10,000 keys.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .transport import TransportError

KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,199}$")
MAX_VALUE = 64 * 1024
MAX_KEYS = 10_000
MODES = ("write", "add", "delete")


class MemoryRefused(TransportError):
    """A request the store refuses: a bad key, a value too large, a full store,
    a count on a word. The run fails the node with it, as with any source."""


def mode(spec: dict[str, Any]) -> str:
    """What a memory request does: ``read``, or one of :data:`MODES`."""
    return next((m for m in MODES if m in spec), "read")


def ttl_seconds(value: Any) -> int | None:
    """``ttl`` as seconds: a number, or a duration such as ``"1 day"``; None when absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise MemoryRefused("'ttl' is a duration such as \"1 hour\", or seconds")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        from .nlp import ParseError, parse_duration

        try:
            seconds = int(value) if str(value).strip().isdigit() else parse_duration(str(value))
        except ParseError:
            raise MemoryRefused(f"'ttl' is a duration such as \"1 hour\", or seconds, got {value!r}") from None
    if seconds < 1:
        raise MemoryRefused(f"'ttl' must be at least a second, got {value!r}")
    return seconds


class Memory:
    """The store: ``{key: {"value", "written", "expires"}}`` in a JSON file,
    or only in this process when ``path`` is None."""

    def __init__(self, path: str | Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise MemoryRefused(f"{self.path}: not a memory file ({e})") from None
            entries = data.get("keys") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                raise MemoryRefused(f"{self.path}: not a memory file (no 'keys' table)")
            self._entries = {k: v for k, v in entries.items() if isinstance(v, dict) and "value" in v}

    # ------------------------------------------------------------ what a node does

    def perform(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Read, write, add to or forget one key, and say what is there now and what was there before."""
        key = str(spec.get("key", ""))
        if not KEY.match(key):
            raise MemoryRefused(f"{key!r} is not a memory key: up to 200 letters, digits and . _ : @ / -, starting with a letter or digit")
        what = mode(spec)
        with self._lock:
            now = self.clock()
            before = self._live(key, now)
            if what == "read":
                return _result(key, before)
            if what == "delete":
                if spec.get("delete") is not True:
                    raise MemoryRefused("'delete' is true, or absent")
                self._entries.pop(key, None)
                self._save()
                return {**_result(key, None), **_before(before)}
            ttl = ttl_seconds(spec.get("ttl"))
            if what == "add":
                step = spec.get("add")
                if isinstance(step, bool) or not isinstance(step, (int, float)):
                    raise MemoryRefused(f"'add' is a number, got {step!r}")
                current = 0 if before is None else before["value"]
                if isinstance(current, bool) or not isinstance(current, (int, float)):
                    raise MemoryRefused(f"{key} holds {json.dumps(current)}, which is not a number to add to")
                value: Any = current + step
            else:
                value = spec.get("write")
            size = len(json.dumps(value, ensure_ascii=False, default=str))
            if size > MAX_VALUE:
                raise MemoryRefused(f"a value is at most {MAX_VALUE} bytes as JSON; {key} would be {size}")
            if before is None and len(self._entries) >= MAX_KEYS:
                self._expire(now)
                if len(self._entries) >= MAX_KEYS:
                    raise MemoryRefused(f"this memory holds {MAX_KEYS} keys, its limit; forget some first")
            entry = {"value": json.loads(json.dumps(value, default=str)), "written": int(now), "expires": int(now) + ttl if ttl else None}
            self._entries[key] = entry
            self._save()
            return {**_result(key, entry), **_before(before)}

    # ------------------------------------------------------------ for people

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        """Every live key, sorted, with its entry."""
        with self._lock:
            now = self.clock()
            return sorted((k, dict(v)) for k, v in self._entries.items() if not _expired(v, now))

    def forget(self, key: str) -> bool:
        with self._lock:
            gone = self._entries.pop(key, None) is not None
            if gone:
                self._save()
            return gone

    # ------------------------------------------------------------ inside

    def _live(self, key: str, now: float) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is not None and _expired(entry, now):
            del self._entries[key]
            return None
        return entry

    def _expire(self, now: float) -> None:
        for k in [k for k, v in self._entries.items() if _expired(v, now)]:
            del self._entries[k]

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"pyrtan-memory": 1, "keys": self._entries}, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def _expired(entry: dict[str, Any], now: float) -> bool:
    return entry.get("expires") is not None and entry["expires"] <= now


def _result(key: str, entry: dict[str, Any] | None) -> dict[str, Any]:
    """What a node sees: every source leaves ``body``, so extracts read memory the way they read a page."""
    if entry is None:
        return {"key": key, "exists": False, "value": None, "json": None, "body": "", "written": None, "expires": None}
    value = entry["value"]
    return {
        "key": key,
        "exists": True,
        "value": value,
        "json": value,
        "body": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
        "written": _iso(entry["written"]),
        "expires": _iso(entry["expires"]) if entry.get("expires") is not None else None,
    }


def _before(entry: dict[str, Any] | None) -> dict[str, Any]:
    return {"existed": entry is not None, "previous": None if entry is None else entry["value"]}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
