"""Language rules: how a sentence is read before a trigger sees it.

This is the whole of Pyrtan's "natural language processing", and none of
it is a model. Every function here is a fixed table or a fixed rule: a
sentence goes in, a rewritten sentence (or a value) comes out, and the same
input always gives the same output. Each rewrite that fires is named, so
the trace can show exactly how a sentence was read.

What the rules do:

- :func:`clean` rewrites a sentence step by step: fold unicode, drop
  greetings and politeness at the edges, expand contractions, turn number
  words into digits. The same rules are applied to trigger patterns at
  load time, so a pattern written as ``[what's the] weather`` still matches
  a sentence written either way.
- :func:`parse_date`, :func:`parse_time`, :func:`parse_duration`,
  :func:`parse_number`, :func:`parse_host`, :func:`parse_url` and
  :func:`parse_email` read one value each, for the typed slots a trigger
  may declare (``{when:date}``, ``{for:duration}``, ``{site:host}``...).
  Dates are relative to a ``now`` the caller passes in; the runtime passes
  its clock, so a run is deterministic given its observations.
- :func:`singular` is a conservative stemmer used so that a literal word in
  a pattern matches its plural (``story``/``stories``), and nothing else.
- :func:`distance` is edit distance, for the opt-in typo rule.

What the rules refuse: anything that would need a guess. A date that could
be read two ways is not read at all. A word not in any table is left alone.
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Any, Callable

# ---------------------------------------------------------------- tables

CONTRACTIONS = {
    "what's": "what is", "whats": "what is", "where's": "where is", "who's": "who is", "how's": "how is",
    "when's": "when is", "why's": "why is", "it's": "it is", "that's": "that is", "there's": "there is",
    "here's": "here is", "let's": "let us", "isn't": "is not", "aren't": "are not", "wasn't": "was not",
    "weren't": "were not", "don't": "do not", "doesn't": "does not", "didn't": "did not", "can't": "can not",
    "cannot": "can not", "couldn't": "could not", "won't": "will not", "wouldn't": "would not",
    "shouldn't": "should not", "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "i'm": "i am", "i've": "i have", "i'd": "i would", "i'll": "i will", "you're": "you are",
    "you've": "you have", "you'd": "you would", "you'll": "you will", "we're": "we are", "we've": "we have",
    "they're": "they are", "they've": "they have", "gimme": "give me", "lemme": "let me", "wanna": "want to",
    "gonna": "going to", "u": "you", "pls": "please", "plz": "please", "thx": "thanks",
}

# Said at the start of a sentence and meaning nothing to a tree.
LEADING_FILLERS = [
    "hey pyrtan", "hi pyrtan", "hello pyrtan", "ok pyrtan", "okay pyrtan", "pyrtan",
    "hey", "hi", "hello", "yo", "ok", "okay", "so", "um", "uh", "well", "please", "kindly",
    "can you", "could you", "would you", "will you", "can you please", "could you please", "would you please",
    "i want you to", "i need you to", "i would like you to", "i'd like you to",
    "quick question", "question",
]
# Said at the end, likewise.
TRAILING_FILLERS = ["please", "thanks", "thank you", "for me", "if you can", "if you could", "if possible", "pyrtan"]

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
NUMBER_WORDS = set(UNITS) | set(TENS) | set(SCALES) | {"and", "a", "dozen"}

# Words whose trailing s is not a plural, so :func:`singular` leaves them.
NOT_PLURALS = {
    "news", "is", "as", "us", "his", "this", "thus", "yes", "plus", "bus", "gas", "was", "has", "its",
    "status", "bonus", "always", "perhaps", "physics", "series", "species", "means", "analysis", "basis",
    "lens", "chess", "address", "process", "access", "class", "pass", "less", "unless", "across", "ssl",
    "https", "dns", "tls", "os", "ios", "chaos", "canvas", "atlas", "virus", "campus", "census", "focus",
    "iris", "oasis", "crisis", "thesis", "diagnosis", "axis", "texas", "paris", "mars", "venus",
}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_ABBR = {d[:3]: i for i, d in enumerate(WEEKDAYS)}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)}
MONTH_ABBR["sept"] = 9

DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
}

WORD = re.compile(r"[A-Za-z][A-Za-z'’]*|\d+(?:[.,]\d+)*|\S")
HOST_RE = re.compile(r"^(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}|localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?:/\S*)?$", re.IGNORECASE)
URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}$")


# ---------------------------------------------------------------- cleaning


def fold(text: str) -> str:
    """Unicode to its plain form: curly quotes straight, full-width ASCII,
    dashes to hyphens, whitespace to one space."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def trim(text: str) -> str:
    """Drop punctuation that carries nothing: around the whole sentence, and
    a comma or full stop between words."""
    text = re.sub(r"^[\s\W_]+|[\s?!.,;:]+$", "", text)
    text = re.sub(r"\s*[,;!?]+\s*", " ", text)
    text = re.sub(r"(?<=[A-Za-z])\.(?=\s|$)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_fillers(text: str, keep: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    """Greetings, politeness and request wrappers at either edge, repeatedly.
    A filler whose first word is in ``keep`` stays: a trigger somewhere
    starts (or ends) with that word, so to that swarm it is not filler."""
    dropped: list[str] = []
    changed = True
    while changed and text:
        changed = False
        low = text.lower()
        for f in sorted(LEADING_FILLERS, key=len, reverse=True):
            if (low == f or low.startswith(f + " ") or low.startswith(f + ",")) and f.split()[0] not in keep:
                dropped.append(text[: len(f)])
                text = text[len(f):].lstrip(" ,")
                changed = True
                break
        low = text.lower()
        for f in sorted(TRAILING_FILLERS, key=len, reverse=True):
            if (low == f or low.endswith(" " + f) or low.endswith("," + f)) and f.split()[-1] not in keep:
                dropped.append(text[len(text) - len(f):])
                text = text[: len(text) - len(f)].rstrip(" ,")
                changed = True
                break
    return text, dropped


def expand_contractions(text: str) -> tuple[str, list[str]]:
    expanded: list[str] = []

    def one(m: re.Match[str]) -> str:
        word = m.group(0)
        full = CONTRACTIONS.get(word.lower().replace("’", "'"))
        if full is None:
            return word
        expanded.append(f"{word}->{full}")
        return full if word.islower() or not word[0].isupper() else full[0].upper() + full[1:]

    return re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", one, text), expanded


def words_to_digits(text: str) -> tuple[str, list[str]]:
    """``twenty-one`` -> ``21``, ``two hundred and five`` -> ``205``, ``a
    dozen`` -> ``12``. Only runs of number words are touched."""
    words = text.split(" ")
    out: list[str] = []
    changed: list[str] = []
    i = 0
    while i < len(words):
        run_end = _number_run(words, i)
        if run_end > i:
            run = words[i:run_end]
            value = _run_value(run)
            if value is not None:
                out.append(str(value))
                changed.append(f"{' '.join(run)}->{value}")
                i = run_end
                continue
        out.append(words[i])
        i += 1
    return " ".join(out), changed


def _is_number_word(word: str) -> bool:
    parts = word.lower().split("-")
    return all(p in UNITS or p in TENS or p in SCALES or p == "dozen" for p in parts)


def _number_run(words: list[str], start: int) -> int:
    """The end (exclusive) of the longest run of number words at ``start``:
    ``two hundred and five`` or ``a dozen``; ``and``/``a`` only inside or,
    for ``a``, before a scale word. Returns ``start`` when there is none."""
    i = start
    first = words[i].lower()
    if first == "a" and i + 1 < len(words) and words[i + 1].lower() in ("dozen", "hundred", "thousand", "million", "billion"):
        i += 1
    elif not _is_number_word(first):
        return start
    end = i
    while i < len(words):
        w = words[i].lower()
        if _is_number_word(w):
            i += 1
            end = i
        elif w in ("and", "a") and i + 1 < len(words) and _is_number_word(words[i + 1].lower()):
            i += 1
        else:
            break
    return end


def _run_value(run: list[str]) -> int | None:
    words: list[str] = []
    for tok in run:
        words.extend(tok.lower().split("-"))
    total = 0
    current = 0
    seen_number = False
    for w in words:
        if w in ("and", "a"):
            if w == "a":
                current = 1
            continue
        if w in UNITS:
            current += UNITS[w]
            seen_number = True
        elif w in TENS:
            current += TENS[w]
            seen_number = True
        elif w == "dozen":
            current = (current or 1) * 12
            seen_number = True
        elif w == "hundred":
            current = (current or 1) * 100
            seen_number = True
        elif w in SCALES:
            total += (current or 1) * SCALES[w]
            current = 0
            seen_number = True
        else:
            return None
    return total + current if seen_number else None


def clean(text: str, *, fillers: bool = True, numbers: bool = True, keep: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    """The whole pipeline. Returns the rewritten sentence and the list of
    rules that fired, each as ``rule: detail``, in order. ``keep`` names
    words that are not filler here (see :func:`strip_fillers`)."""
    steps: list[str] = []
    folded = fold(text)
    if folded != text.strip():
        steps.append("fold")
    text = trim(folded)
    if fillers:
        text, dropped = strip_fillers(text, keep)
        if dropped:
            steps.append("fillers: " + ", ".join(repr(d) for d in dropped))
            text = trim(text)
    text, expanded = expand_contractions(text)
    if expanded:
        steps.append("contractions: " + ", ".join(expanded))
    if numbers:
        text, changed = words_to_digits(text)
        if changed:
            steps.append("numbers: " + ", ".join(changed))
    return re.sub(r"\s+", " ", text).strip(), steps


# ---------------------------------------------------------------- words


def singular(word: str) -> str:
    """A conservative singular: ``stories`` -> ``story``, ``boxes`` ->
    ``box``, ``items`` -> ``item``; ``news`` stays ``news``. Short words and
    words ending in ``ss`` are left alone."""
    w = word.lower()
    if len(w) < 4 or w in NOT_PLURALS or w.endswith("ss") or not w.isalpha():
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def plural(word: str) -> str:
    w = word.lower()
    if not w.isalpha() or w in NOT_PLURALS:
        return w
    if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("ch", "sh", "ss", "x", "z", "s")):
        return w + "es"
    return w + "s"


def forms(word: str) -> set[str]:
    """Every spelling a literal pattern word may be matched by: itself, its
    singular and its plural."""
    w = word.lower()
    out = {w}
    if w.isalpha() and len(w) >= 3:
        base = singular(w)
        out.add(base)
        out.add(plural(base))
    return out


def distance(a: str, b: str, limit: int = 2) -> int:
    """Edit distance with transpositions, stopping early past ``limit``."""
    a, b = a.lower(), b.lower()
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev_prev: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev_prev is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev_prev[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev_prev, prev = prev, cur
    return prev[-1]


# ---------------------------------------------------------------- values


class ParseError(ValueError):
    pass


def parse_number(text: str) -> int | float:
    t, _ = words_to_digits(text.strip())
    t = t.replace(",", "")
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    raise ParseError(f"{text!r} is not a number")


def parse_duration(text: str) -> int:
    """``2 hours``, ``30 min``, ``1h30m``, ``1 hour and a half``, ``an hour`` -> seconds."""
    t, _ = words_to_digits(text.strip().lower())
    t = re.sub(r"\bhalf\s+an?\s+([a-z]+)", r"0.5 \1", t)
    t = re.sub(r"\b(an?)\s+(?=(?:hour|minute|second|day|week)s?\b)", "1 ", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*([a-z]+)\s+and\s+a\s+half\b", lambda m: f"{float(m.group(1)) + 0.5} {m.group(2)}", t)
    t = re.sub(r"\s+and\s+", " ", t)
    total = 0.0
    found = False
    pos = 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([a-z]+)", t):
        if t[pos:m.start()].strip():
            raise ParseError(f"{text!r} is not a duration")
        unit = m.group(2)
        if unit not in DURATION_UNITS:
            raise ParseError(f"{text!r}: unknown unit {unit!r}")
        total += float(m.group(1)) * DURATION_UNITS[unit]
        found = True
        pos = m.end()
    if not found or t[pos:].strip():
        raise ParseError(f"{text!r} is not a duration")
    return int(total)


def parse_time(text: str) -> str:
    """``3pm``, ``15:30``, ``3:05 pm``, ``noon``, ``midnight``, ``9 o'clock`` -> ``HH:MM``."""
    t, _ = words_to_digits(text.strip().lower())
    t = t.replace(".", ":").replace(" ", "")
    if t in ("noon", "midday"):
        return "12:00"
    if t == "midnight":
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?:o'?clock)?(am|pm)?", t)
    if not m:
        raise ParseError(f"{text!r} is not a time")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            raise ParseError(f"{text!r}: {hour} with {half}")
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif m.group(2) is None and not m.group(0).endswith("clock"):
        raise ParseError(f"{text!r}: a bare number is not a time; say 15:00 or 3pm")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ParseError(f"{text!r} is not a time")
    return f"{hour:02d}:{minute:02d}"


def parse_date(text: str, now: _dt.date | _dt.datetime | None = None) -> str:
    """A date, as ``YYYY-MM-DD``, relative to ``now`` where it has to be.

    Reads ISO dates, ``25 oct [2026]``, ``oct 25[, 2026]``, ``25/10/2026``
    (day first; a slash date that could be read either way is refused),
    ``today``, ``tomorrow``, ``yesterday``, ``day after tomorrow``,
    weekdays (``friday`` is the next one, today included; ``next friday`` is
    strictly after today; ``last friday`` strictly before), ``in N days``,
    ``N days ago``, ``next week``, ``last month``, ``end of month``.
    """
    today = (now or _dt.datetime.now(_dt.timezone.utc)).date() if isinstance(now, _dt.datetime) or now is None else now
    t, _ = words_to_digits(text.strip().lower())
    t = re.sub(r"^(on|at|for|the|this coming)\s+", "", t).strip()
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    t = t.replace(",", " ").replace("  ", " ").strip()

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)), text).isoformat()
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if d <= 12 and mo <= 12 and d != mo:
            raise ParseError(f"{text!r} could be day/month or month/day; write it as {today.year}-{mo:02d}-{d:02d} or '{d} {MONTHS[mo - 1]}'")
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        return _date(year, mo, d, text).isoformat()
    m = re.fullmatch(r"(\d{1,2})(?:\s+of)?\s+([a-z]+)(?:\s+(\d{4}))?", t) or re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:\s+(\d{4}))?", t)
    if m and (_month(m.group(1)) or _month(m.group(2))):
        if m.group(1).isdigit():
            day, month = int(m.group(1)), _month(m.group(2))
        else:
            month, day = _month(m.group(1)), int(m.group(2))
        if not month:
            raise ParseError(f"{text!r} is not a date")
        year = int(m.group(3)) if m.group(3) else today.year
        return _date(year, month, day, text).isoformat()

    if t in ("today", "now"):
        return today.isoformat()
    if t in ("tomorrow", "tmrw", "tmr"):
        return (today + _dt.timedelta(days=1)).isoformat()
    if t in ("yesterday",):
        return (today - _dt.timedelta(days=1)).isoformat()
    if t in ("day after tomorrow", "the day after tomorrow"):
        return (today + _dt.timedelta(days=2)).isoformat()
    if t in ("day before yesterday", "the day before yesterday"):
        return (today - _dt.timedelta(days=2)).isoformat()

    m = re.fullmatch(r"(?:(this|next|last)\s+)?([a-z]+)", t)
    if m and _weekday(m.group(2)) is not None:
        target = _weekday(m.group(2))
        which = m.group(1)
        ahead = (target - today.weekday()) % 7
        if which == "next":
            ahead = ahead or 7
        elif which == "last":
            ahead = ahead - 7 if ahead else -7
        return (today + _dt.timedelta(days=ahead)).isoformat()

    m = re.fullmatch(r"in\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)", t)
    if m:
        return _shift(today, int(m.group(1)), m.group(2)).isoformat()
    m = re.fullmatch(r"(\d+)\s+(day|days|week|weeks|month|months|year|years)\s+ago", t)
    if m:
        return _shift(today, -int(m.group(1)), m.group(2)).isoformat()
    m = re.fullmatch(r"(next|last)\s+(week|month|year)", t)
    if m:
        return _shift(today, 1 if m.group(1) == "next" else -1, m.group(2)).isoformat()
    m = re.fullmatch(r"end of (?:the |this )?(month|year)", t)
    if m:
        if m.group(1) == "year":
            return _dt.date(today.year, 12, 31).isoformat()
        nxt = _shift(_dt.date(today.year, today.month, 1), 1, "month")
        return (nxt - _dt.timedelta(days=1)).isoformat()
    if t in ("start of month", "start of the month", "beginning of month", "beginning of the month"):
        return today.replace(day=1).isoformat()
    raise ParseError(f"{text!r} is not a date this reads")


