"""Memory across runs: the store (memory.py), the source (adapters.py), the
checks (tree.py), and what a run, a recording, a test and a service make
of it."""

import json

import pytest

from pyrtan import memory as memorymod
from pyrtan.adapters import LocalAdapters
from pyrtan.cases import cases, failures, run_case, with_tree
from pyrtan.cli import main
from pyrtan.memory import Memory, MemoryRefused
from pyrtan.registry import Registry
from pyrtan.runtime import Runner
from pyrtan.service import Service
from pyrtan.transport import RecordedTransport, RecordingTransport, UrllibTransport
from pyrtan.tree import TreeError, check, load_tree_text


class Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------- the store


def test_the_store_reads_writes_counts_and_forgets(tmp_path):
    clock = Clock()
    mem = Memory(tmp_path / "m.json", clock=clock)
    assert mem.perform({"key": "a/b"}) == {"key": "a/b", "exists": False, "value": None, "json": None, "body": "", "written": None, "expires": None}
    w = mem.perform({"key": "a/b", "write": {"seen": [1, 2]}})
    assert w["exists"] and w["value"] == {"seen": [1, 2]} and w["body"] == '{"seen": [1, 2]}' and w["existed"] is False and w["previous"] is None
    assert w["written"] == "2023-11-14T22:13:20Z" and w["expires"] is None
    assert mem.perform({"key": "a/b", "write": "words"})["previous"] == {"seen": [1, 2]}
    assert mem.perform({"key": "a/b"})["body"] == "words"
    # A count starts at 0 and adds; a count on a word is refused.
    assert mem.perform({"key": "n", "add": 1})["value"] == 1 and mem.perform({"key": "n", "add": 2.5})["value"] == 3.5
    with pytest.raises(MemoryRefused, match="not a number to add to"):
        mem.perform({"key": "a/b", "add": 1})
    with pytest.raises(MemoryRefused, match="'add' is a number"):
        mem.perform({"key": "n", "add": True})
    gone = mem.perform({"key": "n", "delete": True})
    assert gone["exists"] is False and gone["existed"] is True and gone["previous"] == 3.5
    # It is a file: another process sees what this one kept.
    again = Memory(tmp_path / "m.json", clock=clock)
    assert again.perform({"key": "a/b"})["value"] == "words" and not again.perform({"key": "n"})["exists"]
    assert json.loads((tmp_path / "m.json").read_text())["pyrtan-memory"] == 1


def test_a_ttl_forgets_on_its_own():
    clock = Clock()
    mem = Memory(clock=clock)
    kept = mem.perform({"key": "once", "write": True, "ttl": "1 hour"})
    assert kept["expires"] == "2023-11-14T23:13:20Z"
    clock.t += 3599
    assert mem.perform({"key": "once"})["exists"] is True and len(mem.items()) == 1
    clock.t += 1
    assert mem.perform({"key": "once"})["exists"] is False and mem.items() == []
    # A count with a ttl counts from 0 again once it has lapsed.
    mem.perform({"key": "c", "add": 1, "ttl": 60})
    clock.t += 61
    assert mem.perform({"key": "c", "add": 1, "ttl": 60})["value"] == 1


def test_the_store_refuses_what_it_should(tmp_path, monkeypatch):
    mem = Memory()
    for bad in ("", "/lead", "has space", "x" * 201, "a\nb"):
        with pytest.raises(MemoryRefused, match="not a memory key"):
            mem.perform({"key": bad})
    with pytest.raises(MemoryRefused, match="at most 65536 bytes"):
        mem.perform({"key": "big", "write": "x" * 70_000})
    with pytest.raises(MemoryRefused, match="'ttl' is a duration"):
        mem.perform({"key": "t", "write": 1, "ttl": "someday"})
    with pytest.raises(MemoryRefused, match="'delete' is true"):
        mem.perform({"key": "t", "delete": "yes"})
    monkeypatch.setattr(memorymod, "MAX_KEYS", 2)
    mem = Memory()
    mem.perform({"key": "a", "write": 1})
    mem.perform({"key": "b", "write": 1})
    mem.perform({"key": "a", "write": 2})  # a key already kept can always change
    with pytest.raises(MemoryRefused, match="holds 2 keys, its limit"):
        mem.perform({"key": "c", "write": 1})
    (tmp_path / "junk.json").write_text("[1, 2]")
    with pytest.raises(MemoryRefused, match="not a memory file"):
        Memory(tmp_path / "junk.json")


