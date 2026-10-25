"""The example trees, end to end, against recorded responses."""

import pytest

from pyrtan.tree import check


def test_examples_check_clean(examples):
    for tree in examples.trees():
        errors = [p for p in check(tree) if p.level == "error"]
        assert not errors, (tree.name, errors)


def test_top_story(runner):
    out = runner.run_intent("What's the top story on Hacker News?")
    assert out.status == "done"
    assert out.say == "Top of Hacker News: Pyrtan 0.1 — https://example.org/p"
    assert out.output["stories"][0]["by"] == "alice"


def test_top_n_stories_fills_in_self_post_url(runner):
    out = runner.run_intent("top 3 stories on hacker news")
    assert out.status == "done"
    stories = out.output["stories"]
    assert stories[1]["url"] == "https://news.ycombinator.com/item?id=102"
    assert stories[2] is None  # a comment is not a story: that item failed, the run did not
    assert any(s.node == "not-a-story" for s in out.trace.steps)


def test_count_is_bounded(runner):
    out = runner.run_intent("top 99 stories on hacker news")
    assert out.status == "error" and out.reason == "bad-input"


def test_item_missing(runner):
    out = runner.run_intent("hacker news item 999")
    assert out.status == "failed" and out.reason == "no-such-item"


def test_weather_two_hops(runner):
    out = runner.run_intent("weather in Oslo")
    assert out.status == "done"
    assert out.say == "Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h."
    assert [s.chosen for s in out.trace.steps if s.kind == "decide"] == ["exists True", "type 'number'", "between [51, 67]"]


def test_weather_units_input(runner):
    out = runner.run_tree("weather", {"place": "Oslo", "units": "fahrenheit"})
    assert out.status == "done" and "39.6°F, clear" in out.say
    out = runner.run_tree("weather", {"place": "Oslo", "units": "kelvin"})
    assert out.status == "error" and out.reason == "bad-input"


def test_weather_unknown_place(runner):
    out = runner.run_intent("how cold is it in Nowhereville")
    assert out.status == "failed" and out.reason == "unknown-place"


def test_site_status_branches(runner):
    assert runner.run_intent("is Example.com up").status == "done"
    down = runner.run_intent("is broken.example up")
    assert down.status == "failed" and down.reason == "server-error"
    odd = runner.run_intent("check status of teapot.example")
    assert odd.status == "done" and "refuses" in odd.say


def test_site_status_rejects_bad_host(runner):
    # The {host:host} slot refuses what is not a host name, so the trigger
    # does not fire at all, and the reading says which trigger refused.
    out = runner.run_intent("is http://x up")
    assert out.status == "error" and out.reason == "no-match"
    assert out.output["reading"]["considered"] == ["is {host:host} up"]
    out = runner.run_tree("site-status", {"host": "http://x"})
    assert out.status == "error" and out.reason == "bad-input"  # the input's own pattern still guards a direct run


def test_github_release(runner):
    ok = runner.run_intent("latest release of b-i-t-8/pyrtan")
    assert ok.status == "done" and ok.output["tag"] == "v0.1.0"
    none = runner.run_intent("latest b-i-t-8/nothing release")
    assert none.status == "failed" and none.reason == "no-release"
    busy = runner.run_intent("latest release of b-i-t-8/busy")
    assert busy.reason == "rate-limited"


def test_fixture_fails_loudly_on_unforeseen_request(runner):
    out = runner.run_intent("weather in Paris")
    assert out.status == "error" and out.reason == "transport"
