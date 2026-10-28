"""What pyrtan check says about a decision without running it (analysis.py),
held against what the runtime does with the same conditions (conditions.py)."""

import random

import pytest

from pyrtan import analysis, conditions
from pyrtan.analysis import NOTHING, NUMBERS, Interval, Values
from pyrtan.template import MISSING
from pyrtan.tree import check, load_tree_text

SAMPLES = [
    -1000, -1, -0.5, 0, 0.5, 1, 2, 2.5, 3, 4, 44, 45, 48, 49, 50, 99, 100, 200, 250, 299, 300, 404, 499, 500, 599, 600, 1e9,
    "0", "3", " 250 ", "250.0", "2e2", "abc", "", "open", "OPEN", True, False, None, [1], {"a": 1}, MISSING,
]
CONDITIONS = [
    {"eq": 0}, {"eq": 250}, {"eq": "250"}, {"eq": "abc"}, {"eq": True}, {"eq": None}, {"eq": [1]},
    {"in": [1, 2, 3]}, {"in": ["open", 404]}, {"in": [False, "0"]},
    {"lt": 3}, {"lte": 3}, {"gt": 99}, {"gte": 100}, {"lt": True}, {"gt": "abc"}, {"gte": "45"},
    {"between": [45, 48]}, {"between": [200, 299]}, {"between": [9, 1]}, {"between": ["1", 3]},
    {"gt": 5, "lt": 3}, {"gte": 0, "lte": 0}, {"gt": 0, "in": [1, 2, 300]},
]


def contains(values: Values, v) -> bool:
    if v is MISSING:
        return False
    n = analysis._num(v)
    if n is not None:
        return any((i.lo < n or (i.lo == n and i.lo_in)) and (n < i.hi or (n == i.hi and i.hi_in)) for i in values.nums)
    return bool(analysis._point(v).points & values.points)


@pytest.mark.parametrize("cond", CONDITIONS, ids=str)
def test_a_modelled_condition_holds_for_exactly_its_set(cond):
    s = analysis.exact(cond)
    assert s is not None
    for v in SAMPLES:
        assert contains(s, v) == conditions.holds(cond, v), (cond, v)


def test_the_set_arithmetic():
    a = Values((Interval(0, True, 10, True),))
    b = Values((Interval(5, False, 20, False),))
    assert (a & b).nums == (Interval(5, False, 10, True),)
    assert (a | b).nums == (Interval(0, True, 20, False),)
    assert (a - b).nums == (Interval(0, True, 5, True),)
    assert (NUMBERS - a).nums == (Interval(-analysis.INF, False, 0, False), Interval(10, False, analysis.INF, False))
    assert Values((Interval(1, True, 2, True),)).within(a) and not a.within(b) and NOTHING.within(NOTHING)
    # Touching at a point both leave out stays two; touching where one keeps it is one.
    assert len((Values((Interval(0, False, 3, False),)) | Values((Interval(3, False, 5, False),))).nums) == 2
    assert len((Values((Interval(0, False, 3, True),)) | Values((Interval(3, False, 5, False),))).nums) == 1
    p = Values(points=frozenset({("str", "a"), ("str", "b")}))
    assert (p - Values(points=frozenset({("str", "a")}))).points == {("str", "b")}


def test_findings_are_never_wrong_on_random_decisions():
    """Whatever the analysis calls dead must be dead for every sample value,
    whatever it does not model around it."""
    rng = random.Random(7)
    pool = CONDITIONS + [{"matches": "^a"}, {"exists": False}, {"type": "str"}, {"ne": 3}, {"contains": "b"}, {"gt": 2, "type": "int"}, {"eq": 250, "matches": "5"}]
    for _ in range(3000):
        conds = [rng.choice(pool) for _ in range(rng.randint(1, 6))]
        for f in analysis.findings(conds):
            for v in SAMPLES:
                if conditions.holds(conds[f.index], v):
                    assert f.kind == "shadowed", (conds, f, v)
                    assert any(conditions.holds(conds[j], v) for j in range(f.index)), (conds, f, v)


def test_holes_in_words():
    assert analysis.holes_in_words([{"eq": 0}, {"between": [1, 3]}, {"between": [45, 48]}]) == "whole numbers below 0, 4 to 44, above 48"
    assert analysis.holes_in_words([{"lt": 0}, {"gte": 0}]) is None  # every number is taken
    assert analysis.holes_in_words([{"between": [1, 3.5]}]) == "x < 1, x > 3.5"
    assert analysis.holes_in_words([{"eq": "open"}]) is None  # not about numbers
    assert analysis.holes_in_words([{"gt": 0}, {"matches": "x"}]) is None  # a branch not modelled: say nothing
    assert analysis.holes_in_words([{"gt": 0, "lt": 1}, {"gt": 1}]) == "whole numbers below 2"  # 0 and below, and 1 itself, said once
    many = [{"eq": n * 10} for n in range(10)]
    assert analysis.holes_in_words(many).endswith("and 5 more")


TREE = '''
name = "codes"
description = "status codes"
start = "status"

[inputs.code]
type = "int"

[nodes.status]
kind = "decide"
on = "inputs.code"
branches = [
  { when = { between = [200, 399] }, then = "up" },
  { when = { gte = 400 }, then = "down" },
  { when = { between = [500, 599] }, then = "down" },
  { when = { gt = 600, lt = 500 }, then = "down" },
]

[nodes.word]
kind = "lookup"
on = "inputs.code"
into = "w"
table = [ { eq = 0, value = "clear" }, { between = [1, 3], value = "cloudy" }, { in = [2, 3], value = "again" } ]
next = "up"

[nodes.up]
kind = "done"
say = "up"

[nodes.down]
kind = "done"
say = "down"
'''


def test_check_says_what_the_order_gives_away():
    notes = [str(p) for p in check(load_tree_text(TREE))]
    assert "warn: nodes.status.branches[2]: never taken: every value it holds for is taken first by branches[1]" in notes
    assert "warn: nodes.status.branches[3]: this condition holds for no value, so the branch is never taken" in notes
    assert "info: nodes.status: no 'else': an unforeseen value is recorded as a gap and fails the run; no branch takes whole numbers below 200" in notes
    assert "warn: nodes.word.table[2]: never read: every value it holds for is read first by table[1]" in notes
    assert any(n.startswith("info: nodes.word: no 'default'") and n.endswith("no row takes whole numbers below 0, above 3") for n in notes)
    # An else, or a default, catches the rest: nothing to say about holes; the dead branch is still dead.
    caught = [str(p) for p in check(load_tree_text(TREE.replace('{ when = { gt = 600, lt = 500 }, then = "down" },', '{ else = "down" },').replace('next = "up"', 'default = "?"\nnext = "up"')))]
    assert not any("takes whole numbers" in n for n in caught)
    assert any("branches[2]: never taken" in n for n in caught)


def test_the_examples_have_nothing_dead(capsys):
    from conftest import ROOT
    from pyrtan.cli import main

    assert main(["check", str(ROOT / "examples"), str(ROOT / "catalog"), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "never taken" not in out and "holds for no value" not in out and "never read" not in out
