"""The console's identity, with no shell: tests/browser/console_gui.cjs drives it in Chromium."""

import json
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("cryptography")

from pyrtan import identity  # noqa: E402
from pyrtan.hub import serve_hub  # noqa: E402
from conftest import ROOT  # noqa: E402

ECHO = """
name = "echo"
description = "echoes"
start = "say"

[[triggers]]
pattern = "echo {text}"

[inputs.text]
type = "str"

[nodes.say]
kind = "done"
say = "echo: {{ inputs.text }}"
"""
PASS = "a long sentence nobody else would guess"


def _node_playwright() -> bool:
    if shutil.which("node") is None:
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="needs node with playwright, and a Chromium")
def test_the_console_does_identity_without_a_shell(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "echo.toml").write_text(ECHO)
    (tmp_path / "dl").mkdir()
    h, srv = serve_hub(port=0, quiet=True, trees=[tmp_path / "t"], federate=True, token="tok", poll_every=60)
    said: dict[str, str] = {}
    p = subprocess.Popen(["node", str(ROOT / "tests" / "browser" / "console_gui.cjs"), srv.url.rstrip("/"), str(tmp_path / "dl")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
    try:
        for line in p.stdout:  # a Chromium helper may hold the pipe open after node exits: stop at the last line
            word, _, rest = line.rstrip("\n").partition(" ")
            said[word] = rest
            if word == "ROOT":
                h.grants.allow(rest, ["ask", "chat", "operate"])
                p.stdin.write("go\n")
                p.stdin.flush()
            if word in ("DONE", "ERROR", "PAGEERROR"):
                break
    finally:
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
        h.stop()
        srv.stop()
    assert "ERROR" not in said and "PAGEERROR" not in said, said
    root = said["ROOT"]

    # The backup: refused the page until saved, then the file this console wrote opens with the CLI's code.
    assert "Save your backup now" in said["UNSAVED"]
    backup = json.loads(open(said["BACKUP"]).read())
    assert backup["id"] == root and backup["kdf"]["iterations"] == identity.BACKUP_ITERATIONS
    assert identity.open_backup(backup, PASS).id == root
    repo_link = urlsplit(said["PRIVATEREPO"])
    assert repo_link.path == "/b-i-t-8/pyrtan-identity/new/main" and json.loads(parse_qs(repo_link.query)["value"][0])["id"] == root

    # The claims: GitHub's new-file page, filled in with a claim the key signed.
    link = urlsplit(said["CLAIMLINK"])
    assert link.netloc == "github.com" and link.path == "/example/names/new/main/names"
    q = parse_qs(link.query)
    assert q["filename"] == ["alice.json"]
    claim = json.loads(q["value"][0])
    assert identity.check_name(claim, name="alice@names.example.org") is None and claim["id"] == root
    link = urlsplit(said["PYRTANLINK"])
    assert link.path == "/b-i-t-8/pyrtan/new/main/site/names"
    claim = json.loads(parse_qs(link.query)["value"][0])
    assert identity.check_name(claim, name="b-i-t-8@pyrtan.com") is None and claim["github"] == 212773752

    # The device: its note is the root's, and the hub answered it with no token, then refused it once revoked.
    note = json.loads(said["NOTE"])
    assert identity.check_device(note, root=root) is None and note["device"] == said["DEVICE"] and note["may"] == ["ask", "chat"]
    assert int(said["GRANTS"]) == 2
    assert "echo: from the phone" in said["ASKED"]
    assert "not-granted" in said["REVOKED"] or "may not" in said["REVOKED"]

    # Restored in another browser: the same identity, with its devices.
    restored, rows = said["RESTORED"].split()
    assert restored == root and int(rows) >= 1
