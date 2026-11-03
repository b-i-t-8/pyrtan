"""Every route through a tree, without running it: ``pyrtan paths``.

A tree does not loop, so the routes from its start are finite and can be
listed. Each one is the nodes it passes, the branch it takes at each
``decide`` (in the words a trace uses), what it reads and does on the way
(the requests, as written, before any value is known), and how it ends:
``done``, ``fail`` with its reason, or a **gap**, where a decide with no
``else`` (or a lookup with no ``default``) meets a value nobody foresaw.
A branch :mod:`pyrtan.analysis` proves is never taken is listed and
marked, so an audit sees it.

Besides the ending it lists, any request can stop a run on its way, and
those are the same for every route, so :func:`stops` says them once: a
request that fails (``transport``); a target decided at run time that its
allow-list refuses (``host-not-allowed``, ``key-not-allowed``… for each
node ``pyrtan check`` warns of); a service started ``--egress public``,
which refuses the box's own sources, discovery and a private address
(``transport``, naming the nodes: a field device on ``plc.local`` is one);
an answer out of shape (``drift``); a spent budget, the tree's own or a
host's; a confirmation declined.

An ``each`` with a sub-flow is one step of the route that walks it; the
sub-flow's own routes are listed beside the tree's, from its start.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from . import analysis
from .adapters import LOCAL_SOURCES, SOURCES, what
from .template import PLACEHOLDER
from .trace import cond_words
from .tree import Node, Tree, check, row_condition

# The allow-list a target decided at run time is held to, as `pyrtan check`
# names it, and the reason a run that misses it stops with.
REFUSED = {"hosts": ("host", "host-not-allowed"), "paths": ("path", "path-not-allowed"), "commands": ("command", "command-not-allowed"),
           "devices": ("device", "device-not-allowed"), "topics": ("topic", "topic-not-allowed"), "keys": ("key", "key-not-allowed"),
           "networks": ("network", "network-not-allowed")}
PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")

MAX_ROUTES = 1000


@dataclass
class Leg:
    node: str
    kind: str
    does: str = ""          # what the node reads, does or asks, as written
    via: str = ""           # for a decide: the branch taken
    never: bool = False     # that branch is never taken (analysis)
    choice: str | None = None  # what a trace records for it: a decide's condition or "else", "row"; "gap" for none

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
                    walk(b["else"], legs + [Leg(node.id, "decide", via=f"{on}: anything else", choice="else")])
                    continue
                walk(b.get("then"), legs + [Leg(node.id, "decide", via=f"{on} {cond_words(b['when'])}", never=i in never, choice=cond_words(b["when"]))])
                i += 1
            if not any("else" in b for b in branches):
                holes = analysis.holes_in_words(conds)
                end(legs + [Leg(node.id, "decide", via=f"{on}: anything else", choice="gap")], "gap", f"no branch takes it{f' ({holes})' if holes else ''}: recorded as a gap")
        elif node.kind == "lookup":
            leg = Leg(node.id, "lookup", does(node), choice="row")
            walk(node.next, legs + [leg])
            if node.get("default") is None:
                conds = [row_condition(r) for r in node.get("table") or [] if isinstance(r, dict)]
                holes = analysis.holes_in_words(conds)
                end(legs + [Leg(node.id, "lookup", does(node), via=f"{node.get('on')}: no row", choice="gap")], "gap", f"no row takes it{f' ({holes})' if holes else ''}: recorded as a gap")
        else:
            walk(node.next, legs + [Leg(node.id, node.kind, does(node))])

    walk(start or tree.start, [])
    return found, over


def sub_flows(tree: Tree) -> list[str]:
    """The nodes an ``each`` walks as a sub-flow, in the order the tree names them."""
    return [n.spec["start"] for n in tree.nodes.values() if n.kind == "each" and isinstance(n.spec.get("start"), str)]


def _source(node: Node) -> str | None:
    return next((s for s in SOURCES if node.get(s) is not None), None)


def _private(spec: Any) -> bool:
    """Whether a request's address is one ``--egress public`` refuses as
    private, as far as the tree says without running: a LAN name
    (``plc.local``, ``{{ inputs.ups }}.local``) or a private, loopback or
    link-local IP. Any other templated host is not judged, and no name is
    looked up."""
    m = re.match(r"[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/?#]*@)?(\[[^\]]*\]|[^/:?#]*)", str(spec.get("url", ""))) if isinstance(spec, dict) else None
    if not m:
        return False
    host = m.group(1).lower().strip("[]")
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        return True
    if PLACEHOLDER.search(host):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def stops(tree: Tree) -> list[str]:
    """What can stop any route on its way, besides the ending it lists: a
    request that fails, a target decided at run time that its allow-list
    refuses, a service that reaches only public addresses, an answer out of
    shape, a spent budget, a confirmation declined."""
    out: list[str] = []
    requests = [n for n in tree.nodes.values() if n.kind in ("observe", "act") and _source(n) is not None]
    if requests:
        out.append("a request that fails (transport)")
    # A target only the run knows is held to its list then: `pyrtan check` warns of each.
    late: dict[str, list[str]] = {}
    for p in check(tree):
        m = re.search(r"refuses (\w+) outside the list", p.message) if p.level == "warn" else None
        if m and m.group(1) in REFUSED and p.where.startswith("nodes."):
            node = p.where.split(".")[1]
            if node not in late.setdefault(m.group(1), []):
                late[m.group(1)].append(node)
    for allow, nodes in late.items():
        noun, reason = REFUSED[allow]
        out.append(f"a {noun} decided at run time outside the tree's {allow} ({reason}: {', '.join(nodes)})")
    # --egress public closes the box's own sources and discovery, and refuses a private address.
    own = [n.id for n in requests if _source(n) in LOCAL_SOURCES]
    scans = [n.id for n in requests if _source(n) == "discover"]
    lan = [n.id for n in requests if _source(n) not in LOCAL_SOURCES + ("discover",) and _private(n.get(_source(n)))]
    refused = [w for w, ids in (("the box's own files, programs, serial lines and memory", own), ("discovery", scans), ("a private address", lan)) if ids]
    if refused:
        said = refused[0] if len(refused) == 1 else ", ".join(refused[:-1]) + " and " + refused[-1]
        out.append(f"a service started --egress public, which refuses {said} (transport: {', '.join(own + scans + lan)})")
    if any(n.get("expect") for n in requests):
        out.append("an answer out of shape (drift: " + ", ".join(n.id for n in requests if n.get("expect")) + ")")
    budget = getattr(tree, "budget", None)  # a table, or a list of them, where this runtime keeps one
    budgets = [b for b in (budget if isinstance(budget, list) else [budget]) if isinstance(b, dict)]
    if budgets:
        out.append("a spent budget (" + "; ".join(f"{b['requests']} requests per {b['said']}" + (f" for {b['host']}, shared" if b.get("host") else "") for b in budgets) + ")")
    if any(n.kind == "act" and n.get("confirm") for n in tree.nodes.values()):
        out.append("a confirmation answered no (declined)")
    return out


def signature(legs: list[Leg]) -> tuple[tuple[str, str | None], ...]:
    """A route as a trace would show it: each node, and what it chose."""
    return tuple((leg.node, leg.choice) for leg in legs)


def walked(outcome: Any) -> tuple[tuple[str, str | None], ...]:
    """The route a run took at its top level, in the same terms. A node
    that paused and was answered (an ask, an act that asked first) is one
    leg; a decide that found no branch, or a lookup no row, chose "gap"."""
    out: list[tuple[str, str | None]] = []
    for step in outcome.trace.steps if outcome.trace is not None else []:
        if step.depth or step.kind in ("intent", "resume"):
            continue
        if step.kind == "decide":
            choice: str | None = step.chosen or "gap"
        elif step.kind == "lookup":
            choice = "row" if step.chosen else "gap"
        else:
            choice = None
        if out and out[-1][0] == step.node:
            out[-1] = (step.node, choice)
        else:
            out.append((step.node, choice))
    return tuple(out)


@dataclass
class Coverage:
    """Which of a tree's routes its own [[tests]] take."""
    tested: dict[int, list[str]] = field(default_factory=dict)  # route index -> the tests that take it
    stopped: list[tuple[str, str, str]] = field(default_factory=list)  # (test, the node it stopped at, why) for a run no route ends like
    unrun: list[tuple[str, str]] = field(default_factory=list)  # (test, why) for a test that could not run

    def counts(self, found: list[Route]) -> tuple[int, int]:
        """(routes the tests take, routes that can be taken): a route the
        order check proves is never taken is not one a test could take."""
        live = [i for i, r in enumerate(found) if not r.never]
        return sum(1 for i in live if i in self.tested), len(live)

    def untested(self, found: list[Route]) -> list[Route]:
        return [r for i, r in enumerate(found) if not r.never and i not in self.tested]


