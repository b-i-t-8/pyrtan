"""The builder: a sentence nobody answered becomes a tree, one question at a time.

``pyrtan new`` writes a fixed skeleton for an unanswered sentence. This
module asks the author the few things the skeleton cannot know, and writes
the tree those answers describe:

- what the tree *reads* to answer the sentence (an API or page, a file, a
  program, a browser, another tree, or nothing),
- where (the URL, path, command or sentence), and what to *pull out* of
  what comes back (a JSON path, a CSS selector or a regex),
- what it *decides on* and which *branches* that decision has,
- what it *says* when it is done.

The questions are a decision tree themselves: which ones apply depends on
the answers before them, and every one has a default derived by rules
from the sentence, so a build with no answers at all still passes
``pyrtan check``. :func:`questions` lists what applies so far,
:func:`build` renders the file and checks it, and :class:`BuildError`
names the one answer that will not do, so a prompt can ask again.

No model is involved. Every default and every reading of an answer is a
rule in this file; the author decides what the tree means.
"""

from __future__ import annotations

import re
import shlex
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .grow import Proposal, _toml_str, propose
from . import extract as extractmod
from .intent import SLOT, Trigger, TriggerError
from .template import placeholders
from .tree import Tree, TreeError, check, load_tree_text

SOURCES: dict[str, str] = {
    "http": "an API or a web page, over HTTP",
    "file": "a file on the box",
    "shell": "a program on the box",
    "browser": "a page that needs a script to render",
    "delegate": "another tree in the swarm",
    "none": "nothing: it just replies",
}
FORMATS: dict[str, str] = {"json": "JSON", "html": "an HTML page", "text": "plain text"}

