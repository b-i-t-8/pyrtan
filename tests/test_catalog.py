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
    assert cat.find("weather")["version"] == 4
    assert cat.find("weather@4")["name"] == "weather"
    with pytest.raises(CatalogError, match="has version 4, not 5"):
        cat.find("weather@5")
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
    assert "weather@4" in capsys.readouterr().out
    assert main(["search", "zzz", "--catalog", served]) == 1
    assert main(["pull", "site-status", "weather@4", "--catalog", served, "--dir", str(tmp_path / "t")]) == 0
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
    assert cat.find("barry/weather")["version"] == 2 and cat.find("pyrtan/weather@4")["namespace"] == "pyrtan"
    with pytest.raises(CatalogError, match="has version 2, not 9"):
        cat.find("barry/weather@9")
    with pytest.raises(CatalogError, match="not in the catalog"):
        cat.find("nobody/weather")
    assert [e["id"] for e in cat.search("barry")] == ["barry/weather"]
    assert {e["id"] for e in cat.search("shell")} == {"pyrtan/disk-space", "pyrtan/disk-alert"}
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
    # Who owns a namespace is what the base branch says: the change may rewrite or remove OWNERS.
    both = ["catalog/barry/OWNERS", "catalog/barry/lan-printer.toml"]
    assert [str(p) for p in gate(repo, changed=both, author="mallory", base_owners={"barry": ["alice"]})] == [f"{c}: mallory is not in catalog/barry/OWNERS on the base branch; only a namespace's owners change it" for c in both]
    assert gate(repo, changed=both, author="alice", base_owners={"barry": ["alice"]}) == []                  # an owner may add owners
    assert gate(repo, changed=["catalog/new/OWNERS", "catalog/new/x.toml"], author="mallory", base_owners={}) == []  # a new namespace is the author's to name
    assert gate(repo, changed=["catalog/barry/lan-printer.toml"], author="mallory", base_owners={}) != []  # not on base, so the checkout's OWNERS (barry) decides
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


def test_site_gate_judges_owners_by_the_base_branch(tmp_path):
    """site/gate.py as the catalog-gate workflow runs it: a pull request that adds its
    author to OWNERS, or removes the namespace, is judged by OWNERS as main has it."""
    import subprocess
    import sys
    from conftest import ROOT

    repo = tmp_path / "repo"
    (repo / "catalog" / "barry").mkdir(parents=True)
    (repo / "examples").mkdir()
    (repo / "examples" / ".keep").write_text("")  # as in the repository, catalog/ is not the only directory the gate reads
    (repo / "site" / "names").mkdir(parents=True)
    (repo / "site" / "CNAME").write_text("pyrtan.com\n")
    (repo / "site" / "gate.py").write_text("import sys\nsys.exit(0)\n")  # the judged checkout's own gate is never the one run
    (repo / "catalog" / "barry" / "OWNERS").write_text("barry\n")
    (repo / "catalog" / "barry" / "lan-printer.toml").write_text(OTHER.replace('name = "weather"', 'name = "lan-printer"'))
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
    gate = lambda *a: subprocess.run([sys.executable, str(ROOT / "site" / "gate.py"), "--root", str(repo), "--base", "main", *a], cwd=tmp_path, capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    # Mallory adds themself to OWNERS and changes the tree, version bumped and all.
    git("checkout", "-q", "-b", "take")
    (repo / "catalog" / "barry" / "OWNERS").write_text("barry\nmallory\n")
    (repo / "catalog" / "barry" / "lan-printer.toml").write_text(OTHER.replace('name = "weather"', 'name = "lan-printer"').replace("version = 2", "version = 3").replace('say = "ok"', 'say = "mine"'))
    git("commit", "-q", "-am", "take")
    run = gate("--author", "mallory", "--author-id", "9")
    assert run.returncode == 1 and "catalog/barry/OWNERS: mallory is not in catalog/barry/OWNERS on the base branch" in run.stdout, run.stdout
    run = gate("--author", "barry", "--author-id", "1")
    assert run.returncode == 0, run.stdout
    # Mallory removes the namespace altogether.
    git("checkout", "-q", "main")
    git("checkout", "-q", "-b", "remove")
    git("rm", "-q", "-r", "catalog/barry")
    git("commit", "-q", "-m", "remove")
    run = gate("--author", "mallory", "--author-id", "9")
    assert run.returncode == 1 and "catalog/barry/lan-printer.toml: mallory is not in catalog/barry/OWNERS on the base branch" in run.stdout, run.stdout
    # A new namespace is the author's to name.
    git("checkout", "-q", "main")
    git("checkout", "-q", "-b", "new")
    (repo / "catalog" / "mallory").mkdir()
    (repo / "catalog" / "mallory" / "OWNERS").write_text("mallory\n")
    (repo / "catalog" / "mallory" / "lamp.toml").write_text(OTHER.replace('name = "weather"', 'name = "lamp"').replace('namespace = "barry"', 'namespace = "mallory"'))
    git("add", ".")
    git("commit", "-q", "-m", "new")
    run = gate("--author", "mallory", "--author-id", "9")
    assert run.returncode == 0, run.stdout


def test_gate_knows_owners_by_id_and_guards_the_examples(tmp_path):
    from pyrtan.catalog import gate, parse_owner_entries

    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLES, repo / "examples")
    (repo / "catalog" / "barry").mkdir(parents=True)
    (repo / "catalog" / "barry" / "OWNERS").write_text("barry 1  # login, then the numeric id\n")
    (repo / "catalog" / "barry" / "lan-printer.toml").write_text(OTHER.replace('name = "weather"', 'name = "lan-printer"'))
    assert parse_owner_entries((repo / "catalog" / "barry" / "OWNERS").read_text()) == [("barry", 1)]
    assert gate(repo) == []
    tree = ["catalog/barry/lan-printer.toml"]
    base = {"barry": parse_owner_entries("barry 1\n")}
    # The id is who owns it: a login given up and taken by someone else does not come with the namespace.
    assert gate(repo, changed=tree, author="barry", author_id=1, base_owners=base) == []
    assert gate(repo, changed=tree, author="barry-renamed", author_id=1, base_owners=base) == []
    assert gate(repo, changed=tree, author="barry", author_id=666, base_owners=base) != []
    # A line with no id is matched by login, as before.
    assert gate(repo, changed=tree, author="barry", author_id=666, base_owners={"barry": ["barry"]}) == []
    # The examples are every console's trees: only their owners change them, as main's OWNERS says.
    weather = ["examples/weather/weather.toml"]
    owners = parse_owner_entries((EXAMPLES / "OWNERS").read_text())
    assert [str(p) for p in gate(repo, changed=weather, author="mallory", author_id=9, base_examples=owners)] == [
        "examples/weather/weather.toml: mallory is not in examples/OWNERS on the base branch; only its owners change the trees every console carries"]
    login, ident = owners[0]
    assert gate(repo, changed=weather, author=login, author_id=ident, base_examples=owners) == []
    (repo / "examples" / "OWNERS").unlink()
    assert any("examples/ needs an OWNERS file" in str(p) for p in gate(repo))
    shutil.copy(EXAMPLES / "OWNERS", repo / "examples" / "OWNERS")
    # A new namespace that a name at pyrtan.com claims is that name's account's.
    (repo / "site" / "names").mkdir(parents=True)
    (repo / "site" / "names" / "alice.json").write_text(json.dumps({"github": 5}))
    new = ["catalog/alice/OWNERS"]
    (repo / "catalog" / "alice").mkdir()
    (repo / "catalog" / "alice" / "OWNERS").write_text("mallory 9\n")
    assert [str(p) for p in gate(repo, changed=new, author="mallory", author_id=9, base_owners={})] == [
        "catalog/alice/OWNERS: the name 'alice' is claimed at pyrtan.com by GitHub account 5; only it may start that namespace"]
    (repo / "catalog" / "alice" / "OWNERS").write_text("alice 5\n")
    assert gate(repo, changed=new, author="alice", author_id=5, base_owners={}) == []


