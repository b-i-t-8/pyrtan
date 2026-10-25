"""The language rules: fixed tables and fixed rules, the same answer every
time, and a refusal where a guess would be needed."""

import datetime as dt

import pytest

from pyrtan import nlp

SUNDAY = dt.date(2026, 10, 25)


def test_clean_names_every_rule_that_fired():
    text, steps = nlp.clean("Hey Pyrtan, what's the weather in Oslo please?")
    assert text == "what is the weather in Oslo"  # the place keeps its case
    assert steps == ["fillers: 'Hey Pyrtan', 'please'", "contractions: what's->what is"]
    text, steps = nlp.clean("Could you tell me twenty-one things")
    assert text == "tell me 21 things" and steps == ["fillers: 'Could you'", "numbers: twenty-one->21"]
    assert nlp.clean("is example.com up") == ("is example.com up", [])
    assert nlp.clean("What’s New York like")[0] == "What is New York like"  # curly quote folded


def test_fillers_are_kept_when_a_trigger_starts_with_them():
    assert nlp.clean("hi there")[0] == "there"
    assert nlp.clean("hi there", keep=frozenset({"hi"}))[0] == "hi there"
    assert nlp.clean("hey pyrtan, hi there", keep=frozenset({"hi"}))[0] == "hi there"
    assert nlp.clean("say thanks", keep=frozenset({"thanks"}))[0] == "say thanks"
    assert nlp.clean("say thanks")[0] == "say"


@pytest.mark.parametrize("text,value", [
    ("three", 3), ("twenty-one", 21), ("twenty one", 21), ("a dozen", 12), ("two hundred and five", 205),
    ("one hundred thousand", 100000), ("1,000", 1000), ("2.5", 2.5), ("-4", -4), ("ninety nine", 99),
])
def test_numbers(text, value):
    assert nlp.parse_number(text) == value


def test_number_words_only_touch_number_runs():
    assert nlp.words_to_digits("a dozen eggs and two hundred and five items") == ("12 eggs and 205 items", ["a dozen->12", "two hundred and five->205"])
    assert nlp.words_to_digits("set a reminder and one more")[0] == "set a reminder and 1 more"
    assert nlp.words_to_digits("one of a kind")[0] == "1 of a kind"
    with pytest.raises(nlp.ParseError):
        nlp.parse_number("many")


@pytest.mark.parametrize("text,expected", [
    ("today", "2026-10-25"), ("tomorrow", "2026-10-26"), ("yesterday", "2026-10-24"),
    ("the day after tomorrow", "2026-10-27"), ("friday", "2026-10-30"), ("next friday", "2026-10-30"),
    ("last friday", "2026-10-23"), ("sunday", "2026-10-25"), ("this sunday", "2026-10-25"), ("next sunday", "2026-11-01"),
    ("mon", "2026-10-26"), ("in 3 days", "2026-10-28"), ("in two weeks", "2026-11-08"), ("2 weeks ago", "2026-10-11"),
    ("oct 25", "2026-10-25"), ("25 october 2026", "2026-10-25"), ("October 3rd", "2026-10-03"), ("3rd of march", "2026-03-03"),
    ("13/10", "2026-10-13"), ("25.10.2026", "2026-10-25"), ("2026-02-28", "2026-02-28"), ("next month", "2026-11-25"),
    ("last year", "2025-10-25"), ("end of month", "2026-10-31"), ("end of year", "2026-12-31"), ("on tuesday", "2026-10-27"),
])
def test_dates_relative_to_a_fixed_now(text, expected):
    assert nlp.parse_date(text, SUNDAY) == expected


def test_dates_refuse_a_guess():
    with pytest.raises(nlp.ParseError, match="could be day/month or month/day"):
        nlp.parse_date("3/10/2026", SUNDAY)
    with pytest.raises(nlp.ParseError):
        nlp.parse_date("2026-02-30", SUNDAY)
    with pytest.raises(nlp.ParseError):
        nlp.parse_date("soonish", SUNDAY)
    assert nlp.parse_date("2024-01-31", SUNDAY) == "2024-01-31"
    assert nlp.parse_date("in 1 month", dt.date(2026, 1, 31)) == "2026-02-28"  # clamped, not rolled over


@pytest.mark.parametrize("text,expected", [
    ("3pm", "15:00"), ("15:30", "15:30"), ("3:05 pm", "15:05"), ("noon", "12:00"), ("midnight", "00:00"),
    ("9 o'clock", "09:00"), ("12am", "00:00"), ("12 pm", "12:00"), ("7.45am", "07:45"),
])
def test_times(text, expected):
    assert nlp.parse_time(text) == expected


def test_times_refuse():
    for bad in ["25:00", "7", "13pm", "half past"]:
        with pytest.raises(nlp.ParseError):
            nlp.parse_time(bad)


@pytest.mark.parametrize("text,seconds", [
    ("2 hours", 7200), ("30 min", 1800), ("1h30m", 5400), ("an hour", 3600), ("two days", 172800), ("90s", 90),
    ("1 hour 15 minutes", 4500), ("1 hour and a half", 5400), ("half an hour", 1800), ("a week", 604800),
])
def test_durations(text, seconds):
    assert nlp.parse_duration(text) == seconds


def test_durations_refuse():
    for bad in ["3 fortnights", "soon", "2", "hours"]:
        with pytest.raises(nlp.ParseError):
            nlp.parse_duration(bad)


def test_hosts_urls_emails():
    assert nlp.parse_host("https://Example.com/x") == "example.com"
    assert nlp.parse_host("api.github.com") == "api.github.com"
    assert nlp.parse_host("localhost:8700") == "localhost"
    assert nlp.parse_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert nlp.parse_email("Someone@Example.COM") == "someone@example.com"
    for fn, bad in [(nlp.parse_host, "example dot com"), (nlp.parse_host, "not a host"), (nlp.parse_url, "example.com"), (nlp.parse_email, "nope@")]:
        with pytest.raises(nlp.ParseError):
            fn(bad)


@pytest.mark.parametrize("word,base", [
    ("stories", "story"), ("news", "news"), ("boxes", "box"), ("items", "item"), ("status", "status"),
    ("cities", "city"), ("bus", "bus"), ("this", "this"), ("series", "series"), ("addresses", "address"),
    ("analysis", "analysis"), ("is", "is"), ("releases", "release"), ("up", "up"),
])
def test_singular_is_conservative(word, base):
    assert nlp.singular(word) == base


def test_forms_cover_singular_and_plural_only():
    assert nlp.forms("story") == {"story", "stories"}
    assert nlp.forms("stories") == {"story", "stories"}
    assert nlp.forms("news") == {"news"}
    assert nlp.forms("up") == {"up"}


def test_distance():
    assert nlp.distance("weather", "weather") == 0
    assert nlp.distance("wether", "weather") == 1
    assert nlp.distance("waether", "weather") == 1  # a transposition is one edit
    assert nlp.distance("wather", "weather") == 1
    assert nlp.distance("abc", "xyz") == 3
    assert nlp.distance("a", "abcdef", 1) == 2  # stops early past the limit
