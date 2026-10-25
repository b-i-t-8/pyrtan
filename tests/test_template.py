import pytest

from pyrtan.template import MISSING, TemplateError, placeholders, render, resolve

SCOPE = {"inputs": {"city": "New York", "n": 3}, "state": {"resp": {"json": {"items": [{"title": "a"}, {"title": "b"}]}, "status": 200}, "ids": [1, 2, 3]}}


def test_resolve_paths():
    assert resolve("inputs.city", SCOPE) == "New York"
    assert resolve("state.resp.json.items[1].title", SCOPE) == "b"
    assert resolve("state.resp.json.items[-1].title", SCOPE) == "b"
    assert resolve("state.ids[5]", SCOPE) is MISSING
    assert resolve("state.nope.deeper", SCOPE) is MISSING
    assert resolve('state.resp["status"]', SCOPE) == 200


def test_whole_placeholder_keeps_type():
    assert render("{{ state.ids }}", SCOPE) == [1, 2, 3]
    assert render("{{ state.resp.status }}", SCOPE) == 200


def test_interpolation_and_filters():
    assert render("city={{ inputs.city | url }}", SCOPE) == "city=New%20York"
    assert render("{{ inputs.city | lower }} x{{ inputs.n }}", SCOPE) == "new york x3"
    assert render("{{ state.ids | length }}", SCOPE) == 3
    assert render("{{ state.ids | first }}", SCOPE) == 1
    assert render("{{ state.resp.json.items | json }}", SCOPE) == '[{"title": "a"}, {"title": "b"}]'


def test_nested_structures_render():
    out = render({"url": "https://x/{{ inputs.n }}", "list": ["{{ inputs.city }}", 1]}, SCOPE)
    assert out == {"url": "https://x/3", "list": ["New York", 1]}


def test_missing_is_an_error_unless_lenient():
    with pytest.raises(TemplateError):
        render("{{ state.nothing }}", SCOPE)
    with pytest.raises(TemplateError):
        render("x {{ state.nothing }} y", SCOPE)
    assert render("x {{ state.nothing }} y", SCOPE, strict=False) == "x  y"
    assert render("{{ state.nothing }}", SCOPE, strict=False) is None


def test_unknown_filter():
    with pytest.raises(TemplateError):
        render("{{ inputs.city | shout }}", SCOPE)


def test_multiple_placeholders_are_not_one():
    # A regression: a greedy placeholder regex once swallowed the text between two.
    assert render("{{ inputs.city }} ({{ inputs.n }})", SCOPE) == "New York (3)"


def test_placeholders_listing():
    assert placeholders({"a": "{{ inputs.x | url }}", "b": ["{{ state.y }}", "{{ item }}"]}) == ["inputs.x", "state.y", "item"]
