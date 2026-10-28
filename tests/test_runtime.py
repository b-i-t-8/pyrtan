import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.tree import load_tree_text

ASKING = """
name = "greeter"
description = "asks for a name"
start = "have-name"

[inputs.name]
type = "str"
required = false

[nodes.have-name]
kind = "decide"
on = "inputs.name"
branches = [ { when = { exists = true }, then = "hello" }, { else = "ask" } ]

[nodes.ask]
kind = "ask"
prompt = "What is your name?"
into = "inputs.name"
next = "hello"

[nodes.hello]
kind = "done"
say = "Hello, {{ inputs.name }}."
output = { name = "{{ inputs.name }}" }
"""

SUBFLOW = """
name = "doubler"
description = "doubles numbers with a local sub-flow"
start = "walk"

[inputs.numbers]
type = "list"

[nodes.walk]
kind = "each"
over = "inputs.numbers"
limit = 3
start = "double"
into = "doubled"
next = "reply"

[nodes.double]
kind = "set"
set = { twice = "{{ item }}{{ item }}", at = "{{ index }}" }
next = "give"

[nodes.give]
kind = "done"
output = { n = "{{ item }}", twice = "{{ state.twice }}", at = "{{ state.at }}" }

[nodes.reply]
kind = "done"
say = "{{ state.doubled | length }} doubled"
output = "{{ state.doubled }}"
"""

GAPPY = """
name = "gappy"
description = "a decide with no else"
hosts = ["x.test"]
start = "get"

[nodes.get]
kind = "observe"
http = { url = "https://x.test/" }
into = "r"
next = "pick"

[nodes.pick]
kind = "decide"
on = "r.status"
branches = [ { when = { eq = 200 }, then = "ok" } ]

[nodes.ok]
kind = "done"
say = "fine"
"""

EMITTER = """
name = "emitter"
description = "acts by emitting"
start = "fire"

[inputs.what]
type = "str"
default = "ping"

[nodes.fire]
kind = "act"
emit = { event = "pyrtan.{{ inputs.what }}", data = { at = "{{ now.year }}" } }
into = "ev"
next = "ok"

[nodes.ok]
kind = "done"
say = "sent {{ state.ev.event }}"
"""

DELEGATOR = """
name = "delegator"
description = "delegates to the greeter and the doubler"
start = "greet"

[nodes.greet]
kind = "delegate"
tree = "greeter"
with = { name = "Ada" }
into = "greeting"
next = "numbers"

[nodes.numbers]
kind = "delegate"
intent = "double 1 2"
into = "nums"
next = "ok"

[nodes.ok]
kind = "done"
say = "{{ state.greeting.say }} / {{ state.nums.status }}"
"""

RETRYING = """
name = "retrying"
description = "observe retries once, act does not"
hosts = ["x.test"]
start = "look"

[nodes.look]
kind = "observe"
http = { url = "https://x.test/flaky" }
into = "r"
retries = 2
next = "ok"

[nodes.ok]
kind = "done"
say = "{{ state.r.status }}"
"""


def reg_with(*texts: str) -> Registry:
    reg = Registry()
    for t in texts:
        reg.add(load_tree_text(t))
    return reg


def test_ask_and_resume():
    runner = Runner(reg_with(ASKING), RecordedTransport())
    out = runner.run_tree("greeter", {})
    assert out.status == "asked" and out.question == "What is your name?"
    assert out.snapshot["node"] == "ask"
    done = runner.resume(out.snapshot, "Grace")
    assert done.status == "done" and done.say == "Hello, Grace."
    assert [s.node for s in done.trace.steps] == ["have-name", "ask", "ask", "hello"]
    direct = runner.run_tree("greeter", {"name": "Ada"})
    assert direct.say == "Hello, Ada."


def test_each_with_local_subflow():
    runner = Runner(reg_with(SUBFLOW), RecordedTransport())
    out = runner.run_tree("doubler", {"numbers": [1, 2, 3, 4]})
    assert out.status == "done"
    assert out.output == [{"n": 1, "twice": "11", "at": 0}, {"n": 2, "twice": "22", "at": 1}, {"n": 3, "twice": "33", "at": 2}]
    assert out.say == "3 doubled"
    kinds = [(s.node, s.depth) for s in out.trace.steps]
    assert ("double", 1) in kinds and ("walk", 0) in kinds


