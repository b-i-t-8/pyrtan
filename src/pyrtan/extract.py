"""Pulling values out of what was observed.

An ``extract`` entry names where the value goes (``into``) and one way of
finding it:

    { into = "title",   json = "json.items[0].title" }
    { into = "price",   regex = "\\$([0-9.]+)", group = 1 }
    { into = "heading", css = "h1" }                      # text of first match
    { into = "links",   css = "a.story", attr = "href", all = true }
    { into = "rows",    css = "table tr", all = true, fields = { name = "td:nth-child(1)", qty = "td:nth-child(2)" } }
    { into = "temp",    pattern = "temperature: {t:number}°C" }
    { into = "wind",    pattern = "wind {speed:number} km/h from the {dir:word}" }

``pattern`` is a trigger's pattern, searched for in the text: slots and
their kinds (each read by its fixed parser), ``[optional]`` words,
``(a|b)`` choices, case ignored, a word matching its plural. A pattern
with one slot gives that slot's value; with several, a table of them.
Literal words match whole words, and a slot whose kind reads lazily
(``{name}``, ``text``, ``date``, ``duration``) at the end of the pattern
reads to the end of its line. An occurrence whose slots do not read (a
``date`` that is not one) is passed over for the next.

The source is the observation's state (``json``, ``body``, ``status`` …) or
``from`` when given. Extraction never raises on a miss; the value is simply
absent, and a ``decide`` node downstream says what that means.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from . import html as htmlmod
from . import intent, nlp
from .template import MISSING, resolve


class ExtractError(ValueError):
    pass


WAYS = {"json", "regex", "css", "path", "pattern"}


def validate(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise ExtractError("an extract entry is a table")
    if "into" not in entry or not isinstance(entry["into"], str):
        raise ExtractError("extract entry needs a string 'into'")
    ways = WAYS & set(entry)
    if len(ways) != 1:
        raise ExtractError(f"extract entry {entry['into']!r} needs exactly one of {sorted(WAYS)}")
    if "regex" in entry:
        try:
            re.compile(entry["regex"])
        except re.error as e:
            raise ExtractError(f"bad regex in extract {entry['into']!r}: {e}") from e
    if "pattern" in entry:
        if not isinstance(entry["pattern"], str):
            raise ExtractError(f"the pattern in extract {entry['into']!r} must be a string")
        try:
            _, slots = compiled(entry["pattern"])
        except intent.TriggerError as e:
            raise ExtractError(f"bad pattern in extract {entry['into']!r}: {e}") from e
        if not slots:
            raise ExtractError(f"the pattern in extract {entry['into']!r} has no {{slot}}, so it reads nothing")
    if "css" in entry:
        try:
            htmlmod.compile_selector(entry["css"])
        except htmlmod.SelectorError as e:
            raise ExtractError(f"bad selector in extract {entry['into']!r}: {e}") from e
        for sub in (entry.get("fields") or {}).values():
            try:
                htmlmod.compile_selector(sub if isinstance(sub, str) else sub["css"])
            except (htmlmod.SelectorError, KeyError, TypeError) as e:
                raise ExtractError(f"bad field selector in extract {entry['into']!r}: {e}") from e


def extract(entry: dict[str, Any], source: dict[str, Any], doc_cache: dict[int, htmlmod.Element] | None = None) -> Any:
    """Return the extracted value, or :data:`MISSING`."""
    if "json" in entry or "path" in entry:
        path = entry.get("json", entry.get("path"))
        return resolve(path, source)
    if "regex" in entry:
        text = _text_source(entry, source)
        if text is MISSING:
            return MISSING
        pattern = re.compile(entry["regex"], re.DOTALL if entry.get("dotall") else 0)
        group = entry.get("group", 1 if pattern.groups else 0)
        if entry.get("all"):
            return [m.group(group) for m in pattern.finditer(text)]
        m = pattern.search(text)
        return m.group(group) if m else MISSING
    if "pattern" in entry:
        text = _text_source(entry, source)
        if text is MISSING:
            return MISSING
        return _search(entry["pattern"], text, bool(entry.get("all")))
    if "css" in entry:
        text = _text_source(entry, source)
        if text is MISSING:
            return MISSING
        key = id(text)
        if doc_cache is not None and key in doc_cache:
            doc = doc_cache[key]
        else:
            doc = htmlmod.parse(text)
            if doc_cache is not None:
                doc_cache[key] = doc
        found = doc.select(entry["css"])
        if entry.get("all"):
            return [_element_value(entry, el) for el in found]
        if not found:
            return MISSING
        return _element_value(entry, found[0])
    raise ExtractError(f"extract entry {entry.get('into')!r} has no way")


@functools.lru_cache(maxsize=256)
def compiled(pattern: str) -> tuple[re.Pattern[str], dict[str, str | None]]:
    regex, slots, _, _, _ = intent.compile_pattern(nlp.fold(pattern), search=True)
    return regex, slots


def _search(pattern: str, text: str, every: bool) -> Any:
    regex, slots = compiled(pattern)
    # Folded as a sentence is (quotes, dashes, spaces), line by line, so a
    # slot at the end of the pattern still ends with its line.
    text = "\n".join(nlp.fold(line) for line in text.splitlines())
    found: list[Any] = []
    for m in regex.finditer(text):
        values: dict[str, Any] = {}
        try:
            for name, kind in slots.items():
                raw = m.group(name)
                if raw is not None:
                    values[name] = intent.read_slot(kind, raw.strip(), None)
        except (ValueError, nlp.ParseError):
            continue  # not this occurrence: perhaps the next
        if len(slots) == 1:
            if not values:
                continue
            value: Any = next(iter(values.values()))
        else:
            value = values
        if not every:
            return value
        found.append(value)
    return found if every else MISSING


def _text_source(entry: dict[str, Any], source: dict[str, Any]) -> Any:
    frm = entry.get("from", "body")
    value = resolve(frm, source) if isinstance(frm, str) else MISSING
    if value is MISSING or value is None:
        return MISSING
    return value if isinstance(value, str) else str(value)


def _element_value(entry: dict[str, Any], el: htmlmod.Element) -> Any:
    fields = entry.get("fields")
    if fields:
        out: dict[str, Any] = {}
        for name, spec in fields.items():
            if isinstance(spec, str):
                sub = el.select_one(spec)
                out[name] = sub.text if sub else None
            else:
                sub = el.select_one(spec["css"])
                if sub is None:
                    out[name] = None
                elif "attr" in spec:
                    out[name] = sub.get(spec["attr"])
                else:
                    out[name] = sub.text
        return out
    if "attr" in entry:
        return el.get(entry["attr"])
    return el.text
