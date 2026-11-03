"""`once` on an act: at most once per key, across runs and restarts."""

from __future__ import annotations

import json
import threading
import time

import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.memory import Memory
from pyrtan.once import PREFIX, Once
from pyrtan.tree import TreeError, check, load_tree_text

ORDER = '''
name = "order"
description = "posts an order, once per order"
hosts = ["shop.example"]
start = "post"

[inputs.order]
type = "int"

[nodes.post]
kind = "act"
http = { method = "POST", url = "https://shop.example/orders", body = { id = "{{ inputs.order }}" } }
once = "{{ inputs.order }}"
into = "posted"
next = "done"

[nodes.done]
kind = "done"
say = "order {{ inputs.order }}: {{ state.posted.status }}, first {{ state.posted.once.first }}"
'''


def runner(text: str = ORDER, status: int = 201, once: Once | None = None) -> tuple[Runner, RecordedTransport]:
    reg = Registry()
    reg.add(load_tree_text(text))
    transport = RecordedTransport({"POST https://shop.example/orders": {"status": status, "body": "ok"}})
    return Runner(reg, transport, once=once), transport


def posts(transport: RecordedTransport) -> int:
    return sum(1 for m, _ in transport.calls if m == "POST")


def test_an_act_is_done_once_per_key():
    r, t = runner()
    first = r.run_tree("order", {"order": 7})
    assert first.say == "order 7: 201, first True" and posts(t) == 1
    again = r.run_tree("order", {"order": 7})
    assert again.status == "done" and again.say == "order 7: 201, first False" and posts(t) == 1  # what the first got, not done again
    step = next(s for s in again.trace.steps if s.node == "post")
    assert step.summary.startswith("already done for '7' (at ") and step.summary.endswith("): not done again")
    assert r.run_tree("order", {"order": 8}).say == "order 8: 201, first True" and posts(t) == 2  # another key: done


def test_only_a_good_answer_keeps_the_key():
    r, t = runner(status=500)
    assert r.run_tree("order", {"order": 7}).say == "order 7: 500, first "  # not done well: no key, no once
    r.run_tree("order", {"order": 7})
    assert posts(t) == 2  # so it is tried again
    # A request that never got through keeps nothing either.
    reg = Registry()
    reg.add(load_tree_text(ORDER))
    bare = Runner(reg, RecordedTransport({}))
    assert bare.run_tree("order", {"order": 7}).reason == "transport"
    assert bare.once.done(bare.once.slot("order", "post", "7")) is None


def test_keys_outlive_a_restart_in_the_memory_file(tmp_path):
    path = tmp_path / "memory.json"
    r, t = runner(once=Once(Memory(path)))
    r.run_tree("order", {"order": 7})
    kept = json.loads(path.read_text())["keys"]
    (slot, entry), = kept.items()
    assert slot.startswith(f"{PREFIX}/order/post/") and entry["value"]["key"] == "7" and entry["value"]["result"]["status"] == 201
    later, t2 = runner(once=Once(Memory(path)))  # a new process, the same file
    assert later.run_tree("order", {"order": 7}).say == "order 7: 201, first False" and posts(t2) == 0
    # Forgetting the key lets it be done again.
    Memory(path).forget(slot)
    again, t3 = runner(once=Once(Memory(path)))
    assert again.run_tree("order", {"order": 7}).say == "order 7: 201, first True" and posts(t3) == 1


def test_once_for_keeps_a_key_for_a_while(tmp_path):
    now = [1_000_000.0]
    memory = Memory(tmp_path / "m.json", clock=lambda: now[0])
    r, t = runner(ORDER.replace('once = "{{ inputs.order }}"', 'once = "{{ inputs.order }}"\nonce_for = "1 hour"'), once=Once(memory))
    r.run_tree("order", {"order": 7})
    r.run_tree("order", {"order": 7})
    assert posts(t) == 1
    now[0] += 3601
    r.run_tree("order", {"order": 7})
    assert posts(t) == 2


def test_done_before_is_not_asked_and_does_not_stop_a_dry_run():
    r, t = runner(ORDER.replace('once = "{{ inputs.order }}"', 'once = "{{ inputs.order }}"\nconfirm = true'))
    asked = r.run_tree("order", {"order": 7})
    assert asked.status == "asked"
    r.resume(asked.snapshot, "yes")
    assert posts(t) == 1
    assert r.run_tree("order", {"order": 7}).say == "order 7: 201, first False"  # nothing would be done: nothing to ask
    assert r.run_tree("order", {"order": 7}, dry_run=True).status == "done"
    assert r.run_tree("order", {"order": 8}, dry_run=True).status == "asked"  # a new key still stops a dry run


def test_two_runs_at_once_are_taken_in_turn():
    class Slow(RecordedTransport):
        def request(self, *a, **k):
            time.sleep(0.3)
            return super().request(*a, **k)

    reg = Registry()
    reg.add(load_tree_text(ORDER))
    t = Slow({"POST https://shop.example/orders": {"status": 201, "body": "ok"}})
    r = Runner(reg, t)
    outs: list = []
    threads = [threading.Thread(target=lambda: outs.append(r.run_tree("order", {"order": 7}))) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert posts(t) == 1 and sorted(o.say for o in outs) == ["order 7: 201, first False"] * 2 + ["order 7: 201, first True"]


def test_a_key_that_renders_to_nothing_stops_the_run():
    r, _ = runner(ORDER.replace('once = "{{ inputs.order }}"', 'once = "{{ inputs.note }}"').replace('[inputs.order]', '[inputs.note]\ntype = "str"\nrequired = false\n\n[inputs.order]'))
    out = r.run_tree("order", {"order": 7})
    assert out.reason == "template" and "'once' rendered to nothing" in out.say


@pytest.mark.parametrize("change, words", [
    (('kind = "act"', 'kind = "observe"'), "'once' is for an act"),
    (('once = "{{ inputs.order }}"', 'once = ""'), "'once' is the key"),
    (('once = "{{ inputs.order }}"', 'once = "{{ inputs.order }}"\nonce_for = "soon"'), "'once_for' is a duration"),
    (('once = "{{ inputs.order }}"', 'once_for = "1 day"'), "there is no 'once'"),
])
def test_check_holds_once(change, words):
    text = ORDER.replace(*change)
    try:
        problems = [p.message for p in check(load_tree_text(text)) if p.level == "error"]
    except TreeError as e:
        problems = [str(e)]
    assert any(words in p for p in problems), problems


def test_paths_says_an_act_is_done_once_per_key():
    from pyrtan.paths import routes

    found, _ = routes(load_tree_text(ORDER))
    assert found[0].reads == ["post: POST https://shop.example/orders (once per {{ inputs.order }})"]
