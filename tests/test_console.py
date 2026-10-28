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


GAPPY = """
name = "gappy"
description = "a decide with no else, and no I/O"
start = "d"

[[triggers]]
pattern = "pick {n:int}"

[inputs.n]
type = "int"

[nodes.d]
kind = "decide"
on = "inputs.n"
branches = [ { when = { eq = 1 }, then = "ok" } ]

[nodes.ok]
kind = "done"
say = "one"
"""

KEEP = r"""
const Pyrtan = require(process.argv[2]);
const tree = JSON.parse(process.argv[3]);
const disk = {};
const keep = (d) => ({ load: () => (disk[d] ? JSON.parse(disk[d]) : null), save: (v) => { disk[d] = JSON.stringify(v); }, clear: () => { delete disk[d]; } });
const gaps = async (hub) => (await hub.route("GET", "/gaps"))[1].gaps.map((g) => [g.observed, g.count, g.device]);
(async () => {
  const out = {};
  const a = new Pyrtan.LocalHub({ trees: [tree], device: "browser-1", keep: keep("browser-1") });
  for (const n of [2, 2, 3]) await a.route("POST", "/run", { tree: "gappy", inputs: { n } });
  await a.route("POST", "/intent", { text: "nobody answers this" });
  // The page again: what the first kept, and nothing counted twice.
  const b = new Pyrtan.LocalHub({ trees: [tree], device: "browser-1", keep: keep("browser-1") });
  out.reloaded = await gaps(b);
  out.unanswered = (await b.route("GET", "/unanswered"))[1].unanswered.map((e) => [e.text, e.count]);
  // A key is made: the gaps go to it, merged with what it kept here before.
  disk["pyr:k"] = JSON.stringify({ pyrtan: "kept", version: 1, device: "pyr:k", gaps: [{ tree: "gappy", node: "d", on: "inputs.n", observed: 2, count: 5, first_seen: 1, last_seen: 2 }], unanswered: [], suggestions: [] });
  b.rekey("pyr:k", keep("pyr:k"));
  out.rekeyed = await gaps(b);
  out.disk = Object.keys(disk);
  // Without anywhere to keep them, a hub keeps them as before, and its gaps name no device.
  const c = new Pyrtan.LocalHub({ trees: [tree] });
  await c.route("POST", "/run", { tree: "gappy", inputs: { n: 2 } });
  out.plain = await gaps(c);
  b.forget();
  out.forgot = [await gaps(new Pyrtan.LocalHub({ trees: [tree], device: "pyr:k", keep: keep("pyr:k") })), JSON.parse(disk["pyr:k"]).unanswered];
  // What is kept and cannot be read is no reason not to start.
  disk["x"] = "{not json";
  out.bad = await gaps(new Pyrtan.LocalHub({ trees: [tree], device: "x", keep: keep("x") }));
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_hub_in_a_page_keeps_its_gaps_under_the_device_that_met_them(tmp_path):
    (tmp_path / "gappy.toml").write_text(GAPPY, encoding="utf-8")
    tree = Registry.from_paths([tmp_path]).get("gappy").raw
    script = tmp_path / "keep.cjs"
    script.write_text(KEEP, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(ROOT / "src" / "pyrtan" / "js" / "pyrtan.js"), json.dumps(tree)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["reloaded"] == [[2, 2, "browser-1"], [3, 1, "browser-1"]]
    assert got["unanswered"] == [["nobody answers this", 1]]
    assert got["rekeyed"] == [[2, 7, "pyr:k"], [3, 1, "pyr:k"]]
    assert got["disk"] == ["pyr:k"]  # moved, not copied
    assert got["plain"] == [[2, 1, None]]
    assert got["forgot"] == [[], []]
    assert got["bad"] == []


RELOAD = r"""
const { chromium } = require("playwright");
const [file] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.route(/^https?:\/\//, (route) => route.abort());
  const ready = () => page.waitForFunction(() => document.querySelectorAll("#caps tbody tr").length > 0);
  const ask = async (q) => {
    const n = (await page.$$("#out .card")).length;
    await page.fill("#text", q);
    await page.click("#go");
    await page.waitForFunction((n) => document.querySelectorAll("#out .card").length > n, n);
  };
  const gaps = async () => {
    await page.click('#tabs a[data-tab="gaps"]');
    await page.waitForFunction(() => document.querySelectorAll("#gaps tbody tr").length > 0);
    return page.$$eval("#gaps tbody tr", (trs) => trs.map((t) => t.innerText));
  };
  await page.goto("file://" + file);
  await ready();
  await ask("pick 2");
  await ask("pick 2");
  const result = { device: await page.evaluate(() => localStorage.getItem("pyrtan.device")) };
  await page.reload();
  await ready();
  result.lede = await page.textContent("#connect-local");
  result.gaps = await gaps();
  result.where = await page.textContent("#gaps-device");
  page.once("dialog", (d) => d.accept());
  await page.click("#gaps-forget");
  await page.reload();
  await ready();
  result.forgot = await gaps();
  result.errors = errors;
  console.log(JSON.stringify(result));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
def test_a_console_keeps_its_gaps_across_a_reload(tmp_path):
    trees = tmp_path / "trees"
    trees.mkdir()
    (trees / "gappy.toml").write_text(GAPPY, encoding="utf-8")
    out = tmp_path / "console" / "index.html"
    assert main(["console", "--out", str(out), "--trees", str(trees)]) == 0
    script = tmp_path / "reload.cjs"
    script.write_text(RELOAD, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(out)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    device = got["device"]
    assert re.fullmatch(r"browser-[0-9a-f]{16}", device)
    assert device in got["lede"] and got["where"] == device
    assert len(got["gaps"]) == 1 and "gappy" in got["gaps"][0] and "2" in got["gaps"][0] and "this device" in got["gaps"][0]
    assert "\t2\t" in got["gaps"][0]  # asked twice, before the reload
    assert "No gaps" in got["forgot"][0]
    assert got["errors"] == []



STUCK = r"""
const { chromium } = require("playwright");
const [file, init] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.route(/^https?:\/\//, (route) => route.abort());
  await page.addInitScript(init);
  await page.goto("file://" + file);
  await page.waitForFunction(() => document.querySelectorAll("#caps tbody tr").length > 0, null, { timeout: 10000 });
  console.log(JSON.stringify({ who: await page.textContent("#who"), errors }));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""
# What one browser does and another does not, each of which used to leave the console at "…" with no trees.
BROKEN = {
    # IndexedDB that never answers, as Safari and private windows sometimes leave it: the key never comes.
    "the key store never answers": ("IDBFactory.prototype.open = () => ({});", 0),
    # The agents cannot start here.
    "the agents throw as they start": ("(() => { let v; Object.defineProperty(window, 'PyrtanAgents', { configurable: true, get() { return v; }, set(x) { x.Skills = class { constructor() { throw new TypeError('not here'); } }; v = x; } }); })();", 0),
    # One tree fails with an error that is not the tree's own: that tree is lost, not all of them.
    "one tree throws": ("(() => { let v; Object.defineProperty(window, 'Pyrtan', { configurable: true, get() { return v; }, set(x) { const add = x.LocalHub.prototype.add; x.LocalHub.prototype.add = function (raw, w) { if (raw && raw.name === 'weather') throw new TypeError('not here'); return add.call(this, raw, w); }; v = x; } }); })();", 1),
}


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
@pytest.mark.parametrize("case", list(BROKEN))
def test_a_console_holds_its_trees_when_something_in_the_browser_fails(tmp_path, case):
    init, lost = BROKEN[case]
    out = tmp_path / "console" / "index.html"
    assert main(["console", "--out", str(out)]) == 0
    script = tmp_path / "stuck.cjs"
    script.write_text(STUCK, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(out), init], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert f"{defaults.bundle()['count'] - lost} trees" in got["who"]
    assert got["errors"] == []
