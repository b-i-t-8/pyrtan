"""What agents beside the swarm suggest, and how a suggestion becomes a tree.

An agent is a model a person starts in the console (``agents.js``). It
probes the trees with sentences of its own, reads the gaps and the
unanswered sentences, and says what it thinks: a *suggestion*. It never
sits inside a run. What it sends here is data, and nothing in this module
trusts it: every suggestion is checked for shape when it arrives, and
again, by rules and by :func:`pyrtan.tree.check`, when ``pyrtan grow
--suggestions`` writes it into a file.

Three kinds, and only these, because each adds no reach a tree did not
already have:

- ``branch``: what a value a ``decide`` met means. The rules of
  :func:`pyrtan.grow.draft_branch` still choose the condition and the node
  id; the agent supplies the node's meaning (a ``done`` or a ``fail``, its
  ``say`` and ``reason``) in place of "nobody has said what that means".
- ``trigger``: a pattern a tree should answer to, with the sentences it
  covers. It is kept only if one of them then reads to that tree.
- ``tree``: a sentence no tree answers, with a description of the tree
  that would. The tree is still the scaffold :func:`pyrtan.grow.propose`
  draws by rules; the agent's ``hosts_hint`` is a note, never a host.

No suggestion adds an ``observe``, an ``act``, a ``delegate``, a host, a
path, a command or a device; none may read ``env`` in what it says. A
service or hub keeps what agents send at ``/suggestions`` (``--suggestions
FILE`` keeps it across restarts), counted like gaps, bounded like
unanswered sentences.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .grow import Draft, _toml_str, draft_branch, propose
from .intent import Trigger, TriggerError
from .registry import Registry
from .template import placeholders
from .tree import Tree, TreeError, check, load_tree, load_tree_text

KINDS = ("branch", "trigger", "tree")
MEANINGS = ("done", "fail")
LIMIT = 200          # distinct suggestions a store keeps
PER_POST = 50        # suggestions one request may carry
SAY_MAX = 300
WHY_MAX = 500
PATTERN_MAX = 200
COVERS_MAX = 10
SAY_ROOTS = ("inputs", "state", "tree")   # never env: a model's words must not carry a secret out
REASON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def key(s: dict[str, Any]) -> str:
    """What makes two suggestions the same one."""
    kind = s.get("kind")
    if kind == "branch":
        parts = [kind, s.get("tree"), s.get("node"), s.get("on"), s.get("observed")]
    elif kind == "trigger":
        parts = [kind, s.get("tree"), s.get("pattern")]
    else:
        parts = [kind, str(s.get("text", "")).strip().lower()]
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)


def problem(s: Any) -> str | None:
    """Why ``s`` is not a suggestion this module takes, or None."""
    if not isinstance(s, dict):
        return "a suggestion is a table"
    kind = s.get("kind")
    if kind not in KINDS:
        return f"kind is one of {', '.join(KINDS)}"
    if not isinstance(s.get("why", ""), str) or len(s.get("why", "")) > WHY_MAX:
        return f"why is text of at most {WHY_MAX} characters"
    by = s.get("by", {})
    if not isinstance(by, dict) or not all(isinstance(v, str) and len(v) <= 120 for v in by.values()):
        return "by is a table of short strings: agent, model, engine"
    if kind in ("branch", "trigger") and not (isinstance(s.get("tree"), str) and s["tree"]):
        return "a branch or a trigger names its tree"
    if kind == "branch":
        if not isinstance(s.get("node"), str) or not s["node"]:
            return "a branch names the decide node it belongs to"
        if "observed" not in s:
            return "a branch says what value it is for (observed)"
        m = s.get("meaning")
        if not isinstance(m, dict) or m.get("kind") not in MEANINGS:
            return "a branch's meaning is a done or a fail"
        say = m.get("say")
        if not isinstance(say, str) or not say.strip() or len(say) > SAY_MAX:
            return f"a meaning says something, in at most {SAY_MAX} characters"
        for path in placeholders(say):
            head = re.split(r"[.\[|\s]", path.strip(), maxsplit=1)[0]
            if head not in SAY_ROOTS:
                return f"say may read {', '.join(SAY_ROOTS)}, not {head}"
        if "reason" in m and not (isinstance(m["reason"], str) and REASON_RE.match(m["reason"])):
            return "reason is a short lowercase slug, like no-such-place"
    if kind == "trigger":
        pattern = s.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > PATTERN_MAX:
            return f"a trigger is a pattern of at most {PATTERN_MAX} characters"
        try:
            Trigger(pattern)
        except TriggerError as e:
            return f"pattern: {e}"
        covers = s.get("covers", [])
        if not isinstance(covers, list) or not covers or len(covers) > COVERS_MAX or not all(isinstance(c, str) and 0 < len(c) <= 200 for c in covers):
            return f"a trigger names the sentences it covers (1 to {COVERS_MAX})"
    if kind == "tree":
        text = s.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            return "a tree suggestion carries the sentence it would answer"
        if not isinstance(s.get("description", ""), str) or len(s.get("description", "")) > 200:
            return "description is one line"
        hints = s.get("hosts_hint", [])
        if not isinstance(hints, list) or not all(isinstance(h, str) and len(h) <= 100 for h in hints):
            return "hosts_hint is a list of host names (a note, never a host)"
    return None


def clean(s: dict[str, Any]) -> dict[str, Any]:
    """The fields a suggestion keeps; anything else an agent sent is dropped."""
    keep = ["kind", "tree", "node", "on", "observed", "meaning", "pattern", "covers", "text", "description", "hosts_hint", "why", "by", "evidence"]
    out = {k: s[k] for k in keep if k in s}
    if isinstance(out.get("meaning"), dict):
        out["meaning"] = {k: out["meaning"][k] for k in ("kind", "say", "reason") if k in out["meaning"]}
    ev = out.get("evidence")
    if ev is not None:
        out["evidence"] = [e for e in ev if isinstance(e, dict)][:COVERS_MAX] if isinstance(ev, list) else []
    return out


class Suggestions:
    """What agents suggested, counted: the same suggestion twice is one
    entry seen twice. Bounded; beyond ``limit`` the least recently seen
    goes. With a path, every suggestion is appended to a JSON-lines file
    and read back on start."""

    def __init__(self, limit: int = LIMIT, path: str | Path | None = None) -> None:
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
                    if isinstance(rec, dict) and problem(rec) is None:
                        self._take(rec, float(rec.get("last", time.time())), persist=False)
            except OSError as e:
                self.error = str(e)

    def add(self, items: Iterable[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Take what an agent sent: ``(kept, refused)``, each refusal with
        its index and why."""
        kept, refused = [], []
        now = time.time()  # one batch, one moment: within it, the order it was sent in
        for i, s in enumerate(items):
            why = problem(s)
            if why:
                refused.append({"index": i, "why": why})
                continue
            kept.append(self._take(clean(s), now, persist=True))
        return kept, refused

    def _take(self, s: dict[str, Any], when: float, *, persist: bool) -> dict[str, Any]:
        k = key(s)
        with self._lock:
            entry = self._entries.get(k)
            if entry is None:
                entry = {**clean(s), "count": 0, "first": when, "last": when}
                self._entries[k] = entry
            else:
                entry.update(clean(s))  # the newest wording of the same suggestion
            entry["count"] += int(s.get("count", 1)) if not persist else 1
            entry["last"] = max(entry["last"], when)
            while len(self._entries) > self.limit:
                oldest = min(self._entries, key=lambda x: self._entries[x]["last"])
                del self._entries[oldest]
            if persist and self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({**clean(s), "count": 1, "last": when}, ensure_ascii=False, default=str) + "\n")
                    self.error = None
                except OSError as e:
                    self.error = str(e)
            return dict(entry)

    def to_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(e) for e in self._entries.values()), key=lambda e: (-e["count"], -e["last"]))

    def __len__(self) -> int:
        return len(self._entries)

    def route(self, method: str, body: dict[str, Any]) -> tuple[int, Any]:
        """``GET /suggestions`` and ``POST /suggestions``, for a service and a hub alike."""
        if method == "GET":
            return 200, {"suggestions": self.to_list()}
        items = body.get("suggestions") if isinstance(body.get("suggestions"), list) else [body] if body.get("kind") else None
        if items is None:
            return 400, {"status": "error", "reason": "bad-request", "say": 'Send {"suggestions": [{"kind": "branch"|"trigger"|"tree", ...}]}.'}
        if len(items) > PER_POST:
            return 400, {"status": "error", "reason": "too-many", "say": f"At most {PER_POST} suggestions at a time."}
        kept, refused = self.add(items)
        return 200, {"status": "ok", "kept": len(kept), "refused": refused}


