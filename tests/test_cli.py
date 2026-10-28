import json

import pytest

from pyrtan.cli import main
from conftest import EXAMPLES, FIXTURES


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_check_examples(capsys):
    code, out, _ = run(capsys, "check", str(EXAMPLES))
    assert code == 0 and "weather: 15 nodes" in out


def test_check_reports_errors(tmp_path, capsys):
    (tmp_path / "bad.toml").write_text('name = "bad"\nstart = "x"\n[nodes.x]\nkind = "done"\nnext = "y"\n')
    code, out, _ = run(capsys, "check", str(tmp_path))
    assert code == 1 and "error" in out


def test_list(capsys):
    code, out, _ = run(capsys, "list", str(EXAMPLES))
    assert code == 0 and "top {count:int} stories on hacker news" in out
    code, out, _ = run(capsys, "list", "--json", str(EXAMPLES))
    assert {t["name"] for t in json.loads(out)["trees"]} >= {"weather"}


def test_run_with_replay_text_and_json(capsys):
    code, out, _ = run(capsys, "run", "weather in Oslo", "--trees", str(EXAMPLES), "--replay", str(FIXTURES / "examples.json"))
    assert code == 0
    assert "trace: weather" in out and out.strip().endswith("wind 12.3 km/h.")
    code, out, _ = run(capsys, "run", "--tree", "hacker-news-item", "--input", "id=101", "--trees", str(EXAMPLES), "--replay", str(FIXTURES / "examples.json"), "--json", "--no-trace")
    assert code == 0
    data = json.loads(out)
    assert data["output"]["title"] == "Pyrtan 0.1" and "trace" not in data


def test_run_exit_codes(capsys):
    fixture = str(FIXTURES / "examples.json")
    assert run(capsys, "run", "is broken.example up", "--trees", str(EXAMPLES), "--replay", fixture)[0] == 1
    assert run(capsys, "run", "nothing here", "--trees", str(EXAMPLES), "--replay", fixture)[0] == 3
    assert run(capsys, "run", "--trees", str(EXAMPLES))[0] == 2


def test_record_writes_a_fixture(tmp_path, capsys):
    target = tmp_path / "rec.json"
    code, _, _ = run(capsys, "run", "hacker news item 101", "--trees", str(EXAMPLES), "--replay", str(FIXTURES / "examples.json"), "--record", str(target), "--no-trace")
    assert code == 0
    assert "GET https://hacker-news.firebaseio.com/v0/item/101.json" in json.loads(target.read_text())


def test_new_scaffolds_a_tree_that_checks(tmp_path, capsys):
    code, out, _ = run(capsys, "new", "my-thing", "--dir", str(tmp_path))
    assert code == 0 and (tmp_path / "my-thing.toml").exists()
    code, out, _ = run(capsys, "check", str(tmp_path / "my-thing.toml"))
    assert code == 0, out
    assert run(capsys, "new", "my-thing", "--dir", str(tmp_path))[0] == 1


def test_new_from_a_sentence(tmp_path, capsys):
    code, out, _ = run(capsys, "new", "--from", "top 3 stories on hacker news", "--dir", str(tmp_path))
    assert code == 0 and "top {count:int} stories on hacker news" in out
    path = tmp_path / "top-stories-hacker-news.toml"
    assert path.exists() and "[inputs.count]" in path.read_text()
    assert run(capsys, "check", str(path))[0] == 0
    code, out, _ = run(capsys, "new", "hn", "--from", "top 3 stories on hacker news", "--dir", str(tmp_path))
    assert code == 0 and (tmp_path / "hn.toml").exists()
    assert run(capsys, "new", "--dir", str(tmp_path))[0] == 2


def test_run_no_match_proposes_a_tree(capsys):
    code, out, _ = run(capsys, "run", "tell me a riddle", "--trees", str(EXAMPLES), "--replay", str(FIXTURES / "examples.json"), "--json")
    assert code == 3
    data = json.loads(out)
    assert data["reason"] == "no-match" and data["output"]["proposal"]["name"] == "riddle"


def test_gaps_needs_a_target(capsys):
    assert run(capsys, "gaps")[0] == 2
    assert run(capsys, "unanswered")[0] == 2


def test_unanswered_lists_and_scaffolds(tmp_path, capsys):
    from pyrtan import Registry
    from pyrtan.service import Server, Service, http_json

    svc = Service(Registry.from_paths([EXAMPLES]), name="x")
    server = Server(svc.route, quiet=True).start()
    try:
        code, out, _ = run(capsys, "unanswered", "--at", server.url)
        assert code == 0 and "nothing unanswered" in out
        http_json("POST", f"{server.url}/intent", {"text": "ping example.org"})
        http_json("POST", f"{server.url}/intent", {"text": "ping example.org"})
        http_json("POST", f"{server.url}/intent", {"text": "tell me a riddle"})
        code, out, _ = run(capsys, "unanswered", "--at", server.url)
        assert code == 0 and out.startswith("'ping example.org'  x2") and "would be: host-ping  answering  ping {host:host}" in out and "pyrtan new riddle --from" in out
        code, out, _ = run(capsys, "unanswered", "--at", server.url, "--json")
        assert [e["text"] for e in json.loads(out)["unanswered"]] == ["ping example.org", "tell me a riddle"]
        code, out, _ = run(capsys, "unanswered", "--at", server.url, "--scaffold", str(tmp_path))
        assert code == 0 and "wrote" in out and (tmp_path / "host-ping.toml").exists() and (tmp_path / "riddle.toml").exists()
        assert run(capsys, "check", str(tmp_path))[0] == 0
        code, out, _ = run(capsys, "unanswered", "--at", server.url, "--scaffold", str(tmp_path))
        assert "kept" in out and "wrote" not in out
    finally:
        server.stop()


def test_match_explains_a_sentence(capsys):
    code, out, _ = run(capsys, "match", "Hey Pyrtan, what's the forecast in Oslo please?", "--trees", str(EXAMPLES))
    assert code == 0
    assert "read as:  'what is the forecast in Oslo'" in out
    assert "fillers: 'Hey Pyrtan', 'please'" in out and "contractions: what's->what is" in out
    assert "winner: weather" in out and '"place": "Oslo"' in out
    code, out, _ = run(capsys, "match", "is example dot com up", "--trees", str(EXAMPLES))
    assert code == 1 and "no trigger matched" in out
    code, out, _ = run(capsys, "match", "weather in Oslo", "--trees", str(EXAMPLES), "--json")
    assert code == 0 and json.loads(out)["matches"][0]["tree"] == "weather"
    assert run(capsys, "match", "x", "--trees", str(EXAMPLES), "--now", "yesterday")[0] == 2
