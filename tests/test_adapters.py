"""The other forms of I/O: file, shell, serial, queue, browser; their
allow-lists, their fixtures, and the egress policy over them."""

from __future__ import annotations

import http.server
import json
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from pyrtan import Registry, RecordedTransport, Runner
from pyrtan.adapters import (
    LocalAdapters,
    _mqtt_packet,
    _mqtt_read,
    _mqtt_str,
    adapter_key,
    command_allowed,
    device_allowed,
    path_allowed,
    topic_allowed,
)
from pyrtan.cli import main
from pyrtan.transport import EgressRefused, RecordingTransport, TransportError
from pyrtan.tree import TreeError, check, load_tree_text


def registry(tmp_path: Path, **trees: str) -> Registry:
    d = tmp_path / "trees"
    d.mkdir(exist_ok=True)
    for name, text in trees.items():
        (d / f"{name}.toml").write_text(text)
    reg = Registry.from_paths([d])
    assert not reg.errors, reg.errors
    return reg


# ---------------------------------------------------------------- allow-lists


def test_allow_lists():
    assert path_allowed(["/var/log/*"], "/var/log/syslog") and path_allowed(["/var/log"], "/var/log/nginx/access.log")
    assert not path_allowed(["/var/log"], "/var/logs") and not path_allowed(["/var/log/*.log"], "/etc/passwd") and not path_allowed([], "/x")
    assert path_allowed(["*"], "/anything") and path_allowed(["~/notes"], os.path.expanduser("~/notes/today.md"))
    assert command_allowed(["df", "uptime"], "df") and command_allowed(["df"], "/usr/bin/df") and command_allowed(["/usr/bin/df"], "/usr/bin/df")
    assert not command_allowed(["df"], "dfx") and not command_allowed(["/usr/bin/df"], "/bin/df") and command_allowed(["*"], "rm")
    assert device_allowed(["/dev/ttyUSB*"], "/dev/ttyUSB0") and not device_allowed(["/dev/ttyUSB*"], "/dev/ttyACM0")
    assert topic_allowed(["home/+/temperature"], "home/kitchen/temperature") and not topic_allowed(["home/+/temperature"], "home/kitchen/humidity")
    assert topic_allowed(["home/#"], "home/a/b/c") and topic_allowed(["home/#"], "home") and not topic_allowed(["home/#"], "work/a")
    assert topic_allowed(["home/*"], "home/a") and not topic_allowed(["home/*"], "home/a/b") and topic_allowed(["#"], "x/y") and not topic_allowed(["home/a"], "home/a/b")


def test_adapter_keys_are_stable():
    assert adapter_key("file", {"path": "/var/log/syslog", "tail": 3}) == "file /var/log/syslog"
    assert adapter_key("file", {"path": "/tmp/x", "append": "hi"}) == "file append /tmp/x"
    assert adapter_key("shell", {"command": "df", "args": ["-P", "/", 2]}) == "shell df -P / 2"
    assert adapter_key("serial", {"device": "/dev/ttyUSB0", "write": "AT\r\n"}) == 'serial /dev/ttyUSB0 "AT\\r\\n"'
    assert adapter_key("queue", {"url": "mqtt://b", "topic": "t", "publish": "on"}) == "queue publish mqtt://b t"
    assert adapter_key("browser", {"url": "https://x/"}) == "browser https://x/"


# ---------------------------------------------------------------- file


FILE_TREE = """
name = "notes"
description = "reads and appends a notes file"
paths = ["{root}"]
start = "read"

[inputs.path]
type = "str"

[nodes.read]
kind = "observe"
file = {{ path = "{{{{ inputs.path }}}}", tail = 2 }}
into = "f"
extract = [{{ into = "last", regex = "(?m)^(.*)$", group = 1, all = true }}]
next = "exists"

[nodes.exists]
kind = "decide"
on = "f.exists"
branches = [{{ when = {{ eq = true }}, then = "note" }}, {{ else = "none" }}]

[nodes.note]
kind = "act"
file = {{ path = "{{{{ inputs.path }}}}", append = "visited at {{{{ now.iso }}}}\\n" }}
into = "w"
next = "say"

[nodes.say]
kind = "done"
say = "{{{{ state.f.lines | length }}}} line(s), {{{{ state.f.size }}}} B; wrote {{{{ state.w.written }}}} B"

[nodes.none]
kind = "fail"
reason = "no-file"
say = "{{{{ inputs.path }}}} is not there"
"""


