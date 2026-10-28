"""Every console carries the default trees, so it holds them with no network."""

import json
import re
import shutil
import subprocess

import pytest

from pyrtan import Registry, defaults
from pyrtan.cli import main
from pyrtan.page import PAGE, page

from conftest import FIXTURES, ROOT

CARRIED = re.compile(r'<script id="pyrtan-trees" type="application/json">(.*?)</script>', re.S)


def carried(html: str) -> dict:
    m = CARRIED.search(html)
    assert m, "the console carries no trees"
    return json.loads(m.group(1))


def test_the_default_trees_are_the_examples_and_the_catalog():
    assert defaults.default_tree_dirs() == [ROOT / "examples", ROOT / "catalog"]
    b = defaults.bundle()
    ids = [e["id"] for e in b["trees"]]
    assert b["count"] == len(ids) and ids == sorted(ids)
    assert {"pyrtan/weather", "pyrtan/hacker-news", "postfinite/postfinite-overview", "postfinite/postfinite-forecasts"} <= set(ids)
    assert sorted(e["tree"]["name"] for e in b["trees"]) == Registry.from_paths(defaults.default_tree_dirs()).names()
    assert all(len(e["sha256"]) == 64 for e in b["trees"])


def test_an_installed_package_reads_the_trees_it_ships(tmp_path, monkeypatch):
    (tmp_path / "examples").mkdir()
    shutil.copy(ROOT / "examples" / "weather" / "weather.toml", tmp_path / "examples")
    monkeypatch.setattr(defaults, "PACKAGED", tmp_path)
    assert defaults.default_tree_dirs() == [tmp_path / "examples"]
    assert [e["id"] for e in defaults.bundle()["trees"]] == ["pyrtan/weather"]


def test_every_console_carries_the_default_trees():
    assert carried(PAGE) == json.loads(json.dumps(defaults.bundle(), default=str))
    assert "__TREES__" not in PAGE


def test_a_carried_tree_keeps_the_order_its_file_gives():
    tree = {"name": "ordered", "start": "s", "nodes": {"s": {"kind": "set", "set": {"z": "1", "a": "{{ state.z }}"}, "next": "d"}, "d": {"kind": "done"}}}
    got = carried(page({"trees": [{"id": "ordered", "tree": tree}]}))
    assert list(got["trees"][0]["tree"]["nodes"]["s"]["set"]) == ["z", "a"]


def test_a_tree_cannot_end_the_block_it_is_carried_in():
    tree = {"name": "sly", "description": "</script><script>alert(1)</script> <!-- x", "start": "a", "nodes": {"a": {"kind": "done"}}}
    html = page({"trees": [{"id": "sly", "tree": tree}]})
    assert html.count("</script>") == page({"trees": []}).count("</script>")
    assert carried(html)["trees"][0]["tree"] == tree


def test_pyrtan_console_writes_a_console_carrying_what_it_was_given(tmp_path, capsys):
    out = tmp_path / "site" / "index.html"
    assert main(["console", "--out", str(out), "--trees", str(ROOT / "examples" / "weather"), str(ROOT / "catalog" / "postfinite")]) == 0
    assert "carrying 8 trees" in capsys.readouterr().out
    ids = [e["id"] for e in carried(out.read_text(encoding="utf-8"))["trees"]]
    assert ids[-1] == "pyrtan/weather" and len(ids) == 8 and all(i.startswith("postfinite/") for i in ids[:-1])
    assert (out.parent / "pyrtan.js").exists() and (out.parent / "sw.js").exists()
    assert main(["console", "--out", str(out)]) == 0
    assert carried(out.read_text(encoding="utf-8"))["count"] == defaults.bundle()["count"]


DRIVE = r"""
const { chromium } = require("playwright");
const fs = require("fs");
const [file, fixture] = process.argv.slice(2);
const fx = JSON.parse(fs.readFileSync(fixture, "utf8"));
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  // No network: a tree's request is answered from the fixture, with the CORS a public API sends; anything else (a catalog) fails.
  await page.route(/^https?:\/\//, async (route) => {
    const req = route.request(), rec = fx[`${req.method()} ${req.url()}`];
    if (!rec) return route.abort();
    await route.fulfill({ status: rec.status || 200, body: typeof rec.body === "string" ? rec.body : JSON.stringify(rec.body), headers: { "access-control-allow-origin": "*" } });
  });
  await page.goto("file://" + file);
  await page.waitForFunction(() => document.querySelectorAll("#caps tbody tr").length > 0);
  const result = { who: await page.textContent("#who"), lede: await page.textContent("#connect-lede"), tries: await page.$$eval("#tries a", (as) => as.map((a) => a.dataset.q)), rows: await page.$$eval("#caps tbody tr", (trs) => trs.map((t) => t.innerText)), answers: [] };
  for (const q of ["Hey Pyrtan, what's the forecast in Oslo please?", "top 3 stories on hacker news", "postfinite"]) {
    const n = (await page.$$("#out .card")).length;
    await page.fill("#text", q);
    await page.click("#go");
    await page.waitForFunction((n) => document.querySelectorAll("#out .card").length > n, n);
    result.answers.push(await page.$eval("#out .card", (c) => c.innerText));
  }
  result.errors = errors;
  console.log(JSON.stringify(result));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _node_playwright() -> bool:
    if shutil.which("node") is None:
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
def test_a_console_opened_as_a_file_with_no_network_holds_its_own_trees(tmp_path):
    out = tmp_path / "console" / "index.html"
    assert main(["console", "--out", str(out)]) == 0
    script = tmp_path / "drive.cjs"
    script.write_text(DRIVE, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(out), str(FIXTURES / "examples.json")], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert f"{defaults.bundle()['count']} trees" in got["who"]
    assert "the trees this console carries" in got["lede"]
    # Offered: what a page can run. Not offered: what needs a box, or the operator's credentials.
    assert "what's the weather in Oslo" in got["tries"] and "how full is the disk" not in got["tries"]
    assert not [q for q in got["tries"] if "postfinite" in q]
    assert any("postfinite-overview" in r and "needs a hub: credentials" in r for r in got["rows"])
    weather, hn, postfinite = got["answers"]
    assert "Oslo, Norway: 4.2°C, raining" in weather
    assert "Top of Hacker News: Pyrtan 0.1" in hn
    assert "no-credentials" in postfinite and "--env postfinite_email=" in postfinite
    assert got["errors"] == []
