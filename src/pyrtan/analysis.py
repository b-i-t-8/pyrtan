"""What ``pyrtan check`` can say about a decision without running it.

A ``decide`` takes the first branch whose condition holds, and a ``lookup``
reads the first row whose condition holds. Three things about that order
can be known from the file alone:

- a branch that can never be taken, because every value it holds for is
  taken by a branch before it (``between = [500, 599]`` after ``gte = 400``);
- a condition that holds for no value at all (``{ gt = 5, lt = 3 }``,
  ``between = [9, 1]``);
- with no ``else`` (or ``default``), the numbers no branch covers: the
  holes a gap will fall into (``3 < x < 45``), before a run finds them.

Only the comparisons with a plain meaning are modelled: ``eq``, ``in``,
``lt``, ``lte``, ``gt``, ``gte``, ``between``. A value is read the way
:mod:`pyrtan.conditions` reads it, numbers loosely (``"200"`` is 200) and
booleans strictly, so a set here is a set of numbers (intervals) and of
other values (points). Any other test (``matches``, ``contains``,
``type``, ``exists``, ``ne``…) is unknown, and the analysis stays sound
around it: a condition is never called empty, and a branch never called
dead, unless that is true of every value. It may miss some; it never
invents one.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

INF = math.inf
NUMERIC = ("lt", "lte", "gt", "gte", "between")
KNOWN = ("eq", "in") + NUMERIC


@dataclass(frozen=True)
class Interval:
    lo: float
    lo_in: bool
    hi: float
    hi_in: bool

    def empty(self) -> bool:
        return self.lo > self.hi or (self.lo == self.hi and not (self.lo_in and self.hi_in))


@dataclass(frozen=True)
class Values:
    """A set of values: numbers as disjoint sorted intervals, anything else as points."""

    nums: tuple[Interval, ...] = ()
    points: frozenset = frozenset()

    def empty(self) -> bool:
        return not self.nums and not self.points

    def __or__(self, other: "Values") -> "Values":
        return Values(_merge(self.nums + other.nums), self.points | other.points)

    def __and__(self, other: "Values") -> "Values":
        out = []
        for a in self.nums:
            for b in other.nums:
                lo, lo_in = max((a.lo, a.lo_in), (b.lo, b.lo_in), key=lambda e: (e[0], not e[1]))
                hi, hi_in = min((a.hi, a.hi_in), (b.hi, b.hi_in), key=lambda e: (e[0], e[1]))
                i = Interval(lo, lo_in, hi, hi_in)
                if not i.empty():
                    out.append(i)
        return Values(_merge(out), self.points & other.points)

    def __sub__(self, other: "Values") -> "Values":
        nums = list(self.nums)
        for b in other.nums:
            nxt = []
            for a in nums:
                left = Interval(a.lo, a.lo_in, b.lo, not b.lo_in) if b.lo > -INF else None
                right = Interval(b.hi, not b.hi_in, a.hi, a.hi_in) if b.hi < INF else None
                if a.hi < b.lo or (a.hi == b.lo and not (a.hi_in and b.lo_in)) or a.lo > b.hi or (a.lo == b.hi and not (a.lo_in and b.hi_in)):
                    nxt.append(a)  # no overlap
                    continue
                for part in (left, right):
                    if part is None:
                        continue
                    clipped = (Values((part,)) & Values((a,))).nums
                    nxt.extend(clipped)
            nums = nxt
        return Values(_merge(nums), self.points - other.points)

    def within(self, other: "Values") -> bool:
        return (self - other).empty()


NOTHING = Values()
NUMBERS = Values((Interval(-INF, False, INF, False),))


def _merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    items = sorted((i for i in intervals if not i.empty()), key=lambda i: (i.lo, not i.lo_in))
    out: list[Interval] = []
    for i in items:
        if out:
            last = out[-1]
            touches = i.lo < last.hi or (i.lo == last.hi and (i.lo_in or last.hi_in))
            if touches:
                if (i.hi, i.hi_in) > (last.hi, last.hi_in):
                    out[-1] = Interval(last.lo, last.lo_in, i.hi, i.hi_in)
                continue
        out.append(i)
    return tuple(out)


def _num(value: Any) -> float | None:
    """A number the way conditions.py reads one: not a bool; a numeric string counts."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            n = float(value.strip())
        except ValueError:
            return None
        return None if math.isnan(n) else n
    return None


def _point(value: Any) -> Values:
    """The values ``eq = value`` holds for."""
    n = _num(value)
    if n is not None:
        return Values((Interval(n, True, n, True),))
    if isinstance(value, bool):
        return Values(points=frozenset({("bool", value)}))
    if isinstance(value, str):
        return Values(points=frozenset({("str", value)}))
    return Values(points=frozenset({("json", json.dumps(value, sort_keys=True, default=str))}))


