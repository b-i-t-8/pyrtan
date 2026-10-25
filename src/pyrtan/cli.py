"""The ``pyrtan`` command.

    pyrtan check  [PATH ...]                 validate trees
    pyrtan list   [PATH ...]                 what is loaded and what it answers to
    pyrtan run    TEXT [--trees PATH]        answer a sentence, in this process
    pyrtan run    --tree NAME --input k=v    run a named tree
    pyrtan ask    TEXT --at URL              answer a sentence through a service or hub
    pyrtan serve  [PATH ...] [--port N]      host trees as a service
    pyrtan hub    [--peer URL ...]           route between services
    pyrtan gaps   --at URL                   branches waiting to be written
    pyrtan unanswered --at URL [--scaffold DIR]  trees waiting to be written
    pyrtan new    [NAME] [--from TEXT] [--dir PATH]  scaffold a tree file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .grow import Proposal, propose
from .registry import Registry, default_paths
from .runtime import Outcome, Runner
from .service import RemoteResolver, http_json, outcome_from_dict, serve
from .transport import RecordedTransport, RecordingTransport, UrllibTransport
from .tree import check, load_tree



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyrtan", description="A core set of expandable services that act on all forms of I/O through hardcoded, inspectable decision trees.")
    parser.add_argument("--version", action="version", version=f"pyrtan {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="validate trees; exit 1 if any has an error")
    p.add_argument("paths", nargs="*")
    p.add_argument("--quiet", action="store_true", help="only print problems")

    p = sub.add_parser("list", help="what is loaded and what it answers to")
    p.add_argument("paths", nargs="*")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="answer a sentence or run a named tree in this process")
    p.add_argument("text", nargs="?", help="the sentence to answer")
    p.add_argument("--trees", action="append", default=[], help="tree file or directory (repeatable; default $PYRTAN_TREES or ./trees)")
    p.add_argument("--tree", help="run this tree by name instead of matching text")
    p.add_argument("--input", action="append", default=[], metavar="K=V", help="an input for --tree (repeatable; JSON values accepted)")
    p.add_argument("--json", action="store_true", help="print the outcome as JSON")
    p.add_argument("--no-trace", action="store_true", help="do not print the trace")
    p.add_argument("--replay", help="answer HTTP from this fixture instead of the network")
    p.add_argument("--record", help="save every HTTP response to this fixture")
    p.add_argument("--hub", help="forward trees not held here to this hub or service")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="a value trees may read as {{ env.K }} (repeatable)")

    p = sub.add_parser("ask", help="answer a sentence through a running service or hub")
    p.add_argument("text")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="service or hub URL (default $PYRTAN_AT or the local hub)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-trace", action="store_true")

    p = sub.add_parser("serve", help="host trees as a service")
    p.add_argument("paths", nargs="*")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8701)
    p.add_argument("--name", default=None, help="what this service calls itself (default: the first directory's name)")
    p.add_argument("--hub", help="register with this hub, and forward unknown trees to it")
    p.add_argument("--advertise", help="the URL the hub should reach this service at, if not host:port")
    p.add_argument("--env", action="append", default=[], metavar="K=V")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("hub", help="route between services")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8700)
    p.add_argument("--peer", action="append", default=[], help="a service URL to start with (repeatable; services may also register themselves)")
    p.add_argument("--poll", type=float, default=10.0, help="seconds between health polls")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("gaps", help="decisions that met a value with no branch")
    p.add_argument("--at", help="a running service or hub")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("unanswered", help="sentences nobody answered, each with the tree that would")
    p.add_argument("--at", help="a running service or hub")
    p.add_argument("--json", action="store_true")
    p.add_argument("--scaffold", metavar="DIR", help="write a tree file for each sentence into DIR (existing files are kept)")

    p = sub.add_parser("new", help="scaffold a tree file")
    p.add_argument("name", nargs="?", help="the tree's name (derived from --from when omitted)")
    p.add_argument("--from", dest="sentence", metavar="TEXT", help="the sentence the tree should answer; numbers and hosts in it become slots")
    p.add_argument("--dir", default=".")

    args = parser.parse_args(argv)
    return {"check": cmd_check, "list": cmd_list, "run": cmd_run, "ask": cmd_ask, "serve": cmd_serve, "hub": cmd_hub, "gaps": cmd_gaps, "unanswered": cmd_unanswered, "new": cmd_new}[args.command](args)


# ---------------------------------------------------------------- commands


def _paths(given: list[str]) -> list[Path]:
    return [Path(p) for p in given] if given else default_paths()


def _files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(f for f in sorted(p.rglob("*.toml")) if f.name != "pyproject.toml")
        elif p.exists():
            out.append(p)
        else:
            print(f"pyrtan: {p}: no such file or directory", file=sys.stderr)
    return out


def cmd_check(args: argparse.Namespace) -> int:
    worst = 0
    files = _files(_paths(args.paths))
    if not files:
        print("pyrtan: nothing to check", file=sys.stderr)
        return 2
    for path in files:
        try:
            tree = load_tree(path)
        except Exception as e:  # TreeError or TOML trouble: already explained
            print(f"{path}: error: {e}")
            worst = max(worst, 1)
            continue
        problems = check(tree)
        errors = [p for p in problems if p.level == "error"]
        if errors:
            worst = max(worst, 1)
        if not args.quiet or problems:
            summary = f"{len(tree.nodes)} nodes, {len(tree.triggers)} triggers"
            print(f"{path}: {tree.name}: {summary}" + ("" if not problems else f", {len(problems)} note(s)"))
        for p in problems:
            print(f"  {p}")
    return worst


def cmd_list(args: argparse.Namespace) -> int:
    reg = Registry.from_paths(_paths(args.paths))
    if args.json:
        print(json.dumps({"trees": reg.capabilities(), "errors": reg.errors}, indent=2, ensure_ascii=False))
        return 0
    for tree in reg.trees():
        print(f"{tree.name}  v{tree.version}  {tree.description}")
        for t in tree.triggers:
            print(f"    {t.pattern}")
    for path, err in reg.errors.items():
        print(f"{path}: {err}", file=sys.stderr)
    return 0 if not reg.errors else 1


def _kv(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"pyrtan: expected K=V, got {item!r}")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def cmd_run(args: argparse.Namespace) -> int:
    if not args.text and not args.tree:
        print("pyrtan run: give a sentence, or --tree NAME", file=sys.stderr)
        return 2
    reg = Registry.from_paths(_paths(args.trees))
    for path, err in reg.errors.items():
        print(f"pyrtan: {path}: {err}", file=sys.stderr)
    transport: Any = RecordedTransport.from_file(args.replay) if args.replay else UrllibTransport()
    recorder = None
    if args.record:
        recorder = RecordingTransport(transport)
        transport = recorder
    runner = Runner(reg, transport, resolver=RemoteResolver(args.hub) if args.hub else None, env=_kv(args.env))
    if args.tree:
        outcome = runner.run_tree(args.tree, _kv(args.input))
    else:
        outcome = runner.run_intent(args.text)
    outcome = _answer_questions(outcome, runner.resume)
    if recorder is not None:
        recorder.save(args.record)
    return _report(outcome, args.json, not args.no_trace)


def _answer_questions(outcome: Outcome, resume) -> Outcome:
    while outcome.status == "asked":
        if not sys.stdin.isatty():
            return outcome
        try:
            answer = input(f"{outcome.question} ")
        except EOFError:
            return outcome
        outcome = resume(outcome.snapshot, answer)
    return outcome


def cmd_ask(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = http_json("POST", f"{at}/intent", {"text": args.text, "trace": not args.no_trace})
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} answered {status}: {body}", file=sys.stderr)
        return 3
    outcome = outcome_from_dict(body)

    def remote_resume(snapshot: dict[str, Any], answer: Any) -> Outcome:
        st, bd = http_json("POST", f"{at}/resume", {"snapshot": snapshot, "answer": answer, "trace": not args.no_trace})
        return outcome_from_dict(bd) if isinstance(bd, dict) else Outcome("error", "", reason="remote", say=f"{at} answered {st}")

    outcome = _answer_questions(outcome, remote_resume)
    return _report(outcome, args.json, not args.no_trace)


def _report(outcome: Outcome, as_json: bool, with_trace: bool) -> int:
    if as_json:
        print(json.dumps(outcome.to_dict(with_trace=with_trace), indent=2, ensure_ascii=False, default=str))
    else:
        if with_trace and outcome.trace is not None:
            print(outcome.trace.render())
            print()
        if outcome.status == "done":
            print(outcome.say if outcome.say is not None else json.dumps(outcome.output, ensure_ascii=False))
        elif outcome.status == "asked":
            print(f"asked: {outcome.question}")
        else:
            print(f"{outcome.status} ({outcome.reason}): {outcome.say or ''}".rstrip(": "))
    return {"done": 0, "failed": 1, "asked": 4}.get(outcome.status, 3)


def cmd_serve(args: argparse.Namespace) -> int:
    paths = _paths(args.paths)
    name = args.name or (paths[0].resolve().name if paths else "pyrtan")
    service, server = serve(paths, args.host, args.port, name=name, hub=args.hub, env=_kv(args.env), advertise=args.advertise, quiet=args.quiet)
    print(f"pyrtan service {name!r}: {len(service.registry)} tree(s) at {server.url}" + (f", registered with {args.hub}" if args.hub else ""), flush=True)
    for path, err in service.registry.errors.items():
        print(f"  not loaded: {path}: {err}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_hub(args: argparse.Namespace) -> int:
    from .hub import serve_hub

    hub, server = serve_hub(args.host, args.port, peers=args.peer, poll_every=args.poll, quiet=args.quiet)
    print(f"pyrtan hub at {server.url}: {len(hub.peers)} peer(s)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        server.stop()
    return 0


def cmd_gaps(args: argparse.Namespace) -> int:
    if not args.at:
        print("pyrtan gaps: give --at URL of a running service or hub", file=sys.stderr)
        return 2
    try:
        status, body = http_json("GET", f"{args.at.rstrip('/')}/gaps")
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    gaps = (body or {}).get("gaps", []) if isinstance(body, dict) else []
    if args.json:
        print(json.dumps({"gaps": gaps}, indent=2, ensure_ascii=False))
        return 0
    if not gaps:
        print("no gaps: every decision so far had a branch")
        return 0
    for g in gaps:
        peer = f" ({g['peer']})" if g.get("peer") else ""
        print(f"{g['tree']}.{g['node']}: {g['on']} = {json.dumps(g['observed'], ensure_ascii=False)}{peer}")
    return 0


def cmd_unanswered(args: argparse.Namespace) -> int:
    if not args.at:
        print("pyrtan unanswered: give --at URL of a running service or hub", file=sys.stderr)
        return 2
    try:
        status, body = http_json("GET", f"{args.at.rstrip('/')}/unanswered")
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    entries = (body or {}).get("unanswered", []) if isinstance(body, dict) else []
    if args.json:
        print(json.dumps({"unanswered": entries}, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print("nothing unanswered: every sentence so far found a tree")
        return 0
    for e in entries:
        prop = e.get("proposal") or propose(e["text"]).to_dict()
        peer = f" ({e['peer']})" if e.get("peer") else ""
        print(f"{e['text']!r}  x{e.get('count', 1)}{peer}")
        print(f"    would be: {prop['name']}  answering  {prop['trigger']}")
        if args.scaffold:
            path = _write_scaffold(Proposal(prop["text"], prop["name"], prop["trigger"], prop.get("inputs") or {}), Path(args.scaffold))
            print(f"    {'wrote' if path else 'kept'} {Path(args.scaffold) / (prop['name'] + '.toml')}")
        else:
            print(f"    {prop['scaffold']}")
    if args.scaffold:
        print(f"now: pyrtan check {args.scaffold}")
    return 0


def _write_scaffold(proposal: Proposal, directory: Path) -> Path | None:
    """Write the proposal's tree file unless one is already there."""
    path = directory / f"{proposal.name}.toml"
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(proposal.scaffold(), encoding="utf-8")
    return path


def cmd_new(args: argparse.Namespace) -> int:
    if not args.name and not args.sentence:
        print("pyrtan new: give a NAME, or --from \"the sentence it should answer\"", file=sys.stderr)
        return 2
    proposal = propose(args.sentence or args.name.replace("-", " "), name=args.name)
    path = Path(args.dir) / f"{proposal.name}.toml"
    if path.exists():
        print(f"pyrtan: {path} exists", file=sys.stderr)
        return 1
    _write_scaffold(proposal, Path(args.dir))
    print(f"wrote {path}: {proposal.name!r} answering {proposal.trigger!r}; edit it, then: pyrtan check {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
