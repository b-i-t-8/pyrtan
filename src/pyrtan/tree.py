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
``lookup``
    Look at one value and read the matching row of a table: the same
    conditions as ``decide``, but each row gives a value rather than a
    node, so a code-to-word table is one node, not one per word.
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

from . import adapters, conditions, extract as extractmod
from .intent import KINDS as SLOT_KINDS, PRODUCES, Language, Trigger, TriggerError
from .template import PLACEHOLDER, placeholders

NAME = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
KINDS = {"observe", "act", "decide", "set", "lookup", "each", "delegate", "ask", "done", "fail"}
ROW_KEYS = {"value", "note"}  # what a lookup row holds besides its condition
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
    namespace: str = ""                                 # who published it: the catalog id is namespace/name
    watches: list[dict[str, Any]] = field(default_factory=list)  # when this tree runs on its own: a clock, a file, a topic, a serial line
    paths: list[str] = field(default_factory=list)      # what ``file`` nodes may reach
    commands: list[str] = field(default_factory=list)   # what ``shell`` nodes may run
    devices: list[str] = field(default_factory=list)    # what ``serial`` nodes may open
    topics: list[str] = field(default_factory=list)     # what ``queue`` nodes may subscribe to or publish on
    keys: list[str] = field(default_factory=list)       # what ``memory`` nodes may read and write
    tests: list[dict[str, Any]] = field(default_factory=list)  # the tree's own tests: a sentence or inputs, a recording, what the run must come to

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

    @property
    def id(self) -> str:
        """``namespace/name`` in a catalog; just the name without one."""
        return f"{self.namespace}/{self.name}" if self.namespace else self.name

    def allowed(self, source: str, spec: dict[str, Any]) -> tuple[str, str] | None:
        """Why a rendered request to ``source`` is outside this tree's
        allow-list, as ``(reason, text)``, or None when it is inside."""
        if source in ("http", "browser"):
            url = str(spec.get("url", ""))
            return None if self.host_allowed(url) else ("host-not-allowed", f"{url} is outside this tree's hosts {self.hosts}.")
        if source == "file":
            path = adapters.absolute_path(spec.get("path", ""))
            return None if adapters.path_allowed(self.paths, path) else ("path-not-allowed", f"{path} is outside this tree's paths {self.paths}.")
        if source == "shell":
            cmd = str(spec.get("command", ""))
            return None if adapters.command_allowed(self.commands, cmd) else ("command-not-allowed", f"{cmd!r} is not in this tree's commands {self.commands}.")
        if source == "serial":
            dev = str(spec.get("device", ""))
            return None if adapters.device_allowed(self.devices, dev) else ("device-not-allowed", f"{dev} is outside this tree's devices {self.devices}.")
        if source == "queue":
            url = str(spec.get("url", ""))
            try:
                _, host, port = adapters.broker(url)
            except ValueError as e:
                return ("host-not-allowed", str(e))
            if not self.host_allowed(f"mqtt://{host}:{port}/"):
                return ("host-not-allowed", f"{host} is outside this tree's hosts {self.hosts}.")
            topic = str(spec.get("topic", ""))
            return None if adapters.topic_allowed(self.topics, topic) else ("topic-not-allowed", f"{topic!r} is outside this tree's topics {self.topics}.")
        if source == "memory":
            key = str(spec.get("key", ""))
            return None if adapters.key_allowed(self.keys, key) else ("key-not-allowed", f"{key!r} is outside this tree's keys {self.keys}.")
        return None

    def watch_kinds(self) -> list[str]:
        return [next(k for k in WATCH_KINDS if k in w) for w in self.watches]

    def sources(self) -> list[str]:
        """The forms of I/O this tree's nodes use."""
        return sorted({s for n in self.nodes.values() if n.kind in ("observe", "act") for s in adapters.SOURCES if n.get(s) is not None})

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
            **({"namespace": self.namespace} if self.namespace else {}),
            "description": self.description,
            "tags": self.tags,
            "hosts": self.hosts,
            **{k: getattr(self, k) for k in ("paths", "commands", "devices", "topics", "keys") if getattr(self, k)},
            "sources": self.sources(),
            **({"watches": [{"kind": k, "what": _watch_what(w)} for k, w in zip(self.watch_kinds(), self.watches)]} if self.watches else {}),
            "license": self.license,
            "language": self.language.to_dict(),
            "triggers": [t.pattern for t in self.triggers],
            # What a trigger fixes ("top story" means count = 1): a hub routes by these too.
            **({"trigger_inputs": {t.pattern: t.fixed_inputs for t in self.triggers if t.fixed_inputs}} if any(t.fixed_inputs for t in self.triggers) else {}),
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
    lists: dict[str, list[str]] = {}
    for key, noun in (("paths", "paths or globs"), ("commands", "program names"), ("devices", "device paths or globs"), ("topics", "topic filters"), ("keys", "memory keys or globs")):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise TreeError(f"{name}: '{key}' must be a list of {noun}")
        lists[key] = value
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise TreeError(f"{name}: 'tags' must be a list of strings")
    license_ = raw.get("license", "")
    if not isinstance(license_, str):
        raise TreeError(f"{name}: 'license' must be a string such as \"MIT\" (an SPDX identifier)")
    namespace = raw.get("namespace", "")
    if not isinstance(namespace, str) or (namespace and not NAME.match(namespace)):
        raise TreeError(f"{name}: 'namespace' must be lowercase letters, digits and dashes (who publishes the tree), got {namespace!r}")
    watches = raw.get("watches", [])
    if not isinstance(watches, list) or not all(isinstance(w, dict) for w in watches):
        raise TreeError(f"{name}: 'watches' is a list of tables ([[watches]])")
    tests = raw.get("tests", [])
    if not isinstance(tests, list) or not all(isinstance(t, dict) for t in tests):
        raise TreeError(f"{name}: 'tests' is a list of tables ([[tests]])")

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

    tree = Tree(name, description, version, hosts, triggers, inputs, start, nodes, tags, source, raw, license_.strip(), language, namespace, watches, **lists, tests=tests)
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

    used: set[str] = set()
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
            present = [s for s in adapters.SOURCES if node.get(s) is not None]
            frm = node.get("from")
            emit = node.get("emit")
            ways = len(present) + sum(1 for x in (frm, emit) if x is not None)
            if ways != 1:
                err(where, f"an {node.kind} node needs exactly one of {', '.join(repr(s) for s in adapters.SOURCES)}, 'from' or 'emit'")
            for s in present:
                used.add(s)
                _SOURCE_CHECKS[s](tree, node, node.get(s), err, warn)
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
            expect = node.get("expect")
            if expect is not None:
                if emit is not None:
                    err(where, "'expect' says what a good answer looks like; an emit has no answer")
                elif not isinstance(expect, dict) or not expect:
                    err(where, "'expect' is a table of path = condition, the shape of a good answer: { \"json.tag_name\" = { type = \"str\" } }")
                else:
                    for path, cond in expect.items():
                        try:
                            conditions.validate(cond)
                        except conditions.ConditionError as e:
                            err(f"{where}.expect.{path}", str(e))

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

        elif node.kind == "lookup":
            on = node.get("on")
            if not isinstance(on, str) or not on.strip():
                err(where, "a lookup node needs 'on': the state path it looks at")
            if not isinstance(node.get("into"), str):
                err(where, "a lookup node needs 'into': where the row's value goes")
            table = node.get("table")
            if not isinstance(table, list) or not table:
                err(where, "a lookup node needs a non-empty 'table' list of rows such as { eq = 0, value = \"clear\" }")
            else:
                for j, row in enumerate(table):
                    rw = f"{where}.table[{j}]"
                    if not isinstance(row, dict):
                        err(rw, "a row is a table: a condition and a 'value'")
                        continue
                    if "value" not in row:
                        err(rw, "a row needs a 'value'")
                    if "then" in row or "else" in row:
                        err(rw, "a row gives a value, not a node; a value for anything else is the node's 'default', and branching is a decide")
                        continue
                    cond = row_condition(row)
                    if not cond:
                        err(rw, "a row needs a condition such as eq or between; a value for anything else is the node's 'default'")
                        continue
                    try:
                        conditions.validate(cond)
                    except conditions.ConditionError as e:
                        err(rw, str(e))
            if "default" not in node.spec:
                info(where, "no 'default': a value no row covers is recorded as a gap and fails the run")

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

    for i, w in enumerate(tree.watches):
        used.update(_check_watch(tree, i, w, err, warn))

    for i, t in enumerate(tree.tests):
        _check_test(tree, i, t, err)

    for list_name, sources in _NEEDS.items():
        have = getattr(tree, list_name)
        using = sorted(used & sources)
        if using and not have:
            if using == ["http"]:
                err("hosts", "a tree that speaks HTTP must list the hosts it may reach")
            else:
                err(list_name, f"a tree with {', '.join(using)} nodes must list the {list_name} it may reach")
        if have and not using:
            info(list_name, f"{list_name} are listed but no node uses them")

    if tree.start in tree.nodes:
        problems.extend(_check_graph(tree))
    return problems