# What each source's reading node is called, what it lands in, and what the
# first decision looks at.
NODES: dict[str, tuple[str, str, str]] = {
    "http": ("fetch", "resp", "resp.status"),
    "file": ("read", "file", "file.exists"),
    "shell": ("run", "run", "run.status"),
    "browser": ("open", "page", "page.status"),
    "delegate": ("ask-swarm", "w", "w.status"),
}
DEFAULT_BRANCHES: dict[str, str] = {
    "http": "200=reply, 404=not-found, 500-599=down",
    "file": "true=reply, false=missing",
    "shell": "0=reply",
    "browser": "200=reply",
    "delegate": "done=reply",
}
SLOT_TYPES: dict[str | None, str] = {"int": "int", "float": "float", "number": "float", "duration": "int"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class BuildError(ValueError):
    """An answer that will not do: ``key`` names the question."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


@dataclass
class Question:
    key: str
    prompt: str
    default: Any
    choices: dict[str, str] | None = None
    help: str = ""
    answer: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"key": self.key, "prompt": self.prompt, "default": self.default, "answer": self.answer, "help": self.help}
        if self.choices:
            d["choices"] = [{"value": k, "label": v} for k, v in self.choices.items()]
        return d


@dataclass
class Built:
    """A tree the builder wrote: the questions it asked (each with the
    answer taken), the file, and what ``pyrtan check`` says of it."""

    text: str
    name: str
    questions: list[Question]
    answers: dict[str, Any]
    toml: str
    problems: list[dict[str, str]] = field(default_factory=list)
    tree: Tree | None = None

    @property
    def ok(self) -> bool:
        return self.tree is not None and not any(p["level"] == "error" for p in self.problems)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "name": self.name, "ok": self.ok, "questions": [q.to_dict() for q in self.questions], "answers": self.answers, "toml": self.toml, "problems": self.problems}


# ---------------------------------------------------------------- the questions


def questions(text: str, answers: dict[str, Any] | None = None, name: str | None = None) -> list[Question]:
    """The questions that apply to ``text`` given ``answers`` so far, in
    order, each carrying its default and the answer taken (the default
    where none was given). Raises :class:`BuildError` on an answer that
    will not do."""
    return _resolve(text, answers or {}, name)[0]


def build(text: str, answers: dict[str, Any] | None = None, name: str | None = None) -> Built:
    """The tree the answers describe, rendered and checked."""
    qs, taken, notes = _resolve(text, answers or {}, name)
    toml = _render(text, taken)
    problems = list(notes)
    tree: Tree | None = None
    try:
        tree = load_tree_text(toml, source=f"{taken['name']}.toml")
        problems += [{"level": p.level, "where": p.where, "message": p.message} for p in check(tree)]
    except TreeError as e:
        problems.append({"level": "error", "where": "file", "message": str(e)})
    return Built(text, taken["name"], qs, taken, toml, problems, tree)


def _resolve(text: str, given: dict[str, Any], name: str | None) -> tuple[list[Question], dict[str, Any], list[dict[str, str]]]:
    proposal = propose(text, name)
    qs: list[Question] = []
    taken: dict[str, Any] = {}
    notes: list[dict[str, str]] = []

    def ask(key: str, prompt: str, default: Any, read, choices: dict[str, str] | None = None, help: str = "") -> Any:
        raw = given.get(key)
        shown = default if raw is None or (isinstance(raw, str) and raw.strip() == "") else raw
        value = read(shown)  # a default is read like an answer, so what is taken has one shape
        qs.append(Question(key, prompt, default, choices, help, shown))
        taken[key] = value
        return value

    ask("name", "The tree's name", proposal.name, _read_name, help="Lowercase letters, digits and dashes: its identity in the swarm, and its file name.")
    ask("description", "What it does, in one line", f"What {_plain(text)!r} asks for.", lambda v: str(v).strip())
    trigger = ask("trigger", "The sentence it answers to", proposal.trigger, lambda v: _read_trigger(v, text), help="Slots are {name} or {name:kind}; [words] are optional; (a|b) is a choice. It must still match the sentence it came from.")
    inputs = _inputs_for(trigger, proposal)
    host_slot = next((s for s, spec in inputs.items() if spec.get("role") == "host"), None)
    source = ask("source", "What does it read to answer?", "http", lambda v: _read_choice("source", v, SOURCES), SOURCES)

    value: str | None = None
    if source in ("http", "browser"):
        default_url = f"https://{{{{ inputs.{host_slot} | lower }}}}/" if host_slot else "https://example.com/"
        url = ask("url", "The URL", default_url, lambda v: _read_url(v, inputs), help="{{ inputs.x }} fills in a slot; a host decided that way opens hosts to * and the runtime checks it.")
        if source == "browser":
            ask("wait_for", "The selector to wait for before reading", "h1", _read_nonempty)
            fmt = "html"
        else:
            fmt = ask("format", "What comes back", _guess_format(url), lambda v: _read_choice("format", v, FORMATS), FORMATS)
        pick = ask("pick", "What to pull out of it", _default_pick(fmt), lambda v: _read_pick(v, fmt), help="A JSON path (json.title), a CSS selector (h1, .price), a pattern (price: {p:number}) or /a regex/ with one group.")
        value = _value_name(pick)
    elif source == "file":
        ask("path", "The file's path", "/var/log/syslog", _read_path)
        ask("tail", "How many lines from the end (0 for the whole file)", 200, _read_int)
        pick = ask("pick", "What to pull out of it", "/(.+)/", lambda v: _read_pick(v, "text"), help="A pattern (load average: {load:number}) or /a regex/ with one group; the first match is taken.")
        value = _value_name(pick)
    elif source == "shell":
        ask("command", "The program to run", "df", _read_command, help="One program, by name or full path; it is run without a shell.")
        ask("args", "Its arguments", "-h", _read_args, help="As you would type them; quotes group words.")
        pick = ask("pick", "What to pull out of its output", "/(.+)/", lambda v: _read_pick(v, "text"), help="A pattern (used {pct:int}%) or /a regex/ with one group over what it printed; the first match is taken.")
        value = _value_name(pick)
    elif source == "delegate":
        intent = ask("intent", "The sentence to hand the swarm", _plain(text), _read_nonempty, help="Another tree answers it; its say and output land in state.w.")
        if intent.strip().lower() == _plain(text).lower():
            notes.append({"level": "warn", "where": "intent", "message": "the sentence handed on is this tree's own; give one another tree answers, or this tree asks itself"})

    if source != "none":
        node, into, on = NODES[source]
        ask("branches", f"The branches for {on}, as value=node", DEFAULT_BRANCHES[source], lambda v: _read_branches(v, on), help="reply is the done node; any other node is a fail node drafted for you. A number, a-b, >n, <n, true, false, missing, empty, /regex/ or a word; else=node names the fallback.")
    default_say = f"{{{{ state.{value} }}}}" if value else ("{{ state.w.say }}" if source == "delegate" else "Done.")
    ask("say", "What it says when done", default_say, lambda v: _read_say(v, inputs, value), help="A template: {{ inputs.x }}, {{ state.y }}, filters with |.")
    return qs, taken, notes


# ---------------------------------------------------------------- reading answers


def _plain(text: str) -> str:
    return propose(text).text


def _read_name(v: Any) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", str(v).strip().lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    if not name or not NAME_RE.match(name):
        raise BuildError("name", f"{v!r} will not do as a name: lowercase letters, digits and dashes, starting with a letter or digit")
    return name


def _read_trigger(v: Any, text: str) -> str:
    pattern = str(v).strip()
    try:
        trig = Trigger(pattern)
    except TriggerError as e:
        raise BuildError("trigger", f"that trigger does not compile: {e}") from e
    if trig.match(text) is None:
        raise BuildError("trigger", f"{pattern!r} does not match {_plain(text)!r}, the sentence this tree is for")
    return pattern


def _inputs_for(trigger: str, proposal: Proposal) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    for slot, kind in Trigger(trigger).slots.items():
        spec = dict(proposal.inputs.get(slot) or {})
        spec["type"] = SLOT_TYPES.get(kind, "str")
        if kind == "host":
            spec["role"] = "host"
        spec.setdefault("description", f"from the sentence, as {{{slot}}}" if kind in (None, "text") else f"from the sentence, as {{{slot}:{kind}}}")
        inputs[slot] = spec
    return inputs


def _read_choice(key: str, v: Any, choices: dict[str, str]) -> str:
    s = str(v).strip().lower()
    if s not in choices:
        raise BuildError(key, f"{v!r} is not one of {', '.join(choices)}")
    return s


def _read_url(v: Any, inputs: dict[str, Any]) -> str:
    url = str(v).strip()
    if not url.startswith(("http://", "https://")):
        raise BuildError("url", f"{url!r} does not start with http:// or https://")
    for ph in placeholders(url):
        root, _, rest = ph.partition(".")
        if root != "inputs" or rest.split(".")[0].split("[")[0] not in inputs:
            raise BuildError("url", f"{{{{ {ph} }}}} names nothing the trigger provides; the slots are {', '.join(inputs) or 'none'}")
    return url


def _read_nonempty(v: Any) -> str:
    s = str(v).strip()
    if not s:
        raise BuildError("value", "this cannot be empty")
    return s


def _read_path(v: Any) -> str:
    s = str(v).strip()
    if not s.startswith("/") and not s.startswith("{{"):
        raise BuildError("path", f"{s!r} is not an absolute path")
    return s


def _read_int(v: Any) -> int:
    try:
        n = int(str(v).strip())
    except ValueError:
        raise BuildError("tail", f"{v!r} is not a whole number") from None
    if n < 0:
        raise BuildError("tail", "a count cannot be negative")
    return n


def _read_command(v: Any) -> str:
    s = str(v).strip()
    if not s or any(c.isspace() for c in s):
        raise BuildError("command", f"{s!r} is not one program; arguments go in the next answer")
    return s


def _read_args(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    try:
        return shlex.split(str(v))
    except ValueError as e:
        raise BuildError("args", f"could not read the arguments: {e}") from e


def _read_pick(v: Any, fmt: str) -> dict[str, Any]:
    """A JSON path, a CSS selector, a regex or a pattern, told apart by
    shape: a path starts with ``json``, a regex is between slashes, a
    pattern has a ``{slot}``, the rest is a selector (or, for plain text, a
    regex as written)."""
    s = str(v).strip()
    if not s:
        raise BuildError("pick", "say what to pull out")
    if not s.startswith(("/", "json")) and SLOT.search(s):
        try:
            extractmod.validate({"into": "x", "pattern": s})
        except extractmod.ExtractError as e:
            raise BuildError("pick", str(e).replace(" in extract 'x'", "")) from e
        return {"pattern": s}
    if len(s) > 2 and s.startswith("/") and s.endswith("/"):
        body = s[1:-1]
        try:
            groups = re.compile(body).groups
        except re.error as e:
            raise BuildError("pick", f"that regex does not compile: {e}") from e
        return {"regex": body, "group": 1} if groups else {"regex": body}
    if s == "json" or s.startswith(("json.", "json[")):
        return {"json": s}
    if fmt == "json":
        return {"json": "json." + s.lstrip(".")}
    if fmt == "text":
        try:
            groups = re.compile(s).groups
        except re.error as e:
            raise BuildError("pick", f"that regex does not compile: {e}") from e
        return {"regex": s, "group": 1} if groups else {"regex": s}
    return {"css": s}


def _value_name(pick: dict[str, Any]) -> str:
    if "json" in pick:
        keys = KEY_RE.findall(pick["json"].replace("json", "", 1))
        return (keys[-1].lower() if keys else "data")
    if "css" in pick:
        last = re.split(r"[\s>,]+", pick["css"].strip())[-1]
        words = re.findall(r"[a-z0-9]+", last.lower())
        return "_".join(words) if words and words[0][0].isalpha() else "value"
    if "pattern" in pick:
        slots = list(SLOT.findall(pick["pattern"]))
        return slots[0][0] if len(slots) == 1 else "hit"
    return "hit"


def _read_branches(v: Any, on: str) -> list[dict[str, Any]]:
    """``value=node, value=node, else=node`` as a branch list. Each value
    reads as a condition by its shape; each node name is kept, ``reply``
    being the done node, ``else`` the fallback."""
    if isinstance(v, list):
        pairs = [(b.get("value", ""), b.get("then", "")) for b in v if isinstance(b, dict)]
    else:
        pairs = []
        for part in re.split(r"\s*[,;]\s*", str(v).strip()):
            if not part:
                continue
            if "=" not in part:
                raise BuildError("branches", f"{part!r} is not value=node")
            lhs, rhs = part.rsplit("=", 1)  # the last =, so >=500=down reads as >=500 and down
            pairs.append((lhs.strip(), rhs.strip()))
    if not pairs:
        raise BuildError("branches", "give at least one branch, such as 200=reply")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lhs, node in pairs:
        node = node.lower()
        if not NODE_RE.match(node):
            raise BuildError("branches", f"{node!r} will not do as a node name: lowercase letters, digits and dashes")
        if lhs.lower() == "else":
            out.append({"else": node})
            continue
        if lhs in seen:
            raise BuildError("branches", f"{lhs!r} is given twice")
        seen.add(lhs)
        out.append({"when": _condition(lhs, "branches"), "then": node})
    elses = [b for b in out if "else" in b]
    if len(elses) > 1:
        raise BuildError("branches", "only one else")
    if elses:
        out = [b for b in out if "else" not in b] + elses
    if not any(b.get("then") == "reply" for b in out) and not any(b.get("else") == "reply" for b in out):
        raise BuildError("branches", f"no branch of {on} reaches reply, so the tree could never say anything")
    return out


def _condition(spec: str, key: str) -> dict[str, Any]:
    s = spec.strip()
    low = s.lower()
    if low == "missing":
        return {"exists": False}
    if low == "empty":
        return {"empty": True}
    if low in ("true", "false"):
        return {"eq": low == "true"}
    if len(s) > 2 and s.startswith("/") and s.endswith("/"):
        try:
            re.compile(s[1:-1])
        except re.error as e:
            raise BuildError(key, f"that regex does not compile: {e}") from e
        return {"matches": s[1:-1]}
    m = re.match(r"^(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        op = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}[m.group(1)]
        return {op: _number(m.group(2))}
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(?:-|\.\.)\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        lo, hi = _number(m.group(1)), _number(m.group(2))
        if lo > hi:
            raise BuildError(key, f"{s!r} runs backwards")
        return {"between": [lo, hi]}
    if re.match(r"^-?\d+(?:\.\d+)?$", s):
        return {"eq": _number(s)}
    if "|" in s:
        return {"in": [_scalar(x.strip()) for x in s.split("|") if x.strip()]}
    return {"eq": s}


def _number(s: str) -> int | float:
    return float(s) if "." in s else int(s)


def _scalar(s: str) -> Any:
    return _number(s) if re.match(r"^-?\d+(?:\.\d+)?$", s) else s


def _read_say(v: Any, inputs: dict[str, Any], value: str | None) -> str:
    s = str(v).strip()
    if not s:
        raise BuildError("say", "a done node should say something")
    for ph in placeholders(s):
        root = ph.split(".")[0].split("[")[0]
        if root not in ("inputs", "state", "tree", "now", "env"):
            raise BuildError("say", f"{{{{ {ph} }}}} starts with {root!r}; templates read inputs, state, tree, now or env")
    return s


# ---------------------------------------------------------------- rendering


def _guess_format(url: str) -> str:
    head = url.split("{{", 1)[0]
    parsed = urllib.parse.urlsplit(head)
    host = (parsed.hostname or "").lower()
    path = urllib.parse.urlsplit(url).path.lower()
    if host.startswith("api.") or "/api/" in path or path.endswith(".json") or "/v1/" in path or "/v2/" in path:
        return "json"
    return "html"


def _default_pick(fmt: str) -> str:
    return {"json": "json.title", "html": "title", "text": "/(.+)/"}[fmt]


def _render(text: str, a: dict[str, Any]) -> str:
    source = a["source"]
    inputs = _inputs_for(a["trigger"], propose(text, a["name"]))
    lines: list[str] = []
    w = lines.append
    w(f"# {a['name']}: built by pyrtan build from the sentence: {_toml_str(_plain(text))}")
    w(f'name = "{_toml_str(a["name"])}"')
    w("version = 1")
    w(f'description = "{_toml_str(a["description"])}"')
    w("tags = []")
    w('license = ""            # an SPDX identifier such as "MIT", if you share it')
    lists = _allow_lists(a)
    for key, items in lists.items():
        w(f"{key} = [{', '.join(_toml_quoted(i) for i in items)}]")
    start = NODES[source][0] if source != "none" else "reply"
    w(f'start = "{start}"')
    w("")
    w("[[triggers]]")
    w(f'pattern = "{_toml_str(a["trigger"])}"')
    for slot, spec in inputs.items():
        w("")
        w(f"[inputs.{slot}]")
        w(f'type = "{spec["type"]}"')
        if spec.get("description"):
            w(f'description = "{_toml_str(spec["description"])}"')
    w("")
    value: str | None = None
    if source == "none":
        w("[nodes.reply]")
        w('kind = "done"')
        w(f'say = "{_toml_str(a["say"])}"')
        w(f"output = {{ {', '.join(f'{s} = \"{{{{ inputs.{s} }}}}\"' for s in inputs) or 'said = \"' + _toml_str(a['say']) + '\"'} }}")
        return "\n".join(lines) + "\n"

    node, into, on = NODES[source]
    pick = a.get("pick")
    value = _value_name(pick) if pick else None
    dashed = value.replace("_", "-") if value else None  # a node id has no underscores; a state key may
    w(f"[nodes.{node}]")
    w('kind = "observe"')
    if source == "http":
        w(f'http = {{ method = "GET", url = "{_toml_str(a["url"])}"' + (', headers = { Accept = "application/json" }' if a.get("format") == "json" else "") + " }")
    elif source == "browser":
        w(f'browser = {{ url = "{_toml_str(a["url"])}", wait_for = "{_toml_str(a["wait_for"])}" }}')
    elif source == "file":
        tail = f", tail = {a['tail']}" if a.get("tail") else ""
        w(f'file = {{ path = "{_toml_str(a["path"])}"{tail} }}')
    elif source == "shell":
        w(f'shell = {{ command = "{_toml_str(a["command"])}", args = [{", ".join(_toml_quoted(x) for x in a["args"])}] }}')
    elif source == "delegate":
        lines[-1] = 'kind = "delegate"'
        w(f'intent = "{_toml_str(a["intent"])}"')
    w(f'into = "{into}"')
    if pick:
        w(f"extract = [{{ into = \"{value}\", {_pick_toml(pick)} }}]")
    w('next = "status"')
    w("")
    w("[nodes.status]")
    w('kind = "decide"')
    w(f'on = "{on}"')
    w("branches = [")
    branches: list[dict[str, Any]] = a["branches"]
    have = f"have-{dashed}" if value else None
    fails: list[str] = []
    else_node = "unexpected"
    for b in branches:
        if "else" in b:
            else_node = b["else"]
            continue
        then = b["then"]
        if then == "reply" and have:
            then = have
        elif then != "reply" and then not in fails:
            fails.append(then)
        w(f"  {{ when = {_cond_toml(b['when'])}, then = \"{then}\" }},")
    if else_node == "reply" and have:
        else_node = have
    elif else_node != "reply" and else_node not in fails:
        fails.append(else_node)
    w(f'  {{ else = "{else_node}" }},')
    w("]")
    w("")
    if have:
        w(f"[nodes.{have}]")
        w('kind = "decide"')
        w(f'on = "{value}"')
        w(f'branches = [{{ when = {{ exists = true }}, then = "reply" }}, {{ else = "no-{dashed}" }}]')
        w("")
    w("[nodes.reply]")
    w('kind = "done"')
    w(f'say = "{_toml_str(a["say"])}"')
    outs = [f'{s} = "{{{{ inputs.{s} }}}}"' for s in inputs]
    if value:
        outs.append(f'{value} = "{{{{ state.{value} }}}}"')
    elif source == "delegate":
        outs.append('answer = "{{ state.w.output }}"')
    w(f"output = {{ {', '.join(outs)} }}")
    if have:
        w("")
        w(f"[nodes.no-{dashed}]")
        w('kind = "fail"')
        w(f'reason = "no-{dashed}"')
        w(f'say = "{_toml_str(_where(a))} answered, but {_pick_words(pick)} found nothing."')
    for f in fails:
        w("")
        w(f"[nodes.{f}]")
        w('kind = "fail"')
        w(f'reason = "{f}"')
        words = "something nobody has named yet" if f == "unexpected" else f.replace("-", " ")
        w(f'say = "{_toml_str(_where(a))}: {on} was {{{{ state.{on} }}}} ({words})."')
    return "\n".join(lines) + "\n"


def _allow_lists(a: dict[str, Any]) -> dict[str, list[str]]:
    source = a["source"]
    if source in ("http", "browser"):
        head = a["url"].split("{{", 1)[0]
        host = urllib.parse.urlsplit(head).hostname
        return {"hosts": [host.lower()] if host else ["*"]}
    if source == "file":
        path = a["path"]
        return {"paths": [path if not path.startswith("{{") else "*"]}
    if source == "shell":
        return {"commands": [a["command"]]}
    return {}


def _where(a: dict[str, Any]) -> str:
    source = a["source"]
    if source in ("http", "browser"):
        host = urllib.parse.urlsplit(a["url"].split("{{", 1)[0]).hostname
        if host:
            return host
        ph = placeholders(a["url"])
        return f"{{{{ {ph[0]} }}}}" if ph else "the site"
    if source == "file":
        return a["path"]
    if source == "shell":
        return a["command"]
    return "the swarm"


def _pick_words(pick: dict[str, Any]) -> str:
    if "json" in pick:
        return f"the path {pick['json']}"
    if "css" in pick:
        return f"the selector {pick['css']}"
    if "pattern" in pick:
        return f"the pattern {pick['pattern']}"
    return "the regex"


def _pick_toml(pick: dict[str, Any]) -> str:
    return ", ".join(f"{k} = {_toml_scalar(v)}" for k, v in pick.items())


def _cond_toml(cond: dict[str, Any]) -> str:
    return "{ " + ", ".join(f"{k} = {_toml_scalar(v)}" for k, v in cond.items()) + " }"


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    return _toml_quoted(str(v))


def _toml_quoted(s: str) -> str:
    return '"' + _toml_str(s) + '"'
