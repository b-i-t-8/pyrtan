"""Executing a tree.

The :class:`Runner` walks a tree from ``start`` to a terminal node, keeping a
state table, a trace of every step, and the tree's host allow-list in front
of every request. It returns an :class:`Outcome`.

An outcome's ``status`` is one of:

``done``
    A ``done`` node was reached; ``say`` and ``output`` are set.
``failed``
    A ``fail`` node was reached; ``reason`` names which, and ``say`` explains.
``asked``
    An ``ask`` node paused the run; ``question`` holds the prompt and
    ``snapshot`` what :meth:`Runner.resume` needs to continue.
``error``
    The run could not proceed: a transport failure, an unhandled observation
    (a ``decide`` with no matching branch, recorded as a gap), a host outside
    the allow-list, a missing template value, or a limit reached.

Nothing here calls a model. The runner is the whole of Pyrtan's "thinking";
the trace is the whole of its explanation.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import conditions
from .extract import extract
from .registry import Ambiguous, NoMatch, Registry
from .template import MISSING, TemplateError, render, resolve
from .trace import Step, Trace
from .transport import Transport, TransportError, UrllibTransport
from .tree import Node, Tree, TreeError

MAX_STEPS = 500
MAX_DEPTH = 8
DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 1


class Resolver(Protocol):
    """Where ``delegate`` and ``each ... tree`` go when a tree is not local."""

    def run_tree(self, name: str, inputs: dict[str, Any], depth: int) -> "Outcome": ...

    def run_intent(self, text: str, depth: int) -> "Outcome": ...


@dataclass
class Outcome:
    status: str
    tree: str
    say: str | None = None
    output: Any = None
    reason: str | None = None
    question: str | None = None
    snapshot: dict[str, Any] | None = None
    trace: Trace | None = None
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def to_dict(self, *, with_trace: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "tree": self.tree, "inputs": self.inputs}
        if self.say is not None:
            d["say"] = self.say
        if self.output is not None:
            d["output"] = self.output
        if self.reason is not None:
            d["reason"] = self.reason
        if self.question is not None:
            d["question"] = self.question
        if self.snapshot is not None:
            d["snapshot"] = self.snapshot
        if with_trace and self.trace is not None:
            d["trace"] = self.trace.to_dict()
        return d


class _Halt(Exception):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


@dataclass
class Runner:
    registry: Registry
    transport: Transport = field(default_factory=UrllibTransport)
    resolver: Resolver | None = None
    env: dict[str, Any] = field(default_factory=dict)
    max_steps: int = MAX_STEPS
    max_depth: int = MAX_DEPTH
    emitted: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------ entry points

    def run_intent(self, text: str, depth: int = 0) -> Outcome:
        try:
            match = self.registry.match(text)
        except NoMatch:
            if self.resolver is not None:
                return self.resolver.run_intent(text, depth)
            return Outcome("error", "", reason="no-match", say=f"Nothing here answers {text!r}.")
        except Ambiguous as e:
            return Outcome("error", "", reason="ambiguous", say=str(e), output={"candidates": [m.tree for m in e.matches]})
        return self.run_tree(match.tree, match.inputs, depth)

    def run_tree(self, name: str, inputs: dict[str, Any] | None = None, depth: int = 0) -> Outcome:
        tree = self.registry.get(name)
        if tree is None:
            if self.resolver is not None:
                return self.resolver.run_tree(name, inputs or {}, depth)
            return Outcome("error", name, reason="no-such-tree", say=f"No tree named {name!r}.")
        return self.run(tree, inputs or {}, depth=depth)

    def run(self, tree: Tree, inputs: dict[str, Any] | None = None, *, depth: int = 0, start: str | None = None, state: dict[str, Any] | None = None, scope_extra: dict[str, Any] | None = None, trace: Trace | None = None) -> Outcome:
        trace = trace or Trace(tree.name)
        if depth > self.max_depth:
            return Outcome("error", tree.name, reason="too-deep", say=f"Delegation deeper than {self.max_depth} levels.", trace=trace)
        try:
            bound = tree.bind_inputs(inputs or {})
        except TreeError as e:
            return Outcome("error", tree.name, reason="bad-input", say=str(e), trace=trace)
        state = dict(state) if state is not None else {}
        scope: dict[str, Any] = {"inputs": bound, "state": state, "tree": {"name": tree.name, "version": tree.version}, "now": _now(), "env": self.env}
        if scope_extra:
            scope.update(scope_extra)
        try:
            return self._walk(tree, start or tree.start, scope, trace, depth)
        except _Halt as h:
            h.outcome.inputs = bound
            h.outcome.trace = h.outcome.trace or trace
            return h.outcome

    def resume(self, snapshot: dict[str, Any], answer: Any) -> Outcome:
        """Continue a run paused by ``ask``. The snapshot is what the asked
        outcome carried; the answer goes where the ask node said."""
        tree = self.registry.get(snapshot["tree"])
        if tree is None:
            return Outcome("error", snapshot.get("tree", ""), reason="no-such-tree")
        node = tree.node(snapshot["node"])
        into: str = node.get("into")
        inputs = dict(snapshot["inputs"])
        state = dict(snapshot["state"])
        if into.startswith("inputs."):
            key = into[len("inputs."):]
            try:
                inputs[key] = tree.inputs[key].coerce(answer)
            except TreeError as e:
                return Outcome("error", tree.name, reason="bad-input", say=str(e))
        else:
            _put(state, into[len("state."):] if into.startswith("state.") else into, answer)
        trace = Trace(tree.name)
        trace.steps = [Step(**s) for s in snapshot.get("trace", [])]
        trace.add(Step(node.id, "ask", f"answered: {_brief(answer)}", next=node.next))
        return self.run(tree, inputs, start=node.next, state=state, trace=trace)

    # ------------------------------------------------------------ the walk

    def _walk(self, tree: Tree, start: str, scope: dict[str, Any], trace: Trace, depth: int) -> Outcome:
        state: dict[str, Any] = scope["state"]
        node_id = start
        steps = 0
        while True:
            steps += 1
            if steps > self.max_steps:
                raise _Halt(Outcome("error", tree.name, reason="too-many-steps", say=f"More than {self.max_steps} steps.", trace=trace))
            node = tree.node(node_id)
            started = time.monotonic()
            handler = getattr(self, f"_do_{node.kind}")
            try:
                result = handler(tree, node, scope, trace, depth)
            except TemplateError as e:
                trace.add(Step(node.id, node.kind, f"template: {e}", depth=depth))
                raise _Halt(Outcome("error", tree.name, reason="template", say=f"{node.id}: {e}", trace=trace))
            if isinstance(result, Outcome):
                result.trace = trace
                raise _Halt(result)
            step, next_id = result
            step.elapsed_ms = int((time.monotonic() - started) * 1000)
            step.depth = depth
            step.next = next_id
            trace.add(step)
            if next_id is None:
                raise _Halt(Outcome("error", tree.name, reason="dead-end", say=f"{node.id} has nowhere to go.", trace=trace))
            node_id = next_id

    # ------------------------------------------------------------ node kinds

    def _do_observe(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        return self._observe_or_act(tree, node, scope, trace, retry=True)

    def _do_act(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        emit = node.get("emit")
        if emit is not None:
            event = {"event": render(emit["event"], scope), "data": render(emit.get("data", {}), scope), "tree": tree.name}
            self.emitted.append(event)
            _put(scope["state"], node.get("into", "last"), event)
            return Step(node.id, "act", f"emitted {event['event']}", observed=event["data"]), node.next
        return self._observe_or_act(tree, node, scope, trace, retry=False)

    def _observe_or_act(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, *, retry: bool):
        state = scope["state"]
        into = node.get("into", "last")
        http = node.get("http")
        if http is not None:
            req = render(http, scope)
            method = str(req.get("method", "GET")).upper()
            url = str(req["url"])
            if not tree.host_allowed(url):
                return Outcome("error", tree.name, reason="host-not-allowed", say=f"{node.id}: {url} is outside this tree's hosts {tree.hosts}.")
            headers = {str(k): str(v) for k, v in (req.get("headers") or {}).items()}
            body = req.get("body")
            if body is not None and not isinstance(body, str):
                import json as _json

                body = _json.dumps(body)
                headers.setdefault("Content-Type", "application/json")
            timeout = float(node.get("timeout", DEFAULT_TIMEOUT))
            attempts = 1 + int(node.get("retries", DEFAULT_RETRIES)) if retry else 1
            last_error: str | None = None
            for attempt in range(attempts):
                try:
                    resp = self.transport.request(method, url, headers, body, timeout)
                    break
                except TransportError as e:
                    last_error = str(e)
                    if attempt + 1 < attempts:
                        time.sleep(min(0.2 * (attempt + 1), 1.0))
            else:
                trace.add(Step(node.id, node.kind, f"{method} {url} failed: {last_error}"))
                return Outcome("error", tree.name, reason="transport", say=f"{node.id}: {last_error}")
            observed = resp.to_state()
            _put(state, into, observed)
            source = observed
            summary = f"{method} {url} -> {resp.status}"
        else:
            frm = node.get("from")
            source_val = resolve(_state_path(frm), scope)
            if source_val is MISSING:
                source = {}
            elif isinstance(source_val, dict):
                source = source_val
            else:
                source = {"value": source_val, "body": source_val if isinstance(source_val, str) else None}
            summary = f"from {frm}"
        extracted: dict[str, Any] = {}
        cache: dict[int, Any] = {}
        for entry in node.get("extract", []) or []:
            value = extract(entry, source, cache)
            if value is MISSING:
                extracted[entry["into"]] = None
            else:
                _put(state, entry["into"], value)
                extracted[entry["into"]] = value
        if extracted:
            summary += "; " + ", ".join(f"{k}={_brief(v)}" for k, v in extracted.items())
        return Step(node.id, node.kind, summary, observed=extracted or None), node.next

    def _do_decide(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        on = _state_path(node.get("on"))
        value = resolve(on, scope)
        for i, br in enumerate(node.get("branches")):
            if "else" in br:
                return Step(node.id, "decide", f"{on} = {_brief(value)}", observed=_plain(value), chosen="else"), br["else"]
            if conditions.holds(br["when"], value):
                return Step(node.id, "decide", f"{on} = {_brief(value)}", observed=_plain(value), chosen=_cond_text(br["when"])), br["then"]
        trace.gap(node.id, on, _plain(value))
        trace.add(Step(node.id, "decide", f"{on} = {_brief(value)}: no branch", observed=_plain(value), depth=depth))
        return Outcome("error", tree.name, reason="gap", say=f"{node.id}: no branch for {on} = {_brief(value)}.", output={"gap": trace.gaps[-1]})

    def _do_set(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        state = scope["state"]
        values = {}
        for key, tmpl in node.get("set").items():
            value = render(tmpl, scope)
            _put(state, key, value)
            values[key] = value
        return Step(node.id, "set", ", ".join(f"{k}={_brief(v)}" for k, v in values.items()), observed=values), node.next

    def _do_each(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        state = scope["state"]
        items = resolve(_state_path(node.get("over")), scope)
        if items is MISSING or items is None:
            items = []
        if not isinstance(items, list):
            return Outcome("error", tree.name, reason="not-a-list", say=f"{node.id}: {node.get('over')} is not a list.")
        limit = node.get("limit")
        if limit is not None:
            limit = int(render(limit, scope))
            items = items[:limit]
        results: list[Any] = []
        failures = 0
        for index, item in enumerate(items):
            with_inputs = render(node.get("with", {}), {**scope, "item": item, "index": index})
            if "tree" in node.spec:
                sub = self.run_tree(node.get("tree"), with_inputs, depth + 1)
            else:
                sub = self.run(tree, scope["inputs"], depth=depth + 1, start=node.get("start"), state=state, scope_extra={"item": item, "index": index}, trace=trace)
            if sub.status == "done":
                results.append(sub.output if sub.output is not None else sub.say)
            else:
                failures += 1
                results.append(None)
                if node.get("stop_on_fail", False):
                    return Outcome("error", tree.name, reason="each-failed", say=f"{node.id}: item {index} failed: {sub.say or sub.reason}")
            if sub.trace is not None and sub.trace is not trace:
                for s in sub.trace.steps:
                    s.depth = depth + 1
                    trace.steps.append(s)
                trace.gaps.extend(sub.trace.gaps)
        _put(state, node.get("into"), results)
        summary = f"{len(items)} item(s)" + (f", {failures} failed" if failures else "")
        return Step(node.id, "each", summary), node.next

    def _do_delegate(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        state = scope["state"]
        with_inputs = render(node.get("with", {}), scope)
        if "intent" in node.spec:
            text = render(node.get("intent"), scope)
            sub = self.run_intent(str(text), depth + 1)
            summary = f"intent {text!r} -> {sub.tree or '?'}: {sub.status}"
        else:
            sub = self.run_tree(node.get("tree"), with_inputs, depth + 1)
            summary = f"tree {node.get('tree')}: {sub.status}"
        if sub.trace is not None and sub.trace is not trace:
            for s in sub.trace.steps:
                s.depth = depth + 1
                trace.steps.append(s)
            trace.gaps.extend(sub.trace.gaps)
        _put(state, node.get("into"), {"status": sub.status, "say": sub.say, "output": sub.output, "reason": sub.reason, "tree": sub.tree})
        if sub.status == "asked":
            return Outcome("error", tree.name, reason="ask-in-delegate", say=f"{node.id}: {sub.tree} asked a question; delegated trees cannot pause.")
        return Step(node.id, "delegate", summary), node.next

    def _do_ask(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        if depth > 0:
            return Outcome("error", tree.name, reason="ask-in-subflow", say=f"{node.id}: only a top-level run can ask.")
        prompt = render(node.get("prompt"), scope)
        trace.add(Step(node.id, "ask", f"asked: {prompt}", depth=depth))
        snapshot = {"tree": tree.name, "node": node.id, "inputs": scope["inputs"], "state": scope["state"], "trace": [s.to_dict() for s in trace.steps]}
        return Outcome("asked", tree.name, question=str(prompt), snapshot=snapshot)

    def _do_done(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        say = node.get("say")
        output = node.get("output")
        say = render(say, scope, strict=False) if say is not None else None
        output = render(output, scope, strict=False) if output is not None else None
        trace.add(Step(node.id, "done", str(say) if say else "done", depth=depth))
        return Outcome("done", tree.name, say=str(say) if say is not None else None, output=output)

    def _do_fail(self, tree: Tree, node: Node, scope: dict[str, Any], trace: Trace, depth: int):
        say = node.get("say")
        say = render(say, scope, strict=False) if say is not None else None
        reason = str(node.get("reason"))
        trace.add(Step(node.id, "fail", f"{reason}: {say or ''}".rstrip(": "), depth=depth))
        return Outcome("failed", tree.name, reason=reason, say=str(say) if say is not None else None, output=render(node.get("output"), scope, strict=False) if node.get("output") is not None else None)


# ---------------------------------------------------------------- helpers


def _state_path(path: str) -> str:
    path = path.strip()
    head = path.split(".")[0].split("[")[0]
    if head in ("inputs", "state", "item", "index", "tree", "now", "env"):
        return path
    return "state." + path


def _put(state: dict[str, Any], path: str, value: Any) -> None:
    if path.startswith("state."):
        path = path[len("state."):]
    parts = path.split(".")
    cur = state
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _plain(value: Any) -> Any:
    return None if value is MISSING else value


def _brief(value: Any, limit: int = 80) -> str:
    if value is MISSING:
        return "<missing>"
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cond_text(cond: dict[str, Any]) -> str:
    return " and ".join(f"{k} {v!r}" for k, v in cond.items())


def _now() -> dict[str, Any]:
    now = _dt.datetime.now(_dt.timezone.utc)
    return {"iso": now.isoformat(timespec="seconds"), "date": now.date().isoformat(), "epoch": int(now.timestamp()), "year": now.year}
