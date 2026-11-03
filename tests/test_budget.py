"""A tree's declared budget: so many requests in any window, then "budget"."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.service import Service
from pyrtan.tree import TreeError, load_tree_text

HITS = '''
name = "hits"
description = "reads a counter"
hosts = ["api.example"]
budget = { requests = 2, per = "1m" }
start = "get"

[inputs.ids]
type = "list"
default = [1]

[nodes.get]
kind = "each"
over = "inputs.ids"
start = "one"
into = "got"
next = "done"

[nodes.one]
kind = "observe"
http = { url = "https://api.example/{{ item }}" }
into = "r"
next = "said"

[nodes.said]
kind = "done"
say = "{{ state.r.status }}"

[nodes.done]
kind = "done"
say = "{{ state.got | length }} read"
'''

FIXTURE = {f"GET https://api.example/{i}": {"status": 200, "body": str(i)} for i in range(1, 5)}
T0 = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)


def runner(text: str = HITS, fixture=FIXTURE):
    reg = Registry()
    reg.add(load_tree_text(text))
    now = [T0]
    transport = RecordedTransport(json.loads(json.dumps(fixture)))
    return Runner(reg, transport, clock=lambda: now[0]), now, transport


def test_the_request_past_the_budget_is_not_sent():
    r, now, t = runner()
    assert r.run_tree("hits", {}).status == "done"
    assert r.run_tree("hits", {}).status == "done"
    out = r.run_tree("hits", {})
    assert out.status == "error" and out.reason == "budget"
    assert out.say == "one: hits has spent its budget of 2 requests per 1m; the next is allowed in 60 s."
    assert out.output == {"budget": {"requests": 2, "per": 60, "retry_after": 60}}
    assert len(t.calls) == 2  # the third never went
    assert out.trace.steps[-1].summary == "GET https://api.example/1: not sent; the budget of 2 requests per 1m is spent, the next in 60 s"


def test_the_window_slides():
    r, now, _ = runner()
    r.run_tree("hits", {})
    now[0] = T0 + dt.timedelta(seconds=40)
    r.run_tree("hits", {})
    now[0] = T0 + dt.timedelta(seconds=45)
    assert r.run_tree("hits", {}).output["budget"]["retry_after"] == 15  # the first went 45 s ago
    now[0] = T0 + dt.timedelta(seconds=61)
    assert r.run_tree("hits", {}).status == "done"  # the first has left the window; the second is still in it
    assert r.run_tree("hits", {}).reason == "budget"


def test_an_each_stops_at_the_first_item_refused():
    r, _, t = runner()
    out = r.run_tree("hits", {"ids": [1, 2, 3, 4]})
    assert out.reason == "budget" and len(t.calls) == 2
    sent = [s.summary for s in out.trace.steps if s.node == "one"]
    assert len(sent) == 3 and "not sent" in sent[-1]  # two went, the third was refused, the fourth never tried


def test_a_tree_without_a_budget_and_memory_are_not_counted(tmp_path):
    r, _, t = runner(HITS.replace('budget = { requests = 2, per = "1m" }\n', ""))
    for _ in range(5):
        assert r.run_tree("hits", {}).status == "done"
    from pyrtan.adapters import LocalAdapters
    from pyrtan.memory import Memory

    keeper = load_tree_text('''
name = "keeper"
description = "counts in memory"
keys = ["n"]
budget = { requests = 1, per = "1 hour" }
start = "bump"
[nodes.bump]
kind = "act"
memory = { key = "n", add = 1 }
into = "n"
next = "done"
[nodes.done]
kind = "done"
say = "{{ state.n.value }}"
''')
    reg = Registry()
    reg.add(keeper)
    mem = Runner(reg, adapters=LocalAdapters(memory=Memory(tmp_path / "m.json")))
    assert [mem.run_tree("keeper", {}).say for _ in range(3)] == ["1", "2", "3"]


def test_a_service_keeps_the_budget_across_requests(tmp_path):
    d = tmp_path / "trees"
    d.mkdir()
    (d / "hits.toml").write_text(HITS)
    svc = Service(Registry.from_paths([d]), name="s", transport=RecordedTransport(json.loads(json.dumps(FIXTURE))))
    assert svc.capabilities()["trees"][0]["budget"] == {"requests": 2, "per": 60}
    statuses = [svc.run({"tree": "hits"})["status"] for _ in range(3)]
    assert statuses == ["done", "done", "error"]


@pytest.mark.parametrize("budget, words", [
    ('{ requests = 0, per = "1m" }', "budget.requests is a whole number"),
    ('{ requests = 2.5, per = "1m" }', "budget.requests is a whole number"),
    ('{ requests = 2, per = "soon" }', "budget.per is a duration"),
    ('{ requests = 2 }', "'budget' is a table of 'requests' and 'per'"),
    ('{ requests = 2, per = "1m", host = "x" }', "'budget' is a table of 'requests' and 'per'"),
    ('"10 a minute"', "'budget' is a table"),
])
def test_a_bad_budget_is_refused(budget, words):
    with pytest.raises(TreeError, match=words):
        load_tree_text(HITS.replace('{ requests = 2, per = "1m" }', budget))
    assert load_tree_text(HITS.replace('per = "1m"', "per = 90")).budget == {"requests": 2, "per": 90, "said": "90 s"}


def test_the_github_example_keeps_a_budget():
    from conftest import ROOT
    from pyrtan.tree import load_tree

    tree = load_tree(ROOT / "examples" / "github" / "github-release.toml")
    assert tree.budget == {"requests": 30, "per": 3600, "said": "1 hour"}
