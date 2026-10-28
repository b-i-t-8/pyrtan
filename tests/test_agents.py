"""Agents beside the swarm: what they may suggest, where it is kept, and how
it becomes a tree (suggestions.py, agents.js, the console's Agents tab).

Nothing here runs a model. The agent's side is tested with a scripted one
(tests/agents.cjs) and, in a browser, with a scripted server that speaks
the OpenAI chat API, the way Ollama or llama.cpp would.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import ROOT
from pyrtan import suggestions as sg
from pyrtan.cli import main
from pyrtan.grants import ability_for
from pyrtan.hub import serve_hub
from pyrtan.registry import Registry
from pyrtan.service import Service
from pyrtan.tree import TreeError, load_tree

LEVEL = '''name = "level"
version = 1
namespace = "test"
license = "MIT"
description = "Whether a level from 0 to 10 is low or high."
hosts = []
start = "which"

[[triggers]]
pattern = "level {n:int}"
[[triggers]]
pattern = "is the level low at {n:int}"

[inputs.n]
type = "int"
description = "a level, 0 to 10"

[nodes.which]
kind = "decide"
on = "inputs.n"
branches = [
  { when = { between = [0, 3] }, then = "low" },
  { when = { between = [7, 10] }, then = "high" },
]

[nodes.low]
kind = "done"
say = "Level {{ inputs.n }} is low."

[nodes.high]
kind = "done"
say = "Level {{ inputs.n }} is high."
'''

BY = {"agent": "scout", "model": "test-model", "engine": "scripted"}
BRANCH = {"kind": "branch", "tree": "level", "node": "which", "on": "inputs.n", "observed": 5, "meaning": {"kind": "done", "say": "Level {{ inputs.n }} is in the middle.", "reason": "middle"}, "why": "between low and high", "by": BY}
TRIGGER = {"kind": "trigger", "tree": "level", "pattern": "set the level to {n:int}", "covers": ["set the level to 5"], "by": BY}
NEW_TREE = {"kind": "tree", "text": "top 3 stories on lobsters", "description": "Top stories from lobste.rs.", "hosts_hint": ["lobste.rs"], "by": BY}


@pytest.fixture
def trees(tmp_path):
    d = tmp_path / "trees"
    d.mkdir()
    (d / "level.toml").write_text(LEVEL)
    return d


# ---------------------------------------------------------------- what may be suggested


@pytest.mark.parametrize("bad, why", [
    ("nope", "a table"),
    ({"kind": "observe"}, "kind is one of"),
    ({**BRANCH, "meaning": {"kind": "observe", "say": "x"}}, "a done or a fail"),
    ({**BRANCH, "meaning": {"kind": "done", "say": "{{ env.TOKEN }}"}}, "not env"),
    ({**BRANCH, "meaning": {"kind": "done", "say": "x" * 301}}, "at most 300"),
    ({**BRANCH, "meaning": {"kind": "fail", "say": "x", "reason": "Not A Slug"}}, "slug"),
    ({k: v for k, v in BRANCH.items() if k != "observed"}, "observed"),
    ({**BRANCH, "tree": ""}, "names its tree"),
    ({**TRIGGER, "pattern": "{n}"}, "no literal words"),
    ({**TRIGGER, "covers": []}, "covers"),
    ({**NEW_TREE, "hosts_hint": "lobste.rs"}, "hosts_hint"),
    ({**NEW_TREE, "by": {"agent": 3}}, "short strings"),
])
def test_what_an_agent_may_not_suggest(bad, why):
    assert why in sg.problem(bad)


def test_what_it_may():
    for s in (BRANCH, TRIGGER, NEW_TREE):
        assert sg.problem(s) is None


def test_a_store_counts_bounds_and_keeps_across_restarts(tmp_path):
    path = tmp_path / "s.jsonl"
    store = sg.Suggestions(limit=2, path=path)
    kept, refused = store.add([BRANCH, {**BRANCH, "why": "newer words", "extra": "dropped"}, {"kind": "act"}, TRIGGER])
    assert len(kept) == 3 and refused == [{"index": 2, "why": "kind is one of branch, trigger, tree"}]
    entries = store.to_list()
    assert [(e["kind"], e["count"]) for e in entries] == [("branch", 2), ("trigger", 1)]
    assert entries[0]["why"] == "newer words" and "extra" not in entries[0]
    store.add([NEW_TREE])
    assert len(store) == 2  # the least recently seen went
    again = sg.Suggestions(path=path)
    assert {e["kind"]: e["count"] for e in again.to_list()} == {"branch": 2, "trigger": 1, "tree": 1}


def test_a_service_and_a_hub_keep_them(tmp_path, trees):
    service = Service(Registry.from_paths([trees]), suggestions=tmp_path / "s.jsonl")
    assert service.route("POST", "/suggestions", {"suggestions": [BRANCH, {"kind": "act"}]}) == (200, {"status": "ok", "kept": 1, "refused": [{"index": 1, "why": "kind is one of branch, trigger, tree"}]})
    assert service.route("POST", "/suggestions", TRIGGER)[1]["kept"] == 1
    assert service.route("POST", "/suggestions", {"what": 1})[0] == 400
    assert service.route("POST", "/suggestions", {"suggestions": [TRIGGER] * 51})[1]["reason"] == "too-many"
    status, body = service.route("GET", "/suggestions", None)
    assert status == 200 and [s["kind"] for s in body["suggestions"]] == ["trigger", "branch"]
    assert service.health()["suggestions"] == 2
    assert "/suggestions" in service.route("GET", "/api", None)[1]["endpoints"]
    # Sending one is as much as asking; reading them is the operator's.
    assert ability_for("POST", "/suggestions") == "ask"
    assert ability_for("GET", "/suggestions") == "operate"
    h, srv = serve_hub(port=0, quiet=True, trees=[trees], poll_every=60, suggestions=str(tmp_path / "hub.jsonl"))
    try:
        assert h.route("POST", "/suggestions", {"suggestions": [NEW_TREE]})[1]["kept"] == 1
        assert h.route("GET", "/suggestions", None)[1]["suggestions"][0]["text"] == "top 3 stories on lobsters"
        assert h.health()["suggestions"] == 1
    finally:
        h.stop()
        srv.stop()


# ---------------------------------------------------------------- how one becomes a tree


def test_a_branch_is_the_rules_draft_with_the_agents_words(trees):
    tree = load_tree(trees / "level.toml")
    d = sg.draft(tree, BRANCH)
    assert d.when == {"between": [4, 6]} and d.then == "which-5"
    assert 'kind = "done"' in d.node_text and 'say = "Level {{ inputs.n }} is in the middle."' in d.node_text
    assert "# Suggested by scout (test-model)" in d.node_text and "# between low and high" in d.node_text
    assert sg.draft(tree, {**BRANCH, "observed": 2}) is None  # covered already



def test_a_branch_suggestion_does_not_fit_a_lookup():
    from pyrtan.tree import load_tree_text

    tree = load_tree_text('name = "lv"\ndescription = "x"\nstart = "which"\n[inputs.n]\ntype = "int"\n'
                          '[nodes.which]\nkind = "lookup"\non = "inputs.n"\ninto = "w"\ntable = [{ lte = 3, value = "low" }]\nnext = "reply"\n'
                          '[nodes.reply]\nkind = "done"\nsay = "{{ state.w }}"\n')
    # A lookup's gap asks for a row; a branch suggestion's meaning is a node, which a row has nowhere to put.
    assert sg.draft(tree, {**BRANCH, "tree": "lv", "node": "which", "on": "inputs.n", "observed": 5}) is None


def test_a_trigger_is_kept_only_if_it_reads(trees):
    path = trees / "level.toml"
    tree = load_tree(path)
    text = sg.apply_trigger(path, tree, TRIGGER, Registry.from_paths([trees]))
    assert '# Suggested by scout (test-model): covers "set the level to 5"\n[[triggers]]\npattern = "set the level to {n:int}"\n' in text
    assert text.index("set the level") < text.index("[inputs.n]")  # beside the other triggers
    grown = Registry.from_paths([trees])
    assert grown.read("set the level to 5").winner.inputs == {"n": 5}
    tree = load_tree(path)
    with pytest.raises(TreeError, match="already answers"):
        sg.apply_trigger(path, tree, TRIGGER)
    with pytest.raises(TreeError, match="does not take as inputs"):
        sg.apply_trigger(path, tree, {**TRIGGER, "pattern": "set level {m:int}"})
    before = path.read_text()
    with pytest.raises(TreeError, match="none of the sentences it covers reads to this tree with it, that did not before"):
        sg.apply_trigger(path, tree, {**TRIGGER, "pattern": "turn the level to {n:int}", "covers": ["set the level to 5"]})
    assert path.read_text() == before


def test_grow_writes_suggestions_through_the_rules(tmp_path, trees, capsys):
    f = tmp_path / "pyrtan-suggestions.json"
    f.write_text(json.dumps({"pyrtan-suggestions": 1, "suggestions": [BRANCH, TRIGGER, NEW_TREE, {**BRANCH, "observed": 1}, {**BRANCH, "meaning": {"kind": "done", "say": "{{ env.X }}"}}]}))
    assert main(["grow", "--suggestions", str(f), "--trees", str(trees)]) == 0
    out = capsys.readouterr().out
    assert "{ when = { between = [4, 6] }, then = \"which-5\" }," in out and "nothing written" in out
    assert "refused branch" in out and "not env" in out and "is covered already" in out
    assert main(["grow", "--suggestions", str(f), "--trees", str(trees), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "3 written" in out
    reg = Registry.from_paths([trees])
    assert not reg.errors
    from pyrtan import RecordedTransport, Runner
    runner = Runner(reg, RecordedTransport({}))
    assert runner.run_intent("level 5").say == "Level 5 is in the middle."
    assert runner.run_intent("set the level to 9").say == "Level 9 is high."
    new = trees / "top-stories-lobsters.toml"
    assert 'description = "Top stories from lobste.rs."' in new.read_text() and "It guessed these hosts might answer: lobste.rs" in new.read_text()
    assert 'hosts = ["example.com"]' in new.read_text()  # a guess is never a host
    assert main(["check", str(trees), "--quiet"]) == 0


def test_suggestions_from_a_running_hub(tmp_path, trees, capsys):
    h, srv = serve_hub(port=0, quiet=True, trees=[trees], poll_every=60)
    try:
        h.route("POST", "/suggestions", {"suggestions": [BRANCH, TRIGGER]})
        assert main(["suggestions", "--at", srv.url]) == 0
        out = capsys.readouterr().out
        assert "branch  level.which: inputs.n = 5 means done: 'Level {{ inputs.n }} is in the middle.'" in out and "by scout (test-model): between low and high" in out
        assert main(["grow", "--suggestions", "--at", srv.url, "--trees", str(trees), "--json"]) == 0
        drafts = json.loads(capsys.readouterr().out)["drafts"]
        assert [d["suggestion"]["kind"] for d in drafts] == ["branch", "trigger"]
    finally:
        h.stop()
        srv.stop()


# ---------------------------------------------------------------- the agent, in node and in a browser


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_agents_js():
    run = subprocess.run(["node", str(ROOT / "tests" / "agents.cjs")], capture_output=True, text=True, cwd=str(ROOT))
    assert run.returncode == 0 and "agents ok" in run.stdout, run.stdout + run.stderr


def _reply(task: str, system: str) -> str:
    """What the scripted model says to each of the agent's jobs."""
    if "Use the skill terse now" in task:
        return "Zero to ten, it says."
    if "new sentences" in task:
        return json.dumps({"sentences": ["level 5", "level five", "set the level to 5", "level 2"]})
    if "no trigger reads them" in task:
        return 'Here you go: {"patterns": [{"pattern": "set the level to {n:int}", "covers": ["set the level to 5"]}], "why": "people set levels"}'
    if "no branch covers" in task:
        return json.dumps({"kind": "done", "say": "Level {{ inputs.n }} is in the middle.", "reason": "middle", "why": "between low and high"})
    if "You asked the swarm" in task:
        return "The level tree says: " + re.search(r"\(done\): (.*)", task).group(1)
    if "The person says" in task:
        return "Let me ask the trees.\nASK: is the level low at 2\nREMEMBER: the person asks about levels"
    return "ok"


