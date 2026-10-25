import pytest

from pyrtan.extract import ExtractError, extract, validate
from pyrtan.template import MISSING

HTML = '<ul><li class="s"><a href="/a">A</a><span>1</span></li><li class="s"><a href="/b">B</a><span>2</span></li></ul>'
SOURCE = {"status": 200, "body": HTML, "json": {"items": [{"t": "x"}], "price": "$12.50"}}


def test_json_path():
    assert extract({"into": "t", "json": "json.items[0].t"}, SOURCE) == "x"
    assert extract({"into": "t", "json": "json.items[9].t"}, SOURCE) is MISSING


def test_regex():
    assert extract({"into": "p", "regex": r"\$([0-9.]+)", "from": "json.price"}, SOURCE) == "12.50"
    assert extract({"into": "p", "regex": r"href=\"(/\w)\"", "all": True}, SOURCE) == ["/a", "/b"]
    assert extract({"into": "p", "regex": "zzz"}, SOURCE) is MISSING
    assert extract({"into": "p", "regex": "<li", "group": 0}, SOURCE) == "<li"


def test_css_text_attr_all_fields():
    assert extract({"into": "x", "css": "li a"}, SOURCE) == "A"
    assert extract({"into": "x", "css": "li a", "attr": "href", "all": True}, SOURCE) == ["/a", "/b"]
    rows = extract({"into": "x", "css": "li.s", "all": True, "fields": {"name": "a", "n": "span", "href": {"css": "a", "attr": "href"}}}, SOURCE)
    assert rows == [{"name": "A", "n": "1", "href": "/a"}, {"name": "B", "n": "2", "href": "/b"}]
    assert extract({"into": "x", "css": "h1"}, SOURCE) is MISSING
    assert extract({"into": "x", "css": "h1", "all": True}, SOURCE) == []


def test_css_caches_parsed_document():
    cache = {}
    extract({"into": "x", "css": "li a"}, SOURCE, cache)
    extract({"into": "y", "css": "span"}, SOURCE, cache)
    assert len(cache) == 1


def test_validate():
    validate({"into": "a", "json": "x"})
    with pytest.raises(ExtractError):
        validate({"into": "a"})
    with pytest.raises(ExtractError):
        validate({"into": "a", "json": "x", "css": "y"})
    with pytest.raises(ExtractError):
        validate({"into": "a", "regex": "("})
    with pytest.raises(ExtractError):
        validate({"into": "a", "css": "a ~ b"})
    with pytest.raises(ExtractError):
        validate({"into": "a", "css": "li", "fields": {"x": "a ~ b"}})
    with pytest.raises(ExtractError):
        validate({"json": "x"})
