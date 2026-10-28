"""A tree's own tests, and what an edit changes.

A tree may carry ``[[tests]]``: a sentence (or inputs), the recorded
responses to answer it from, and what the run must come to.

    [[tests]]
    name = "rain in Oslo"
    sentence = "weather in Oslo"
    fixture = "weather.fixture.json"     # beside the tree file; pyrtan run --record writes one
    expect = { status = "done", path = ["geocode", "found", "forecast", "have-temp", "sky", "reply"], output = { sky = "raining" } }

Because a run is deterministic given its observations (design.md, invariant
1), a recorded run is a real test: the same tree and the same recording
take the same path every time, so the path itself can be asserted, not
only the answer. :func:`run_case` runs one, :func:`failures` says how it
missed its ``expect``, and ``pyrtan test`` runs them all.

:func:`diff` is the other half. It runs every case both versions of a
tree carry against both versions, against the same recordings, and
reports each case whose outcome or path changed, whatever either
expected: ``pyrtan diff weather.toml`` says what an edit to the file
does to the runs it was tested on, before anyone has to find out.

Nothing here reaches the network: a case answers from its fixture or
from nothing, and a request the fixture did not foresee fails the run.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .registry import Registry
from .runtime import Outcome, Runner
from .transport import RecordedTransport
from .tree import Tree


@dataclass
class Case:
    """One ``[[tests]]`` entry of a tree, with its fixture resolved."""

    tree: str
    index: int
    raw: dict[str, Any]
    base: Path | None = None  # the directory the tree file is in; a fixture is relative to it

    @property
    def name(self) -> str:
        if isinstance(self.raw.get("name"), str) and self.raw["name"]:
            return self.raw["name"]
        if "sentence" in self.raw:
            return repr(self.raw["sentence"])
        return "inputs " + json.dumps(self.raw.get("inputs", {}), ensure_ascii=False, sort_keys=True)

    @property
    def fixture(self) -> Path | None:
        f = self.raw.get("fixture")
        if not isinstance(f, str) or not f:
            return None
        p = Path(f)
        return p if p.is_absolute() or self.base is None else self.base / p

    def key(self) -> str:
        """What makes two versions' cases the same case: the question and the recording, not the expectations."""
        return json.dumps({k: self.raw.get(k) for k in ("sentence", "inputs", "fixture", "now", "env", "answers")}, sort_keys=True, ensure_ascii=False, default=str)


def cases(tree: Tree) -> list[Case]:
    base = Path(tree.source).parent if tree.source else None
    return [Case(tree.name, i, t, base) for i, t in enumerate(tree.tests)]


def parse_now(value: Any) -> _dt.datetime:
    """A case's clock: an ISO date-time (``2026-10-25T12:00:00Z``) or a date, UTC unless it says otherwise."""
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day, tzinfo=_dt.timezone.utc)
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    now = _dt.datetime.fromisoformat(text)
    return now if now.tzinfo else now.replace(tzinfo=_dt.timezone.utc)


def with_tree(others: Iterable[Tree], tree: Tree) -> Registry:
    """A registry holding ``tree`` and, for what it delegates to or walks, every other tree but its namesake."""
    reg = Registry()
    for t in others:
        if t.name != tree.name:
            reg.add(t)
    reg.add(tree)
    return reg


def run_case(case: Case, tree: Tree, registry: Registry) -> Outcome:
    """Run one case of ``tree`` against its fixture, with its clock, its env and its answers."""
    raw = case.raw
    fixture = case.fixture
    if fixture is not None and not fixture.exists():
        return Outcome("error", tree.name, reason="no-fixture", say=f"no fixture at {fixture}; record one with: pyrtan run ... --record {fixture}")
    try:
        transport = RecordedTransport.from_file(fixture) if fixture is not None else RecordedTransport()
    except (OSError, ValueError) as e:
        return Outcome("error", tree.name, reason="no-fixture", say=f"{fixture}: {e}")
    clock = (lambda now=parse_now(raw["now"]): now) if raw.get("now") is not None else (lambda: _dt.datetime.now(_dt.timezone.utc))
    runner = Runner(registry, transport, env={str(k): v for k, v in (raw.get("env") or {}).items()}, clock=clock)
    if "sentence" in raw:
        outcome = runner.run_intent(str(raw["sentence"]))
    else:
        outcome = runner.run_tree(tree.name, dict(raw.get("inputs") or {}))
    for answer in raw.get("answers") or []:
        if outcome.status != "asked" or outcome.snapshot is None:
            break
        outcome = runner.resume(outcome.snapshot, answer)
    return outcome


def path(outcome: Outcome) -> list[str]:
    """The nodes the run walked at its top level, in order: not the reading,
    and not the steps of an ``each`` sub-flow or of a tree it delegated to."""
    if outcome.trace is None:
        return []
    return [s.node for s in outcome.trace.steps if s.depth == 0 and s.kind != "intent"]


