"""The catalog: building an index from a directory, and pulling from one
over HTTP with every check that keeps a bad file out."""

import hashlib
import json
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyrtan.catalog import Catalog, CatalogError, build_index
from pyrtan.cli import main
from conftest import EXAMPLES


@pytest.fixture
def catalog_dir(tmp_path):
    root = tmp_path / "catalog"
    shutil.copytree(EXAMPLES, root)
    index, problems = build_index(root)
    assert not problems, problems
    (root / "index.json").write_text(json.dumps(index))
    return root


@pytest.fixture
def served(catalog_dir):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(catalog_dir)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_build_index_describes_every_tree(catalog_dir):
    index = json.loads((catalog_dir / "index.json").read_text())
    assert index["pyrtan-catalog"] == 1 and index["count"] == 5
    weather = next(e for e in index["trees"] if e["name"] == "weather")
    assert weather["path"] == "weather/weather.toml" and weather["license"] == "MIT"
    assert weather["sha256"] == hashlib.sha256((catalog_dir / "weather/weather.toml").read_bytes()).hexdigest()
    assert "[what's the] weather in {place}" in weather["triggers"]
    assert weather["inputs"]["place"]["required"] is True


def test_build_index_leaves_out_what_does_not_belong(tmp_path):
    (tmp_path / "ok.toml").write_text('name = "ok"\nlicense = "MIT"\nstart = "s"\n[[triggers]]\npattern = "ok now"\n[nodes.s]\nkind = "done"\nsay = "ok"\n')
    (tmp_path / "zz-dup.toml").write_text('name = "ok"\nlicense = "MIT"\nstart = "s"\n[[triggers]]\npattern = "ok again"\n[nodes.s]\nkind = "done"\nsay = "ok"\n')
    (tmp_path / "unlicensed.toml").write_text('name = "free"\nstart = "s"\n[[triggers]]\npattern = "free now"\n[nodes.s]\nkind = "done"\nsay = "ok"\n')
    (tmp_path / "broken.toml").write_text("name = \n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.toml").write_text("nope")
    index, problems = build_index(tmp_path)
    assert [e["name"] for e in index["trees"]] == ["ok"]
    messages = {p.path: p.message for p in problems}
    assert "already at ok.toml" in messages["zz-dup.toml"]
    assert "license" in messages["unlicensed.toml"]
    assert "broken.toml" in messages
    assert "x.toml" not in " ".join(messages)
    index, problems = build_index(tmp_path, require_license=False)
    assert {e["name"] for e in index["trees"]} == {"ok", "free"}


def test_search_and_find(served):
    cat = Catalog(served)
    assert {e["name"] for e in cat.search("")} == {"weather", "hacker-news", "hacker-news-item", "site-status", "github-release"}
    assert [e["name"] for e in cat.search("open-meteo")] == ["weather"]
    assert [e["name"] for e in cat.search("news stories")] == ["hacker-news"]
    assert cat.search("nothing like this") == []
    assert cat.find("weather")["version"] == 1
    assert cat.find("weather@1")["name"] == "weather"
    with pytest.raises(CatalogError, match="has version 1, not 4"):
        cat.find("weather@4")
    with pytest.raises(CatalogError, match="not in the catalog"):
        cat.find("nope")
    with pytest.raises(CatalogError, match="NAME"):
        cat.find("Not Valid")


def test_pull_writes_verified_file_and_refuses_to_clobber(served, tmp_path):
    cat = Catalog(served)
    target = cat.pull("weather", tmp_path / "trees")
    assert target == tmp_path / "trees" / "weather.toml"
    assert target.read_bytes() == (EXAMPLES / "weather" / "weather.toml").read_bytes()
    assert cat.pull("weather", tmp_path / "trees") == target  # same bytes: fine
    target.write_text(target.read_text() + "\n# mine\n")
    with pytest.raises(CatalogError, match="--force"):
        cat.pull("weather", tmp_path / "trees")
    cat.pull("weather", tmp_path / "trees", force=True)
    assert "# mine" not in target.read_text()


def test_pull_refuses_a_file_that_does_not_match_the_index(catalog_dir, served, tmp_path):
    index = json.loads((catalog_dir / "index.json").read_text())
    # Tamper with the served file after the index was built.
    path = catalog_dir / "weather" / "weather.toml"
    path.write_text(path.read_text().replace("api.open-meteo.com", "evil.example"))
    with pytest.raises(CatalogError, match="does not match the index"):
        Catalog(served).pull("weather", tmp_path)
    # An index that points outside the catalog is refused too.
    for e in index["trees"]:
        if e["name"] == "weather":
            e["path"] = "../../etc/passwd"
    (catalog_dir / "index.json").write_text(json.dumps(index))
    with pytest.raises(CatalogError, match="outside the catalog"):
        Catalog(served).pull("weather", tmp_path)
    # An index whose entry says one name while the file says another.
    index = json.loads((catalog_dir / "index.json").read_text())
    hn = next(e for e in index["trees"] if e["name"] == "hacker-news")
    hn["path"] = "github/github-release.toml"
    hn["sha256"] = hashlib.sha256((catalog_dir / "github/github-release.toml").read_bytes()).hexdigest()
    (catalog_dir / "index.json").write_text(json.dumps(index))
    with pytest.raises(CatalogError, match="says it is 'github-release'"):
        Catalog(served).pull("hacker-news", tmp_path)


def test_not_a_catalog(served, tmp_path):
    (tmp_path / "index.json").write_text("{}")
    with pytest.raises(CatalogError, match="not a pyrtan catalog"):
        Catalog(str(tmp_path)).entries()
    with pytest.raises(CatalogError):
        Catalog(served + "missing/").entries()


def test_cli_index_search_pull(catalog_dir, served, tmp_path, capsys):
    out = tmp_path / "idx.json"
    assert main(["index", str(catalog_dir), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["count"] == 5
    assert main(["search", "weather", "--catalog", served]) == 0
    assert "weather@1" in capsys.readouterr().out
    assert main(["search", "zzz", "--catalog", served]) == 1
    assert main(["pull", "site-status", "weather@1", "--catalog", served, "--dir", str(tmp_path / "t")]) == 0
    assert (tmp_path / "t" / "site-status.toml").exists() and (tmp_path / "t" / "weather.toml").exists()
    assert main(["pull", "weather@9", "--catalog", served, "--dir", str(tmp_path / "t")]) == 1
    assert "not 9" in capsys.readouterr().err
    assert main(["console", "--out", str(tmp_path / "site" / "console.html")]) == 0
    assert "<title>Pyrtan</title>" in (tmp_path / "site" / "console.html").read_text()