# ---------------------------------------------------------------- writing one into a file


def meaning_text(draft: Draft, s: dict[str, Any]) -> str:
    """The node a branch suggestion asks for: the rules' id, the agent's words."""
    m = s["meaning"]
    by = s.get("by") or {}
    who = " ".join(x for x in (by.get("agent"), f"({by['model']})" if by.get("model") else None) if x) or "an agent"
    reason = m.get("reason") or re.sub(r"[^a-z0-9]+", "-", draft.then.lower()).strip("-")[:40] or "unhandled"
    lines = [f"# Suggested by {who} for a gap: {draft.on} = {json.dumps(draft.observed, ensure_ascii=False)} had no branch."]
    for why in _comment_lines(s.get("why", "")):
        lines.append(f"# {why}")
    lines.append(f"[nodes.{draft.then}]")
    lines.append(f'kind = "{m["kind"]}"')
    if m["kind"] == "fail":
        lines.append(f'reason = "{_toml_str(reason)}"')
    lines.append(f'say = "{_toml_str(m["say"].strip())}"')
    return "\n".join(lines) + "\n"


def _comment_lines(text: str, width: int = 76) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:6]


def draft(tree: Tree, s: dict[str, Any]) -> Draft | None:
    """The branch a ``branch`` suggestion asks for, drafted by the rules,
    with the agent's meaning in place of the rules' placeholder; None when
    a branch already covers the value."""
    d = draft_branch(tree, {"node": s.get("node"), "on": s.get("on"), "observed": s.get("observed")})
    if d is None:
        return None
    d.node_text = meaning_text(d, s)
    d.reason = f"{d.reason}; meaning suggested by {(s.get('by') or {}).get('agent', 'an agent')}"
    return d


