"""The terminal chat's line editor, and `pyrtan chat` at a real (pseudo-)terminal:
the stranger sees you typing, and lines that arrive are drawn above yours."""

import os
import subprocess
import sys
import time

import pytest

from pyrtan.term import EOF, LineEditor


def editor():
    drawn, changes = [], []
    return LineEditor(drawn.append, changes.append), drawn, changes


def test_keys_make_lines():
    ed, drawn, changes = editor()
    assert ed.feed("hel") == [] and ed.buffer == "hel" and changes == ["hel"]
    assert ed.feed("\x7f") == [] and ed.buffer == "he" and drawn[-1] == "\b \b"
    assert ed.feed("\x1b[A\x1b[1;5C") == [] and ed.buffer == "he"  # arrow keys are swallowed, not typed
    assert ed.feed("llo\r") == ["hello"] and ed.buffer == "" and changes[-1] == ""
    assert ed.feed("one two\x17") == [] and ed.buffer == "one "  # Ctrl-W: a word
    assert ed.feed("\x15") == [] and ed.buffer == ""                # Ctrl-U: the line
    assert ed.feed("a\x04") == [] and ed.buffer == "a"              # Ctrl-D with something typed: nothing
    assert ed.feed("\x15\x04rest") == [EOF]                         # on an empty line: leave
    assert ed.feed("x\ry\n") == ["x", "y"]                          # a paste of two lines


def test_up_and_down_go_through_the_lines_sent():
    ed, drawn, changes = editor()
    up, down = "\x1b[A", "\x1b[B"
    assert ed.feed(up + down) == [] and ed.buffer == ""         # nothing sent yet: nothing to go back to
    ed.feed("one\rtwo\rtwo\r\r  \r")                           # a repeat and blank lines are not kept twice, or at all
    assert ed.history == ["one", "two"]
    ed.feed("half")
    assert ed.feed(up) == [] and ed.buffer == "two" and drawn[-1] == "\r\x1b[K> two" and changes[-1] == "two"
    assert ed.feed(up + up + up) == [] and ed.buffer == "one"   # stops at the oldest
    ed.feed(down)
    assert ed.buffer == "two"
    ed.feed(down)
    assert ed.buffer == "half"                                  # past the newest: what was being typed
    ed.feed("\x1bOA")                                           # the same key in application mode
    assert ed.buffer == "two"
    ed.feed("!")                                                # a recalled line is edited, then sent
    assert ed.feed("\r") == ["two!"] and ed.history == ["one", "two", "two!"]
    assert ed.feed(down) == [] and ed.buffer == ""              # the line sent left nothing to come back to
    ed.feed("\x1bOP\x1b[1;5C")                                  # F1 and Ctrl-Right: swallowed, not typed
    assert ed.buffer == ""
    for n in range(150):
        ed.feed(f"{n}\r")
    assert len(ed.history) == 100 and ed.history[0] == "50"


def test_a_line_from_outside_is_drawn_above_the_one_being_typed():
    ed, drawn, _ = editor()
    ed.feed("half a thou")
    drawn.clear()
    ed.above("Stranger: hi")
    assert "".join(drawn) == "\r\x1b[KStranger: hi\n\r\x1b[K> half a thou"


@pytest.mark.skipif(sys.platform == "win32", reason="no pty")
def test_chat_at_a_terminal_says_you_are_typing():
    import pty

    from pyrtan.hub import Hub
    from pyrtan.service import Server

    hub = Hub(name="h", chat=True)
    srv = Server(hub.route, quiet=True).start()
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", "from pyrtan.cli import main; raise SystemExit(main())", "chat", "--people", "--at", srv.url],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    seen = b""

    def read_until(text, timeout=10.0):
        nonlocal seen
        deadline = time.time() + timeout
        while text.encode() not in seen:
            assert time.time() < deadline, seen.decode(errors="replace")
            import select

            if select.select([master], [], [], 0.2)[0]:
                seen += os.read(master, 4096)

    try:
        read_until("Looking for someone")
        _, there = hub.route("POST", "/chat/start", {"want": "people"})
        assert there["state"] == "talking"
        read_until("random stranger")
        # Keys, not a line yet: the other seat reads "typing".
        os.write(master, b"hel")
        _, view = hub.route("POST", "/chat/poll", {"seat": there["seat"], "after": there["after"], "wait": 5, "partner_typing": False})
        assert view["partner_typing"] is True and view["events"] == []
        # A line arrives while a word is half typed: it is drawn above, and the word stays.
        hub.route("POST", "/chat/say", {"seat": there["seat"], "text": "hi there"})
        read_until("Stranger: hi there")
        read_until("> hel")
        # Enter: the line goes, and the typing with it.
        os.write(master, b"lo\r")
        deadline = time.time() + 5
        while True:
            _, view = hub.route("POST", "/chat/poll", {"seat": there["seat"], "after": view["after"], "wait": 1})
            if any(e["kind"] == "stranger" for e in view["events"]):
                break
            assert time.time() < deadline
        assert [e["text"] for e in view["events"] if e["kind"] == "stranger"] == ["hello"] and view["partner_typing"] is False
        # Up brings the line back, Enter sends it again.
        os.write(master, b"\x1b[A\r")
        deadline = time.time() + 5
        while True:
            _, view = hub.route("POST", "/chat/poll", {"seat": there["seat"], "after": view["after"], "wait": 1})
            if any(e["kind"] == "stranger" for e in view["events"]):
                break
            assert time.time() < deadline
        assert [e["text"] for e in view["events"] if e["kind"] == "stranger"] == ["hello"]
        # Ctrl-D on an empty line leaves, and the terminal is given back.
        os.write(master, b"\x04")
        assert proc.wait(timeout=10) == 0
        _, view = hub.route("POST", "/chat/poll", {"seat": there["seat"], "after": view["after"]})
        assert view["state"] == "ended"
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
        srv.stop()
        hub.stop()
