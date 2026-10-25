import pytest

from pyrtan.tree import TreeError, check, load_tree_text

MINIMAL = """
name = "t"
description = "a test"
hosts = ["example.com"]
start = "get"

[nodes.get]
kind = "observe"
http = { url = "https://example.com/" }
into = "r"
next = "ok"

[nodes.ok]
kind = "done"
say = "{{ state.r.status }}"
"""


def test_minimal_loads_clean():
    tree = load_tree_text(MINIMAL)
    assert tree.name == "t" and tree.start == "get"
    assert [p for p in check(tree) if p.level != "info"] == []
    assert tree.capability()["triggers"] == []


def problems(text: str) -> list[str]:
    try:
        tree = load_tree_text(text)
    except TreeError as e:
        return [str(e)]
    return [str(p) for p in check(tree)]


def test_start_buried_in_table_gets_a_hint():
    text = MINIMAL.replace('start = "get"\n', "") + '\nstart = "get"\n'
    with pytest.raises(TreeError, match="before the first"):
        load_tree_text(text)


def test_missing_target():
    text = MINIMAL.replace('next = "ok"', 'next = "nope"')
    with pytest.raises(TreeError, match="does not exist"):
        load_tree_text(text)


def test_cycles_are_refused():
    text = MINIMAL + """
[nodes.loop-a]
kind = "set"
set = { x = 1 }
next = "loop-b"

[nodes.loop-b]
kind = "set"
set = { x = 2 }
next = "loop-a"
"""
    with pytest.raises(TreeError, match="do not loop"):
        load_tree_text(text)


def test_host_not_listed():
    with pytest.raises(TreeError, match="not in this tree's hosts"):
        load_tree_text(MINIMAL.replace('hosts = ["example.com"]', 'hosts = ["other.com"]'))
    with pytest.raises(TreeError, match="must list the hosts"):
        load_tree_text(MINIMAL.replace('hosts = ["example.com"]', ""))


def test_wildcard_hosts():
    tree = load_tree_text(MINIMAL.replace('hosts = ["example.com"]', 'hosts = ["*.example.com"]').replace("https://example.com/", "https://api.example.com/"))
    assert tree.host_allowed("https://api.example.com/x")
    assert tree.host_allowed("https://example.com/")
    assert not tree.host_allowed("https://example.org/")
    assert not tree.host_allowed("not a url")


def test_observe_with_write_method_is_refused():
    with pytest.raises(TreeError, match="make this an act"):
        load_tree_text(MINIMAL.replace('http = { url = "https://example.com/" }', 'http = { method = "POST", url = "https://example.com/" }'))


def test_undeclared_input_in_template():
    with pytest.raises(TreeError, match="inputs.city, which is not declared"):
        load_tree_text(MINIMAL.replace("https://example.com/", "https://example.com/{{ inputs.city }}"))


def test_trigger_slot_must_be_an_input():
    text = MINIMAL + '\n[[triggers]]\npattern = "get {thing}"\n'
    with pytest.raises(TreeError, match="not a declared input"):
        load_tree_text(text)


def test_unreachable_node_warns_and_decide_without_else_informs():
    text = MINIMAL + """
[nodes.lonely]
kind = "decide"
on = "r.status"
branches = [ { when = { eq = 200 }, then = "ok" } ]
"""
    notes = problems(text)
    assert any("not reachable" in n for n in notes)
    assert any("no 'else'" in n for n in notes)


def test_state_reference_nobody_produces_warns():
    notes = problems(MINIMAL.replace("{{ state.r.status }}", "{{ state.ghost }}"))
    assert any("state.ghost, which no node produces" in n for n in notes)


def test_bad_input_definitions():
    with pytest.raises(TreeError, match="unknown type"):
        load_tree_text(MINIMAL + '\n[inputs.x]\ntype = "money"\n')
    with pytest.raises(TreeError, match="must be lowercase"):
        load_tree_text(MINIMAL.replace('name = "t"', 'name = "Not Ok"'))


def test_bind_inputs_defaults_coercion_and_limits():
    tree = load_tree_text(MINIMAL + '\n[inputs.n]\ntype = "int"\ndefault = 1\nmin = 1\nmax = 3\n\n[inputs.flag]\ntype = "bool"\ndefault = false\n\n[inputs.name]\ntype = "str"\n')
    assert tree.bind_inputs({"name": "x"}) == {"n": 1, "flag": False, "name": "x"}
    assert tree.bind_inputs({"name": "x", "n": "2", "flag": "yes"}) == {"n": 2, "flag": True, "name": "x"}
    with pytest.raises(TreeError, match="required"):
        tree.bind_inputs({})
    with pytest.raises(TreeError, match="at most 3"):
        tree.bind_inputs({"name": "x", "n": 4})
    with pytest.raises(TreeError, match="unknown inputs"):
        tree.bind_inputs({"name": "x", "zzz": 1})
    with pytest.raises(TreeError, match="must be int"):
        tree.bind_inputs({"name": "x", "n": "two"})


def test_each_needs_tree_xor_start_and_into():
    base = MINIMAL + """
[nodes.walk]
kind = "each"
over = "r.json"
into = "out"
next = "ok"
"""
    with pytest.raises(TreeError, match="exactly one of 'tree'"):
        load_tree_text(base)
    with pytest.raises(TreeError, match="exactly one of 'tree'"):
        load_tree_text(base.replace('over = "r.json"', 'over = "r.json"\ntree = "a"\nstart = "ok"'))
    notes = problems(base.replace('over = "r.json"', 'over = "r.json"\ntree = "a"'))
    assert any("no 'limit'" in n for n in notes)


def test_slot_kinds_must_fit_their_inputs():
    base = 'name = "t"\nstart = "s"\n[[triggers]]\npattern = "{pat}"\n[inputs.x]\ntype = "{typ}"\n[nodes.s]\nkind = "done"\nsay = "x"\n'
    for pat, typ in [("at {x:date}", "int"), ("wait {x:duration}", "bool"), ("n {x:float}", "int"), ("on {x:host}", "list")]:
        with pytest.raises(TreeError, match="produces a"):
            load_tree_text(base.format(pat=pat, typ=typ))
    for pat, typ in [("at {x:date}", "str"), ("wait {x:duration}", "int"), ("wait {x:duration}", "str"), ("n {x:number}", "int"), ("n {x:number}", "float"), ("n {x:int}", "float")]:
        load_tree_text(base.format(pat=pat, typ=typ))
    with pytest.raises(TreeError, match="language.synonyms"):
        load_tree_text('name = "t"\nstart = "s"\n[language]\nsynonyms = "no"\n[nodes.s]\nkind = "done"\nsay = "x"\n')
    with pytest.raises(TreeError, match="unknown key"):
        load_tree_text('name = "t"\nstart = "s"\n[language]\nstemming = true\n[nodes.s]\nkind = "done"\nsay = "x"\n')
