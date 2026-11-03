"""At most once per key: an ``act`` that must not be done twice for the same thing.

A tree that posts an order, sends a message or opens a door can be run
again for the same thing: a watch fires twice, a person asks twice, a hub
retries. Saying ``once`` on the act names what makes it the same::

    [nodes.post]
    kind = "act"
    http = { method = "POST", url = "https://shop.example/orders", body = { id = "{{ inputs.order }}" } }
    once = "{{ inputs.order }}"      # at most once per order
    once_for = "30 days"             # optional: how long a key is kept (for good, without it)
    into = "posted"                  # ... and state.posted.once = { key, first, at }

The first run that reaches the act with a key does it. A later one with
the same key does not do it again: the step says so, and ``into`` gets
what the first run got, with ``once.first`` false, so the rest of the tree
runs as it did. Only a good answer (a 2xx page, a program that exited 0…)
keeps the key: an act that failed, or a request that never got through,
may be tried again. Two runs at once with one key are taken in turn.

Keys are the box's, kept where its memory is (``--memory FILE``, so they
outlive a restart; in the process without one), under ``pyrtan-once/``,
where ``pyrtan memory`` lists them and ``--forget`` lets one be done
again. A key is the tree's and the node's: two acts may use the same
words. Only one service is assumed to use a memory file.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from .memory import MAX_VALUE, Memory, MemoryRefused

PREFIX = "pyrtan-once"
WAIT = 60.0  # seconds a run waits for another run's act with the same key


class Once:
    """The keys acts were done for, in a :class:`~pyrtan.memory.Memory`."""

    def __init__(self, memory: Memory | None = None) -> None:
        self.memory = memory if memory is not None else Memory(None)
        self._lock = threading.Condition()
        self._doing: set[str] = set()

    @staticmethod
    def slot(tree: str, node: str, key: str) -> str:
        """Where a key is kept: memory keys are a fixed alphabet, so the words are hashed."""
        return f"{PREFIX}/{tree}/{node}/{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"

    def done(self, slot: str) -> dict[str, Any] | None:
        """What the act was done with for this key, if it was: ``{key, at, result}``."""
        got = self.memory.perform({"key": slot})
        return got["value"] if got["exists"] and isinstance(got["value"], dict) else None

    def claim(self, slot: str) -> dict[str, Any] | None:
        """Take the key for this run, or say it is done. A run doing it
        now is waited for (up to :data:`WAIT`), then asked again."""
        with self._lock:
            deadline = time.monotonic() + WAIT
            while slot in self._doing and time.monotonic() < deadline:
                self._lock.wait(deadline - time.monotonic())
            prior = self.done(slot)
            if prior is None:
                self._doing.add(slot)
            return prior

    def keep(self, slot: str, key: str, at: str, result: Any, ttl: Any = None) -> None:
        """The act was done well for this key: keep it, with what it got."""
        value = {"key": key, "at": at, "result": result}
        if len(json.dumps(value, ensure_ascii=False, default=str)) > MAX_VALUE - 256:
            slim = {k: result[k] for k in ("status", "ok", "exit", "exists") if isinstance(result, dict) and k in result}
            value = {"key": key, "at": at, "result": slim}
        try:
            self.memory.perform({"key": slot, "write": value, **({"ttl": ttl} if ttl is not None else {})})
        except MemoryRefused:
            pass  # a full memory keeps no key: the act may then be done again, which is what it was before once
        finally:
            self.release(slot)

    def release(self, slot: str) -> None:
        with self._lock:
            self._doing.discard(slot)
            self._lock.notify_all()