# ---------------------------------------------------------------- the tree


TALLY = '''
name = "tally"
description = "counts visits, and says when one is new"
keys = ["tally/*"]
start = "bump"

[[triggers]]
pattern = "visit from {who:word}"

[inputs.who]
type = "str"

[nodes.bump]
kind = "act"
memory = { key = "tally/{{ inputs.who }}", add = 1 }
into = "n"
next = "first"

[nodes.first]
kind = "decide"
on = "n.existed"
branches = [ { when = { eq = false }, then = "hello" }, { else = "again" } ]

[nodes.hello]
kind = "done"
say = "first visit from {{ inputs.who }}"
output = { count = "{{ state.n.value }}" }

[nodes.again]
kind = "done"
say = "visit {{ state.n.value }} from {{ inputs.who }}"
output = { count = "{{ state.n.value }}" }
'''


def problems(text: str) -> list[str]:
    try:
        return [str(p) for p in check(load_tree_text(text))]
    except TreeError as e:
        return [f"error: {e}"]


def test_memory_nodes_are_checked():
    assert not [p for p in problems(TALLY) if p.startswith("error")]
    for old, new, message in [
        ('keys = ["tally/*"]\n', "", "must list the keys it may reach"),
        ('key = "tally/{{ inputs.who }}"', 'key = "elsewhere/x"', "is not in this tree's keys"),
        ("[nodes.hello]", '[nodes.peek]\nkind = "observe"\nmemory = { key = "tally/x", write = 1 }\ninto = "p"\nnext = "hello"\n\n[nodes.hello]', "make this an act node"),
        ("add = 1", "add = 1, write = 2", "one of 'write', 'add' or 'delete', not write and add"),
        ("add = 1", 'add = "lots"', "'add' is a number"),
        ("add = 1", "delete = 1", "'delete' is true, or absent"),
        ("add = 1", 'add = 1, ttl = "whenever"', "'ttl' is a duration"),
        ("add = 1", 'delete = true, ttl = "1 day"', "'ttl' goes with 'write' or 'add'"),
        ("add = 1", "add = 1, until = 3", "unknown keys ['until']"),
        ('key = "tally/{{ inputs.who }}"', 'key = "tally/a b"', "is not a memory key"),
        ('memory = { key = "tally/{{ inputs.who }}", add = 1 }', 'memory = "tally"', "'memory' is a table with 'key'"),
    ]:
        assert old in TALLY, old
        got = problems(TALLY.replace(old, new, 1))
        assert any(message in p for p in got), (message, got)
    assert any("an act that only reads" in p for p in problems(TALLY.replace(", add = 1", "")))
    assert any("the key is decided at run time" in p for p in problems(TALLY))
    assert load_tree_text(TALLY).capability()["keys"] == ["tally/*"] and load_tree_text(TALLY).sources() == ["memory"]


def runner_with(tree_text: str, memory: Memory | None = None, transport=None, egress: str = "any") -> Runner:
    reg = Registry()
    reg.add(load_tree_text(tree_text))
    adapters = transport if transport is not None else LocalAdapters(egress=egress, memory=memory)
    return Runner(reg, transport or UrllibTransport(), adapters=adapters)


def test_a_tree_remembers_across_runs(tmp_path):
    path = tmp_path / "memory.json"
    first = runner_with(TALLY, Memory(path)).run_intent("visit from ada")
    assert first.say == "first visit from ada" and first.output == {"count": 1}
    step = next(s for s in first.trace.steps if s.node == "bump")
    assert step.summary == "memory tally/ada -> now 1, was null"
    # Another runner, another process for all it knows: the file remembers.
    second = runner_with(TALLY, Memory(path)).run_intent("visit from ada")
    assert second.say == "visit 2 from ada" and second.output == {"count": 2}
    assert runner_with(TALLY, Memory(path)).run_intent("visit from bob").say == "first visit from bob"


