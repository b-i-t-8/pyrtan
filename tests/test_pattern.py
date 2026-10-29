"""Reading text with a trigger's pattern: ``extract = [{ into, pattern }]``."""

import pytest

from pyrtan.build import _pick_words, _read_pick, _value_name
from pyrtan.extract import ExtractError, extract, validate
from pyrtan.intent import Trigger
from pyrtan.template import MISSING
from pyrtan.tree import TreeError, check, load_tree_text

PANEL = """Now: Temperature: 21.5°C, feels 19
Wind 12 km/h from the NW
Status:   all systems go
Updated: 25 oct 2026
Maxtemperature: 99
used 1,234 of 5,000 MB, used 7 of 8 MB
"""


def read(pattern: str, text: str = PANEL, **more):
    entry = {"into": "x", "pattern": pattern, **more}
    validate(entry)
    return extract(entry, {"body": text})


def test_one_slot_gives_its_value_read_by_its_kind():
    assert read("temperature: {t:number}°C") == 21.5
    assert read("feels {f:int}") == 19
    assert read("updated: {d:date}") == "2026-10-25"
    assert read("pressure {p:number}") is MISSING


def test_several_slots_give_a_table():
    assert read("wind {speed:number} km/h from the {dir:word}") == {"speed": 12, "dir": "NW"}
    assert read("used {n:number} of {of:number} MB", all=True) == [{"n": 1234, "of": 5000}, {"n": 7, "of": 8}]


def test_it_reads_as_a_trigger_does():
    # Case ignored, a word matches its plural, [optional] words, (a|b) choices.
    assert read("WIND {s:int} km/h") == 12
    assert read("[current] temperatures: {t:number}") == 21.5
    assert read("(status|state): {s:word}") == "all"


def test_literal_words_are_whole_words():
    # "temperature" does not match inside "Maxtemperature", nor "ature" inside "Temperature".
    assert read("temperature: {t:number}", all=True) == [21.5]
    assert read("ature: {n:number}") is MISSING
    # A word touching a slot in the pattern touches it in the text too.
    assert read("{n:int}MB", text="free 512MB") == 512


def test_a_lazy_slot_at_the_end_reads_to_the_end_of_its_line():
    assert read("status: {s}") == "all systems go"
    assert read("status: {s:text}", text="Status: up\nnext line") == "up"


def test_an_occurrence_that_does_not_read_is_passed_over():
    assert read("on {d:date}", text="on 2026-13-45\non 2026-11-02") == "2026-11-02"
    assert read("on {d:date}", text="on someday") is MISSING
    assert read("on {d:date}", text="on someday", all=True) == []


def test_text_is_folded_as_a_sentence_is():
    assert read("it's {n:int}", text="It’s 30 out") == 30
    assert read("temp {t:number}", text="temp  7") == 7
    assert read("{t:number}°C", text="21.5℃") == 21.5  # NFKC


def test_it_reads_from_another_field():
    assert extract({"into": "x", "pattern": "load {l:number}", "from": "stdout"}, {"stdout": "cpu load 0.42"}) == 0.42
    assert extract({"into": "x", "pattern": "load {l:number}", "from": "stdout"}, {"body": "load 1"}) is MISSING


@pytest.mark.parametrize("pattern, words", [
    ("{x}", "has no literal words"),
    ("temperature", "has no {slot}, so it reads nothing"),
    ("temp {x:colour}", "kind must be one of"),
    ("temp [{x}", "unbalanced [ in pattern"),
    (3, "must be a string"),
])
def test_a_bad_pattern_is_refused(pattern, words):
    with pytest.raises(ExtractError, match="extract 'x'") as e:
        validate({"into": "x", "pattern": pattern})
    assert words in str(e.value)


def test_triggers_are_unchanged():
    t = Trigger("weather in {city}")
    assert t.match("weather in Oslo") == {"city": "Oslo"}
    assert t.match("the weather in Oslo") is None  # a trigger still matches the whole sentence


def test_check_holds_a_tree_to_its_patterns():
    tree = """
name = "panel"
description = "reads a panel"
start = "read"
[inputs.text]
type = "str"
[nodes.read]
kind = "observe"
from = "inputs.text"
extract = [ { into = "t", pattern = "temperature: {t:number}°C" } ]
next = "done"
[nodes.done]
kind = "done"
say = "{{ state.t }}"
"""
    assert not [p for p in check(load_tree_text(tree)) if p.level == "error"]
    with pytest.raises(TreeError, match=r"nodes.read.extract\[0\]: the pattern in extract 't' has no \{slot\}, so it reads nothing"):
        load_tree_text(tree.replace("{t:number}°C", "°C"))


def test_the_builder_takes_a_pattern():
    pick = _read_pick("load average: {load:number}", "text")
    assert pick == {"pattern": "load average: {load:number}"}
    assert _value_name(pick) == "load" and _pick_words(pick) == "the pattern load average: {load:number}"
    assert _value_name(_read_pick("used {a:int} of {b:int}", "html")) == "hit"
    assert _read_pick("/x(\\d)/", "text") == {"regex": "x(\\d)", "group": 1}  # a regex is still a regex


def test_a_built_tree_reads_with_its_pattern():
    from pyrtan import RecordedTransport, Registry, Runner
    from pyrtan.build import build

    built = build("how full is the disk", {"source": "shell", "command": "df", "args": "-P /", "pick": "{used:int}% /", "say": "{{ state.used }}% full"})
    assert built.ok, built.problems
    assert built.tree.node("run").get("extract") == [{"into": "used", "pattern": "{used:int}% /"}]
    reg = Registry()
    reg.add(built.tree)
    out = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000 470 530 47% /\n"
    fixture = {"shell df -P /": {"status": 0, "ok": True, "stdout": out, "stderr": "", "body": out}}
    assert Runner(reg, RecordedTransport(fixture)).run_tree(built.tree.name, {}).say == "47% full"