_NEEDS = {"hosts": {"http", "browser", "queue"}, "paths": {"file"}, "commands": {"shell"}, "devices": {"serial"}, "topics": {"queue"}, "keys": {"memory"}}
WATCH_KINDS = ("every", "file", "queue", "serial")
WATCH_ROOTS = ("event", "env", "now")


def _watch_what(w: dict[str, Any]) -> str:
    if "every" in w:
        return str(w["every"])
    if "file" in w:
        return str(w["file"])
    if "queue" in w and isinstance(w["queue"], dict):
        return f"{w['queue'].get('topic')} at {w['queue'].get('url')}"
    if "serial" in w and isinstance(w["serial"], dict):
        return str(w["serial"].get("device"))
    return "?"


def _check_watch(tree: Tree, i: int, w: dict[str, Any], err, warn) -> set[str]:
    """A watch is static: no template reaches a path, a topic or a device,
    so every one is checked here against the tree's lists. Returns the
    sources it uses, for the allow-list bookkeeping."""
    from .nlp import ParseError, parse_duration

    where = f"watches[{i}]"
    kinds = [k for k in WATCH_KINDS if k in w]
    if len(kinds) != 1:
        err(where, f"a watch is exactly one of {', '.join(WATCH_KINDS)}, with optional 'inputs' and 'name'")
        return set()
    kind = kinds[0]
    used: set[str] = set()
    if kind == "every":
        value = w["every"]
        try:
            seconds = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else parse_duration(str(value))
        except (ParseError, ValueError):
            seconds = 0
        if seconds < 1:
            err(where, f"'every' is a duration such as \"5m\" or \"1 hour\", got {value!r}")
    elif kind == "file":
        path = w["file"]
        if not isinstance(path, str) or not path or PLACEHOLDER.search(path):
            err(where, "'file' is a literal path; a watch cannot be decided at run time")
        elif tree.paths and not adapters.path_allowed(tree.paths, path):
            err(where, f"{path!r} is not under this tree's paths")
        used.add("file")
    elif kind == "queue":
        spec = w["queue"]
        if not isinstance(spec, dict) or not isinstance(spec.get("url"), str) or not isinstance(spec.get("topic"), str) or PLACEHOLDER.search(spec["url"] + spec["topic"]):
            err(where, "'queue' is a table with literal 'url' and 'topic'")
        else:
            try:
                _, host, port = adapters.broker(spec["url"])
            except ValueError as e:
                err(where, str(e))
            else:
                if tree.hosts and not tree.host_allowed(f"mqtt://{host}:{port}/"):
                    err(where, f"host {host!r} is not in this tree's hosts")
            if tree.topics and not adapters.topic_allowed(tree.topics, spec["topic"]):
                err(where, f"{spec['topic']!r} is not under this tree's topics")
        used.add("queue")
    elif kind == "serial":
        spec = w["serial"]
        if not isinstance(spec, dict) or not isinstance(spec.get("device"), str) or PLACEHOLDER.search(spec["device"]):
            err(where, "'serial' is a table with a literal 'device' and optionally 'baud', 'read_until'")
        elif tree.devices and not adapters.device_allowed(tree.devices, spec["device"]):
            err(where, f"{spec['device']!r} is not in this tree's devices")
        used.add("serial")
    inputs = w.get("inputs", {})
    if not isinstance(inputs, dict):
        err(where, "'inputs' is a table of input = value (templates may read event, env and now)")
        inputs = {}
    for k, v in inputs.items():
        if k not in tree.inputs:
            err(where, f"input {k!r} is not declared")
        for expr in placeholders(v):
            head = expr.split("|")[0].strip().split(".")[0].split("[")[0]
            if head not in WATCH_ROOTS:
                err(where, f"template root {head!r} is not one of {', '.join(WATCH_ROOTS)}")
    for k, spec in tree.inputs.items():
        if spec.required and spec.default is None and k not in inputs:
            err(where, f"input {k!r} is required and the watch does not give it")
    if w.get("name") is not None and not isinstance(w["name"], str):
        err(where, "'name' is a string")
    return used


