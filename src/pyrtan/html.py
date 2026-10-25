"""A small HTML document model with a CSS selector subset, on the standard
library's parser.

The subset is what trees actually need to read a page: tag, ``#id``,
``.class``, ``[attr]``, ``[attr=value]``, ``[attr^=v]``, ``[attr$=v]``,
``[attr*=v]``, ``:first-child``, ``:nth-child(n)``, ``:contains(text)``,
the descendant (space) and child (``>``) combinators, and ``,`` groups.
Anything else is a :class:`SelectorError` at tree check time, not a silent
empty match at run time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
INLINE = {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "del", "ins"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element | str"] = field(default_factory=list)
    parent: "Element | None" = field(default=None, repr=False, compare=False)

    @property
    def text(self) -> str:
        """All descendant text, whitespace collapsed. Inline elements join
        their neighbours directly; any other element boundary counts as a
        space, so two table cells never run together."""
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag in INLINE:
                parts.append(child.text)
            else:
                parts.append(" " + child.text + " ")
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    def elements(self) -> Iterator["Element"]:
        for child in self.children:
            if isinstance(child, Element):
                yield child

    def descendants(self) -> Iterator["Element"]:
        for child in self.elements():
            yield child
            yield from child.descendants()

    def select(self, selector: str) -> list["Element"]:
        return select(self, selector)

    def select_one(self, selector: str) -> "Element | None":
        found = select(self, selector)
        return found[0] if found else None

    def to_dict(self) -> dict:
        return {"tag": self.tag, "attrs": self.attrs, "text": self.text}


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        el = Element(tag.lower(), {k.lower(): (v if v is not None else "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(el)
        if tag.lower() not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        el = Element(tag.lower(), {k.lower(): (v if v is not None else "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(el)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse(text: str) -> Element:
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root


class SelectorError(ValueError):
    pass


_TOKEN = re.compile(
    r"""
    (?P<tag>[a-zA-Z][a-zA-Z0-9\-]*|\*)
  | \#(?P<id>[\w\-]+)
  | \.(?P<cls>[\w\-]+)
  | \[(?P<attr>[\w\-:]+)(?:(?P<op>[\^\$\*]?=)(?P<q>["']?)(?P<val>[^\]"']*)(?P=q))?\]
  | :(?P<pseudo>first-child|last-child|nth-child\(\d+\)|contains\((?P<cq>["']?)(?P<ctext>[^)"']*)(?P=cq)\))
    """,
    re.VERBOSE,
)


@dataclass
class _Simple:
    tag: str | None = None
    id: str | None = None
    classes: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str, str | None]] = field(default_factory=list)
    pseudos: list[str] = field(default_factory=list)

    def matches(self, el: Element) -> bool:
        if self.tag and self.tag != "*" and el.tag != self.tag:
            return False
        if self.id and el.attrs.get("id") != self.id:
            return False
        if self.classes and not set(self.classes) <= set(el.classes):
            return False
        for name, op, val in self.attrs:
            if name not in el.attrs:
                return False
            have = el.attrs[name]
            if op == "=" and have != val:
                return False
            if op == "^=" and not have.startswith(val or ""):
                return False
            if op == "$=" and not have.endswith(val or ""):
                return False
            if op == "*=" and (val or "") not in have:
                return False
        for p in self.pseudos:
            if not _pseudo(p, el):
                return False
        return True


def _pseudo(p: str, el: Element) -> bool:
    if p.startswith("contains:"):
        return p[len("contains:"):] in el.text
    siblings = list(el.parent.elements()) if el.parent else [el]
    if p == "first-child":
        return siblings[0] is el
    if p == "last-child":
        return siblings[-1] is el
    if p.startswith("nth-child("):
        n = int(p[len("nth-child("):-1])
        return 0 < n <= len(siblings) and siblings[n - 1] is el
    raise SelectorError(f"unknown pseudo-class :{p}")


def _parse_compound(text: str) -> _Simple:
    simple = _Simple()
    pos = 0
    text = text.strip()
    if not text:
        raise SelectorError("empty selector part")
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise SelectorError(f"cannot parse selector near {text[pos:]!r}")
        pos = m.end()
        if m.group("tag"):
            if simple.tag:
                raise SelectorError(f"two tags in one compound selector: {text!r}")
            simple.tag = m.group("tag").lower()
        elif m.group("id"):
            simple.id = m.group("id")
        elif m.group("cls"):
            simple.classes.append(m.group("cls"))
        elif m.group("attr"):
            simple.attrs.append((m.group("attr").lower(), m.group("op") or "", m.group("val")))
        elif m.group("pseudo"):
            p = m.group("pseudo")
            if p.startswith("contains("):
                simple.pseudos.append("contains:" + m.group("ctext"))
            else:
                simple.pseudos.append(p)
    return simple


def compile_selector(selector: str) -> list[list[tuple[str, _Simple]]]:
    """Compile to groups of (combinator, compound) pairs. Raises on anything
    outside the supported subset."""
    groups = []
    for alt in selector.split(","):
        alt = alt.strip()
        if not alt:
            raise SelectorError(f"empty alternative in {selector!r}")
        parts = re.split(r"\s*(>)\s*|\s+", alt)
        chain: list[tuple[str, _Simple]] = []
        comb = " "
        for part in parts:
            if part is None or part == "":
                continue
            if part == ">":
                comb = ">"
                continue
            chain.append((comb, _parse_compound(part)))
            comb = " "
        if not chain:
            raise SelectorError(f"nothing to match in {selector!r}")
        groups.append(chain)
    return groups


def select(root: Element, selector: str) -> list[Element]:
    compiled = compile_selector(selector)
    out: list[Element] = []
    seen: set[int] = set()
    for el in root.descendants():
        for chain in compiled:
            if _chain_matches(el, chain) and id(el) not in seen:
                seen.add(id(el))
                out.append(el)
                break
    return out


def _chain_matches(el: Element, chain: list[tuple[str, _Simple]]) -> bool:
    comb, simple = chain[-1]
    if not simple.matches(el):
        return False
    if len(chain) == 1:
        return True
    rest = chain[:-1]
    if comb == ">":
        return el.parent is not None and el.parent.tag != "document" and _chain_matches(el.parent, rest)
    anc = el.parent
    while anc is not None and anc.tag != "document":
        if _chain_matches(anc, rest):
            return True
        anc = anc.parent
    return False
