"""The catalog: building an index from a directory, and pulling from one
over HTTP with every check that keeps a bad file out."""

import hashlib
import json
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyrtan import Registry
from pyrtan.catalog import Catalog, CatalogError, build_index
from pyrtan.cli import main
from pyrtan.tree import load_tree
from conftest import EXAMPLES

EXAMPLE_NAMES = set(Registry.from_paths([EXAMPLES]).names())


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
    assert index["pyrtan-catalog"] == 1 and index["count"] == len(EXAMPLE_NAMES)
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
    assert {e["name"] for e in cat.search("")} == EXAMPLE_NAMES
    meteo = cat.search("open-meteo")
    assert "weather" in [e["name"] for e in meteo] and all(any("open-meteo" in h for h in e["hosts"]) for e in meteo)
    assert [e["name"] for e in cat.search("news stories")] == ["hacker-news", "hn-search"]
    assert cat.search("nothing like this") == []
    assert cat.find("weather")["version"] == 2
    assert cat.find("weather@2")["name"] == "weather"
    with pytest.raises(CatalogError, match="has version 2, not 4"):
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
    assert json.loads(out.read_text())["count"] == len(EXAMPLE_NAMES)
    assert main(["search", "weather", "--catalog", served]) == 0
    assert "weather@2" in capsys.readouterr().out
    assert main(["search", "zzz", "--catalog", served]) == 1
    assert main(["pull", "site-status", "weather@2", "--catalog", served, "--dir", str(tmp_path / "t")]) == 0
    assert (tmp_path / "t" / "site-status.toml").exists() and (tmp_path / "t" / "weather.toml").exists()
    assert main(["pull", "weather@9", "--catalog", served, "--dir", str(tmp_path / "t")]) == 1
    assert "not 9" in capsys.readouterr().err
    assert main(["console", "--out", str(tmp_path / "site" / "console.html")]) == 0
    assert "<title>Pyrtan</title>" in (tmp_path / "site" / "console.html").read_text()


def test_index_writes_the_trees_as_json_for_the_browser(catalog_dir, tmp_path):
    # The console in a browser holds the catalog's trees: the same trees, as JSON, beside the index.
    site = tmp_path / "site"
    assert main(["index", str(catalog_dir), "--copy", str(site)]) == 0
    index = json.loads((site / "index.json").read_text())
    bundle = json.loads((site / "trees.json").read_text())
    assert bundle["pyrtan-trees"] == 1 and bundle["count"] == index["count"] == len(index["trees"]) > 0
    assert [(t["id"], t["sha256"]) for t in bundle["trees"]] == [(e["id"], e["sha256"]) for e in index["trees"]]
    weather = next(t["tree"] for t in bundle["trees"] if t["id"].endswith("weather"))
    assert weather == load_tree(site / next(e["path"] for e in index["trees"] if e["id"].endswith("weather"))).raw


# ---------------------------------------------------------------- namespaces, the gate, publishing


OTHER = '''
name = "weather"
version = 2
namespace = "barry"
description = "someone else's weather"
license = "MIT"
hosts = ["example.com"]
start = "get"

[nodes.get]
kind = "observe"
http = { url = "https://example.com/" }
next = "say"

[nodes.say]
kind = "done"
say = "ok"
'''


def test_namespaces_in_the_index_and_in_pulls(tmp_path):
    src = tmp_path / "src"
    (src / "examples").mkdir(parents=True)
    shutil.copytree(EXAMPLES, src / "examples", dirs_exist_ok=True)
    (src / "catalog" / "barry").mkdir(parents=True)
    (src / "catalog" / "barry" / "weather.toml").write_text(OTHER)
    out = tmp_path / "out"
    index, problems = build_index([src / "examples", src / "catalog"], copy_to=out, require_namespace=True)
    assert not problems, problems
    ids = {e["id"]: e for e in index["trees"]}
    assert ids["pyrtan/weather"]["path"] == "pyrtan/weather.toml" and ids["barry/weather"]["path"] == "barry/weather.toml"
    assert (out / "pyrtan" / "weather.toml").exists() and (out / "barry" / "weather.toml").read_text() == OTHER
    assert ids["pyrtan/disk-space"]["sources"] == ["shell"] and ids["pyrtan/disk-space"]["commands"] == ["df"]
    (out / "index.json").write_text(json.dumps(index))
    cat = Catalog(str(out))
    # A bare name is enough when one namespace has it; with two, say which.
    assert cat.find("github-release")["id"] == "pyrtan/github-release"
    with pytest.raises(CatalogError, match="more than one namespace: barry/weather, pyrtan/weather"):
        cat.find("weather")
    assert cat.find("barry/weather")["version"] == 2 and cat.find("pyrtan/weather@2")["namespace"] == "pyrtan"
    with pytest.raises(CatalogError, match="has version 2, not 9"):
        cat.find("barry/weather@9")
    with pytest.raises(CatalogError, match="not in the catalog"):
        cat.find("nobody/weather")
    assert [e["id"] for e in cat.search("barry")] == ["barry/weather"]
    assert {e["id"] for e in cat.search("shell")} == {"pyrtan/disk-space"}
    target = cat.pull("barry/weather", tmp_path / "trees")
    assert target.name == "weather.toml" and target.read_text() == OTHER
    # A file whose namespace is not the index's is refused.
    (out / "barry" / "weather.toml").write_text(OTHER.replace('namespace = "barry"', 'namespace = "mallory"'))
    index["trees"] = [dict(e, sha256=hashlib.sha256((out / e["path"]).read_bytes()).hexdigest()) if e["id"] == "barry/weather" else e for e in index["trees"]]
    (out / "index.json").write_text(json.dumps(index))
    with pytest.raises(CatalogError, match="namespace is 'mallory'"):
        Catalog(str(out)).pull("barry/weather", tmp_path / "trees2")
    # Two directories need a place to copy to; one does not.
    with pytest.raises(ValueError):
        build_index([src / "examples", src / "catalog"])
    one, problems = build_index(src / "examples")
    assert not problems and {e["path"] for e in one["trees"]} >= {"weather/weather.toml"}


