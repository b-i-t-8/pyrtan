"""Names: ``alice@example.org`` resolved to an identity, pinned, and served.

A name resolves by fetching ``https://<domain>/.well-known/pyrtan/name/<local>.json``,
a claim signed by the identity it names. The resolver checks that the
claim is for the name it asked and that the signature holds, then
compares the answer with the *pin*: the identity the name resolved to
the first time. A different answer later is never followed; it is shown
beside the pinned one and the person re-pins by hand, as SSH does with a
changed host key. That is what makes a domain's word safe to take.

Any domain can be a name provider by serving those files.
:func:`build_wellknown` writes them from a directory of claims (and of
identity documents, served at ``/.well-known/pyrtan/id/<56>.json``);
pyrtan.com's build runs it on ``site/names/``. :func:`gate` is pyrtan.com's
own rule for that directory: a name is its holder's GitHub login, added
and changed only by the GitHub account whose numeric id the claim
carries, because GitHub frees a login when its owner renames.

This module never fetches an ``.onion``: that needs Tor, which pyrtan does
not reach yet. The design is ``_meta/identity.md`` §6.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import identity
from .catalog import Problem
from .identity import IdentityError

WELL_KNOWN = ".well-known/pyrtan"
NAMES_DIR = "site/names"
GITHUB_LOGIN = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}$")

Fetch = Callable[[str], tuple[int, Any]]


# ---------------------------------------------------------------- resolving


def _fetch(url: str) -> tuple[int, Any]:
    from .service import http_json
    return http_json("GET", url, timeout=10.0)


def _base(host: str) -> str:
    host = identity.check_locator(host)
    if host.split(":")[0].endswith(".onion"):
        raise IdentityError(f"{host} is an onion service: reaching it needs Tor, which pyrtan does not reach yet")
    return f"https://{host}"


def name_url(name: str) -> str:
    parsed = identity.parse(name)
    if parsed.form != "name":
        raise IdentityError(f"{name!r} is not a name (name@domain)")
    return f"{_base(parsed.domain)}/{WELL_KNOWN}/name/{parsed.name}.json"


def identity_url(ident: str, host: str) -> str:
    return f"{_base(host)}/{WELL_KNOWN}/id/{identity.normal(ident)[len(identity.PREFIX):]}.json"


def resolve(name: str, *, fetch: Fetch | None = None) -> dict[str, Any]:
    """The checked claim ``name``'s domain serves; IdentityError says why there is none."""
    url = name_url(name)
    try:
        status, claim = (fetch or _fetch)(url)
    except Exception as e:
        raise IdentityError(f"{url}: {e}") from None
    if status == 404:
        raise IdentityError(f"{identity.parse(name)} has no claim at {url}")
    if status != 200:
        raise IdentityError(f"{url} answered {status}")
    problem = identity.check_name(claim, name=name)
    if problem:
        raise IdentityError(f"{url}: {problem}")
    return claim


def fetch_identity(ident: str, host: str, *, fetch: Fetch | None = None) -> dict[str, Any] | None:
    """The identity document ``host`` serves for ``ident``, if it serves one that holds."""
    try:
        status, doc = (fetch or _fetch)(identity_url(ident, host))
    except Exception:
        return None
    if status != 200 or identity.check_identity(doc, ident=ident) is not None:
        return None
    return doc


# ---------------------------------------------------------------- pins


