"""What a run did, step by step.

A trace is the product that distinguishes a decision tree from a model: every
observation is recorded, every branch names the condition that chose it, and
the whole thing is plain data a person or another service can read.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Step:
    node: str
    kind: str
    summary: str
    observed: Any = None
    chosen: str | None = None
    next: str | None = None
    elapsed_ms: int = 0
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["observed"] is None:
            del d["observed"]
        if d["chosen"] is None:
            del d["chosen"]
        return d


@dataclass
class Trace:
    tree: str
    steps: list[Step] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    def gap(self, node: str, on: str, observed: Any) -> None:
        """Record a decision whose observed value matched no branch. Gaps are
        how a tree grows: each one is a branch an author has not written yet."""
        self.gaps.append({"tree": self.tree, "node": node, "on": on, "observed": _short(observed)})

    def to_dict(self) -> dict[str, Any]:
        return {"tree": self.tree, "started": self.started, "steps": [s.to_dict() for s in self.steps], "gaps": self.gaps}

    def render(self) -> str:
        lines = [f"trace: {self.tree}"]
        for s in self.steps:
            pad = "  " * (s.depth + 1)
            arrow = f" -> {s.next}" if s.next else ""
            chosen = f" [{s.chosen}]" if s.chosen else ""
            lines.append(f"{pad}{s.node} ({s.kind}){chosen}: {s.summary}{arrow}")
        for g in self.gaps:
            lines.append(f"  gap: {g['node']} saw {g['on']} = {json.dumps(g['observed'], ensure_ascii=False)} and had no branch for it")
        return "\n".join(lines)


def _short(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > limit:
            return text[:limit] + "…"
    return value
