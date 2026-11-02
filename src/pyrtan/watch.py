"""Watches: a tree that runs when something speaks, without being asked.

A tree may carry ``[[watches]]``: a clock (``every = "5m"``), a calendar
(``at = "mon-fri 09:00"``, with ``tz = "Europe/Oslo"``; see
:mod:`pyrtan.schedule`), a file
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

Each watch also keeps a *timeline*: what its runs came to over the last
month, as spans of runs that ended the same way (the same status and the
same output). A device watched every minute that stays up for a week is
one span; each change starts another. A clock watch's spans abut when the
watch kept time (the change happened between two runs); a gap longer than
two and a half intervals (the service was down) is left as a gap, which a
reader shows as unknown. A calendar watch kept time when no slot of its
calendar passed between two runs: a slot passed by while the service was
down is a gap too, however long the calendar's own gaps (a weekend, a
month) are. The newest span holds until ``holds_until``: two and a half
intervals past its last run, or a calendar's next slot and
:data:`CALENDAR_GRACE` after it; then it is unknown too. :class:`Timelines`
keeps them, in a JSON file when the service is given one
(``--watch-history``), and ``GET /watches/history`` serves them; the
console's Devices tab draws uptime from them.

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
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import adapters, schedule
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
TIMELINE_DAYS = 31     # how far back a timeline reaches
TIMELINE_SPANS = 5000  # spans kept per watch, at most
TIMELINE_OUTPUT = 2048 # an output larger than this (as JSON) is compared, not kept
TIMELINE_SAVE = 60.0   # seconds between saves while nothing changes
CONTIGUOUS = 2.5       # intervals between two runs of a clock watch that still count as one stretch
CALENDAR_GRACE = 300.0 # seconds past a calendar's slot that its last outcome still holds, while the slot's run runs


class Timelines:
    """What each watch's runs came to, over time: per watch, a list of
    spans ``{from, to, n, status, output, h}`` (``h`` a digest of status and
    output, ``n`` the runs it holds). Kept in ``path`` when given, written
    whole and atomically: at once when a span starts, else at most every
    minute, so a restart loses at most a minute of a span's end."""

    def __init__(self, path: str | Path | None = None, note: Callable[[str], None] | None = None) -> None:
        self.path = Path(path) if path else None
        self.note = note or (lambda text: None)
        self.data: dict[str, list[dict[str, Any]]] = {}
        self.error: str | None = None
        self._saved = 0.0
        self._lock = threading.Lock()
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("timelines"), dict):
                    self.data = {str(k): [s for s in v if isinstance(s, dict)] for k, v in raw["timelines"].items() if isinstance(v, list)}
            except (OSError, ValueError) as e:
                self.error = f"{self.path}: {e}"
                self.note(f"watch history: {self.error}; starting afresh")

    def record(self, key: str, at: float, status: str, output: Any, interval: float | None, missed: Callable[[float], bool] | None = None) -> None:
        """One run. It continues the last span when nothing was missed since
        that span's end: ``missed(end)`` says so for a calendar (did a slot
        pass with no run?); otherwise a clock watch's ``interval`` does
        (more than two and a half of them is a gap), and an event watch,
        with neither, is never late."""
        sig = json.dumps([status, output], sort_keys=True, ensure_ascii=False, default=str)
        h = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            spans = self.data.setdefault(key, [])
            last = spans[-1] if spans else None
            if last is None:
                contiguous = False
            elif missed is not None:
                contiguous = not missed(float(last["to"]))
            else:
                contiguous = interval is None or at - float(last["to"]) <= CONTIGUOUS * interval
            started = False
            if last is not None and contiguous and last.get("h") == h:
                last["to"] = at
                last["n"] = int(last.get("n", 1)) + 1
            else:
                span: dict[str, Any] = {"from": float(last["to"]) if contiguous and last is not None else at, "to": at, "n": 1, "status": status, "h": h}
                if len(sig) <= TIMELINE_OUTPUT:
                    span["output"] = output
                spans.append(span)
                started = True
            cutoff = at - TIMELINE_DAYS * 86400
            while spans and float(spans[0]["to"]) < cutoff:
                spans.pop(0)
            del spans[:-TIMELINE_SPANS]
            if started or at - self._saved >= TIMELINE_SAVE:
                self._save(at)

    def spans(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self.data.get(key, [])]

    def _save(self, at: float) -> None:
        self._saved = at
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"pyrtan-watch-history": 1, "timelines": self.data}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            self.error = None
        except OSError as e:
            if self.error is None:
                self.note(f"watch history: {self.path}: {e.strerror or e}; kept in memory only")
            self.error = f"{self.path}: {e.strerror or e}"


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
        self.kind = next(k for k in ("every", "at", "file", "queue", "serial") if k in spec)
        self.name = str(spec.get("name") or f"{tree_name}#{index}")
        self.key = json.dumps([tree_name, index, spec], sort_keys=True, default=str)
        self.timeline = f"{tree_name}/{self.name}"   # outlives an edit to the watch, so its history does too
        self.interval = every_seconds(spec["every"]) if self.kind == "every" else None
        self.calendar: tuple[schedule.Schedule, _dt.tzinfo] | None = None
        if self.kind == "at":
            try:
                self.calendar = (schedule.parse_at(spec["at"]), schedule.zone(spec.get("tz")))
            except (schedule.ScheduleError, ValueError):
                pass  # pyrtan check refuses it; _at says so when it runs
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
        self.next_at: float | None = None  # a calendar watch: when it runs next
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
                elif self.kind == "at":
                    self._at()
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

    def _at(self) -> None:
        when = schedule.parse_at(self.spec["at"])
        tz = schedule.zone(self.spec.get("tz"))
        n = 0
        after = self.watcher.clock()
        while not self.stop.is_set():
            due = schedule.next_after(when, after, tz)
            with self.lock:
                self.next_at = due.timestamp()
            # Wait in short steps against the clock, so a clock that jumps
            # (a laptop that slept) is noticed within a minute.
            while (now := self.watcher.clock()) < due:
                if self.stop.wait(min(60.0, (due - now).total_seconds())):
                    return
            n += 1
            self.fire({"kind": "at", "n": n, "at": due.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "slot": schedule.slot_name(due, tz), "tz": str(self.spec.get("tz") or "UTC")}, slot=due)
            after = max(due, self.watcher.clock())  # a slot missed while running is not made up

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

    def slot_after(self, at: float) -> float | None:
        """A calendar watch: its first slot after ``at``, as a timestamp."""
        if self.calendar is None:
            return None
        when, tz = self.calendar
        return schedule.next_after(when, _dt.datetime.fromtimestamp(at, _dt.timezone.utc), tz).timestamp()

    def holds_until(self, spans: list[dict[str, Any]]) -> float | None:
        """Until when the newest span's outcome is taken to hold: a clock
        watch's two and a half intervals past it, a calendar's next slot
        and a grace for that slot's run, an event watch's for good (None
        then, as for no span)."""
        if not spans:
            return None
        to = float(spans[-1]["to"])
        if self.interval:
            return to + CONTIGUOUS * self.interval
        nxt = self.slot_after(to)
        return nxt + CALENDAR_GRACE if nxt is not None else None

    def fire(self, event: dict[str, Any], slot: _dt.datetime | None = None) -> Outcome | None:
        """Run the tree for one event (for a calendar, the ``slot`` it is
        for). Returns the outcome, or None when the tree is gone or its
        inputs do not render."""
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
            last = self.last_outcome
        # A calendar's slots are read on the watcher's clock, so its timeline is too.
        missed = None
        if slot is not None and self.calendar is not None:
            due = slot.timestamp()
            missed = lambda end: (self.slot_after(end) or due) < due - 1  # noqa: E731  a slot between the last run and this one
        self.watcher.timelines.record(self.timeline, self.watcher.clock().timestamp() if missed else now, last.get("status", ""), last.get("output"), self.interval, missed)
        self.watcher.after(self, event, outcome)
        return outcome

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "tree": self.tree_name, "index": self.index, "name": self.name, "kind": self.kind, "what": _what(self.kind, self.spec),
                "fires": self.fires, "errors": self.errors, "last_error": self.last_error, "last_at": self.last_at,
                "last": self.last_outcome, "path": self.last_path, "path_changed_at": self.path_changed_at, "previous_path": self.previous_path,
                "alive": bool(self.thread and self.thread.is_alive()),
                **({"next_at": self.next_at} if self.kind == "at" else {}),
            }