TEST_KEYS = {"name", "sentence", "inputs", "fixture", "now", "env", "answers", "expect"}
EXPECT_KEYS = ("status", "say", "say_contains", "reason", "output", "path")
STATUSES = ("done", "failed", "asked", "error")


def _check_test(tree: Tree, i: int, t: dict[str, Any], err) -> None:
    """A test is checked for shape here; ``pyrtan test`` runs it."""
    where = f"tests[{i}]"
    unknown = sorted(set(t) - TEST_KEYS)
    if unknown:
        err(where, f"unknown keys {unknown}; a test has {', '.join(sorted(TEST_KEYS))}")
    if ("sentence" in t) == ("inputs" in t):
        err(where, "a test needs exactly one of 'sentence' (read it, then run what it reads to) or 'inputs' (run this tree with them)")
    if "sentence" in t:
        if not isinstance(t["sentence"], str) or not t["sentence"].strip():
            err(where, "'sentence' is the text a person would send")
        elif not tree.triggers:
            err(where, "this tree has no triggers, so no sentence reads to it; give 'inputs' instead")
    if "inputs" in t:
        if not isinstance(t["inputs"], dict):
            err(where, "'inputs' is a table of input = value")
        else:
            for k in t["inputs"]:
                if k not in tree.inputs:
                    err(where, f"input {k!r} is not declared")
    for key in ("name", "fixture"):
        if key in t and (not isinstance(t[key], str) or not t[key]):
            err(where, f"'{key}' is a string")
    if "now" in t:
        from .cases import parse_now

        try:
            parse_now(t["now"])
        except (TypeError, ValueError):
            err(where, f"'now' is a date-time such as \"2026-10-25T12:00:00Z\", got {t['now']!r}")
    if "env" in t and (not isinstance(t["env"], dict) or not all(isinstance(v, str) for v in t["env"].values())):
        err(where, "'env' is a table of name = text, what {{ env.NAME }} reads")
    if "answers" in t and not isinstance(t["answers"], list):
        err(where, "'answers' is a list: what each ask is answered, in order")
    expect = t.get("expect")
    if not isinstance(expect, dict) or not expect:
        err(where, f"a test needs 'expect': a table of what the run must come to ({', '.join(EXPECT_KEYS)})")
        return
    unknown = sorted(set(expect) - set(EXPECT_KEYS))
    if unknown:
        err(f"{where}.expect", f"unknown keys {unknown}; one of {', '.join(EXPECT_KEYS)}")
    if "status" in expect and expect["status"] not in STATUSES:
        err(f"{where}.expect", f"status is one of {', '.join(STATUSES)}, got {expect['status']!r}")
    if "path" in expect:
        steps = expect["path"]
        if not isinstance(steps, list) or not all(isinstance(n, str) for n in steps):
            err(f"{where}.expect", "'path' is the list of node ids the run walks, in order")
        else:
            for n in steps:
                if n not in tree.nodes:
                    err(f"{where}.expect", f"path names node {n!r}, which does not exist")


