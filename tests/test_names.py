import json
import subprocess
import sys

import pytest

pytest.importorskip("cryptography")

from pyrtan import identity, names  # noqa: E402
from pyrtan.cli import main  # noqa: E402
from pyrtan.identity import IdentityError  # noqa: E402
from conftest import ROOT  # noqa: E402


@pytest.fixture
def alice():
    return identity.Key.generate()


@pytest.fixture
def bob():
    return identity.Key.generate()


def serving(files):
    """A fetch that answers from {url: body}, 404 otherwise, and remembers what it was asked."""
    asked = []

    def fetch(url):
        asked.append(url)
        return (200, files[url]) if url in files else (404, None)
    fetch.asked = asked
    return fetch


# ---------------------------------------------------------------- resolving


def test_resolve(alice):
    claim = identity.make_name(alice, "alice@example.org")
    fetch = serving({"https://example.org/.well-known/pyrtan/name/alice.json": claim})
    assert names.resolve("Alice@Example.org", fetch=fetch) == claim


def test_resolve_refuses(alice, bob):
    url = "https://example.org/.well-known/pyrtan/name/alice.json"
    with pytest.raises(IdentityError, match="no claim"):
        names.resolve("alice@example.org", fetch=serving({}))
    with pytest.raises(IdentityError, match="not alice@example.org"):
        names.resolve("alice@example.org", fetch=serving({url: identity.make_name(alice, "bob@example.org")}))
    forged = {**identity.make_name(alice, "alice@example.org"), "id": bob.id}
    with pytest.raises(IdentityError, match="signature"):
        names.resolve("alice@example.org", fetch=serving({url: forged}))
    with pytest.raises(IdentityError, match="answered 500"):
        names.resolve("alice@example.org", fetch=lambda u: (500, None))
    with pytest.raises(IdentityError, match="boom"):
        names.resolve("alice@example.org", fetch=lambda u: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(IdentityError, match="Tor"):
        names.resolve("alice@" + "a" * 56 + ".onion", fetch=serving({}))
    with pytest.raises(IdentityError, match="not a name"):
        names.resolve(alice.id, fetch=serving({}))


def test_fetch_identity(alice, bob):
    doc = identity.make_identity(alice, routes=["hub.example"])
    url = names.identity_url(alice.id, "example.org")
    assert url == f"https://example.org/.well-known/pyrtan/id/{alice.id[4:]}.json"
    assert names.fetch_identity(alice.id, "example.org", fetch=serving({url: doc})) == doc
    assert names.fetch_identity(bob.id, "example.org", fetch=serving({names.identity_url(bob.id, "example.org"): doc})) is None
    assert names.fetch_identity(alice.id, "example.org", fetch=serving({})) is None


def test_pins(tmp_path, alice, bob):
    pins = names.Pins(tmp_path / "pins.json")
    assert pins.compare("Alice@pyrtan.com", alice.id) == "new"
    pins.pin("Alice@pyrtan.com", alice.id, now=100)
    pins.pin("alice@pyrtan.com", alice.id, now=200)
    assert pins.get("alice@pyrtan.com") == {"id": alice.id, "first": 100, "seen": 200}
    assert pins.compare("alice@pyrtan.com", alice.id.upper()) == "same"
    assert pins.compare("alice@pyrtan.com", bob.id) == "changed"
    pins.save()
    again = names.Pins(tmp_path / "pins.json")
    assert again.get("alice@pyrtan.com")["first"] == 100
    again.pin("alice@pyrtan.com", bob.id, now=300)
    assert again.get("alice@pyrtan.com")["first"] == 300
    assert again.forget("alice@pyrtan.com") and not again.forget("alice@pyrtan.com")
    (tmp_path / "bad.json").write_text("[]")
    with pytest.raises(IdentityError, match="pins file"):
        names.Pins(tmp_path / "bad.json")


# ---------------------------------------------------------------- serving a directory of names


def test_build_wellknown(tmp_path, alice, bob):
    d = tmp_path / "names"
    d.mkdir()
    doc = identity.make_identity(alice, routes=["hub.example"])
    (d / "alice.json").write_text(json.dumps(identity.make_name(alice, "alice@example.org")))
    (d / "alice.id.json").write_text(json.dumps(doc))
    (d / "bob.json").write_text(json.dumps(identity.make_name(bob, "bob@elsewhere.org")))   # for another domain
    (d / "carol.id.json").write_text(json.dumps(identity.make_identity(bob)))              # no claim beside it
    (d / "notes.txt").write_text("hi")
    (d / "README.md").write_text("# names")
    count, problems = names.build_wellknown(d, tmp_path / "out", "example.org")
    assert count == 1
    text = "\n".join(str(p) for p in problems)
    assert "bob.json: the claim is for bob@elsewhere.org" in text
    assert "carol.id.json: an identity document needs" in text
    assert "notes.txt: only" in text
    root = tmp_path / "out" / ".well-known" / "pyrtan"
    assert json.loads((root / "name" / "alice.json").read_text())["id"] == alice.id
    assert not (root / "name" / "bob.json").exists()
    assert json.loads((root / "id" / f"{alice.id[4:]}.json").read_text()) == doc
    index = json.loads((root / "names.json").read_text())
    assert index["domain"] == "example.org" and list(index["names"]) == ["alice"]


# ---------------------------------------------------------------- pyrtan.com's gate


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "site" / "names").mkdir(parents=True)
    (tmp_path / "site" / "CNAME").write_text("pyrtan.com\n")
    return tmp_path


