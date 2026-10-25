"""Where a service keeps its gaps.

A gap is a decision that met a value no branch covered. The runtime records
one on the trace; the service collects them here so an author can read the
list and write the missing branches. Without a path the store lives in
memory and is lost with the process, which is enough to try the loop (run,
see the gap, add the branch, reload). With a path it appends every new gap
to a JSON-lines file and reads the file back on start, so a fleet's gaps
outlive restarts and a hub can collect them later.

The same gap seen twice is one entry with a count: the list is what to
write next, not a log of every run.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable


def _key(gap: dict[str, Any]) -> str:
    return json.dumps([gap.get("tree"), gap.get("node"), gap.get("on"), gap.get("observed")], sort_keys=True, ensure_ascii=False, default=str)


class GapStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.error: str | None = None  # the last failure to write the file; a run never fails over it
        self._gaps: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        if self.path is not None and self.path.exists():
            try:
                self._load()
            except OSError as e:
                self.error = str(e)
                print(f"pyrtan: cannot read gaps from {self.path}: {e}; starting empty", file=sys.stderr)

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                gap = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(gap, dict) and "tree" in gap:
                self._merge(gap, persist=False)

    def _merge(self, gap: dict[str, Any], *, persist: bool) -> None:
        key = _key(gap)
        now = time.time()
        current = self._gaps.get(key)
        if current is None:
            entry = {
                "tree": gap.get("tree"),
                "node": gap.get("node"),
                "on": gap.get("on"),
                "observed": gap.get("observed"),
                "count": int(gap.get("count", 1)),
                "first_seen": gap.get("first_seen", now),
                "last_seen": gap.get("last_seen", now),
            }
            self._gaps[key] = entry
        else:
            current["count"] += int(gap.get("count", 1))
            current["last_seen"] = max(current["last_seen"], gap.get("last_seen", now))
            entry = current
        if persist and self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({**entry, "count": 1, "last_seen": now}, ensure_ascii=False, default=str) + "\n")
                self.error = None
            except OSError as e:
                if self.error is None:
                    print(f"pyrtan: cannot write gaps to {self.path}: {e}; keeping them in memory", file=sys.stderr)
                self.error = str(e)

    def add(self, gaps: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            for gap in gaps:
                self._merge(gap, persist=True)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(g) for g in self._gaps.values()), key=lambda g: (-g["count"], g["last_seen"]))

    def __len__(self) -> int:
        return len(self._gaps)