def trigger_text(s: dict[str, Any]) -> str:
    by = s.get("by") or {}
    head = f"# Suggested by {by.get('agent', 'an agent')}" + (f" ({by['model']})" if by.get("model") else "") + ": covers " + "; ".join(json.dumps(c, ensure_ascii=False) for c in s.get("covers", [])[:3])
    return f"{head}\n[[triggers]]\npattern = \"{_toml_str(s['pattern'])}\"\n"


def apply_trigger(path: str | Path, tree: Tree, s: dict[str, Any], others: Registry | None = None) -> str:
    """Write a ``trigger`` suggestion into the tree file, after its last
    ``[[triggers]]``. Kept only when the file loads clean, every slot in
    the pattern is one of the tree's inputs, and at least one sentence it
    covers then reads to this tree (among ``others``, when given) without
    a tie; otherwise the file is put back and :class:`TreeError` raised."""
    path = Path(path)
    try:
        trig = Trigger(s["pattern"], language=tree.language)
    except TriggerError as e:
        raise TreeError(f"{tree.name}: the pattern does not compile ({e})") from e
    unknown = sorted(set(trig.slots) - set(tree.inputs))
    if unknown:
        raise TreeError(f"{tree.name}: the pattern names {', '.join(unknown)}, which the tree does not take as inputs")
    if s["pattern"] in [t.pattern for t in tree.triggers]:
        raise TreeError(f"{tree.name}: already answers to {s['pattern']!r}")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    last = max((i for i, l in enumerate(lines) if l.strip() == "[[triggers]]"), default=None)
    block = trigger_text(s)
    if last is None:
        text = original + ("" if original.endswith("\n") else "\n") + "\n" + block
    else:
        end = next((i for i in range(last + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        while end > last + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1  # the comments and blank lines before the next table belong to it
        text = "".join(lines[:end]) + block + "".join(lines[end:])
    path.write_text(text, encoding="utf-8")
    try:
        grown = load_tree(path)
        errors = [p for p in check(grown) if p.level == "error"]
        if errors:
            raise TreeError("; ".join(f"{p.where}: {p.message}" for p in errors))
        before, after = Registry(), Registry()
        for t in (others.trees() if others is not None else []):
            if t.name != tree.name:
                before.add(t)
                after.add(t)
        before.add(tree)
        after.add(grown)
        # It must read a sentence the tree did not read before. A tie has no winner, so a cover that ties does not count.
        reads = lambda reg, c: (w := reg.read(c).winner) is not None and w.tree == tree.name  # noqa: E731
        if not any(reads(after, c) and not reads(before, c) for c in s.get("covers", [])):
            raise TreeError("none of the sentences it covers reads to this tree with it, that did not before")
    except TreeError as e:
        path.write_text(original, encoding="utf-8")
        raise TreeError(f"{path}: the trigger did not fit ({e}); nothing changed") from e
    return text


def tree_text(s: dict[str, Any]) -> tuple[str, str]:
    """A ``tree`` suggestion as ``(name, file text)``: the rules' scaffold,
    with the agent's description and a note of the hosts it guessed."""
    p = propose(s["text"])
    text = p.scaffold()
    desc = str(s.get("description", "")).strip()
    if desc:
        text = text.replace('description = ""', f'description = "{_toml_str(desc)}"', 1)
    hints = [h for h in s.get("hosts_hint", []) if h]
    by = s.get("by") or {}
    note = f"# Described by {by.get('agent', 'an agent')}" + (f" ({by['model']})" if by.get("model") else "") + "."
    if hints:
        note += f" It guessed these hosts might answer: {', '.join(hints)}. Check before you list any."
    for why in _comment_lines(s.get("why", "")):
        note += f"\n# {why}"
    return p.name, text.replace("\n\nname = ", f"\n{note}\n\nname = ", 1)


def write_tree(directory: str | Path, s: dict[str, Any]) -> Path:
    """Write a ``tree`` suggestion as a new file in ``directory``; never over one."""
    name, text = tree_text(s)
    path = Path(directory) / f"{name}.toml"
    if path.exists():
        raise TreeError(f"{path} exists already; nothing changed")
    tree = load_tree_text(text, str(path))
    errors = [p for p in check(tree) if p.level == "error"]
    if errors:
        raise TreeError(f"{name}: the scaffold does not check ({'; '.join(p.message for p in errors)})")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
