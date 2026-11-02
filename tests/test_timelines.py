"""Watch timelines: a month of what each watch's runs came to, as spans,
kept across restarts and served at /watches/history (by a service, and by a
hub for its services)."""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

from pyrtan.hub import serve_hub
from pyrtan.service import serve
from pyrtan.watch import TIMELINE_DAYS, Timelines

UP, DOWN = {"online": True}, {"online": False}


def test_spans_merge_abut_and_leave_gaps():
    t = Timelines()
    for at in (0, 60, 120):
        t.record("w", at, "done", UP, 60)
    t.record("w", 180, "done", DOWN, 60)        # a change: a new span from where the last one ended
    t.record("w", 240, "done", DOWN, 60)
    t.record("w", 1000, "done", DOWN, 60)       # 12 minutes of silence (the service was down): a gap, not "down"
    t.record("w", 1060, "error", None, 60)      # a run that did not finish is its own span
    spans = t.spans("w")
    assert [(s["from"], s["to"], s["n"], s["status"], s.get("output")) for s in spans] == [
        (0, 120, 3, "done", UP), (120, 240, 2, "done", DOWN), (1000, 1000, 1, "done", DOWN), (1000, 1060, 1, "error", None)]


def test_event_watches_abut_and_big_outputs_are_compared_not_kept():
    t = Timelines()
    big = {"devices": ["x" * 100] * 50}
    t.record("w", 0, "done", big, None)
    t.record("w", 5000, "done", big, None)       # no interval: the same outcome holds between events
    t.record("w", 9000, "done", {"devices": []}, None)
    spans = t.spans("w")
    assert [(s["from"], s["to"], s["n"]) for s in spans] == [(0, 5000, 2), (5000, 9000, 1)]
    assert "output" not in spans[0] and spans[1]["output"] == {"devices": []}


def test_a_month_is_kept():
    t = Timelines()
    t.record("w", 0, "done", UP, 60)
    t.record("w", 60, "done", DOWN, 60)
    now = (TIMELINE_DAYS + 1) * 86400
    t.record("w", now, "done", DOWN, 60)
    assert [s["from"] for s in t.spans("w")] == [now]


def test_timelines_survive_a_restart(tmp_path):
    path = tmp_path / "history.json"
    t = Timelines(path)
    t.record("lamp/kettle", 0, "done", UP, 60)
    t.record("lamp/kettle", 60, "done", DOWN, 60)  # a new span is written at once
    again = Timelines(path)
    assert [s["output"] for s in again.spans("lamp/kettle")] == [UP, DOWN]
    assert json.loads(path.read_text())["pyrtan-watch-history"] == 1
    # A file it cannot read starts afresh and says so; one it cannot write keeps the timeline in memory.
    path.write_text("{not json")
    notes: list[str] = []
    broken = Timelines(path, note=notes.append)
    assert broken.spans("lamp/kettle") == [] and broken.error and notes
    stuck = Timelines(tmp_path / "no-such-dir" / "h.json", note=notes.append)
    stuck.record("w", 0, "done", UP, 60)
    assert stuck.spans("w")[0]["output"] == UP and "no-such-dir" in stuck.error


TREE = """
name = "lamp"
description = "a device that is always up"
start = "say"

[[watches]]
name = "kettle"
every = 1

[nodes.say]
kind = "done"
say = "up"
output = { host = "192.168.1.40", online = true }
"""


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def test_services_and_hubs_serve_the_timelines(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "lamp.toml").write_text(TREE)
    history = tmp_path / "history.json"
    service, server = serve([tmp_path / "t"], port=0, quiet=True, watch=True, watch_history=history)
    hub, hub_server = serve_hub(port=0, quiet=True, poll_every=60)
    try:
        hub.add_peer(server.url.rstrip("/"), "mine")
        deadline = time.time() + 10
        while time.time() < deadline and not (_get(server.url.rstrip("/") + "/watches/history")["watches"][0]["spans"] or [{}])[0].get("n", 0) >= 2:
            time.sleep(0.2)
        own = _get(server.url.rstrip("/") + "/watches/history")
        w = own["watches"][0]
        assert (own["days"], w["tree"], w["name"], w["every"]) == (TIMELINE_DAYS, "lamp", "kettle", 1)
        assert len(w["spans"]) == 1 and w["spans"][0]["n"] >= 2 and w["spans"][0]["output"]["online"] is True
        via = _get(hub_server.url.rstrip("/") + "/watches/history")
        assert [(x["name"], x["peer"]) for x in via["watches"]] == [("kettle", server.url.rstrip("/"))]
        assert history.exists() and "lamp/kettle" in json.loads(history.read_text())["timelines"]
    finally:
        hub.stop()
        hub_server.stop()
        service.stop()
        server.stop()


# A calendar watch keeps time when no slot passes between two runs, so a
# weekend with no slots is not a gap, and a slot the service was down for is.

