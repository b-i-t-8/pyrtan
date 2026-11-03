"""Calendar clocks for watches: ``at = "mon-fri 09:00"``.

A schedule is days and times of day, in a fixed little grammar::

    "09:00"                     every day at nine
    "mon-fri 09:00, 17:30"      weekdays, twice
    "sat,sun 10am"              the weekend, at ten
    "weekdays noon"             also: daily, every day, weekends
    "fri-mon 22:00"             a range may wrap the week

Days are names (``mon`` or ``monday``), ranges of them, or the words
above; no days means every day. Times are read by :func:`nlp.parse_time`
(``09:00``, ``9am``, ``noon``, ``midnight``), so a time has no space in
it. The times are in ``tz`` (an IANA zone such as ``Europe/Oslo``), or
UTC when none is given.

:func:`next_after` is the whole of the arithmetic: the first slot strictly
after an instant. A local time that a change of clocks skips is run at the
same instant the hour before would have been, shifted (02:30 on the night
clocks go forward runs at 03:30); one that happens twice runs once, the
first time. A slot missed while nothing was running is not made up.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

from . import nlp

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WORDS = {"daily": range(7), "everyday": range(7), "weekdays": range(5), "weekends": range(5, 7), "weekend": range(5, 7)}


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    days: frozenset[int]          # 0 = Monday, as datetime.weekday()
    times: tuple[tuple[int, int], ...]  # (hour, minute), sorted

    def describe(self) -> str:
        days = "every day" if len(self.days) == 7 else ",".join(DAYS[d] for d in sorted(self.days))
        return f"{days} {', '.join(f'{h:02d}:{m:02d}' for h, m in self.times)}"


def _day(word: str) -> int:
    i = nlp._weekday(word)
    if i is None:
        raise ScheduleError(f"{word!r} is not a day; say mon, tuesday, mon-fri, weekdays, weekends or daily")
    return i


def parse_at(text: object) -> Schedule:
    if not isinstance(text, str) or not text.strip():
        raise ScheduleError("'at' is days and times of day, such as \"mon-fri 09:00\"")
    t = re.sub(r"\bevery\s+day\b", "everyday", text.strip().lower())
    days: set[int] = set()
    times: set[tuple[int, int]] = set()
    for token in (x for x in re.split(r"[\s,]+", t) if x):
        if token in WORDS:
            days.update(WORDS[token])
        elif re.fullmatch(r"[a-z]+-[a-z]+", token):
            first, last = (_day(w) for w in token.split("-"))
            days.update((first + i) % 7 for i in range((last - first) % 7 + 1))
        elif re.fullmatch(r"[a-z]+", token) and nlp._weekday(token) is not None:
            days.add(_day(token))
        else:
            try:
                hh, mm = nlp.parse_time(token).split(":")
            except nlp.ParseError as e:
                raise ScheduleError(f"{token!r} in {text!r} is not a day or a time of day ({e})") from None
            times.add((int(hh), int(mm)))
    if not times:
        raise ScheduleError(f"{text!r} names no time of day; say when, such as \"{text.strip()} 09:00\"")
    return Schedule(frozenset(days or range(7)), tuple(sorted(times)))


def zone(name: object) -> _dt.tzinfo:
    """The zone ``tz`` names; UTC when it names none."""
    if name is None or name == "UTC":
        return _dt.timezone.utc
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*", name):
        raise ScheduleError(f"'tz' is a time zone name such as \"Europe/Oslo\", got {name!r}")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError(f"{name!r} is not a time zone this machine knows") from None


def next_after(schedule: Schedule, after: _dt.datetime, tz: _dt.tzinfo) -> _dt.datetime:
    """The first slot strictly after ``after`` (an aware instant), in UTC."""
    local = after.astimezone(tz)
    for d in range(8):
        day = local.date() + _dt.timedelta(days=d)
        if day.weekday() not in schedule.days:
            continue
        for hour, minute in schedule.times:
            slot = _dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(_dt.timezone.utc)
            if slot > after:
                return slot
    raise AssertionError("a schedule has a slot every week")  # every schedule has a day and a time


def slot_name(at: _dt.datetime, tz: _dt.tzinfo) -> str:
    """How a slot reads in a trace: ``fri 09:00``, in the schedule's own zone."""
    local = at.astimezone(tz)
    return f"{DAYS[local.weekday()]} {local:%H:%M}"
