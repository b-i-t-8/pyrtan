"""A tree's own tests (``[[tests]]``, ``pyrtan test``) and the diff an edit
makes to them (``pyrtan diff``)."""

import json
import shutil
import subprocess

import pytest

from conftest import ROOT
from pyrtan.cases import Case, cases, diff, failures, path, run_case, with_tree
from pyrtan.cli import main
from pyrtan.registry import Registry
from pyrtan.tree import TreeError, check, load_tree, load_tree_text

GRADE = '''
name = "grade"
description = "a score, fetched and looked up"
hosts = ["scores.example"]
start = "fetch"

[[triggers]]
pattern = "grade for {who:word}"

[inputs.who]
type = "str"

[[tests]]
name = "a pass"
sentence = "grade for ada"
fixture = "grade.json"
expect = { status = "done", say = "ada: pass", path = ["fetch", "ok", "word", "reply"], output = { who = "ada", word = "pass" } }

[[tests]]
inputs = { who = "bob" }
fixture = "grade.json"
expect = { status = "failed", reason = "no-score" }

[nodes.fetch]
kind = "observe"
http = { url = "https://scores.example/{{ inputs.who }}" }
into = "r"
extract = [{ into = "score", json = "json.score" }]
next = "ok"

[nodes.ok]
kind = "decide"
on = "r.status"
branches = [ { when = { eq = 200 }, then = "word" }, { else = "none" } ]

[nodes.word]
kind = "lookup"
on = "score"
into = "word"
table = [ { gte = 50, value = "pass" } ]
default = "fail"
next = "reply"

[nodes.reply]
kind = "done"
say = "{{ inputs.who }}: {{ state.word }}"
output = { who = "{{ inputs.who }}", word = "{{ state.word }}", score = "{{ state.score }}" }

[nodes.none]
kind = "fail"
reason = "no-score"
say = "no score for {{ inputs.who }}"
'''

FIXTURE = {
    "GET https://scores.example/ada": {"status": 200, "body": {"score": 71}},
    "GET https://scores.example/bob": {"status": 404, "body": ""},
    "GET https://scores.example/cy": {"status": 200, "body": {"score": 12}},
}


@pytest.fixture
def grade(tmp_path):
    (tmp_path / "grade.json").write_text(json.dumps(FIXTURE))
    (tmp_path / "grade.toml").write_text(GRADE)
    return tmp_path / "grade.toml"


def problems(text: str) -> list[str]:
    try:
        return [str(p) for p in check(load_tree_text(text))]
    except TreeError as e:
        return [f"error: {e}"]


def test_tests_are_checked_for_shape():
    assert not [p for p in problems(GRADE) if p.startswith("error")]
    base = GRADE.split("[[tests]]")[0]
    rest = "[nodes.fetch]" + GRADE.split("[nodes.fetch]")[1]
    for test, message in [
        ('sentence = "grade for ada"\ninputs = { who = "a" }\nexpect = { status = "done" }', "exactly one of 'sentence'"),
        ('expect = { status = "done" }', "exactly one of 'sentence'"),
        ('inputs = { whom = "a" }\nexpect = { status = "done" }', "input 'whom' is not declared"),
        ('inputs = { who = "a" }', "a test needs 'expect'"),
        ('inputs = { who = "a" }\nexpect = { status = "fine" }', "status is one of"),
        ('inputs = { who = "a" }\nexpect = { verdict = "done" }', "unknown keys ['verdict']"),
        ('inputs = { who = "a" }\nexpect = { path = ["fetch", "nowhere"] }', "path names node 'nowhere'"),
        ('inputs = { who = "a" }\nnow = "someday"\nexpect = { status = "done" }', "'now' is a date-time"),
        ('inputs = { who = "a" }\nenv = { KEY = 1 }\nexpect = { status = "done" }', "'env' is a table of name = text"),
        ('inputs = { who = "a" }\nanswers = "yes"\nexpect = { status = "done" }', "'answers' is a list"),
        ('inputs = { who = "a" }\nrecording = "x.json"\nexpect = { status = "done" }', "unknown keys ['recording']"),
    ]:
        assert any(message in p for p in problems(base + "[[tests]]\n" + test + "\n\n" + rest)), (test, problems(base + "[[tests]]\n" + test + "\n\n" + rest))
    no_triggers = base.replace('[[triggers]]\npattern = "grade for {who:word}"\n', "") + '[[tests]]\nsentence = "grade for ada"\nexpect = { status = "done" }\n\n' + rest
    assert any("no sentence reads to it" in p for p in problems(no_triggers))
    with pytest.raises(TreeError, match="'tests' is a list of tables"):
        load_tree_text('tests = "all of them"\n' + base + rest)


