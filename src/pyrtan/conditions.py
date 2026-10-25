"""Conditions for ``decide`` branches.

A condition is a small table with one or more tests; all tests must hold.
Every test is a plain comparison a reader can check by eye, which is the
point: the branch taken is always explainable from the observed value and
the condition text.

    { eq = 200 }
    { gte = 500 }
    { matches = "^https://" }
    { contains = "error" }
    { in = ["open", "pending"] }
    { exists = true }
    { empty = false }
    { between = [200, 299] }
    { type = "list" }
"""

from __future__ import annotations

import re
from typing import Any

from .template import MISSING

KNOWN = {"eq", "ne", "lt", "lte", "gt", "gte", "matches", "contains", "in", "exists", "empty", "between", "type", "startswith", "endswith"}

TYPE_NAMES = {
    "str": str,
    "string": str,
    "int": int,
    "float": float,
    "number": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
    "map": dict,
    "null": type(None),
    "none": type(None),
}


class ConditionError(ValueError):
    pass


def validate(cond: Any) -> None:
    if not isinstance(cond, dict) or not cond:
        raise ConditionError("a condition is a non-empty table")
    unknown = set(cond) - KNOWN
    if unknown:
        raise ConditionError(f"unknown condition keys: {sorted(unknown)}")
    if "matches" in cond:
        try:
            re.compile(cond["matches"])
        except re.error as e:
            raise ConditionError(f"bad regex in matches: {e}") from e
    if "between" in cond and (not isinstance(cond["between"], list) or len(cond["between"]) != 2):
        raise ConditionError("between takes a two-item list")
    if "type" in cond and cond["type"] not in TYPE_NAMES:
        raise ConditionError(f"unknown type {cond['type']!r}; one of {sorted(TYPE_NAMES)}")
    if "in" in cond and not isinstance(cond["in"], list):
        raise ConditionError("in takes a list")


def holds(cond: dict[str, Any], value: Any) -> bool:
    """True when every test in ``cond`` holds for ``value``."""
    for key, expected in cond.items():
        if not _test(key, expected, value):
            return False
    return True


def _is_empty(value: Any) -> bool:
    if value is MISSING or value is None:
        return True
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) == 0
    return False


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _test(key: str, expected: Any, value: Any) -> bool:
    if key == "exists":
        return (value is not MISSING and value is not None) == bool(expected)
    if key == "empty":
        return _is_empty(value) == bool(expected)
    if value is MISSING:
        return False
    if key == "eq":
        return _loose_eq(value, expected)
    if key == "ne":
        return not _loose_eq(value, expected)
    if key in ("lt", "lte", "gt", "gte"):
        a, b = _num(value), _num(expected)
        if a is None or b is None:
            return False
        return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[key]
    if key == "between":
        a, lo, hi = _num(value), _num(expected[0]), _num(expected[1])
        if a is None or lo is None or hi is None:
            return False
        return lo <= a <= hi
    if key == "matches":
        return value is not None and re.search(expected, str(value)) is not None
    if key == "contains":
        if isinstance(value, (list, tuple)):
            return any(_loose_eq(item, expected) for item in value)
        if isinstance(value, dict):
            return expected in value
        return str(expected) in str(value)
    if key == "startswith":
        return str(value).startswith(str(expected))
    if key == "endswith":
        return str(value).endswith(str(expected))
    if key == "in":
        return any(_loose_eq(value, option) for option in expected)
    if key == "type":
        return isinstance(value, TYPE_NAMES[expected]) and not (expected in ("int", "number", "float") and isinstance(value, bool))
    raise ConditionError(f"unknown condition key {key!r}")


def _loose_eq(a: Any, b: Any) -> bool:
    """Equality that lets ``"200"`` match ``200`` but keeps bools strict."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or (isinstance(a, bool) and isinstance(b, bool) and a == b)
    if a == b:
        return True
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None and (isinstance(a, str) or isinstance(b, str)):
        return na == nb
    return False
