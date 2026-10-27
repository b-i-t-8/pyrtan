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