class Pins:
    """Name -> the identity it first resolved to, in a JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.names: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise IdentityError(f"{path}: {e}") from None
            if not isinstance(data, dict) or data.get("pyrtan-pins") != 1:
                raise IdentityError(f"{path} is not a pyrtan pins file")
            self.names = data.get("names", {})

    def get(self, name: str) -> dict[str, Any] | None:
        return self.names.get(str(identity.parse(name)))

    def compare(self, name: str, ident: str) -> str:
        """``new``, ``same`` or ``changed``."""
        pin = self.get(name)
        return "new" if pin is None else "same" if pin["id"] == identity.normal(ident) else "changed"

    def pin(self, name: str, ident: str, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        key, ident = str(identity.parse(name)), identity.normal(ident)
        old = self.names.get(key)
        first = old["first"] if old and old["id"] == ident else now
        self.names[key] = {"id": ident, "first": first, "seen": now}

    def forget(self, name: str) -> bool:
        return self.names.pop(str(identity.parse(name)), None) is not None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"pyrtan-pins": 1, "names": self.names}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(self.path)


# ---------------------------------------------------------------- a directory of names


def read_directory(directory: Path, domain: str, *, label: str | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[Problem]]:
    """The claims (``<name>.json``) and identity documents (``<name>.id.json``) in
    ``directory``, for ``domain``: ``(claims by name, documents by name, problems)``.
    Problems name files as ``label/<file>`` (default: the directory as given)."""
    claims: dict[str, dict[str, Any]] = {}
    docs: dict[str, dict[str, Any]] = {}
    problems: list[Problem] = []
    if not directory.is_dir():
        return claims, docs, problems
    label = directory.as_posix() if label is None else label

    def rel(p: Path) -> str:
        return f"{label}/{p.name}"

    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.name == "README.md":
            continue
        if not path.is_file() or not path.name.endswith(".json"):
            problems.append(Problem(rel(path), "only <name>.json and <name>.id.json files belong here"))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(Problem(rel(path), f"not JSON: {e}"))
            continue
        if path.name.endswith(".id.json"):
            docs[path.name[: -len(".id.json")]] = data
            continue
        stem = path.name[: -len(".json")]
        problem = identity.check_name(data, name=f"{stem}@{domain}") if identity.LOCAL.match(stem) else f"{stem!r} is not a name"
        if problem:
            problems.append(Problem(rel(path), problem))
            continue
        claims[stem] = data
    for stem, doc in docs.items():
        where = rel(directory / f"{stem}.id.json")
        if stem not in claims:
            problems.append(Problem(where, f"an identity document needs its name's claim, {stem}.json, beside it"))
            continue
        problem = identity.check_identity(doc, ident=claims[stem]["id"])
        if problem:
            problems.append(Problem(where, problem))
    docs = {s: d for s, d in docs.items() if s in claims and identity.check_identity(d, ident=claims[s]["id"]) is None}
    return claims, docs, problems


def build_wellknown(directory: Path, out: Path, domain: str) -> tuple[int, list[Problem]]:
    """Write ``out/.well-known/pyrtan/``: ``name/<name>.json`` per claim, ``id/<56>.json``
    per identity document, and ``names.json`` listing every claim. Returns how
    many names were written and the problems; nothing that fails a check is served."""
    claims, docs, problems = read_directory(directory, domain)
    root = out / WELL_KNOWN
    if root.exists():
        shutil.rmtree(root)
    (root / "name").mkdir(parents=True)
    (root / "id").mkdir()
    for stem, claim in claims.items():
        (root / "name" / f"{stem}.json").write_text(json.dumps(claim, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    newest: dict[str, dict[str, Any]] = {}
    for doc in docs.values():
        if doc["id"] not in newest or doc["updated"] > newest[doc["id"]]["updated"]:
            newest[doc["id"]] = doc
    for ident, doc in newest.items():
        (root / "id" / f"{ident[len(identity.PREFIX):]}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    index = {"pyrtan-names": 1, "domain": domain, "names": claims}
    (root / "names.json").write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return len(claims), problems


# ---------------------------------------------------------------- pyrtan.com's gate


def gate(root: Path, *, changed: list[str] | None = None, author: str | None = None, author_id: int | None = None,
         base: dict[str, bytes] | None = None) -> list[Problem]:
    """pyrtan.com's rule for ``site/names/``, on top of every claim and document holding:
    a name is a GitHub login, at most one per GitHub account (its numeric ``github`` id).
    Given the pull request's ``changed`` paths, ``author`` (login), ``author_id`` and
    ``base`` (``path -> bytes`` on the base branch), a new name must be the author's own
    login and carry the author's id, and a name may be changed or removed only by the
    account whose id it carried before."""
    domain = (root / "site" / "CNAME").read_text(encoding="utf-8").strip() if (root / "site" / "CNAME").is_file() else "pyrtan.com"
    directory = root / NAMES_DIR
    claims, _, problems = read_directory(directory, domain, label=NAMES_DIR)
    holders: dict[int, str] = {}
    for stem, claim in claims.items():
        where = f"{NAMES_DIR}/{stem}.json"
        if not GITHUB_LOGIN.match(stem):
            problems.append(Problem(where, "a name at pyrtan.com is a GitHub login"))
        github = claim.get("github")
        if not isinstance(github, int) or isinstance(github, bool) or github <= 0:
            problems.append(Problem(where, "a claim at pyrtan.com carries \"github\": your numeric GitHub user id"))
            continue
        if github in holders:
            problems.append(Problem(where, f"GitHub account {github} already holds {holders[github]}; one name per account"))
        holders[github] = stem
    if not changed:
        return problems
    base = base or {}
    login = (author or "").lstrip("@").lower()
    for c in changed:
        if not c.startswith(f"{NAMES_DIR}/") or c.endswith("/README.md"):
            continue
        name = Path(c).name
        stem = name[: -len(".id.json")] if name.endswith(".id.json") else name[: -len(".json")] if name.endswith(".json") else name
        claim_path = f"{NAMES_DIR}/{stem}.json"
        before = _github_of(base.get(claim_path))
        if author_id is None:
            problems.append(Problem(c, "the gate needs the pull request author's numeric id (--author-id) to judge a change to a name"))
            continue
        if before is not None:
            if before != author_id:
                problems.append(Problem(c, f"{stem} belongs to GitHub account {before}; only that account changes or removes it (a maintainer may merge over this)"))
            elif stem in claims and claims[stem].get("github") != before:
                problems.append(Problem(c, f"{stem} stays with GitHub account {before}; to move it, remove it and add your new login's name"))
            continue
        # A new name.
        if stem not in claims:
            continue  # a stray file, already reported
        if stem != login:
            problems.append(Problem(c, f"a new name is your own GitHub login: {stem!r} is not {login or 'the author'}'s"))
        if claims[stem].get("github") != author_id:
            problems.append(Problem(c, f"the claim says github {claims[stem].get('github')}; the pull request's author is {author_id}"))
    return problems


def _github_of(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    try:
        github = json.loads(raw.decode("utf-8")).get("github")
    except (ValueError, AttributeError):
        return None
    return github if isinstance(github, int) and not isinstance(github, bool) else None


def place(checkout: Path, claim: dict[str, Any], doc: dict[str, Any] | None) -> list[Path]:
    """Write a claim (and the identity document) into ``checkout/site/names/``."""
    directory = checkout / NAMES_DIR
    if not (checkout / "site").is_dir():
        raise IdentityError(f"{checkout}: no site/ directory; is this a checkout of github.com/b-i-t-8/pyrtan?")
    domain = (checkout / "site" / "CNAME").read_text(encoding="utf-8").strip() if (checkout / "site" / "CNAME").is_file() else "pyrtan.com"
    if claim["domain"] != domain:
        raise IdentityError(f"this checkout serves names at {domain}; the claim is for {claim['name']}@{claim['domain']}")
    directory.mkdir(parents=True, exist_ok=True)
    written = [directory / f"{claim['name']}.json"]
    written[0].write_text(json.dumps(claim, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if doc is not None:
        written.append(directory / f"{claim['name']}.id.json")
        written[1].write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return written


def github_id(login: str, *, fetch: Fetch | None = None) -> int:
    """A GitHub login's numeric user id, from GitHub's public API."""
    url = f"https://api.github.com/users/{login}"
    try:
        status, body = (fetch or _fetch)(url)
    except Exception as e:
        raise IdentityError(f"{url}: {e}; pass --github YOUR-NUMERIC-ID") from None
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("id"), int):
        raise IdentityError(f"{url} answered {status}; pass --github YOUR-NUMERIC-ID")
    return body["id"]
