"""The tree: Pyrtan's unit of automation.

A tree is a TOML file. It names what it answers to (``triggers``), what it
needs (``inputs``), which hosts it may touch (``hosts``), and a set of
``nodes`` beginning at ``start``. Node kinds:

``observe``
    Read something: an HTTP request, or an extraction from state already
    held. Safe to retry.
``act``
    Change something: an HTTP request with side effects, or an event emitted
    to the hub. Never retried automatically.
``decide``
    Look at one value and pick a branch by plain conditions. The heart of the
    format; every decision a run makes is one of these.
``set``
    Compute state from templates.
``each``
    For every item of a list (up to ``limit``), run another tree or a local
    sub-flow, collecting the outputs.
``delegate``
    Hand an intent or a named tree to the swarm and keep its output.
``ask``
    Pause and request an input from whoever is running the tree.
``done`` / ``fail``
    Terminal: what to say and what to output, or why it did not work.

Trees do not loop. A path through a tree is finite by construction, which
is what makes the trace a complete account of the run; repetition is
``each`` with a limit.

:func:`check` validates a tree and returns every problem found, at the level
``error`` (will not run), ``warn`` (will run, may surprise) or ``info``.
"""

from __future__ import annotations

import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import conditions, extract as extractmod
from .intent import KINDS as SLOT_KINDS, PRODUCES, Language, Trigger, TriggerError
from .template import PLACEHOLDER, placeholders

NAME = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
KINDS = {"observe", "act", "decide", "set", "each", "delegate", "ask", "done", "fail"}
TERMINAL = {"done", "fail"}
INPUT_TYPES = {"str", "int", "float", "bool", "list", "dict"}
HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


class TreeError(ValueError):
    pass


@dataclass
class Problem:
    level: str  # error | warn | info
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclass
class InputSpec:
    name: str
    type: str = "str"
    default: Any = None
    required: bool = True
    description: str = ""
    min: float | None = None
    max: float | None = None
    one_of: list[Any] | None = None
    pattern: str | None = None

    def coerce(self, value: Any) -> Any:
        try:
            if self.type == "int":
                if isinstance(value, bool):
                    raise ValueError
                value = int(value) if not isinstance(value, str) else int(value.strip())
            elif self.type == "float":
                value = float(value)
            elif self.type == "bool":
                if isinstance(value, str):
                    low = value.strip().lower()
                    if low in ("true", "yes", "1", "on"):
                        value = True
                    elif low in ("false", "no", "0", "off"):
                        value = False
                    else:
                        raise ValueError
                else:
                    value = bool(value)
            elif self.type == "str":
                value = str(value)
            elif self.type == "list" and not isinstance(value, list):
                raise ValueError
            elif self.type == "dict" and not isinstance(value, dict):
                raise ValueError
        except (TypeError, ValueError):
            raise TreeError(f"input {self.name!r} must be {self.type}, got {value!r}") from None
        if self.min is not None and isinstance(value, (int, float)) and value < self.min:
            raise TreeError(f"input {self.name!r} must be at least {self.min}, got {value}")
        if self.max is not None and isinstance(value, (int, float)) and value > self.max:
            raise TreeError(f"input {self.name!r} must be at most {self.max}, got {value}")
        if self.one_of is not None and value not in self.one_of:
            raise TreeError(f"input {self.name!r} must be one of {self.one_of}, got {value!r}")
        if self.pattern is not None and isinstance(value, str) and not re.fullmatch(self.pattern, value):
            raise TreeError(f"input {self.name!r} must match {self.pattern!r}, got {value!r}")
        return value


@dataclass
class Node:
    id: str
    kind: str
    spec: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.spec.get(key, default)

    @property
    def next(self) -> str | None:
        return self.spec.get("next")

    def targets(self) -> list[str]:
        """Every node id this node can hand off to."""
        out: list[str] = []
        if self.kind == "decide":
            for br in self.spec.get("branches", []):
                if isinstance(br, dict):
                    if "then" in br:
                        out.append(br["then"])
                    if "else" in br:
                        out.append(br["else"])
        if self.kind == "each" and "start" in self.spec:
            out.append(self.spec["start"])
        if self.next:
            out.append(self.next)
        return out


