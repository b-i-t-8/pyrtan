"""``{{ path }}`` interpolation against the run's state.

Templates are deliberately tiny. A placeholder is a dotted path into the
state, optionally followed by one or more filters::

    {{ inputs.city }}
    {{ state.resp.json.results[0].name }}
    {{ inputs.city | url }}
    {{ state.count | int }}
    {{ state.rows | pluck:qty | sum }}
    {{ state.temp | c_to_f | round:1 }}

A few filters take one fixed argument after a colon: a number of places
(``round:1``, ``percent:0``) or a key (``pluck:price``, ``sort:name``).
Arguments are literals, never paths: a filter cannot read the state.

There is no logic in templates. Branching belongs to ``decide`` nodes, where
it is visible in the trace; a template that could branch would hide it.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
PATH_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_\-]*)|\[(-?\d+)\]|\[\"([^\"]*)\"\]|\['([^']*)'\]")


class TemplateError(ValueError):
    pass


class Missing:
    """Sentinel for a path that does not resolve."""

    def __repr__(self) -> str:
        return "<missing>"


MISSING = Missing()


def resolve(path: str, scope: dict[str, Any]) -> Any:
    """Resolve a dotted path like ``state.resp.json.items[0].title``.

    Returns :data:`MISSING` when any step does not exist, rather than raising,
    so that ``exists`` conditions can be expressed.
    """
    pos = 0
    cur: Any = scope
    path = path.strip()
    if not path:
        return MISSING
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = PATH_TOKEN.match(path, pos)
        if not m:
            raise TemplateError(f"bad path {path!r} at offset {pos}")
        pos = m.end()
        name, index, key_dq, key_sq = m.groups()
        if name is not None:
            cur = _get_key(cur, name)
        elif index is not None:
            cur = _get_index(cur, int(index))
        else:
            cur = _get_key(cur, key_dq if key_dq is not None else key_sq)
        if cur is MISSING:
            return MISSING
    return cur


def _get_key(cur: Any, key: str) -> Any:
    if isinstance(cur, dict):
        return cur.get(key, MISSING)
    if isinstance(cur, list) and key.isdigit():
        return _get_index(cur, int(key))
    return MISSING


def _get_index(cur: Any, index: int) -> Any:
    if isinstance(cur, (list, tuple, str)):
        try:
            return cur[index]
        except IndexError:
            return MISSING
    return MISSING


def _text(value: Any) -> str:
    """A value as it reads inside a sentence: a list or table as JSON, the rest as itself."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ------------------------------------------------------------ numbers
# The number filters give the same answer in the JavaScript runtime, digit
# for digit: rounding works on a float's shortest decimal form (half away
# from zero), sums add left to right, and a whole result is an int.

NUMBER_TEXT = re.compile(r"\s*-?\d+(?:\.\d+)?\s*")
FLOAT_FORM = re.compile(r"(-?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?")
MAX_PLACES = 12
KEY = re.compile(r"[A-Za-z0-9_-]+")


def _number(v: Any, name: str) -> int | float:
    """A number, or a string that is one (``"12.5"``, as JSON APIs often send)."""
    if isinstance(v, str) and NUMBER_TEXT.fullmatch(v):
        t = v.strip()
        return float(t) if "." in t else int(t)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        raise TemplateError(f"{name}: {_text(v)!r} is not a number")
    return v


def _whole(x: int | float) -> int | float:
    return int(x) if isinstance(x, float) and x.is_integer() and abs(x) < 2**53 else x


def round_text(x: int | float, places: int) -> str:
    """``x`` to ``places`` decimals, half away from zero, as text with no
    trailing zeros: 2.25 -> "2.3", 2.0 -> "2", -0.04 -> "0"."""
    if isinstance(x, int):
        return str(x)
    m = FLOAT_FORM.fullmatch(repr(x))
    if m is None:
        raise TemplateError(f"{x!r} is not a number")
    sign, whole, frac, exp = m.group(1), m.group(2), m.group(3) or "", int(m.group(4) or 0)
    digits, point = whole + frac, len(whole) + exp
    if point < 0:
        digits, point = "0" * -point + digits, 0
    digits = digits.ljust(point, "0")
    keep = point + places
    if keep < len(digits):
        kept = str(int(digits[:keep] or "0") + (1 if digits[keep] >= "5" else 0))
    else:
        kept = digits.ljust(keep, "0")
    kept = kept.rjust(places + 1, "0")
    head, tail = kept[: len(kept) - places].lstrip("0") or "0", kept[len(kept) - places:].rstrip("0")
    text = head + ("." + tail if tail else "")
    return text if text == "0" else sign + text