def test_a_case_runs_against_its_recording_and_says_how_it_missed(grade):
    tree = load_tree(grade)
    reg = with_tree([], tree)
    first, second = cases(tree)
    assert first.name == "a pass" and second.name == 'inputs {"who": "bob"}' and first.fixture == grade.parent / "grade.json"
    out = run_case(first, tree, reg)
    assert out.say == "ada: pass" and path(out) == ["fetch", "ok", "word", "reply"] and failures(first, out) == []
    assert failures(second, run_case(second, tree, reg)) == []
    # Each way to miss says what it expected and what it got.
    miss = Case("grade", 0, {"inputs": {"who": "cy"}, "fixture": "grade.json", "expect": {"status": "failed", "say": "cy: pass", "say_contains": "excellent", "path": ["fetch", "none"], "output": {"word": "pass", "who": {"name": "cy"}, "rank": 1}}}, grade.parent)
    got = failures(miss, run_case(miss, tree, reg))
    assert got == [
        "status: expected 'failed', got 'done'",
        "say: expected 'cy: pass', got 'cy: fail'",
        "say: expected it to contain 'excellent', got 'cy: fail'",
        'output.word: expected "pass", got "fail"',
        "path: expected fetch -> none, got fetch -> ok -> word -> reply",
    ]
    # A request the recording did not foresee is an error that names it.
    unforeseen = run_case(Case("grade", 0, {"inputs": {"who": "dee"}, "fixture": "grade.json", "expect": {"status": "done"}}, grade.parent), tree, reg)
    assert unforeseen.reason == "transport" and "no recorded response for GET https://scores.example/dee" in unforeseen.say
    # No fixture where the test says: an error that says how to make one.
    gone = run_case(Case("grade", 0, {"inputs": {"who": "ada"}, "fixture": "elsewhere.json", "expect": {}}, grade.parent), tree, reg)
    assert gone.reason == "no-fixture" and "--record" in gone.say


def test_a_sentence_must_read_to_its_own_tree(grade):
    tree = load_tree(grade)
    thief = load_tree_text('name = "thief"\ndescription = "x"\nstart = "r"\n[[triggers]]\npattern = "grade for ada"\n[nodes.r]\nkind = "done"\nsay = "mine"\n')
    case = cases(tree)[0]
    out = run_case(case, tree, with_tree([thief], tree))
    assert out.tree == "thief" and failures(case, out)[0] == "the sentence went to thief, not grade"


def test_answers_a_clock_and_env(tmp_path):
    tree = load_tree_text('''
name = "remind"
description = "asks, reads the clock and env"
start = "what"
[[triggers]]
pattern = "remind me {when:date}"
[inputs.when]
type = "str"
[[tests]]
sentence = "remind me tomorrow"
now = "2026-10-25T12:00:00Z"
env = { WHO = "Ada" }
answers = ["the bins"]
expect = { status = "done", say = "2026-10-26: the bins, for Ada (asked 2026-10-25)" }
[nodes.what]
kind = "ask"
prompt = "Remind you of what?"
into = "state.what"
next = "say"
[nodes.say]
kind = "done"
say = "{{ inputs.when }}: {{ state.what }}, for {{ env.WHO }} (asked {{ now.date }})"
''')
    case = cases(tree)[0]
    out = run_case(case, tree, with_tree([], tree))
    assert failures(case, out) == [], out.say
    # Without its answer the run stops at the ask, and says so.
    unanswered = Case("remind", 0, {**case.raw, "answers": []})
    assert failures(unanswered, run_case(unanswered, tree, with_tree([], tree))) == ["status: expected 'done', got 'asked'", "say: expected '2026-10-26: the bins, for Ada (asked 2026-10-25)', got None"]