def put(repo, key, login, github, *, doc=True):
    claim = identity.make_name(key, f"{login}@pyrtan.com", **({"github": github} if github is not None else {}))
    (repo / "site" / "names" / f"{login}.json").write_text(json.dumps(claim))
    if doc:
        (repo / "site" / "names" / f"{login}.id.json").write_text(json.dumps(identity.make_identity(key)))
    return claim


def raw(claim):
    return json.dumps(claim).encode()


def messages(problems):
    return "\n".join(str(p) for p in problems)


def test_gate_the_whole_directory(repo, alice, bob):
    put(repo, alice, "alice", 1)
    assert names.gate(repo) == []
    put(repo, bob, "bob", 1, doc=False)
    assert "already holds" in messages(names.gate(repo))
    put(repo, bob, "bob", None, doc=False)
    assert "numeric GitHub user id" in messages(names.gate(repo))
    (repo / "site" / "names" / "bob.json").unlink()
    put(repo, bob, "b.b", 2, doc=False)
    assert "is a GitHub login" in messages(names.gate(repo))


def test_gate_a_new_name(repo, alice):
    put(repo, alice, "alice", 1)
    changed = ["site/names/alice.json", "site/names/alice.id.json"]
    assert names.gate(repo, changed=changed, author="alice", author_id=1) == []
    assert "is not mallory's" in messages(names.gate(repo, changed=changed, author="mallory", author_id=1))
    assert "author is 2" in messages(names.gate(repo, changed=changed, author="alice", author_id=2))
    assert "--author-id" in messages(names.gate(repo, changed=changed, author="alice"))


def test_gate_a_change_or_removal(repo, alice, bob):
    before = put(repo, alice, "alice", 1)
    base = {"site/names/alice.json": raw(before)}
    # The owner rotates the key.
    put(repo, bob, "alice", 1)
    assert names.gate(repo, changed=["site/names/alice.json"], author="alice", author_id=1, base=base) == []
    # Someone who took the login after a rename cannot.
    assert "belongs to GitHub account 1" in messages(names.gate(repo, changed=["site/names/alice.json"], author="alice", author_id=9, base=base))
    assert "belongs to GitHub account 1" in messages(names.gate(repo, changed=["site/names/alice.id.json"], author="alice", author_id=9, base=base))
    # Nor can the owner hand it to another account.
    put(repo, alice, "alice", 7)
    assert "stays with GitHub account 1" in messages(names.gate(repo, changed=["site/names/alice.json"], author="alice", author_id=1, base=base))
    # Removal.
    for f in ("alice.json", "alice.id.json"):
        (repo / "site" / "names" / f).unlink()
    assert names.gate(repo, changed=["site/names/alice.json"], author="alice", author_id=1, base=base) == []
    assert "belongs to" in messages(names.gate(repo, changed=["site/names/alice.json"], author="x", author_id=2, base=base))


