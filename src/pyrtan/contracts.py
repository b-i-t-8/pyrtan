"""What a tree promises to give back, and the trees that rely on it.

A tree may declare ``returns``: the fields its ``output`` always carries
when it ends ``done``, each with a type::

    returns = { tag = "str", url = "str", prerelease = "bool", notes = "str?" }

The types are ``str``, ``int``, ``float``, ``number`` (int or float),
``bool``, ``list``, ``dict`` and ``any`` (anything but nothing); a ``?``
says the field may be empty (absent, or null). Three things hold it:

- ``pyrtan check`` refuses a ``done`` the tree ends on (not one that ends
  an ``each`` sub-flow) whose ``output`` lacks a field it promises;
- ``pyrtan check`` over several trees refuses a caller that reads a field
  of a delegated tree's output (``state.r.output.tag``, or
  ``state.items[0].tag`` after an ``each`` over the tree) that the tree
  does not promise, and says what it does promise;
- a run that ends ``done`` with a field missing or of another type stops
  with reason ``contract`` instead, naming the field, so a caller is
  never handed what it was not promised.

A caller of a tree that promises nothing is not held to anything; check
says so, once, where it reads the output.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .template import placeholders

TYPES = ("str", "int", "float", "number", "bool", "list", "dict", "any")
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


class ContractError(ValueError):
    pass


def parse_returns(name: str, raw: Any) -> dict[str, tuple[str, bool]] | None:
    """``returns`` as field -> (type, may be empty); None when not given."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ContractError(f"{name}: 'returns' is a table of field = type, such as {{ tag = \"str\" }}")
    out: dict[str, tuple[str, bool]] = {}
    for field, kind in raw.items():
        if not FIELD.fullmatch(field):
            raise ContractError(f"{name}: returns.{field}: a field is letters, digits, '_' and '-'")
        if not isinstance(kind, str) or kind.rstrip("?") not in TYPES or kind.count("?") > 1 or ("?" in kind and not kind.endswith("?")):
            raise ContractError(f"{name}: returns.{field} is one of {', '.join(TYPES)}, with a '?' if it may be empty; got {kind!r}")
        out[field] = (kind.rstrip("?"), kind.endswith("?"))
    return out


def fits(kind: str, value: Any) -> bool:
    if kind == "any":
        return True
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if kind == "int":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if kind in ("float", "number"):
        return isinstance(value, (int, float))
    return isinstance(value, list if kind == "list" else dict)


def broken(returns: dict[str, tuple[str, bool]], output: Any) -> str | None:
    """The first promise ``output`` breaks, in words, or None."""
    table = output if isinstance(output, dict) else {}
    for field, (kind, optional) in returns.items():
        value = table.get(field)
        if value is None or value == "":
            if not optional:
                return f"{field} ({kind}) is empty"
            continue
        if not fits(kind, value):
            return f"{field} is {_kind_of(value)} {_brief(value)}, where it promises {kind}"
    return None


def _kind_of(value: Any) -> str:
    return {bool: "a bool", int: "an int", float: "a float", str: "a str", list: "a list", dict: "a dict"}.get(type(value), "a value")


def _brief(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:59] + "…"


def ending(tree: Any) -> list[Any]:
    """The ``done`` nodes a run of the tree can end on: reachable from its
    start without going into an ``each`` sub-flow."""
    seen: set[str] = set()
    todo = [tree.start]
    while todo:
        nid = todo.pop()
        if nid in seen or nid not in tree.nodes:
            continue
        seen.add(nid)
        node = tree.nodes[nid]
        targets = list(node.targets())
        if node.kind == "each" and isinstance(node.spec.get("start"), str):
            targets.remove(node.spec["start"])
        todo.extend(targets)
    return [tree.nodes[n] for n in sorted(seen) if tree.nodes[n].kind == "done"]


def check_own(tree: Any, err: Callable[[str, str], None]) -> None:
    """A tree's own done nodes against what it promises."""
    returns = getattr(tree, "returns", None)
    if not returns:
        return
    for node in ending(tree):
        output = node.get("output")
        if not isinstance(output, dict):
            err(f"nodes.{node.id}", f"the tree returns {', '.join(returns)}, but this done has no output table")
            continue
        missing = [f for f, (_, optional) in returns.items() if not optional and f not in output]
        if missing:
            err(f"nodes.{node.id}", f"the tree returns {', '.join(missing)}, which this done's output does not give")


def reads(tree: Any) -> list[tuple[str, str, str, str]]:
    """Every field a tree reads of a delegated tree's output: (node,
    callee, field, the path as written)."""
    hands: dict[str, tuple[str, str]] = {}  # state key -> (callee, how its result is kept)
    for node in tree.nodes.values():
        callee, into = node.spec.get("tree"), node.spec.get("into")
        if isinstance(callee, str) and isinstance(into, str) and node.kind in ("delegate", "each"):
            key = into[len("state."):] if into.startswith("state.") else into
            hands[key.split(".")[0]] = (callee, node.kind)
    if not hands:
        return []
    found: list[tuple[str, str, str, str]] = []
    for node in tree.nodes.values():
        paths = list(placeholders(node.spec))
        for key in ("on",):
            if isinstance(node.spec.get(key), str):
                paths.append(node.spec[key])
        for path in paths:
            p = path[len("state."):] if path.startswith("state.") else path
            root = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", p)
            if not root or root.group(0) not in hands:
                continue
            callee, how = hands[root.group(0)]
            rest = p[root.end():]
            m = re.match(r"\.output\.([A-Za-z_][A-Za-z0-9_-]*)", rest) if how == "delegate" else re.match(r"\[-?\d+\]\.([A-Za-z_][A-Za-z0-9_-]*)", rest)
            if m:
                found.append((node.id, callee, m.group(1), path))
    return found


def check_callers(trees: dict[str, Any]) -> dict[str, list[tuple[str, str, str]]]:
    """For each tree, (level, where, message) about the fields it reads of
    the trees it delegates to, where those trees are among ``trees``."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for tree in trees.values():
        told: set[str] = set()
        for node_id, callee, field, path in reads(tree):
            other = trees.get(callee)
            if other is None:
                continue  # delegated to a tree not here (the swarm's): nothing to hold it to
            returns = getattr(other, "returns", None)
            where = f"nodes.{node_id}"
            if returns is None:
                if callee not in told:
                    told.add(callee)
                    out.setdefault(tree.name, []).append(("info", where, f"reads {path}, but {callee} promises nothing about its output; give it 'returns' to have this checked"))
            elif field not in returns:
                out.setdefault(tree.name, []).append(("error", where, f"reads {path}, but {callee} does not return {field!r}; it returns {', '.join(returns)}"))
    return out
