"""An act that asks first (``confirm``), and a dry run, which asks before
every act and does none of them unasked."""

import json

import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.adapters import would
from pyrtan.cli import main
from pyrtan.hub import Hub, Local, Peer
from pyrtan.service import Service
from pyrtan.tree import TreeError, check, load_tree_text

LIGHT = '''
name = "light"
description = "switches a light, after saying so"
hosts = ["lights.example"]
start = "now"

[[triggers]]
pattern = "turn {state:word} the {room:word} light"

[inputs.state]
type = "str"
[inputs.room]
type = "str"

[nodes.now]
kind = "observe"
http = { url = "https://lights.example/{{ inputs.room }}" }
into = "cur"
next = "switch"

[nodes.switch]
kind = "act"
http = { method = "POST", url = "https://lights.example/{{ inputs.room }}", body = { on = "{{ inputs.state }}" } }
confirm = "Turn the {{ inputs.room }} light {{ inputs.state }}?"
into = "set"
next = "log"

[nodes.log]
kind = "act"
http = { method = "POST", url = "https://lights.example/log", body = "{{ inputs.room }} {{ inputs.state }}" }
into = "logged"
next = "done"

[nodes.done]
kind = "done"
say = "The {{ inputs.room }} light is {{ inputs.state }} ({{ state.set.status }})."
'''

FIXTURE = {
    "GET https://lights.example/kitchen": {"status": 200, "body": {"on": False}},
    "POST https://lights.example/kitchen": {"status": 204, "body": ""},
    "POST https://lights.example/log": {"status": 201, "body": ""},
}


def runner(*texts: str, fixture=FIXTURE, resolver=None) -> tuple[Runner, RecordedTransport]:
    reg = Registry()
    for t in texts:
        reg.add(load_tree_text(t))
    transport = RecordedTransport(json.loads(json.dumps(fixture)))
    return Runner(reg, transport, resolver=resolver), transport


def posts(transport: RecordedTransport) -> list[str]:
    return [url for method, url in transport.calls if method == "POST"]


def test_an_act_with_confirm_asks_first_and_yes_does_it():
    r, t = runner(LIGHT)
    out = r.run_intent("turn on the kitchen light")
    assert out.status == "asked" and out.question == "Turn the kitchen light on?" and posts(t) == []
    assert out.snapshot["confirm"] is True and out.snapshot["would"] == 'POST https://lights.example/kitchen with {"on": "on"}'
    assert out.trace.steps[-1].summary == 'asked: Turn the kitchen light on? (would POST https://lights.example/kitchen with {"on": "on"})'
    done = r.resume(out.snapshot, "Yes!")
    assert done.status == "done" and done.say == "The kitchen light is on (204)."
    assert posts(t) == ["https://lights.example/kitchen", "https://lights.example/log"]  # the log act has no confirm: it just runs
    assert [s.summary for s in done.trace.steps if s.node == "switch"][-2:] == ["answered: Yes!", "POST https://lights.example/kitchen -> 204"]


@pytest.mark.parametrize("answer", ["no", "", "not now", "maybe", None, False, "nope"])
def test_anything_but_a_plain_yes_declines(answer):
    r, t = runner(LIGHT)
    out = r.resume(r.run_intent("turn on the kitchen light").snapshot, answer)
    assert out.status == "failed" and out.reason == "declined" and out.say == "Not done: switch was not confirmed." and posts(t) == []


def test_confirm_true_asks_in_words_of_its_own():
    r, _ = runner(LIGHT.replace('confirm = "Turn the {{ inputs.room }} light {{ inputs.state }}?"', "confirm = true"))
    assert r.run_intent("turn on the kitchen light").question == 'switch would POST https://lights.example/kitchen with {"on": "on"}. Go ahead?'


def test_a_dry_run_asks_before_every_act_and_does_none_unasked():
    r, t = runner(LIGHT.replace('confirm = "Turn the {{ inputs.room }} light {{ inputs.state }}?"\n', ""))
    out = r.run_intent("turn on the kitchen light", dry_run=True)
    assert out.status == "asked" and out.question == 'Dry run: switch would POST https://lights.example/kitchen with {"on": "on"}. Go ahead?'
    assert [m for m, _ in t.calls] == ["GET"]  # it read, for real
    # Saying yes does that one act; the next one asks too, since the run is still a dry run.
    nxt = r.resume(out.snapshot, "yes")
    assert nxt.status == "asked" and nxt.question == 'Dry run: log would POST https://lights.example/log with "kitchen on". Go ahead?'
    assert posts(t) == ["https://lights.example/kitchen"]
    assert r.resume(nxt.snapshot, "yes").status == "done" and posts(t) == ["https://lights.example/kitchen", "https://lights.example/log"]


def test_a_dry_run_still_says_when_an_act_would_be_refused():
    r, t = runner(LIGHT.replace('url = "https://lights.example/{{ inputs.room }}", body', 'url = "https://{{ inputs.room }}/", body').replace('hosts = ["lights.example"]', 'hosts = ["lights.example", "*.example"]'))
    out = r.run_tree("light", {"state": "on", "room": "kitchen"}, dry_run=True)
    assert out.status == "error" and out.reason == "host-not-allowed" and posts(t) == []


CALLER = '''
name = "evening"
description = "lights for the evening"
start = "ask"
[[triggers]]
pattern = "evening"
[nodes.ask]
kind = "delegate"
tree = "light"
with = { state = "on", room = "kitchen" }
into = "l"
next = "done"
[nodes.done]
kind = "done"
say = "{{ state.l.say }}"
'''