def test_gate_and_publish(tmp_path, capsys):
    from pyrtan.catalog import gate, publish

    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLES, repo / "examples")
    (repo / "catalog").mkdir()
    assert gate(repo) == []
    # Publishing needs a namespace and a licence in the file, and an owner for a new namespace.
    tree = tmp_path / "lan-printer.toml"
    tree.write_text(OTHER.replace('name = "weather"', 'name = "lan-printer"').replace('namespace = "barry"\n', ""))
    with pytest.raises(CatalogError, match='namespace = "you"'):
        publish(tree, repo, owners=["barry"])
    tree.write_text(OTHER.replace('name = "weather"', 'name = "lan-printer"'))
    with pytest.raises(CatalogError, match="say who owns it"):
        publish(tree, repo)
    placed, notes = publish(tree, repo, owners=["@Barry"])
    assert placed == (repo / "catalog" / "barry" / "lan-printer.toml").resolve() and notes == ["new namespace catalog/barry/ owned by barry"]
    assert (repo / "catalog" / "barry" / "OWNERS").read_text().endswith("barry\n")
    assert gate(repo) == []
    # The same file again is a no-op; a changed file must bump its version.
    assert publish(tree, repo)[1] == ["catalog/barry/lan-printer.toml already holds this exact file"]
    tree.write_text(tree.read_text().replace('say = "ok"', 'say = "changed"'))
    with pytest.raises(CatalogError, match="bump this file's version past it"):
        publish(tree, repo)
    tree.write_text(tree.read_text().replace("version = 2", "version = 3"))
    assert publish(tree, repo, owners=["alice"])[1] == ["added alice to catalog/barry/OWNERS", "replaces version 2 with 3"]
    # What the gate refuses.
    bad = repo / "catalog" / "barry" / "weather.toml"
    bad.write_text(OTHER.replace('namespace = "barry"', 'namespace = "pyrtan"'))
    problems = [str(p) for p in gate(repo)]
    assert any("catalog/barry/weather.toml: a tree with id 'pyrtan/weather' is already at" in p for p in problems)
    assert any("sits under catalog/barry/" in p for p in problems)
    bad.write_text(OTHER.replace('name = "weather"', 'name = "other"'))
    assert any("its file must be other.toml" in p for p in [str(p) for p in gate(repo)])
    bad.unlink()
    (repo / "catalog" / "barry" / "notes.txt").write_text("x")
    assert any("only <name>.toml files and OWNERS belong" in p for p in [str(p) for p in gate(repo)])
    (repo / "catalog" / "barry" / "notes.txt").unlink()
    (repo / "catalog" / "nobody").mkdir()
    (repo / "catalog" / "nobody" / "t.toml").write_text(OTHER.replace('namespace = "barry"', 'namespace = "nobody"').replace('name = "weather"', 'name = "t"'))
    assert any("needs an OWNERS file" in p for p in [str(p) for p in gate(repo)])
    shutil.rmtree(repo / "catalog" / "nobody")
    (repo / "examples" / "weather" / "weather.toml").write_text((repo / "examples" / "weather" / "weather.toml").read_text().replace('namespace = "pyrtan"', 'namespace = "barry"'))
    assert any("published by 'pyrtan', not 'barry'" in p for p in [str(p) for p in gate(repo)])
    shutil.copy(EXAMPLES / "weather" / "weather.toml", repo / "examples" / "weather" / "weather.toml")
    assert gate(repo) == []
    # Only owners change a namespace; a changed file bumps its version.
    assert [str(p) for p in gate(repo, changed=["catalog/barry/lan-printer.toml"], author="mallory")] == ["catalog/barry/lan-printer.toml: mallory is not in catalog/barry/OWNERS; only a namespace's owners change it"]
    assert gate(repo, changed=["catalog/barry/lan-printer.toml"], author="Alice") == []
    assert gate(repo, changed=["catalog/barry/lan-printer.toml"], author="alice", base={"catalog/barry/lan-printer.toml": (3, b"older bytes")}) != []
    assert gate(repo, changed=["catalog/barry/lan-printer.toml"], author="alice", base={"catalog/barry/lan-printer.toml": (2, b"older bytes")}) == []
    assert gate(repo, base={"catalog/barry/gone.toml": (1, b"x")}) == []  # a removal is fine
    # The command, and the index the site serves.
    assert main(["publish", str(tree), "--into", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "placed" in out and "git checkout -b catalog/barry-lan-printer" in out
    assert main(["index", str(repo / "examples"), str(repo / "catalog"), "--copy", str(tmp_path / "site"), "--require-namespace"]) == 0
    served = json.loads((tmp_path / "site" / "index.json").read_text())
    assert {e["id"] for e in served["trees"]} == {f"pyrtan/{n}" for n in EXAMPLE_NAMES} | {"barry/lan-printer"}
    assert main(["index", str(repo / "examples"), str(repo / "catalog")]) == 2  # two roots need --copy
    assert main(["search", "lan", "--catalog", str(tmp_path / "site")]) == 0 and "barry/lan-printer@3" in capsys.readouterr().out
    assert main(["pull", "lan-printer", "--catalog", str(tmp_path / "site"), "--dir", str(tmp_path / "pulled")]) == 0
    assert "barry/lan-printer@3 ->" in capsys.readouterr().out
