"""Bake a snapshot of the directory into the site.

GitHub Actions runs this on every build and on a schedule: it asks each
seed hub for its ``/directory``, merges what they say with the snapshot
already published (so a build when every seed is down keeps the last good
one rather than publishing an empty list), drops entries nobody has heard
of for a week, and writes ``rendezvous/directory.json``. The landing page
shows the live directory when a seed answers the visitor's browser and
this snapshot, marked with its time, when none does; ``pyrtan follow``
with no URL tries the hubs in it after the seeds.

Standard library only; no pyrtan import, so it runs before anything is
installed.

    python3 site/snapshot.py SEEDS OUT [--previous URL_OR_FILE ...]
                             [--reports OUT --previous-reports URL_OR_FILE ...]

With ``--reports`` it also asks each seed for ``/reports``, the gap
reports hubs that opted in (``--report-gaps``) gossip for catalog trees,
merges them the same way, and writes ``rendezvous/reports.json``; the
catalog page shows them beside each tree.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

TTL = 7 * 86400
REPORT_TTL = 30 * 86400
TIMEOUT = 8.0


def fetch(source: str) -> Any:
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"Accept": "application/json", "User-Agent": "pyrtan.com snapshot"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def merge(into: dict[str, dict[str, Any]], entries: Any, via: str, now: float) -> int:
    taken = 0
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].startswith(("http://", "https://")):
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
    ap.add_argument("seeds", help="seeds.json, a path or URL")
    ap.add_argument("out", help="where to write directory.json")
    ap.add_argument("--previous", action="append", default=[], help="an earlier directory.json to keep entries from (URL or path; repeatable, first that answers wins)")
    ap.add_argument("--reports", default=None, help="also write the gap reports the seeds carry to this reports.json")
    ap.add_argument("--previous-reports", action="append", default=[], help="an earlier reports.json to keep rows from (URL or path; repeatable, first that answers wins)")
    args = ap.parse_args(argv)
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

    try:
        seeds = [h.rstrip("/") for h in fetch(args.seeds).get("hubs", []) if isinstance(h, str)]
    except Exception as e:
        print(f"snapshot: cannot read seeds from {args.seeds}: {e}", file=sys.stderr)
        seeds = []
    for seed in seeds:
        try:
            d = fetch(f"{seed}/directory")
        except Exception as e:
            report["silent"].append({"seed": seed, "error": str(e)[:120]})
            continue
        if isinstance(d, dict) and isinstance(d.get("hubs"), list):
            merge(hubs, d["hubs"], seed, now)
            report["answered"].append(seed)
        else:
            report["silent"].append({"seed": seed, "error": "not a directory"})
        if args.reports:
            try:
                r = fetch(f"{seed}/reports")
                if isinstance(r, dict):
                    merge_reports(reports, r.get("reports"), now)
            except Exception:
                pass

    out = {
        "pyrtan-directory": 1,
        "as_of": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seeds": report,
        "hubs": sorted(hubs.values(), key=lambda h: (-h["seen"], h["id"])),
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"snapshot: {len(out['hubs'])} hub(s); {len(report['answered'])} seed(s) answered, {len(report['silent'])} silent" + (f"; kept entries from {report['previous']}" if report["previous"] else ""))
    if args.reports:
        rows = sorted(reports.values(), key=lambda r: (-r["count"], -r["last"], r["id"]))
        rpath = Path(args.reports)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.write_text(json.dumps({"pyrtan-reports": 1, "as_of": out["as_of"], "count": len(rows), "reports": rows}, indent=2) + "\n", encoding="utf-8")
        print(f"snapshot: {len(rows)} gap report(s) -> {rpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