def test_a_dry_run_stops_at_an_act_inside_a_delegated_tree():
    r, t = runner(LIGHT, CALLER)
    out = r.run_intent("evening", dry_run=True)
    assert out.status == "error" and out.reason == "dry-run" and posts(t) == []
    assert out.say == 'switch: would POST https://lights.example/kitchen with {"on": "on"}, but only a top-level run can stop to ask.'
    assert any(s.summary.startswith("would POST") and s.depth == 1 for s in out.trace.steps)
    # Not a dry run: the delegated tree's confirm cannot pause, and says so; nothing is posted.
    real = r.run_intent("evening")
    assert real.status == "done" and posts(t) == []
    assert real.say == 'switch: would POST https://lights.example/kitchen with {"on": "on"}, but only a top-level run can stop to ask.'


def test_a_dry_run_hands_nothing_to_the_swarm():
    class Resolver:
        called = []

        def run_tree(self, name, inputs, depth):
            self.called.append(name)

        def run_intent(self, text, depth):
            self.called.append(text)

    res = Resolver()
    r, _ = runner(CALLER.replace('tree = "light"', 'tree = "elsewhere"'), resolver=res)
    out = r.run_intent("evening", dry_run=True)
    assert out.reason == "dry-run" and "would run elsewhere where the swarm keeps it" in out.say and res.called == []
    assert r.run_intent("something nobody here answers", dry_run=True).reason == "dry-run" and res.called == []


def test_would_names_what_each_source_would_do():
    assert would("http", {"method": "put", "url": "https://x/y", "body": {"a": 1}}) == 'PUT https://x/y with {"a": 1}'
    assert would("file", {"path": "/tmp/n", "append": "line\n"}) == 'append "line\\n" to /tmp/n'
    assert would("queue", {"url": "mqtt://b", "topic": "home/light", "publish": "on"}) == 'publish "on" to home/light at mqtt://b'
    assert would("serial", {"device": "/dev/ttyUSB0", "write": "AT\r\n"}) == 'send "AT\\r\\n" to /dev/ttyUSB0'
    assert would("http", {"method": "POST", "url": "https://x", "body": "x" * 200}).endswith('…"')


def test_confirm_is_checked():
    def problems(text):
        try:
            return [str(p) for p in check(load_tree_text(text))]
        except TreeError as e:
            return [f"error: {e}"]

    assert not [p for p in problems(LIGHT) if p.startswith("error")]
    assert any("'confirm' is for an act" in p for p in problems(LIGHT.replace('into = "cur"', 'into = "cur"\nconfirm = true')))
    assert any("'confirm' is true, or the question" in p for p in problems(LIGHT.replace('confirm = "Turn the {{ inputs.room }} light {{ inputs.state }}?"', 'confirm = 3')))
    assert any("'confirm' is true, or the question" in p for p in problems(LIGHT.replace('confirm = "Turn the {{ inputs.room }} light {{ inputs.state }}?"', 'confirm = "  "')))


def test_a_service_and_a_hub_carry_a_dry_run_only_where_it_is_kept(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "light.toml").write_text(LIGHT.replace('confirm = "Turn the {{ inputs.room }} light {{ inputs.state }}?"\n', ""))
    svc = Service(Registry.from_paths([tmp_path / "t"]), name="s", transport=RecordedTransport(json.loads(json.dumps(FIXTURE))))
    assert svc.capabilities()["dry_run"] is True
    out = svc.run({"tree": "light", "inputs": {"state": "on", "room": "kitchen"}, "dry_run": True})
    assert out["status"] == "asked" and out["question"].startswith("Dry run: switch would POST")
    assert svc.intent({"text": "turn on the kitchen light", "dry_run": True})["status"] == "asked"
    # A hub forwards a dry run to its own trees and to a service that says it keeps one, and nowhere else.
    hub = Hub(name="h")
    assert hub.forward(Peer("http://old.example"), "/run", {"tree": "light", "dry_run": True})[1]["reason"] == "dry-run"
    assert hub.forward(Peer("http://old.example"), "/resume", {"snapshot": {"tree": "light", "dry_run": True}, "answer": "yes"})[1]["reason"] == "dry-run"
    local = Local(svc)
    assert hub.forward(local, "/run", {"tree": "light", "inputs": {"state": "on", "room": "kitchen"}, "dry_run": True})[1]["status"] == "asked"


def test_the_run_command_does_a_dry_run(tmp_path, capsys):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "light.toml").write_text(LIGHT)
    (tmp_path / "fx.json").write_text(json.dumps({"GET https://lights.example/kitchen": FIXTURE["GET https://lights.example/kitchen"]}))  # no POST: one would fail the run
    assert main(["run", "turn on the kitchen light", "--trees", str(tmp_path / "t"), "--replay", str(tmp_path / "fx.json"), "--dry-run", "--no-trace"]) == 0
    assert capsys.readouterr().out.strip() == 'dry run: switch would POST https://lights.example/kitchen with {"on": "on"}; nothing was changed'


def test_a_tree_test_can_answer_a_confirm(tmp_path):
    from pyrtan.cases import cases, failures, run_case, with_tree
    from pyrtan.tree import load_tree

    (tmp_path / "fx.json").write_text(json.dumps(FIXTURE))
    (tmp_path / "light.toml").write_text(LIGHT.replace('start = "now"', 'start = "now"\n\n[[tests]]\nsentence = "turn on the kitchen light"\nfixture = "fx.json"\nanswers = ["yes"]\nexpect = { status = "done", path = ["now", "switch", "switch", "switch", "log", "done"] }'))
    tree = load_tree(tmp_path / "light.toml")
    case = cases(tree)[0]
    assert failures(case, run_case(case, tree, with_tree([], tree))) == []
