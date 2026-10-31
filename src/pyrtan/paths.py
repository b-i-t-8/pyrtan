"""Every route through a tree, without running it: ``pyrtan paths``.

A tree does not loop, so the routes from its start are finite and can be
listed. Each one is the nodes it passes, the branch it takes at each
``decide`` (in the words a trace uses), what it reads and does on the way
(the requests, as written, before any value is known), and how it ends:
``done``, ``fail`` with its reason, or a **gap**, where a decide with no
``else`` (or a lookup with no ``default``) meets a value nobody foresaw.
A branch :mod:`pyrtan.analysis` proves is never taken is listed and
marked, so an audit sees it.

Besides the ending it lists, any request can stop a run on its way
(``transport``, ``drift`` when it has an ``expect``, ``budget``,
a template that resolves to nothing): those are the same for every route
and are said once, by :func:`stops`.

An ``each`` with a sub-flow is one step of the route that walks it; the
sub-flow's own routes are listed beside the tree's, from its start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import analysis
from .adapters import SOURCES, what
from .trace import cond_words
from .tree import Node, Tree, row_condition

MAX_ROUTES = 1000


@dataclass
class Leg:
    node: str
    kind: str
    does: str = ""          # what the node reads, does or asks, as written
    via: str = ""           # for a decide: the branch taken
    never: bool = False     # that branch is never taken (analysis)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {"node": self.node, "kind": self.kind, "does": self.does, "via": self.via, "never": self.never}.items() if v not in ("", False)}


@dataclass
class Route:
    legs: list[Leg] = field(default_factory=list)
    end: str = ""           # done | fail | gap | dead-end
    says: str = ""          # the done's say, the fail's reason, or what the gap is

    @property
    def never(self) -> bool:
        return any(leg.never for leg in self.legs)

    @property
    def reads(self) -> list[str]:
        """What the route reaches, does or asks on the way: its requests, the
        trees it hands to, what it walks and what it asks a person."""
        return [f"{leg.node}: {leg.does}" for leg in self.legs if leg.kind in ("observe", "act", "delegate", "each", "ask") and leg.does]

    def to_dict(self) -> dict[str, Any]:
        return {"path": [leg.node for leg in self.legs], "end": self.end, "says": self.says, "never": self.never, "reads": self.reads, "legs": [leg.to_dict() for leg in self.legs]}


def does(node: Node) -> str:
    """What one node reads, does or asks, before any value is known."""
    spec = node.spec
    if node.kind in ("observe", "act"):
        if node.get("emit") is not None:
            text = f"emit {node.get('emit').get('event')}"
        elif node.get("from") is not None:
            text = f"from {node.get('from')}"
        else:
            source = next((s for s in SOURCES if node.get(s) is not None), None)
            if source is None:
                return ""
            s = node.get(source)
            text = f"{str(s.get('method', 'GET')).upper()} {s.get('url')}" if source == "http" else f"{source} {what(source, s)}"
        if node.kind == "act" and node.get("confirm"):
            text += " (asks first)"
        return text
    if node.kind == "delegate":
        return f"tree {spec['tree']}" if "tree" in spec else f"intent {spec.get('intent')!r}"
    if node.kind == "each":
        walks = f"sub-flow from {spec['start']}" if "start" in spec else f"tree {spec.get('tree')}"
        return f"each of {spec.get('over')}: {walks}"
    if node.kind == "set":
        return "set " + ", ".join(spec.get("set") or {})
    if node.kind == "lookup":
        return f"{node.get('on')} -> {node.get('into')}"
    if node.kind == "ask":
        return f"asks {spec.get('prompt')!r}"
    return ""


def routes(tree: Tree, start: str | None = None, limit: int = MAX_ROUTES) -> tuple[list[Route], bool]:
    """Every route from ``start`` (the tree's own by default). The second
    value says whether there were more than ``limit``."""
    found: list[Route] = []
    over = False

    def end(legs: list[Leg], kind: str, says: str) -> None:
        nonlocal over
        if len(found) >= limit:
            over = True
            return
        found.append(Route(list(legs), kind, says))

    def walk(node_id: str | None, legs: list[Leg]) -> None:
        if over:
            return
        node = tree.nodes.get(node_id) if node_id else None
        if node is None:
            end(legs, "dead-end", f"{node_id!r} is not a node" if node_id else f"{legs[-1].node} has nowhere to go")
            return
        if any(leg.node == node.id for leg in legs):
            end(legs, "dead-end", f"{node.id} again")  # pyrtan check refuses a loop; listed rather than followed
            return
        if node.kind == "done":
            end(legs + [Leg(node.id, "done")], "done", str(node.get("say") if node.get("say") is not None else node.get("output")))
        elif node.kind == "fail":
            end(legs + [Leg(node.id, "fail")], "fail", str(node.get("reason")))
        elif node.kind == "decide":
            on = node.get("on")
            branches = [b for b in node.get("branches") or [] if isinstance(b, dict)]
            conds = [b["when"] for b in branches if "when" in b]
            never = {f.index for f in analysis.findings(conds)}
            i = 0
            for b in branches:
                if "else" in b:
                    walk(b["else"], legs + [Leg(node.id, "decide", via=f"{on}: anything else")])
                    continue
                walk(b.get("then"), legs + [Leg(node.id, "decide", via=f"{on} {cond_words(b['when'])}", never=i in never)])
                i += 1
            if not any("else" in b for b in branches):
                holes = analysis.holes_in_words(conds)
                end(legs + [Leg(node.id, "decide", via=f"{on}: anything else")], "gap", f"no branch takes it{f' ({holes})' if holes else ''}: recorded as a gap")
        elif node.kind == "lookup":
            leg = Leg(node.id, "lookup", does(node))
            walk(node.next, legs + [leg])
            if node.get("default") is None:
                conds = [row_condition(r) for r in node.get("table") or [] if isinstance(r, dict)]
                holes = analysis.holes_in_words(conds)
                end(legs + [Leg(node.id, "lookup", does(node), via=f"{node.get('on')}: no row")], "gap", f"no row takes it{f' ({holes})' if holes else ''}: recorded as a gap")
        else:
            walk(node.next, legs + [Leg(node.id, node.kind, does(node))])

    walk(start or tree.start, [])
    return found, over


def sub_flows(tree: Tree) -> list[str]:
    """The nodes an ``each`` walks as a sub-flow, in the order the tree names them."""
    return [n.spec["start"] for n in tree.nodes.values() if n.kind == "each" and isinstance(n.spec.get("start"), str)]


def stops(tree: Tree) -> list[str]:
    """What can stop any route on its way, besides the ending it lists."""
    out: list[str] = []
    requests = [n for n in tree.nodes.values() if n.kind in ("observe", "act") and any(n.get(s) is not None for s in SOURCES)]
    if requests:
        out.append("a request that fails (transport)")
    if any(n.get("expect") for n in requests):
        out.append("an answer out of shape (drift: " + ", ".join(n.id for n in requests if n.get("expect")) + ")")
    budget = getattr(tree, "budget", None)  # a tree's budget, where this runtime keeps one
    if isinstance(budget, dict):
        out.append(f"a spent budget ({budget['requests']} requests per {budget['said']})")
    if any(n.kind == "act" and n.get("confirm") for n in tree.nodes.values()):
        out.append("a confirmation answered no (declined)")
    return out


def render(tree: Tree, limit: int = MAX_ROUTES) -> str:
    """The routes as text, the way ``pyrtan paths`` prints them."""
    lines: list[str] = []
    sections = [(None, tree.start)] + [(f"each sub-flow from {s}", s) for s in sub_flows(tree)]
    total = 0
    for title, start in sections:
        found, over = routes(tree, start, limit)
        total += len(found)
        if title:
            lines.append("")
            lines.append(f"{title}:")
        for i, r in enumerate(found, 1):
            steps = " -> ".join(f"{leg.node} [{leg.via}]" if leg.via else leg.node for leg in r.legs)
            mark = "  (never taken)" if r.never else ""
            lines.append(f"{i}. {steps}: {r.end} {r.says}{mark}")
            for read in r.reads:
                lines.append(f"     {read}")
        if over:
            lines.append(f"… more than {limit} routes; the rest are not listed")
    head = f"{tree.name}: {total} route(s)"
    can = stops(tree)
    if can:
        head += "; any of them can also stop at " + "; ".join(can)
    return "\n".join([head] + lines)
