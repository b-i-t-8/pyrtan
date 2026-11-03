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


# The other half: what a caller hands a tree, held to that tree's inputs.

CALLEE = '''
name = "fetch-one"
description = "one thing by id"
start = "done"

[inputs.id]
type = "int"

[inputs.kind]
type = "str"
one_of = ["story", "comment"]
default = "story"

[inputs.count]
type = "int"
min = 1
max = 5
required = false

[nodes.done]
kind = "done"
say = "{{ inputs.id }}"
'''


def caller(with_: str, kind: str = "delegate", extra: str = "") -> str:
    over = 'over = "inputs.ids"\nlimit = 3\n' if kind == "each" else ""
    return f'''
name = "caller"
description = "hands fetch-one its inputs"
start = "get"
{extra}
[inputs.ids]
type = "list"
default = [1]

[inputs.flag]
type = "bool"
default = false

[inputs.ratio]
type = "float"
default = 0.5

[nodes.get]
kind = "{kind}"
{over}tree = "fetch-one"
with = {with_}
into = "got"
next = "done"

[nodes.done]
kind = "done"
say = "ok"
'''


def handed(with_: str, kind: str = "delegate") -> list[tuple[str, str, str]]:
    from pyrtan.contracts import check_inputs

    trees = {t.name: t for t in (load_tree_text(CALLEE), load_tree_text(caller(with_, kind)))}
    return check_inputs(trees).get("caller", [])


def test_a_caller_is_held_to_the_callee_s_inputs():
    assert handed('{ id = "{{ inputs.ids }}" }') == [("warn", "nodes.get.with", "hands fetch-one id from inputs.ids, a list, where it takes a int")]
    assert handed('{ id = 7 }') == []
    assert handed('{ id = "{{ item }}" }', kind="each") == []  # an each over the tree, its item handed on
    assert handed('{ id = 7, page = 2 }') == [("error", "nodes.get.with", "hands fetch-one 'page', which it does not take; it takes id, kind, count")]
    assert handed('{ kind = "story" }') == [("error", "nodes.get.with", "does not hand fetch-one 'id', which it requires")]
    assert handed('{ id = "seven" }') == [("error", "nodes.get.with", "hands fetch-one id = 'seven', which it refuses: input 'id' must be int, got 'seven'")]
    assert handed('{ id = 7, count = 9 }') == [("error", "nodes.get.with", "hands fetch-one count = 9, which it refuses: input 'count' must be at most 5, got 9")]
    assert handed('{ id = 7, kind = "poll" }') == [("error", "nodes.get.with", "hands fetch-one kind = 'poll', which it refuses: input 'kind' must be one of ['story', 'comment'], got 'poll'")]
    # A caller's own input handed whole, of a type the callee's can never take; a str is not judged.
    assert handed('{ id = 7, count = "{{ inputs.flag }}" }')[0][2] == "hands fetch-one count from inputs.flag, a bool, where it takes a int"
    assert handed('{ id = 7, count = "{{ inputs.ratio }}" }')[0][2] == "hands fetch-one count from inputs.ratio, a float, where it takes a int"
    assert handed('{ id = "{{ inputs.flag | int }}" }') == []  # through a filter: not judged
    # A tree the swarm holds (not among those checked) is left alone.
    from pyrtan.contracts import check_inputs

    assert check_inputs({"caller": load_tree_text(caller('{ page = 2 }'))}) == {}


@pytest.mark.parametrize("given, refused", [
    ({"id": 7}, False),
    ({"id": 7, "kind": "comment", "count": 5}, False),
    ({"id": 7, "page": 2}, True),
    ({"kind": "story"}, True),
    ({"id": "seven"}, True),
    ({"id": 7, "count": 9}, True),
    ({"id": 7, "kind": "poll"}, True),
])
def test_what_check_refuses_the_run_refuses_too(given, refused):
    # Every error check gives is a bad-input the callee's own run would stop with.
    import tomllib

    reg = Registry()
    reg.add(load_tree_text(CALLEE))
    with_ = "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in given.items()) + " }"
    assert tomllib.loads(f"w = {with_}")["w"] == given
    errors = [h for h in handed(with_) if h[0] == "error"]
    out = Runner(reg, RecordedTransport({})).run_tree("fetch-one", given)
    assert bool(errors) == refused == (out.reason == "bad-input"), (given, errors, out.reason)


def test_the_command_holds_callers_to_inputs(tmp_path, capsys):
    (tmp_path / "fetch-one.toml").write_text(CALLEE)
    (tmp_path / "caller.toml").write_text(caller('{ kind = "story" }'))
    assert main(["check", str(tmp_path)]) == 1
    assert "error: nodes.get.with: does not hand fetch-one 'id', which it requires" in capsys.readouterr().out
    (tmp_path / "caller.toml").write_text(caller('{ id = 7 }'))
    assert main(["check", str(tmp_path)]) == 0
