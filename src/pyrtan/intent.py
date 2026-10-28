"""Triggers: how a sentence finds the tree that answers it.

A trigger is a pattern with slots::

    "weather in {city}"
    "top {count:int} stories on hacker news"
    "is {host:host} up"
    "what's [the] latest release of {repo}"
    "remind me {when:date} at {at:time}"

A slot is ``{name}`` (any text) or ``{name:kind}``. The kinds: ``int``,
``float``, ``word`` (no spaces), ``text``; and the read kinds, each with a
fixed parser in :mod:`pyrtan.nlp`: ``number`` (digits or words),
``date``, ``time``, ``duration``, ``host``, ``url``, ``email``. Square
brackets mark optional words, round brackets a required choice, and ``|``
separates alternatives inside either: ``"how (hot|cold) is it in {place}"``.

Before matching, the sentence and the pattern both go through
:func:`pyrtan.nlp.clean`: unicode folded, greetings and politeness dropped
from the edges, contractions expanded, number words turned to digits.
Matching is case-insensitive and a literal word matches its plural or
singular. A tree may add ``synonyms`` and turn on ``typos`` in its
``[language]`` table; both are applied here, and every rule that fires is
reported in the :class:`Match` so the trace can show it.

When several triggers match, the most specific wins: the one with the most
literal text. A tie is reported as ambiguity rather than resolved by luck.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

from . import nlp

KINDS = ("int", "float", "word", "text", "number", "date", "time", "duration", "host", "url", "email")
SLOT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([a-z]+))?\}")
SLOT_RE = {
    "int": r"-?\d+",
    "float": r"-?\d+(?:\.\d+)?",
    "number": r"-?\d+(?:[.,]\d+)*",
    "word": r"\S+",
    "text": r".+?",
    "date": r".+?",
    "time": r"\S+(?:\s*[ap]m)?",
    "duration": r".+?",
    "host": r"\S+",
    "url": r"\S+",
    "email": r"\S+",
    None: r".+?",
}
# The input type each slot kind produces, for the checker.
PRODUCES = {"int": "int", "float": "float", "number": "number", "word": "str", "text": "str", "date": "str", "time": "str", "duration": "int", "host": "str", "url": "str", "email": "str", None: "str"}
TYPO_MIN_LENGTH = 5


class TriggerError(ValueError):
    pass


def normalise(text: str) -> str:
    """The light form: trimmed, one space between words. Used where a
    sentence is kept as a person wrote it (the unanswered log)."""
    text = text.strip()
    text = re.sub(r"^[\s\W_]+|[\s?!.,;:]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


@dataclass
class Language:
    """A tree's ``[language]`` table: what it adds to the fixed rules."""

    synonyms: dict[str, list[str]] = field(default_factory=dict)
    typos: bool = False

    @classmethod
    def from_raw(cls, raw: Any, where: str = "language") -> "Language":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TriggerError(f"{where} must be a table")
        syn = raw.get("synonyms", {})
        if not isinstance(syn, dict):
            raise TriggerError(f"{where}.synonyms must be a table of word = [words]")
        synonyms: dict[str, list[str]] = {}
        for word, alts in syn.items():
            if isinstance(alts, str):
                alts = [alts]
            if not isinstance(alts, list) or not all(isinstance(a, str) and a.strip() for a in alts):
                raise TriggerError(f"{where}.synonyms.{word} must be a list of words")
            if not re.fullmatch(r"[A-Za-z][A-Za-z'-]*", word):
                raise TriggerError(f"{where}.synonyms: {word!r} is not a word")
            synonyms[word.lower()] = [a.strip().lower() for a in alts]
        typos = raw.get("typos", False)
        if not isinstance(typos, bool):
            raise TriggerError(f"{where}.typos must be true or false")
        for key in raw:
            if key not in ("synonyms", "typos"):
                raise TriggerError(f"{where}: unknown key {key!r}; one of synonyms, typos")
        return cls(synonyms, typos)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.synonyms:
            out["synonyms"] = self.synonyms
        if self.typos:
            out["typos"] = True
        return out


