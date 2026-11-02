"""The console's Devices tab: what the watched devices last said, in Chromium
against a running hub whose watches fire for real."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request

import pytest

from pyrtan.hub import serve_hub

DEVICE = """
name = "lamp-check"
description = "says what it is told: a stand-in for device-offline"
start = "say"

[inputs.host]
type = "str"
[inputs.name]
type = "str"
[inputs.online]
type = "bool"
[inputs.since]
type = "str"

[[watches]]
name = "kettle"
every = 1
inputs = { host = "192.168.1.40", name = "Kettle plug", online = true, since = "2026-01-01T08:00:00Z" }

[[watches]]
name = "printer"
every = 1
inputs = { host = "192.168.1.50", name = "The printer", online = false, since = "2026-01-01T09:30:00Z" }

[nodes.say]
kind = "done"
say = "{{ inputs.name }}"
output = { host = "{{ inputs.host }}", name = "{{ inputs.name }}", online = "{{ inputs.online }}", since = "{{ inputs.since }}" }
"""

OTHER = """
name = "tick"
description = "a watch that is not a device"
start = "say"

[[watches]]
every = 1

[nodes.say]
kind = "done"
say = "tick"
"""

SCRIPT = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  const out = {};
  try {
    for (const [label, viewport] of [["wide", { width: 1200, height: 800 }], ["phone", { width: 375, height: 740 }]]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(process.argv[2] + "/#devices");
      await page.waitForSelector("#devices .device");
      out[label] = {
        cards: await page.$$eval("#devices .device", (els) => els.map((e) => ({ cls: e.className, text: e.innerText.replace(/\s+/g, " ").trim(), uptime: (e.querySelector(".uptime") || {}).innerText, bar: [...e.querySelectorAll("svg.bar rect")].map((r) => r.getAttribute("class")) }))),
        summary: (await page.textContent("#devices-summary")).trim(),
        overflow: await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth),
        errors,
      };
      await page.click('#tab-devices .range button[data-range="604800"]');
      await page.waitForFunction(() => document.querySelector("#devices .device .range-name").textContent === "7 d");
      out[label].pressed = await page.$$eval("#tab-devices .range button", (bs) => bs.map((b) => b.getAttribute("aria-pressed")));
      await page.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1); });
"""


def _node_playwright() -> bool:
    return shutil.which("node") is not None and subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="needs node with playwright, and a Chromium")
def test_the_devices_tab_shows_what_each_watched_device_last_said(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "lamp-check.toml").write_text(DEVICE)
    (tmp_path / "t" / "tick.toml").write_text(OTHER)
    hub, srv = serve_hub(port=0, quiet=True, trees=[tmp_path / "t"], watch=True, poll_every=60)
    url = srv.url.rstrip("/")
    try:
        deadline = time.time() + 15
        while time.time() < deadline:  # until each watch has fired once
            with urllib.request.urlopen(url + "/watches", timeout=5) as r:
                w = json.load(r)
            if w["count"] == 3 and all(x["last"] for x in w["watches"]):
                break
            time.sleep(0.3)
        (tmp_path / "s.cjs").write_text(SCRIPT)
        run = subprocess.run(["node", str(tmp_path / "s.cjs"), url], capture_output=True, text=True, timeout=120)
        got = json.loads(run.stdout.strip().splitlines()[-1])
    finally:
        hub.stop()
        srv.stop()
    assert "error" not in got, got
    for label in ("wide", "phone"):
        view = got[label]
        assert view["errors"] == [] and view["overflow"] is False, (label, view)
        assert view["summary"] == "1 online · 1 offline"
        # Offline first; the tick watch is not a device.
        assert [c["cls"] for c in view["cards"]] == ["device down", "device up"]
        printer, kettle = (c["text"] for c in view["cards"])
        assert printer.startswith("The printer 192.168.1.50 Offline for ") and "checked " in printer
        assert kettle.startswith("Kettle plug 192.168.1.40 Online for ")
        # A few seconds of history: all of it up for the kettle, down for the printer, the rest of the day unchecked.
        kettle_card, printer_card = view["cards"][1], view["cards"][0]
        assert kettle_card["uptime"].replace("\n", " ").startswith("100% up of ") and printer_card["uptime"].startswith("0.0% up of ")
        assert kettle_card["bar"][0] == "unknown" and kettle_card["bar"][-1] == "up" and printer_card["bar"][-1] == "down"
        assert view["pressed"] == ["false", "true", "false"]