def test_file_read_tail_and_append(tmp_path):
    reg = registry(tmp_path, notes=FILE_TREE.format(root=str(tmp_path / "data")))
    (tmp_path / "data").mkdir()
    f = tmp_path / "data" / "notes.txt"
    f.write_text("one\ntwo\nthree\n")
    out = Runner(reg).run_tree("notes", {"path": str(f)})
    assert out.status == "done", out.say
    assert out.say.startswith("2 line(s), 14 B; wrote ")
    assert f.read_text().startswith("one\ntwo\nthree\nvisited at 20")
    assert out.trace.steps[0].summary == f"file {f} -> 14 B; last=['two', 'three']"
    assert out.trace.steps[2].summary.startswith(f"file {f} -> wrote ")
    out = Runner(reg).run_tree("notes", {"path": str(tmp_path / "data" / "missing.txt")})
    assert out.status == "failed" and out.reason == "no-file" and "missing" in out.trace.steps[0].summary
    # Outside the tree's paths: refused at run time, by the runtime, before the adapter.
    out = Runner(reg).run_tree("notes", {"path": "/etc/hostname"})
    assert out.status == "error" and out.reason == "path-not-allowed" and "outside this tree's paths" in out.say
    # And ".." does not escape.
    out = Runner(reg).run_tree("notes", {"path": str(tmp_path / "data" / ".." / "secret")})
    assert out.reason == "path-not-allowed"
    # A whole file, and JSON parsed.
    reg2 = registry(tmp_path, j='''
name = "j"
paths = ["*"]
start = "r"
[nodes.r]
kind = "observe"
file = { path = "%s" }
into = "f"
next = "d"
[nodes.d]
kind = "done"
say = "{{ state.f.json.a }}"
''' % (tmp_path / "x.json"))
    (tmp_path / "x.json").write_text('{"a": 42}')
    assert Runner(reg2).run_tree("j").say == "42"


def test_file_checker():
    base = '''
name = "f"
paths = ["/var/log"]
start = "r"
[nodes.r]
kind = "observe"
file = { path = "/var/log/syslog" }
next = "d"
[nodes.d]
kind = "done"
'''
    tree = load_tree_text(base)
    assert tree.sources() == ["file"] and tree.capability()["paths"] == ["/var/log"] and tree.capability()["sources"] == ["file"]
    with pytest.raises(TreeError, match="make this an act"):
        load_tree_text(base.replace('path = "/var/log/syslog"', 'path = "/var/log/syslog", append = "x"'))
    with pytest.raises(TreeError, match="not under this tree's paths"):
        load_tree_text(base.replace("/var/log/syslog", "/etc/passwd"))
    with pytest.raises(TreeError, match="must list the paths"):
        load_tree_text(base.replace('paths = ["/var/log"]', ""))
    with pytest.raises(TreeError, match="exactly one of"):
        load_tree_text(base.replace('file = { path = "/var/log/syslog" }', 'file = { path = "/var/log/syslog" }\nshell = { command = "df" }'))
    notes = [str(p) for p in check(load_tree_text(base.replace('paths = ["/var/log"]', 'paths = ["/var/log"]\ncommands = ["df"]')))]
    assert any("commands are listed but no node uses them" in n for n in notes)
    notes = [str(p) for p in check(load_tree_text(base.replace("/var/log/syslog", "/var/log/{{ inputs.name }}").replace('start = "r"', 'start = "r"\n[inputs.name]\ntype = "str"')))]
    assert any("decided at run time" in n for n in notes)


# ---------------------------------------------------------------- shell


SHELL_TREE = """
name = "say"
description = "runs a program"
commands = ["printf", "{py}", "false", "sleep"]
start = "run"

[inputs.text]
type = "str"
default = "hi"

[nodes.run]
kind = "observe"
shell = {{ command = "printf", args = ["%s|%s", "{{{{ inputs.text }}}}", "$HOME;ls"] }}
into = "p"
retries = 0
next = "ok"

[nodes.ok]
kind = "decide"
on = "p.ok"
branches = [{{ when = {{ eq = true }}, then = "say" }}, {{ else = "bad" }}]

[nodes.say]
kind = "done"
say = "{{{{ state.p.stdout }}}}"

[nodes.bad]
kind = "fail"
reason = "exit"
say = "exit {{{{ state.p.status }}}}: {{{{ state.p.stderr | strip }}}}"
"""