def key_values(key: str, expected: Any) -> Values | None:
    """The exact set one test holds for, or None when it is not modelled."""
    if key == "eq":
        return _point(expected)
    if key == "in":
        if not isinstance(expected, list):
            return None
        out = NOTHING
        for v in expected:
            out = out | _point(v)
        return out
    if key == "between":
        if not isinstance(expected, list) or len(expected) != 2:
            return None
        lo, hi = _num(expected[0]), _num(expected[1])
        return NOTHING if lo is None or hi is None else Values(_merge([Interval(lo, True, hi, True)]))
    if key in ("lt", "lte", "gt", "gte"):
        n = _num(expected)
        if n is None:
            return NOTHING  # conditions.py: a comparison with no number holds for nothing
        return Values((
            {"lt": Interval(-INF, False, n, False), "lte": Interval(-INF, False, n, True),
             "gt": Interval(n, False, INF, False), "gte": Interval(n, True, INF, False)}[key],
        ))
    return None


def exact(cond: dict[str, Any]) -> Values | None:
    """What a condition holds for, when every test in it is modelled; None otherwise."""
    out: Values | None = None
    for k, v in cond.items():
        s = key_values(k, v)
        if s is None:
            return None
        out = s if out is None else out & s
    return out


def over(cond: dict[str, Any]) -> Values | None:
    """A set the condition's values lie within (the modelled tests alone); None when none is modelled."""
    out: Values | None = None
    for k, v in cond.items():
        s = key_values(k, v)
        if s is not None:
            out = s if out is None else out & s
    return out


@dataclass
class Finding:
    index: int            # which branch or row
    kind: str             # "never-holds" or "shadowed"
    by: list[int]         # for "shadowed": the earlier branches or rows that take its values


def findings(conds: list[dict[str, Any]]) -> list[Finding]:
    """The branches (or rows), in order, that hold for nothing or are taken by earlier ones."""
    out: list[Finding] = []
    covered = NOTHING
    exacts: list[Values | None] = []
    for i, cond in enumerate(conds):
        o = over(cond)
        if o is not None and o.empty():
            out.append(Finding(i, "never-holds", []))
        elif o is not None and o.within(covered):
            by = [j for j, e in enumerate(exacts) if e is not None and not (e & o).empty()]
            out.append(Finding(i, "shadowed", by))
        e = exact(cond)
        exacts.append(e)
        if e is not None:
            covered = covered | e
    return out


def number_holes(conds: list[dict[str, Any]]) -> list[Interval] | None:
    """With no else: the numbers no branch takes, when every branch is a plain
    numeric comparison (so the holes say something); None otherwise."""
    covered = NOTHING
    for cond in conds:
        e = exact(cond)
        if e is None or e.points:
            return None
        covered = covered | e
    if covered.empty():
        return None
    return list((NUMBERS - covered).nums)


def holes_in_words(conds: list[dict[str, Any]]) -> str | None:
    """The holes as a reader would say them. When every bound the conditions
    name is a whole number, the values are most likely whole numbers too, so
    the holes are said as whole numbers (``below 0, 4 to 44, above 48``) and
    labelled so; otherwise as intervals (``3.5 < x < 4``)."""
    holes = number_holes(conds)
    if not holes:
        return None
    bounds = [b for c in conds for b in _bounds(c)]
    if bounds and all(float(b).is_integer() for b in bounds):
        words = _whole(holes)
        return f"whole numbers {words}" if words else None
    return describe(holes)


def _bounds(cond: dict[str, Any]) -> list[float]:
    out: list[float] = []
    for k, v in cond.items():
        for x in (v if isinstance(v, list) else [v]):
            n = _num(x)
            if n is not None:
                out.append(n)
    return out


def _whole(intervals: list[Interval], limit: int = 6) -> str:
    ranges: list[list[float]] = []
    for i in intervals:
        lo = -INF if i.lo == -INF else (math.floor(i.lo) + 1 if (i.lo.is_integer() and not i.lo_in) else math.ceil(i.lo))
        hi = INF if i.hi == INF else (math.ceil(i.hi) - 1 if (i.hi.is_integer() and not i.hi_in) else math.floor(i.hi))
        if lo > hi:
            continue  # a hole with no whole number in it
        if ranges and ranges[-1][1] + 1 >= lo:
            ranges[-1][1] = max(ranges[-1][1], hi)  # 0 and below, and 1: below 2
        else:
            ranges.append([lo, hi])
    words = []
    for lo, hi in ranges:
        if lo == -INF:
            words.append(f"below {_fmt(hi + 1)}")
        elif hi == INF:
            words.append(f"above {_fmt(lo - 1)}")
        elif lo == hi:
            words.append(_fmt(lo))
        else:
            words.append(f"{_fmt(lo)} to {_fmt(hi)}")
    if len(words) > limit:
        words = words[:limit] + [f"and {len(words) - limit} more"]
    return ", ".join(words)


def describe(intervals: list[Interval], limit: int = 6) -> str:
    """``x < 0, 3 < x < 45, x > 99``: a reader's words for a few intervals."""
    words = []
    for i in intervals[:limit]:
        lo, hi = _fmt(i.lo), _fmt(i.hi)
        if i.lo == i.hi:
            words.append(f"x = {lo}")
        elif i.lo == -INF:
            words.append(f"x {'<=' if i.hi_in else '<'} {hi}")
        elif i.hi == INF:
            words.append(f"x {'>=' if i.lo_in else '>'} {lo}")
        else:
            words.append(f"{lo} {'<=' if i.lo_in else '<'} x {'<=' if i.hi_in else '<'} {hi}")
    if len(intervals) > limit:
        words.append(f"and {len(intervals) - limit} more")
    return ", ".join(words)


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else repr(n)
