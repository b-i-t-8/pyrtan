"""The ``pyrtan`` command.

    pyrtan check  [PATH ...]                 validate trees
    pyrtan list   [PATH ...]                 what is loaded and what it answers to
    pyrtan run    TEXT [--trees PATH]        answer a sentence, in this process
    pyrtan run    --tree NAME --input k=v    run a named tree
    pyrtan match  TEXT [--trees PATH]        how a sentence reads: the rewrites and every trigger's verdict
    pyrtan test   [PATH ...]                 run each tree's [[tests]] against their recordings
    pyrtan diff   FILE [--against REF]       what an edit changes: every test's run, before and after
    pyrtan ask    TEXT --at URL              answer a sentence through a service or hub
    pyrtan chat   [--at HUB] [--bot|--people]  talk to a random stranger or a bot, through a hub started --chat
    pyrtan serve  [PATH ...] [--port N]      host trees as a service
    pyrtan hub    [--peer URL ...]           route between services
    pyrtan hub    --trees DIR                and hold trees itself: a swarm in one process
    pyrtan hub    --federate --advertise URL [--follow URL ...] [--listed]  and between hubs
    pyrtan hub    --chat                     and keep a lobby: strangers and bots, Omegle-style
    pyrtan hub    --lan --join default       and find the hubs on this LAN by multicast, with nobody to ask
    pyrtan hub    --chat-public --chat-reach 8  and link its lobby with other open lobbies, for chat only
    pyrtan follow [URL] --at HUB [--listed]  follow another hub (and route to it); default: join the default network
    pyrtan unfollow URL --at HUB
    pyrtan drop   FOLLOWER --at HUB          let a follower go: a lost phone, an unwanted hub
    pyrtan peers  --at HUB                   services, and hubs followed and following
    pyrtan directory [--at HUB]              every hub that asked to be found, as the swarm knows it
    pyrtan keep KEY [FILE] --at HUB          give your hub a blob to keep, and have its keepers keep it
    pyrtan kept   --at HUB                   what your hub keeps, who holds it, and what it keeps for others
    pyrtan fetch  KEY --at HUB               get a blob back, from your hub or from whoever holds it
    pyrtan gaps   --at URL                   branches waiting to be written
    pyrtan unanswered --at URL [--scaffold DIR | --build DIR]  trees waiting to be written
    pyrtan new    [NAME] [--from TEXT] [--dir PATH]  scaffold a tree file
    pyrtan build  [NAME] --from TEXT [--dir PATH] [--answer k=v ...]  be asked what the tree reads, decides and says, and get the file
    pyrtan search [WORDS] [--catalog URL]    find trees in a catalog
    pyrtan pull   NAME[@VERSION] [--dir]     fetch a tree from a catalog, verified
    pyrtan index  DIR [--out FILE]           build a catalog index from a directory
    pyrtan console --out FILE [--trees DIR]  write the console as one static HTML file, carrying trees
    pyrtan id     new|show|check|device|revoke|route|name   your identity: a key, any domain
    pyrtan id     resolve NAME@DOMAIN        what identity a name is, pinned the first time
    pyrtan allow  WHO --may ask,run --at HUB  grant an identity abilities on your hub; allowed, disallow
    pyrtan names  init DIR --domain D | gate  a network's own names, in any git repository

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
from .chat import clean as clean_line
from .federation import bootstrap_candidates
from .build import BuildError, build
from .grow import Proposal, _quote as shell_quote, propose
from .identity import ABILITIES, KINDS, IdentityError
from .page import MANIFEST, PYRTAN_JS, SERVICE_WORKER, page
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
    _token_arg(p, "the token the hub (--hub) requires, if any", sign=False)

    p = sub.add_parser("test", help="run each tree's [[tests]] against their recordings; exit 1 if any fails")
    p.add_argument("paths", nargs="*")
    p.add_argument("--tree", action="append", default=[], help="only this tree's tests (repeatable)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("diff", help="what an edit to a tree changes: every test either version carries, run on both; exit 1 if any run changed")
    p.add_argument("file", help="the tree file as it is now")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--against", default="HEAD", help="the git revision to compare with (default HEAD)")
    g.add_argument("--old", help="compare with this file instead of a git revision")
    p.add_argument("--trees", action="append", default=[], help="the other trees it delegates to or walks (repeatable; default the file's directory)")
    p.add_argument("--json", action="store_true")

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
    p.add_argument("--suggestions", metavar="FILE", default=os.environ.get("PYRTAN_SUGGESTIONS"), help="keep what agents suggest in this file (JSON lines) across restarts (default $PYRTAN_SUGGESTIONS)")
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
    _token_arg(p, "the hub's token, if any", sign=False)

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
    p.add_argument("--suggestions", nargs="?", const="", metavar="HUB_OR_FILE", help="draft what agents suggested instead of gaps: from a hub's or service's URL (default --at), or a file the console saved")
    p.add_argument("--dir", metavar="DIR", help="with --suggestions, where a suggested new tree is written (default: the first --trees directory)")
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
    p.add_argument("--suggestions", metavar="FILE", default=os.environ.get("PYRTAN_SUGGESTIONS"), help="keep what agents suggest in this file across restarts (default $PYRTAN_SUGGESTIONS)")
    p.add_argument("--watch", action="store_true", default=os.environ.get("PYRTAN_WATCH", "").lower() in ("1", "true", "yes"), help="run the hub's own trees' [[watches]] (default $PYRTAN_WATCH)")
    p.add_argument("--egress", choices=EGRESS_POLICIES, default=os.environ.get("PYRTAN_EGRESS", "any"), help="for the hub's own trees, and for its requests to hosts it did not choose (followed hubs and their followers, the hub list, identity documents; never --peer or a service registered with the token): 'public' refuses private addresses")
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
    p.add_argument("--allow", action="append", default=[a for a in os.environ.get("PYRTAN_ALLOW", "").split() if a], metavar="WHO=ABILITIES", help="grant an identity (pyr:… or name@domain, resolved and pinned now) abilities here: ask, chat, run, operate (repeatable; default $PYRTAN_ALLOW, space-separated)")
    p.add_argument("--names", metavar="DIR", default=os.environ.get("PYRTAN_NAMES"), help="serve the name claims in DIR (a checkout of a names repository, `pyrtan names init`) at /.well-known/pyrtan/, as this network's name provider (default $PYRTAN_NAMES)")
    p.add_argument("--names-domain", metavar="DOMAIN", default=os.environ.get("PYRTAN_NAMES_DOMAIN"), help="the domain the names are for (default $PYRTAN_NAMES_DOMAIN, else this hub's own host); the hub must be reachable at https://DOMAIN")
    p.add_argument("--grants", metavar="FILE", default=os.environ.get("PYRTAN_GRANTS"), help="keep the grants in this file (default $PYRTAN_GRANTS, else grants.json beside --state, else in memory)")
    p.add_argument("--lan", action="store_true", default=os.environ.get("PYRTAN_LAN", "").lower() in ("1", "true", "yes"), help="find hubs on this local network with no one to ask: announce this hub over UDP multicast (TTL 1) and try the hubs heard there when joining a network; needs --advertise with a LAN address (default $PYRTAN_LAN)")
    p.add_argument("--chat-reach", type=int, metavar="N", default=int(os.environ.get("PYRTAN_CHAT_REACH", "0") or 0), help="keep up to N chat links with other open lobbies this hub hears of (LAN, directory), so its strangers can meet theirs: for chat only, never a follow; needs an open lobby (--chat-public, or no token) (default $PYRTAN_CHAT_REACH or 0)")
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

    p = sub.add_parser("drop", help="let a follower go: a lost phone, or a hub you no longer want following yours")
    p.add_argument("follower", help="its id, as `pyrtan peers` shows it")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    _token_arg(p, "your hub's token")

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
    p.add_argument("--build", metavar="DIR", help="be walked through each sentence's tree (what it reads, decides and says) and write the files into DIR")
    p.add_argument("--yes", action="store_true", help="with --build: take every default, asking nothing")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("suggestions", help="what agents beside the swarm suggested: a branch's meaning, a trigger, a tree")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="a running service or hub (default $PYRTAN_AT or the local hub)")
    p.add_argument("--json", action="store_true")
    _token_arg(p, "the token the service or hub requires, if any")

    p = sub.add_parser("new", help="scaffold a tree file")
    p.add_argument("name", nargs="?", help="the tree's name (derived from --from when omitted)")
    p.add_argument("--from", dest="sentence", metavar="TEXT", help="the sentence the tree should answer; numbers and hosts in it become slots")
    p.add_argument("--dir", default=".")

    p = sub.add_parser("build", help="build the tree a sentence asks for, one question at a time")
    p.add_argument("name", nargs="?", help="the tree's name (derived from --from when omitted)")
    p.add_argument("--from", dest="sentence", metavar="TEXT", help="the sentence the tree should answer")
    p.add_argument("--dir", default=".")
    p.add_argument("--answer", action="append", default=[], metavar="KEY=VALUE", help="answer a question ahead of being asked (source=shell, url=https://…, branches=\"200=reply, 404=none\"); repeatable")
    p.add_argument("--yes", action="store_true", help="take the default for every question not answered; nothing is asked")
    p.add_argument("--show", action="store_true", help="print the file instead of writing it")

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

    p = sub.add_parser("names", help="a network's own names, in any git repository: make one, or run its gate")
    nms = p.add_subparsers(dest="names_command", required=True)
    q = nms.add_parser("init", help="write a names repository for DOMAIN: README, names/, and the gate and Pages for its git host")
    q.add_argument("dir", type=Path)
    q.add_argument("--domain", required=True, help="the domain the names are for: name@DOMAIN, served at https://DOMAIN/.well-known/pyrtan/")
    q.add_argument("--host", choices=["github", "gitlab", "plain"], default="github", help="github: Actions (pull_request_target) and Pages; gitlab: .gitlab-ci.yml and Pages; plain: neither (default github)")
    q.add_argument("--pyrtan-ref", default="main", metavar="REF", help="the pyrtan the gate installs (a branch, tag or commit; pin a commit you have read; default main)")
    q = nms.add_parser("gate", help="judge a change to a directory of names owned by their keys: HEAD against BASE")
    q.add_argument("head", type=Path, help="the names directory as the change leaves it")
    q.add_argument("--base", type=Path, help="the names directory the change starts from (its merge base); omit for a first commit")
    q.add_argument("--domain", required=True)

    p = sub.add_parser("allow", help="grant an identity abilities on your hub: ask, chat, run, operate")
    p.add_argument("who", metavar="pyr:…|NAME@DOMAIN", help="an identity, or a name your hub resolves and pins now")
    p.add_argument("--may", required=True, metavar="ABILITIES", help="comma-separated: ask (the routed paths), chat (the lobby), run (be a trusted member), operate (what the token allows)")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("allowed", help="the identities your hub grants anything, and what")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    p.add_argument("--json", action="store_true")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("disallow", help="take back everything your hub grants an identity")
    p.add_argument("who", metavar="pyr:…|NAME@DOMAIN")
    p.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="your hub (default $PYRTAN_AT or the local hub)")
    _token_arg(p, "your hub's token, if any")

    p = sub.add_parser("console", help="write the console as one static HTML file, to host anywhere; it carries the default trees, so it needs no hub and no network to hold them")
    p.add_argument("--out", required=True, help="the file to write ('-' for stdout)")
    p.add_argument("--trees", nargs="+", metavar="DIR", help="the trees the console carries, instead of the default trees (examples and catalog)")

    p = sub.add_parser("id", help="your identity: a key, reachable at any domain, named by any domain (_meta/identity.md)")
    ids = p.add_subparsers(dest="id_command", required=True)
    q = ids.add_parser("new", help="make your root key and identity document")
    q.add_argument("--kind", choices=KINDS, default="person", help="what the identity says it is (default person)")
    q.add_argument("--route", action="append", default=[], metavar="HOST", help="a hub or .onion that takes messages for you (repeatable)")
    q = ids.add_parser("show", help="your identity, its routes and devices")
    q.add_argument("--json", action="store_true", help="print the signed identity document")
    q = ids.add_parser("check", help="check an identity, address or name, or a document (a file, or - for stdin)")
    q.add_argument("thing", help="pyr:…, pyr:…@host, name@domain, or a JSON file")
    q = ids.add_parser("device", help="delegate to a device key: a phone, a browser, another machine")
    q.add_argument("action", choices=["add"])
    q.add_argument("device", metavar="pyr:…", help="the device's identity")
    q.add_argument("--may", default="ask,chat", help=f"what the device may ever be granted, comma-separated from {','.join(ABILITIES)} (default ask,chat)")
    q.add_argument("--label", default="", help="what to call it: phone, laptop")
    q.add_argument("--days", type=int, default=365, help="how long the note lasts, at most 365 (default 365)")
    q = ids.add_parser("revoke", help="revoke a device; hubs drop it when they next read your document")
    q.add_argument("device", metavar="pyr:…")
    q = ids.add_parser("route", help="add or remove a place you can be reached")
    q.add_argument("action", choices=["add", "remove"])
    q.add_argument("host", help="a host[:port] or <56>.onion")
    q = ids.add_parser("name", help="sign a claim that you are name@domain, for the domain to serve")
    q.add_argument("name", metavar="NAME@DOMAIN")
    q.add_argument("--github", type=int, metavar="USER_ID", help="your numeric GitHub user id, for a name at pyrtan.com (with --into, looked up from the name when omitted)")
    where = q.add_mutually_exclusive_group()
    where.add_argument("--out", help="write the claim here (default: stdout)")
    where.add_argument("--into", type=Path, metavar="CHECKOUT", help="put the claim and your identity document into a checkout of a network's names repository (pyrtan names init), or of this repository's site/names/, ready for a pull request")
    q.add_argument("--handover-from", type=Path, metavar="KEY-FILE", help="the key that holds the name now (its root.key): it signs the handover that moves the name to you")
    q = ids.add_parser("release", help="sign that you give up a name, to replace your claim with in the names repository")
    q.add_argument("name", metavar="NAME@DOMAIN")
    q.add_argument("--out", help="write the release here (default: stdout)")
    q = ids.add_parser("resolve", help="what identity a name@domain is, pinned the first time")
    q.add_argument("name", metavar="NAME@DOMAIN")
    q.add_argument("--repin", action="store_true", help="accept a different identity than the one pinned")
    q.add_argument("--json", action="store_true", help="print the claim, and the identity document if the domain serves one")
    q = ids.add_parser("pins", help="the names you have resolved, and the identity each is pinned to")
    q.add_argument("--forget", metavar="NAME@DOMAIN", help="drop a pin, so the next resolve takes what the domain says")
    q = ids.add_parser("backup", help="write your key, encrypted with a passphrase, and your identity document, to keep somewhere only you can read")
    q.add_argument("--out", required=True, help="the file to write (a private place: your disk, your own private repository; never a public one)")
    q.add_argument("--passphrase-env", metavar="VAR", help="read the passphrase from this environment variable instead of asking")
    q = ids.add_parser("restore", help="make this machine your identity again, from a backup (the console's or `pyrtan id backup`'s)")
    q.add_argument("file", help="the backup file")
    q.add_argument("--passphrase-env", metavar="VAR", help="read the passphrase from this environment variable instead of asking")
    q = ids.add_parser("push", help="give a hub your newest identity document, so it knows your devices and revocations now")
    q.add_argument("--at", default=os.environ.get("PYRTAN_AT", "http://127.0.0.1:8700"), help="the hub (default $PYRTAN_AT or the local hub)")
    q = ids.add_parser("wellknown", help="build /.well-known/pyrtan/ from a directory of name claims, for any domain to serve")
    q.add_argument("names", type=Path, metavar="DIR", help="<name>.json claims, and <name>.id.json identity documents")
    q.add_argument("--domain", required=True, help="the domain that will serve them; every claim must be for it")
    q.add_argument("--out", type=Path, required=True, help="the site's root; .well-known/pyrtan/ is written under it")
    for q in ids.choices.values():
        q.add_argument("--dir", type=Path, default=None, help="where your key and document live (default $PYRTAN_IDENTITY or ~/.config/pyrtan/identity)")

    args = parser.parse_args(argv)
    commands = {
        "check": cmd_check, "list": cmd_list, "run": cmd_run, "test": cmd_test, "diff": cmd_diff, "match": cmd_match, "ask": cmd_ask, "chat": cmd_chat, "serve": cmd_serve, "hub": cmd_hub, "gaps": cmd_gaps, "unanswered": cmd_unanswered, "new": cmd_new, "build": cmd_build,
        "watch": cmd_watch, "watches": cmd_watches, "grow": cmd_grow, "suggestions": cmd_suggestions,
        "search": cmd_search, "pull": cmd_pull, "index": cmd_index, "publish": cmd_publish, "console": cmd_console,
        "follow": cmd_follow, "join": cmd_join, "network": cmd_network, "unfollow": cmd_unfollow, "drop": cmd_drop, "peers": cmd_peers, "directory": cmd_directory, "reports": cmd_reports,
        "keep": cmd_keep, "kept": cmd_kept, "fetch": cmd_fetch, "id": cmd_id,
        "allow": cmd_allow, "allowed": cmd_allowed, "disallow": cmd_disallow, "names": cmd_names,
    }
    return commands[args.command](args)


def _token_arg(p: argparse.ArgumentParser, help_text: str, *, sign: bool = True) -> None:
    p.add_argument("--token", default=os.environ.get("PYRTAN_TOKEN"), help=f"{help_text} (default $PYRTAN_TOKEN)")
    if sign:
        p.add_argument("--sign", action="store_true", default=os.environ.get("PYRTAN_SIGN", "").lower() in ("1", "true", "yes"),
                       help="sign requests with your identity's key (pyrtan id new), for a hub that grants it (default $PYRTAN_SIGN)")



def _call(args: argparse.Namespace, method: str, url: str, payload: Any = None, **kw: Any) -> tuple[int, Any]:
    """``http_json`` with the command's token, and with its key's signature under --sign."""
    headers = _key_headers(args, method, url, payload) if getattr(args, "sign", False) else None
    return http_json(method, url, payload, token=getattr(args, "token", None), headers=headers, **kw)


def _key_headers(args: argparse.Namespace, method: str, url: str, payload: Any) -> dict[str, str]:
    """Sign as the root key in $PYRTAN_IDENTITY (or ~/.config/pyrtan/identity), for the
    audience that is the URL being called. Never for one the hub names: a hub that
    said "sign for that other hub" would collect signatures good there."""
    from urllib.parse import urlsplit
    from . import identity
    audience = identity.audience_of(url)
    if audience is None:
        raise SystemExit(f"pyrtan: {url} is not an http(s) URL to sign for")
    key = identity.Key.load(identity.home() / "root.key")
    if method == "POST" and payload is None:
        payload = {}
    return identity.sign_request(key, audience, method, urlsplit(url).path or "/", payload)


def _access_args(p: argparse.ArgumentParser) -> None:
    _token_arg(p, "require this bearer token on every request but /health and the console", sign=False)
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


def cmd_test(args: argparse.Namespace) -> int:
    """Run every tree's own tests. Each answers from its recording, with its
    clock, so a pass means the same path as when it was written."""
    from .cases import cases, failures, run_case

    reg = Registry.from_paths(_paths(args.paths))
    for path, err in reg.errors.items():
        print(f"pyrtan: {path}: {err}", file=sys.stderr)
    trees = [t for t in reg.trees() if t.tests and (not args.tree or t.name in args.tree)]
    if not trees:
        print("pyrtan test: no tree here carries [[tests]]", file=sys.stderr)
        return 2
    report, failed, total = [], 0, 0
    for tree in trees:
        if not args.json:
            print(f"{tree.name}: {len(tree.tests)} test(s)")
        for case in cases(tree):
            total += 1
            outcome = run_case(case, tree, reg)
            missed = failures(case, outcome)
            failed += bool(missed)
            report.append({"tree": tree.name, "test": case.name, "ok": not missed, "failures": missed, "outcome": outcome.to_dict(with_trace=False)})
            if not args.json:
                print(f"  {'ok  ' if not missed else 'FAIL'}  {case.name}")
                for m in missed:
                    print(f"        {m}")
    if args.json:
        print(json.dumps({"passed": total - failed, "failed": failed, "tests": report}, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"{total - failed}/{total} passed")
    return 1 if failed or reg.errors else 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Run every test the file and its other version carry on both, against
    the same recordings, and say which runs changed. Expectations do not
    matter here; behaviour does."""
    import subprocess

    from .cases import diff
    from .tree import load_tree_text

    path = Path(args.file)
    try:
        new = load_tree(path)
    except TreeError as e:
        print(f"pyrtan: {e}", file=sys.stderr)
        return 2
    if args.old:
        label = args.old
        try:
            old = load_tree(args.old)
        except TreeError as e:
            print(f"pyrtan: {e}", file=sys.stderr)
            return 2
    else:
        label = f"{args.against}:{path}"
        where = path.resolve().parent
        try:
            top = subprocess.run(["git", "-C", str(where), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
            rel = path.resolve().relative_to(Path(top).resolve()).as_posix()
            text = subprocess.run(["git", "-C", top, "show", f"{args.against}:{rel}"], capture_output=True, text=True, check=True).stdout
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", "") or str(e)
            print(f"pyrtan diff: cannot read {label} from git: {detail.strip()}", file=sys.stderr)
            return 2
        try:
            old = load_tree_text(text, source=str(path))
        except TreeError as e:
            print(f"pyrtan diff: {label}: {e}", file=sys.stderr)
            return 2
    others = Registry.from_paths(_paths(args.trees) if args.trees else [path.parent])
    result = diff(old, new, others.trees())
    if args.json:
        print(json.dumps({"against": label, **result.to_dict()}, indent=2, ensure_ascii=False, default=str))
        return 1 if result.changed else 0
    total = len(result.same) + len(result.changed)
    if not total:
        print(f"{new.name}: neither version carries [[tests]], so there is nothing to compare")
        return 0
    for c in result.changed:
        print(c.render())
    print(f"{new.name}: {len(result.changed)} of {total} test run(s) changed against {label}")
    return 1 if result.changed else 0


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
        status, body = _call(args, "POST", f"{at}/intent", {"text": args.text, "trace": not args.no_trace})
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} answered {status}: {body}", file=sys.stderr)
        return 3
    outcome = outcome_from_dict(body)

    def remote_resume(snapshot: dict[str, Any], answer: Any) -> Outcome:
        st, bd = _call(args, "POST", f"{at}/resume", {"snapshot": snapshot, "answer": answer, "trace": not args.no_trace})
        return outcome_from_dict(bd) if isinstance(bd, dict) else Outcome("error", "", reason="remote", say=f"{at} answered {st}")

    outcome = _answer_questions(outcome, remote_resume)
    return _report(outcome, args.json, not args.no_trace)


def cmd_chat(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    want = "bot" if args.bot else "people" if args.people else "anyone"

    def post(path: str, body: dict[str, Any], timeout: float = 15.0) -> tuple[int, Any]:
        return _call(args, "POST", f"{at}{path}", body, timeout=timeout)

    try:
        status, body = post("/chat/start", {"want": want, "interests": args.interests, "typing_indicator": not args.no_typing})
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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
                text = clean_line(str(e.get("text", "")))  # whatever a hub sends, no line steers this terminal
                if e["kind"] == "system":
                    print(f"  {text}", flush=True)
                elif e["kind"] == "bot":
                    print(f"Bot: {text}", flush=True)
                    if args.trace and isinstance(e.get("trace"), dict):
                        print(outcome_from_dict({**e, "say": e["text"]}).trace.render(), flush=True)
                    state["owed"] = max(0, state["owed"] - 1)
                elif e["kind"] == "stranger":
                    print(f"Stranger: {text}", flush=True)
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
            if outcome.reason == "no-match" and isinstance(outcome.output, dict) and isinstance(outcome.output.get("unanswered"), str):
                print(f"Or be asked what it reads, decides and says: pyrtan build --from {shell_quote(outcome.output['unanswered'])}")
    return {"done": 0, "failed": 1, "asked": 4}.get(outcome.status, 3)


def cmd_serve(args: argparse.Namespace) -> int:
    paths = _paths(args.paths)
    name = args.name or (paths[0].resolve().name if paths else "pyrtan")
    service, server = serve(paths, args.host, args.port, name=name, hub=args.hub, env=_kv(args.env), advertise=args.advertise, quiet=args.quiet, token=args.token, cors=args.cors, gaps=args.gaps, egress=args.egress, unanswered=args.unanswered, watch=args.watch, suggestions=args.suggestions)
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
        status, body = _call(args, "GET", f"{args.at.rstrip('/')}/watches")
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {args.at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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

    if args.suggestions is not None:
        return _grow_suggestions(args)
    gaps: list[dict[str, Any]] = []
    if args.at:
        try:
            status, body = _call(args, "GET", f"{args.at.rstrip('/')}/gaps")
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
            skipped.append(f"{tree.name}.{g.get('node')}: {g.get('on')} = {json.dumps(g.get('observed'), default=str)} is covered already, or names no decide or lookup node")
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
                what = f"the new row's value in [nodes.{draft.node}]" if draft.list_key == "table" else f"[nodes.{draft.then}]"
                print(f"# written into {tree.source}; now edit {what} to say what the value means, then: pyrtan check {tree.source}")
            except TreeError as e:
                print(f"# {e}", file=sys.stderr)
                worst = 1
            reg.reload()
    if not args.apply:
        print("\n# nothing written; --apply puts each draft into its file for you to finish")
    return worst


def _load_suggestions(args: argparse.Namespace, source: str) -> list[dict[str, Any]] | None:
    """Suggestions from a running service or hub, or a file: a /suggestions
    answer, a list, or JSON lines."""
    if source.startswith(("http://", "https://")):
        try:
            status, body = _call(args, "GET", f"{source.rstrip('/')}/suggestions")
        except Exception as e:
            print(f"pyrtan: {source}: {e}", file=sys.stderr)
            return None
        if status != 200 or not isinstance(body, dict):
            print(f"pyrtan: {source} answered {status}" + (": pass --token, or --sign as an identity it grants operate" if status == 401 else ""), file=sys.stderr)
            return None
        return [s for s in body.get("suggestions", []) if isinstance(s, dict)]
    text = Path(source).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        rows = data.get("suggestions", [data]) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    return [s for s in rows if isinstance(s, dict)]


def cmd_suggestions(args: argparse.Namespace) -> int:
    rows = _load_suggestions(args, args.at)
    if rows is None:
        return 3
    if args.json:
        print(json.dumps({"suggestions": rows}, indent=2, ensure_ascii=False, default=str))
        return 0
    if not rows:
        print("no suggestions: no agent has reported to this Pyrtan yet")
        return 0
    for s in rows:
        by = s.get("by") or {}
        who = by.get("agent", "an agent") + (f" ({by['model']})" if by.get("model") else "")
        count = f"  x{s.get('count', 1)}"
        if s.get("kind") == "branch":
            m = s.get("meaning") or {}
            print(f"branch  {s.get('tree')}.{s.get('node')}: {s.get('on')} = {json.dumps(s.get('observed'), ensure_ascii=False, default=str)} means {m.get('kind')}: {m.get('say')!r}{count}")
        elif s.get("kind") == "trigger":
            print(f"trigger {s.get('tree')}: {s.get('pattern')!r}, covering {', '.join(repr(c) for c in s.get('covers', []))}{count}")
        else:
            print(f"tree    {s.get('text')!r}: {s.get('description') or 'no description'}{count}")
        print(f"        by {who}" + (f": {s['why']}" if s.get("why") else ""))
    print("\nnow: pyrtan grow --suggestions " + args.at + " --trees DIR  (add --apply to write them)")
    return 0


def _grow_suggestions(args: argparse.Namespace) -> int:
    """``pyrtan grow --suggestions``: each suggestion drafted by the rules
    and checked, nothing trusted because an agent said it."""
    from .grow import apply_draft
    from . import suggestions as sg

    say = (lambda *a, **k: None) if args.json else print
    source = args.suggestions or args.at
    if not source:
        print("pyrtan grow --suggestions: say where they are: a hub's URL, a file, or --at URL", file=sys.stderr)
        return 2
    rows = _load_suggestions(args, source)
    if rows is None:
        return 3
    if args.tree:
        rows = [s for s in rows if s.get("tree") in args.tree]  # a new tree has none, so --tree leaves it out
    reg = Registry.from_paths(_paths(args.trees))
    for path, err in reg.errors.items():
        print(f"pyrtan: {path}: {err}", file=sys.stderr)
    out_dir = Path(args.dir) if args.dir else next((Path(t) for t in _paths(args.trees) if Path(t).is_dir()), Path("trees"))
    worst, done, report = 0, 0, []
    for s in rows:
        why = sg.problem(s)
        label = f"{s.get('kind')} {s.get('tree') or s.get('text')!r}"
        if why:
            say(f"refused {label}: {why}")
            report.append({"suggestion": s, "refused": why})
            continue
        kind = s["kind"]
        if kind == "tree":
            name, text = sg.tree_text(s)
            if reg.get(name):
                say(f"skipped {label}: a tree named {name} is here already")
                continue
            say(f"\n# a new tree, {out_dir / (name + '.toml')}:\n{text}")
            if args.apply:
                try:
                    path = sg.write_tree(out_dir, s)
                    say(f"# written: {path}; now say what it observes and how it decides, then: pyrtan check {path}")
                    done += 1
                except TreeError as e:
                    print(f"# {e}", file=sys.stderr)
                    worst = 1
            report.append({"suggestion": s, "file": str(out_dir / (name + ".toml")), "text": text})
            continue
        tree = reg.get(s["tree"])
        if tree is None:
            say(f"skipped {label}: not among the trees here")
            continue
        if kind == "branch":
            d = sg.draft(tree, s)
            if d is None:
                say(f"skipped {label}.{s.get('node')}: {s.get('on')} = {json.dumps(s.get('observed'), default=str)} is covered already, or names no decide node")
                continue
            say(f"\n{d.render()}")
            say(f"# why: {d.reason}")
            report.append({"suggestion": s, "file": str(tree.source), "draft": d.to_dict()})
            if args.apply:
                try:
                    apply_draft(tree.source, d)
                    say(f"# written into {tree.source}; read it, then: pyrtan check {tree.source}")
                    done += 1
                except TreeError as e:
                    print(f"# {e}", file=sys.stderr)
                    worst = 1
        else:
            say(f"\n# {tree.name}: add after its last [[triggers]]:\n{sg.trigger_text(s)}")
            report.append({"suggestion": s, "file": str(tree.source), "text": sg.trigger_text(s)})
            if args.apply:
                try:
                    sg.apply_trigger(tree.source, tree, s, reg)
                    say(f"# written into {tree.source}")
                    done += 1
                except TreeError as e:
                    print(f"# {e}", file=sys.stderr)
                    worst = 1
        reg.reload()
    if args.json:
        print(json.dumps({"drafts": report}, indent=2, ensure_ascii=False, default=str))
    if not rows:
        say("nothing to draft: no suggestions")
        return 1
    if not args.apply:
        say("\n# nothing written; --apply puts each into its file, checked, for you to read")
    elif done:
        say(f"\n# {done} written; bump each tree's version when you keep it")
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
    from .identity import IdentityError

    follow = list(args.follow)
    join = list(args.join)
    try:
        hub, server = serve_hub(
            args.host, args.port, name=args.name, peers=args.peer, poll_every=args.poll, quiet=args.quiet, token=args.token, cors=args.cors,
            federate=args.federate or bool(args.advertise) or bool(follow) or bool(join) or args.listed or args.lan or args.chat_reach > 0, advertise=args.advertise, federation_secret=args.federation_secret,
            state=args.state, open_follows=not args.closed, follow=follow, join=join, listed=args.listed,
            trees=[Path(t) for t in args.trees], handout=[Path(t) for t in args.handout], env=_kv(args.env), gaps=args.gaps, egress=args.egress,
            store=args.store, replicas=args.replicas, report_gaps=args.report_gaps, unanswered=args.unanswered, suggestions=args.suggestions, watch=args.watch, chat=args.chat or args.chat_public, chat_typing=args.chat_typing, chat_public=args.chat_public, lan=args.lan, chat_reach=args.chat_reach,
            allow=args.allow, grants=args.grants, names=args.names, names_domain=args.names_domain,
        )
    except IdentityError as e:
        print(f"pyrtan hub: {e}", file=sys.stderr)
        return 1
    fed = f", federated as {hub.federation.id}" + (f" following {len(hub.federation.following)}" if hub.federation.following else "") + (", listed" if hub.federation.listed else "") if hub.federation else ""
    local = f", holding {len(hub.local.trees)} tree(s)" if hub.local else ""
    if hub.local is not None and hub.local.service.watcher is not None:
        local += f", watching {len(hub.local.service.watcher)}"
    if hub.handout_registry is not None:
        local += f", handing out {len(hub.handout_registry.trees())} tree(s) to trusted members"
    if hub.lobby is not None:
        local += ", chatting at /chat" + (" (open to callers without the token, people only)" if hub.chat_public and args.token else "")
        if hub.lobby.reach:
            local += f", linking with up to {hub.lobby.reach} other open lobbies" if hub.lobby.open() else ", but --chat-reach links only an open lobby (--chat-public, or no token)"
    if hub.lan is not None:
        lan = hub.lan.view()
        local += f", on the LAN ({lan['group']}): " + (f"cannot join it: {hub.lan.error}" if hub.lan.error else f"heard {len(lan['hubs'])} hub(s)" + ("" if lan["announcing"] else "; listening only, since this hub's id is not a LAN address (give --advertise http://<its LAN address>:PORT)"))
    if len(hub.grants):
        local += f", granting {len(hub.grants)} identit{'y' if len(hub.grants) == 1 else 'ies'}"
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
        status, body = _call(args, "GET", f"{args.at.rstrip('/')}/gaps")
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {args.at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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
        status, body = _call(args, "GET", f"{at}/networks")
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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
        status, body = _call(args, "GET", f"{at}/keep")
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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
        status, body = _call(args, "POST", f"{at}/fetch", {"key": args.key})
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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


def cmd_drop(args: argparse.Namespace) -> int:
    return _hub_post(args, "/drop", {"follower": args.follower}, lambda b: f"{args.at} let {args.follower} go: its mailbox, what it was handed and what was kept for it are gone" if b.get("dropped") else f"{args.follower} does not follow {args.at}")


def cmd_unfollow(args: argparse.Namespace) -> int:
    return _hub_post(args, "/unfollow", {"url": args.url}, lambda b: f"{args.at} no longer follows {args.url}" if b.get("removed") else f"{args.at} was not following {args.url}")


def _hub_post(args: argparse.Namespace, path: str, payload: dict[str, Any], said) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = _call(args, "POST", f"{at}{path}", payload)
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        said_401 = body.get("say") if isinstance(body, dict) and getattr(args, "sign", False) else None
        print(f"pyrtan: {at}: {said_401}" if said_401 else f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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
        status, body = _call(args, "GET", f"{at}/peers")
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
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
        kind = "member  " if r.get("kind") == "member" else "follower"
        print(f"{r['link']:8} {kind} {r['name'] or '-':12} {r['url']}" + ("  (trusted)" if r.get("trusted") else ""))
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
    print("then open a pull request; the catalog-gate workflow runs the same gate (main's copy) and a maintainer merges.")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    from .defaults import bundle

    if args.trees:
        reg = Registry.from_paths(args.trees)
        for path, err in sorted(reg.errors.items()):
            print(f"pyrtan console: {path}: {err} (left out)", file=sys.stderr)
    carried = bundle(args.trees)
    html = page(carried)
    if args.out == "-":
        sys.stdout.write(html)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    (out.parent / "manifest.webmanifest").write_text(MANIFEST, encoding="utf-8")
    (out.parent / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    (out.parent / "pyrtan.js").write_text(PYRTAN_JS, encoding="utf-8")
    print(f"wrote {out}, manifest.webmanifest, sw.js and pyrtan.js beside it, carrying {carried['count']} trees; serve the folder from any static host, or open the file: there the browser is a hub of its own, holding those trees (or a catalog's trees.json beside it, ../catalog/, when there is one), until it joins a hub started with --cors for that host")
    return 0


def cmd_unanswered(args: argparse.Namespace) -> int:
    if not args.at:
        print("pyrtan unanswered: give --at URL of a running service or hub", file=sys.stderr)
        return 2
    try:
        status, body = _call(args, "GET", f"{args.at.rstrip('/')}/unanswered")
    except Exception as e:
        print(f"pyrtan: {args.at}: {e}", file=sys.stderr)
        return 3
    if status == 401:
        print(f"pyrtan: {args.at} needs a token: pass --token or set $PYRTAN_TOKEN, or --sign as an identity it grants", file=sys.stderr)
        return 3
    entries = (body or {}).get("unanswered", []) if isinstance(body, dict) else []
    if args.json:
        print(json.dumps({"unanswered": entries}, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print("nothing unanswered: every sentence so far found a tree")
        return 0
    if args.build:
        if args.scaffold:
            print("pyrtan unanswered: --scaffold writes skeletons and --build asks; give one", file=sys.stderr)
            return 2
        worst = 0
        for e in entries:
            peer = f" ({e['peer']})" if e.get("peer") else ""
            print(f"{e['text']!r}  x{e.get('count', 1)}{peer}")
            code = _build_one(e["text"], None, Path(args.build), {}, ask=not args.yes, show=False)
            worst = max(worst, code)
            print()
        print(f"now: pyrtan check {args.build}")
        return worst
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
    else:
        print("to be asked what one reads, decides and says, and get its file: pyrtan build --from \"…\"; for all of them: --build DIR")
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


def cmd_build(args: argparse.Namespace) -> int:
    if not args.name and not args.sentence:
        print("pyrtan build: give --from \"the sentence it should answer\" (and a NAME, if the one drawn from the words will not do)", file=sys.stderr)
        return 2
    answers: dict[str, Any] = {}
    for item in args.answer:
        if "=" not in item:
            print(f"pyrtan build: --answer takes KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        answers[key.strip()] = value
    text = args.sentence or args.name.replace("-", " ")
    return _build_one(text, args.name, Path(args.dir), answers, ask=not args.yes, show=args.show)


def _build_one(text: str, name: str | None, directory: Path, answers: dict[str, Any], *, ask: bool, show: bool) -> int:
    """Walk the builder's questions for one sentence on the terminal, then
    write the file (or print it). With no terminal, or ``ask`` off, every
    question not answered takes its default."""
    interactive = ask and sys.stdin.isatty()
    while True:
        try:
            built = build(text, answers, name)
        except BuildError as e:
            if not interactive or e.key not in answers:
                print(f"pyrtan build: {e.key}: {e}", file=sys.stderr)
                return 2
            print(f"    that will not do: {e}")
            del answers[e.key]
            continue
        pending = [q for q in built.questions if q.key not in answers]
        if not pending or not interactive:
            break
        q = pending[0]
        if q.help:
            print(f"  {q.help}")
        if q.choices:
            for value, label in q.choices.items():
                print(f"    {value:<10} {label}")
        try:
            raw = input(f"  {q.prompt} [{q.default}]: ")
        except EOFError:
            print()
            interactive = False
            continue
        answers[q.key] = raw.strip() or q.default
    for prob in built.problems:
        print(f"  {prob['level']}: {prob['where']}: {prob['message']}")
    if show:
        print(built.toml, end="")
        return 0 if built.ok else 1
    path = directory / f"{built.name}.toml"
    if path.exists():
        print(f"pyrtan: {path} exists; nothing written", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(built.toml, encoding="utf-8")
    print(f"wrote {path}: {built.name!r} answering {built.answers['trigger']!r}" + ("" if built.ok else " (it does not pass check yet; see above)"))
    print(f"now: pyrtan check {path}, then: pyrtan run {shell_quote(text)} --trees {shell_quote(str(directory))}")
    return 0 if built.ok else 1


# ---------------------------------------------------------------- pyrtan id


def cmd_id(args: argparse.Namespace) -> int:
    from . import identity
    directory = args.dir or identity.home()
    try:
        return {
            "new": _id_new, "show": _id_show, "check": _id_check, "device": _id_device,
            "revoke": _id_revoke, "route": _id_route, "name": _id_name,
            "resolve": _id_resolve, "pins": _id_pins, "wellknown": _id_wellknown, "push": _id_push,
            "backup": _id_backup, "restore": _id_restore, "release": _id_release,
        }[args.id_command](args, directory)
    except IdentityError as e:
        print(f"pyrtan id: {e}", file=sys.stderr)
        return 1


def _id_load(directory: Path):
    """Your root key and your current identity document."""
    from . import identity
    key_file, doc_file = directory / "root.key", directory / "identity.json"
    if not key_file.exists():
        raise IdentityError(f"no identity in {directory}: make one with `pyrtan id new`")
    key = identity.Key.load(key_file)
    try:
        doc = json.loads(doc_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise IdentityError(f"{doc_file}: {e}") from None
    problem = identity.check_identity(doc, ident=key.id)
    if problem:
        raise IdentityError(f"{doc_file}: {problem}")
    return key, doc


def _id_save(directory: Path, doc: dict[str, Any]) -> None:
    path = directory / "identity.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _id_new(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    key_file = directory / "root.key"
    if key_file.exists():
        print(f"pyrtan id: {key_file} exists; an identity is not replaced (move it away first, knowing that it is you)", file=sys.stderr)
        return 1
    key = identity.Key.generate()
    doc = identity.make_identity(key, kind=args.kind, routes=args.route)
    key.save(key_file)
    _id_save(directory, doc)
    print(f"you are {key.id}")
    print(f"the key is {key_file} (readable only by you). It is the identity: lose it and nobody can give it back,")
    print("copy it and whoever has the copy is you. Back it up somewhere offline.")
    print(f"the document hubs and name providers hold is {directory / 'identity.json'}")
    return 0


def _id_show(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    _, doc = _id_load(directory)
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    print(f"{doc['id']}  ({doc['kind']}, updated {time.strftime('%Y-%m-%d %H:%M', time.localtime(doc['updated']))})")
    print("routes: " + (", ".join(doc["routes"]) if doc["routes"] else "none (add one with `pyrtan id route add HOST`)"))
    print(f"devices: {len(doc['devices'])}" + ("" if doc["devices"] else " (add one with `pyrtan id device add pyr:…`)"))
    for note in doc["devices"]:
        status = identity.device_status(doc, note["device"])
        until = time.strftime("%Y-%m-%d", time.localtime(note["expires"]))
        print(f"  {note['device']}  {note['label'] or '-':10} may {','.join(note['may']) or 'nothing'}  {status} until {until}")
    for device in doc["revoked"]:
        print(f"  {device}  revoked")
    return 0


def _id_check(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    thing = args.thing
    if thing != "-" and not Path(thing).is_file():
        parsed = identity.parse(thing)
        if parsed.form == "name":
            print(f"{parsed}: a well-formed name; `pyrtan id resolve {parsed}` asks {parsed.domain} who it is")
        else:
            print(f"{parsed}: a well-formed {parsed.form}, key {identity.decode(parsed.id).hex()}")
        return 0
    try:
        doc = json.loads(sys.stdin.read() if thing == "-" else Path(thing).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise IdentityError(f"{thing}: {e}") from None
    kind, problem = identity.check_any(doc)
    if problem:
        print(f"{thing}: {kind}: refused: {problem}")
        return 1
    if kind == "identity":
        print(f"{thing}: the identity document of {doc['id']}, signed by it; {len(doc['devices'])} device(s), {len(doc['revoked'])} revoked")
    elif kind == "device":
        print(f"{thing}: {doc['device']} may {','.join(doc['may']) or 'nothing'} for {doc['root']}, until {time.strftime('%Y-%m-%d', time.localtime(doc['expires']))}")
    elif kind == "release":
        print(f"{thing}: {doc['id']} gives up {doc['name']}@{doc['domain']}, signed by it")
    elif kind == "backup":
        print(f"{thing}: a key backup of {doc['id']}, {doc['kdf']['iterations']} PBKDF2 rounds" + (", with its identity document" if doc.get("document") else "") + "; `pyrtan id restore` opens it")
    else:
        print(f"{thing}: {doc['id']} claims {doc['name']}@{doc['domain']}, signed by it")
    return 0


def _id_device(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    if not 0 < args.days <= 365:
        raise IdentityError("--days is from 1 to 365")
    key, doc = _id_load(directory)
    may = [a for a in args.may.replace(" ", "").split(",") if a]
    issued = int(time.time())
    note = identity.make_device(key, args.device, may, label=args.label, issued=issued, expires=issued + args.days * 86400)
    _id_save(directory, identity.with_device(doc, key, note))
    print(json.dumps(note, ensure_ascii=False))
    print(f"{note['device']} may {','.join(note['may']) or 'nothing'} for you until {time.strftime('%Y-%m-%d', time.localtime(note['expires']))}; give the device the note above", file=sys.stderr)
    return 0


def _id_revoke(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    key, doc = _id_load(directory)
    device = identity.normal(args.device)
    _id_save(directory, identity.with_revoked(doc, key, device))
    print(f"revoked {device}; hubs drop it when they next read your document")
    return 0


def _id_route(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    key, doc = _id_load(directory)
    host = identity.check_locator(args.host)
    routes = [r for r in doc["routes"] if r != host] + ([host] if args.action == "add" else [])
    _id_save(directory, identity.with_routes(doc, key, routes))
    print("routes: " + (", ".join(routes) or "none"))
    return 0


def _id_name(args: argparse.Namespace, directory: Path) -> int:
    from . import identity, names
    key, doc = _id_load(directory)
    network = names.repo_domain(args.into) if args.into else None
    github = args.github
    if args.into and network is None and github is None:
        local = identity.parse(args.name).name
        github = names.github_id(local)
        print(f"GitHub says {local} is user {github}", file=sys.stderr)
    extra = {"github": github} if github is not None else {}
    old = identity.Key.load(args.handover_from) if args.handover_from else None
    claim = identity.make_name(key, args.name, handover_from=old, **extra)
    text = json.dumps(claim, indent=2, ensure_ascii=False) + "\n"
    if args.into:
        written = names.place(args.into, claim, doc)
        problems = names.gate_key(args.into / "names", None, network) if network else names.gate(args.into)
        for p in problems:
            print(f"gate: {p}", file=sys.stderr)
        if problems:
            return 1
        print("wrote " + " and ".join(str(w) for w in written))
        where = "names" if network else names.NAMES_DIR
        print(f"now: git checkout -b names/{claim['name']} && git add {where} && git commit && git push -u origin HEAD, and open a pull request"
              + ("" if network else f" from the GitHub account {claim['name']}"))
        return 0
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {claim['id']} claims {claim['name']}@{claim['domain']}; the domain serves it at /.well-known/pyrtan/name/{claim['name']}.json")
    else:
        sys.stdout.write(text)
    return 0


def _id_release(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    key, _ = _id_load(directory)
    release = identity.make_release(key, args.name)
    text = json.dumps(release, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {key.id} gives up {release['name']}@{release['domain']}; put it in place of names/{release['name']}.json")
    else:
        sys.stdout.write(text)
    return 0


def _id_resolve(args: argparse.Namespace, directory: Path) -> int:
    from . import identity, names
    claim = names.resolve(args.name)
    name, ident = f"{claim['name']}@{claim['domain']}", claim["id"]
    pins = names.Pins(directory / "pins.json")
    verdict, pinned = pins.compare(name, ident), pins.get(name)
    if verdict == "changed" and not args.repin:
        print(f"pyrtan id: {name} now says it is {ident}", file=sys.stderr)
        print(f"           but it was pinned to {pinned['id']} on {time.strftime('%Y-%m-%d', time.localtime(pinned['first']))}", file=sys.stderr)
        print("           the domain changed its answer, or someone is answering for it. If you know why, pyrtan id resolve --repin", file=sys.stderr)
        return 1
    pins.pin(name, ident)
    pins.save()
    doc = names.fetch_identity(ident, claim["domain"])
    if args.json:
        print(json.dumps({"claim": claim, "identity": doc}, indent=2, ensure_ascii=False))
        return 0
    since = {"new": "pinned now", "same": f"as pinned since {time.strftime('%Y-%m-%d', time.localtime(pinned['first'])) if pinned else ''}", "changed": "re-pinned"}[verdict]
    print(f"{name} is {ident} ({since})")
    if doc:
        print("routes: " + (", ".join(doc["routes"]) or "none") + f"; {len(doc['devices'])} device(s), {len(doc['revoked'])} revoked")
    return 0


def _id_pins(args: argparse.Namespace, directory: Path) -> int:
    from . import names
    pins = names.Pins(directory / "pins.json")
    if args.forget:
        if not pins.forget(args.forget):
            print(f"pyrtan id: {args.forget} is not pinned", file=sys.stderr)
            return 1
        pins.save()
        print(f"forgot {args.forget}; the next resolve takes what its domain says")
        return 0
    if not pins.names:
        print("no pins yet: pyrtan id resolve NAME@DOMAIN")
    for name, pin in sorted(pins.names.items()):
        print(f"{name:32} {pin['id']}  since {time.strftime('%Y-%m-%d', time.localtime(pin['first']))}")
    return 0


def _id_wellknown(args: argparse.Namespace, directory: Path) -> int:
    from . import identity, names
    domain = identity.check_host(args.domain.lower())
    count, problems = names.build_wellknown(args.names, args.out, domain)
    for p in problems:
        print(f"wellknown: {p} (left out)", file=sys.stderr)
    print(f"wrote {count} name(s) for {domain} under {args.out / names.WELL_KNOWN}")
    return 1 if problems else 0


def _passphrase(args: argparse.Namespace, *, twice: bool) -> str:
    import getpass
    if args.passphrase_env:
        value = os.environ.get(args.passphrase_env)
        if not value:
            raise IdentityError(f"${args.passphrase_env} is empty")
        return value
    first = getpass.getpass("passphrase: ")
    if twice and getpass.getpass("again: ") != first:
        raise IdentityError("the two passphrases differ")
    return first


def _id_backup(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    key, doc = _id_load(directory)
    out = Path(args.out)
    if out.exists():
        raise IdentityError(f"{out} exists; a backup is not overwritten")
    backup = identity.make_backup(key, _passphrase(args, twice=True), document=doc)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(backup, indent=2) + "\n")
    print(f"wrote {out}: {key.id}, encrypted with your passphrase, and your identity document")
    print("keep it where only you can read it (your disk, your own private repository), never in a public one: anyone with a copy can guess at the passphrase forever")
    return 0


def _id_restore(args: argparse.Namespace, directory: Path) -> int:
    from . import identity
    try:
        backup = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise IdentityError(f"{args.file}: {e}") from None
    problem = identity.backup_problem(backup)
    if problem:
        raise IdentityError(f"{args.file}: {problem}")
    key_file = directory / "root.key"
    if key_file.exists():
        held = identity.Key.load(key_file)
        if held.id != backup["id"].lower():
            raise IdentityError(f"{directory} already holds {held.id}; move it away first, knowing that it is an identity")
    key = identity.open_backup(backup, _passphrase(args, twice=False))
    doc = backup.get("document") or identity.make_identity(key)
    current = None
    if (directory / "identity.json").exists():
        try:
            current = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
    doc = identity.newest([doc, current], ident=key.id) or doc
    if not key_file.exists():
        key.save(key_file)
    _id_save(directory, doc)
    print(f"you are {key.id} again, in {directory}" + ("" if backup.get("document") else "; the backup held no identity document, so a new one lists no devices"))
    return 0


def _id_push(args: argparse.Namespace, directory: Path) -> int:
    _, doc = _id_load(directory)
    at = args.at.rstrip("/")
    try:
        status, body = http_json("POST", f"{at}/identity", {"document": doc}, timeout=15.0)
    except Exception as e:
        print(f"pyrtan id: {at}: {e}", file=sys.stderr)
        return 3
    if status != 200:
        print(f"pyrtan id: {at} refused the document: {(body or {}).get('say') if isinstance(body, dict) else status}", file=sys.stderr)
        return 1
    print(f"{at} holds your document of {time.strftime('%Y-%m-%d %H:%M', time.localtime(doc['updated']))}: {len(doc['devices'])} device(s), {len(doc['revoked'])} revoked")
    return 0


# ---------------------------------------------------------------- a network's names


def cmd_names(args: argparse.Namespace) -> int:
    from . import identity, names
    try:
        if args.names_command == "init":
            written, kept = names.init_repo(args.dir, args.domain, host=args.host, pyrtan_ref=args.pyrtan_ref)
            for path in written:
                print(f"wrote {path}")
            for path in kept:
                print(f"kept  {path} (already there)")
            print(f"next: commit it, push it to your git host, point {args.domain} at its Pages (or at a hub started --names {args.dir / 'names'}),"
                  f" and claim the first name: pyrtan id name <you>@{args.domain} --into {args.dir}")
            return 0
        domain = identity.check_host(args.domain.lower())
        if not args.head.is_dir():
            raise IdentityError(f"{args.head} is not a directory")
        problems = names.gate_key(args.head, args.base, domain, label=args.head.name or "names")
    except IdentityError as e:
        print(f"pyrtan names: {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"gate: {p}")
    print(f"gate: {'refused' if problems else 'open'} ({domain}, names owned by their keys)")
    return 1 if problems else 0


# ---------------------------------------------------------------- grants


def cmd_allow(args: argparse.Namespace) -> int:
    from .grants import parse_may
    from .identity import IdentityError
    try:
        may = parse_may(args.may)
    except IdentityError as e:
        print(f"pyrtan allow: {e}", file=sys.stderr)
        return 2
    return _hub_post(args, "/allow", {"who": args.who, "may": may}, lambda b: f"{args.at} grants {b['grant']['name'] or b['grant']['id']} ({b['grant']['id']}): {', '.join(b['grant']['may'])}")


def cmd_disallow(args: argparse.Namespace) -> int:
    return _hub_post(args, "/disallow", {"who": args.who}, lambda b: f"{args.at} grants {args.who} nothing now" if b["removed"] else f"{args.at} granted {args.who} nothing")


def cmd_allowed(args: argparse.Namespace) -> int:
    at = args.at.rstrip("/")
    try:
        status, body = _call(args, "GET", f"{at}/allow")
    except Exception as e:
        print(f"pyrtan: {at}: {e}", file=sys.stderr)
        return 3
    if status in (401, 403):
        print(f"pyrtan: {at}: {(body or {}).get('say') if isinstance(body, dict) else status}", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"pyrtan: {at} answered {status}; is it a hub of pyrtan 0.14 or later?", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    grants = body.get("grants", [])
    print(f"{at} (audience {body.get('audience')}) grants {len(grants)} identit{'y' if len(grants) == 1 else 'ies'}")
    for g in grants:
        doc = g.get("document")
        held = f"document of {time.strftime('%Y-%m-%d', time.localtime(doc['updated']))}, {doc['devices']} device(s), {doc['revoked']} revoked" if doc else "no document yet (pyrtan id push)"
        print(f"  {g['name'] or '-':28} {g['id']}  may {','.join(g['may'])}  {held}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
