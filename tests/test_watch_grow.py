"""Watches (a tree that runs when something speaks) and growing a tree from
a gap (the branch drafted by rules for a person to accept)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from pyrtan import Registry
from pyrtan.cli import main
from pyrtan.grow import Draft, Unanswered, apply_draft, draft_branch
from pyrtan.service import Server, Service, http_json
from pyrtan.tree import TreeError, check, load_tree, load_tree_text
from pyrtan.watch import Watcher
from test_adapters import Broker

LINES = '''
name = "lines"
description = "counts the lines a watch brought"
paths = ["{root}"]
start = "count"

[inputs.text]
type = "str"

[inputs.n]
type = "int"
default = 0

[[watches]]
name = "log"
file = "{log}"
inputs = {{ text = "{{{{ event.body | strip }}}}", n = "{{{{ event.lines | length }}}}" }}

[nodes.count]
kind = "decide"
on = "inputs.n"
branches = [
  {{ when = {{ eq = 1 }}, then = "one" }},
  {{ when = {{ gt = 1 }}, then = "many" }},
  {{ else = "none" }},
]

[nodes.one]
kind = "done"
say = "one line: {{{{ inputs.text }}}}"

[nodes.many]
kind = "done"
say = "{{{{ inputs.n }}}} lines"

[nodes.none]
kind = "fail"
reason = "nothing"
say = "nothing"
'''

TICK = '''
name = "tick"
description = "reads a file on a clock; the path changes when the file does"
paths = ["{root}"]
start = "read"

[[watches]]
name = "clock"
every = 1

[nodes.read]
kind = "observe"
file = {{ path = "{flag}" }}
into = "f"
next = "there"

[nodes.there]
kind = "decide"
on = "f.exists"
branches = [{{ when = {{ eq = true }}, then = "yes" }}, {{ else = "no" }}]

[nodes.yes]
kind = "done"
say = "there"

[nodes.no]
kind = "done"
say = "gone"
'''


def wait_for(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def registry(tmp_path: Path, **trees: str) -> Registry:
    d = tmp_path / "trees"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in trees.items():
        (d / f"{name}.toml").write_text(text)
    reg = Registry.from_paths([d])
    assert not reg.errors, reg.errors
    return reg


# ---------------------------------------------------------------- watches


def test_watches_are_checked():
    base = LINES.format(root="/tmp", log="/tmp/x.log")
    tree = load_tree_text(base)
    assert tree.watch_kinds() == ["file"] and tree.capability()["watches"] == [{"kind": "file", "what": "/tmp/x.log"}]
    with pytest.raises(TreeError, match="exactly one of every, file, queue, serial"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'file = "/tmp/x.log"\nevery = "5m"'))
    with pytest.raises(TreeError, match="not under this tree's paths"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'file = "/etc/x.log"'))
    with pytest.raises(TreeError, match="literal path"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'file = "/tmp/{{ env.x }}"'))
    with pytest.raises(TreeError, match="template root 'state' is not one of event, env, now"):
        load_tree_text(base.replace("{{ event.body | strip }}", "{{ state.body }}"))
    with pytest.raises(TreeError, match="input 'zzz' is not declared"):
        load_tree_text(base.replace("n = \"{{ event.lines | length }}\"", "n = \"{{ event.lines | length }}\", zzz = 1"))
    with pytest.raises(TreeError, match="input 'text' is required and the watch does not give it"):
        load_tree_text(base.replace('inputs = { text = "{{ event.body | strip }}", n = "{{ event.lines | length }}" }', 'inputs = { n = 1 }'))
    with pytest.raises(TreeError, match="'every' is a duration"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'every = "soon"').replace('paths = ["/tmp"]', ""))
    ok = load_tree_text(base.replace('file = "/tmp/x.log"', 'every = "1 hour and a half"').replace('paths = ["/tmp"]', ""))
    assert ok.capability()["watches"] == [{"kind": "every", "what": "1 hour and a half"}]
    with pytest.raises(TreeError, match="not under this tree's topics"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'queue = { url = "mqtt://b", topic = "x/y" }').replace('paths = ["/tmp"]', 'hosts = ["b"]\ntopics = ["a/#"]'))
    with pytest.raises(TreeError, match="host 'c' is not in this tree's hosts"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'queue = { url = "mqtt://c", topic = "a/y" }').replace('paths = ["/tmp"]', 'hosts = ["b"]\ntopics = ["a/#"]'))
    with pytest.raises(TreeError, match="not in this tree's devices"):
        load_tree_text(base.replace('file = "/tmp/x.log"', 'serial = { device = "/dev/ttyACM0" }').replace('paths = ["/tmp"]', 'devices = ["/dev/ttyUSB*"]'))
    # A watch on a source counts as using it: the list is required.
    with pytest.raises(TreeError, match="must list the paths"):
        load_tree_text(base.replace('paths = ["/tmp"]', ""))


def test_file_watch_runs_the_tree_for_appended_lines(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("old line\n")
    reg = registry(tmp_path, lines=LINES.format(root=str(tmp_path), log=str(log)))
    svc = Service(reg, name="w")
    fired: list = []
    watcher = Watcher(reg, svc.run_watched, after=lambda w, e, o: fired.append((w.name, e, o))).start()
    try:
        assert len(watcher) == 1 and watcher.status()["watches"][0]["what"] == str(log)
        time.sleep(0.3)  # the watch starts at the end: the old line is not an event
        with log.open("a") as f:
            f.write("hello\n")
        assert wait_for(lambda: len(fired) == 1)
        name, event, out = fired[0]
        assert name == "log" and event["lines"] == ["hello"] and out.status == "done" and out.say == "one line: hello"
        assert out.trace.steps[0].kind == "watch" and out.trace.steps[0].node == "watch" and out.trace.steps[0].next == "count"
        assert "hello" in out.trace.steps[0].summary or "1 new line" in out.trace.steps[0].summary
        with log.open("a") as f:
            f.write("a\nb\nc\n")
        assert wait_for(lambda: len(fired) == 2) and fired[1][2].say == "3 lines"
        # Truncation (log rotation) is read from the start again.
        log.write_text("fresh\n")
        assert wait_for(lambda: len(fired) == 3) and fired[2][2].say == "one line: fresh"
        st = watcher.status()["watches"][0]
        assert st["fires"] == 3 and st["last"]["status"] == "done" and st["path"] == ["watch", "count", "one"] and st["alive"]
        # The path changed between runs 2 and 3 (many -> one), and the service counted the runs.
        assert st["path_changed_at"] is not None and st["previous_path"] == ["watch", "count", "many"]
        assert svc.runs == 3
    finally:
        watcher.stop()


def test_clock_watch_notices_the_path_change_and_sync_follows_the_files(tmp_path):
    flag = tmp_path / "flag"
    reg = registry(tmp_path, tick=TICK.format(root=str(tmp_path), flag=str(flag)))
    svc = Service(reg, name="w")
    fired: list = []
    watcher = Watcher(reg, svc.run_watched, after=lambda w, e, o: fired.append(o)).start()
    try:
        assert wait_for(lambda: len(fired) >= 1) and fired[0].say == "gone" and fired[0].trace.steps[0].summary == "every 1 s, run 1"
        flag.write_text("x")
        assert wait_for(lambda: any(o.say == "there" for o in fired), 5)
        st = watcher.status()["watches"][0]
        assert st["path_changed_at"] and st["previous_path"] == ["watch", "read", "there", "no"] and st["path"] == ["watch", "read", "there", "yes"]
        # A tree that vanishes stops its watch; one that appears starts one.
        (tmp_path / "trees" / "tick.toml").unlink()
        assert watcher.sync() == (0, 1) and len(watcher) == 0
        (tmp_path / "trees" / "tick.toml").write_text(TICK.format(root=str(tmp_path), flag=str(flag)))
        assert watcher.sync() == (1, 0) and len(watcher) == 1
    finally:
        watcher.stop()


def test_queue_watch_runs_per_message_and_serial_watch_per_line(tmp_path):
    b = Broker(message=None)
    tree = f'''
name = "sensor"
description = "x"
hosts = ["127.0.0.1"]
topics = ["home/#"]
start = "say"

[inputs.reading]
type = "str"

[[watches]]
queue = {{ url = "{b.url}", topic = "home/+/temperature" }}
inputs = {{ reading = "{{{{ event.topic }}}}={{{{ event.json.temperature }}}}" }}

[nodes.say]
kind = "done"
say = "{{{{ inputs.reading }}}}"
'''
    reg = registry(tmp_path, sensor=tree)
    svc = Service(reg, name="w")
    fired: list = []
    watcher = Watcher(reg, svc.run_watched, after=lambda w, e, o: fired.append(o)).start()
    try:
        assert wait_for(lambda: b.subscribed == ["home/+/temperature"])
        b.publish_to_subscribers("home/kitchen/temperature", b'{"temperature": 21}')
        assert wait_for(lambda: len(fired) == 1) and fired[0].say == "home/kitchen/temperature=21"
        b.publish_to_subscribers("home/attic/temperature", b'{"temperature": 30}')
        assert wait_for(lambda: len(fired) == 2) and fired[1].say == "home/attic/temperature=30"
    finally:
        watcher.stop()
        b.close()
    # Serial: a pty, each line an event.
    master, slave = os.openpty()
    device = os.ttyname(slave)
    stree = f'''
name = "modem"
description = "x"
devices = ["/dev/pts/*"]
start = "say"

[inputs.line]
type = "str"

[[watches]]
serial = {{ device = "{device}", baud = 9600, read_until = "\\n" }}
inputs = {{ line = "{{{{ event.body | strip }}}}" }}

[nodes.say]
kind = "done"
say = "line: {{{{ inputs.line }}}}"
'''
    reg2 = registry(tmp_path / "s", modem=stree)
    svc2 = Service(reg2, name="w2")
    fired2: list = []
    watcher2 = Watcher(reg2, svc2.run_watched, after=lambda w, e, o: fired2.append(o)).start()
    try:
        time.sleep(0.3)
        os.write(master, b"RING\r\nOK\r\n")
        assert wait_for(lambda: len(fired2) == 2) and [o.say for o in fired2] == ["line: RING", "line: OK"]
    finally:
        watcher2.stop()
        os.close(master)
        os.close(slave)


def test_hosted_service_watches_no_file_and_errors_are_shown(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("")
    reg = registry(tmp_path, lines=LINES.format(root=str(tmp_path), log=str(log)))
    svc = Service(reg, name="w", egress="public")
    notes: list[str] = []
    watcher = Watcher(reg, svc.run_watched, egress="public", note=notes.append).start()
    try:
        assert wait_for(lambda: notes)
        assert "watches no file" in notes[0]
        st = watcher.status()["watches"][0]
        assert st["errors"] == 1 and "watches no file" in st["last_error"]
    finally:
        watcher.stop()


def test_watches_over_http_and_the_commands(tmp_path, capsys):
    log = tmp_path / "x.log"
    log.write_text("")
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "lines.toml").write_text(LINES.format(root=str(tmp_path), log=str(log)))
    from pyrtan.hub import serve_hub
    from pyrtan.service import serve

    hub, hub_srv = serve_hub(port=0, quiet=True, poll_every=60)
    svc, srv = serve([tmp_path / "t"], port=0, quiet=True, watch=True, hub=hub_srv.url)
    try:
        assert srv.url in {p.url for p in hub.peers.values()}
        with log.open("a") as f:
            f.write("ping\n")
        assert wait_for(lambda: http_json("GET", f"{srv.url}/watches")[1]["watches"][0]["fires"] == 1)
        _, w = http_json("GET", f"{srv.url}/watches")
        assert w["count"] == 1 and w["watches"][0]["last"]["say"] == "one line: ping"
        _, hw = http_json("GET", f"{hub_srv.url}/watches")
        assert hw["count"] == 1 and hw["watches"][0]["peer"] == srv.url
        assert "/watches" in http_json("GET", f"{srv.url}/api")[1]["endpoints"]
        assert main(["watches", "--at", hub_srv.url]) == 0
        out = capsys.readouterr().out
        assert "log  file " in out and "fired 1×" in out and "one line: ping" in out
        assert main(["watches", "--at", hub_srv.url, "--json"]) == 0 and json.loads(capsys.readouterr().out)["watches"][0]["name"] == "log"
        # A service without --watch watches nothing, and says so.
        svc2, srv2 = serve([tmp_path / "t"], port=0, quiet=True)
        try:
            assert main(["watches", "--at", srv2.url]) == 0 and "watches nothing" in capsys.readouterr().out
        finally:
            srv2.stop()
    finally:
        svc.stop()
        srv.stop()
        hub.stop()
        hub_srv.stop()


def test_unanswered_survive_a_restart(tmp_path):
    path = tmp_path / "unanswered.jsonl"
    u = Unanswered(path=path)
    u.record("top 3 stories on lobsters")
    u.record("top 3 stories on lobsters", peer="x")
    u.record("is the printer on")
    again = Unanswered(path=path)
    rows = again.to_list()
    assert [(r["text"], r["count"]) for r in rows] == [("top 3 stories on lobsters", 2), ("is the printer on", 1)]
    assert rows[0]["proposal"]["name"] == "top-stories-lobsters"
    (tmp_path / "none").mkdir()
    svc = Service(Registry.from_paths([tmp_path / "none"]), name="s", unanswered=path)
    assert len(svc.unanswered) == 2


# ---------------------------------------------------------------- growing from a gap


WEATHERISH = '''
name = "sky"
description = "a code into a word"
start = "pick"

[inputs.code]
type = "int"

[nodes.pick]
kind = "decide"
on = "inputs.code"
branches = [
  { when = { eq = 0 }, then = "clear" },
  { when = { between = [1, 3] }, then = "cloudy" },
  { when = { between = [51, 67] }, then = "rain" },
  { when = { between = [95, 99] }, then = "storm" },
]

[nodes.clear]
kind = "done"
say = "clear"

[nodes.cloudy]
kind = "done"
say = "cloudy"

[nodes.rain]
kind = "done"
say = "rain"

[nodes.storm]
kind = "done"
say = "storm"
'''


def test_draft_branch_rules():
    tree = load_tree_text(WEATHERISH)
    # A hole between ranges: the range of the hole.
    d = draft_branch(tree, {"tree": "sky", "node": "pick", "on": "inputs.code", "observed": 71})
    assert d is not None and d.when == {"between": [68, 94]} and d.then == "pick-71" and "hole between 67 and 95" in d.reason
    assert d.branch == '{ when = { between = [68, 94] }, then = "pick-71" },'
    assert d.node_text.startswith("# Drafted by pyrtan grow") and '[nodes.pick-71]\nkind = "fail"\nreason = "71"' in d.node_text
    # Beyond the last range: just the value.
    assert draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": 120}).when == {"eq": 120}
    # Covered already: nothing to draft.
    assert draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": 2}) is None
    # Not a decide, or no such node: nothing.
    assert draft_branch(tree, {"node": "clear", "on": "x", "observed": 1}) is None and draft_branch(tree, {"node": "nope", "observed": 1}) is None
    # Missing, strings, types from a report.
    assert draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": None}).when == {"exists": False}
    s = draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": "N/A"})
    assert s.when == {"eq": "N/A"} and s.then == "pick-n-a" and s.branch == '{ when = { eq = "N/A" }, then = "pick-n-a" },'
    assert draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": "<dict>"}).when == {"type": "dict"}
    assert draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": True}).when == {"eq": True}
    # A node name already taken gets a suffix.
    taken = load_tree_text(WEATHERISH.replace('[nodes.storm]', '[nodes.pick-71]\nkind = "done"\nsay = "x"\n\n[nodes.storm]'))
    assert draft_branch(taken, {"node": "pick", "on": "inputs.code", "observed": 71}).then == "pick-71-2"


def test_apply_draft_edits_the_file_and_reverts_when_it_does_not_fit(tmp_path):
    path = tmp_path / "sky.toml"
    path.write_text(WEATHERISH)
    tree = load_tree(path)
    d = draft_branch(tree, {"node": "pick", "on": "inputs.code", "observed": 71})
    text = apply_draft(path, d)
    grown = load_tree(path)
    assert "pick-71" in grown.nodes and grown.nodes["pick-71"].kind == "fail"
    assert [b.get("then") for b in grown.nodes["pick"].get("branches")] == ["clear", "cloudy", "rain", "storm", "pick-71"]
    assert "  { when = { between = [68, 94] }, then = \"pick-71\" },\n]" in text
    assert not [p for p in check(grown) if p.level == "error"]
    # Before an else, and on a one-line list.
    one = tmp_path / "one.toml"
    one.write_text(WEATHERISH.replace("  { when = { between = [95, 99] }, then = \"storm\" },\n]", "  { when = { between = [95, 99] }, then = \"storm\" },\n  { else = \"storm\" },\n]"))
    apply_draft(one, draft_branch(load_tree(one), {"node": "pick", "on": "inputs.code", "observed": 71}))
    assert [b.get("then") or "else" for b in load_tree(one).nodes["pick"].get("branches")] == ["clear", "cloudy", "rain", "storm", "pick-71", "else"]
    flat = tmp_path / "flat.toml"
    flat.write_text('name = "f"\ndescription = "x"\nstart = "p"\n[inputs.v]\ntype = "str"\n[nodes.p]\nkind = "decide"\non = "inputs.v"\nbranches = [{ when = { eq = "a" }, then = "a" }, { else = "b" }]\n[nodes.a]\nkind = "done"\nsay = "a"\n[nodes.b]\nkind = "done"\nsay = "b"\n')
    apply_draft(flat, draft_branch(load_tree(flat), {"node": "p", "on": "inputs.v", "observed": "zz"}))
    assert [b.get("then") or "else" for b in load_tree(flat).nodes["p"].get("branches")] == ["a", "p-zz", "else"]
    # A draft that cannot fit (a node the file has no table for) leaves the file as it was.
    before = path.read_text()
    with pytest.raises(TreeError, match="no \\[nodes.nope\\] table"):
        apply_draft(path, Draft("sky", "nope", "x", 1, {"eq": 1}, "nope-1", '{ when = { eq = 1 }, then = "nope-1" },', '[nodes.nope-1]\nkind = "done"\n'))
    assert path.read_text() == before
    bad = Draft("sky", "pick", "inputs.code", 5, {"eq": 5}, "pick-5", '{ when = { eq = 5 }, then = "pick-5" },', '[nodes.pick-5]\nkind = "done"\nnext = "nowhere"\n')
    with pytest.raises(TreeError, match="did not fit the file"):
        apply_draft(path, bad)
    assert path.read_text() == before


def test_grow_command(tmp_path, capsys):
    (tmp_path / "t").mkdir()
    path = tmp_path / "t" / "sky.toml"
    path.write_text(WEATHERISH)
    gaps = tmp_path / "gaps.jsonl"
    gaps.write_text(json.dumps({"tree": "sky", "node": "pick", "on": "inputs.code", "observed": 71, "count": 3}) + "\n" + json.dumps({"tree": "sky", "node": "pick", "on": "inputs.code", "observed": 2}) + "\n" + json.dumps({"tree": "other", "node": "x", "on": "y", "observed": 1}) + "\n")
    assert main(["grow", "--from", str(gaps), "--trees", str(tmp_path / "t")]) == 0
    out = capsys.readouterr().out
    assert "skipped other: not among the trees here" in out and "is covered already" in out and "[nodes.pick-71]" in out and "nothing written" in out
    assert load_tree(path).nodes.get("pick-71") is None
    assert main(["grow", "--from", str(gaps), "--trees", str(tmp_path / "t"), "--apply"]) == 0
    assert "written into" in capsys.readouterr().out and load_tree(path).nodes["pick-71"].kind == "fail"
    assert main(["grow", "--from", str(gaps), "--trees", str(tmp_path / "t")]) == 1 and "every gap has its branch" in capsys.readouterr().out
    # From a federation report: the id names the tree; from a running service: its /gaps.
    reports = tmp_path / "reports.json"
    reports.write_text(json.dumps({"reports": [{"id": "pyrtan/sky@1", "node": "pick", "on": "inputs.code", "observed": 120, "count": 2, "reporters": 1}]}))
    assert main(["grow", "--reports", str(reports), "--trees", str(tmp_path / "t"), "--json"]) == 0
    drafts = json.loads(capsys.readouterr().out)["drafts"]
    assert drafts[0]["when"] == {"eq": 120} and drafts[0]["file"].endswith("sky.toml")
    svc = Service(Registry.from_paths([tmp_path / "t"]), name="s")
    srv = Server(svc.route, quiet=True).start()
    try:
        assert svc.run({"tree": "sky", "inputs": {"code": 42}})["reason"] == "gap"
        assert main(["grow", "--at", srv.url, "--trees", str(tmp_path / "t")]) == 0 and "pick-42" in capsys.readouterr().out
    finally:
        srv.stop()
    assert main(["grow", "--trees", str(tmp_path / "t")]) == 2
