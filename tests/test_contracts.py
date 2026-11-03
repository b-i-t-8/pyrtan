"""What a tree returns, and the trees that rely on it: ``returns``."""

from __future__ import annotations

import json

import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.cli import main
from pyrtan.contracts import broken, check_callers, ending, parse_returns, reads
from pyrtan.tree import TreeError, check, load_tree_text

ITEM = '''
name = "item"
description = "one item"
hosts = ["api.example"]
returns = { title = "str", score = "int", url = "str?" }
start = "get"

[inputs.id]
type = "int"
default = 1

[nodes.get]
kind = "observe"
http = { url = "https://api.example/{{ inputs.id }}" }
into = "r"
extract = [{ into = "title", json = "json.title" }, { into = "score", json = "json.score" }]
next = "done"

[nodes.done]
kind = "done"
say = "{{ state.title }}"
output = { title = "{{ state.title }}", score = "{{ state.score }}" }
'''

CALLER = '''
name = "caller"
description = "reads one item, and walks a few"
start = "one"

[nodes.one]
kind = "delegate"
tree = "item"
with = { id = 1 }
into = "first"
next = "many"

[nodes.many]
kind = "each"
over = "inputs.ids"
tree = "item"
with = { id = "{{ item }}" }
into = "items"
next = "high"

[inputs.ids]
type = "list"
default = [1, 2]

[nodes.high]
kind = "decide"
on = "first.output.score"
branches = [{ when = { gt = 10 }, then = "done" }, { else = "done" }]

[nodes.done]
kind = "done"
say = "{{ state.first.output.title }} and {{ state.items[1].title }}"
'''

FIXTURE = {
    "GET https://api.example/1": {"status": 200, "body": {"title": "One", "score": 12}},
    "GET https://api.example/2": {"status": 200, "body": {"title": "Two", "score": "lots"}},
}


@pytest.mark.parametrize("raw, words", [
    ({}, "a table of field = type"),
    ([], "a table of field = type"),
    ({"x": "string"}, "one of str, int"),
    ({"x": "str??"}, "one of str, int"),
    ({"x": "?str"}, "one of str, int"),
    ({"a b": "str"}, "a field is letters"),
])
def test_returns_is_checked(raw, words):
    with pytest.raises(Exception, match=words):
        parse_returns("t", raw)
    with pytest.raises(TreeError, match=words):
        load_tree_text(ITEM.replace('returns = { title = "str", score = "int", url = "str?" }', "returns = " + json.dumps(raw).replace(":", " =") if isinstance(raw, dict) else "returns = []"))


def test_types():
    returns = parse_returns("t", {"s": "str", "i": "int", "f": "float", "n": "number", "b": "bool", "l": "list", "d": "dict", "a": "any", "o": "str?"})
    good = {"s": "x", "i": 3, "f": 2.5, "n": 4, "b": False, "l": [], "d": {}, "a": 0}
    assert broken(returns, good) is None
    assert broken(returns, {**good, "i": True}) == "i is a bool True, where it promises int"
    assert broken(returns, {**good, "s": ""}) == "s (str) is empty"  # an empty string is empty
    assert broken(returns, {**good, "o": None}) is None  # it may be
    assert broken(returns, {**good, "i": 3.0}) is None  # a whole float is a whole number
    assert broken(returns, None) == "s (str) is empty"


def test_a_tree_is_held_to_its_own_promise():
    assert not [p for p in check(load_tree_text(ITEM)) if p.level == "error"]
    with pytest.raises(TreeError, match="nodes.done: the tree returns score, which this done's output does not give"):
        load_tree_text(ITEM.replace(', score = "{{ state.score }}"', ""))
    with pytest.raises(TreeError, match="this done has no output table"):
        load_tree_text(ITEM.replace('output = { title = "{{ state.title }}", score = "{{ state.score }}" }\n', ""))
    # The url may be empty, so it need not be given at all.
    assert load_tree_text(ITEM).returns["url"] == ("str", True)


def test_an_each_sub_flow_is_not_the_tree_s_ending():
    walker = load_tree_text('''
name = "walker"
description = "walks"
returns = { n = "int" }
start = "walk"
[inputs.xs]
type = "list"
default = [1]
[nodes.walk]
kind = "each"
over = "inputs.xs"
start = "one"
into = "got"
next = "done"
[nodes.one]
kind = "done"
say = "{{ item }}"
[nodes.done]
kind = "done"
say = "walked"
output = { n = "{{ state.got | length }}" }
''')
    assert [n.id for n in ending(walker)] == ["done"]
    reg = Registry()
    reg.add(walker)
    out = Runner(reg).run_tree("walker", {"xs": [1, 2, 3]})
    assert out.status == "done" and out.output == {"n": 3}


def test_callers_are_held_to_what_the_callee_returns():
    item, caller = load_tree_text(ITEM), load_tree_text(CALLER)
    assert sorted((n, c, f) for n, c, f, _ in reads(caller)) == [("done", "item", "title"), ("done", "item", "title"), ("high", "item", "score")]
    assert check_callers({"item": item, "caller": caller}) == {}
    wrong = load_tree_text(CALLER.replace("state.items[1].title", "state.items[1].author"))
    assert check_callers({"item": item, "caller": wrong}) == {"caller": [("error", "nodes.done", "reads state.items[1].author, but item does not return 'author'; it returns title, score, url")]}
    # A callee that promises nothing: said once; one that is not here: nothing to hold it to.
    loose = load_tree_text(ITEM.replace('returns = { title = "str", score = "int", url = "str?" }\n', ""))
    said = check_callers({"item": loose, "caller": caller})["caller"]
    assert len(said) == 1 and said[0][0] == "info" and "item promises nothing about its output" in said[0][2]
    assert check_callers({"caller": wrong}) == {}


def test_a_run_that_breaks_the_promise_stops_as_contract():
    reg = Registry()
    reg.add(load_tree_text(ITEM))
    r = Runner(reg, RecordedTransport(json.loads(json.dumps(FIXTURE))))
    assert r.run_tree("item", {"id": 1}).output == {"title": "One", "score": 12}
    out = r.run_tree("item", {"id": 2})
    assert out.status == "error" and out.reason == "contract"
    assert out.say == "done: item promises what it returns, and score is a str 'lots', where it promises int."
    assert out.trace.steps[-1].summary == "contract: score is a str 'lots', where it promises int"
    # Its caller hears it as a delegated tree that did not end done.
    reg.add(load_tree_text(CALLER))
    walked = r.run_tree("caller", {})
    assert walked.status == "done" and walked.say == "One and "  # the second item came back empty, not as a bad score


def test_the_capability_and_the_command(tmp_path, capsys):
    assert load_tree_text(ITEM).capability()["returns"] == {"title": "str", "score": "int", "url": "str?"}
    (tmp_path / "item.toml").write_text(ITEM)
    (tmp_path / "caller.toml").write_text(CALLER.replace("state.items[1].title", "state.items[1].author"))
    assert main(["check", str(tmp_path), "--quiet"]) == 1
    out = capsys.readouterr().out
    assert "caller: 4 nodes" in out and "error: nodes.done: reads state.items[1].author, but item does not return 'author'" in out
