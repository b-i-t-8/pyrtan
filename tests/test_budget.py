"""A tree's declared budget: so many requests in any window, then "budget"."""

from __future__ import annotations

import datetime as dt
import json
import re

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
    assert svc.capabilities()["trees"][0]["budget"] == [{"requests": 2, "per": 60}]
    statuses = [svc.run({"tree": "hits"})["status"] for _ in range(3)]
    assert statuses == ["done", "done", "error"]


@pytest.mark.parametrize("budget, words", [
    ('{ requests = 0, per = "1m" }', "budget.requests is a whole number"),
    ('{ requests = 2.5, per = "1m" }', "budget.requests is a whole number"),
    ('{ requests = 2, per = "soon" }', "budget.per is a duration"),
    ('{ requests = 2 }', "'budget' is a table of 'requests' and 'per'"),
    ('{ requests = 2, per = "1m", scope = "x" }', "'budget' is a table of 'requests' and 'per'"),
    ('"10 a minute"', "'budget' is a table"),
])
def test_a_bad_budget_is_refused(budget, words):
    with pytest.raises(TreeError, match=words):
        load_tree_text(HITS.replace('{ requests = 2, per = "1m" }', budget))
    assert load_tree_text(HITS.replace('per = "1m"', "per = 90")).budget == [{"requests": 2, "per": 90, "said": "90 s", "host": None}]


def test_the_github_example_keeps_a_budget():
    from conftest import ROOT
    from pyrtan.tree import load_tree

    tree = load_tree(ROOT / "examples" / "github" / "github-release.toml")
    assert tree.budget == [{"requests": 30, "per": 3600, "said": "1 hour", "host": None}]


def shop(name: str, budget: str, url: str = "https://api.example/1") -> str:
    return f'''
name = "{name}"
description = "reads the shop"
hosts = ["api.example", "*.shop.example"]
budget = {budget}
start = "get"
[nodes.get]
kind = "observe"
http = {{ url = "{url}" }}
into = "r"
retries = 0
next = "done"
[nodes.done]
kind = "done"
say = "{{{{ state.r.status }}}}"
'''


def shops(*trees: str):
    reg = Registry()
    for t in trees:
        reg.add(load_tree_text(t))
    fixture = {**FIXTURE, "GET https://EU.shop.example/x": {"status": 200, "body": ""}}
    now = [T0]
    return Runner(reg, RecordedTransport(json.loads(json.dumps(fixture))), clock=lambda: now[0]), now


def test_a_host_s_budget_is_shared_by_every_tree_that_names_it():
    host = '{ host = "api.example", requests = 2, per = "1m" }'
    r, now = shops(shop("a", host), shop("b", host))
    assert [r.run_tree(t, {}).status for t in ("a", "b")] == ["done", "done"]
    out = r.run_tree("a", {})
    assert out.reason == "budget" and out.output["budget"] == {"requests": 2, "per": 60, "retry_after": 60, "host": "api.example"}
    assert out.say == "get: the budget of 2 requests per 1m for api.example, which every tree here naming it shares, is spent; the next is allowed in 60 s."
    assert r.run_tree("b", {}).reason == "budget"  # b's is the same count
    now[0] = T0 + dt.timedelta(seconds=61)
    assert r.run_tree("b", {}).status == "done"


def test_each_tree_holds_the_shared_count_to_its_own_number():
    r, now = shops(shop("strict", '{ host = "api.example", requests = 1, per = "1m" }'), shop("easy", '{ host = "api.example", requests = 3, per = "1 hour" }'))
    assert r.run_tree("easy", {}).status == "done"
    assert r.run_tree("strict", {}).reason == "budget"  # one has gone already
    assert r.run_tree("easy", {}).status == "done"
    now[0] = T0 + dt.timedelta(minutes=30)  # past strict's minute, inside easy's hour: the count keeps easy's window
    assert r.run_tree("easy", {}).status == "done"
    assert r.run_tree("easy", {}).reason == "budget"


def test_a_request_must_fit_every_budget_and_a_refused_one_counts_in_none():
    both = '[{ requests = 1, per = "1m" }, { host = "api.example", requests = 5, per = "1m" }]'
    r, _ = shops(shop("both", both), shop("other", '{ host = "api.example", requests = 5, per = "1m" }'))
    assert r.run_tree("both", {}).status == "done"
    assert r.run_tree("both", {}).output["budget"] == {"requests": 1, "per": 60, "retry_after": 60}  # its own, not the host's
    # The refused one was not counted against the host: four of its five are left.
    assert [r.run_tree("other", {}).status for _ in range(5)] == ["done"] * 4 + ["error"]