def describe(r: Route) -> str:
    """A route in one line, as ``pyrtan paths`` lists it."""
    steps = " -> ".join(f"{leg.node} [{leg.via}]" if leg.via else leg.node for leg in r.legs)
    return f"{steps}: {r.end} {r.says}"


def coverage(tree: Tree, others: list[Tree] = (), found: list[Route] | None = None, ran: list[tuple[str, Any]] | None = None) -> Coverage:
    """Run the tree's tests against their recordings (or take the runs
    already made, as ``(test name, outcome)``), and match each run's route
    to one of the routes from the tree's start."""
    from .cases import cases, run_case, with_tree

    found = found if found is not None else routes(tree)[0]
    by_signature: dict[tuple[tuple[str, str | None], ...], int] = {}
    for i, r in enumerate(found):
        by_signature.setdefault(signature(r.legs), i)
    out = Coverage()
    if ran is None:
        registry = with_tree(list(others), tree)
        ran = [(case.name, run_case(case, tree, registry)) for case in cases(tree)]
    for name, outcome in ran:
        if outcome.reason == "no-fixture":
            out.unrun.append((name, outcome.say or "no recording"))
            continue
        took = walked(outcome)
        i = by_signature.get(took)
        if i is not None:
            out.tested.setdefault(i, []).append(name)
        else:
            out.stopped.append((name, took[-1][0] if took else tree.start, outcome.reason or outcome.status))
    return out


def cases_of(tree: Tree) -> list[Any]:
    from .cases import cases

    return cases(tree)


def render(tree: Tree, limit: int = MAX_ROUTES, covered: Coverage | None = None) -> str:
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
            mark = "  (never taken)" if r.never else ""
            if covered is not None and title is None:
                by = covered.tested.get(i - 1)
                mark += f"  [tested: {', '.join(by)}]" if by else ("" if r.never else "  [untested]")
            lines.append(f"{i}. {describe(r)}{mark}")
            for read in r.reads:
                lines.append(f"     {read}")
        if over:
            lines.append(f"… more than {limit} routes; the rest are not listed")
    if covered is not None:
        took, can = covered.counts(routes(tree, tree.start, limit)[0])
        lines.append("")
        lines.append(f"tested: {took} of {can} route(s) that can be taken, by {len(cases_of(tree))} test(s)")
        for name, node, why in covered.stopped:
            lines.append(f"  {name}: stopped at {node} ({why}), on no route above")
        for name, why in covered.unrun:
            lines.append(f"  {name}: not run: {why}")
    head = f"{tree.name}: {total} route(s)"
    can = stops(tree)
    if can:
        head += "; any of them can also stop at " + "; ".join(can)
    return "\n".join([head] + lines)
