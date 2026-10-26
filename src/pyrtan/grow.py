"""Where the swarm grows: sentences nobody answered, and the tree that would.

A ``decide`` that meets a value no branch covers records a *gap*: a branch
waiting to be written. A sentence no tree answers is the same thing one
level up: a tree waiting to be written. This module gives that second case
the same treatment the first already has.

- :func:`propose` turns a sentence into a :class:`Proposal`: a tree name
  drawn from its words, a trigger pattern with slots where the sentence had
  numbers or hosts, and the inputs those slots need.
- :meth:`Proposal.scaffold` renders the proposal as a tree file an author
  fills in, the same skeleton ``pyrtan new`` writes.
- :class:`Unanswered` is the log a service or hub keeps of sentences it
  could not answer, counted, each with its proposal. ``GET /unanswered``
  serves it; ``pyrtan unanswered --at URL --scaffold DIR`` turns it into
  files.

No model is involved. The proposal is derived from the words by rules, and
it is a starting point, not an answer: the author decides what the tree
observes and how it decides.
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import conditions
from .intent import Trigger, TriggerError, normalise
from .tree import Tree, TreeError, check, load_tree

# Words that carry no meaning for a tree's name: dropped from the name,
# kept in the trigger (where "what's the" is worth matching literally).
FILLER = {"a", "an", "the", "what", "what's", "whats", "how", "please", "me", "my", "tell", "show", "give", "is", "are", "of", "to", "for", "in", "on", "at", "do", "does", "it", "and", "with"}
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?$", re.IGNORECASE)
PATH_RE = re.compile(r"^[\w.\-]+/[\w.\-/]+$")
NAME_MAX = 40
UNANSWERED_LIMIT = 200

SCAFFOLD = '''# {name}: say what this tree does, in one line, below.
# Proposed from the sentence: {text}

name = "{name}"
version = 1
description = ""
tags = []
license = ""            # an SPDX identifier such as "MIT", if you share it
hosts = {hosts}
start = "fetch"

[[triggers]]
pattern = "{trigger}"
{inputs}
[nodes.fetch]
kind = "observe"
http = {{ method = "GET", url = "{url}" }}
into = "resp"
extract = [{{ into = "title", css = "title" }}]
next = "status"

[nodes.status]
kind = "decide"
on = "resp.status"
branches = [
  {{ when = {{ eq = 200 }}, then = "reply" }},
  {{ else = "unexpected" }},
]

[nodes.reply]
kind = "done"
say = "{{{{ state.title }}}}"
output = {{ title = "{{{{ state.title }}}}" }}

[nodes.unexpected]
kind = "fail"
reason = "unexpected-response"
say = "{host_word} answered {{{{ state.resp.status }}}}."
'''

INPUT_BLOCK = '''
[inputs.{slot}]
type = "{type}"
description = "{description}"
'''


@dataclass
class Proposal:
    """A tree that would answer ``text``, before anyone has written it."""

    text: str
    name: str
    trigger: str
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "name": self.name, "trigger": self.trigger, "inputs": self.inputs, "scaffold": self.command()}

    def command(self, directory: str | None = None) -> str:
        """The ``pyrtan new`` invocation that writes this tree's file."""
        parts = ["pyrtan", "new", self.name, "--from", _quote(self.text)]
        if directory:
            parts += ["--dir", _quote(directory)]
        return " ".join(parts)

    def scaffold(self) -> str:
        """The tree file, ready for ``pyrtan check`` and for an author."""
        host_slot = next((s for s, spec in self.inputs.items() if spec.get("role") == "host"), None)
        if host_slot:
            hosts, url, host_word = '["*"]', f"https://{{{{ inputs.{host_slot} | lower }}}}/", f"{{{{ inputs.{host_slot} }}}}"
        else:
            hosts, url, host_word = '["example.com"]', "https://example.com/", "example.com"
        inputs = "".join(INPUT_BLOCK.format(slot=slot, type=spec["type"], description=spec.get("description", "")) for slot, spec in self.inputs.items())
        return SCAFFOLD.format(name=self.name, text=_toml_safe(self.text), hosts=hosts, trigger=_toml_safe(self.trigger), inputs=inputs, url=url, host_word=host_word)


