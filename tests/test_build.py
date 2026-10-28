"""The builder: an unanswered sentence becomes a tree through a few
questions, every default passes check, every bad answer names its
question, and the file it writes runs."""

import pytest

from pyrtan import RecordedTransport, Registry, Runner
from pyrtan.build import SOURCES, BuildError, build, questions
from pyrtan.intent import Trigger
from pyrtan.tree import load_tree_text

SENTENCE = "what's the latest release of astral-sh/uv"


@pytest.mark.parametrize("source", list(SOURCES))
def test_every_source_builds_clean_from_defaults(source):
    built = build(SENTENCE, {"source": source})
    assert built.ok, built.problems
    assert built.tree is not None and built.tree.name == "latest-release"
    assert [t.pattern for t in built.tree.triggers] == ["what's the latest release of {target:word}"]
    assert Trigger(built.answers["trigger"]).match(SENTENCE) == {"target": "astral-sh/uv"}
    keys = [q.key for q in built.questions]
    assert keys[:4] == ["name", "description", "trigger", "source"] and keys[-1] == "say"
    if source == "none":
        assert built.tree.start == "reply" and built.tree.node("reply").kind == "done"
    else:
        assert built.tree.node("status").kind == "decide"
        assert built.tree.node("reply").kind == "done"


def test_questions_follow_the_source():
    assert [q.key for q in questions("tell me a joke")] == ["name", "description", "trigger", "source", "url", "format", "pick", "branches", "say"]
    assert [q.key for q in questions("tell me a joke", {"source": "shell"})] == ["name", "description", "trigger", "source", "command", "args", "pick", "branches", "say"]
    assert [q.key for q in questions("tell me a joke", {"source": "file"})] == ["name", "description", "trigger", "source", "path", "tail", "pick", "branches", "say"]
    assert [q.key for q in questions("tell me a joke", {"source": "browser"})] == ["name", "description", "trigger", "source", "url", "wait_for", "pick", "branches", "say"]
    assert [q.key for q in questions("tell me a joke", {"source": "delegate"})] == ["name", "description", "trigger", "source", "intent", "branches", "say"]
    assert [q.key for q in questions("tell me a joke", {"source": "none"})] == ["name", "description", "trigger", "source", "say"]
    q = {q.key: q for q in questions("tell me a joke")}
    assert q["name"].default == "joke" and q["source"].answer == "http" and [c["value"] for c in q["source"].to_dict()["choices"]] == list(SOURCES)


def test_a_host_in_the_sentence_opens_hosts_and_fills_the_url():
    built = build("is example.com up")
    assert built.ok and built.tree.hosts == ["*"]
    assert built.tree.node("fetch").get("http")["url"] == "https://{{ inputs.host | lower }}/"
    assert "{{ inputs.host }}: resp.status was" in built.tree.node("down").get("say")


def test_a_json_api_is_guessed_and_the_tree_runs():
    built = build(SENTENCE, {"url": "https://api.github.com/repos/{{ inputs.target }}/releases/latest", "pick": "tag_name", "say": "{{ inputs.target }}: {{ state.tag_name }}"})
    assert built.ok, built.problems
    assert built.answers["format"] == "json" and built.tree.hosts == ["api.github.com"]
    assert built.tree.node("fetch").get("extract") == [{"into": "tag_name", "json": "json.tag_name"}]
    transport = RecordedTransport()
    transport.add("GET", "https://api.github.com/repos/astral-sh/uv/releases/latest", 200, {"tag_name": "0.9.0"})
    registry = Registry()
    registry.add(built.tree)
    runner = Runner(registry, transport)
    out = runner.run_intent(SENTENCE)
    assert out.status == "done" and out.say == "astral-sh/uv: 0.9.0"
    assert [s.node for s in out.trace.steps] == ["intent", "fetch", "status", "have-tag-name", "reply"]
    transport.add("GET", "https://api.github.com/repos/astral-sh/uv/releases/latest", 404, {})
    out = runner.run_intent(SENTENCE)
    assert out.status == "failed" and out.reason == "not-found"
    transport.add("GET", "https://api.github.com/repos/astral-sh/uv/releases/latest", 200, {"other": 1})
    out = runner.run_intent(SENTENCE)
    assert out.status == "failed" and out.reason == "no-tag-name"


def test_branches_read_by_shape():
    built = build("ping", {"branches": "200=reply, >=500=down, 403|429=rate-limited, 300-399=moved, /^4/=client, missing=gone, else=odd"})
    assert built.ok, built.problems
    branches = built.tree.node("status").get("branches")
    assert branches == [
        {"when": {"eq": 200}, "then": "have-title"},
        {"when": {"gte": 500}, "then": "down"},
        {"when": {"in": [403, 429]}, "then": "rate-limited"},
        {"when": {"between": [300, 399]}, "then": "moved"},
        {"when": {"matches": "^4"}, "then": "client"},
        {"when": {"exists": False}, "then": "gone"},
        {"else": "odd"},
    ]
    assert {n for n in built.tree.nodes if built.tree.node(n).kind == "fail"} == {"down", "rate-limited", "moved", "client", "gone", "odd", "no-title"}


