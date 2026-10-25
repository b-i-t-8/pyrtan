import pytest

from pyrtan.html import SelectorError, compile_selector, parse

DOC = """
<html><head><title>Shop</title></head>
<body>
  <div id="main" class="wrap big">
    <h1>Items</h1>
    <table>
      <tr class="row"><td>Apple</td><td class="qty">3</td></tr>
      <tr class="row sold-out"><td>Pear</td><td class="qty">0</td></tr>
    </table>
    <a href="/one" class="story">One</a>
    <a href="https://x/two" class="story external">Two &amp; more</a>
    <p>Total: <b>2</b> items<br>end</p>
  </div>
</body></html>
"""


@pytest.fixture
def doc():
    return parse(DOC)


def test_tag_id_class(doc):
    assert doc.select_one("title").text == "Shop"
    assert doc.select_one("#main").tag == "div"
    assert [e.text for e in doc.select(".row")] == ["Apple 3", "Pear 0"]
    assert len(doc.select(".row.sold-out")) == 1
    assert doc.select_one("div.wrap h1").text == "Items"


def test_attribute_selectors(doc):
    assert [a.get("href") for a in doc.select("a[href]")] == ["/one", "https://x/two"]
    assert doc.select_one('a[href="/one"]').text == "One"
    assert doc.select_one("a[href^=https]").text == "Two & more"
    assert doc.select_one("a[href$=two]").text == "Two & more"
    assert doc.select_one("a[class*=external]").get("href") == "https://x/two"


def test_combinators_and_pseudo(doc):
    assert [e.text for e in doc.select("tr > td:nth-child(2)")] == ["3", "0"]
    assert doc.select_one("tr:first-child td").text == "Apple"
    assert doc.select_one("tr:last-child td").text == "Pear"
    assert doc.select_one("td:contains(Pear)").text == "Pear"
    assert [e.tag for e in doc.select("h1, title")] == ["title", "h1"]
    assert doc.select("#main > td") == []
    assert len(doc.select("#main td")) == 4


def test_text_collapses_whitespace_and_void_tags(doc):
    assert doc.select_one("p").text == "Total: 2 items end"


def test_unsupported_selector_is_an_error():
    with pytest.raises(SelectorError):
        compile_selector("a ~ b")
    with pytest.raises(SelectorError):
        compile_selector("div:hover")
    with pytest.raises(SelectorError):
        compile_selector("")


def test_malformed_html_does_not_raise():
    doc = parse("<div><p>unclosed<div>another</p></div>")
    assert doc.select_one("div").text.startswith("unclosed")