def propose(text: str, name: str | None = None) -> Proposal:
    """A proposal for the sentence ``text``.

    Numbers in the sentence become ``{count:int}`` / ``{value:float}``
    slots, host names ``{host:host}``, ``owner/repo``-shaped words
    ``{target:word}``; every other word is literal. The name is the literal
    words with filler dropped, or ``name`` if given.
    """
    clean = normalise(text) or text.strip() or "new tree"
    tokens = clean.split(" ")
    pieces: list[str] = []
    inputs: dict[str, dict[str, Any]] = {}
    literal_words: list[str] = []
    for tok in tokens:
        slot = _slot_for(tok, inputs)
        if slot is not None:
            pieces.append(slot)
        else:
            pieces.append(_literal(tok))
            literal_words.append(tok)
    trigger = " ".join(p for p in pieces if p)
    try:
        Trigger(trigger)
    except TriggerError:
        # Everything became a slot, or the words left nothing matchable:
        # fall back to the sentence itself, literally.
        trigger = " ".join(_literal(t) for t in tokens if _literal(t)) or "new tree"
        inputs = {}
        literal_words = tokens
    return Proposal(text=clean, name=_name_for(name, literal_words, tokens, list(inputs)), trigger=trigger, inputs=inputs)


def _slot_for(tok: str, inputs: dict[str, dict[str, Any]]) -> str | None:
    bare = tok.strip("\"'(),;:!?")
    if INT_RE.match(bare):
        slot = _unique("count", inputs)
        inputs[slot] = {"type": "int", "description": f"as in: {bare}"}
        return f"{{{slot}:int}}"
    if FLOAT_RE.match(bare):
        slot = _unique("value", inputs)
        inputs[slot] = {"type": "float", "description": f"as in: {bare}"}
        return f"{{{slot}:float}}"
    if HOST_RE.match(bare) and not bare.endswith(("...", ".")):
        slot = _unique("host", inputs)
        inputs[slot] = {"type": "str", "description": f"a host name, as in: {bare}", "role": "host"}
        return f"{{{slot}:host}}"
    if PATH_RE.match(bare):
        slot = _unique("target", inputs)
        inputs[slot] = {"type": "str", "description": f"as in: {bare}"}
        return f"{{{slot}:word}}"
    return None


def _unique(base: str, taken: dict[str, Any]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def _literal(tok: str) -> str:
    # Trigger syntax characters would be read as slots or groups; a literal
    # word must not contain them.
    return re.sub(r"[{}\[\]()|]", "", tok).strip()


def _name_for(given: str | None, literal_words: list[str], tokens: list[str], slots: list[str] = ()) -> str:
    if given:
        return given
    words = [re.sub(r"[^a-z0-9]+", "-", w.lower()).strip("-") for w in literal_words if w.lower() not in FILLER]
    words = [w for w in words if w]
    if len(words) < 2 and slots:
        # "is example.com up" would be called "up"; "host-up" says more.
        words = [re.sub(r"\d+$", "", s) for s in slots] + words
    if not words:
        words = [re.sub(r"[^a-z0-9]+", "-", w.lower()).strip("-") for w in tokens]
        words = [w for w in words if w]
    name = "-".join(words)[:NAME_MAX].strip("-")
    name = re.sub(r"-{2,}", "-", name)
    if not name or not name[0].isalnum():
        name = "new-tree"
    return name


def _quote(s: str) -> str:
    """Shell-quote ``s``, preferring double quotes so an apostrophe in a
    sentence does not turn into ``'what'"'"'s'``."""
    if re.fullmatch(r"[\w./:@%+=,-]+", s):
        return s
    if not any(c in s for c in '"$`\\'):
        return f'"{s}"'
    return shlex.quote(s)


def _toml_safe(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def no_match_outcome(text: str, where: str) -> dict[str, Any]:
    """The ``say`` and ``output`` an unanswered sentence gets back: what
    happened, and the one command that would change it."""
    p = propose(text)
    say = f"{where} answers {text!r}. To write the tree that would: {p.command()}"
    return {"say": say, "output": {"unanswered": text, "proposal": p.to_dict()}}


class Unanswered:
    """Sentences nobody answered, counted, with a proposal for each.

    Bounded: beyond ``limit`` distinct sentences the least recently seen is
    dropped. Lives in memory, like gaps; a restart forgets it.
    """

    def __init__(self, limit: int = UNANSWERED_LIMIT, path: "str | Path | None" = None) -> None:
        self.limit = limit
        self.path = Path(path) if path else None
        self.error: str | None = None
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        if self.path is not None and self.path.exists():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("text"), str):
                        self._take(rec["text"], rec.get("peer"), int(rec.get("count", 1)), float(rec.get("last", time.time())), persist=False)
            except OSError as e:
                self.error = str(e)

    def record(self, text: str, peer: str | None = None) -> dict[str, Any]:
        return self._take(text, peer, 1, time.time(), persist=True)

    def _take(self, text: str, peer: str | None, count: int, when: float, *, persist: bool) -> dict[str, Any]:
        key = normalise(text).lower() or text
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = {"text": normalise(text) or text, "count": 0, "first": when, "last": when, "proposal": propose(text).to_dict()}
                if peer:
                    entry["peer"] = peer
                self._entries[key] = entry
            entry["count"] += count
            entry["last"] = max(entry["last"], when)
            while len(self._entries) > self.limit:
                oldest = min(self._entries, key=lambda k: self._entries[k]["last"])
                del self._entries[oldest]
            if persist and self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"text": entry["text"], "peer": peer, "count": 1, "last": when}, ensure_ascii=False) + "\n")
                    self.error = None
                except OSError as e:
                    self.error = str(e)
        return dict(entry)

    def to_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(e) for e in self._entries.values()), key=lambda e: (-e["count"], -e["last"]))

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------- growing a tree from a gap