def test_shell_and_file_sources():
    built = build("how full is the disk", {"source": "shell", "command": "df", "args": "-P /", "pick": "/(\\d+)%/", "say": "{{ state.hit }}% full"})
    assert built.ok, built.problems
    assert built.tree.commands == ["df"]
    assert built.tree.node("run").get("shell") == {"command": "df", "args": ["-P", "/"]}
    assert built.tree.node("run").get("extract") == [{"into": "hit", "regex": "(\\d+)%", "group": 1}]
    built = build("any errors in the log", {"source": "file", "path": "/var/log/syslog", "tail": "50", "pick": "/(error.*)/"})
    assert built.ok, built.problems
    assert built.tree.paths == ["/var/log/syslog"] and built.tree.node("read").get("file") == {"path": "/var/log/syslog", "tail": 50}
    assert built.tree.node("status").get("on") == "file.exists"


def test_a_changed_trigger_brings_its_own_inputs():
    built = build("weather in oslo", {"trigger": "[what's the] weather in {place}", "url": "https://api.example.com/w?q={{ inputs.place | url }}", "pick": "json.temp", "say": "{{ inputs.place }}: {{ state.temp }}°"})
    assert built.ok, built.problems
    assert set(built.tree.inputs) == {"place"} and built.tree.inputs["place"].type == "str"
    built = build("top 3 stories", {"trigger": "top {count:int} stories [please]"})
    assert built.ok and built.tree.inputs["count"].type == "int"


def test_delegate_warns_when_it_would_ask_itself():
    built = build("picnic in oslo", {"source": "delegate"})
    assert built.ok
    assert [p["level"] for p in built.problems] == ["warn"] and "asks itself" in built.problems[0]["message"]
    built = build("picnic in oslo", {"source": "delegate", "intent": "weather in oslo"})
    assert built.ok and built.problems == []
    assert built.tree.node("ask-swarm").kind == "delegate" and built.tree.node("ask-swarm").get("intent") == "weather in oslo"


@pytest.mark.parametrize(
    "answers, key, words",
    [
        ({"name": "!!"}, "name", "will not do as a name"),
        ({"trigger": "weather in {place}"}, "trigger", "does not match"),
        ({"trigger": "{x} {y}"}, "trigger", "does not compile"),
        ({"source": "magic"}, "source", "not one of"),
        ({"url": "ftp://x"}, "url", "http://"),
        ({"url": "https://{{ inputs.nope }}/"}, "url", "names nothing"),
        ({"pick": "/(/"}, "pick", "does not compile"),
        ({"branches": "x"}, "branches", "value=node"),
        ({"branches": "200=nowhere"}, "branches", "reaches reply"),
        ({"branches": "200=reply, 200=again"}, "branches", "twice"),
        ({"branches": "200=reply, 9-1=odd"}, "branches", "backwards"),
        ({"say": "{{ foo }}"}, "say", "starts with 'foo'"),
        ({"source": "shell", "command": "df -h"}, "command", "one program"),
        ({"source": "file", "path": "notes.txt"}, "path", "absolute"),
        ({"source": "file", "tail": "many"}, "tail", "whole number"),
    ],
)
def test_a_bad_answer_names_its_question(answers, key, words):
    with pytest.raises(BuildError) as e:
        build("top 3 stories on hacker news", answers)
    assert e.value.key == key and words in str(e.value)


def test_an_empty_answer_takes_the_default():
    built = build("tell me a joke", {"say": "", "source": " ", "url": ""})
    assert built.ok and built.answers["source"] == "http" and built.answers["say"] == "{{ state.title }}"


def test_quotes_and_backslashes_survive_the_file():
    built = build('say "hi" to c:\\temp', {"say": 'He said "hi" at {{ now.date }}', "description": 'quotes "and" backslashes \\'})
    assert built.ok, built.problems
    tree = load_tree_text(built.toml)
    assert tree.description == 'quotes "and" backslashes \\' and tree.node("reply").get("say") == 'He said "hi" at {{ now.date }}'


def test_to_dict_carries_the_form():
    d = build("tell me a joke", {"source": "none", "say": "No."}).to_dict()
    assert d["ok"] is True and d["name"] == "joke" and d["answers"]["say"] == "No."
    assert [q["key"] for q in d["questions"]] == ["name", "description", "trigger", "source", "say"]
    assert d["questions"][3]["answer"] == "none" and d["questions"][4]["answer"] == "No." and d["questions"][4]["default"] == "Done."
    assert d["toml"].startswith("# joke: built by pyrtan build")