def test_site_gate_refuses_a_release_number_main_already_has(tmp_path):
    """Two branches that each bump to the next version cannot both be merged under it:
    the one that comes second is told to bump past the base's (0.35.0 named two releases)."""
    import subprocess
    import sys
    from conftest import ROOT

    repo = tmp_path / "repo"
    (repo / "examples").mkdir(parents=True)
    (repo / "examples" / ".keep").write_text("")
    (repo / "site" / "names").mkdir(parents=True)
    (repo / "site" / "CNAME").write_text("pyrtan.com\n")
    (repo / "src" / "pyrtan").mkdir(parents=True)

    def version(v: str, init: str | None = None) -> None:
        (repo / "pyproject.toml").write_text(f'[project]\nname = "pyrtan"\nversion = "{v}"\n')
        (repo / "src" / "pyrtan" / "__init__.py").write_text(f'"""pyrtan"""\n\n__version__ = "{init or v}"\n')

    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
    gate = lambda: subprocess.run([sys.executable, str(ROOT / "site" / "gate.py"), "--root", str(repo), "--base", "main"], cwd=tmp_path, capture_output=True, text=True)
    version("0.34.2")
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "0.34.2")
    git("checkout", "-q", "-b", "game")
    version("0.35.0")
    git("commit", "-q", "-am", "the game; release 0.35.0")
    assert gate().returncode == 0  # above main's 0.34.2
    git("checkout", "-q", "main")
    version("0.35.0")
    git("commit", "-q", "-am", "LDAP users; release 0.35.0")  # another branch takes the number first
    git("checkout", "-q", "game")
    run = gate()
    assert run.returncode == 1 and "sets version 0.35.0, which is already the base's" in run.stdout and "above 0.35.0" in run.stdout, run.stdout
    version("0.34.9")
    git("commit", "-q", "-am", "lower")
    assert "below the base's 0.35.0" in gate().stdout
    version("0.36.0")
    git("commit", "-q", "-am", "past it")
    assert gate().returncode == 0
    version("0.36.0", init="0.35.0")
    git("commit", "-q", "-am", "forgot one")
    run = gate()
    assert run.returncode == 1 and "__init__.py says __version__ = '0.35.0'" in run.stdout, run.stdout
    # A branch that sets no number of its own is open, however old its fork.
    git("checkout", "-q", "-b", "docs", "main~1")
    (repo / "examples" / "README").write_text("docs\n")
    git("add", ".")
    git("commit", "-q", "-m", "docs only")
    assert gate().returncode == 0
