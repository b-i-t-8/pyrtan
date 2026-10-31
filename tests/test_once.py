"""A paused run is resumed once: the first answer to a snapshot is the answer."""

from __future__ import annotations

import json
import threading
import time

import pytest

from pyrtan import RecordedTransport, Registry, Runner, runtime
from pyrtan.service import Service
from pyrtan.tree import load_tree_text
from test_confirm import FIXTURE, LIGHT, posts, runner

ORDER = '''
name = "order"
description = "asks how many, then orders them"
hosts = ["shop.example"]
start = "how-many"

[inputs.n]
type = "int"
default = 1

[nodes.how-many]
kind = "ask"
prompt = "How many?"
into = "inputs.n"
next = "buy"

[nodes.buy]
kind = "act"
http = { method = "POST", url = "https://shop.example/order", body = { n = "{{ inputs.n }}" } }
into = "bought"
next = "done"

[nodes.done]
kind = "done"
say = "Ordered {{ inputs.n }}."
'''

SHOP = {"POST https://shop.example/order": {"status": 201, "body": ""}}


def test_a_second_yes_gets_the_first_outcome_and_does_nothing_again():
    r, t = runner(LIGHT)
    asked = r.run_intent("turn on the kitchen light")
    assert isinstance(asked.snapshot["id"], str) and len(asked.snapshot["id"]) >= 12
    first = r.resume(asked.snapshot, "yes")
    again = r.resume(asked.snapshot, "yes")
    assert posts(t) == ["https://lights.example/kitchen", "https://lights.example/log"]  # once
    assert (again.status, again.say) == (first.status, first.say) == ("done", "The kitchen light is on (204).")
    last = again.trace.steps[-1]
    assert last.kind == "resume" and last.summary.startswith("already answered (yes, at ") and last.summary.endswith("nothing was done again")
    assert len(again.trace.steps) == len(first.trace.steps) + 1 and first.trace.steps[-1].kind != "resume"  # the first is left as it was


def test_the_first_answer_stands_whatever_the_second_says():
    r, t = runner(LIGHT)
    asked = r.run_intent("turn on the kitchen light")
    assert r.resume(asked.snapshot, "no").reason == "declined"
    assert r.resume(asked.snapshot, "yes").reason == "declined" and posts(t) == []
    # A new question is a new snapshot, answered afresh.
    fresh = r.run_intent("turn on the kitchen light")
    assert fresh.snapshot["id"] != asked.snapshot["id"]
    assert r.resume(fresh.snapshot, "yes").status == "done" and len(posts(t)) == 2


def test_an_ask_is_answered_once_too():
    r, t = runner(ORDER, fixture=SHOP)
    asked = r.run_tree("order", {})
    assert r.resume(asked.snapshot, "3").say == "Ordered 3."
    assert r.resume(asked.snapshot, "300").say == "Ordered 3." and posts(t) == ["https://shop.example/order"]


def test_a_snapshot_without_an_id_resumes_as_before():
    r, t = runner(LIGHT)
    asked = r.run_intent("turn on the kitchen light")
    old = {k: v for k, v in asked.snapshot.items() if k != "id"}
    r.resume(old, "yes")
    r.resume(old, "yes")
    assert len(posts(t)) == 4  # nothing to know it by: done twice, as before ids


def test_two_answers_at_once_do_it_once():
    class Slow(RecordedTransport):
        def request(self, method, url, headers, body, timeout):
            if method == "POST":
                time.sleep(0.3)
            return super().request(method, url, headers, body, timeout)

    reg = Registry()
    reg.add(load_tree_text(LIGHT))
    t = Slow(json.loads(json.dumps(FIXTURE)))
    r = Runner(reg, t)
    asked = r.run_intent("turn on the kitchen light")
    outs = []
    threads = [threading.Thread(target=lambda: outs.append(r.resume(asked.snapshot, "yes"))) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(posts(t)) == 2 and {o.say for o in outs} == {"The kitchen light is on (204)."}
    assert sum(o.trace.steps[-1].kind == "resume" for o in outs) == 2


def test_a_resume_that_breaks_is_not_kept(monkeypatch):
    r, t = runner(LIGHT)
    asked = r.run_intent("turn on the kitchen light")

    def boom(snapshot, answer):
        raise RuntimeError("the runtime broke")

    monkeypatch.setattr(r, "_resume", boom)
    with pytest.raises(RuntimeError):
        r.resume(asked.snapshot, "yes")
    monkeypatch.undo()
    assert r.resume(asked.snapshot, "yes").status == "done"  # nothing settled, so it may be answered


def test_the_runner_remembers_the_last_answers(monkeypatch):
    monkeypatch.setattr(runtime, "ANSWERS_KEPT", 2)
    r, t = runner(ORDER, fixture=SHOP)
    snaps = [r.run_tree("order", {}).snapshot for _ in range(3)]
    for s in snaps:
        r.resume(s, "1")
    assert list(r.answered) == [snaps[1]["id"], snaps[2]["id"]]
    r.resume(snaps[0], "1")  # forgotten: answered again
    assert len(posts(t)) == 4


def test_a_service_answers_a_retried_resume_from_memory(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "light.toml").write_text(LIGHT)
    t = RecordedTransport(json.loads(json.dumps(FIXTURE)))
    svc = Service(Registry.from_paths([tmp_path / "t"]), name="s", transport=t)
    asked = svc.intent({"text": "turn on the kitchen light"})
    one = svc.resume({"snapshot": asked["snapshot"], "answer": "yes"})
    two = svc.resume({"snapshot": asked["snapshot"], "answer": "yes"})
    assert one["say"] == two["say"] and len(posts(t)) == 2
    assert two["trace"]["steps"][-1]["kind"] == "resume"