def test_shell_runs_one_program_without_a_shell(tmp_path):
    reg = registry(tmp_path, say=SHELL_TREE.format(py=sys.executable))
    out = Runner(reg).run_tree("say", {"text": "a b"})
    assert out.status == "done" and out.say == "a b|$HOME;ls"  # the arguments reached printf as given: no expansion, no second command
    assert out.trace.steps[0].summary == "shell printf %s|%s a b $HOME;ls -> exit 0"
    # Exit status is observed, not an error; stderr too; stdout as JSON when it is.
    reg2 = registry(tmp_path, say=SHELL_TREE.format(py=sys.executable).replace('shell = { command = "printf", args = ["%s|%s", "{{ inputs.text }}", "$HOME;ls"] }', 'shell = { command = "%s", args = ["-c", "import sys; print(\'{\\"n\\": 3}\'); sys.stderr.write(\'careful\'); sys.exit(2)"] }' % sys.executable).replace('say = "{{ state.p.stdout }}"', 'say = "{{ state.p.json.n }}"'))
    out = Runner(reg2).run_tree("say")
    assert out.status == "failed" and out.say == "exit 2: careful"
    # A program not on the box, or too slow, is a transport error (an observe retries; here retries = 0).
    reg3 = registry(tmp_path, say=SHELL_TREE.format(py=sys.executable).replace('command = "printf"', 'command = "{{ inputs.text }}"'))
    out = Runner(reg3).run_tree("say", {"text": "no-such-program-xyz"})
    assert out.status == "error" and out.reason == "command-not-allowed"
    out = Runner(reg3).run_tree("say", {"text": "sleep"})  # sleep "%s|%s" ... exits non-zero: fine, that is observed
    assert out.status == "failed"
    reg4 = registry(tmp_path, say=SHELL_TREE.format(py=sys.executable).replace('shell = { command = "printf", args = ["%s|%s", "{{ inputs.text }}", "$HOME;ls"] }', 'shell = { command = "sleep", args = ["5"] }\ntimeout = 0.3'))
    t0 = time.time()
    out = Runner(reg4).run_tree("say")
    assert out.status == "error" and out.reason == "transport" and "no answer within" in out.say and time.time() - t0 < 3
    with pytest.raises(TreeError, match="not in this tree's commands"):
        load_tree_text(SHELL_TREE.format(py=sys.executable).replace('command = "printf"', 'command = "rm"'))
    with pytest.raises(TreeError, match="must list the commands"):
        load_tree_text(SHELL_TREE.format(py=sys.executable).replace('commands = ["printf", "%s", "false", "sleep"]' % sys.executable, ""))


# ---------------------------------------------------------------- serial


SERIAL_TREE = """
name = "modem"
description = "asks a modem whether it is there"
devices = ["/dev/pts/*", "/dev/ttyUSB*"]
start = "at"

[inputs.device]
type = "str"

[nodes.at]
kind = "observe"
serial = { device = "{{ inputs.device }}", baud = 9600, write = "AT\\r\\n", read_until = "\\n" }
into = "s"
timeout = 2
retries = 0
extract = [{ into = "answer", regex = "(OK|ERROR)" }]
next = "ok"

[nodes.ok]
kind = "decide"
on = "answer"
branches = [{ when = { eq = "OK" }, then = "there" }, { else = "odd" }]

[nodes.there]
kind = "done"
say = "{{ inputs.device }} answers ({{ state.s.bytes }} B, sent {{ state.s.sent }})"

[nodes.odd]
kind = "fail"
reason = "odd-answer"
say = "{{ state.s.body | strip }}"
"""


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no pty on this platform")
def test_serial_exchange_over_a_pty(tmp_path):
    reg = registry(tmp_path, modem=SERIAL_TREE)
    master, slave = os.openpty()
    device = os.ttyname(slave)
    got: list[bytes] = []

    def modem():
        buf = b""
        while b"\n" not in buf:
            buf += os.read(master, 64)
        got.append(buf)
        os.write(master, b"OK\r\n")

    t = threading.Thread(target=modem, daemon=True)
    t.start()
    try:
        out = Runner(reg).run_tree("modem", {"device": device})
        t.join(2)
        assert out.status == "done", out.say
        assert out.say == f"{device} answers (4 B, sent 4)" and got == [b"AT\r\n"]
        assert out.trace.steps[0].summary == f"serial {device} -> 4 B; answer=OK"
        # Nothing answers: a transport error, not a hang.
        t0 = time.time()
        out = Runner(reg).run_tree("modem", {"device": device})
        assert out.status == "error" and out.reason == "transport" and "no answer within" in out.say and time.time() - t0 < 5
    finally:
        os.close(master)
        os.close(slave)
    assert Runner(reg).run_tree("modem", {"device": "/dev/ttyACM0"}).reason == "device-not-allowed"
    out = Runner(reg).run_tree("modem", {"device": "/dev/ttyUSB99"})
    assert out.status == "error" and out.reason == "transport"
    with pytest.raises(TreeError, match="not in this tree's devices"):
        load_tree_text(SERIAL_TREE.replace('device = "{{ inputs.device }}"', 'device = "/dev/ttyACM0"'))