def _month(word: str | None) -> int | None:
    if not word or word.isdigit():
        return None
    if word in MONTHS:
        return MONTHS.index(word) + 1
    return MONTH_ABBR.get(word[:4] if word.startswith("sept") else word[:3]) if len(word) >= 3 and word[:3] in MONTH_ABBR and MONTHS[MONTH_ABBR[word[:3]] - 1].startswith(word) else None


def _weekday(word: str) -> int | None:
    if word in WEEKDAYS:
        return WEEKDAYS.index(word)
    if len(word) >= 3 and word[:3] in WEEKDAY_ABBR and WEEKDAYS[WEEKDAY_ABBR[word[:3]]].startswith(word):
        return WEEKDAY_ABBR[word[:3]]
    return None


def _date(year: int, month: int, day: int, text: str) -> _dt.date:
    # Checked here rather than left to datetime, whose wording changes
    # between Python versions; pyrtan.js says the same words.
    if not 1 <= year <= 9999:
        why = f"year {year} is out of range"
    elif not 1 <= month <= 12:
        why = "month must be in 1..12"
    elif not 1 <= day <= _days_in(year, month):
        why = "day is out of range for month"
    else:
        return _dt.date(year, month, day)
    raise ParseError(f"{text!r}: {why}")


def _days_in(year: int, month: int) -> int:
    return [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]


def _shift(day: _dt.date, n: int, unit: str) -> _dt.date:
    unit = unit.rstrip("s")
    if unit == "day":
        return day + _dt.timedelta(days=n)
    if unit == "week":
        return day + _dt.timedelta(weeks=n)
    months = day.month - 1 + (n if unit == "month" else 12 * n)
    year, month = day.year + months // 12, months % 12 + 1
    return _dt.date(year, month, min(day.day, _days_in(year, month)))


def parse_host(text: str) -> str:
    m = HOST_RE.fullmatch(text.strip())
    if not m:
        raise ParseError(f"{text!r} is not a host name")
    return m.group(1).lower()


def parse_url(text: str) -> str:
    t = text.strip()
    if not URL_RE.fullmatch(t):
        raise ParseError(f"{text!r} is not a URL")
    return t


def parse_email(text: str) -> str:
    t = text.strip()
    if not EMAIL_RE.fullmatch(t):
        raise ParseError(f"{text!r} is not an email address")
    return t.lower()


PARSERS: dict[str, Callable[..., Any]] = {
    "number": parse_number,
    "duration": parse_duration,
    "time": parse_time,
    "date": parse_date,
    "host": parse_host,
    "url": parse_url,
    "email": parse_email,
}
