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