@dataclass
class Trigger:
    pattern: str
    fixed_inputs: dict[str, Any] = field(default_factory=dict)
    language: Language = field(default_factory=Language)
    regex: re.Pattern[str] = field(init=False, repr=False)
    slots: dict[str, str | None] = field(init=False, default_factory=dict)
    specificity: int = field(init=False, default=0)
    cleaned: str = field(init=False, default="")
    vocabulary: set[str] = field(init=False, default_factory=set, repr=False)
    edges: tuple[str, str] = field(init=False, default=("", ""), repr=False)  # first and last literal word

    def __post_init__(self) -> None:
        if isinstance(self.language, dict):
            self.language = Language.from_raw(self.language)
        # The pattern gets the rewrites a sentence gets, minus the ones that
        # only make sense for a sentence (fillers, number words): folded,
        # contractions expanded, so "[what's the]" and "what is the" agree.
        self.cleaned = "".join(part if part.startswith("{") else nlp.expand_contractions(part)[0] for part in re.split(r"(\{[^}]*\})", nlp.fold(self.pattern)))
        self.regex, self.slots, self.specificity, self.vocabulary, self.edges = compile_pattern(self.cleaned, self.language)

    def match(self, text: str, *, now: _dt.datetime | _dt.date | None = None, cleaned: bool = False) -> dict[str, Any] | None:
        """The inputs this trigger reads from ``text``, or None. ``cleaned``
        says the text already went through :func:`nlp.clean`."""
        got = self.match_detail(text, now=now, cleaned=cleaned)
        return got.inputs if got is not None else None

    def match_detail(self, text: str, *, now: _dt.datetime | _dt.date | None = None, cleaned: bool = False) -> "Match | None":
        sentence = text if cleaned else nlp.clean(text)[0]
        notes: list[str] = []
        if self.language.typos:
            sentence, fixed = correct_typos(sentence, self.vocabulary)
            notes.extend(f"typo: {a}->{b}" for a, b in fixed)
        m = self.regex.fullmatch(sentence)
        if not m:
            return None
        inputs: dict[str, Any] = dict(self.fixed_inputs)
        for name, kind in self.slots.items():
            raw = m.group(name)
            if raw is None:
                continue
            try:
                inputs[name] = read_slot(kind, raw.strip(), now)
            except (ValueError, nlp.ParseError) as e:
                notes.append(f"slot {name}: {e}")
                return Match("", self, {}, self.specificity, notes, rejected=True)
            if kind in nlp.PARSERS and str(inputs[name]) != raw.strip():
                notes.append(f"{name}: {raw.strip()!r} read as {inputs[name]!r}")
        return Match("", self, inputs, self.specificity, notes)


def read_slot(kind: str | None, raw: str, now: _dt.datetime | _dt.date | None) -> Any:
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind in (None, "word", "text"):
        return raw
    parser = nlp.PARSERS[kind]
    return parser(raw, now) if kind == "date" else parser(raw)