def test_site_gate_reads_owners_from_git(tmp_path, alice, bob):
    """site/gate.py, as the catalog-gate workflow runs it: this checkout's gate, judging
    another checkout (--root) against a real base branch."""
    repo = tmp_path / "repo"
    (repo / "site" / "names").mkdir(parents=True)
    (repo / "catalog").mkdir()
    # The judged checkout's own gate would let anything through; it is never the one run.
    (repo / "site" / "gate.py").write_text("import sys\nsys.exit(0)\n")
    (repo / "site" / "CNAME").write_text("pyrtan.com\n")
    put(repo, alice, "alice", 1)
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "pr")
    put(repo, bob, "alice", 1)
    git("commit", "-q", "-am", "take alice")
    gate = lambda *a: subprocess.run([sys.executable, str(ROOT / "site" / "gate.py"), "--root", str(repo), "--base", "main", *a], cwd=tmp_path, capture_output=True, text=True)
    run = gate("--author", "alice", "--author-id", "2")
    assert run.returncode == 1 and "belongs to GitHub account 1" in run.stdout
    run = gate("--author", "alice", "--author-id", "1")
    assert run.returncode == 0, run.stdout
    # Moving someone's name out of site/names/ is a removal of it, which git would call a rename.
    git("checkout", "-q", "main")
    git("checkout", "-q", "-b", "move")
    git("mv", "site/names/alice.json", "site/alice.json")
    git("commit", "-q", "-m", "move")
    run = gate("--author", "mallory", "--author-id", "9")
    assert run.returncode == 1 and "site/names/alice.json: alice belongs to GitHub account 1" in run.stdout


# ---------------------------------------------------------------- pyrtan id


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_resolve_pins_and_repin(tmp_path, capsys, monkeypatch, alice, bob):
    d = str(tmp_path / "me")
    url = "https://example.org/.well-known/pyrtan/name/alice.json"
    doc = identity.make_identity(alice, routes=["hub.example"])
    files = {url: identity.make_name(alice, "alice@example.org"), names.identity_url(alice.id, "example.org"): doc}
    monkeypatch.setattr(names, "_fetch", serving(files))
    code, out, _ = run(capsys, "id", "resolve", "alice@example.org", "--dir", d)
    assert code == 0 and f"alice@example.org is {alice.id} (pinned now)" in out and "routes: hub.example" in out
    code, out, _ = run(capsys, "id", "resolve", "alice@example.org", "--dir", d)
    assert code == 0 and "as pinned since" in out
    files[url] = identity.make_name(bob, "alice@example.org")
    code, _, err = run(capsys, "id", "resolve", "alice@example.org", "--dir", d)
    assert code == 1 and bob.id in err and alice.id in err and "--repin" in err
    code, out, _ = run(capsys, "id", "resolve", "alice@example.org", "--repin", "--json", "--dir", d)
    assert code == 0 and json.loads(out)["claim"]["id"] == bob.id
    code, out, _ = run(capsys, "id", "pins", "--dir", d)
    assert "alice@example.org" in out and bob.id in out
    code, out, _ = run(capsys, "id", "pins", "--forget", "alice@example.org", "--dir", d)
    assert code == 0 and "forgot" in out
    code, _, err = run(capsys, "id", "pins", "--forget", "alice@example.org", "--dir", d)
    assert code == 1


def test_cli_name_into_a_checkout(tmp_path, capsys, monkeypatch, repo):
    d = str(tmp_path / "me")
    run(capsys, "id", "new", "--dir", d)
    monkeypatch.setattr(names, "_fetch", serving({"https://api.github.com/users/alice": {"login": "alice", "id": 4242}}))
    code, out, err = run(capsys, "id", "name", "alice@pyrtan.com", "--into", str(repo), "--dir", d)
    assert code == 0, err
    assert "user 4242" in err and "open a pull request from the GitHub account alice" in out
    claim = json.loads((repo / "site" / "names" / "alice.json").read_text())
    assert claim["github"] == 4242 and identity.check_name(claim, name="alice@pyrtan.com") is None
    assert (repo / "site" / "names" / "alice.id.json").is_file()
    assert names.gate(repo, changed=["site/names/alice.json"], author="alice", author_id=4242) == []
    code, _, err = run(capsys, "id", "name", "alice@example.org", "--github", "1", "--into", str(repo), "--dir", d)
    assert code == 1 and "serves names at pyrtan.com" in err


def test_cli_wellknown(tmp_path, capsys, alice):
    d = tmp_path / "names"
    d.mkdir()
    (d / "alice.json").write_text(json.dumps(identity.make_name(alice, "alice@example.org")))
    code, out, _ = run(capsys, "id", "wellknown", str(d), "--domain", "Example.org", "--out", str(tmp_path / "site"))
    assert code == 0 and "wrote 1 name(s) for example.org" in out
    (d / "bad.json").write_text("{}")
    code, _, err = run(capsys, "id", "wellknown", str(d), "--domain", "example.org", "--out", str(tmp_path / "site"))
    assert code == 1 and "bad.json" in err
