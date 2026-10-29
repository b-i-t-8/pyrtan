"""A line editor for the terminal chat: keys as they are pressed, not lines.

``pyrtan chat`` reads a whole line at a time when its input is a pipe, but
at a terminal it reads key by key, so the stranger sees "Stranger is
typing…" while you type, exactly as with the console. Lines that arrive
while you are mid-word are printed above what you are typing, which is
drawn again under them.

:class:`LineEditor` is the editing alone (bytes in, lines out, nothing about
terminals), so it can be tested without one; :func:`keys` is the terminal
part: cbreak mode on the way in, the old settings restored on the way out,
whatever happens. Standard library only; where ``termios`` is missing
(Windows) :func:`interactive` says no and the chat reads lines as before.

What it refuses: being a readline. No cursor movement, no completion:
Backspace, Ctrl-U (clear the line), Ctrl-W (a word), Enter, Ctrl-D on an
empty line (leave), Ctrl-C (leave), and Up and Down for the lines sent
before. That history is kept in memory for this one chat and is gone when
it ends; nothing is written to disk. Other arrow keys and escape sequences
are swallowed rather than printed.
"""

from __future__ import annotations

import codecs
import os
import sys
from contextlib import contextmanager
from typing import Callable, Iterator

try:
    import termios
    import tty
except ImportError:  # Windows: no termios; the chat reads lines
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

EOF = object()   # what feed() yields for Ctrl-D on an empty line
HISTORY = 100    # lines Up can go back through


def interactive(stream=None) -> bool:
    """Whether keys can be read one by one from ``stream`` (default stdin)."""
    stream = sys.stdin if stream is None else stream
    try:
        return termios is not None and stream.isatty()
    except (AttributeError, ValueError):
        return False


class LineEditor:
    """The line being typed. ``write`` draws on the terminal; ``changed`` is
    told the line after each key that changed it."""

    def __init__(self, write: Callable[[str], None], changed: Callable[[str], None] = lambda _: None, prompt: str = "> ") -> None:
        self.write = write
        self.changed = changed
        self.prompt = prompt
        self.buffer = ""
        self.history: list[str] = []   # lines sent, oldest first
        self._at = 0         # where Up and Down are in the history; len(history) is the line being typed
        self._draft = ""     # the line being typed, kept while Up looks back
        self._escape = ""    # an escape sequence being read

    def feed(self, text: str) -> list[object]:
        """Keys in; the lines they finished out (and :data:`EOF` for Ctrl-D on an empty line)."""
        done: list[object] = []
        before = self.buffer
        for ch in text:
            if self._escape:
                # ESC [ … final byte, ESC O and one more, or ESC and one more:
                # read whole; Up and Down move through the history, the rest is swallowed.
                self._escape += ch
                seq = self._escape
                if (len(seq) == 2 and ch not in "[O") or (seq[1] == "O" and len(seq) == 3) or (seq[1] == "[" and len(seq) > 2 and "@" <= ch <= "~"):
                    self._escape = ""
                    if seq in ("\x1b[A", "\x1bOA"):
                        self.recall(-1)
                    elif seq in ("\x1b[B", "\x1bOB"):
                        self.recall(+1)
                continue
            if ch == "\x1b":
                self._escape = ch
            elif ch in ("\r", "\n"):
                line, self.buffer = self.buffer, ""
                self.write("\r\x1b[K")
                done.append(line)
                if line.strip() and (not self.history or self.history[-1] != line):
                    self.history = (self.history + [line])[-HISTORY:]
                self._at, self._draft = len(self.history), ""
                self.write(self.prompt)
            elif ch in ("\x7f", "\x08"):
                if self.buffer:
                    self.buffer = self.buffer[:-1]
                    self.write("\b \b")
            elif ch == "\x15":  # Ctrl-U
                self.buffer = ""
                self.redraw()
            elif ch == "\x17":  # Ctrl-W
                self.buffer = self.buffer.rstrip()
                self.buffer = self.buffer[: self.buffer.rfind(" ") + 1] if " " in self.buffer else ""
                self.redraw()
            elif ch == "\x04":  # Ctrl-D
                if not self.buffer:
                    done.append(EOF)
                    break
            elif ch >= " ":
                self.buffer += ch
                self.write(ch)
        if self.buffer != before:
            self.changed(self.buffer)
        return done

    def recall(self, step: int) -> None:
        """Up (-1) or Down (+1) through the lines sent; Down past the newest
        gives back what was being typed."""
        at = min(max(self._at + step, 0), len(self.history))
        if at == self._at:
            return
        if self._at == len(self.history):
            self._draft = self.buffer
        self._at = at
        self.buffer = self.history[at] if at < len(self.history) else self._draft
        self.redraw()

    def redraw(self) -> None:
        self.write("\r\x1b[K" + self.prompt + self.buffer)

    def above(self, text: str) -> None:
        """Print a line above the one being typed, which is drawn again under it."""
        self.write("\r\x1b[K" + text + "\n")
        self.redraw()


@contextmanager
def cbreak(fd: int) -> Iterator[None]:
    """Keys one by one and no echo (Ctrl-C still interrupts); the terminal as it was, after."""
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def keys(fd: int) -> Iterator[str]:
    """What is typed at ``fd``, as text, as it is typed; ends at end of input."""
    decode = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        data = os.read(fd, 64)
        if not data:
            return
        text = decode.decode(data)
        if text:
            yield text
