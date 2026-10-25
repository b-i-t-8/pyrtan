import pytest

from pyrtan.intent import Trigger, TriggerError, best_matches, normalise


def test_normalise():
    assert normalise("  What's the Weather in Oslo?!  ") == "What's the Weather in Oslo"
    assert normalise("a   b") == "a b"


def test_slots_and_types():
    t = Trigger("top {count:int} stories on hacker news")
    assert t.match("Top 5 stories on Hacker News") == {"count": 5}
    assert t.match("top five stories on hacker news") == {"count": 5}  # number words are read
    assert t.match("top many stories on hacker news") is None
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


# ---------------------------------------------------------------- reading

import datetime as dt

from pyrtan.intent import Language, read


def test_patterns_and_sentences_get_the_same_rewrites():
    t = Trigger("[what's the] weather in {place}")
    assert t.match("What is the weather in Oslo") == {"place": "Oslo"}
    assert t.match("Hey Pyrtan, what's the weather in Oslo, please?") == {"place": "Oslo"}
    assert t.match("could you tell me the weather in Oslo") is None  # "tell me" is not filler


def test_literal_words_match_their_plural_or_singular():
    t = Trigger("top {count:int} stories on hacker news")
    assert t.match("top 1 story on hacker news") == {"count": 1}
    assert Trigger("list the item").match("list the items") == {}
    assert Trigger("latest news").match("latest new") is None  # "news" is not a plural


def test_synonyms_from_the_language_table():
    lang = Language.from_raw({"synonyms": {"weather": ["forecast", "temperature"], "stories": "posts"}})
    t = Trigger("weather in {place}", language=lang)
    assert t.match("forecast in Oslo") == {"place": "Oslo"}
    assert t.match("temperature in Oslo") == {"place": "Oslo"}
    assert t.match("climate in Oslo") is None
    assert Trigger("top {n:int} stories", language=lang).match("top 3 posts") == {"n": 3}
    assert Trigger("hacker news", language=Language.from_raw({"synonyms": {"hacker": ["hn", "y combinator"]}})).match("y combinator news") == {}
    for bad in [{"synonyms": "weather"}, {"synonyms": {"weather": [1]}}, {"typos": "yes"}, {"other": 1}, {"synonyms": {"two words": ["x"]}}]:
        with pytest.raises(TriggerError):
            Language.from_raw(bad)


def test_typos_are_opt_in_and_only_when_one_word_is_close():
    strict = Trigger("weather in {place}")
    loose = Trigger("weather in {place}", language=Language(typos=True))
    assert strict.match("wether in Oslo") is None
    got = loose.match_detail("wether in Oslo")
    assert got.inputs == {"place": "Oslo"} and got.notes == ["typo: wether->weather"]
    assert loose.match("weathr in Oslo") == {"place": "Oslo"}
    assert loose.match("wather in Oslo") == {"place": "Oslo"}
    assert loose.match("water in Oslo") is None  # two edits away: not corrected
    assert loose.match("in in Oslo") is None  # short words are never corrected
    both = Trigger("(hotel|model) in {place}", language=Language(typos=True))
    assert both.match("hodel in Oslo") is None  # one edit from both: a guess, so refused
    assert both.match("hostel in Oslo") == {"place": "Oslo"}  # one edit from hotel only


def test_typed_slots_read_values():
    sunday = dt.date(2026, 10, 25)
    t = Trigger("remind me {when:date} at {at:time}")
    assert t.match("remind me tomorrow at 3pm", now=sunday) == {"when": "2026-10-26", "at": "15:00"}
    assert t.match("remind me next friday at 15:30", now=sunday) == {"when": "2026-10-30", "at": "15:30"}
    got = t.match_detail("remind me someday at 3pm", now=sunday)
    assert got.rejected and got.notes == ["slot when: 'someday' is not a date this reads"]
    assert Trigger("wait {for:duration}").match("wait an hour and a half") == {"for": 5400}
    assert Trigger("is {site:host} up").match("is https://Example.com/ up") == {"site": "example.com"}
    assert Trigger("is {site:host} up").match("is example dot com up") is None
    assert Trigger("open {u:url}").match("open https://example.com/a") == {"u": "https://example.com/a"}
    assert Trigger("mail {to:email}").match("mail Someone@Example.com") == {"to": "someone@example.com"}
    assert Trigger("top {n:number} stories").match("top twenty-one stories") == {"n": 21}
    with pytest.raises(TriggerError):
        Trigger("{x:money}")


def test_read_reports_the_rewrites_and_every_verdict():
    sunday = dt.date(2026, 10, 25)
    candidates = [("w", Trigger("[what's the] weather in {place}")), ("r", Trigger("remind me {when:date}")), ("g", Trigger("hi {name}"))]
    r = read("Hey, what's the weather in Oslo please", candidates, sunday)
    assert r.cleaned == "what is the weather in Oslo"
    assert r.steps == ["fillers: 'Hey', 'please'", "contractions: what's->what is"]
    assert r.winner.tree == "w" and r.winner.inputs == {"place": "Oslo"}
    step = r.step()
    assert step["kind"] == "intent" and "w via" in step["summary"] and step["observed"]["inputs"] == {"place": "Oslo"}
    r = read("remind me someday", candidates, sunday)
    assert r.matches == [] and [m.rejected for m in r.considered] == [True]
    assert "no trigger matched" in r.step()["summary"]
    # "hi" is kept because a trigger starts with it.
    r = read("hi Bob", candidates, sunday)
    assert r.winner.tree == "g" and r.winner.inputs == {"name": "Bob"}
    r = read("hi", [("w", candidates[0][1])], sunday)
    assert r.cleaned == "" and r.matches == []
