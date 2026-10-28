"""A node's ``expect``: the shape of a good answer, and the drift recorded
when a source changes under a tree."""

import json

import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.cases import cases, failures, run_case, with_tree
from pyrtan.grow import draft_branch
from pyrtan.service import Service
from pyrtan.tree import TreeError, check, load_tree, load_tree_text

RELEASE = '''
name = "release"
description = "the latest release"
hosts = ["api.example"]
start = "fetch"

[inputs.repo]
type = "str"

[nodes.fetch]
kind = "observe"
http = { url = "https://api.example/{{ inputs.repo }}" }
into = "resp"
expect = { "json.tag" = { type = "str" }, "json.url" = { startswith = "https://" } }
extract = [{ into = "tag", json = "json.tag" }]
next = "status"

[nodes.status]
kind = "decide"
on = "resp.status"
branches = [ { when = { eq = 200 }, then = "reply" }, { else = "none" } ]

[nodes.reply]
kind = "done"
say = "{{ state.tag }}"

[nodes.none]
kind = "fail"
reason = "none"
say = "no release"
'''

FIXTURE = {
    "GET https://api.example/fine": {"status": 200, "body": {"tag": "v2", "url": "https://x/v2"}},
    "GET https://api.example/moved": {"status": 200, "body": {"release": {"tag": "v1"}, "url": "http://x/v1"}},
    "GET https://api.example/half": {"status": 200, "body": {"tag": "v3", "url": "ftp://x"}},
    "GET https://api.example/gone": {"status": 404, "body": {"message": "Not Found"}},
}


def run(text: str, inputs: dict, fixture=FIXTURE, adapters=None):
    reg = Registry()
    reg.add(load_tree_text(text))
    transport = RecordedTransport(json.loads(json.dumps(fixture)))
    return Runner(reg, transport, adapters=adapters or transport).run_tree(load_tree_text(text).name, inputs)


def test_a_good_answer_in_its_shape_runs_on():
    out = run(RELEASE, {"repo": "fine"})
    assert out.status == "done" and out.say == "v2" and out.trace.gaps == []


def test_a_good_answer_out_of_shape_is_drift_naming_each_field():
    out = run(RELEASE, {"repo": "moved"})
    assert out.status == "error" and out.reason == "drift"
    assert out.say == "fetch: GET https://api.example/moved answered, but not in the shape this tree expects: json.tag = <missing>, expected type 'str' (and 1 more)."
    assert out.trace.gaps == [
        {"tree": "release", "node": "fetch", "on": "state.resp.json.tag", "observed": None, "kind": "drift", "expected": {"type": "str"}},
        {"tree": "release", "node": "fetch", "on": "state.resp.json.url", "observed": "http://x/v1", "kind": "drift", "expected": {"startswith": "https://"}},
    ]
    assert out.output == {"drift": out.trace.gaps}
    step = out.trace.steps[-1]
    assert step.node == "fetch" and "drift: json.tag = <missing>, expected type 'str'" in step.summary and step.observed == {"json.tag": None, "json.url": "http://x/v1"}
    assert "  drift: fetch saw state.resp.json.url = \"http://x/v1\", where it expects startswith 'https://'" in out.trace.render()
    # One field off is drift too, and says so alone.
    half = run(RELEASE, {"repo": "half"})
    assert half.reason == "drift" and half.say.endswith("json.url = ftp://x, expected startswith 'https://'.")


def test_a_bad_answer_is_the_trees_to_decide_not_drift():
    out = run(RELEASE, {"repo": "gone"})
    assert out.status == "failed" and out.reason == "none" and out.trace.gaps == []