def test_a_host_budget_matches_by_name_and_glob_and_only_what_reaches_a_host():
    r, _ = shops(shop("glob", '{ host = "*.shop.example", requests = 1, per = "1m" }', url="https://EU.shop.example/x"))
    assert r.run_tree("glob", {}).status == "done" and r.run_tree("glob", {}).reason == "budget"
    r, _ = shops(shop("elsewhere", '{ host = "other.example", requests = 1, per = "1m" }'))
    assert [r.run_tree("elsewhere", {}).status for _ in range(3)] == ["done"] * 3  # api.example has no budget here


@pytest.mark.parametrize("budget, words", [
    ('{ host = "API.example", requests = 1, per = "1m" }', "budget.host is a host name"),
    ('{ host = "a b", requests = 1, per = "1m" }', "budget.host is a host name"),
    ('[{ requests = 1, per = "1m" }, { requests = 2, per = "1h" }]', "names a host (or the tree itself) twice"),
    ('[{ host = "api.example", requests = 1, per = "1m" }, { host = "api.example", requests = 2, per = "1h" }]', "twice"),
    ("[]", "a table, or a list of them"),
    ('[{ requests = 0, per = "1m" }]', "budget[0].requests is a whole number"),
])
def test_a_bad_host_budget_is_refused(budget, words):
    with pytest.raises(TreeError, match=re.escape(words)):
        load_tree_text(shop("bad", budget))


def raised(budget: str, env: dict[str, str]):
    reg = Registry()
    reg.add(load_tree_text(shop("gh", budget)))
    return Runner(reg, RecordedTransport(json.loads(json.dumps(FIXTURE))), env=env, clock=lambda: T0)


def test_a_credential_in_the_environment_raises_the_budget():
    budget = '{ requests = 1, per = "1 hour", raised_by = { SHOP_TOKEN = 3 } }'
    r = raised(budget, {"SHOP_TOKEN": "s3cret"})
    assert [r.run_tree("gh", {}).status for _ in range(3)] == ["done"] * 3
    out = r.run_tree("gh", {})
    assert out.reason == "budget"
    assert out.say == "get: gh has spent its budget of 3 requests per 1 hour (raised by SHOP_TOKEN); the next is allowed in 3600 s."
    assert out.output == {"budget": {"requests": 3, "per": 3600, "retry_after": 3600, "raised_by": "SHOP_TOKEN"}}
    assert "s3cret" not in out.say + json.dumps(out.output)  # the name, never the value


@pytest.mark.parametrize("env", [{}, {"SHOP_TOKEN": ""}, {"OTHER": "x"}])
def test_without_the_credential_the_budget_is_the_plain_one(env):
    r = raised('{ requests = 1, per = "1 hour", raised_by = { SHOP_TOKEN = 3 } }', env)
    assert r.run_tree("gh", {}).status == "done"
    out = r.run_tree("gh", {})
    assert out.reason == "budget" and out.output["budget"] == {"requests": 1, "per": 3600, "retry_after": 3600}
    assert "raised" not in out.say


def test_of_several_credentials_present_the_largest_counts():
    budget = '{ requests = 1, per = "1m", raised_by = { A_TOKEN = 2, B_TOKEN = 4, C_TOKEN = 9 } }'
    r = raised(budget, {"A_TOKEN": "a", "B_TOKEN": "b"})
    assert [r.run_tree("gh", {}).status for _ in range(5)] == ["done"] * 4 + ["error"]
    assert r.run_tree("gh", {}).output["budget"]["raised_by"] == "B_TOKEN"


def test_a_raised_host_budget_and_the_capability():
    budget = '{ host = "api.example", requests = 1, per = "1m", raised_by = { SHOP_TOKEN = 2 } }'
    r = raised(budget, {"SHOP_TOKEN": "t"})
    assert [r.run_tree("gh", {}).status for _ in range(3)] == ["done", "done", "error"]
    tree = load_tree_text(shop("gh", budget))
    assert tree.budget == [{"requests": 1, "per": 60, "said": "1m", "host": "api.example", "raised_by": {"SHOP_TOKEN": 2}}]
    assert tree.capability()["budget"] == [{"requests": 1, "per": 60, "host": "api.example", "raised_by": {"SHOP_TOKEN": 2}}]


@pytest.mark.parametrize("raised_by, words", [
    ('{}', "budget.raised_by is a table of env names"),
    ('"GITHUB_TOKEN"', "budget.raised_by is a table of env names"),
    ('{ "not-a-name" = 5 }', "not an env name"),
    ('{ SHOP_TOKEN = 1 }', "budget.raised_by.SHOP_TOKEN is a whole number above requests (1)"),
    ('{ SHOP_TOKEN = 2.5 }', "budget.raised_by.SHOP_TOKEN is a whole number above requests"),
    ('{ SHOP_TOKEN = true }', "budget.raised_by.SHOP_TOKEN is a whole number above requests"),
])
def test_a_bad_raised_by_is_refused(raised_by, words):
    with pytest.raises(TreeError, match=re.escape(words)):
        load_tree_text(shop("bad", f'{{ requests = 1, per = "1m", raised_by = {raised_by} }}'))
