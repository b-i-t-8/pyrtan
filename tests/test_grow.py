"""An unanswered sentence becomes a proposed tree, a scaffold that checks,
and an entry in the unanswered log."""

import pytest

from pyrtan.grow import Proposal, Unanswered, no_match_outcome, propose
from pyrtan.intent import Trigger
from pyrtan.tree import check, load_tree_text


@pytest.mark.parametrize(
    "text, name, trigger, inputs",
    [
        ("top 3 stories on hacker news", "top-stories-hacker-news", "top {count:int} stories on hacker news", {"count": 3}),
        ("is example.com up", "host-up", "is {host:host} up", {"host": "example.com"}),
        ("what's the latest release of astral-sh/uv", "latest-release", "what's the latest release of {target:word}", {"target": "astral-sh/uv"}),
        ("convert 12.5 km to miles", "convert-km-miles", "convert {value:float} km to miles", {"value": 12.5}),
        ("turn on the lights", "turn-lights", "turn on the lights", {}),
        ("Weather in Oslo?", "weather-oslo", "Weather in Oslo", {}),
        ("ping 1 then 2", "ping-then", "ping {count:int} then {count2:int}", {"count": 1, "count2": 2}),
    ],
)
def test_propose_names_slots_and_matches_the_sentence(text, name, trigger, inputs):
    p = propose(text)
    assert (p.name, p.trigger) == (name, trigger)
    assert Trigger(p.trigger).match(text) == inputs
    assert set(p.inputs) == set(inputs)


def test_propose_survives_trigger_syntax_and_bare_numbers():
    p = propose("{weird} [tokens] | here")
    Trigger(p.trigger)  # compiles
    assert "{" not in p.trigger.replace("{count", "")
    p = propose("42")
    assert p.trigger == "42" and p.inputs == {}
    assert propose("").name == "new-tree"


def test_given_name_wins():
    assert propose("my thing", name="custom").name == "custom"


@pytest.mark.parametrize("text", ["top 3 stories on hacker news", "is example.com up", "what's the latest release of astral-sh/uv", 'say "hi" to {everyone}', "turn on the lights"])
def test_scaffold_loads_and_checks_clean(text):
    p = propose(text)
    tree = load_tree_text(p.scaffold())
    assert tree.name == p.name
    assert [t.pattern for t in tree.triggers] == [p.trigger]
    assert [x for x in check(tree) if x.level == "error"] == []
    assert set(tree.inputs) == set(p.inputs)


def test_host_slot_opens_hosts_and_builds_the_url():
    tree = load_tree_text(propose("is example.com up").scaffold())
    assert tree.hosts == ["*"]
    assert "inputs.host" in tree.node("fetch").get("http")["url"]


def test_command_quotes_for_a_shell():
    assert propose("top 3 stories on hacker news").command() == 'pyrtan new top-stories-hacker-news --from "top 3 stories on hacker news"'
    assert propose("what's up").command("trees") == 'pyrtan new up --from "what\'s up" --dir trees'
    assert propose("costs $5").command().endswith("--from 'costs $5'")


def test_no_match_outcome_points_at_the_next_tree():
    out = no_match_outcome("tell me a joke", "Nothing here")
    assert out["say"].startswith("Nothing here answers 'tell me a joke'. To write the tree that would: pyrtan new joke --from")
    assert out["output"]["unanswered"] == "tell me a joke"
    assert out["output"]["proposal"]["name"] == "joke"


def test_unanswered_counts_and_bounds():
    log = Unanswered(limit=2)
    first = log.record("Tell me a joke")
    assert first["count"] == 1 and first["proposal"]["name"] == "joke"
    assert log.record("tell me a joke!")["count"] == 2  # same sentence, normalised
    log.record("second")
    log.record("third")
    texts = [e["text"] for e in log.to_list()]
    assert texts == ["third", "second"]  # the least recently seen went
    log = Unanswered()
    log.record("rare")
    log.record("common")
    log.record("common")
    assert [e["count"] for e in log.to_list()] == [2, 1]  # most seen first


def test_proposal_round_trips_through_its_dict():
    p = propose("is example.com up")
    d = p.to_dict()
    again = Proposal(d["text"], d["name"], d["trigger"], d["inputs"])
    assert again.scaffold() == p.scaffold()