def test_keys_outside_the_list_and_a_public_service_are_refused():
    peek = TALLY.replace('key = "tally/{{ inputs.who }}"', 'key = "{{ inputs.who }}/count"')
    out = runner_with(peek).run_intent("visit from secrets")
    assert out.reason == "key-not-allowed" and "'secrets/count' is outside this tree's keys ['tally/*']" in out.say
    out = runner_with(TALLY, egress="public").run_intent("visit from ada")
    assert out.reason == "transport" and "files, shells, serial lines and memory are its own box" in out.say


def test_a_recording_keeps_memory_and_a_test_replays_it(tmp_path):
    mem = Memory()
    mem.perform({"key": "tally/ada", "add": 4})
    recorder = RecordingTransport(UrllibTransport(), LocalAdapters(memory=mem))
    assert runner_with(TALLY, transport=recorder).run_intent("visit from ada").say == "visit 5 from ada"
    recorder.save(tmp_path / "tally.json")
    fixture = json.loads((tmp_path / "tally.json").read_text())
    assert list(fixture) == ["memory add tally/ada"] and fixture["memory add tally/ada"]["previous"] == 4
    # Replayed, the store is not touched: the recording answers.
    assert runner_with(TALLY, transport=RecordedTransport.from_file(tmp_path / "tally.json")).run_intent("visit from ada").say == "visit 5 from ada"
    assert mem.perform({"key": "tally/ada"})["value"] == 5
    # And a tree's own test runs against it, path and all.
    (tmp_path / "tally.toml").write_text(TALLY.replace('start = "bump"', 'start = "bump"\n\n[[tests]]\nsentence = "visit from ada"\nfixture = "tally.json"\nexpect = { say = "visit 5 from ada", path = ["bump", "first", "again"] }'))
    from pyrtan.tree import load_tree

    tree = load_tree(tmp_path / "tally.toml")
    case = cases(tree)[0]
    assert failures(case, run_case(case, tree, with_tree([], tree))) == []


def test_a_service_keeps_memory_in_its_file(tmp_path):
    (tmp_path / "trees").mkdir()
    (tmp_path / "trees" / "tally.toml").write_text(TALLY)
    path = tmp_path / "memory.json"
    svc = Service(Registry.from_paths([tmp_path / "trees"]), name="s", memory=path)
    assert svc.run({"tree": "tally", "inputs": {"who": "ada"}})["output"] == {"count": 1}
    assert svc.run({"tree": "tally", "inputs": {"who": "ada"}})["output"] == {"count": 2}
    restarted = Service(Registry.from_paths([tmp_path / "trees"]), name="s", memory=path)
    assert restarted.run({"tree": "tally", "inputs": {"who": "ada"}})["output"] == {"count": 3}
    # A service with no file remembers for as long as it runs.
    fresh = Service(Registry.from_paths([tmp_path / "trees"]), name="t")
    fresh.run({"tree": "tally", "inputs": {"who": "ada"}})
    assert fresh.run({"tree": "tally", "inputs": {"who": "ada"}})["output"] == {"count": 2}


def test_the_run_and_memory_commands(tmp_path, capsys):
    (tmp_path / "trees").mkdir()
    (tmp_path / "trees" / "tally.toml").write_text(TALLY)
    mem = str(tmp_path / "memory.json")
    for _ in range(2):
        assert main(["run", "visit from ada", "--trees", str(tmp_path / "trees"), "--memory", mem, "--no-trace"]) == 0
    assert "visit 2 from ada" in capsys.readouterr().out
    assert main(["memory", mem]) == 0
    assert "tally/ada = 2  (written " in capsys.readouterr().out
    assert main(["memory", mem, "--json"]) == 0 and json.loads(capsys.readouterr().out)["keys"]["tally/ada"]["value"] == 2
    assert main(["memory", mem, "--forget", "tally/ada"]) == 0 and "forgot tally/ada" in capsys.readouterr().out
    assert main(["memory", mem, "--forget", "tally/ada"]) == 1
    assert main(["memory", mem]) == 0 and "nothing kept" in capsys.readouterr().out
    assert main(["memory", str(tmp_path / "nowhere.json")]) == 2
    (tmp_path / "bad.json").write_text("{oops")
    assert main(["run", "visit from ada", "--trees", str(tmp_path / "trees"), "--memory", str(tmp_path / "bad.json")]) == 2
    assert "not a memory file" in capsys.readouterr().err
