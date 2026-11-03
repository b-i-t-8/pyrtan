"""Every route through a tree, listed without running it: ``pyrtan paths``."""

from __future__ import annotations

import json

from conftest import ROOT
from pyrtan.cli import main
from pyrtan.paths import render, routes, stops, sub_flows
from pyrtan.registry import Registry
from pyrtan.tree import load_tree_text

CODES = '''
name = "codes"
description = "status codes"
start = "status"

[inputs.code]
type = "int"

[nodes.status]
kind = "decide"
on = "inputs.code"
branches = [
  { when = { between = [200, 399] }, then = "word" },
  { when = { gte = 400 }, then = "down" },
  { when = { between = [500, 599] }, then = "down" },
]

[nodes.word]
kind = "lookup"
on = "inputs.code"
into = "w"
table = [ { eq = 200, value = "ok" }, { between = [300, 399], value = "moved" } ]
next = "up"

[nodes.up]
kind = "done"
say = "up: {{ state.w }}"

[nodes.down]
kind = "fail"
reason = "down"
say = "down"
'''


def test_every_route_with_its_branches_and_its_gaps():
    found, more = routes(load_tree_text(CODES))
    assert not more
    assert [(r.end, [leg.node for leg in r.legs]) for r in found] == [
        ("done", ["status", "word", "up"]),
        ("gap", ["status", "word"]),          # a code the lookup has no row for
        ("fail", ["status", "down"]),
        ("fail", ["status", "down"]),         # branches[2], which branches[1] always takes first
        ("gap", ["status"]),                  # no else: a code below 200
    ]
    assert found[0].legs[0].via == "inputs.code between [200, 399]"
    assert [r.never for r in found] == [False, False, False, True, False]
    assert found[4].says == "no branch takes it (whole numbers below 200): recorded as a gap"


def test_the_text_marks_what_is_never_taken():
    text = render(load_tree_text(CODES))
    assert text.splitlines()[0] == "codes: 5 route(s)"
    assert "4. status [inputs.code between [500, 599]] -> down: fail down  (never taken)" in text
    assert "word [inputs.code: no row]: gap no row takes it" in text


WALKER = '''
name = "walker"
description = "reads each page it is given, and asks before it posts"
hosts = ["api.example"]
start = "pages"

[inputs.ids]
type = "list"
default = [1]

[nodes.pages]
kind = "each"
over = "inputs.ids"
start = "one"
into = "got"
next = "post"

[nodes.one]
kind = "observe"
http = { url = "https://api.example/{{ item }}" }
into = "r"
expect = { status = { eq = 200 } }
next = "said"

[nodes.said]
kind = "done"
say = "{{ state.r.body }}"

[nodes.post]
kind = "act"
http = { method = "POST", url = "https://api.example/log", body = "{{ state.got | length }}" }
confirm = true
into = "p"
next = "done"

[nodes.done]
kind = "done"
say = "posted"
'''


def test_a_sub_flow_is_listed_beside_the_tree_and_what_can_stop_any_route():
    tree = load_tree_text(WALKER)
    assert sub_flows(tree) == ["one"]
    top, _ = routes(tree)
    assert len(top) == 1 and top[0].reads == ["pages: each of inputs.ids: sub-flow from one", "post: POST https://api.example/log (asks first)"]
    sub, _ = routes(tree, "one")
    assert sub[0].reads == ["one: GET https://api.example/{{ item }}"]
    assert stops(tree) == ["a request that fails (transport)", "an answer out of shape (drift: one)", "a confirmation answered no (declined)"]
    assert "each sub-flow from one:\n1. one -> said: done {{ state.r.body }}" in render(tree)


def test_a_limit():
    found, more = routes(load_tree_text(CODES), limit=2)
    assert len(found) == 2 and more
    assert render(load_tree_text(CODES), limit=2).endswith("… more than 2 routes; the rest are not listed")