def test_gap_is_recorded_and_fails_the_run():
    rt = RecordedTransport()
    rt.add("GET", "https://x.test/", 418, "")
    out = Runner(reg_with(GAPPY), rt).run_tree("gappy")
    assert out.status == "error" and out.reason == "gap"
    assert out.trace.gaps == [{"tree": "gappy", "node": "pick", "on": "state.r.status", "observed": 418}]
    assert "no branch" in out.trace.render()


def test_host_enforced_at_run_time():
    tree = load_tree_text(GAPPY.replace('http = { url = "https://x.test/" }', 'http = { url = "https://{{ inputs.h }}/" }') + '\n[inputs.h]\ntype = "str"\n')
    reg = Registry()
    reg.add(tree)
    out = Runner(reg, RecordedTransport()).run_tree("gappy", {"h": "evil.test"})
    assert out.status == "error" and out.reason == "host-not-allowed"


def test_emit_is_collected():
    runner = Runner(reg_with(EMITTER), RecordedTransport())
    out = runner.run_tree("emitter", {"what": "pong"})
    assert out.status == "done" and out.say == "sent pyrtan.pong"
    assert runner.emitted[0]["event"] == "pyrtan.pong" and runner.emitted[0]["tree"] == "emitter"


def test_delegate_by_tree_and_by_intent():
    doubler = SUBFLOW + '\n[[triggers]]\npattern = "double {numbers}"\n'
    doubler = doubler.replace('type = "list"', 'type = "str"').replace('over = "inputs.numbers"', 'over = "nums"').replace('start = "walk"', 'start = "split"') + """
[nodes.split]
kind = "set"
set = { nums = ["{{ inputs.numbers | first }}", "{{ inputs.numbers | last }}"] }
next = "walk"
"""
    runner = Runner(reg_with(ASKING, doubler, DELEGATOR), RecordedTransport())
    out = runner.run_tree("delegator")
    assert out.status == "done", out.say
    assert out.say == "Hello, Ada. / done"
    assert any(s.node == "hello" and s.depth == 1 for s in out.trace.steps)


def test_delegate_to_unknown_tree_without_resolver():
    runner = Runner(reg_with(DELEGATOR), RecordedTransport())
    out = runner.run_tree("delegator")
    assert out.status == "done"
    assert out.say == "No tree named 'greeter'. / error"  # the delegate's answer is captured; the run continues


def test_observe_retries_but_act_does_not():
    rt = RecordedTransport({"GET https://x.test/flaky": [{"error": "boom"}, {"error": "boom"}, {"status": 200, "body": "ok"}]})
    out = Runner(reg_with(RETRYING), rt).run_tree("retrying")
    assert out.status == "done" and out.say == "200"
    assert len(rt.calls) == 3
    rt = RecordedTransport({"GET https://x.test/flaky": [{"error": "boom"}, {"status": 200, "body": "ok"}]})
    out = Runner(reg_with(RETRYING.replace("retries = 2", "retries = 0")), rt).run_tree("retrying")
    assert out.status == "error" and out.reason == "transport"


def test_no_match_and_ambiguity():
    a = ASKING.replace('name = "greeter"', 'name = "a"') + '\n[[triggers]]\npattern = "hi {name}"\n'
    b = ASKING.replace('name = "greeter"', 'name = "b"') + '\n[[triggers]]\npattern = "hi {name}"\n'
    runner = Runner(reg_with(a, b), RecordedTransport())
    assert runner.run_intent("hi there").reason == "ambiguous"
    assert runner.run_intent("bye").reason == "no-match"


def test_depth_limit():
    text = """
name = "recursive"
description = "delegates to itself"
start = "again"

[nodes.again]
kind = "delegate"
tree = "recursive"
into = "r"
next = "ok"

[nodes.ok]
kind = "done"
say = "{{ state.r.reason }}"
"""
    out = Runner(reg_with(text), RecordedTransport()).run_tree("recursive")
    assert out.status == "done" and "too-deep" in out.trace.render()