class Watcher:
    """Every watch of every tree in a registry, kept in step with the files
    on disk. ``run(tree, inputs, trace)`` is how a run happens (a service
    gives its own, so gaps are counted and delegations route); ``after``
    is told about each fire."""

    def __init__(self, registry: Any, run: Callable[[Tree, dict[str, Any], Trace], Outcome], *, env: dict[str, Any] | None = None, egress: str = "any", clock: Callable[[], _dt.datetime] | None = None, after: Callable[["Watch", dict[str, Any], Outcome], None] | None = None, note: Callable[[str], None] | None = None, timelines: Timelines | None = None) -> None:
        self.registry = registry
        self.run = run
        self.env = env or {}
        self.egress = egress
        self.clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self.after = after or (lambda w, e, o: None)
        self.note = note or (lambda text: None)
        self.timelines = timelines if timelines is not None else Timelines(note=self.note)
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

    def history(self) -> dict[str, Any]:
        """Each watch's timeline (``GET /watches/history``)."""
        with self._lock:
            current = list(self.watches.values())
        out = []
        for w in current:
            spans = self.timelines.spans(w.timeline)
            out.append({"tree": w.tree_name, "index": w.index, "name": w.name, "kind": w.kind, "every": w.interval, "holds_until": w.holds_until(spans), "spans": spans})
        out.sort(key=lambda w: (w["tree"], w["index"]))
        return {"count": len(out), "days": TIMELINE_DAYS, "watches": out, **({"error": self.timelines.error} if self.timelines.error else {})}

    def __len__(self) -> int:
        return len(self.watches)


def _what(kind: str, spec: dict[str, Any]) -> str:
    if kind == "every":
        return f"every {spec['every']}"
    if kind == "at":
        return f"at {spec['at']}" + (f" ({spec['tz']})" if spec.get("tz") else " (UTC)")
    if kind == "file":
        return str(spec["file"])
    if kind == "queue":
        return f"{spec['queue'].get('topic')} at {spec['queue'].get('url')}"
    return str(spec["serial"].get("device"))


def _summary(kind: str, event: dict[str, Any]) -> str:
    if kind == "every":
        return f"every {event.get('every')} s, run {event.get('n')}"
    if kind == "at":
        return f"at {event.get('slot')} {event.get('tz')}, run {event.get('n')}"
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
