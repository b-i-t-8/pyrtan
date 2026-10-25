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

import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .intent import Trigger, TriggerError, normalise

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
    slots, host names ``{host:word}``, ``owner/repo``-shaped words
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
        return f"{{{slot}:word}}"
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

    def __init__(self, limit: int = UNANSWERED_LIMIT) -> None:
        self.limit = limit
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, text: str, peer: str | None = None) -> dict[str, Any]:
        key = normalise(text).lower() or text
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = {"text": normalise(text) or text, "count": 0, "first": now, "last": now, "proposal": propose(text).to_dict()}
                if peer:
                    entry["peer"] = peer
                self._entries[key] = entry
            entry["count"] += 1
            entry["last"] = now
            while len(self._entries) > self.limit:
                oldest = min(self._entries, key=lambda k: self._entries[k]["last"])
                del self._entries[oldest]
        return dict(entry)

    def to_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(e) for e in self._entries.values()), key=lambda e: (-e["count"], -e["last"]))

    def __len__(self) -> int:
        return len(self._entries)
