"""The list and number filters: fixed, with at most one literal argument."""

import pytest

from pyrtan.template import MISSING, TemplateError, check_expression, render, render_value, round_text
from pyrtan.tree import TreeError, load_tree_text

ROWS = [{"name": "pear", "qty": 2}, {"name": "apple", "qty": 3.5}, {"name": "fig", "qty": "1.25"}, {"name": "date"}]
SCOPE = {"state": {"rows": ROWS, "xs": [3, 1, 2, 1.5], "words": ["b", "a", "B", "é"], "t": 21.37, "share": 0.4735, "empty": []}}


def value(expr: str):
    return render_value(expr, SCOPE)


def test_lists_of_numbers():
    assert value("state.xs | sum") == 7.5
    assert value("state.xs | average") == 1.875
    assert value("state.xs | min") == 1 and value("state.xs | max") == 3
    assert value("state.xs | sort") == [1, 1.5, 2, 3] and value("state.xs | sort | reverse") == [3, 2, 1.5, 1]
    # Nothing to take the least of is nothing; a sum of nothing is 0.
    assert value("state.empty | sum") == 0
    assert value("state.empty | max") is MISSING and value("state.empty | average") is MISSING


def test_lists_of_tables():
    # pluck leaves out a row without the key; a string that is a number counts as one.
    assert value("state.rows | pluck:qty") == [2, 3.5, "1.25"]
    assert value("state.rows | pluck:qty | sum") == 6.75
    assert value("state.rows | pluck:name | sort") == ["apple", "date", "fig", "pear"]
    with pytest.raises(TemplateError, match="sort:qty needs a list of tables that all have 'qty'"):
        value("state.rows | sort:qty")
    assert value("state.rows | pluck:qty | sort") == ["1.25", 2, 3.5]  # sorted as numbers, kept as given


def test_words_sort_by_code_point():
    assert value("state.words | sort") == ["B", "a", "b", "é"]
    with pytest.raises(TemplateError, match="a list of tables takes sort:key"):
        value("state.rows | sort")


@pytest.mark.parametrize("x, places, text", [
    (2.5, 0, "3"), (-2.5, 0, "-3"), (1.005, 2, "1.01"), (2.675, 2, "2.68"), (9.995, 2, "10"),
    (-0.04, 1, "0"), (1e-7, 3, "0"), (0.000123456, 5, "0.00012"), (123.0, 2, "123"), (7, 3, "7"),
])
def test_rounding_is_half_away_from_zero_on_the_digits_people_see(x, places, text):
    assert round_text(x, places) == text


def test_numbers_as_people_read_them():
    assert value("state.t | round:1") == 21.4 and value("state.t | round") == 21
    assert isinstance(value("state.t | round:0"), int)
    assert value("state.share | percent:1") == "47.3%" and value("state.share | percent") == "47%"
    assert render("{{ state.n | bytes }}", {"state": {"n": 512}}) == "512 B"
    assert render("{{ state.n | bytes }}", {"state": {"n": 1536}}) == "1.5 KiB"
    assert render("{{ state.n | bytes }}", {"state": {"n": 5 * 1024**3}}) == "5 GiB"
    assert render("{{ state.n | bytes }}", {"state": {"n": 1048566}}) == "1 MiB"  # 1023.99 KiB reads as 1 MiB


def test_unit_conversions():
    assert value("state.t | c_to_f | round:1") == 70.5
    assert render("{{ state.c | c_to_f }}", {"state": {"c": -40}}) == -40
    assert render("{{ state.f | f_to_c }}°C", {"state": {"f": 212}}) == "100°C"  # a whole result reads as a whole number
    assert value("state.t | km_to_mi | round:2") == 13.28 and value("state.t | mi_to_km | round:2") == 34.39
    assert render("{{ state.x | lb_to_kg | round:3 }}", {"state": {"x": 1}}) == 0.454
    assert render("{{ state.x | ft_to_m }}", {"state": {"x": 10}}) == 3.048


@pytest.mark.parametrize("expr, words", [
    ("state.rows | pluck:qty | max | bytes | upper | nope", "unknown filter 'nope'"),
    ("state.t | round:13", "from 0 to 12"),
    ("state.t | round:x", "from 0 to 12"),
    ("state.t | length:2", "length takes no argument"),
    ("state.xs | pluck", "pluck needs a key"),
    ("state.xs | pluck:a.b", "the key is letters"),
])
def test_a_bad_filter_is_named(expr, words):
    assert words in check_expression(expr)
    with pytest.raises(TemplateError, match=words.split(":")[0]):
        value(expr)


@pytest.mark.parametrize("expr, words", [
    ("state.words | sum", "sum: 'b' is not a number"),
    ("state.t | sum", "sum needs a list"),
    ("state.words | round", "round: '[\"b\", \"a\", \"B\", \"é\"]' is not a number"),
])
def test_the_wrong_kind_of_value_stops_the_run(expr, words):
    with pytest.raises(TemplateError) as e:
        value(expr)
    assert words in str(e.value)


def test_check_refuses_a_bad_filter():
    tree = """
name = "sums"
description = "adds up"
start = "done"
[inputs.xs]
type = "list"
[nodes.done]
kind = "done"
say = "{{ inputs.xs | sum | round:2 }}"
"""
    load_tree_text(tree)
    with pytest.raises(TreeError, match="nodes.done: unknown filter 'total'"):
        load_tree_text(tree.replace("sum | round:2", "total"))
    with pytest.raises(TreeError, match="round:two"):
        load_tree_text(tree.replace("round:2", "round:two"))
