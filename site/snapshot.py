"""Bake a snapshot of the directory into the site.

GitHub Actions runs this on every build and on a schedule: it asks each
hub in the hub list (``rendezvous/hubs.json``, which operators add to by
pull request) for its ``/directory``, then every other hub it knows of
(from the last snapshot and from what the hubs it asked said), so the
directory stays fresh while any hub it knows of is up. pyrtan.com runs no
hub of its own; this is the whole of its part. It
merges what they say with the snapshot already published (so a build when
nobody answers keeps the last good one rather than publishing an empty
list), drops entries nobody has heard of for a week, and writes
``rendezvous/directory.json``. It keeps only the default network's part
of each directory (``--network``, the ``network.json`` the site
publishes) and what was heard outside any network; a private network's
hubs never reach the snapshot. The landing page
shows the live directory when a listed hub answers the visitor's browser
and this snapshot, marked with its time, when none does; ``pyrtan join``
with no invite tries the hubs in it after the listed ones.

The hubs it asks are whoever a directory names, so it asks only hubs on
public addresses (every redirect checked too; ``--any-address`` for tests
or a build inside a private network) and reads at most 4 MiB from each.

Standard library only; no pyrtan import, so it runs before anything is
installed.

    python3 site/snapshot.py HUBS OUT [--previous URL_OR_FILE ...]
                             [--network URL_OR_FILE] [--max N]
                             [--reports OUT --previous-reports URL_OR_FILE ...]

With ``--reports`` it also asks each hub that answered for ``/reports``, the gap
reports hubs that opted in (``--report-gaps``) gossip for catalog trees,
merges them the same way, and writes ``rendezvous/reports.json``; the
catalog page shows them beside each tree.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import ipaddress
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TTL = 7 * 86400
REPORT_TTL = 30 * 86400
TIMEOUT = 8.0
MAX_ASK = 200      # hubs asked per run, listed ones included
PARALLEL = 16
MAX_BYTES = 4 * 1024 * 1024   # a directory or reports answer, at most
ANY_ADDRESS = False           # --any-address: hubs on private addresses too (tests, a private build)


def public(url: str) -> None:
    """Refuse a URL whose host is, or resolves to, an address that is not on the
    public internet: the hubs are whoever a directory names, and the runner
    this crawls from is not theirs to point at its own network."""
    if ANY_ADDRESS:
        return
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise ValueError(f"{url}: no host")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise ValueError(f"{host}: {e}") from None
    for a in addresses:
        if not ipaddress.ip_address(a.split("%", 1)[0]).is_global:
            raise ValueError(f"{host} is {a}, not a public address")


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        public(newurl)  # each hop, not only the first
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_hub_opener = urllib.request.build_opener(_Redirects())


def fetch(source: str, *, hub: bool = False) -> Any:
    """JSON from a URL or a file. ``hub``: a URL a directory named, so only on a
    public address. Either way at most :data:`MAX_BYTES`."""
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"Accept": "application/json", "User-Agent": "pyrtan.com snapshot"})
        if hub:
            public(source)
        with (_hub_opener.open if hub else urllib.request.urlopen)(req, timeout=TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError(f"{source}: more than {MAX_BYTES} bytes")
        return json.loads(data.decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def network_id(name: str, secret: str) -> str:
    """As pyrtan.federation.network_id; repeated so this runs before pyrtan is installed."""
    return hashlib.sha256(f"pyrtan-network\n{name}\n{secret}".encode("utf-8")).hexdigest()[:16]


def merge(into: dict[str, dict[str, Any]], entries: Any, via: str, now: float, network: str | None = None) -> int:
    """Take a directory's rows; with ``network`` (an id), only those heard
    in it or outside any network."""
    taken = 0
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].startswith(("http://", "https://")):
            continue
        nets = e.get("networks")
        if network is not None and isinstance(nets, list) and nets and network not in nets:
            continue
        try:
            seen = min(float(e.get("seen", 0)), now)
        except (TypeError, ValueError):
            continue
        if now - seen >= TTL:
            continue
        hub_id = e["id"].rstrip("/")
        current = into.get(hub_id)
        if current is not None and current["seen"] >= seen:
            continue
        into[hub_id] = {"id": hub_id, "name": str(e.get("name") or "")[:80], "trees": [str(t)[:80] for t in e.get("trees", []) if isinstance(t, (str, int))][:200], "seen": seen, "via": via}
        taken += 1
    return taken


def merge_reports(into: dict[str, dict[str, Any]], rows: Any, now: float) -> int:
    """One row per gap: the hub's view already sums reporters, so a newer
    or larger row for the same gap replaces an older one."""
    taken = 0
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or "@" not in r["id"] or not isinstance(r.get("node"), str):
            continue
        try:
            last = min(float(r.get("last", 0)), now)
            count = max(1, int(r.get("count", 1)))
            reporters = max(1, int(r.get("reporters", 1)))
        except (TypeError, ValueError):
            continue
        if now - last >= REPORT_TTL:
            continue
        key = json.dumps([r["id"], r["node"], r.get("on"), r.get("observed")], sort_keys=True, ensure_ascii=False, default=str)
        current = into.get(key)
        if current is not None and (current["last"], current["count"]) >= (last, count):
            continue
        observed = r.get("observed")
        into[key] = {"id": r["id"][:120], "node": r["node"][:80], "on": str(r.get("on", ""))[:120], "observed": observed if isinstance(observed, (str, int, float, bool)) or observed is None else str(observed)[:80], "count": count, "reporters": reporters, "last": last}
        taken += 1
    return taken


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("hub_list", help="hubs.json, a path or URL")
    ap.add_argument("out", help="where to write directory.json")
    ap.add_argument("--previous", action="append", default=[], help="an earlier directory.json to keep entries from (URL or path; repeatable, first that answers wins)")
    ap.add_argument("--network", default=None, help="the default network's network.json (URL or path): keep only its part of each directory")
    ap.add_argument("--max", type=int, default=MAX_ASK, help=f"hubs to ask at most, listed ones included (default {MAX_ASK})")
    ap.add_argument("--reports", default=None, help="also write the gap reports the hubs carry to this reports.json")
    ap.add_argument("--previous-reports", action="append", default=[], help="an earlier reports.json to keep rows from (URL or path; repeatable, first that answers wins)")
    ap.add_argument("--any-address", action="store_true", help="ask hubs on loopback and private addresses too (tests, a build inside a private network); by default only public ones")
    args = ap.parse_args(argv)
    global ANY_ADDRESS
    ANY_ADDRESS = args.any_address
    now = time.time()
    hubs: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"answered": [], "silent": [], "previous": None}
    reports: dict[str, dict[str, Any]] = {}
    if args.reports:
        for source in args.previous_reports:
            try:
                prev = fetch(source)
            except Exception:
                continue
            if isinstance(prev, dict) and isinstance(prev.get("reports"), list):
                merge_reports(reports, prev["reports"], now)
                break

    for source in args.previous:
        try:
            prev = fetch(source)
        except Exception:
            continue
        if isinstance(prev, dict) and isinstance(prev.get("hubs"), list):
            merge(hubs, prev["hubs"], "snapshot", now)
            report["previous"] = source
            break

    network: str | None = None
    if args.network:
        try:
            n = fetch(args.network)
            network = network_id(n["name"], n["secret"])
        except Exception as e:
            print(f"snapshot: cannot read the default network from {args.network}: {e}; keeping every network's part", file=sys.stderr)
    try:
        listed = [h.rstrip("/") for h in fetch(args.hub_list).get("hubs", []) if isinstance(h, str)]
    except Exception as e:
        print(f"snapshot: cannot read the hub list from {args.hub_list}: {e}", file=sys.stderr)
        listed = []
    report["crawled"] = {"answered": [], "silent": []}

    def ask(hub: str) -> tuple[str, Any, Any, str | None]:
        try:
            d = fetch(f"{hub}/directory", hub=True)
        except Exception as e:
            return hub, None, None, str(e)[:120]
        r = None
        if args.reports and isinstance(d, dict):
            try:
                r = fetch(f"{hub}/reports", hub=True)
            except Exception:
                pass
        return hub, d, r, None

    # The listed hubs first, then every hub the last snapshot heard of, most
    # recent first, then whoever the hubs asked name: rounds of parallel
    # asks until nobody new is named or --max is reached.
    asked: set[str] = set()
    pending = list(dict.fromkeys(listed + [h["id"] for h in sorted(hubs.values(), key=lambda h: -h["seen"])]))
    while pending and len(asked) < args.max:
        batch = [h for h in pending if h not in asked][: args.max - len(asked)]
        pending = []
        asked.update(batch)
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            results = list(pool.map(ask, batch))
        for hub, d, r, error in results:
            book = report if hub in listed else report["crawled"]
            if error is None and isinstance(d, dict) and isinstance(d.get("hubs"), list):
                merge(hubs, d["hubs"], hub, now, network)
                book["answered"].append(hub)
                pending.extend(e["id"].rstrip("/") for e in d["hubs"] if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"].startswith(("http://", "https://")) and e["id"].rstrip("/") not in asked)
                if isinstance(r, dict):
                    merge_reports(reports, r.get("reports"), now)
            else:
                book["silent"].append({"hub": hub, "error": error or "not a directory"})
        pending = list(dict.fromkeys(pending))

    out = {
        "pyrtan-directory": 1,
        "as_of": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "listed": report,
        "hubs": sorted(hubs.values(), key=lambda h: (-h["seen"], h["id"])),
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"snapshot: {len(out['hubs'])} hub(s); {len(report['answered'])} listed hub(s) answered, {len(report['silent'])} silent; {len(report['crawled']['answered'])} other hub(s) answered, {len(report['crawled']['silent'])} silent" + (f"; kept entries from {report['previous']}" if report["previous"] else ""))
    if args.reports:
        rows = sorted(reports.values(), key=lambda r: (-r["count"], -r["last"], r["id"]))
        rpath = Path(args.reports)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.write_text(json.dumps({"pyrtan-reports": 1, "as_of": out["as_of"], "count": len(rows), "reports": rows}, indent=2) + "\n", encoding="utf-8")
        print(f"snapshot: {len(rows)} gap report(s) -> {rpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