def _literal(value: Any) -> str | None:
    """The value when it is a string with no placeholder, else None."""
    return value if isinstance(value, str) and not PLACEHOLDER.search(value) else None


def _check_browser(tree: Tree, node: Node, spec: Any, err, warn) -> None:
    where = f"nodes.{node.id}.browser"
    if not isinstance(spec, dict) or not isinstance(spec.get("url"), str) or not spec["url"]:
        err(where, "'browser' is a table with 'url' and optionally 'wait_for', 'wait_until', 'actions'")
        return
    head = PLACEHOLDER.split(spec["url"], 1)[0]
    parsed = urllib.parse.urlsplit(head)
    if parsed.scheme and parsed.hostname:
        if parsed.scheme not in ("http", "https"):
            err(where, f"scheme {parsed.scheme!r} is not http or https")
        if not tree.host_allowed(head):
            err(where, f"host {parsed.hostname!r} is not in this tree's hosts")
    else:
        warn(where, "the host is decided at run time; the runtime refuses hosts outside the list")
    actions = spec.get("actions", [])
    if not isinstance(actions, list):
        err(where, "'actions' is a list of tables")
        return
    for i, a in enumerate(actions):
        keys = set(a) & set(adapters.BROWSER_ACTIONS) if isinstance(a, dict) else set()
        if len(keys) != 1:
            err(f"{where}.actions[{i}]", f"an action is a table with exactly one of {', '.join(adapters.BROWSER_ACTIONS)}")
        elif keys & {"fill", "press", "select"}:
            k = keys.pop()
            need = "value" if k != "press" else "key"
            if not isinstance(a[k], dict) or "selector" not in a[k] or need not in a[k]:
                err(f"{where}.actions[{i}]", f"'{k}' is a table with 'selector' and '{need}'")