@dataclass
class Tree:
    name: str
    description: str
    version: int
    hosts: list[str]
    triggers: list[Trigger]
    inputs: dict[str, InputSpec]
    start: str
    nodes: dict[str, Node]
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    license: str = ""
    language: Language = field(default_factory=Language)

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise TreeError(f"{self.name}: no node {node_id!r}") from None

    def host_allowed(self, url: str) -> bool:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            return False
        for allowed in self.hosts:
            allowed = allowed.lower()
            if allowed == "*":
                return True
            if allowed.startswith("*."):
                suffix = allowed[1:]
                if host.endswith(suffix) or host == allowed[2:]:
                    return True
            elif host == allowed:
                return True
        return False

    def bind_inputs(self, given: dict[str, Any]) -> dict[str, Any]:
        """Apply defaults, coerce types, and refuse unknown or missing inputs."""
        unknown = set(given) - set(self.inputs)
        if unknown:
            raise TreeError(f"{self.name}: unknown inputs {sorted(unknown)}")
        bound: dict[str, Any] = {}
        for name, spec in self.inputs.items():
            if name in given and given[name] is not None:
                bound[name] = spec.coerce(given[name])
            elif spec.default is not None:
                bound[name] = spec.default
            elif spec.required:
                raise TreeError(f"{self.name}: input {name!r} is required")
            else:
                bound[name] = None
        return bound

    def capability(self) -> dict[str, Any]:
        """What this tree advertises to the swarm."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": self.tags,
            "hosts": self.hosts,
            "license": self.license,
            "language": self.language.to_dict(),
            "triggers": [t.pattern for t in self.triggers],
            "inputs": {
                n: {k: v for k, v in {"type": s.type, "default": s.default, "required": s.required and s.default is None, "description": s.description}.items() if v not in (None, "", False)}
                for n, s in self.inputs.items()
            },
        }


def load_tree(path: str | Path) -> Tree:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise TreeError(f"cannot read {path}: {e}") from e
    tree = load_tree_text(text, source=str(path))
    return tree


def load_tree_text(text: str, source: str | None = None) -> Tree:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TreeError(f"{source or '<text>'}: not valid TOML: {e}") from e
    return build(raw, source=source)


def build(raw: dict[str, Any], source: str | None = None) -> Tree:
    where = source or raw.get("name", "<tree>")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise TreeError(f"{where}: 'name' must be lowercase letters, digits and dashes, got {name!r}")
    description = str(raw.get("description", "")).strip()
    version = raw.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise TreeError(f"{name}: 'version' must be a positive integer")
    hosts = raw.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        raise TreeError(f"{name}: 'hosts' must be a list of host names")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise TreeError(f"{name}: 'tags' must be a list of strings")
    license_ = raw.get("license", "")
    if not isinstance(license_, str):
        raise TreeError(f"{name}: 'license' must be a string such as \"MIT\" (an SPDX identifier)")

    try:
        language = Language.from_raw(raw.get("language"), "language")
    except TriggerError as e:
        raise TreeError(f"{name}: {e}") from e

    triggers: list[Trigger] = []
    for i, t in enumerate(raw.get("triggers", [])):
        if isinstance(t, str):
            t = {"pattern": t}
        if not isinstance(t, dict) or "pattern" not in t:
            raise TreeError(f"{name}: triggers[{i}] needs a 'pattern'")
        try:
            triggers.append(Trigger(t["pattern"], dict(t.get("inputs", {})), language))
        except TriggerError as e:
            raise TreeError(f"{name}: triggers[{i}]: {e}") from e

    inputs: dict[str, InputSpec] = {}
    raw_inputs = raw.get("inputs", {})
    if not isinstance(raw_inputs, dict):
        raise TreeError(f"{name}: 'inputs' must be a table")
    for iname, spec in raw_inputs.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", iname):
            raise TreeError(f"{name}: bad input name {iname!r}")
        if not isinstance(spec, dict):
            spec = {"default": spec}
        itype = spec.get("type", _infer_type(spec.get("default")))
        if itype not in INPUT_TYPES:
            raise TreeError(f"{name}: input {iname!r} has unknown type {itype!r}")
        inputs[iname] = InputSpec(
            name=iname,
            type=itype,
            default=spec.get("default"),
            required=bool(spec.get("required", spec.get("default") is None)),
            description=str(spec.get("description", "")),
            min=spec.get("min"),
            max=spec.get("max"),
            one_of=spec.get("one_of"),
            pattern=spec.get("pattern"),
        )

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        raise TreeError(f"{name}: 'nodes' must be a non-empty table")
    nodes: dict[str, Node] = {}
    for nid, spec in raw_nodes.items():
        if not NAME.match(nid):
            raise TreeError(f"{name}: node id {nid!r} must be lowercase letters, digits and dashes")
        if not isinstance(spec, dict):
            raise TreeError(f"{name}: node {nid!r} must be a table")
        kind = spec.get("kind")
        if kind not in KINDS:
            raise TreeError(f"{name}: node {nid!r} has unknown kind {kind!r}; one of {sorted(KINDS)}")
        nodes[nid] = Node(nid, kind, spec)

    start = raw.get("start")
    if not isinstance(start, str):
        hint = " (top-level keys such as 'start' must come before the first [table] in TOML)" if _buried(raw, "start") else ""
        raise TreeError(f"{name}: 'start' must name a node{hint}")

    tree = Tree(name, description, version, hosts, triggers, inputs, start, nodes, tags, source, raw, license_.strip(), language)
    errors = [p for p in check(tree) if p.level == "error"]
    if errors:
        raise TreeError(f"{name}: " + "; ".join(f"{p.where}: {p.message}" for p in errors))
    return tree


def _buried(raw: Any, key: str, depth: int = 0) -> bool:
    """True when ``key`` appears inside some table rather than at top level."""
    if not isinstance(raw, dict) or depth > 3:
        return False
    return any((key in v) if isinstance(v, dict) else False for v in raw.values()) or any(_buried(v, key, depth + 1) for v in raw.values())


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "str"


# ---------------------------------------------------------------- checking


def _slot_fits(kind: str | None, input_type: str) -> bool:
    """Whether what a slot kind reads can be bound to an input of this type."""
    produced = PRODUCES[kind]
    if produced == "int":
        return input_type in ("int", "float", "str")
    if produced == "float":
        return input_type in ("float", "str")
    if produced == "number":
        return input_type in ("int", "float", "str")
    return input_type == "str"


def check(tree: Tree) -> list[Problem]:
    """Validate structure, references, templates, conditions, selectors,
    hosts and reachability. Never raises; returns what it found."""
    problems: list[Problem] = []
    err = lambda where, msg: problems.append(Problem("error", where, msg))
    warn = lambda where, msg: problems.append(Problem("warn", where, msg))
    info = lambda where, msg: problems.append(Problem("info", where, msg))

    if not tree.description:
        warn("description", "a tree should say what it does; the swarm shows this to people")
    if tree.start not in tree.nodes:
        err("start", f"start node {tree.start!r} does not exist")

    for i, t in enumerate(tree.triggers):
        for slot, kind in t.slots.items():
            if slot not in tree.inputs:
                err(f"triggers[{i}]", f"slot {{{slot}}} is not a declared input")
                continue
            want = tree.inputs[slot].type
            if not _slot_fits(kind, want):
                err(f"triggers[{i}]", f"slot {{{slot}:{kind}}} produces a {PRODUCES[kind]}, but input {slot!r} is a {want}")
        for fixed in t.fixed_inputs:
            if fixed not in tree.inputs:
                err(f"triggers[{i}]", f"fixed input {fixed!r} is not a declared input")

    produced: set[str] = {"last"}
    for node in tree.nodes.values():
        for key in ("into",):
            v = node.get(key)
            if isinstance(v, str):
                produced.add(v.split(".")[0].split("[")[0])
        for ex in node.get("extract", []) or []:
            if isinstance(ex, dict) and isinstance(ex.get("into"), str):
                produced.add(ex["into"].split(".")[0])
        if node.kind == "set" and isinstance(node.get("set"), dict):
            produced.update(k.split(".")[0] for k in node.get("set"))

    uses_http = False
    for node in tree.nodes.values():
        where = f"nodes.{node.id}"
        for target in node.targets():
            if target not in tree.nodes:
                err(where, f"refers to node {target!r}, which does not exist")
        if node.kind in TERMINAL and node.next:
            err(where, f"a {node.kind} node has no 'next'")
        if node.kind not in TERMINAL and node.kind != "decide" and not node.next:
            err(where, f"a {node.kind} node needs 'next'")

        if node.kind in ("observe", "act"):
            http = node.get("http")
            frm = node.get("from")
            emit = node.get("emit")
            ways = sum(1 for x in (http, frm, emit) if x is not None)
            if ways != 1:
                err(where, f"an {node.kind} node needs exactly one of 'http', 'from' or 'emit'")
            if http is not None:
                uses_http = True
                _check_http(tree, node, http, err, warn)
            if emit is not None:
                if node.kind != "act":
                    err(where, "'emit' is an act, not an observation")
                if not isinstance(emit, dict) or not isinstance(emit.get("event"), str):
                    err(where, "'emit' needs a string 'event'")
            if frm is not None and not isinstance(frm, str):
                err(where, "'from' must be a state path")
            if node.get("retries") is not None and node.kind == "act":
                err(where, "an act is never retried; drop 'retries' or make it an observe")
            for j, ex in enumerate(node.get("extract", []) or []):
                try:
                    extractmod.validate(ex)
                except extractmod.ExtractError as e:
                    err(f"{where}.extract[{j}]", str(e))

        elif node.kind == "decide":
            on = node.get("on")
            if not isinstance(on, str) or not on.strip():
                err(where, "a decide node needs 'on': the state path it looks at")
            branches = node.get("branches")
            if not isinstance(branches, list) or not branches:
                err(where, "a decide node needs a non-empty 'branches' list")
            else:
                has_else = False
                for j, br in enumerate(branches):
                    bw = f"{where}.branches[{j}]"
                    if not isinstance(br, dict):
                        err(bw, "a branch is a table")
                        continue
                    if "else" in br:
                        if j != len(branches) - 1:
                            err(bw, "'else' must be the last branch")
                        if set(br) - {"else", "note"}:
                            err(bw, "an else branch has only 'else'")
                        has_else = True
                    else:
                        if "when" not in br or "then" not in br:
                            err(bw, "a branch needs 'when' and 'then'")
                            continue
                        try:
                            conditions.validate(br["when"])
                        except conditions.ConditionError as e:
                            err(bw, str(e))
                if not has_else:
                    info(where, "no 'else': an unforeseen value is recorded as a gap and fails the run")
            if node.next:
                err(where, "a decide node routes through its branches, not 'next'")

        elif node.kind == "set":
            if not isinstance(node.get("set"), dict) or not node.get("set"):
                err(where, "a set node needs a non-empty 'set' table")

        elif node.kind == "each":
            if not isinstance(node.get("over"), str):
                err(where, "an each node needs 'over': the list to walk")
            if ("tree" in node.spec) == ("start" in node.spec):
                err(where, "an each node needs exactly one of 'tree' (delegate per item) or 'start' (local sub-flow)")
            if "start" in node.spec and node.get("start") == node.id:
                err(where, "a sub-flow cannot start at the each node itself")
            if node.get("limit") is None:
                warn(where, "no 'limit': the whole list is walked, however long")
            if not isinstance(node.get("into"), str):
                err(where, "an each node needs 'into' to collect results")

        elif node.kind == "delegate":
            if ("intent" in node.spec) == ("tree" in node.spec):
                err(where, "a delegate node needs exactly one of 'intent' or 'tree'")
            if not isinstance(node.get("into"), str):
                err(where, "a delegate node needs 'into'")

        elif node.kind == "ask":
            if not isinstance(node.get("prompt"), str):
                err(where, "an ask node needs a 'prompt'")
            into = node.get("into")
            if not isinstance(into, str):
                err(where, "an ask node needs 'into'")
            elif into.startswith("inputs.") and into[len("inputs."):] not in tree.inputs:
                err(where, f"asks for {into}, which is not a declared input")

        elif node.kind == "done":
            if node.get("say") is None and node.get("output") is None:
                warn(where, "a done node with neither 'say' nor 'output' tells nobody anything")
        elif node.kind == "fail":
            if not isinstance(node.get("reason"), str):
                err(where, "a fail node needs a 'reason' code")

        for path in placeholders(node.spec):
            head, _, rest = path.partition(".")
            if head == "inputs":
                key = rest.split(".")[0].split("[")[0]
                if key not in tree.inputs:
                    err(where, f"template refers to inputs.{key}, which is not declared")
            elif head == "state":
                key = rest.split(".")[0].split("[")[0]
                if key and key not in produced:
                    warn(where, f"template refers to state.{key}, which no node produces")
            elif head not in ("item", "index", "tree", "now", "env"):
                err(where, f"template root {head!r} is not one of inputs, state, item, index, tree, now, env")

    if uses_http and not tree.hosts:
        err("hosts", "a tree that speaks HTTP must list the hosts it may reach")
    if tree.hosts and not uses_http:
        info("hosts", "hosts are listed but no node speaks HTTP")

    if tree.start in tree.nodes:
        problems.extend(_check_graph(tree))
    return problems


def _check_http(tree: Tree, node: Node, http: Any, err, warn) -> None:
    where = f"nodes.{node.id}.http"
    if not isinstance(http, dict):
        err(where, "'http' is a table with 'url' and optionally 'method', 'headers', 'body'")
        return
    url = http.get("url")
    if not isinstance(url, str) or not url:
        err(where, "needs a 'url'")
        return
    method = str(http.get("method", "GET")).upper()
    if method not in HTTP_METHODS:
        err(where, f"unknown method {method!r}")
    if node.kind == "observe" and method not in READ_METHODS:
        err(where, f"{method} changes things; make this an act node")
    if node.kind == "act" and method in READ_METHODS and http.get("body") is None:
        warn(where, f"{method} with no body is a read; an observe node would retry it safely")
    head = PLACEHOLDER.split(url, 1)[0]
    parsed = urllib.parse.urlsplit(head)
    if parsed.scheme and parsed.hostname:
        if parsed.scheme not in ("http", "https"):
            err(where, f"scheme {parsed.scheme!r} is not http or https")
        if not tree.host_allowed(head):
            err(where, f"host {parsed.hostname!r} is not in this tree's hosts")
    else:
        warn(where, "the host is decided at run time; the runtime refuses hosts outside the list")
    headers = http.get("headers", {})
    if headers is not None and not isinstance(headers, dict):
        err(where, "'headers' is a table")


def _check_graph(tree: Tree) -> list[Problem]:
    problems: list[Problem] = []
    # Reachability from start.
    seen: set[str] = set()
    stack = [tree.start]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in tree.nodes:
            continue
        seen.add(nid)
        stack.extend(tree.nodes[nid].targets())
    for nid in tree.nodes:
        if nid not in seen:
            problems.append(Problem("warn", f"nodes.{nid}", "not reachable from start"))
    # Cycles: a tree does not loop.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {nid: WHITE for nid in tree.nodes}

    def visit(nid: str, path: list[str]) -> None:
        colour[nid] = GREY
        for t in tree.nodes[nid].targets():
            if t not in tree.nodes:
                continue
            if colour[t] == GREY:
                cycle = path[path.index(t):] + [t]
                problems.append(Problem("error", f"nodes.{nid}", "trees do not loop: " + " -> ".join(cycle) + " (use each with a limit)"))
            elif colour[t] == WHITE:
                visit(t, path + [t])
        colour[nid] = BLACK

    for nid in tree.nodes:
        if colour[nid] == WHITE:
            visit(nid, [nid])
    return problems