PRINTER = """
name = "printer-check"
description = "a printer that is up now"
start = "say"

[[watches]]
name = "printer"
every = 60

[nodes.say]
kind = "done"
say = "up"
output = { host = "192.168.1.50", name = "The printer", online = true, since = "2026-11-01T00:00:00Z" }
"""

DETAIL = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  const out = {};
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(process.argv[2] + "/#devices");
    await page.waitForSelector("#devices .device .name a");
    await page.click("#devices .device .name a");
    await page.waitForSelector("#device-detail .stats");
    const read = async () => ({
      hash: await page.evaluate(() => decodeURIComponent(location.hash)),
      title: (await page.textContent("#device-detail h3")).trim(),
      stats: await page.$$eval("#device-detail .stat", (els) => els.map((e) => e.innerText.replace(/\s+/g, " ").trim())),
      summary: await page.$eval("#device-detail table.outages", (t) => t.previousElementSibling.textContent.trim()),
      rows: await page.$$eval("#device-detail table.outages tbody tr", (rs) => rs.map((r) => [...r.cells].map((c) => c.textContent.trim()))),
      grid: await page.isVisible("#devices-all"),
      overflow: await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth),
    });
    out.clicked = await read();
    await page.goBack();
    await page.waitForSelector("#devices-all", { state: "visible" });
    out.back = { grid: await page.isVisible("#devices-all"), detail: await page.isVisible("#device-detail") };
    await page.goto(process.argv[2] + "/" + out.clicked.hash.replace(/^#devices\//, "#devices/" ).replace(/ /g, "%20"));
    await page.reload();
    await page.waitForSelector("#device-detail .stats");
    out.linked = (await page.textContent("#device-detail h3")).trim();
    out.errors = errors;
  } finally { await browser.close(); }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="needs node with playwright, and a Chromium")
def test_a_device_page_shows_its_uptime_and_outages(tmp_path):
    now, H, D = time.time(), 3600, 86400

    def span(a, b, online):
        return {"from": now - a, "to": now - b, "n": 1, "status": "done", "h": f"{online}{a}", "output": {"host": "192.168.1.50", "name": "The printer", "online": online}}

    history = tmp_path / "history.json"
    history.write_text(json.dumps({"pyrtan-watch-history": 1, "timelines": {"printer-check/printer": [
        span(20 * D, 3 * D, True), span(3 * D, 3 * D - 2 * H, False), span(3 * D - 2 * H, 5 * H, True),
        span(5 * H, 4.5 * H, False), span(4.5 * H, 0, True)]}}))
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "printer-check.toml").write_text(PRINTER)
    hub, srv = serve_hub(port=0, quiet=True, trees=[tmp_path / "t"], watch=True, poll_every=60, watch_history=history)
    url = srv.url.rstrip("/")
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            with urllib.request.urlopen(url + "/watches", timeout=5) as r:
                if all(x["last"] for x in json.load(r)["watches"]):
                    break
            time.sleep(0.2)
        (tmp_path / "d.cjs").write_text(DETAIL)
        run = subprocess.run(["node", str(tmp_path / "d.cjs"), url], capture_output=True, text=True, timeout=120)
        got = json.loads(run.stdout.strip().splitlines()[-1])
    finally:
        hub.stop()
        srv.stop()
    assert "error" not in got and got["errors"] == [], got
    c = got["clicked"]
    assert c["hash"] == "#devices/local printer-check printer" and c["title"] == "The printer" and c["grid"] is False and c["overflow"] is False
    # 24 h: the half-hour outage; 7 and 30 d: both (and only 20 of the 30 days were checked).
    assert [s.split(" ")[3] for s in c["stats"]] == ["97.9%", "98.5%", "99.48%"], c["stats"]
    assert c["stats"][0].endswith("1 outage") and "2 outages" in c["stats"][1] and c["stats"][2].endswith("checked")
    assert c["summary"] == "2 in the last 30 days · 2 h 30 min offline in all · the longest 2 h"
    assert [row[2] for row in c["rows"]] == ["30 min", "2 h"]  # newest first
    assert got["back"] == {"grid": True, "detail": False}
    assert got["linked"] == "The printer"  # a device's page is a link of its own
