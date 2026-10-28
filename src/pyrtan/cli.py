"""The ``pyrtan`` command.

    pyrtan check  [PATH ...]                 validate trees
    pyrtan list   [PATH ...]                 what is loaded and what it answers to
    pyrtan run    TEXT [--trees PATH]        answer a sentence, in this process
    pyrtan run    --tree NAME --input k=v    run a named tree
    pyrtan match  TEXT [--trees PATH]        how a sentence reads: the rewrites and every trigger's verdict
    pyrtan ask    TEXT --at URL              answer a sentence through a service or hub
    pyrtan chat   [--at HUB] [--bot|--people]  talk to a random stranger or a bot, through a hub started --chat
    pyrtan serve  [PATH ...] [--port N]      host trees as a service
    pyrtan hub    [--peer URL ...]           route between services
    pyrtan hub    --trees DIR                and hold trees itself: a swarm in one process
    pyrtan hub    --federate --advertise URL [--follow URL ...] [--listed]  and between hubs
    pyrtan hub    --chat                     and keep a lobby: strangers and bots, Omegle-style
    pyrtan follow [URL] --at HUB [--listed]  follow another hub (and route to it); default: join the default network
    pyrtan unfollow URL --at HUB
    pyrtan peers  --at HUB                   services, and hubs followed and following
    pyrtan directory [--at HUB]              every hub that asked to be found, as the swarm knows it
    pyrtan keep KEY [FILE] --at HUB          give your hub a blob to keep, and have its keepers keep it
    pyrtan kept   --at HUB                   what your hub keeps, who holds it, and what it keeps for others
    pyrtan fetch  KEY --at HUB               get a blob back, from your hub or from whoever holds it
    pyrtan gaps   --at URL                   branches waiting to be written
    pyrtan unanswered --at URL [--scaffold DIR]  trees waiting to be written
    pyrtan new    [NAME] [--from TEXT] [--dir PATH]  scaffold a tree file
    pyrtan search [WORDS] [--catalog URL]    find trees in a catalog
    pyrtan pull   NAME[@VERSION] [--dir]     fetch a tree from a catalog, verified
    pyrtan index  DIR [--out FILE]           build a catalog index from a directory
    pyrtan console --out FILE                write the console as one static HTML file

``serve`` and ``hub`` take ``--token`` (or ``$PYRTAN_TOKEN``) to require a
bearer token, ``--cors ORIGIN`` to let a browser elsewhere call them, and
``serve`` takes ``--gaps FILE`` to keep gaps across restarts and
``--egress public`` to refuse private addresses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .catalog import Catalog, CatalogError, build_bundle, build_index, catalog_url
from .federation import bootstrap_candidates
from .grow import Proposal, propose
from .page import MANIFEST, PAGE, PYRTAN_JS, SERVICE_WORKER
from .registry import Registry, default_paths
from .runtime import Outcome, Runner
from .service import RemoteResolver, http_json, outcome_from_dict, serve
from .adapters import LocalAdapters
from .transport import EGRESS_POLICIES, RecordedTransport, RecordingTransport, UrllibTransport
from .tree import TreeError, check, load_tree



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyrtan", description="A core set of expandable services that act on all forms of I/O through inspectable decision trees.")
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
    p.add_argument("--record", help="save every response (HTTP and the other sources) to this fixture")
    p.add_argument("--hub", help="forward trees not held here to this hub or service")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="a value trees may read as {{ env.K }} (repeatable)")
    p.add_argument("--egress", choices=EGRESS_POLICIES, default="any", help="'public' refuses private, loopback and link-local addresses")
    _token_arg(p, "the token the hub (--hub) requires, if any")

    p = sub.add_parser("match", help="how a sentence reads against the trees: the rewrites, every trigger's verdict, the winner")
    p.add_argument("text")
    p.add_argument("--trees", action="append", default=[], help="tree file or directory (repeatable; default $PYRTAN_TREES or ./trees)")
    p.add_argument("--now", help="read dates against this day (YYYY-MM-DD) instead of today")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ask", help="answer a sentence through a running service or hub")
    p.add_argument("text")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="service or hub URL (default $PYRTAN_AT or the local hub)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-trace", action="store_true")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("chat", help="talk to a random stranger or a bot, through a hub started with --chat")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="the hub (default $PYRTAN_AT or the local hub)")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--bot", action="store_true", help="meet a bot: trees on the network, not a person")
    who.add_argument("--people", action="store_true", help="meet people only; never a bot")
    p.add_argument("--interests", default="", metavar="WORDS", help="up to five words, comma-separated; a stranger or bot that shares one is met first")
    p.add_argument("--trace", action="store_true", help="print the path behind each bot line")
    p.add_argument("--no-typing", action="store_true", help="turn the typing indicator off: no \"Stranger is typing…\" (a terminal reads whole lines, so it never says you are typing)")
    _token_arg(p, "the token the hub requires, if any")

    p = sub.add_parser("serve", help="host trees as a service")
    p.add_argument("paths", nargs="*")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8701)
    p.add_argument("--name", default=None, help="what this service calls itself (default: the first directory's name)")
    p.add_argument("--hub", help="register with this hub, and forward unknown trees to it")
    p.add_argument("--advertise", help="the URL the hub should reach this service at, if not host:port")
    p.add_argument("--env", action="append", default=[], metavar="K=V")
    p.add_argument("--gaps", metavar="FILE", help="keep gaps in this file (JSON lines) so they survive a restart")
    p.add_argument("--unanswered", metavar="FILE", default=os.environ.get("PYRTAN_UNANSWERED"), help="keep unanswered sentences in this file (JSON lines) so they survive a restart (default $PYRTAN_UNANSWERED)")
    p.add_argument("--watch", action="store_true", default=os.environ.get("PYRTAN_WATCH", "").lower() in ("1", "true", "yes"), help="run the trees' [[watches]]: a clock, a file, a topic or a serial line starts a run (default $PYRTAN_WATCH)")
    p.add_argument("--egress", choices=EGRESS_POLICIES, default=os.environ.get("PYRTAN_EGRESS", "any"), help="'public' refuses private, loopback and link-local addresses (default $PYRTAN_EGRESS or any)")
    _access_args(p)
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("watch", help="run the trees' [[watches]] in this process, printing each run; nothing is served")
    p.add_argument("paths", nargs="*")
    p.add_argument("--env", action="append", default=[], metavar="K=V")
    p.add_argument("--gaps", metavar="FILE", help="keep gaps in this file")
    p.add_argument("--egress", choices=EGRESS_POLICIES, default=os.environ.get("PYRTAN_EGRESS", "any"))
    p.add_argument("--hub", help="delegate to this hub what the trees here do not hold")
    p.add_argument("--json", action="store_true", help="one JSON outcome per line instead of prose")
    _token_arg(p, "the hub's token, if any")

    p = sub.add_parser("watches", help="what a running service or hub is watching, and what each watch last did")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="a running service or hub (default $PYRTAN_AT or the local hub)")
    p.add_argument("--json", action="store_true")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("grow", help="draft the branch a gap asks for, by rules, for you to accept")
    p.add_argument("--trees", action="append", default=[], help="tree file or directory holding the trees the gaps name (repeatable; default $PYRTAN_TREES or ./trees)")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT"), help="read gaps from this running service or hub (default $PYRTAN_AT)")
    p.add_argument("--from", dest="source", metavar="FILE", help="read gaps from a file: a --gaps JSON-lines file, a /gaps answer, or a reports.json")
    p.add_argument("--reports", nargs="?", const="", metavar="HUB_OR_FILE", help="read the federation's gap reports for your catalog trees (from a hub's URL, a baked reports.json, or pyrtan.com)")
    p.add_argument("--tree", action="append", default=[], help="only gaps of this tree (repeatable)")
    p.add_argument("--apply", action="store_true", help="write each draft into its tree file (the file must load clean afterwards)")
    p.add_argument("--json", action="store_true")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("hub", help="route between services")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8700)
    p.add_argument("--name", default="hub", help="what this hub calls itself, to the console and to hubs that follow it")
    p.add_argument("--trees", action="append", default=[], metavar="PATH", help="trees this hub holds itself, in-process (repeatable); a swarm in one process")
    p.add_argument("--handout", action="append", default=[t for t in os.environ.get("PYRTAN_HANDOUT", "").split(os.pathsep) if t], metavar="PATH", help="trees for this hub's trusted members (a phone or a browser joined with the token) to run, not this hub (repeatable; default $PYRTAN_HANDOUT); its own --trees are handed out too")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="a value the hub's own trees may read as {{ env.K }}")
    p.add_argument("--gaps", metavar="FILE", help="keep the hub's own trees' gaps in this file")
    p.add_argument("--unanswered", metavar="FILE", default=os.environ.get("PYRTAN_UNANSWERED"), help="keep unanswered sentences in this file across restarts (default $PYRTAN_UNANSWERED)")
    p.add_argument("--watch", action="store_true", default=os.environ.get("PYRTAN_WATCH", "").lower() in ("1", "true", "yes"), help="run the hub's own trees' [[watches]] (default $PYRTAN_WATCH)")
    p.add_argument("--egress", choices=EGRESS_POLICIES, default=os.environ.get("PYRTAN_EGRESS", "any"), help="for the hub's own trees: 'public' refuses private addresses")
    p.add_argument("--peer", action="append", default=[], help="a service URL to start with (repeatable; services may also register themselves)")
    p.add_argument("--poll", type=float, default=10.0, help="seconds between health polls")
    _access_args(p)
    p.add_argument("--federate", action="store_true", help="answer /actor and /inbox so other hubs can follow this one, and follow others")
    p.add_argument("--advertise", default=os.environ.get("PYRTAN_ADVERTISE"), help="the URL other hubs reach this hub at (implies --federate; default $PYRTAN_ADVERTISE, else this host and port)")
    p.add_argument("--follow", action="append", default=[h for h in os.environ.get("PYRTAN_FOLLOW", "").replace(",", " ").split() if h], metavar="URL", help="a hub to follow on start (repeatable; default $PYRTAN_FOLLOW; to join the default network, --join default)")
    p.add_argument("--join", action="append", default=[h for h in os.environ.get("PYRTAN_JOIN", "").replace(",", " ").split() if h], metavar="INVITE", help="a network to be in on start: 'default' for the public one built into pyrtan, or an invite NAME:SECRET[@HUB] (repeatable; default $PYRTAN_JOIN)")
    p.add_argument("--federation-secret", default=os.environ.get("PYRTAN_FEDERATION_SECRET"), help="only hubs that sign their Follow with this secret may follow (default $PYRTAN_FEDERATION_SECRET; without it, follows are open)")
    p.add_argument("--closed", action="store_true", help="take no follows at all (this hub can still follow others)")
    p.add_argument("--state", metavar="FILE", default=os.environ.get("PYRTAN_STATE"), help="keep who this hub follows and is followed by, and the directory, in this file across restarts (default $PYRTAN_STATE)")
    p.add_argument("--listed", action="store_true", default=os.environ.get("PYRTAN_LISTED", "").lower() in ("1", "true", "yes"), help="put this hub in the directory it gossips, so others can find it (default $PYRTAN_LISTED)")
    p.add_argument("--store", metavar="DIR", default=os.environ.get("PYRTAN_STORE"), help="where this hub keeps blobs, its own and others' (default $PYRTAN_STORE, else next to --state)")
    p.add_argument("--replicas", type=int, default=int(os.environ.get("PYRTAN_REPLICAS", "2")), help="how many members or hubs should keep each of this hub's blobs (default $PYRTAN_REPLICAS or 2)")
    p.add_argument("--chat", action="store_true", default=os.environ.get("PYRTAN_CHAT", "").lower() in ("1", "true", "yes"), help="keep a lobby at /chat where people meet a random stranger here or at a hub this one follows, or a bot made of the trees it routes to (default $PYRTAN_CHAT)")
    p.add_argument("--chat-public", action="store_true", default=os.environ.get("PYRTAN_CHAT_PUBLIC", "").lower() in ("1", "true", "yes"), help="open the lobby to callers without the token (implies --chat): they meet people only, never a bot, and everything else still needs the token (default $PYRTAN_CHAT_PUBLIC)")
    p.add_argument("--chat-typing", action=argparse.BooleanOptionalAction, default=os.environ.get("PYRTAN_CHAT_TYPING", "").lower() not in ("0", "false", "no", "off"), help="the lobby's typing indicator: \"Stranger is typing…\" for anyone who keeps theirs on (default on; $PYRTAN_CHAT_TYPING=0 turns it off)")
    p.add_argument("--report-gaps", action="store_true", default=os.environ.get("PYRTAN_REPORT_GAPS", "").lower() in ("1", "true", "yes"), help="put the gaps this swarm's catalog trees meet (tree id, node, value; never inputs) into its announces, so they reach the tree's author through the federation and pyrtan.com (default $PYRTAN_REPORT_GAPS)")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("follow", help="follow another hub, through a running federated hub of yours")
    p.add_argument("url", nargs="?", default=None, help="the other hub's URL (default: join the default network, as `pyrtan join`)")
    p.add_argument("--network", help="follow it in this network of yours, so the directory heard over the link stays in it")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    p.add_argument("--listed", action="store_true", help="also put your hub in the directory it gossips, so others can find it")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("join", help="put your hub in a network: the default one from pyrtan.com, or one an invite names")
    p.add_argument("invite", nargs="?", default="default", help="NAME:SECRET[@HUB] from `pyrtan network new` on another hub, or 'default' (the default)")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    p.add_argument("--listed", action="store_true", help="also put your hub in the directory it gossips, so others can find it")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("network", help="the networks your hub is in: start one, list them with their invites, leave one")
    p.add_argument("action", choices=["new", "list", "leave"])
    p.add_argument("name", nargs="?", help="the network's name (new, leave)")
    p.add_argument("--public", action="store_true", help="new: show this network's part of the directory to anyone, not only to you")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    p.add_argument("--json", action="store_true")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("unfollow", help="stop following a hub")
    p.add_argument("url", help="the other hub's URL")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("keep", help="give your hub a blob to keep; its keepers get a copy")
    p.add_argument("key", help="a key: lowercase, digits, dot, dash, underscore, slash")
    p.add_argument("file", nargs="?", help="a JSON file to keep (default: read JSON from stdin)")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("kept", help="what your hub keeps, who holds each blob, and what it keeps for others")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"))
    p.add_argument("--json", action="store_true")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("fetch", help="get a blob back from your hub, or from whoever holds it")
    p.add_argument("key")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"))
    p.add_argument("--out", help="write the data here (default: stdout)")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("directory", help="every hub that asked to be found, as a hub knows it")
    p.add_argument("--at", default=None, help="a federated hub to ask (default $PYRTAN_AT, else the local hub, else the first hub from pyrtan.com's hub list that answers)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("peers", help="a hub's services, and the hubs it follows and is followed by")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"))
    p.add_argument("--json", action="store_true")
    _token_arg(p, "the hub's token, if any")

    p = sub.add_parser("gaps", help="decisions that met a value with no branch")
    p.add_argument("--at", help="a running service or hub")
    p.add_argument("--json", action="store_true")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("reports", help="gaps reported across the federation for catalog trees, as a hub (or pyrtan.com) carries them")
    p.add_argument("id", nargs="?", default=None, metavar="NAMESPACE/NAME", help="only this tree (any version)")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT"), help="a federated hub (default $PYRTAN_AT, else the snapshot at pyrtan.com)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("unanswered", help="sentences nobody answered, each with the tree that would")
    p.add_argument("--at", help="a running service or hub")
    p.add_argument("--json", action="store_true")
    p.add_argument("--scaffold", metavar="DIR", help="write a tree file for each sentence into DIR (existing files are kept)")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("new", help="scaffold a tree file")
    p.add_argument("name", nargs="?", help="the tree's name (derived from --from when omitted)")
    p.add_argument("--from", dest="sentence", metavar="TEXT", help="the sentence the tree should answer; numbers and hosts in it become slots")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("search", help="find trees in a catalog")
    p.add_argument("words", nargs="*", help="words that must all appear in the name, description, tags, hosts or triggers")
    p.add_argument("--catalog", default=None, help=f"catalog URL or directory (default $PYRTAN_CATALOG or {catalog_url()})")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pull", help="fetch a tree from a catalog, verified against its index")
    p.add_argument("spec", nargs="+", metavar="[NAMESPACE/]NAME[@VERSION]")
    p.add_argument("--dir", default=None, help="where to write (default: the first of $PYRTAN_TREES or ./trees)")
    p.add_argument("--catalog", default=None, help=f"catalog URL or directory (default $PYRTAN_CATALOG or {catalog_url()})")
    p.add_argument("--force", action="store_true", help="replace a file that exists and differs")

    p = sub.add_parser("index", help="build a catalog index.json from directories of trees")
    p.add_argument("dir", nargs="+")
    p.add_argument("--out", default=None, help="write the index here (default: DIR/index.json, or COPY/index.json with --copy; '-' for stdout)")
    p.add_argument("--copy", metavar="DIR", default=None, help="copy every tree file into DIR as <namespace>/<name>.toml and describe that layout (needed with more than one source directory)")
    p.add_argument("--allow-unlicensed", action="store_true", help="index trees with no 'license' too")
    p.add_argument("--require-namespace", action="store_true", help="leave out trees with no 'namespace'")

    p = sub.add_parser("publish", help="put a tree into a checkout of the catalog repository, ready for a pull request")
    p.add_argument("file", help="the tree file; it must carry 'namespace' and 'license'")
    p.add_argument("--into", required=True, metavar="CHECKOUT", help="a checkout of github.com/b-i-t-8/pyrtan (or any repository with a catalog/ directory)")
    p.add_argument("--owner", action="append", default=[], metavar="LOGIN", help="GitHub login(s) that own the namespace; needed when the namespace is new")

    p = sub.add_parser("console", help="write the console as one static HTML file, to host anywhere")
    p.add_argument("--out", required=True, help="the file to write ('-' for stdout)")

    args = parser.parse_args(argv)
    commands = {
        "check": cmd_check, "list": cmd_list, "run": cmd_run, "match": cmd_match, "ask": cmd_ask, "chat": cmd_chat, "serve": cmd_serve, "hub": cmd_hub, "gaps": cmd_gaps, "unanswered": cmd_unanswered, "new": cmd_new,
        "watch": cmd_watch, "watches": cmd_watches, "grow": cmd_grow,
        "search": cmd_search, "pull": cmd_pull, "index": cmd_index, "publish": cmd_publish, "console": cmd_console,
        "follow": cmd_follow, "join": cmd_join, "network": cmd_network, "unfollow": cmd_unfollow, "peers": cmd_peers, "directory": cmd_directory, "reports": cmd_reports,
        "keep": cmd_keep, "kept": cmd_kept, "fetch": cmd_fetch,
    }
    return commands[args.command](args)


def _token_arg(p: argparse.ArgumentParser, help_text: str) -> None:
    p.add_argument("--token", default=os.environ.get("PYRTAN_TOKEN"), help=f"{help_text} (default $PYRTAN_TOKEN)")


def _access_args(p: argparse.ArgumentParser) -> None:
    _token_arg(p, "require this bearer token on every request but /health and the console")
    p.add_argument("--cors", action="append", default=[], metavar="ORIGIN", help="let a browser at this origin call (repeatable; '*' for any; e.g. https://pyrtan.com)")


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
        via = ", ".join(s for s in tree.sources() if s != "http")
        print(f"{tree.name}  v{tree.version}  {tree.description}" + (f"  [{via}]" if via else ""))
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
    transport: Any = RecordedTransport.from_file(args.replay) if args.replay else UrllibTransport(egress=args.egress)
    adapters: Any = transport if args.replay else LocalAdapters(egress=args.egress)
    recorder = None
    if args.record:
        recorder = RecordingTransport(transport, adapters)
        transport = adapters = recorder
    runner = Runner(reg, transport, resolver=RemoteResolver(args.hub, args.token) if args.hub else None, env=_kv(args.env), adapters=adapters)
    if args.tree:
        outcome = runner.run_tree(args.tree, _kv(args.input))
    else:
        outcome = runner.run_intent(args.text)
    outcome = _answer_questions(outcome, runner.resume)
    if recorder is not None:
        recorder.save(args.record)
    return _report(outcome, args.json, not args.no_trace)


def cmd_match(args: argparse.Namespace) -> int:
    """Show how a sentence is read, without running anything: the rewrites
    the language rules made, each trigger that matched or refused, and the
    winner. The tool for someone writing a trigger."""
    import datetime as _dt

    reg = Registry.from_paths(_paths(args.trees))
    for path, err in reg.errors.items():
        print(f"pyrtan: {path}: {err}", file=sys.stderr)
    now: Any = _dt.datetime.now(_dt.timezone.utc)
    if args.now:
        try:
            now = _dt.date.fromisoformat(args.now)
        except ValueError:
            print(f"pyrtan match: --now must be YYYY-MM-DD, got {args.now!r}", file=sys.stderr)
            return 2
    reading = reg.read(args.text, now)
    if args.json:
        print(json.dumps(reading.to_dict(), indent=2, ensure_ascii=False, default=str))
        return 0 if reading.winner else 1
    print(f"sentence: {reading.text!r}")
    if reading.cleaned != reading.text:
        print(f"read as:  {reading.cleaned!r}")
    for step in reading.steps:
        print(f"    {step}")
    if not reading.considered:
        print("no trigger matched" + (f" ({len(reg.candidates())} tried)" if reg.candidates() else " (no trees loaded)"))
        return 1
    print("triggers:")
    for m in sorted(reading.considered, key=lambda m: (-m.specificity, m.tree)):
        verdict = "refused" if m.rejected else ("match" if m in reading.matches else "match, less specific")
        print(f"    {m.tree}: {m.trigger.pattern!r}  {m.specificity} literal chars  -> {verdict}")
        if m.inputs and not m.rejected:
            print(f"        inputs: {json.dumps(m.inputs, ensure_ascii=False, default=str)}")
        for note in m.notes:
            print(f"        {note}")
    if reading.winner:
        print(f"winner: {reading.winner.tree}")
        return 0
    if reading.matches:
        print(f"tie: {sorted({m.tree for m in reading.matches})}; make one trigger more specific")
    else:
        print("no match: every trigger that fit refused a slot")
    return 1


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
        status, body = http_json("POST", f"{at}/intent", {"text": args.text, "trace": not args.no_trace}, token=args.token)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} answered {status}: {body}", file=sys.stderr)
        return 3
    outcome = outcome_from_dict(body)

    def remote_resume(snapshot: dict[str, Any], answer: Any) -> Outcome:
        st, bd = http_json("POST", f"{at}/resume", {"snapshot": snapshot, "answer": answer, "trace": not args.no_trace}, token=args.token)
        return outcome_from_dict(bd) if isinstance(bd, dict) else Outcome("error", "", reason="remote", say=f"{at} answered {st}")

    outcome = _answer_questions(outcome, remote_resume)
    return _report(outcome, args.json, not args.no_trace)


def cmd_chat(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    want = "bot" if args.bot else "people" if args.people else "anyone"

    def post(path: str, body: dict[str, Any], timeout: float = 15.0) -> tuple[int, Any]:
        return http_json("POST", f"{at}{path}", body, timeout=timeout, token=args.token)

    try:
        status, body = post("/chat/start", {"want": want, "interests": args.interests, "typing_indicator": not args.no_typing})
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict) or "seat" not in body:
        print(f"pyrtan: {at}: {body.get('say') if isinstance(body, dict) else body or status}", file=sys.stderr)
        return 3
    seat = body["seat"]
    state = {"after": 0, "owed": 0, "partner": body.get("partner"), "typing": False}  # owed: lines said to a bot it has not answered yet
    lock = threading.Condition()
    stop = threading.Event()
    print("Type to talk. /next for someone new, /quit or Ctrl-D to leave.", flush=True)

    def show(answer: dict[str, Any]) -> None:
        with lock:
            for e in answer.get("events", []):
                if e["n"] <= state["after"]:
                    continue
                state["after"] = e["n"]
                if e["kind"] == "system":
                    print(f"  {e['text']}", flush=True)
                elif e["kind"] == "bot":
                    print(f"Bot: {e['text']}", flush=True)
                    if args.trace and isinstance(e.get("trace"), dict):
                        print(outcome_from_dict({**e, "say": e["text"]}).trace.render(), flush=True)
                    state["owed"] = max(0, state["owed"] - 1)
                elif e["kind"] == "stranger":
                    print(f"Stranger: {e['text']}", flush=True)
            state["partner"] = answer.get("partner")
            typing = bool(answer.get("partner_typing"))
            if typing and not state["typing"]:
                print("  Bot is thinking…" if (state["partner"] or {}).get("kind") == "bot" else "  Stranger is typing…", flush=True)
            state["typing"] = typing
            lock.notify_all()

    show(body)

    def pump() -> None:
        while not stop.is_set():
            try:
                ask = {"seat": seat, "after": state["after"], "wait": 20}
                if body.get("typing_indicator"):
                    ask["partner_typing"] = state["typing"]  # woken when it changes
                st, answer = post("/chat/poll", ask, timeout=40.0)
            except Exception as e:
                if stop.is_set():
                    return  # leaving: the hub not answering any more is expected
                print(f"  ({at} did not answer: {e})", flush=True)
                stop.wait(2)
                continue
            if st == 404:
                print("  This seat is gone; start again.", flush=True)
                stop.set()
                with lock:
                    lock.notify_all()
                return
            if st == 200 and isinstance(answer, dict):
                show(answer)
                if answer.get("state") == "left":
                    return

    threading.Thread(target=pump, daemon=True).start()
    try:
        for line in sys.stdin:
            line = line.strip()
            if stop.is_set() or line in ("/quit", "/q"):
                break
            if line in ("/next", "/n"):
                st, answer = post("/chat/next", {"seat": seat, "after": state["after"]})
                if st == 200 and isinstance(answer, dict):
                    show(answer)
                continue
            if not line:
                continue
            with lock:
                to_bot = (state["partner"] or {}).get("kind") == "bot"
                if to_bot:
                    state["owed"] += 1
            st, answer = post("/chat/say", {"seat": seat, "text": line})
            if st != 200:
                with lock:
                    state["owed"] -= 1 if to_bot else 0
                print(f"  {answer.get('say') if isinstance(answer, dict) else answer}", flush=True)
        else:
            # Input ran out (a pipe): wait for the bot to answer what it was told.
            with lock:
                lock.wait_for(lambda: state["owed"] <= 0 or stop.is_set(), timeout=60)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            post("/chat/leave", {"seat": seat}, timeout=5.0)
        except Exception:
            pass
    return 0


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
    service, server = serve(paths, args.host, args.port, name=name, hub=args.hub, env=_kv(args.env), advertise=args.advertise, quiet=args.quiet, token=args.token, cors=args.cors, gaps=args.gaps, egress=args.egress, unanswered=args.unanswered, watch=args.watch)
    watching = f", watching {len(service.watcher)}" if service.watcher else ""
    print(f"pyrtan service {name!r}: {len(service.registry)} tree(s) at {server.url}{watching}" + (f", registered with {args.hub}" if args.hub else "") + _access_note(args), flush=True)
    for path, err in service.registry.errors.items():
        print(f"  not loaded: {path}: {err}", file=sys.stderr)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.stop()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .service import Service

    paths = _paths(args.paths)
    reg = Registry.from_paths(paths)
    for path, err in reg.errors.items():
        print(f"pyrtan: {path}: {err}", file=sys.stderr)
    service = Service(reg, name="watch", hub=args.hub, env=_kv(args.env), token=args.token, gaps=args.gaps, egress=args.egress)
    from .watch import Watcher

    def after(w, event, outcome):
        if args.json:
            print(json.dumps({"watch": w.name, "event": event, **outcome.to_dict(with_trace=False)}, ensure_ascii=False, default=str), flush=True)
        else:
            changed = " (the path changed)" if w.path_changed_at and w.last_at == w.path_changed_at else ""
            print(f"{time.strftime('%H:%M:%S')} {w.name}: {outcome.status}" + (f" ({outcome.reason})" if outcome.reason else "") + (f": {outcome.say}" if outcome.say else "") + changed, flush=True)

    watcher = Watcher(reg, service.run_watched, env=_kv(args.env), egress=args.egress, after=after, note=lambda text: print(f"pyrtan: {text}", file=sys.stderr, flush=True)).start()
    if not len(watcher):
        print("pyrtan watch: no tree here has [[watches]]; nothing to do", file=sys.stderr)
        watcher.stop()
        return 2
    for w in watcher.status()["watches"]:
        print(f"watching {w['name']}: {w['what']}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
    return 0


def cmd_watches(args: argparse.Namespace) -> int:
    try:
        status, body = http_json("GET", f"{args.at.rstrip('/')}/watches", token=args.token)
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {args.at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    watches = (body or {}).get("watches", []) if isinstance(body, dict) else []
    if args.json:
        print(json.dumps({"watches": watches}, indent=2, ensure_ascii=False))
        return 0
    if not watches:
        print(f"{args.at} watches nothing (start it with --watch, and give a tree [[watches]])")
        return 0
    for w in watches:
        last = w.get("last") or {}
        when = time.strftime("%H:%M:%S", time.localtime(w["last_at"])) if w.get("last_at") else "never"
        line = f"{w['name']}  {w['kind']} {w['what']}  fired {w.get('fires', 0)}× (last {when})"
        if last:
            line += f": {last.get('status')}" + (f" ({last.get('reason')})" if last.get("reason") else "") + (f" {last.get('say')}" if last.get("say") else "")
        if w.get("path_changed_at"):
            line += f"  [path changed at {time.strftime('%H:%M:%S', time.localtime(w['path_changed_at']))}: {' > '.join(w.get('previous_path') or [])} -> {' > '.join(w.get('path') or [])}]"
        if w.get("last_error"):
            line += f"  error: {w['last_error']}"
        if w.get("peer") and w["peer"] != "local":
            line += f"  ({w['peer']})"
        print(line)
    return 0


def cmd_grow(args: argparse.Namespace) -> int:
    from .grow import apply_draft, draft_branch

    gaps: list[dict[str, Any]] = []
    if args.at:
        try:
            status, body = http_json("GET", f"{args.at.rstrip('/')}/gaps", token=args.token)
        except Exception as e:
            print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
            return 3
        if status != 200 or not isinstance(body, dict):
            print(f"pyrtan: {args.at} answered {status}", file=sys.stderr)
            return 3
        gaps.extend(g for g in body.get("gaps", []) if isinstance(g, dict))
    if args.source:
        text = Path(args.source).read_text(encoding="utf-8")
        try:
            data = json.loads(text)
            if isinstance(data, dict) and ("gaps" in data or "reports" in data):
                rows = data.get("gaps") or data.get("reports") or []  # a /gaps answer, or a reports.json
            elif isinstance(data, dict):
                rows = [data]  # one gap, one line
            else:
                rows = data
        except json.JSONDecodeError:
            rows = [json.loads(l) for l in text.splitlines() if l.strip()]  # a --gaps file: JSON lines
        gaps.extend(_from_report(r) for r in rows if isinstance(r, dict))
    if args.reports is not None:
        given = args.reports
        source = f"{given.rstrip('/')}/reports" if given.startswith(("http://", "https://")) else given if given else (os.environ.get("PYRTAN_REPORTS") or "https://pyrtan.com/rendezvous/reports.json")
        try:
            if source.startswith(("http://", "https://")):
                status, body = http_json("GET", source)
            else:
                status, body = 200, json.loads(Path(source).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"pyrtan: {source}: {e}", file=sys.stderr)
            return 3
        gaps.extend(_from_report(r) for r in (body or {}).get("reports", []) if isinstance(r, dict))
    if not args.at and not args.source and args.reports is None:
        print("pyrtan grow: say where the gaps are: --at URL, --from FILE or --reports", file=sys.stderr)
        return 2
    if args.tree:
        gaps = [g for g in gaps if g.get("tree") in args.tree]
    reg = Registry.from_paths(_paths(args.trees))
    for path, err in reg.errors.items():
        print(f"pyrtan: {path}: {err}", file=sys.stderr)
    drafts = []
    skipped: list[str] = []
    seen: set[str] = set()
    for g in gaps:
        tree = reg.get(str(g.get("tree", "")))
        if tree is None:
            skipped.append(f"{g.get('tree')}: not among the trees here")
            continue
        key = json.dumps([tree.name, g.get("node"), g.get("on"), g.get("observed")], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        draft = draft_branch(tree, g)
        if draft is None:
            skipped.append(f"{tree.name}.{g.get('node')}: {g.get('on')} = {json.dumps(g.get('observed'), default=str)} is covered already, or names no decide node")
            continue
        drafts.append((tree, draft))
    if args.json:
        print(json.dumps({"drafts": [d.to_dict() | {"file": str(t.source)} for t, d in drafts], "skipped": skipped}, indent=2, ensure_ascii=False, default=str))
        return 0 if drafts else 1
    for note in skipped:
        print(f"skipped {note}")
    if not drafts:
        print("nothing to draft" + (": every gap has its branch" if gaps else ": no gaps"))
        return 1
    worst = 0
    for tree, draft in drafts:
        print(f"\n{draft.render()}")
        print(f"# why: {draft.reason}")
        if args.apply:
            if not tree.source:
                print(f"# cannot apply: {tree.name} has no file", file=sys.stderr)
                worst = 1
                continue
            try:
                apply_draft(tree.source, draft)
                print(f"# written into {tree.source}; now edit [nodes.{draft.then}] to say what the value means, then: pyrtan check {tree.source}")
            except TreeError as e:
                print(f"# {e}", file=sys.stderr)
                worst = 1
            reg.reload()
    if not args.apply:
        print("\n# nothing written; --apply puts each draft into its file for you to finish")
    return worst


def _from_report(r: dict[str, Any]) -> dict[str, Any]:
    """A federation report (id namespace/name@version) as a gap (tree name)."""
    if "tree" in r:
        return r
    tree_id = str(r.get("id", ""))
    name = tree_id.split("/", 1)[-1].split("@", 1)[0]
    return {**r, "tree": name}


def cmd_hub(args: argparse.Namespace) -> int:
    from .hub import serve_hub

    follow = list(args.follow)
    join = list(args.join)
    hub, server = serve_hub(
        args.host, args.port, name=args.name, peers=args.peer, poll_every=args.poll, quiet=args.quiet, token=args.token, cors=args.cors,
        federate=args.federate or bool(args.advertise) or bool(follow) or bool(join) or args.listed, advertise=args.advertise, federation_secret=args.federation_secret,
        state=args.state, open_follows=not args.closed, follow=follow, join=join, listed=args.listed,
        trees=[Path(t) for t in args.trees], handout=[Path(t) for t in args.handout], env=_kv(args.env), gaps=args.gaps, egress=args.egress,
        store=args.store, replicas=args.replicas, report_gaps=args.report_gaps, unanswered=args.unanswered, watch=args.watch, chat=args.chat or args.chat_public, chat_typing=args.chat_typing, chat_public=args.chat_public,
    )
    fed = f", federated as {hub.federation.id}" + (f" following {len(hub.federation.following)}" if hub.federation.following else "") + (", listed" if hub.federation.listed else "") if hub.federation else ""
    local = f", holding {len(hub.local.trees)} tree(s)" if hub.local else ""
    if hub.local is not None and hub.local.service.watcher is not None:
        local += f", watching {len(hub.local.service.watcher)}"
    if hub.handout_registry is not None:
        local += f", handing out {len(hub.handout_registry.trees())} tree(s) to trusted members"
    if hub.lobby is not None:
        local += ", chatting at /chat" + (" (open to callers without the token, people only)" if hub.chat_public and args.token else "")
    print(f"pyrtan hub at {server.url}: {len(hub.peers)} peer(s){local}{fed}" + _access_note(args), flush=True)
    try:
        server.wait()
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
        status, body = http_json("GET", f"{args.at.rstrip('/')}/gaps", token=args.token)
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {args.at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
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
        times = f" ×{g['count']}" if g.get("count", 1) > 1 else ""
        print(f"{g['tree']}.{g['node']}: {g['on']} = {json.dumps(g['observed'], ensure_ascii=False)}{times}{peer}")
    return 0


def cmd_reports(args: argparse.Namespace) -> int:
    source = f"{args.at.rstrip('/')}/reports" if args.at else (os.environ.get("PYRTAN_REPORTS") or "https://pyrtan.com/rendezvous/reports.json")
    try:
        if source.startswith(("http://", "https://")):
            status, body = http_json("GET", source)
        else:
            status, body = 200, json.loads(Path(source).read_text(encoding="utf-8"))  # a baked reports.json on disk
    except Exception as e:
        print(f"pyrtan: {source}: {e}", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {source} answered {status}", file=sys.stderr)
        return 3
    reports = [r for r in body.get("reports", []) if isinstance(r, dict)]
    if args.id:
        reports = [r for r in reports if str(r.get("id", "")).split("@")[0] == args.id]
    if args.json:
        print(json.dumps({"source": source, "reports": reports}, indent=2, ensure_ascii=False))
        return 0
    if not reports:
        print(f"no gap reports at {source}" + (f" for {args.id}" if args.id else ""))
        return 0
    for r in reports:
        who = f", {r['reporters']} hub(s)" if r.get("reporters", 1) > 1 else ""
        print(f"{r['id']} {r['node']}: {r['on']} = {json.dumps(r.get('observed'), ensure_ascii=False)}  ×{r.get('count', 1)}{who}")
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    if not args.url:
        if args.network:
            print("pyrtan follow: --network needs a hub URL to follow", file=sys.stderr)
            return 2
        args.invite = "default"
        return cmd_join(args)
    payload = {"url": args.url, "listed": bool(args.listed)}
    if args.network:
        payload["network"] = args.network
    return _hub_post(args, "/follow", payload, lambda b: f"{args.at} now follows {b['following']['url']} ({b['following']['state']})" + (f" in {args.network}" if args.network else "") + "; it routes there once accepted" + (" and is listed in the directory" if b.get("listed") else ""))


def cmd_join(args: argparse.Namespace) -> int:
    def said(b: dict[str, Any]) -> str:
        lines = [f"{args.at}: {b.get('say')}" + (" and listed in the directory" if args.listed else "")]
        if b.get("invite") and not b.get("public"):
            lines.append(f"invite others: pyrtan join {b['invite']}")
        return "\n".join(lines)

    code = _hub_post(args, "/join", {"invite": args.invite, "listed": bool(args.listed)}, said)
    if code == 1 and args.invite == "default":
        tried = bootstrap_candidates()
        print("pyrtan join: no listed or snapshot hub answers" + (f" (tried {', '.join(tried)})" if tried else " (none listed; set $PYRTAN_HUB_LIST or $PYRTAN_RENDEZVOUS)") + "; give an invite or a hub URL", file=sys.stderr)
        return 3
    return code


def cmd_network(args: argparse.Namespace) -> int:
    if args.action in ("new", "leave") and not args.name:
        print(f"pyrtan network {args.action}: give the network's name", file=sys.stderr)
        return 2
    if args.action == "new":
        return _hub_post(args, "/networks", {"name": args.name, "public": bool(args.public)}, lambda b: f"{args.at} is in {b['network']} ({b['id']}), a network of one.\ninvite others: pyrtan join {b['invite']}\nthe invite holds the network's secret: share it only with the hubs you want in it")
    if args.action == "leave":
        return _hub_post(args, "/leave", {"network": args.name}, lambda b: f"{args.at} left {args.name}" if b.get("left") else f"{args.at} was not in {args.name}")
    at = args.at.rstrip("/")
    try:
        status, body = http_json("GET", f"{at}/networks", token=args.token)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} is not a federated hub ({status})", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    nets = body.get("networks", [])
    print(f"{at} is in {len(nets)} network(s)")
    for n in nets:
        print(f"{n['name']:16} {n['id']}  {'public' if n['public'] else 'private'}  following {len(n['following'])}, followed by {len(n['followers'])}")
        print(f"    invite: {n['invite']}")
    return 0


def cmd_keep(args: argparse.Namespace) -> int:
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read())
    except (OSError, json.JSONDecodeError) as e:
        print(f"pyrtan keep: {e}", file=sys.stderr)
        return 2
    return _hub_post(args, "/keep", {"key": args.key, "data": data}, lambda b: f"{args.at} keeps {b['keeping']['key']} ({b['keeping']['size']} bytes); held by {len(b['keeping']['holders'])} of {b['keeping']['wanted']} wanted keeper(s)")


def cmd_kept(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = http_json("GET", f"{at}/keep", token=args.token)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} is not a federated hub ({status})", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    print(f"{at} keeps {len(body['keeping'])} blob(s) of its own, wants {body['replicas']} keeper(s) each; keeps {len(body['kept'])} for {body['store']['actors']} other(s)")
    for k in body["keeping"]:
        print(f"  {k['key']:24} {k['size']:>8} B  held by {len(k['holders'])}/{k['wanted']}" + (f"  ({', '.join(k['holders'])})" if k["holders"] else "  (nobody yet)"))
    for k in body["kept"]:
        print(f"  for {k['actor']}: {k['key']} ({k['size']} B)")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = http_json("POST", f"{at}/fetch", {"key": args.key}, token=args.token)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} answered {status}", file=sys.stderr)
        return 3
    if body.get("status") == "asked":
        print(f"pyrtan fetch: {body.get('say')}", file=sys.stderr)
        return 4
    if body.get("status") != "ok":
        print(f"pyrtan fetch: {body.get('say') or body.get('reason')}", file=sys.stderr)
        return 1
    blob = body["blob"]
    text = json.dumps(blob["data"], indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"{blob['key']} ({blob['size']} bytes, from {body['source']}) -> {args.out}")
    else:
        print(text)
    return 0


def cmd_directory(args: argparse.Namespace) -> int:
    tried: list[str] = []
    candidates = [args.at] if args.at else [os.environ.get("PYRTAN_AT") or "http://127.0.0.1:8700", *bootstrap_candidates()]
    body: Any = None
    at = ""
    for at in candidates:
        at = at.rstrip("/")
        tried.append(at)
        try:
            status, body = http_json("GET", f"{at}/directory", timeout=5.0)
        except Exception:
            body = None
            continue
        if status == 200 and isinstance(body, dict) and "hubs" in body:
            break
        body = None
    if body is None and not args.at:
        # Nobody live: the snapshot pyrtan.com bakes from the hub list, with its age.
        source = os.environ.get("PYRTAN_SNAPSHOT") or "https://pyrtan.com/rendezvous/directory.json"
        try:
            if source.startswith(("http://", "https://")):
                status, snap = http_json("GET", source, timeout=10.0)
            else:
                status, snap = 200, json.loads(Path(source).read_text(encoding="utf-8"))
        except Exception:
            status, snap = 0, None
        if status == 200 and isinstance(snap, dict) and isinstance(snap.get("hubs"), list):
            import time as _time

            now = _time.time()
            for h in snap["hubs"]:
                age = now - float(h.get("seen", 0) or 0)
                h["link"] = "up" if age < 600 else "stale" if age < 86400 else "down"
            body = {**snap, "name": f"snapshot as of {snap.get('as_of', '?')}", "hub": source}
            at = source
    if body is None:
        print(f"pyrtan directory: no federated hub answered (tried {', '.join(tried)})", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    hubs = body.get("hubs", [])
    print(f"directory as {body.get('name') or at} ({body.get('hub', at)}) knows it: {len(hubs)} hub(s)")
    for h in hubs:
        print(f"{h['link']:8} {h['name'] or '-':16} {h['id']}  {', '.join(h.get('trees', []))}")
    print("to follow one: pyrtan follow URL --at YOUR-HUB; to be in it: pyrtan follow --listed, or pyrtan hub --listed")
    return 0


def cmd_unfollow(args: argparse.Namespace) -> int:
    return _hub_post(args, "/unfollow", {"url": args.url}, lambda b: f"{args.at} no longer follows {args.url}" if b.get("removed") else f"{args.at} was not following {args.url}")


def _hub_post(args: argparse.Namespace, path: str, payload: dict[str, Any], said) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = http_json("POST", f"{at}{path}", payload, token=args.token)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status == 404:
        print(f"pyrtan: {at} is not a federated hub; start it with --federate", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict) or body.get("status") != "ok":
        print(f"pyrtan: {at} answered {status}: {(body or {}).get('say') if isinstance(body, dict) else body}", file=sys.stderr)
        return 1
    print(said(body))
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = http_json("GET", f"{at}/peers", token=args.token)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} answered {status}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    for p in body.get("peers", []):
        print(f"{p['link']:8} service  {p['name'] or '-':12} {p['url']}  {', '.join(p.get('trees', []))}")
    for r in body.get("following", []):
        print(f"{r['link']:8} follows  {r['name'] or '-':12} {r['url']}  {', '.join(r.get('trees', []))}" + (f"  ({r['last_error']})" if r.get("last_error") else ""))
    for r in body.get("followers", []):
        print(f"{r['link']:8} follower {r['name'] or '-':12} {r['url']}")
    if not any(body.get(k) for k in ("peers", "following", "followers")):
        print("no peers")
    return 0


def _access_note(args: argparse.Namespace) -> str:
    notes = []
    if getattr(args, "token", None):
        notes.append("token required")
    if getattr(args, "cors", None):
        notes.append("cors: " + ", ".join(args.cors))
    if getattr(args, "egress", "any") != "any":
        notes.append(f"egress: {args.egress}")
    if getattr(args, "federation_secret", None):
        notes.append("follows need the federation secret")
    elif getattr(args, "closed", False):
        notes.append("no follows taken")
    return f" ({'; '.join(notes)})" if notes else ""


# ---------------------------------------------------------------- catalog


def cmd_search(args: argparse.Namespace) -> int:
    cat = Catalog(args.catalog)
    try:
        found = cat.search(" ".join(args.words))
    except CatalogError as e:
        print(f"pyrtan: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps({"catalog": cat.base, "trees": found}, indent=2, ensure_ascii=False))
        return 0
    if not found:
        print(f"nothing in {cat.base} matches {' '.join(args.words)!r}")
        return 1
    for e in found:
        reach = ", ".join(e.get("hosts", []) + [s for s in e.get("sources", []) if s != "http"])
        print(f"{e.get('id', e['name'])}@{e.get('version', '?')}  {e.get('description', '')}" + (f"  [{reach}]" if reach else ""))
        for t in e.get("triggers", [])[:3]:
            print(f"    {t}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    cat = Catalog(args.catalog)
    into = Path(args.dir) if args.dir else default_paths()[0]
    worst = 0
    for spec in args.spec:
        try:
            target = cat.pull(spec, into, force=args.force)
        except CatalogError as e:
            print(f"pyrtan: {e}", file=sys.stderr)
            worst = 1
            continue
        tree = load_tree(target)
        reach = ", ".join(tree.hosts + [s for s in tree.sources() if s != "http"])
        print(f"{tree.id}@{tree.version} -> {target}" + (f"  (reaches: {reach})" if reach else ""))
    return worst


def cmd_index(args: argparse.Namespace) -> int:
    roots = [Path(d) for d in args.dir]
    for root in roots:
        if not root.is_dir():
            print(f"pyrtan: {root}: not a directory", file=sys.stderr)
            return 2
    if len(roots) > 1 and not args.copy:
        print("pyrtan index: more than one directory needs --copy DIR, so the index has one place to point at", file=sys.stderr)
        return 2
    index, problems = build_index(roots, require_license=not args.allow_unlicensed, copy_to=args.copy, require_namespace=args.require_namespace)
    for p in problems:
        print(f"{p}", file=sys.stderr)
    text = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        out = Path(args.out) if args.out else (Path(args.copy) if args.copy else roots[0]) / "index.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        # The same trees as JSON, for the console in a browser (it reads JSON, not TOML).
        bundle = build_bundle(index, Path(args.copy) if args.copy else roots[0])
        (out.parent / "trees.json").write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), default=str) + "\n", encoding="utf-8")
        print(f"wrote {out} and trees.json beside it: {index['count']} tree(s)" + (f", copied to {args.copy}" if args.copy else "") + (f", {len(problems)} left out" if problems else ""))
    return 1 if problems else 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Copy a tree into a checkout's catalog/<namespace>/, write OWNERS for
    a new namespace, run the gate, and say what to do next. The pull
    request is the publishing; this prepares it."""
    from .catalog import publish

    try:
        placed, notes = publish(args.file, args.into, owners=args.owner)
    except CatalogError as e:
        print(f"pyrtan publish: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(n)
    print(f"placed {placed}")
    rel = placed.relative_to(Path(args.into).resolve())
    print("next, from that checkout:")
    print(f"  git checkout -b catalog/{rel.parent.name}-{rel.stem}")
    print(f"  git add {rel.parent}")
    print(f"  git commit -m \"catalog: {rel.parent.name}/{rel.stem}\"")
    print("  git push -u origin HEAD")
    print("then open a pull request; the catalog workflow runs the same gate and a maintainer merges.")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    if args.out == "-":
        sys.stdout.write(PAGE)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE, encoding="utf-8")
    (out.parent / "manifest.webmanifest").write_text(MANIFEST, encoding="utf-8")
    (out.parent / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    (out.parent / "pyrtan.js").write_text(PYRTAN_JS, encoding="utf-8")
    print(f"wrote {out}, manifest.webmanifest, sw.js and pyrtan.js beside it; serve the folder from any static host: there the browser is a hub of its own, with a catalog's trees.json (../catalog/ beside it, else pyrtan.com's), until it joins a hub started with --cors for that host")
    return 0


def cmd_unanswered(args: argparse.Namespace) -> int:
    if not args.at:
        print("pyrtan unanswered: give --at URL of a running service or hub", file=sys.stderr)
        return 2
    try:
        status, body = http_json("GET", f"{args.at.rstrip('/')}/unanswered", token=args.token)
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {args.at} needs a token: pass --token or set $PYRTAN_TOKEN", file=sys.stderr)
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