def test_the_command(tmp_path, capsys):
    (tmp_path / "codes.toml").write_text(CODES)
    assert main(["paths", str(tmp_path / "codes.toml")]) == 0
    assert capsys.readouterr().out.startswith("codes: 5 route(s)\n1. status [inputs.code between [200, 399]] -> word -> up: done up:")
    assert main(["paths", str(tmp_path / "codes.toml"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tree"] == "codes" and [r["end"] for r in out["flows"]["status"]["routes"]] == ["done", "gap", "fail", "fail", "gap"]
    assert out["flows"]["status"]["routes"][3]["never"] is True
    assert main(["paths", str(tmp_path / "nothing")]) == 2


def test_every_example_ends_somewhere():
    reg = Registry.from_paths([ROOT / "examples", ROOT / "catalog"])
    for tree in reg.trees():
        for start in [tree.start] + sub_flows(tree):
            found, more = routes(tree, start)
            assert found and not more, tree.name
            assert all(r.end in ("done", "fail", "gap") for r in found), (tree.name, [r.says for r in found if r.end == "dead-end"])


DEVICES = '''
name = "devices"
description = "a PLC, a templated UPS, a key, a log, a program and a scan"
hosts = ["plc.local", "api.example"]
keys = ["devices/*"]
paths = ["/var/log"]
commands = ["uptime"]
networks = ["192.168.1.0/24"]
start = "plc"

[inputs.ups]
type = "str"
default = "ups"

[nodes.plc]
kind = "observe"
modbus = { url = "modbus://plc.local", read = "holding", address = 100 }
into = "level"
next = "ups"

[nodes.ups]
kind = "observe"
snmp = { url = "snmp://{{ inputs.ups }}.example", get = "sysName.0" }
into = "ups"
next = "seen"

[nodes.seen]
kind = "act"
memory = { key = "devices/{{ inputs.ups }}", write = "{{ state.ups.ok }}" }
into = "m"
next = "log"

[nodes.log]
kind = "observe"
file = { path = "/var/log/{{ inputs.ups }}.log" }
into = "l"
next = "up"

[nodes.up]
kind = "observe"
shell = { command = "uptime" }
into = "u"
next = "scan"

[nodes.scan]
kind = "observe"
discover = { scan = "192.168.1.0/24", ports = [502] }
into = "found"
next = "api"

[nodes.api]
kind = "observe"
http = { url = "https://api.example/x" }
into = "a"
next = "done"

[nodes.done]
kind = "done"
say = "ok"
'''


def test_what_can_stop_a_tree_of_devices():
    got = stops(load_tree_text(DEVICES))
    assert got == [
        "a request that fails (transport)",
        "a host decided at run time outside the tree's hosts (host-not-allowed: ups)",
        "a key decided at run time outside the tree's keys (key-not-allowed: seen)",
        "a path decided at run time outside the tree's paths (path-not-allowed: log)",
        # The PLC is on the LAN (plc.local); the API is not, and nor is a templated host it cannot tell.
        "a service started --egress public, which refuses the box's own files, programs, serial lines and memory, discovery and a private address (transport: seen, log, up, scan, plc)",
    ]


def test_a_spent_budget_reads_in_either_shape():
    # A runtime with budgets keeps a tree's as one table (declared budgets,
    # b-i-t-8/pyrtan#66) or a list of them (a host's budget, #79).
    tree = load_tree_text(WALKER)
    tree.budget = {"requests": 5, "per": 60, "said": "1m"}
    assert stops(tree)[-2] == "a spent budget (5 requests per 1m)"
    tree.budget = [{"requests": 10, "per": 60, "said": "1m", "host": None}, {"requests": 60, "per": 3600, "said": "1 hour", "host": "api.example"}]
    assert stops(tree)[-2] == "a spent budget (10 requests per 1m; 60 requests per 1 hour for api.example, shared)"


def test_a_stop_paths_lists_is_how_the_run_stops():
    from pyrtan import RecordedTransport, Registry, Runner

    reg = Registry()
    reg.add(load_tree_text(DEVICES))
    fixture = {"modbus modbus://plc.local unit 1 holding 100": {"ok": True, "value": 1}}
    out = Runner(reg, RecordedTransport(fixture)).run_tree("devices", {"ups": "elsewhere"})
    assert out.reason == "host-not-allowed" and out.trace.steps[-1].node in ("plc", "ups")  # snmp://elsewhere.example is not in hosts


def test_private_addresses_as_paths_reads_them():
    from pyrtan.paths import _private

    assert all(_private({"url": u}) for u in ["modbus://plc.local", "snmp://{{ inputs.ups }}.local", "coap://192.168.1.9/x", "ws://[::1]:5580/ws", "http://localhost:8080/", "mqtt://10.0.0.2"])
    assert not any(_private({"url": u}) for u in ["https://api.example/x", "snmp://{{ inputs.host }}", "bacnet://8.8.8.8", "nonsense"])