def test_good_means_what_each_source_says_it_means():
    shell = '''
name = "df"
description = "x"
commands = ["df"]
start = "df"
[nodes.df]
kind = "observe"
shell = { command = "df" }
into = "out"
expect = { "stdout" = { contains = "Filesystem" } }
next = "ok"
[nodes.ok]
kind = "done"
say = "ok"
'''
    # A program that failed is a bad answer: the tree decides on it, it is not drift.
    failed = {"shell df": {"command": "df", "args": [], "status": 1, "ok": False, "stdout": "", "stderr": "nope", "body": "", "json": None, "lines": []}}
    assert run(shell, {}, failed).status == "done"
    changed = {"shell df": {"command": "df", "args": [], "status": 0, "ok": True, "stdout": "Size Used", "stderr": "", "body": "Size Used", "json": None, "lines": ["Size Used"]}}
    assert run(shell, {}, changed).reason == "drift"
    # An observation from state has no bad answer: it is always checked.
    frm = RELEASE.replace('''http = { url = "https://api.example/{{ inputs.repo }}" }
into = "resp"''', '''from = "inputs.repo"''')
    frm = frm.replace('''expect = { "json.tag" = { type = "str" }, "json.url" = { startswith = "https://" } }''', '''expect = { "value" = { matches = "^[a-z]+$" } }''').replace('hosts = ["api.example"]\n', "")
    frm = frm.replace('on = "resp.status"', 'on = "inputs.repo"').replace("{ eq = 200 }", '{ exists = true }')
    out = run(frm, {"repo": "Not Lower"})
    assert out.reason == "drift" and out.trace.gaps[0]["on"] == "inputs.repo.value"
    assert run(frm, {"repo": "fine"}).status == "done"


def test_expect_is_checked():
    def problems(text):
        try:
            return [str(p) for p in check(load_tree_text(text))]
        except TreeError as e:
            return [f"error: {e}"]

    assert not [p for p in problems(RELEASE) if p.startswith("error")]
    for old, new, message in [
        ('expect = { "json.tag" = { type = "str" }, "json.url" = { startswith = "https://" } }', 'expect = "a release"', "'expect' is a table of path = condition"),
        ('expect = { "json.tag" = { type = "str" }, "json.url" = { startswith = "https://" } }', "expect = {}", "'expect' is a table of path = condition"),
        ('{ type = "str" }', '{ kind = "str" }', "nodes.fetch.expect.json.tag: unknown condition keys"),
        ('{ type = "str" }', '{ type = "text" }', "unknown type 'text'"),
    ]:
        assert any(message in p for p in problems(RELEASE.replace(old, new, 1))), (message, problems(RELEASE.replace(old, new, 1)))
    emit = RELEASE.replace('''http = { url = "https://api.example/{{ inputs.repo }}" }''', '''emit = { event = "x" }''').replace('kind = "observe"', 'kind = "act"', 1)
    assert any("an emit has no answer" in p for p in problems(emit))


def test_drift_reaches_the_gaps_and_grow_leaves_it_to_a_person(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "release.toml").write_text(RELEASE)
    svc = Service(Registry.from_paths([tmp_path / "t"]), name="s", transport=RecordedTransport(json.loads(json.dumps({**FIXTURE, "GET https://api.example/moved": [FIXTURE["GET https://api.example/moved"]] * 3}))))
    for _ in range(2):
        assert svc.run({"tree": "release", "inputs": {"repo": "moved"}})["reason"] == "drift"
    gaps = {g["on"]: g for g in svc.gaps.all()}
    assert gaps["state.resp.json.tag"]["count"] == 2 and gaps["state.resp.json.tag"]["node"] == "fetch"
    # A drift is not a missing branch: grow drafts nothing for it.
    tree = load_tree(tmp_path / "t" / "release.toml")
    assert draft_branch(tree, gaps["state.resp.json.tag"]) is None


def test_a_tree_can_test_for_its_own_drift(tmp_path):
    (tmp_path / "fx.json").write_text(json.dumps(FIXTURE))
    (tmp_path / "release.toml").write_text(RELEASE.replace('start = "fetch"', 'start = "fetch"\n\n[[tests]]\ninputs = { repo = "moved" }\nfixture = "fx.json"\nexpect = { status = "error", reason = "drift", path = ["fetch"] }'))
    tree = load_tree(tmp_path / "release.toml")
    case = cases(tree)[0]
    assert failures(case, run_case(case, tree, with_tree([], tree))) == []