def correct_typos(sentence: str, vocabulary: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """Replace a word that is one edit from exactly one vocabulary word
    (and is not itself one), for words of :data:`TYPO_MIN_LENGTH` letters
    or more. Returns the sentence and what changed."""
    if not vocabulary:
        return sentence, []
    fixed: list[tuple[str, str]] = []

    def one(m: re.Match[str]) -> str:
        word = m.group(0)
        low = word.lower()
        if len(low) < TYPO_MIN_LENGTH or low in vocabulary or any(low in nlp.forms(v) for v in vocabulary):
            return word
        close = [v for v in vocabulary if len(v) >= TYPO_MIN_LENGTH and nlp.distance(low, v, 1) <= 1]
        if len(close) != 1:
            return word
        fixed.append((word, close[0]))
        return close[0]

    return re.sub(r"[A-Za-z]+", one, sentence), fixed


def _word_regex(word: str, language: Language) -> str:
    """A literal word matches itself, its singular or plural, and the same
    for each of its synonyms."""
    spellings = set(nlp.forms(word))
    phrases: list[str] = []
    for syn in language.synonyms.get(word.lower(), []):
        if " " in syn:
            phrases.append(r"\s+".join(re.escape(p) for p in syn.split()))
        else:
            spellings.update(nlp.forms(syn))
    alternatives = [re.escape(s) for s in sorted(spellings, key=lambda s: (-len(s), s))] + phrases
    return "(?:" + "|".join(alternatives) + ")"


# A slot kind whose regex is lazy reads as little as it can: at the end of
# a pattern that is searched for (not matched whole), it reads to the end
# of its line instead of one character.
LAZY = {None, "text", "date", "duration"}
EDGE = r"(?![^\W_])"  # a word in a searched pattern starts and ends at a word's edge
EDGE_BEFORE = r"(?<![^\W_])"


def compile_pattern(pattern: str, language: Language | None = None, *, search: bool = False) -> tuple[re.Pattern[str], dict[str, str | None], int, set[str], tuple[str, str]]:
    """``search`` compiles the pattern to be found in a text (extraction's
    ``pattern``) rather than to match a whole sentence: a literal word must
    start and end at a word's edge (where it does not touch a slot), and a
    lazy slot that ends the pattern reads to the end of its line."""
    language = language or Language()
    what = "pattern" if search else "trigger"
    if not pattern or not pattern.strip():
        raise TriggerError(f"a {what} pattern cannot be empty")
    slots: dict[str, str | None] = {}
    out: list[str] = []
    vocabulary: set[str] = set()
    ordered: list[str] = []
    literal_chars = 0
    pos = 0
    depth = 0
    text = pattern.strip().lower()
    word: list[str] = []

    def flush_word() -> None:
        if word:
            w = "".join(word)
            if search:
                start, end = pos - len(w), pos
                before = EDGE_BEFORE if start == 0 or text[start - 1] != "}" else ""
                after = EDGE if end >= len(text) or text[end] != "{" else ""
                out.append(before + _word_regex(w, language) + after)
            else:
                out.append(_word_regex(w, language))
            vocabulary.add(w)
            ordered.append(w)
            for syn in language.synonyms.get(w, []):
                vocabulary.update(syn.split())
            word.clear()

    while pos < len(text):
        ch = text[pos]
        if ch.isalpha() or (word and ch in "'-" and pos + 1 < len(text) and text[pos + 1].isalpha()):
            word.append(ch)
            literal_chars += 1
            pos += 1
            continue
        flush_word()
        if ch == "{":
            m = SLOT.match(text, pos)
            if not m:
                raise TriggerError(f"bad slot in {what} {pattern!r} at {pos}")
            name, kind = m.group(1), m.group(2)
            if kind is not None and kind not in KINDS:
                raise TriggerError(f"slot {{{name}:{kind}}} in {pattern!r}: kind must be one of {', '.join(KINDS)}")
            if name in slots:
                raise TriggerError(f"slot {name!r} appears twice in {pattern!r}")
            slots[name] = kind
            out.append(f"(?P<{name}>{SLOT_RE[kind]})")
            pos = m.end()
        elif ch == "[":
            depth += 1
            out.append(r"(?:\s*")
            pos += 1
        elif ch == "]":
            if depth == 0:
                raise TriggerError(f"unbalanced ] in {what} {pattern!r}")
            depth -= 1
            out.append(r")?")
            pos += 1
        elif ch == "(":
            depth += 1
            out.append(r"(?:")
            pos += 1
        elif ch == ")":
            if depth == 0:
                raise TriggerError(f"unbalanced ) in {what} {pattern!r}")
            depth -= 1
            out.append(r")")
            pos += 1
        elif ch == "|" and depth > 0:
            out.append("|")
            pos += 1
        elif ch.isspace():
            while pos < len(text) and text[pos].isspace():
                pos += 1
            # Space is optional around optional groups and slots so that
            # "[the] weather" matches "weather" without a leading space.
            if out and out[-1] in (r"(?:\s*", r")?", r"(?:", r")", "|"):
                out.append(r"\s*")
            elif pos < len(text) and text[pos] in "[(|":
                out.append(r"\s*")
            else:
                out.append(r"\s+")
        else:
            out.append(re.escape(ch))
            literal_chars += 1
            pos += 1
    flush_word()
    if depth != 0:
        raise TriggerError(f"unbalanced [ in {what} {pattern!r}")
    if literal_chars == 0:
        raise TriggerError(f"{what} {pattern!r} has no literal words; it would match anything")
    if search and slots and out[-1].startswith("(?P<") and list(slots.values())[-1] in LAZY:
        out.append(r"(?=[^\S\n]*$)")
    try:
        regex = re.compile("".join(out), re.IGNORECASE | (re.MULTILINE if search else 0))
    except re.error as e:
        raise TriggerError(f"{what} {pattern!r} does not compile: {e}") from e
    return regex, slots, literal_chars, vocabulary, (ordered[0], ordered[-1]) if ordered else ("", "")


@dataclass
class Match:
    tree: str
    trigger: Trigger
    inputs: dict[str, Any]
    specificity: int
    notes: list[str] = field(default_factory=list)
    rejected: bool = False


@dataclass
class Reading:
    """How a sentence was read: the rewrites, and every trigger's verdict."""

    text: str
    cleaned: str
    steps: list[str]
    matches: list[Match]          # the winners: one, or several at a tie
    considered: list[Match]       # every trigger that matched, or matched and then refused a slot
    now: str | None = None

    @property
    def winner(self) -> Match | None:
        return self.matches[0] if len(self.matches) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "cleaned": self.cleaned,
            "steps": self.steps,
            "now": self.now,
            "matches": [_match_dict(m) for m in self.matches],
            "considered": [_match_dict(m) for m in self.considered],
        }

    def step(self) -> dict[str, Any]:
        """The ``intent`` step a trace starts with: what the sentence became
        and which trigger took it."""
        rewrite = f"{self.text!r}" + (f" -> {self.cleaned!r}" if self.cleaned != self.text else "")
        if self.winner:
            m = self.winner
            summary = f"{rewrite}; {m.tree} via {m.trigger.pattern!r} ({m.specificity} literal chars)"
            if m.notes:
                summary += "; " + "; ".join(m.notes)
        elif self.matches:
            summary = f"{rewrite}; tie between {sorted({m.tree for m in self.matches})}"
        else:
            summary = f"{rewrite}; no trigger matched"
        observed = {"cleaned": self.cleaned, "steps": self.steps}
        if self.winner:
            observed["trigger"] = self.winner.trigger.pattern
            observed["inputs"] = self.winner.inputs
            if self.winner.notes:
                observed["notes"] = self.winner.notes
        return {"node": "intent", "kind": "intent", "summary": summary, "observed": observed}


