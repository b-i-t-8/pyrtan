"""Grants: which identities may do what on this hub.

An identity grants nothing by itself (``_meta/identity.md`` §1): a hub
keeps its own list, from ``--allow`` and from its operator at ``/allow``,
of roots and what each may do here: ``ask`` (the routed paths behind a
token), ``chat`` (a seat in a lobby behind a token), ``run`` (follow as a
member and be handed trees) and ``operate`` (everything the token allows).
A device may do what its root's grant says, intersected with its note's
``may``.

A grant made by name (``alice@pyrtan.com``) resolves the name once and is
held against the identifier from then on: the name is the pin, and is
never resolved again, so a domain that later points it elsewhere moves
nothing here. Each grant keeps
the newest identity document it has seen, from a push to ``/identity``,
from the name's domain or from the document's own routes, re-read at
least hourly and on a request from a device not seen in a day; a device
the document revokes is refused. The list lives in a JSON file beside
the hub's state, or in memory without one.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import identity, names
from .identity import ABILITIES, IdentityError, Signer

REFRESH_EVERY = 3600.0      # seconds between reads of a grant's identity document
DEVICE_UNSEEN = 86400.0     # a device not seen for this long makes its root's document be re-read

# Reached with a key whatever its grants: following as a member is open, and trust is decided there.
OPEN = frozenset({"/inbox"})


def ability_for(method: str, path: str) -> str | None:
    """The ability a key-signed request needs for ``path`` on a hub with a token; None for
    the paths anyone may reach (the public ones, the inbox, a pushed identity document)."""
    from .service import public_path
    if public_path(path) or path in OPEN:
        return None
    if path in ("/intent", "/run", "/resume") or (method == "GET" and path == "/capabilities") or (method == "POST" and path == "/suggestions"):
        return "ask"  # an agent's report is as much as an ask: its asks record gaps already
    if path == "/chat" or path.startswith("/chat/"):
        return "chat"
    return "operate"


def parse_may(text: Any) -> list[str]:
    """``"ask,run"`` (or a list) as abilities, each once, in the canonical order."""
    items = text if isinstance(text, list) else str(text or "").replace(" ", "").split(",")
    items = [a for a in items if a]
    unknown = [a for a in items if a not in ABILITIES]
    if unknown or not items:
        raise IdentityError(f"abilities are some of {', '.join(ABILITIES)}" + (f"; not {', '.join(unknown)}" if unknown else ""))
    return [a for a in ABILITIES if a in items]


def parse_spec(spec: str) -> tuple[str, list[str]]:
    """``WHO=ask,run`` as ``(who, abilities)``; WHO is ``pyr:…`` or ``name@domain``."""
    who, eq, may = spec.rpartition("=")
    if not eq or not who:
        raise IdentityError(f"{spec!r}: write WHO=ABILITIES, e.g. alice@pyrtan.com=ask,chat")
    identity.parse(who)
    return who, parse_may(may)


@dataclass
class Grant:
    id: str
    may: list[str]
    name: str | None = None
    doc: dict[str, Any] | None = None
    fetched: float = 0.0
    granted: float = field(default_factory=time.time)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "may": self.may, "granted": self.granted,
                "document": {"updated": self.doc["updated"], "routes": self.doc["routes"], "devices": len(self.doc["devices"]), "revoked": len(self.doc["revoked"])} if self.doc else None}

    def to_state(self) -> dict[str, Any]:
        return {"id": self.id, "may": self.may, "name": self.name, "doc": self.doc, "fetched": self.fetched, "granted": self.granted}


class Grants:
    def __init__(self, path: str | Path | None = None, *, fetch: names.Fetch | None = None,
                 on_change: Callable[[str], None] | None = None) -> None:
        self.path = Path(path) if path else None
        self.fetch = fetch
        self.on_change = on_change          # called with a root whose grant or document changed
        self.grants: dict[str, Grant] = {}
        self.seen: dict[str, float] = {}     # device -> when it last signed here
        self._lock = threading.RLock()
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------ the list

    def __len__(self) -> int:
        return len(self.grants)

    def by_name(self, name: str) -> Grant | None:
        key = str(identity.parse(name))
        return next((g for g in self.grants.values() if g.name == key), None)

    def allow(self, who: str, may: list[str]) -> Grant:
        """Grant ``who`` (``pyr:…`` or a name, resolved and pinned now) the abilities ``may``."""
        may = parse_may(may)
        parsed = identity.parse(who)
        if parsed.form == "name":
            held = self.by_name(who)
            claim = names.resolve(who, fetch=self.fetch) if held is None else None
            ident = held.id if held else claim["id"]
            name = str(parsed)
        else:
            ident, name, claim = parsed.id, None, None
        with self._lock:
            grant = self.grants.get(ident)
            if grant is None:
                grant = self.grants[ident] = Grant(ident, may, name)
            else:
                grant.may = may
                grant.name = grant.name or name
            self._save()
        if claim is not None and grant.doc is None:
            self.refresh_one(ident)
        self._changed(ident)
        return grant

    def disallow(self, who: str) -> bool:
        parsed = identity.parse(who)
        grant = self.by_name(who) if parsed.form == "name" else self.grants.get(parsed.id)
        if grant is None:
            return False
        with self._lock:
            self.grants.pop(grant.id, None)
            self._save()
        self._changed(grant.id)
        return True

    def view(self) -> list[dict[str, Any]]:
        with self._lock:
            return [g.view() for g in sorted(self.grants.values(), key=lambda g: (g.name or "~", g.id))]

    # ------------------------------------------------------------ callers

    def may(self, signer: Signer, *, now: float | None = None) -> set[str]:
        """What ``signer`` may do here: its root's grant, capped by the device note, and
        nothing for a device its root's document revokes. Notes a device's visit."""
        now = time.time() if now is None else now
        grant = self.grants.get(signer.root)
        if grant is None:
            return set()
        if signer.note is None:
            return set(grant.may)
        if grant.doc is not None and identity.device_status(grant.doc, signer.key, now=now) == "revoked":
            return set()
        last = self.seen.get(signer.key)
        self.seen[signer.key] = now
        if last is None or now - last > DEVICE_UNSEEN:
            threading.Thread(target=self.refresh_one, args=(signer.root,), daemon=True).start()
        return set(grant.may) & set(signer.note["may"])

    def still(self, root: str, device: str, note: dict[str, Any] | None, ability: str, *, now: float | None = None) -> bool:
        """Whether a member that followed as ``device`` for ``root`` may still do ``ability``."""
        if note is not None and identity.check_device(note, root=root, now=now) is not None:
            return False
        return ability in self.may(Signer(device, root, note), now=now)

    # ------------------------------------------------------------ documents

    def document(self, ident: str) -> dict[str, Any] | None:
        grant = self.grants.get(ident.lower())
        return grant.doc if grant else None

    def take_document(self, doc: Any, *, now: float | None = None) -> str | None:
        """Keep ``doc`` if it is a granted root's and newer than ours; None, or why not."""
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str):
            return "not an identity document"
        grant = self.grants.get(doc["id"].lower())
        if grant is None:
            return "this hub grants that identity nothing, so keeps no document for it"
        problem = identity.check_identity(doc, ident=grant.id, now=now)
        if problem:
            return problem
        with self._lock:
            if grant.doc is not None and doc["updated"] <= grant.doc["updated"]:
                return None
            grant.doc = doc
            self._save()
        self._changed(grant.id)
        return None

    def refresh_one(self, ident: str) -> None:
        """Look for a newer document where the grant's name and routes say it lives."""
        grant = self.grants.get(ident)
        if grant is None:
            return
        hosts = list(grant.doc["routes"]) if grant.doc else []
        if grant.name:
            hosts.insert(0, identity.parse(grant.name).domain)
        grant.fetched = time.time()
        for host in dict.fromkeys(hosts):
            try:
                doc = names.fetch_identity(ident, host, fetch=self.fetch)
            except IdentityError:
                continue  # an onion route, for now
            if doc is not None:
                self.take_document(doc)

    def refresh(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for ident in [g.id for g in list(self.grants.values()) if now - g.fetched >= REFRESH_EVERY]:
            self.refresh_one(ident)

    # ------------------------------------------------------------ state

    def _changed(self, ident: str) -> None:
        if self.on_change:
            self.on_change(ident)

    def _save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {"pyrtan-grants": 1, "grants": [g.to_state() for g in self.grants.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def _load(self) -> None:
        assert self.path
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise IdentityError(f"{self.path}: {e}") from None
        if not isinstance(data, dict) or data.get("pyrtan-grants") != 1:
            raise IdentityError(f"{self.path} is not a pyrtan grants file")
        for d in data.get("grants", []):
            try:
                ident = identity.normal(d["id"])
                doc = d.get("doc") if identity.check_identity(d.get("doc"), ident=ident) is None else None
                self.grants[ident] = Grant(ident, parse_may(d.get("may")), d.get("name"), doc, float(d.get("fetched", 0)), float(d.get("granted", 0)))
            except (IdentityError, KeyError, TypeError, ValueError):
                continue