@dataclass
class Draft:
    """A branch a gap asks for, drafted by rules: the condition that would
    have taken the observed value, a new terminal node for it, and the
    text of both. A person reads it, changes the node's kind and words to
    what the value means, and keeps it. Nothing here decides anything."""

    tree: str
    node: str
    on: str
    observed: Any
    when: dict[str, Any]
    then: str
    branch: str
    node_text: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tree": self.tree, "node": self.node, "on": self.on, "observed": self.observed, "when": self.when, "then": self.then, "branch": self.branch, "node_text": self.node_text, "reason": self.reason}

    def render(self) -> str:
        return f"# {self.tree}: in [nodes.{self.node}], add to branches (before the else, if any):\n  {self.branch}\n\n{self.node_text}"


def draft_branch(tree: Tree, gap: dict[str, Any]) -> Draft | None:
    """The branch a gap on ``tree`` asks for, or None when the gap names
    no decide node of this tree or a branch already covers the value
    (the file is newer than the gap).

    Rules: a missing value asks for ``{ exists = false }``; a number that
    falls in a hole between the ``between`` ranges or ``eq`` points the
    node already has asks for the range of that hole; anything else asks
    for ``{ eq = value }`` (``{ type = ... }`` for a value a report could
    only carry as its type). The new node is a ``fail`` that says what
    was seen; the author decides what it means."""
    node = tree.nodes.get(str(gap.get("node", "")))
    if node is None or node.kind != "decide":
        return None
    on = str(gap.get("on") or node.get("on") or "")
    observed = gap.get("observed")
    branches = [b for b in (node.get("branches") or []) if isinstance(b, dict)]
    if any("when" in b and conditions.holds(b["when"], observed) for b in branches):
        return None
    when, reason = _condition_for(observed, branches)
    then = _unique_node(tree, f"{node.id}-{_slug(observed)}")
    slug = _slug(observed)
    branch = "{ when = " + _toml_inline(when) + f', then = "{then}" }},'
    node_text = (
        f"# Drafted by pyrtan grow from a gap: {on} = {json.dumps(observed, ensure_ascii=False)} had no branch.\n"
        f"# Decide what that value means: keep this as a fail, or make it a done, a set, an observe...\n"
        f"[nodes.{then}]\n"
        f'kind = "fail"\n'
        f'reason = "{_slug(observed, 40) or "unhandled"}"\n'
        f'say = "{_toml_str(on)} was {_toml_str(json.dumps(observed, ensure_ascii=False))}; nobody has said what that means yet."\n'
    )
    return Draft(tree.name, node.id, on, observed, when, then, branch, node_text, reason)


