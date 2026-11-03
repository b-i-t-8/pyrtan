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
