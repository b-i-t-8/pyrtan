"""Triggers: how a sentence finds the tree that answers it.

A trigger is a pattern with slots::

    "weather in {city}"
    "top {count:int} stories on hacker news"
    "is {host:word} up"
    "what's [the] latest release of {repo}"

A slot is ``{name}`` (any text), ``{name:int}``, ``{name:float}`` or
``{name:word}`` (no spaces). Square brackets mark optional words, round
brackets a required choice, and ``|`` separates alternatives inside either:
``"how (hot|cold) is it in {place}"``. Matching is
case-insensitive, ignores surrounding punctuation, and collapses whitespace.

When several triggers match, the most specific wins: the one with the most
literal text. A tie is reported as ambiguity rather than resolved by luck.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SLOT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::(int|float|word|text))?\}")
SLOT_RE = {
    "int": r"-?\d+",
    "float": r"-?\d+(?:\.\d+)?",
    "word": r"\S+",
    "text": r".+?",
    None: r".+?",
}
CAST = {"int": int, "float": float, "word": str, "text": str, None: str}


class TriggerError(ValueError):
    pass


def normalise(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^[\s\W_]+|[\s?!.,;:]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


@dataclass
class Trigger:
    pattern: str
    fixed_inputs: dict[str, Any] = field(default_factory=dict)
    regex: re.Pattern[str] = field(init=False, repr=False)
    slots: dict[str, str | None] = field(init=False, default_factory=dict)
    specificity: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.regex, self.slots, self.specificity = compile_pattern(self.pattern)

    def match(self, text: str) -> dict[str, Any] | None:
        m = self.regex.fullmatch(normalise(text))
        if not m:
            return None
        inputs: dict[str, Any] = dict(self.fixed_inputs)
        for name, kind in self.slots.items():
            raw = m.group(name)
            if raw is None:
                continue
            try:
                inputs[name] = CAST[kind](raw.strip())
            except ValueError:
                return None
        return inputs


def compile_pattern(pattern: str) -> tuple[re.Pattern[str], dict[str, str | None], int]:
    if not pattern or not pattern.strip():
        raise TriggerError("a trigger pattern cannot be empty")
    slots: dict[str, str | None] = {}
    out: list[str] = []
    literal_chars = 0
    pos = 0
    depth = 0
    text = pattern.strip().lower()
    while pos < len(text):
        ch = text[pos]
        if ch == "{":
            m = SLOT.match(text, pos)
            if not m:
                raise TriggerError(f"bad slot in trigger {pattern!r} at {pos}")
            name, kind = m.group(1), m.group(2)
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
                raise TriggerError(f"unbalanced ] in trigger {pattern!r}")
            depth -= 1
            out.append(r")?")
            pos += 1
        elif ch == "(":
            depth += 1
            out.append(r"(?:")
            pos += 1
        elif ch == ")":
            if depth == 0:
                raise TriggerError(f"unbalanced ) in trigger {pattern!r}")
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
    if depth != 0:
        raise TriggerError(f"unbalanced [ in trigger {pattern!r}")
    if literal_chars == 0:
        raise TriggerError(f"trigger {pattern!r} has no literal words; it would match anything")
    try:
        regex = re.compile("".join(out), re.IGNORECASE)
    except re.error as e:
        raise TriggerError(f"trigger {pattern!r} does not compile: {e}") from e
    return regex, slots, literal_chars


@dataclass
class Match:
    tree: str
    trigger: Trigger
    inputs: dict[str, Any]
    specificity: int


def best_matches(text: str, candidates: list[tuple[str, Trigger]]) -> list[Match]:
    """All matches at the highest specificity. One item is a clear winner;
    several is ambiguity; none is no match."""
    matches: list[Match] = []
    for tree, trig in candidates:
        inputs = trig.match(text)
        if inputs is not None:
            matches.append(Match(tree, trig, inputs, trig.specificity))
    if not matches:
        return []
    top = max(m.specificity for m in matches)
    return [m for m in matches if m.specificity == top]
