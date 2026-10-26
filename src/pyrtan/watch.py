"""Watches: a tree that runs when something speaks, without being asked.

A tree may carry ``[[watches]]``: a clock (``every = "5m"``), a file
(``file = "/var/log/syslog"``: each time lines are appended), a queue
(``queue = { url, topic }``: each message) or a serial line (``serial =
{ device, baud }``: each line). Each watch may give ``inputs``, templates
over ``event`` (what spoke), ``env`` and ``now``. A :class:`Watcher` holds
one thread per watch over a service's trees, runs the tree through the
service's own runner (so delegations reach the hub and gaps are kept),
and remembers for each watch when it last fired, what the outcome was,
and the *path* the run took: the sequence of nodes. When the path of a
scheduled run differs from the last one, the watch records that too.
That is how a person learns a site has changed before anyone asks.

The trace of a watched run begins with a ``watch`` step, as an asked
run's begins with ``intent``. Watches are static: a path, a topic, a
device is literal in the file and checked against the tree's lists by
``pyrtan check``, and a service started ``--egress public`` watches no
file and no serial line, as it reads none.

    pyrtan serve trees --watch        # a service that also watches
    pyrtan hub --trees trees --watch  # likewise, in a hub
    pyrtan watch trees                # nothing served: just the watches, printing each run
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from typing import Any, Callable

from . import adapters
from .nlp import ParseError, parse_duration
from .runtime import Outcome
from .template import TemplateError, render
from .trace import Step, Trace
from .transport import EgressRefused, TransportError
from .tree import Tree

SYNC_EVERY = 10.0      # how often the watcher looks for trees that changed on disk
FILE_POLL = 2.0        # how often a file watch looks at the file
RETRY_AFTER = 5.0      # after a stream fails, before reconnecting
HISTORY = 20           # fires remembered per watch


def every_seconds(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return parse_duration(str(value))
    except ParseError:
        return 0


class Watch:
    """One watch of one tree, on its own thread."""

    def __init__(self, watcher: "Watcher", tree_name: str, index: int, spec: dict[str, Any]) -> None:
        self.watcher = watcher
        self.tree_name = tree_name
        self.index = index
        self.spec = spec
        self.kind = next(k for k in ("every", "file", "queue", "serial") if k in spec)
        self.name = str(spec.get("name") or f"{tree_name}#{index}")
        self.key = json.dumps([tree_name, index, spec], sort_keys=True, default=str)
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.fires = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_at: float | None = None
        self.last_outcome: dict[str, Any] | None = None
        self.last_path: list[str] | None = None
        self.path_changed_at: float | None = None
        self.previous_path: list[str] | None = None
        self.history: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"pyrtan-watch-{self.name}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        egress = self.watcher.egress
        if self.kind in ("file", "serial") and egress != "any":
            self._fail(f"{self.kind}: this service reaches only public addresses (--egress {egress}); it watches no file and no serial line")
            return
        while not self.stop.is_set():
            try:
                if self.kind == "every":
                    self._every()
                elif self.kind == "file":
                    self._file()
                elif self.kind == "queue":
                    adapters.mqtt_stream(self.spec["queue"], lambda m: self.fire({"kind": "queue", **m}), self.stop, egress)
                elif self.kind == "serial":
                    adapters.serial_lines(self.spec["serial"], lambda line: self.fire({"kind": "serial", "device": self.spec["serial"].get("device"), "body": line, "lines": [line]}), self.stop)
                return
            except EgressRefused as e:
                self._fail(str(e))
                return
            except TransportError as e:
                self._fail(str(e))
            except Exception as e:  # a bug, not a line: keep the watch alive, say so
                self._fail(f"{type(e).__name__}: {e}")
            self.stop.wait(RETRY_AFTER)

    def _fail(self, text: str) -> None:
        with self.lock:
            self.errors += 1
            self.last_error = text
        self.watcher.note(f"watch {self.name}: {text}")

    def _every(self) -> None:
        seconds = max(1, every_seconds(self.spec["every"]))
        n = 0
        while not self.stop.is_set():
            n += 1
            self.fire({"kind": "every", "n": n, "at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "every": seconds})
            if self.stop.wait(seconds):
                return

    def _file(self) -> None:
        path = adapters.absolute_path(self.spec["file"])
        offset: int | None = None  # None until the file is first seen: a watch starts at the end
        while not self.stop.is_set():
            try:
                st = os.stat(path)
            except OSError:
                offset = None
                if self.stop.wait(FILE_POLL):
                    return
                continue
            if offset is None or st.st_size < offset:
                offset = st.st_size if offset is None else 0  # new file: start at its end; truncated: read it all
                if st.st_size < (offset or 0):
                    offset = 0
            if st.st_size > offset:
                with open(path, "rb") as f:
                    f.seek(offset)
                    raw = f.read(adapters.MAX_READ)
                offset += len(raw)
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()
                if lines:
                    self.fire({"kind": "file", "path": path, "body": text, "lines": lines, "size": st.st_size})
            if self.stop.wait(FILE_POLL):
                return

    def fire(self, event: dict[str, Any]) -> Outcome | None:
        """Run the tree for one event. Returns the outcome, or None when the
        tree is gone or its inputs do not render."""
        tree = self.watcher.registry.get(self.tree_name)
        if tree is None:
            self._fail(f"tree {self.tree_name!r} is gone")
            return None
        scope = {"event": event, "env": self.watcher.env, "now": _now_scope(self.watcher.clock())}
        try:
            inputs = {k: render(v, scope) for k, v in (self.spec.get("inputs") or {}).items()}
        except TemplateError as e:
            self._fail(f"inputs: {e}")
            return None
        trace = Trace(tree.name)
        trace.add(Step("watch", "watch", _summary(self.kind, event), observed=_observed(event), next=tree.start))
        outcome = self.watcher.run(tree, inputs, trace)
        path = [s.node for s in outcome.trace.steps] if outcome.trace else []
        now = time.time()
        with self.lock:
            self.fires += 1
            self.last_at = now
            self.last_outcome = {k: v for k, v in outcome.to_dict(with_trace=False).items() if k in ("status", "reason", "say", "output")}
            if self.last_path is not None and path != self.last_path:
                self.previous_path = self.last_path
                self.path_changed_at = now
            self.last_path = path
            self.history.append({"at": now, "status": outcome.status, "say": outcome.say, "path": path, "event": _observed(event)})
            del self.history[:-HISTORY]
        self.watcher.after(self, event, outcome)
        return outcome

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "tree": self.tree_name, "index": self.index, "name": self.name, "kind": self.kind, "what": _what(self.kind, self.spec),
                "fires": self.fires, "errors": self.errors, "last_error": self.last_error, "last_at": self.last_at,
                "last": self.last_outcome, "path": self.last_path, "path_changed_at": self.path_changed_at, "previous_path": self.previous_path,
                "alive": bool(self.thread and self.thread.is_alive()),
            }


class Watcher:
    """Every watch of every tree in a registry, kept in step with the files
    on disk. ``run(tree, inputs, trace)`` is how a run happens (a service
    gives its own, so gaps are counted and delegations route); ``after``
    is told about each fire."""

    def __init__(self, registry: Any, run: Callable[[Tree, dict[str, Any], Trace], Outcome], *, env: dict[str, Any] | None = None, egress: str = "any", clock: Callable[[], _dt.datetime] | None = None, after: Callable[["Watch", dict[str, Any], Outcome], None] | None = None, note: Callable[[str], None] | None = None) -> None:
        self.registry = registry
        self.run = run
        self.env = env or {}
        self.egress = egress
        self.clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self.after = after or (lambda w, e, o: None)
        self.note = note or (lambda text: None)
        self.watches: dict[str, Watch] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def sync(self) -> tuple[int, int]:
        """Start watches for trees that appeared or changed, stop the ones
        that vanished. Returns (started, stopped)."""
        self.registry.reload()
        wanted: dict[str, Watch] = {}
        for tree in self.registry.trees():
            for i, spec in enumerate(tree.watches):
                w = Watch(self, tree.name, i, spec)
                wanted[w.key] = w
        started = stopped = 0
        with self._lock:
            for key, w in list(self.watches.items()):
                if key not in wanted:
                    w.stop.set()
                    del self.watches[key]
                    stopped += 1
            for key, w in wanted.items():
                if key not in self.watches:
                    self.watches[key] = w
                    w.start()
                    started += 1
        return started, stopped

    def start(self) -> "Watcher":
        self.sync()

        def loop() -> None:
            while not self._stop.wait(SYNC_EVERY):
                try:
                    self.sync()
                except Exception as e:
                    self.note(f"watcher: {e}")

        self._thread = threading.Thread(target=loop, name="pyrtan-watcher", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for w in self.watches.values():
                w.stop.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            watches = [w.to_dict() for w in self.watches.values()]
        watches.sort(key=lambda w: (w["tree"], w["index"]))
        return {"count": len(watches), "watches": watches}

    def __len__(self) -> int:
        return len(self.watches)


def _what(kind: str, spec: dict[str, Any]) -> str:
    if kind == "every":
        return f"every {spec['every']}"
    if kind == "file":
        return str(spec["file"])
    if kind == "queue":
        return f"{spec['queue'].get('topic')} at {spec['queue'].get('url')}"
    return str(spec["serial"].get("device"))


def _summary(kind: str, event: dict[str, Any]) -> str:
    if kind == "every":
        return f"every {event.get('every')} s, run {event.get('n')}"
    if kind == "file":
        return f"{event.get('path')}: {len(event.get('lines') or [])} new line(s)"
    if kind == "queue":
        return f"{event.get('topic')}: {len(str(event.get('body') or ''))} B"
    return f"{event.get('device')}: {str(event.get('body') or '')[:60]}"


def _observed(event: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in event.items() if k != "lines"}
    if isinstance(out.get("body"), str) and len(out["body"]) > 200:
        out["body"] = out["body"][:200] + "…"
    return out


def _now_scope(now: _dt.datetime) -> dict[str, Any]:
    from .runtime import _now

    return _now(now)
