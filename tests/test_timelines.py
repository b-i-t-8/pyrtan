"""Watch timelines: a month of what each watch's runs came to, as spans,
kept across restarts and served at /watches/history (by a service, and by a
hub for its services)."""

from __future__ import annotations

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