def shape(outcome: Outcome) -> dict[str, Any]:
    """What a diff compares: where the run went, how each decision went, and what it came to."""
    steps = [] if outcome.trace is None else [s for s in outcome.trace.steps if s.depth == 0 and s.kind != "intent"]
    return {
        "tree": outcome.tree,
        "status": outcome.status,
        "reason": outcome.reason,
        "say": outcome.say,
        "output": outcome.output,
        "steps": [s.node + (f" [{s.chosen}]" if s.chosen else "") for s in steps],
    }


def failures(case: Case, outcome: Outcome) -> list[str]:
    """How ``outcome`` missed the case's ``expect``; empty when it did not."""
    expect = case.raw.get("expect") or {}
    out: list[str] = []
    if "sentence" in case.raw and outcome.tree != case.tree and outcome.reason not in ("no-match", "ambiguous"):
        out.append(f"the sentence went to {outcome.tree or 'no tree'}, not {case.tree}")
    if "status" in expect and outcome.status != expect["status"]:
        out.append(f"status: expected {expect['status']!r}, got {outcome.status!r}" + (f" ({outcome.reason}: {outcome.say})" if outcome.status == "error" else ""))
    if "reason" in expect and outcome.reason != expect["reason"]:
        out.append(f"reason: expected {expect['reason']!r}, got {outcome.reason!r}")
    if "say" in expect and outcome.say != expect["say"]:
        out.append(f"say: expected {expect['say']!r}, got {outcome.say!r}")
    if "say_contains" in expect and str(expect["say_contains"]) not in (outcome.say or ""):
        out.append(f"say: expected it to contain {expect['say_contains']!r}, got {outcome.say!r}")
    if "output" in expect:
        miss = _output_miss(expect["output"], outcome.output, "output")
        if miss:
            out.append(miss)
    if "path" in expect and path(outcome) != list(expect["path"]):
        out.append(f"path: expected {' -> '.join(expect['path'])}, got {' -> '.join(path(outcome)) or 'nothing'}")
    return out


def _output_miss(expected: Any, got: Any, where: str) -> str | None:
    """A table expects at least its keys, each as given; anything else expects itself."""
    if isinstance(expected, dict):
        if not isinstance(got, dict):
            return f"{where}: expected a table, got {_brief(got)}"
        for k, v in expected.items():
            if k not in got:
                return f"{where}.{k}: expected {_brief(v)}, got nothing"
            miss = _output_miss(v, got[k], f"{where}.{k}")
            if miss:
                return miss
        return None
    if expected != got:
        return f"{where}: expected {_brief(expected)}, got {_brief(got)}"
    return None


def _brief(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- two versions


@dataclass
class Change:
    """A case whose run is not what it was."""

    case: str
    before: dict[str, Any]
    after: dict[str, Any]
    fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"case": self.case, "changed": self.fields, "before": self.before, "after": self.after}

    def render(self) -> str:
        lines = [f"changed  {self.case}"]
        for f in self.fields:
            if f == "steps":
                lines.append(f"    path before: {' -> '.join(self.before['steps']) or 'nothing'}")
                lines.append(f"    path after:  {' -> '.join(self.after['steps']) or 'nothing'}")
            else:
                lines.extend(f"    {where}: {_brief(a)} -> {_brief(b)}" for where, a, b in _changes(self.before[f], self.after[f], f))
        return "\n".join(lines)


def _changes(before: Any, after: Any, where: str) -> list[tuple[str, Any, Any]]:
    """Where two values differ: key by key through tables, whole otherwise."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[tuple[str, Any, Any]] = []
        for k in list(before) + [k for k in after if k not in before]:
            if before.get(k, _ABSENT) != after.get(k, _ABSENT):
                out.extend(_changes(before.get(k, _ABSENT), after.get(k, _ABSENT), f"{where}.{k}"))
        return out
    return [(where, None if before is _ABSENT else before, None if after is _ABSENT else after)]


_ABSENT = object()


@dataclass
class Diff:
    tree: str
    same: list[str] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"tree": self.tree, "cases": len(self.same) + len(self.changed), "same": self.same, "changed": [c.to_dict() for c in self.changed]}


def diff(old: Tree, new: Tree, others: Iterable[Tree] = ()) -> Diff:
    """Every case either version carries, run on both against the same
    recordings (the new version's directory), compared by outcome and path."""
    others = list(others)
    new_cases = cases(new)
    base = new_cases[0].base if new_cases else (Path(new.source).parent if new.source else None)
    seen: dict[str, Case] = {}
    for c in new_cases + [Case(old.name, c.index, c.raw, base) for c in cases(old)]:
        seen.setdefault(c.key(), c)
    result = Diff(new.name)
    old_reg, new_reg = with_tree(others, old), with_tree(others, new)
    for c in seen.values():
        before, after = shape(run_case(c, old, old_reg)), shape(run_case(c, new, new_reg))
        fields = [k for k in ("status", "reason", "say", "output", "steps") if before[k] != after[k]]
        if fields:
            result.changed.append(Change(c.name, before, after, fields))
        else:
            result.same.append(c.name)
    return result