def _rounded(v: Any, places: int) -> int | float:
    x = _number(v, "round")
    if abs(x) >= 2**53:
        return _whole(x)  # a float this large is already whole
    text = round_text(x, places)
    return float(text) if "." in text else int(text)


def _percent(v: Any, places: int) -> str:
    return round_text(_number(v, "percent") * 100, places) + "%"


BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _bytes(v: Any) -> str:
    """A size in bytes as people read it: 512 B, 1.5 KiB, 3 GiB (powers of 1024)."""
    n: int | float = _number(v, "bytes")
    i = 0
    while abs(n) >= 1024 and i < len(BYTE_UNITS) - 1:
        n, i = n / 1024, i + 1
    text = round_text(n, 0 if i == 0 else 1)
    if text.lstrip("-") == "1024" and i < len(BYTE_UNITS) - 1:
        text, i = ("-1" if text.startswith("-") else "1"), i + 1
    return f"{text} {BYTE_UNITS[i]}"


# Unit conversions: a fixed table, each one formula, so both runtimes agree.
CONVERSIONS: dict[str, Any] = {
    "c_to_f": lambda x: x * 9 / 5 + 32,
    "f_to_c": lambda x: (x - 32) * 5 / 9,
    "km_to_mi": lambda x: x / 1.609344,
    "mi_to_km": lambda x: x * 1.609344,
    "kg_to_lb": lambda x: x / 0.45359237,
    "lb_to_kg": lambda x: x * 0.45359237,
    "m_to_ft": lambda x: x / 0.3048,
    "ft_to_m": lambda x: x * 0.3048,
}


def _list(v: Any, name: str) -> list[Any]:
    if not isinstance(v, (list, tuple)):
        raise TemplateError(f"{name} needs a list, not {_text(v)!r}")
    return list(v)


def _sum(v: Any) -> int | float:
    total: int | float = 0
    for x in _list(v, "sum"):
        total = total + _number(x, "sum")
    return _whole(total)


def _average(v: Any) -> Any:
    items = _list(v, "average")
    return _whole(_sum(items) / len(items)) if items else MISSING


def _extreme(v: Any, name: str) -> Any:
    numbers = [_number(x, name) for x in _list(v, name)]
    if not numbers:
        return MISSING
    best = numbers[0]
    for n in numbers[1:]:
        if (n < best) if name == "min" else (n > best):
            best = n
    return _whole(best)


def _sorted(v: Any, key: str | None) -> list[Any]:
    items = _list(v, "sort")
    if key is None:
        values = items
    else:
        if not all(isinstance(x, dict) and key in x for x in items):
            raise TemplateError(f"sort:{key} needs a list of tables that all have {key!r}")
        values = [x[key] for x in items]
    # Numbers (or strings that are numbers, as the number filters read them)
    # sort as numbers; otherwise words sort as words.
    try:
        values = [_number(x, "sort") for x in values]
    except TemplateError:
        if not all(isinstance(x, str) for x in values):
            raise TemplateError("sort needs a list of numbers or a list of words (a list of tables takes sort:key)") from None
    order = sorted(range(len(items)), key=lambda i: values[i])
    return [items[i] for i in order]


def _pluck(v: Any, key: str) -> list[Any]:
    return [x[key] for x in _list(v, "pluck") if isinstance(x, dict) and key in x]


# A filter's argument: what it is, whether it must be given.
ARGS: dict[str, tuple[str, bool]] = {"round": ("places", False), "percent": ("places", False), "sort": ("key", False), "pluck": ("key", True)}


FILTERS = {
    "url": lambda v: urllib.parse.quote(str(v), safe=""),
    "json": lambda v: json.dumps(v, ensure_ascii=False),
    "int": lambda v: int(float(v)),
    "float": lambda v: float(v),
    "str": lambda v: str(v),
    "lower": lambda v: str(v).lower(),
    "upper": lambda v: str(v).upper(),
    "strip": lambda v: str(v).strip(),
    "first": lambda v: v[0] if isinstance(v, (list, tuple, str)) and v else MISSING,
    "last": lambda v: v[-1] if isinstance(v, (list, tuple, str)) and v else MISSING,
    "length": lambda v: len(v) if hasattr(v, "__len__") else MISSING,
    "title": lambda v: str(v).title(),
    # A list in a sentence: "a, b, c". Its items read as they would on their own.
    "join": lambda v: ", ".join(_text(x) for x in v) if isinstance(v, (list, tuple)) else _text(v),
    # Lists of numbers, and lists of tables.
    "sum": _sum,
    "average": _average,
    "min": lambda v: _extreme(v, "min"),
    "max": lambda v: _extreme(v, "max"),
    "sort": lambda v, key=None: _sorted(v, key),
    "reverse": lambda v: _list(v, "reverse")[::-1],
    "pluck": _pluck,
    # Numbers as people read them.
    "round": lambda v, places=0: _rounded(v, places),
    "percent": lambda v, places=0: _percent(v, places),
    "bytes": _bytes,
    **{name: (lambda f, n: lambda v: _whole(f(_number(v, n))))(f, name) for name, f in CONVERSIONS.items()},
}


