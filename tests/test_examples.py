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


def test_small_talk_is_one_decide_on_what_the_trigger_fixed(runner):
    for sentence, topic, starts in [("how are you doing?", "how", "Deterministic"), ("Are you human", "who", "A bot"), ("what can you do", "what", "Only what"), ("see ya", "bye", "Bye")]:
        out = runner.run_intent(sentence)
        assert out.status == "done" and out.tree == "small-talk" and out.output == {"topic": topic} and out.say.startswith(starts), sentence
        assert [s.node for s in out.trace.steps if s.kind != "intent"] == ["topic", topic]
    # A greeting alone is filler: it reads as nothing, and no tree answers it.
    assert runner.run_intent("hello").reason == "no-match"


def test_weather_two_hops(runner):
    out = runner.run_intent("weather in Oslo")
    assert out.status == "done"
    assert out.say == "Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h."
    assert [s.chosen for s in out.trace.steps if s.kind == "decide"] == ["exists True", "type 'number'"]
    sky = next(s for s in out.trace.steps if s.kind == "lookup")
    assert (sky.node, sky.chosen, sky.observed, sky.next) == ("sky", "between [51, 67]", 61, "reply")


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


def test_disk_space_over_a_shell(runner):
    out = runner.run_intent("how full is the disk")
    assert out.status == "done" and out.say == "/ is 71% full." and out.output == {"mounted": "/", "percent": 71}
    assert out.trace.steps[1].summary == "shell df -P / -> exit 0; percent=71, mounted=/"
    assert runner.run_intent("disk space on /data").reason == "disk-full"
    failed = runner.run_intent("disk space on /nowhere")
    assert failed.status == "failed" and failed.reason == "df-failed" and "No such file" in failed.say


def test_log_errors_from_a_file(runner):
    out = runner.run_intent("errors in the log")
    assert out.status == "done" and out.say.startswith("2 error line(s) in the last 5 of /var/log/syslog; latest: ") and out.output["errors"] == 2
    assert runner.run_intent("errors in /var/log/quiet.log").say == "No error lines in the last 1 of /var/log/quiet.log."
    assert runner.run_intent("errors in /var/log/nothing.log").reason == "no-such-file"
    # The path from the sentence is checked against the tree's paths before anything is read.
    out = runner.run_intent("errors in /etc/passwd")
    assert out.status == "error" and out.reason == "path-not-allowed" and [s.kind for s in out.trace.steps] == ["intent"]


def test_room_temperature_from_a_queue(runner):
    assert runner.run_intent("temperature in the kitchen").say == "kitchen: 21.5°C."
    assert runner.run_intent("how warm is the attic").say == "attic: 31°C, warm."
    assert runner.run_intent("temperature in the garage").reason == "no-reading"
    gap = runner.run_intent("temperature in the hall")  # a payload that is not JSON: a gap, not a guess
    assert gap.status == "error" and gap.reason == "gap" and gap.trace.gaps[0]["node"] == "reading"
    assert runner.run_tree("room-temperature", {"room": "a-b"}).status == "error"  # fixture has nothing for it


def test_rendered_headline_through_a_browser(runner):
    out = runner.run_intent("headline of https://app.example/dashboard")
    assert out.status == "done" and out.say == "Dashboard — Welcome back, Barry" and out.output["heading"] == "Welcome back, Barry"
    assert out.trace.steps[1].summary == "browser https://app.example/dashboard -> 200; heading=Welcome back, Barry"
    assert runner.run_intent("what does https://app.example/empty say").reason == "no-heading"
    assert runner.run_intent("headline of https://app.example/gone").reason == "unexpected-response"


def test_modem_check_over_serial(runner):
    assert runner.run_intent("is the modem up").say == "The modem on /dev/ttyUSB0 is there."
    silent = runner.run_intent("is the modem on /dev/ttyUSB1 up")
    assert silent.status == "error" and silent.reason == "transport" and "no answer within 3 s" in silent.say
    assert runner.run_intent("is the modem on /dev/ttyACM0 up").reason == "modem-error"
    assert runner.run_intent("is the modem on /dev/ttyS0 up").reason == "device-not-allowed"


def test_capabilities_name_the_sources_and_lists(examples):
    caps = {t["name"]: t for t in examples.capabilities()}
    assert caps["disk-space"]["sources"] == ["shell"] and caps["disk-space"]["commands"] == ["df"] and "paths" not in caps["disk-space"]
    assert caps["room-temperature"]["sources"] == ["queue"] and caps["room-temperature"]["topics"] == ["home/+/temperature"]
    assert caps["weather"]["sources"] == ["http"] and caps["modem-check"]["devices"] == ["/dev/ttyUSB*", "/dev/ttyACM*"]