def _check_file(tree: Tree, node: Node, spec: Any, err, warn) -> None:
    where = f"nodes.{node.id}.file"
    if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not spec["path"]:
        err(where, "'file' is a table with 'path' and optionally 'tail' (to read), 'write' or 'append' (to change)")
        return
    writes = "write" in spec or "append" in spec
    if writes and node.kind == "observe":
        err(where, "a write changes things; make this an act node")
    if not writes and node.kind == "act":
        warn(where, "an act that only reads; an observe node would retry it safely")
    if "write" in spec and "append" in spec:
        err(where, "'write' or 'append', not both")
    if spec.get("tail") is not None and not isinstance(spec["tail"], int) and not (isinstance(spec["tail"], str) and PLACEHOLDER.search(spec["tail"])):
        err(where, "'tail' is a number of lines")
    path = _literal(spec["path"])
    if path is None:
        warn(where, "the path is decided at run time; the runtime refuses paths outside the list")
    elif tree.paths and not adapters.path_allowed(tree.paths, path):
        err(where, f"{path!r} is not under this tree's paths")


def _check_shell(tree: Tree, node: Node, spec: Any, err, warn) -> None:
    where = f"nodes.{node.id}.shell"
    if not isinstance(spec, dict) or not isinstance(spec.get("command"), str) or not spec["command"]:
        err(where, "'shell' is a table with 'command' (a program name) and optionally 'args' (a list), 'stdin', 'env', 'cwd'")
        return
    if spec.get("args") is not None and not isinstance(spec["args"], list):
        err(where, "'args' is a list; no shell reads the line, so there is nothing to quote")
    cmd = _literal(spec["command"])
    if cmd is None:
        warn(where, "the command is decided at run time; the runtime refuses commands outside the list")
    elif tree.commands and not adapters.command_allowed(tree.commands, cmd):
        err(where, f"{cmd!r} is not in this tree's commands")


def _check_serial(tree: Tree, node: Node, spec: Any, err, warn) -> None:
    where = f"nodes.{node.id}.serial"
    if not isinstance(spec, dict) or not isinstance(spec.get("device"), str) or not spec["device"]:
        err(where, "'serial' is a table with 'device' and optionally 'baud', 'write', 'read_until' or 'read'")
        return
    if spec.get("baud") is not None and not isinstance(spec["baud"], int):
        err(where, "'baud' is a number")
    dev = _literal(spec["device"])
    if dev is None:
        warn(where, "the device is decided at run time; the runtime refuses devices outside the list")
    elif tree.devices and not adapters.device_allowed(tree.devices, dev):
        err(where, f"{dev!r} is not in this tree's devices")