def parse_filter(part: str, expr: str) -> tuple[str, Any]:
    """``round:1`` -> ("round", 1); refuses an unknown filter or a bad argument."""
    name, colon, arg = part.partition(":")
    name, arg = name.strip(), arg.strip()
    if name not in FILTERS:
        raise TemplateError(f"unknown filter {name!r} in {{{{ {expr} }}}}")
    if not colon:
        if name in ARGS and ARGS[name][1]:
            raise TemplateError(f"{name} needs a {ARGS[name][0]}: {name}:{ARGS[name][0]} in {{{{ {expr} }}}}")
        return name, None
    if name not in ARGS:
        raise TemplateError(f"{name} takes no argument in {{{{ {expr} }}}}")
    if ARGS[name][0] == "places":
        if not arg.isdigit() or int(arg) > MAX_PLACES:
            raise TemplateError(f"{name}:{arg}: the places are a whole number from 0 to {MAX_PLACES}, in {{{{ {expr} }}}}")
        return name, int(arg)
    if not KEY.fullmatch(arg):
        raise TemplateError(f"{name}:{arg}: the key is letters, digits, '_' and '-', in {{{{ {expr} }}}}")
    return name, arg


def check_expression(expr: str) -> str | None:
    """What is wrong with a placeholder's filters, or None."""
    try:
        for part in expr.split("|")[1:]:
            parse_filter(part, expr.strip())
    except TemplateError as e:
        return str(e)
    return None


def render_value(expr: str, scope: dict[str, Any]) -> Any:
    """Evaluate one placeholder expression (path plus filters) to a value."""
    parts = [p.strip() for p in expr.split("|")]
    value = resolve(parts[0], scope)
    for part in parts[1:]:
        name, arg = parse_filter(part, expr)
        if value is MISSING:
            continue
        value = FILTERS[name](value) if arg is None else FILTERS[name](value, arg)
    return value


def render(template: Any, scope: dict[str, Any], *, strict: bool = True) -> Any:
    """Render a template.

    - A string that is exactly one placeholder returns the resolved value with
      its type intact, so ``"{{ state.ids }}"`` yields a list, not its repr.
    - A string with surrounding text interpolates placeholders as text.
    - Lists and dicts are rendered recursively.
    - Other values pass through.

    With ``strict`` (the default) a placeholder that resolves to nothing raises
    :class:`TemplateError`; a tree should decide on absence explicitly rather
    than paste an empty string into a URL.
    """
    if isinstance(template, str):
        whole = PLACEHOLDER.fullmatch(template.strip())
        if whole and template.strip() == template:
            value = render_value(whole.group(1), scope)
            if value is MISSING:
                if strict:
                    raise TemplateError(f"{template} resolves to nothing")
                return None
            return value

        def sub(m: re.Match[str]) -> str:
            value = render_value(m.group(1), scope)
            if value is MISSING:
                if strict:
                    raise TemplateError(f"{m.group(0)} resolves to nothing")
                return ""
            return _text(value)

        return PLACEHOLDER.sub(sub, template)
    if isinstance(template, list):
        return [render(item, scope, strict=strict) for item in template]
    if isinstance(template, dict):
        return {k: render(v, scope, strict=strict) for k, v in template.items()}
    return template


def expressions(template: Any) -> list[str]:
    """Every placeholder expression in a template, filters and all."""
    if isinstance(template, str):
        return [m.group(1) for m in PLACEHOLDER.finditer(template)]
    if isinstance(template, list):
        return [e for item in template for e in expressions(item)]
    if isinstance(template, dict):
        return [e for v in template.values() for e in expressions(v)]
    return []


def placeholders(template: Any) -> list[str]:
    """Every placeholder path used anywhere in a template, filters stripped."""
    found: list[str] = []
    if isinstance(template, str):
        for m in PLACEHOLDER.finditer(template):
            found.append(m.group(1).split("|")[0].strip())
    elif isinstance(template, list):
        for item in template:
            found.extend(placeholders(item))
    elif isinstance(template, dict):
        for v in template.values():
            found.extend(placeholders(v))
    return found