UPS = """
name = "ups"
description = "a UPS checked each weekday morning"
start = "say"

[[watches]]
name = "office ups"
at = "mon-fri 09:00"
tz = "Europe/Oslo"

[nodes.say]
kind = "done"
say = "on mains"
output = { host = "192.168.1.9", online = true }
"""


def _utc(*a: int) -> "dt.datetime":
    return dt.datetime(*a, tzinfo=dt.timezone.utc)


def test_a_calendar_watch_keeps_time_by_its_slots(tmp_path):
    from pyrtan.registry import Registry
    from pyrtan.service import Service
    from pyrtan.watch import CALENDAR_GRACE, Watch, Watcher

    (tmp_path / "ups.toml").write_text(UPS)
    reg = Registry.from_paths([tmp_path])
    now = [_utc(2026, 10, 30, 8, 0, 2)]
    watcher = Watcher(reg, Service(reg, name="s").run_watched, clock=lambda: now[0])
    w = Watch(watcher, "ups", 0, reg.get("ups").watches[0])
    watcher.watches[w.key] = w

    def run_at(slot: "dt.datetime") -> None:
        now[0] = slot + dt.timedelta(seconds=2)  # the run ends a moment after its slot
        w.fire({"kind": "at"}, slot=slot)

    run_at(_utc(2026, 10, 30, 8))   # Friday 09:00 in Oslo
    run_at(_utc(2026, 11, 2, 8))    # Monday: the weekend has no slots, so this one follows on
    run_at(_utc(2026, 11, 4, 8))    # Wednesday: Tuesday's slot passed with the service down
    spans = watcher.timelines.spans(w.timeline)
    t = lambda *a: _utc(*a).timestamp()  # noqa: E731
    assert [(s["from"], s["to"], s["n"]) for s in spans] == [
        (t(2026, 10, 30, 8, 0, 2), t(2026, 11, 2, 8, 0, 2), 2),
        (t(2026, 11, 4, 8, 0, 2), t(2026, 11, 4, 8, 0, 2), 1),   # a gap before it: Tuesday is unknown
    ]
    # The newest span holds until Thursday's slot, and a grace for its run.
    (h,) = watcher.history()["watches"]
    assert h["holds_until"] == t(2026, 11, 5, 8) + CALENDAR_GRACE and h["kind"] == "at"
    # A clock watch's holds two and a half intervals; an event watch's, for good.
    clock = Watch(watcher, "ups", 0, {"every": 60})
    assert clock.holds_until([{"to": 1000.0}]) == 1150.0 and clock.holds_until([]) is None
    assert Watch(watcher, "ups", 0, {"file": "/var/log/x"}).holds_until([{"to": 1000.0}]) is None


def test_the_console_draws_a_calendar_watch_until_its_next_slot():
    import shutil
    import subprocess

    import pytest

    from pyrtan.page import default_page

    if shutil.which("node") is None:
        pytest.skip("no node")
    page = default_page()
    start = page.index("function stretches(")
    body = page[start:page.index("\n}\n", start) + 3]
    script = body + """
const day = 86400, spans = [{from: 0, to: 100, n: 2, status: "done", output: {online: true}}];
const show = (p) => p.map((x) => `${x.state} ${x.a}-${x.b}`).join(", ");
console.log(JSON.stringify([
  show(stretches(spans, null, 0, day / 2, day + 300)),        // before the next slot: up until now
  show(stretches(spans, null, 0, 2 * day, day + 300)),        // the slot passed with no run: unknown after the last run
  show(stretches(spans, 60, 0, 200, undefined)),              // an older service: the clock rule
  show(stretches(spans, 60, 0, 1000, undefined)),
]));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    assert json.loads(out) == ["up 0-43200", "up 0-100, unknown 100-172800", "up 0-200", "up 0-100, unknown 100-1000"]


def test_a_device_on_a_calendar_reads_late_only_past_its_slot():
    import shutil
    import subprocess

    import pytest

    from pyrtan.page import default_page

    if shutil.which("node") is None:
        pytest.skip("no node")
    page = default_page()
    consts = "\n".join(line for line in page.splitlines() if line.startswith(("const STALE_AFTER", "const CALENDAR_GRACE")))
    start = page.index("function devicesFrom(")
    body = page[start:page.index("\n}\n", start) + 3]
    script = consts + "\n" + body + """
const out = {host: "192.168.1.9", online: true};
const daily = {tree: "ups", name: "office ups", kind: "at", next_at: 90000, last_at: 3600, last: {output: out}};
const minute = {tree: "lamp", name: "lamp", kind: "every", last_at: 3600, last: {output: out}};
console.log(JSON.stringify([
  devicesFrom([daily], 50000)[0].state,          // hours since its check, but its next slot is still ahead
  devicesFrom([daily], 90000 + 301)[0].state,    // its slot and the grace have passed with no run
  devicesFrom([minute], 3600 + 601)[0].state,    // a clock watch: ten minutes
]));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    assert json.loads(out) == ["up", "stale", "stale"]