class _Model(BaseHTTPRequestHandler):
    """An OpenAI-compatible chat server, scripted, that lets any page call it."""

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body["messages"]
        reply = _reply(msgs[-1]["content"], msgs[0]["content"])
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}]}).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def model_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def _node_playwright() -> bool:
    if shutil.which("node") is None:
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


def _drive(page: str, model: str) -> dict[str, str]:
    run = subprocess.run(["node", str(ROOT / "tests" / "browser" / "console_agents.cjs"), page, model], capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    said: dict[str, str] = {}
    for line in run.stdout.splitlines():
        word, _, rest = line.partition(" ")
        said[word] = rest
    assert "ERROR" not in said and "PAGEERROR" not in said and "DONE" in said, run.stdout + run.stderr
    return said


@pytest.mark.skipif(not _node_playwright(), reason="needs node with playwright, and a Chromium")
def test_the_console_runs_an_agent_beside_a_hub(trees, model_server):
    h, srv = serve_hub(port=0, quiet=True, trees=[trees], poll_every=60)
    try:
        said = _drive(srv.url, model_server)
        assert "scripted" in said["STARTED"] and "on http://127.0.0.1" in said["STARTED"]
        assert "4 sentences for level: 3 read to it, 1 read to nothing" in said["PROBED"]
        assert said["SUGGESTED"] == "branch,trigger"
        assert "accepted · kept at" in said["ACCEPTED"] and "dismissed" in said["ACCEPTED"]
        assert "Level 2 is low." in said["ASKED"]
        assert "no branch for inputs.n = 5" in said["LEVEL"]  # a hub's trees grow by pyrtan grow, not in the page
        assert "the level is out of ten" in said["KEPT"] and "dismissed the pattern" in said["KEPT"] and "too vague" in said["KEPT"]
        assert "terse" in said["SKILL"].split(",") and said["TERSE"].endswith("Zero to ten, it says.")
        assert said["NOGPU"].startswith("wllama qwen2.5-0.5b-instruct-q4_k_m,")
        assert "Run it on the CPU instead" in said["NOTICE"] and "Chrome or Edge" in said["NOTICE"] and "Firefox" in said["NOTICE"]
        kept = h.suggestions.to_list()
        assert [(s["kind"], s["meaning"]["say"]) for s in kept] == [("branch", "Level {{ inputs.n }} is in the middle.")]
        assert kept[0]["by"] == {"agent": "scout", "model": "scripted", "engine": "openai"}
        assert any(g["observed"] == 5 for g in h.gaps()["gaps"])  # the probe's gap is a real one
    finally:
        h.stop()
        srv.stop()


@pytest.mark.skipif(not _node_playwright(), reason="needs node with playwright, and a Chromium")
def test_the_console_grows_its_own_trees_when_it_is_the_hub(tmp_path, trees, model_server):
    site = tmp_path / "site"
    assert main(["console", "--out", str(site / "console" / "index.html")]) == 0
    assert main(["index", str(trees), "--copy", str(site / "catalog")]) == 0
    class Static(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(site), **k)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Static)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        said = _drive(f"http://127.0.0.1:{srv.server_address[1]}/console/", model_server)
        assert "beside this browser" in said["STARTED"]
        assert "grown in this page" in said["ACCEPTED"]
        assert said["LEVEL"] == "Level 5 is in the middle."
    finally:
        srv.shutdown()