# ---------------------------------------------------------------- queue (MQTT)


class Broker:
    """Just enough of an MQTT 3.1.1 broker: one client at a time, a canned
    message for whoever subscribes, a record of what is published."""

    def __init__(self, message: bytes | None = b'{"temperature": 23.5}', refuse: int = 0):
        self.message = message
        self.refuse = refuse
        self.published: list[tuple[str, bytes]] = []
        self.subscribed: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.url = f"mqtt://127.0.0.1:{self.port}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket):
        deadline = time.monotonic() + 10
        try:
            while True:
                pkt = _mqtt_read(conn, deadline)
                if pkt is None:
                    return
                ptype, flags, body = pkt
                if ptype == 1:
                    conn.sendall(_mqtt_packet(0x20, bytes([0, self.refuse])))
                    if self.refuse:
                        return
                elif ptype == 8:
                    pid = body[:2]
                    tlen = struct.unpack(">H", body[2:4])[0]
                    topic = body[4:4 + tlen].decode()
                    self.subscribed.append(topic)
                    conn.sendall(_mqtt_packet(0x90, pid + b"\x00"))
                    if self.message is not None:
                        conn.sendall(_mqtt_packet(0x30, _mqtt_str(topic.replace("+", "kitchen")) + self.message))
                elif ptype == 3:
                    tlen = struct.unpack(">H", body[:2])[0]
                    self.published.append((body[2:2 + tlen].decode(), body[2 + tlen:]))
                elif ptype == 14:
                    return
        finally:
            conn.close()

    def close(self):
        self.sock.close()


QUEUE_TREE = """
name = "sensor"
description = "the last temperature a room reported"
hosts = ["127.0.0.1"]
topics = ["home/+/temperature", "home/+/light"]
start = "wait"

[inputs.room]
type = "str"
default = "kitchen"

[inputs.broker]
type = "str"

[nodes.wait]
kind = "observe"
queue = { url = "{{ inputs.broker }}", topic = "home/{{ inputs.room }}/temperature" }
into = "q"
timeout = 2
retries = 0
next = "got"

[nodes.got]
kind = "decide"
on = "q.received"
branches = [{ when = { eq = true }, then = "warm" }, { else = "quiet" }]

[nodes.warm]
kind = "decide"
on = "q.json.temperature"
branches = [{ when = { gt = 25 }, then = "cool-it" }, { else = "fine" }]

[nodes.cool-it]
kind = "act"
queue = { url = "{{ inputs.broker }}", topic = "home/{{ inputs.room }}/light", publish = { on = false } }
into = "sent"
next = "fine"

[nodes.fine]
kind = "done"
say = "{{ inputs.room }}: {{ state.q.json.temperature }} from {{ state.q.message.topic }}"

[nodes.quiet]
kind = "fail"
reason = "no-reading"
say = "nothing on home/{{ inputs.room }}/temperature within 2 s"
"""


