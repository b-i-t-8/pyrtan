"""Calendar watches: ``at = "mon-fri 09:00"`` with an optional ``tz``."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from pyrtan import Registry
from pyrtan.schedule import ScheduleError, next_after, parse_at, slot_name, zone
from pyrtan.service import Service
from pyrtan.tree import TreeError, load_tree_text
from pyrtan.watch import Watcher

UTC = dt.timezone.utc
OSLO = zone("Europe/Oslo")


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize("text, reads", [
    ("09:00", "every day 09:00"),
    ("mon-fri 09:00, 17:30", "mon,tue,wed,thu,fri 09:00, 17:30"),
    ("sat,sun 10am", "sat,sun 10:00"),
    ("weekdays noon", "mon,tue,wed,thu,fri 12:00"),
    ("Every day midnight", "every day 00:00"),
    ("fri-mon 22:00", "mon,fri,sat,sun 22:00"),  # a range may wrap the week
    ("Monday 9:15 17:00", "mon 09:15, 17:00"),
])
def test_the_grammar(text, reads):
    assert parse_at(text).describe() == reads


@pytest.mark.parametrize("text, words", [
    ("mon-fri", "names no time of day"),
    ("someday 09:00", "'someday' in 'someday 09:00' is not a day or a time of day"),
    ("mon-funday 09:00", "'funday' is not a day"),
    ("09:00 25:00", "'25:00'"),
    ("", "days and times of day"),
    (9, "days and times of day"),
])
def test_what_it_refuses(text, words):
    with pytest.raises(ScheduleError, match=words):
        parse_at(text)


def test_the_next_slot_is_strictly_after():
    when = parse_at("mon-fri 09:00, 17:30")
    # Friday 30 October 2026, 17:00 in Oslo (16:00 UTC, winter time).
    at = utc(2026, 10, 30, 16, 0)
    slots = []
    for _ in range(4):
        at = next_after(when, at, OSLO)
        slots.append(slot_name(at, OSLO))
    assert slots == ["fri 17:30", "mon 09:00", "mon 17:30", "tue 09:00"]
    assert next_after(when, utc(2026, 11, 2, 8, 0), OSLO) == utc(2026, 11, 2, 16, 30)  # at 09:00 itself, the next is 17:30
    assert next_after(parse_at("09:00"), utc(2026, 11, 2, 8, 0), UTC) == utc(2026, 11, 2, 9, 0)  # no tz: UTC


def test_a_change_of_clocks():
    two_thirty = parse_at("02:30")
    # Clocks go forward in Oslo at 02:00 on 28 March 2027: 02:30 does not happen, and runs as 03:30.
    spring = next_after(two_thirty, utc(2027, 3, 27, 12, 0), OSLO)
    assert spring == utc(2027, 3, 28, 1, 30) and slot_name(spring, OSLO) == "sun 03:30"
    assert next_after(two_thirty, spring, OSLO) == utc(2027, 3, 29, 0, 30)
    # They go back at 03:00 on 25 October 2026: 02:30 happens twice, and runs once, the first time.
    autumn = next_after(two_thirty, utc(2026, 10, 24, 12, 0), OSLO)
    assert autumn == utc(2026, 10, 25, 0, 30)
    assert next_after(two_thirty, autumn, OSLO) == utc(2026, 10, 26, 1, 30)


def test_zones():
    assert zone(None) is UTC and zone("UTC") is UTC
    for bad in ("Mars/Olympus", "../etc", 3):
        with pytest.raises(ScheduleError):
            zone(bad)


BASE = '''
name = "standup"
description = "says good morning on weekdays"
start = "say"

[inputs.slot]
type = "str"
default = ""

[[watches]]
name = "morning"
at = "mon-fri 09:00"
tz = "Europe/Oslo"
inputs = { slot = "{{ event.slot }}" }

[nodes.say]
kind = "done"
say = "good morning ({{ inputs.slot }})"
'''


def test_check_holds_a_calendar_watch():
    tree = load_tree_text(BASE)
    assert tree.capability()["watches"] == [{"kind": "at", "what": "mon-fri 09:00 (Europe/Oslo)"}]
    for old, new, words in [
        ('at = "mon-fri 09:00"', 'at = "mon-fri"', "names no time of day"),
        ('tz = "Europe/Oslo"', 'tz = "Europe/Nowhere"', "not a time zone this machine knows"),
        ('at = "mon-fri 09:00"\ntz = "Europe/Oslo"', 'every = "5m"\ntz = "Europe/Oslo"', "'tz' is the zone of an 'at' watch's times"),
        ('at = "mon-fri 09:00"', 'at = "mon-fri 09:00"\nevery = "5m"', "a watch is exactly one of every, at, file"),
    ]:
        with pytest.raises(TreeError, match=words):
            load_tree_text(BASE.replace(old, new))


def test_a_calendar_watch_runs_at_its_slot(tmp_path):
    d = tmp_path / "trees"
    d.mkdir()
    (d / "standup.toml").write_text(BASE)
    reg = Registry.from_paths([d])
    svc = Service(reg, name="s")
    # The watch starts on Friday 30 October 2026 at 08:59:59 in Oslo; the
    # clock then reads 09:00:01, so the slot is due at once.
    start = utc(2026, 10, 30, 7, 59, 59)
    readings = iter([start])
    clock = lambda: next(readings, utc(2026, 10, 30, 8, 0, 1))  # noqa: E731
    fired = []
    watcher = Watcher(reg, svc.run_watched, clock=clock, after=lambda w, e, o: fired.append((e, o))).start()
    try:
        deadline = time.time() + 10
        while not fired and time.time() < deadline:
            time.sleep(0.05)
        assert fired, "the slot never ran"
        event, outcome = fired[0]
        assert event == {"kind": "at", "n": 1, "at": "2026-10-30T08:00:00Z", "slot": "fri 09:00", "tz": "Europe/Oslo"}
        assert outcome.say == "good morning (fri 09:00)"
        assert outcome.trace.steps[0].summary == "at fri 09:00 Europe/Oslo, run 1"
        status = watcher.status()["watches"][0]
        assert status["what"] == "at mon-fri 09:00 (Europe/Oslo)"
        # The next slot is Monday's: the weekend is skipped.
        deadline = time.time() + 5
        while watcher.status()["watches"][0].get("next_at") == utc(2026, 10, 30, 8, 0).timestamp() and time.time() < deadline:
            time.sleep(0.05)
        assert watcher.status()["watches"][0]["next_at"] == utc(2026, 11, 2, 8, 0).timestamp()
        assert len(fired) == 1
    finally:
        watcher.stop()


def runs(text: str, after: dt.datetime, n: int = 5, tz=OSLO) -> list[str]:
    when, out = parse_at(text), []
    for _ in range(n):
        after = next_after(when, after, tz)
        out.append(after.astimezone(tz).strftime("%a %Y-%m-%d %H:%M"))
    return out


def test_days_of_the_month():
    assert parse_at("1st,15th 09:00").describe() == "1st,15th 09:00"
    assert runs("1st,15th 09:00", utc(2026, 11, 1, 12, 0), 4) == ["Sun 2026-11-15 09:00", "Tue 2026-12-01 09:00", "Tue 2026-12-15 09:00", "Fri 2027-01-01 09:00"]
    # A month without the day is passed over, as cron does; "last" is each month's own last day.
    assert runs("31st 08:00", utc(2026, 1, 1), 3) == ["Sat 2026-01-31 08:00", "Tue 2026-03-31 08:00", "Sun 2026-05-31 08:00"]
    assert runs("last 18:00", utc(2028, 1, 1), 3) == ["Mon 2028-01-31 18:00", "Tue 2028-02-29 18:00", "Fri 2028-03-31 18:00"]  # a leap year


def test_every_other_week_counts_from_the_week_of_its_from_date():
    # From Wednesday 4 November 2026: that Monday is before it, so the first run is the Thursday.
    assert runs("every 2 weeks from 2026-11-04 mon,thu 10:00", utc(2026, 10, 1)) == [
        "Thu 2026-11-05 10:00", "Mon 2026-11-16 10:00", "Thu 2026-11-19 10:00", "Mon 2026-11-30 10:00", "Thu 2026-12-03 10:00"]
    assert parse_at("every other week from 2026-11-02 fri 17:00") == parse_at("every 2 weeks from 2026-11-02 fri 17:00")
    assert parse_at("fortnightly from 2026-11-02 fri 17:00").describe() == "every 2 weeks from 2026-11-02 fri 17:00"
    assert runs("every 3 weeks from 2026-11-02 mon 09:00", utc(2026, 11, 3), 2) == ["Mon 2026-11-23 09:00", "Mon 2026-12-14 09:00"]
    # Across a new year the weeks keep counting (an ISO week number would restart).
    assert runs("every 2 weeks from 2026-12-21 mon 09:00", utc(2026, 12, 1), 3) == ["Mon 2026-12-21 09:00", "Mon 2027-01-04 09:00", "Mon 2027-01-18 09:00"]


@pytest.mark.parametrize("text, words", [
    ("fortnightly mon 09:00", "counts from a week; say which"),
    ("every 2 weeks from soon mon 09:00", "'soon' is not a date"),
    ("every 0 weeks from 2026-11-02 mon 09:00", "every 0 weeks is never"),
    ("1st mon 09:00", "days of the month and days of the week"),
    ("every 2 weeks from 2026-11-02 1st 09:00", "names days of the week, not of the month"),
    ("32nd 09:00", "not a day of the month"),
    ("2th 09:00", "not a day of the month"),
])
def test_what_the_calendar_refuses(text, words):
    with pytest.raises(ScheduleError, match=words):
        parse_at(text)


def test_a_weekday_by_its_place_in_the_month():
    assert parse_at("first mon, third wed 08:00").describe() == "first mon, third wed 08:00"
    assert runs("first mon 09:00", utc(2026, 11, 1), 3) == ["Mon 2026-11-02 09:00", "Mon 2026-12-07 09:00", "Mon 2027-01-04 09:00"]
    assert runs("last fri 17:00", utc(2026, 11, 1), 3) == ["Fri 2026-11-27 17:00", "Fri 2026-12-25 17:00", "Fri 2027-01-29 17:00"]
    assert runs("first mon, third wed 08:00", utc(2026, 11, 1), 4) == ["Mon 2026-11-02 08:00", "Wed 2026-11-18 08:00", "Mon 2026-12-07 08:00", "Wed 2026-12-16 08:00"]
    # A month without a fifth Monday is passed over: after 30 November 2026, the next is in March.
    assert runs("fifth mon 09:00", utc(2026, 11, 1), 2) == ["Mon 2026-11-30 09:00", "Mon 2027-03-29 09:00"]
    # "last" with no day after it is still the month's last day; "last fri" is the last Friday.
    assert parse_at("last 18:00").monthdays and not parse_at("last 18:00").nth
    assert parse_at("last fri 18:00").nth == frozenset({(-1, 4)})


@pytest.mark.parametrize("text, words", [
    ("first 09:00", "'first' needs a day after it"),
    ("first mon-fri 09:00", "'first' needs a day after it"),
    ("first mon, 15th 09:00", "a schedule is one or the other"),
    ("first mon, fri 09:00", "a schedule is one or the other"),
    ("every 2 weeks from 2026-11-02 first mon 09:00", "not their place in the month"),
])
def test_what_a_weekday_s_place_refuses(text, words):
    with pytest.raises(ScheduleError, match=words):
        parse_at(text)