def _match_dict(m: Match) -> dict[str, Any]:
    return {"tree": m.tree, "trigger": m.trigger.pattern, "specificity": m.specificity, "inputs": m.inputs, "notes": m.notes, "rejected": m.rejected}


def read(text: str, candidates: list[tuple[Any, Trigger]], now: _dt.datetime | _dt.date | None = None) -> Reading:
    """Read ``text`` against every candidate and say what happened."""
    keep = frozenset(w for _, trig in candidates for w in trig.edges if w)
    cleaned, steps = nlp.clean(text, keep=keep)
    considered: list[Match] = []
    for tree, trig in candidates:
        got = trig.match_detail(cleaned, now=now, cleaned=True)
        if got is not None:
            got.tree = tree
            considered.append(got)
    live = [m for m in considered if not m.rejected]
    top = max((m.specificity for m in live), default=0)
    matches = [m for m in live if m.specificity == top]
    now_text = now.isoformat() if isinstance(now, (_dt.date, _dt.datetime)) else None
    return Reading(text, cleaned, steps, matches, considered, now_text)


def best_matches(text: str, candidates: list[tuple[Any, Trigger]], now: _dt.datetime | _dt.date | None = None) -> list[Match]:
    """All matches at the highest specificity. One item is a clear winner;
    several is ambiguity; none is no match."""
    return read(text, candidates, now).matches