def test_diff_names_each_run_an_edit_changed(grade, tmp_path):
    old = load_tree(grade)
    # Same file: nothing changed.
    assert diff(old, old).changed == [] and len(diff(old, old).same) == 2
    # The pass mark moves: ada's 71 now fails, bob's 404 does not care.
    raised = load_tree_text(GRADE.replace("gte = 50", "gte = 80"), source=str(grade))
    d = diff(old, raised)
    assert d.same == ['inputs {"who": "bob"}'] and [c.case for c in d.changed] == ["a pass"]
    change = d.changed[0]
    assert change.fields == ["say", "output", "steps"]  # the lookup read its default, not the gte row
    assert change.after["steps"] == ["fetch", "ok [eq 200]", "word [default]", "reply"]
    assert "output.word: \"pass\" -> \"fail\"" in change.render() and "say: \"ada: pass\" -> \"ada: fail\"" in change.render()
    # A route that changes shows the path both ways, with each decision.
    rerouted = load_tree_text(GRADE.replace('{ when = { eq = 200 }, then = "word" }', '{ when = { eq = 201 }, then = "word" }'), source=str(grade))
    c = diff(old, rerouted).changed[0]
    assert c.fields == ["status", "reason", "say", "output", "steps"]
    assert c.before["steps"] == ["fetch", "ok [eq 200]", "word [gte 50]", "reply"] and c.after["steps"] == ["fetch", "ok [else]", "none"]
    # A case only the old version carries is still run on both.
    dropped = load_tree_text(GRADE.split("[[tests]]")[0] + "[nodes.fetch]" + GRADE.split("[nodes.fetch]")[1], source=str(grade))
    assert len(diff(old, dropped).same) == 2


def test_the_test_and_diff_commands(grade, tmp_path, capsys):
    assert main(["test", str(grade.parent)]) == 0
    out = capsys.readouterr().out
    assert "grade: 2 test(s)" in out and "  ok    a pass" in out and "2/2 passed" in out
    grade.write_text(GRADE.replace("gte = 50", "gte = 80"))
    assert main(["test", str(grade.parent), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1 and report["tests"][0]["failures"][0] == "say: expected 'ada: pass', got 'ada: fail'"
    assert main(["test", str(grade.parent), "--tree", "nothing"]) == 2
    # Against a file.
    (tmp_path / "before.toml").write_text(GRADE)
    assert main(["diff", str(grade), "--old", str(tmp_path / "before.toml")]) == 1
    out = capsys.readouterr().out
    assert "changed  a pass" in out and "grade: 1 of 2 test run(s) changed" in out
    assert main(["diff", str(grade), "--old", str(grade), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_diff_against_a_git_revision(grade, capsys):
    repo = grade.parent
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "grade")
    assert main(["diff", str(grade)]) == 0
    assert "0 of 2 test run(s) changed against HEAD:" in capsys.readouterr().out
    grade.write_text(GRADE.replace("gte = 50", "gte = 80"))
    assert main(["diff", str(grade), "--against", "HEAD"]) == 1
    assert "changed  a pass" in capsys.readouterr().out
    assert main(["diff", str(grade), "--against", "no-such-ref"]) == 2
    assert "cannot read no-such-ref" in capsys.readouterr().err


def test_the_examples_pass_their_own_tests(capsys):
    assert main(["test", str(ROOT / "examples")]) == 0
    assert "failed" not in capsys.readouterr().out.lower()
