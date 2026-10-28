"""``{{ path }}`` interpolation against the run's state.

Templates are deliberately tiny. A placeholder is a dotted path into the
state, optionally followed by one or more filters::

    {{ inputs.city }}
    {{ state.resp.json.results[0].name }}
    {{ inputs.city | url }}
    {{ state.count | int }}

There is no logic in templates. Branching belongs to ``decide`` nodes, where
it is visible in the trace; a template that could branch would hide it.
"""

from __future__ import annotations

import json
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
}


def render_value(expr: str, scope: dict[str, Any]) -> Any:
    """Evaluate one placeholder expression (path plus filters) to a value."""
    parts = [p.strip() for p in expr.split("|")]
    value = resolve(parts[0], scope)
    for name in parts[1:]:
        if name not in FILTERS:
            raise TemplateError(f"unknown filter {name!r} in {{{{ {expr} }}}}")
        if value is MISSING:
            continue
        value = FILTERS[name](value)
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