def _check_queue(tree: Tree, node: Node, spec: Any, err, warn) -> None:
    where = f"nodes.{node.id}.queue"
    if not isinstance(spec, dict) or not isinstance(spec.get("url"), str) or not isinstance(spec.get("topic"), str):
        err(where, "'queue' is a table with 'url' (mqtt://host[:port]), 'topic' and, to send, 'publish'")
        return
    if "publish" in spec and node.kind == "observe":
        err(where, "a publish changes things; make this an act node")
    if "publish" not in spec and node.kind == "act":
        warn(where, "an act that only waits for a message; an observe node would retry it safely")
    url = _literal(spec["url"])
    if url is None:
        warn(where, "the broker is decided at run time; the runtime refuses hosts outside the list")
    else:
        try:
            _, host, port = adapters.broker(url)
        except ValueError as e:
            err(where, str(e))
        else:
            if tree.hosts and not tree.host_allowed(f"mqtt://{host}:{port}/"):
                err(where, f"host {host!r} is not in this tree's hosts")
    topic = _literal(spec["topic"])
    if topic is None:
        warn(where, "the topic is decided at run time; the runtime refuses topics outside the list")
    elif tree.topics and not adapters.topic_allowed(tree.topics, topic):
        err(where, f"{topic!r} is not under this tree's topics")


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


def _check_memory(tree: Tree, node: Node, spec: Any, err, warn) -> None:
    from . import memory

    where = f"nodes.{node.id}.memory"
    if not isinstance(spec, dict) or not isinstance(spec.get("key"), str) or not spec["key"]:
        err(where, "'memory' is a table with 'key' and, to change it, one of 'write', 'add' or 'delete', and optionally 'ttl'")
        return
    unknown = sorted(set(spec) - {"key", "ttl", *memory.MODES})
    if unknown:
        err(where, f"unknown keys {unknown}; a memory table has key, write, add, delete and ttl")
    modes = [m for m in memory.MODES if m in spec]
    if len(modes) > 1:
        err(where, f"one of 'write', 'add' or 'delete', not {' and '.join(modes)}")
    if modes and node.kind == "observe":
        err(where, f"'{modes[0]}' changes what is kept; make this an act node")
    if not modes and node.kind == "act":
        warn(where, "an act that only reads; an observe node would retry it safely")
    if "delete" in spec and spec["delete"] is not True:
        err(where, "'delete' is true, or absent")
    if "add" in spec and not (isinstance(spec["add"], (int, float)) and not isinstance(spec["add"], bool)) and not (isinstance(spec["add"], str) and PLACEHOLDER.search(spec["add"])):
        err(where, "'add' is a number (a missing key counts from 0)")
    if "ttl" in spec:
        if not ({"write", "add"} & set(spec)):
            err(where, "'ttl' goes with 'write' or 'add': how long the value is kept")
        elif not (isinstance(spec["ttl"], str) and PLACEHOLDER.search(spec["ttl"])):
            try:
                memory.ttl_seconds(spec["ttl"])
            except memory.MemoryRefused as e:
                err(where, str(e))
    key = _literal(spec["key"])
    if key is None:
        warn(where, "the key is decided at run time; the runtime refuses keys outside the list")
    else:
        if not memory.KEY.match(key):
            err(where, f"{key!r} is not a memory key: up to 200 letters, digits and . _ : @ / -, starting with a letter or digit")
        if tree.keys and not adapters.key_allowed(tree.keys, key):
            err(where, f"{key!r} is not in this tree's keys")


_SOURCE_CHECKS = {"http": _check_http, "browser": _check_browser, "file": _check_file, "shell": _check_shell, "serial": _check_serial, "queue": _check_queue, "memory": _check_memory}


def row_condition(row: dict[str, Any]) -> dict[str, Any]:
    """A lookup row's condition: the row without its value and note."""
    return {k: v for k, v in row.items() if k not in ROW_KEYS}


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