def _condition_for(observed: Any, branches: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    if observed is None:
        return {"exists": False}, "the value was missing"
    if isinstance(observed, str) and observed.startswith("<") and observed.endswith(">") and observed[1:-1] in ("str", "dict", "list", "int", "float", "bool"):
        return {"type": observed[1:-1]}, "a report carries a long or structured value as its type"
    if isinstance(observed, (int, float)) and not isinstance(observed, bool):
        covered: list[tuple[float, float]] = []
        for b in branches:
            w = b.get("when") if isinstance(b, dict) else None
            if not isinstance(w, dict):
                continue
            if "between" in w and isinstance(w["between"], list) and len(w["between"]) == 2:
                try:
                    covered.append((float(w["between"][0]), float(w["between"][1])))
                except (TypeError, ValueError):
                    pass
            elif "eq" in w and isinstance(w["eq"], (int, float)) and not isinstance(w["eq"], bool):
                covered.append((float(w["eq"]), float(w["eq"])))
        if covered and isinstance(observed, int):
            below = [hi for lo, hi in covered if hi < observed]
            above = [lo for lo, hi in covered if lo > observed]
            if below and above and all(float(x).is_integer() for x in below + above):
                lo, hi = int(max(below)) + 1, int(min(above)) - 1
                if lo <= hi:
                    return {"between": [lo, hi]}, f"the value falls in the hole between {int(max(below))} and {int(min(above))}, which the ranges around it leave open"
    return {"eq": observed}, "the value itself"


def _unique_node(tree: Tree, base: str) -> str:
    name = base
    n = 2
    while name in tree.nodes:
        name = f"{base}-{n}"
        n += 1
    return name


def _slug(value: Any, limit: int = 24) -> str:
    text = "missing" if value is None else str(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:limit].rstrip("-") or "value")


def _toml_inline(cond: dict[str, Any]) -> str:
    return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in cond.items()) + " }"


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + _toml_str(str(v)) + '"'


def _toml_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def apply_draft(path: str | Path, draft: Draft) -> str:
    """Write a draft into the tree file: the branch into the node's
    ``branches`` list (before its ``else``), the node at the end. The file
    must load clean afterwards, else it is put back and :class:`TreeError`
    raised. Returns the new text."""
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    header = f"[nodes.{draft.node}]"
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is None:
        raise TreeError(f"{path}: no {header} table")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    b = next((i for i in range(start, end) if re.match(r"^\s*branches\s*=\s*\[", lines[i])), None)
    if b is None:
        raise TreeError(f"{path}: {header} has no branches list")
    indent = "  "
    if "]" in lines[b].split("[", 1)[1]:
        # A one-line list.
        head, tail = lines[b].rsplit("]", 1)
        m = re.search(r",?\s*\{\s*else\s*=", head)
        if m:
            head = head[: m.start()] + ", " + draft.branch.rstrip(",") + head[m.start():]
        else:
            head = head.rstrip().rstrip(",") + ", " + draft.branch.rstrip(",")
        lines[b] = head + "]" + tail
    else:
        close = next(i for i in range(b + 1, end) if lines[i].strip().startswith("]"))
        else_line = next((i for i in range(b + 1, close) if re.match(r"^\s*\{\s*else\s*=", lines[i])), None)
        at = else_line if else_line is not None else close
        if at > b + 1:
            indent = re.match(r"^\s*", lines[at - 1 if else_line is None else at]).group(0) or indent
        lines.insert(at, f"{indent}{draft.branch}\n")
    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    text += "\n" + draft.node_text
    path.write_text(text, encoding="utf-8")
    try:
        tree = load_tree(path)
        errors = [p for p in check(tree) if p.level == "error"]
        if errors:
            raise TreeError("; ".join(f"{p.where}: {p.message}" for p in errors))
    except TreeError as e:
        path.write_text(original, encoding="utf-8")
        raise TreeError(f"{path}: the draft did not fit the file ({e}); nothing changed") from e
    return text