REMINDER = """
name = "reminder"
description = "reads a date and a time"
start = "say"

[language]
synonyms = { remind = ["ping", "nudge"] }

[[triggers]]
pattern = "remind me {when:date} at {at:time}"

[inputs.when]
type = "str"
[inputs.at]
type = "str"

[nodes.say]
kind = "done"
say = "{{ inputs.when }} {{ inputs.at }}"
"""


def test_trace_starts_with_how_the_sentence_was_read():
    import datetime as dt

    runner = Runner(reg_with(REMINDER), RecordedTransport(), clock=lambda: dt.datetime(2026, 10, 25, 12, tzinfo=dt.timezone.utc))
    out = runner.run_intent("Hey Pyrtan, could you nudge me next friday at 3pm please")
    assert out.status == "done" and out.say == "2026-10-30 15:00"
    first = out.trace.steps[0]
    assert first.kind == "intent" and first.node == "intent" and first.next == "say"
    assert first.observed["cleaned"] == "nudge me next friday at 3pm"
    assert first.observed["steps"] == ["fillers: 'Hey Pyrtan', 'please', 'could you'"]  # in the order they were dropped
    assert first.observed["inputs"] == {"when": "2026-10-30", "at": "15:00"}
    assert "when: 'next friday' read as '2026-10-30'" in first.observed["notes"]
    assert "reminder via 'remind me {when:date} at {at:time}'" in out.trace.render()
    # The same clock drives {{ now }}: a run is deterministic given its observations.
    out2 = runner.run_intent("remind me tomorrow at noon")
    assert out2.say == "2026-10-26 12:00"
    # A sentence that fits the shape but not the value is refused, and says why.
    out3 = runner.run_intent("remind me someday at noon")
    assert out3.reason == "no-match" and out3.output["reading"]["considered"] == ["remind me {when:date} at {at:time}"]


GRADE = """
name = "grade"
description = "a score into a word"
start = "word"

[inputs.score]
type = "int"
required = false

[nodes.word]
kind = "lookup"
on = "inputs.score"
into = "result.word"
table = [
  { between = [90, 100], value = "excellent" },
  { between = [50, 89], value = "a pass, {{ inputs.score }}" },
  { exists = false, value = { word = "absent", points = 0 }, note = "a row may give a table" },
]
next = "reply"

[nodes.reply]
kind = "done"
output = "{{ state.result.word }}"
"""


def test_lookup_reads_the_first_row_that_holds_and_records_a_gap_when_none_does():
    runner = Runner(reg_with(GRADE), RecordedTransport())
    out = runner.run_tree("grade", {"score": 95})
    assert out.output == "excellent"
    step = out.trace.steps[0]
    assert (step.kind, step.chosen, step.observed, step.next) == ("lookup", "between [90, 100]", 95, "reply")
    assert step.summary == "inputs.score = 95; result.word=excellent"
    # A value is a template, read when the row is; a row may give a table.
    assert runner.run_tree("grade", {"score": 60}).output == "a pass, 60"
    assert runner.run_tree("grade", {}).output == {"word": "absent", "points": 0}
    # No row and no default: a gap, the same shape a decide records.
    gap = runner.run_tree("grade", {"score": 10})
    assert gap.status == "error" and gap.reason == "gap" and gap.say == "word: no row for inputs.score = 10."
    assert gap.trace.gaps == [{"tree": "grade", "node": "word", "on": "inputs.score", "observed": 10}]
    # With a default, nothing is a gap.
    out = Runner(reg_with(GRADE.replace('next = "reply"', 'default = "{{ inputs.score }} is low"\nnext = "reply"')), RecordedTransport()).run_tree("grade", {"score": 10})
    assert out.output == "10 is low" and out.trace.steps[0].chosen == "default"


def test_lookup_value_that_names_nothing_fails_the_node():
    tree = GRADE.replace('value = "excellent"', 'value = "{{ state.nothing }}"')
    out = Runner(reg_with(tree), RecordedTransport()).run_tree("grade", {"score": 95})
    assert out.status == "error" and out.reason == "template"