def test_queue_subscribe_wait_and_publish(tmp_path):
    reg = registry(tmp_path, sensor=QUEUE_TREE)
    b = Broker()
    try:
        out = Runner(reg).run_tree("sensor", {"broker": b.url})
        assert out.status == "done", out.say
        assert out.say == "kitchen: 23.5 from home/kitchen/temperature" and b.subscribed == ["home/kitchen/temperature"] and b.published == []
        assert out.trace.steps[0].summary == f"queue home/kitchen/temperature at {b.url} -> 1 message"
        hot = Broker(message=b'{"temperature": 31}')
        out = Runner(reg).run_tree("sensor", {"broker": hot.url})
        for _ in range(50):  # the broker reads the publish on its own thread
            if hot.published:
                break
            time.sleep(0.02)
        assert out.status == "done" and hot.published == [("home/kitchen/light", b'{"on": false}')]
        assert out.trace.steps[3].summary == f"queue home/kitchen/light at {hot.url} -> published 13 B"
        hot.close()
        quiet = Broker(message=None)
        t0 = time.time()
        out = Runner(reg).run_tree("sensor", {"broker": quiet.url})
        assert out.status == "failed" and out.reason == "no-reading" and time.time() - t0 < 5
        quiet.close()
        refusing = Broker(refuse=5)
        out = Runner(reg).run_tree("sensor", {"broker": refusing.url})
        assert out.status == "error" and "refused (code 5)" in out.say
        refusing.close()
        # Allow-lists: the broker's host in hosts, the topic in topics.
        assert Runner(reg).run_tree("sensor", {"broker": "mqtt://broker.example"}).reason == "host-not-allowed"
        assert Runner(reg).run_tree("sensor", {"broker": b.url, "room": "a/b"}).reason == "topic-not-allowed"
        assert Runner(reg).run_tree("sensor", {"broker": "http://127.0.0.1/"}).reason == "host-not-allowed"
    finally:
        b.close()
    with pytest.raises(TreeError, match="make this an act"):
        load_tree_text(QUEUE_TREE.replace('topic = "home/{{ inputs.room }}/temperature" }', 'topic = "home/{{ inputs.room }}/temperature", publish = "x" }'))
    with pytest.raises(TreeError, match="must list the topics"):
        load_tree_text(QUEUE_TREE.replace('topics = ["home/+/temperature", "home/+/light"]', ""))
    with pytest.raises(TreeError, match="not under this tree's topics"):
        load_tree_text(QUEUE_TREE.replace('topic = "home/{{ inputs.room }}/light"', 'topic = "work/light"'))


# ---------------------------------------------------------------- egress, fixtures, the command


def test_egress_public_keeps_the_box_closed(tmp_path):
    hosted = LocalAdapters(egress="public")
    for source, spec in (("file", {"path": "/etc/hostname"}), ("shell", {"command": "uptime"}), ("serial", {"device": "/dev/ttyUSB0"})):
        with pytest.raises(EgressRefused, match="its own box"):
            hosted.perform(source, spec, 1.0)
    with pytest.raises(EgressRefused):
        hosted.perform("queue", {"url": "mqtt://127.0.0.1", "topic": "t"}, 1.0)
    with pytest.raises(EgressRefused):
        hosted.perform("browser", {"url": "http://10.0.0.1/"}, 1.0)
    # Through a tree: the refusal is an error the trace shows, and an observe does not retry it.
    reg = registry(tmp_path, say=SHELL_TREE.format(py=sys.executable))
    out = Runner(reg, adapters=hosted).run_tree("say")
    assert out.status == "error" and out.reason == "transport" and "its own box" in out.say


