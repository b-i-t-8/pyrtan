import pytest

from pyrtan.intent import Trigger, TriggerError, best_matches, normalise


def test_normalise():
    assert normalise("  What's the Weather in Oslo?!  ") == "What's the Weather in Oslo"
    assert normalise("a   b") == "a b"


def test_slots_and_types():
    t = Trigger("top {count:int} stories on hacker news")
    assert t.match("Top 5 stories on Hacker News") == {"count": 5}
    assert t.match("top five stories on hacker news") is None
    assert Trigger("is {host:word} up").match("is example.com up") == {"host": "example.com"}
    assert Trigger("is {host:word} up").match("is example dot com up") is None
    assert Trigger("set {x:float}").match("set 1.5") == {"x": 1.5}
    assert Trigger("find {q}").match("find a thing with spaces") == {"q": "a thing with spaces"}


def test_slot_keeps_case():
    assert Trigger("weather in {place}").match("Weather in New York") == {"place": "New York"}


def test_optional_and_alternatives():
    t = Trigger("[what's the] weather in {place}")
    assert t.match("weather in Oslo") == {"place": "Oslo"}
    assert t.match("What's the weather in Oslo") == {"place": "Oslo"}
    t = Trigger("how (hot|cold) is it in {place}")
    assert t.match("how cold is it in Oslo") == {"place": "Oslo"}
    assert t.match("how warm is it in Oslo") is None
    t = Trigger("how [hot|cold] is it in {place}")
    assert t.match("how is it in Oslo") == {"place": "Oslo"}


def test_fixed_inputs():
    t = Trigger("top story on hacker news", {"count": 1})
    assert t.match("top story on hacker news") == {"count": 1}


def test_bad_patterns():
    for bad in ["", "{x", "{x:money}", "[unbalanced", "unbalanced]", "{a} {a}", "{x}"]:
        with pytest.raises(TriggerError):
            Trigger(bad)


def test_specificity_picks_the_longer_literal():
    specific = Trigger("weather in {place}")
    general = Trigger("tell me {anything}")  # a bare "{anything}" is rejected: it would match everything
    matches = best_matches("tell me weather in Oslo", [("a", general), ("b", specific)])
    assert [m.tree for m in matches] == ["a"]  # only one matches at all
    matches = best_matches("weather in Oslo", [("a", Trigger("weather {place}")), ("b", specific)])
    assert [m.tree for m in matches] == ["b"]


def test_ties_are_reported_not_resolved():
    a, b = Trigger("ping {x}"), Trigger("ping {y}")
    matches = best_matches("ping me", [("a", a), ("b", b)])
    assert {m.tree for m in matches} == {"a", "b"}
