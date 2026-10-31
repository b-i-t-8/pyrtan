"""site/source.py: the source browser pyrtan.com publishes at /source/."""

import importlib.util
import subprocess
import sys

from conftest import ROOT

spec = importlib.util.spec_from_file_location("site_source", ROOT / "site" / "source.py")
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


def test_builds_from_this_checkout(tmp_path):
    """Works on whatever git this checkout has, a shallow clone included."""
    out = tmp_path / "source"
    run = subprocess.run([sys.executable, "site/source.py", str(out)], capture_output=True, text=True, cwd=str(ROOT))
    assert run.returncode == 0, run.stderr
    for page in ("index.html", "releases.html", "branches.html", "commits.html", "tree/src/pyrtan/index.html", "blob/pyproject.toml.html"):
        assert (out / page).is_file(), page
    blob = (out / "blob/pyproject.toml.html").read_text()
    assert 'id="L1"' in blob and "[project]" in blob
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
    assert (out / f"commit/{head}.html").is_file()
    assert 'class="md"' in (out / "index.html").read_text()    # the README, rendered


def test_markdown_escapes_and_links():
    resolve = lambda url: "R:" + url
    html = source.markdown("# T <b>\n\nSee [the `docs`](docs/x.md) and **<i>**.\n\n```\n<script>\n```\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |", resolve)
    assert "<h1>T &lt;b&gt;</h1>" in html
    assert '<a href="R:docs/x.md">the <code>docs</code></a>' in html
    assert "<b>&lt;i&gt;</b>" in html
    assert "<pre>&lt;script&gt;</pre>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html
    assert "<tr><td>1</td><td>2</td></tr>" in html and "---" not in html


def test_notes_reflow_and_trailers():
    body = "One line\nwrapped here.\n\n- a list\n- kept\n\nCo-Authored-By: X <x@example.com>"
    assert "<p>One line wrapped here.</p>" in source.notes(body)
    assert "- a list\n- kept" in source.notes(body)
    assert "Co-Authored-By" in source.notes(body)
    assert "Co-Authored-By" not in source.notes(body, trailers=False)


def test_links_that_leave_the_site_are_only_web_and_mail():
    assert source.outside("https://pyrtan.com/") == "https://pyrtan.com/" and source.outside("mailto:a@b.c") == "mailto:a@b.c"
    assert source.outside("#usage") == "#usage" and source.outside("docs/x.md") is None
    for bad in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<script>", "vbscript:x"):
        assert source.outside(bad) == "#", bad   # a Markdown file merged to main is no script on pyrtan.com


def test_releases_name_each_version_once(tmp_path, monkeypatch):
    """A release is a version main's own line said first, once: two branches that each
    bumped to 0.35.0 are one 0.35.0 (the first merged), and a branch that set its number
    in a merge of main gives that merge's message as the notes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    def version(v: str, message: str) -> None:
        (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{v}"\n')
        git("add", ".")
        git("commit", "-q", "-m", message)

    git("init", "-q", "-b", "main")
    version("0.34.2", "release 0.34.2")
    git("checkout", "-q", "-b", "ldap")
    version("0.35.0", "LDAP users; release 0.35.0")
    git("checkout", "-q", "-b", "game", "main")
    version("0.35.0", "The game across hubs; release 0.35.0")
    git("checkout", "-q", "-b", "late", "main")
    version("0.31.0", "Late guesses; release 0.31.0")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "Merge ldap", "ldap")
    git("merge", "-q", "--no-ff", "-m", "Merge game", "game")   # no version change on main: not a release
    git("checkout", "-q", "late")
    git("merge", "-q", "-X", "theirs", "-m", "Merge main into late; this is release 0.35.1", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.35.1"\n')
    git("commit", "-q", "-a", "--amend", "--no-edit")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "Merge late", "late")
    monkeypatch.setattr(source, "ROOT", repo)
    got = [(r["version"], r["subject"], r["notes"]["subject"]) for r in source.releases("HEAD")]
    assert got == [
        ("0.35.1", "Merge late", "Merge main into late; this is release 0.35.1"),
        ("0.35.0", "Merge ldap", "LDAP users; release 0.35.0"),
        ("0.34.2", "release 0.34.2", "release 0.34.2"),
    ]  # 0.31.0 was never main's, and the game's 0.35.0 is not a second one