def test_fixtures_record_and_replay_every_source(tmp_path, capsys):
    reg = registry(tmp_path, say=SHELL_TREE.format(py=sys.executable), notes=FILE_TREE.format(root=str(tmp_path)))
    (tmp_path / "n.txt").write_text("a\nb\n")
    rec = RecordingTransport(None, LocalAdapters())  # type: ignore[arg-type]
    runner = Runner(reg, rec)
    assert runner.run_tree("say", {"text": "x"}).status == "done"
    assert runner.run_tree("notes", {"path": str(tmp_path / "n.txt")}).status == "done"
    assert set(rec.recorded) == {"shell printf %s|%s x $HOME;ls", f"file {tmp_path / 'n.txt'}", f"file append {tmp_path / 'n.txt'}"}
    rec.save(tmp_path / "fx.json")
    replay = RecordedTransport.from_file(tmp_path / "fx.json")
    out = Runner(reg, replay).run_tree("say", {"text": "x"})
    assert out.status == "done" and out.say == "x|$HOME;ls" and replay.calls == [("shell", "shell printf %s|%s x $HOME;ls")]
    out = Runner(reg, replay).run_tree("say", {"text": "unrecorded"})
    assert out.status == "error" and "no recorded result for shell printf %s|%s unrecorded" in out.say
    # The command records and replays the same way.
    code = main(["run", "--tree", "say", "--input", "text=cli", "--trees", str(tmp_path / "trees"), "--record", str(tmp_path / "cli.json"), "--json", "--no-trace"])
    assert code == 0 and json.loads(capsys.readouterr().out)["say"] == "cli|$HOME;ls"
    assert "shell printf %s|%s cli $HOME;ls" in json.loads((tmp_path / "cli.json").read_text())
    code = main(["run", "--tree", "say", "--input", "text=cli", "--trees", str(tmp_path / "trees"), "--replay", str(tmp_path / "cli.json"), "--json", "--no-trace"])
    assert code == 0 and json.loads(capsys.readouterr().out)["say"] == "cli|$HOME;ls"
    # A service with --egress public refuses the box; a recorded transport answers for the sources too.
    from pyrtan.service import Service

    svc = Service(reg, egress="public")
    assert svc.run({"tree": "say", "inputs": {}})["reason"] == "transport"
    svc = Service(reg, transport=replay)
    assert svc.run({"tree": "say", "inputs": {"text": "x"}})["status"] == "done"


# ---------------------------------------------------------------- browser


PAGE = b"""<!doctype html><html><head><title>App</title></head><body>
<div id="root">loading</div><a id="away" href="%s">away</a>
<script>setTimeout(() => { document.getElementById("root").innerHTML = "<h1>Rendered</h1><button id=more>more</button>";
document.getElementById("more").onclick = () => { document.getElementById("root").insertAdjacentHTML("beforeend", "<p class=extra>extra</p>"); }; }, 50);</script>
</body></html>"""

BROWSER_TREE = """
name = "app"
description = "a page that needs a script to render"
hosts = ["127.0.0.1"]
start = "open"

[inputs.url]
type = "str"

[nodes.open]
kind = "observe"
browser = { url = "{{ inputs.url }}", wait_for = "h1", actions = [{ click = "#more" }, { wait_for = ".extra" }] }
into = "page"
timeout = 15
retries = 0
extract = [{ into = "heading", css = "h1" }, { into = "extra", css = "p.extra" }]
next = "say"

[nodes.say]
kind = "done"
say = "{{ state.page.title }}: {{ state.heading }} / {{ state.extra }} ({{ state.page.status }})"
"""


@pytest.fixture
def pages():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE % f"http://localhost:{self.server.server_port}/elsewhere".encode())

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_browser_renders_acts_and_stays_on_its_hosts(tmp_path, pages):
    pytest.importorskip("playwright")
    adapters = LocalAdapters()
    try:
        adapters.perform("browser", {"url": pages + "/"}, 15.0)
    except TransportError as e:
        pytest.skip(str(e))
    reg = registry(tmp_path, app=BROWSER_TREE)
    try:
        out = Runner(reg, adapters=adapters).run_tree("app", {"url": pages + "/app"})
        assert out.status == "done", out.say
        assert out.say == "App: Rendered / extra (200)"
        assert out.trace.steps[0].summary == f"browser {pages}/app -> 200; heading=Rendered, extra=extra"
        # A click that would take the page to another host is refused; so is a URL outside hosts, before any browser starts.
        reg2 = registry(tmp_path, app=BROWSER_TREE.replace("{ click = \"#more\" }", "{ click = \"#away\" }"))
        out = Runner(reg2, adapters=adapters).run_tree("app", {"url": pages + "/app"})
        assert out.status == "error" and out.reason == "transport" and "outside this tree's hosts" in out.say
        out = Runner(reg, adapters=adapters).run_tree("app", {"url": "http://localhost:1/"})
        assert out.reason == "host-not-allowed"
    finally:
        adapters.close()
    with pytest.raises(TreeError, match="exactly one of click"):
        load_tree_text(BROWSER_TREE.replace('{ click = "#more" }', '{ tap = "#more" }'))
    with pytest.raises(TreeError, match="not in this tree's hosts"):
        load_tree_text(BROWSER_TREE.replace('url = "{{ inputs.url }}"', 'url = "https://example.com/"'))
