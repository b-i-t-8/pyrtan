"""Names: ``alice@example.org`` resolved to an identity, pinned, and served.

A name resolves by fetching ``https://<domain>/.well-known/pyrtan/name/<local>.json``,
a claim signed by the identity it names. The resolver checks that the
claim is for the name it asked and that the signature holds, then
compares the answer with the *pin*: the identity the name resolved to
the first time. A different answer later is never followed; it is shown
beside the pinned one and the person re-pins by hand, as SSH does with a
changed host key. That is what makes a domain's word safe to take.

Any domain can be a name provider by serving those files, from any
static host or from its own hubs (``pyrtan hub --names DIR``).
:func:`build_wellknown` writes them from a directory of claims (and of
identity documents, served at ``/.well-known/pyrtan/id/<56>.json``);
pyrtan.com's build runs it on ``site/names/``.

Who may change a name is a directory's own rule. :func:`gate_key`, for any
network on any git host, needs nothing but signatures: a new name is open
to whoever signs it, a name moves to a new key only with a *handover* its
old key signs, it is given up only by a *release* its key signs, and an
identity document is never replaced by an older one or deleted while its
name stands. :func:`gate` is pyrtan.com's own rule: a name is its holder's
GitHub login, added and changed only by the GitHub account whose numeric
id the claim carries, because GitHub frees a login when its owner renames.

This module never fetches an ``.onion``: that needs Tor, which pyrtan does
not reach yet. The design is ``_meta/identity.md`` §6.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
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


@dataclass
class Directory:
    """What a directory of names holds: claims, identity documents and releases, by name."""
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    releases: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)


def load_directory(directory: Path | None, domain: str, *, label: str | None = None) -> Directory:
    """The claims (``<name>.json``), releases (a ``<name>.json`` that gives the name up) and
    identity documents (``<name>.id.json``) in ``directory``, for ``domain``. Problems name
    files as ``label/<file>`` (default: the directory as given); what fails a check is left out."""
    d = Directory()
    if directory is None or not directory.is_dir():
        return d
    label = directory.as_posix() if label is None else label

    def rel(p: Path) -> str:
        return f"{label}/{p.name}"

    raw_docs: dict[str, Any] = {}
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.name == "README.md":
            continue
        if not path.is_file() or not path.name.endswith(".json"):
            d.problems.append(Problem(rel(path), "only <name>.json and <name>.id.json files belong here"))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            d.problems.append(Problem(rel(path), f"not JSON: {e}"))
            continue
        if path.name.endswith(".id.json"):
            raw_docs[path.name[: -len(".id.json")]] = data
            continue
        stem = path.name[: -len(".json")]
        if not identity.LOCAL.match(stem):
            d.problems.append(Problem(rel(path), f"{stem!r} is not a name"))
            continue
        if isinstance(data, dict) and "pyrtan-name-release" in data:
            problem = identity.check_release(data, name=f"{stem}@{domain}")
            target = d.releases
        else:
            problem = identity.check_name(data, name=f"{stem}@{domain}")
            target = d.claims
        if problem:
            d.problems.append(Problem(rel(path), problem))
            continue
        target[stem] = data
    for stem, doc in raw_docs.items():
        where = rel(directory / f"{stem}.id.json")
        if stem not in d.claims:
            d.problems.append(Problem(where, f"an identity document needs its name's claim, {stem}.json, beside it"))
            continue
        problem = identity.check_identity(doc, ident=d.claims[stem]["id"])
        if problem:
            d.problems.append(Problem(where, problem))
            continue
        d.docs[stem] = doc
    return d


def read_directory(directory: Path, domain: str, *, label: str | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[Problem]]:
    """``(claims by name, documents by name, problems)``; see :func:`load_directory`."""
    d = load_directory(directory, domain, label=label)
    return d.claims, d.docs, d.problems


def gate_key(head: Path, base: Path | None, domain: str, *, label: str = "names") -> list[Problem]:
    """The rule for a directory of names owned by their keys, on any git host: ``head`` is the
    directory a change proposes, ``base`` the one it changes (None for a first commit).
    Needs only the two trees, never who proposed the change."""
    h = load_directory(head, domain, label=label)
    b = load_directory(base, domain, label=label)
    problems = list(h.problems)
    names = sorted(set(h.claims) | set(h.releases) | set(b.claims) | set(b.releases) | set(b.docs))
    for n in names:
        where, dwhere = f"{label}/{n}.json", f"{label}/{n}.id.json"
        held, hc, hr = b.claims.get(n), h.claims.get(n), h.releases.get(n)
        if held is not None:
            if hc is not None and hc["id"] != held["id"]:
                problem = identity.check_handover(hc, held["id"])
                if problem:
                    problems.append(Problem(where, problem))
            elif hr is not None and hr["id"] != held["id"]:
                problems.append(Problem(where, f"the release is {identity.short(hr['id'])}'s; {n} is held by {identity.short(held['id'])}"))
            elif hc is None and hr is None and not (head / f"{n}.json").exists():
                problems.append(Problem(where, f"{n} is given up by replacing its claim with a release its key signs (pyrtan id release), not by deleting it"))
        elif hr is not None and n not in b.releases:
            problems.append(Problem(where, f"nobody holds {n}; there is nothing to release"))
        # An identity document only moves forward while its name stays with the same key.
        old = b.docs.get(n)
        if old is not None and held is not None and hc is not None and hc["id"] == held["id"]:
            new = h.docs.get(n)
            if new is None and not (head / f"{n}.id.json").exists():
                problems.append(Problem(dwhere, "an identity document is replaced by a newer one its key signs, not deleted: deleting it would hide its revocations"))
            elif new is not None and new["updated"] < old["updated"]:
                problems.append(Problem(dwhere, "this document is older than the one it replaces: an older one could bring a revoked device back"))
    return problems


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
    """Write a claim (and the identity document) where ``checkout`` keeps names: a network's
    names repository (``pyrtan-names.json`` and ``names/``), or this repository's ``site/names/``."""
    network = repo_domain(checkout)
    if network is not None:
        directory, domain = checkout / "names", network
    elif (checkout / "site").is_dir():
        directory = checkout / NAMES_DIR
        domain = (checkout / "site" / "CNAME").read_text(encoding="utf-8").strip() if (checkout / "site" / "CNAME").is_file() else "pyrtan.com"
    else:
        raise IdentityError(f"{checkout}: neither a names repository (pyrtan names init) nor a checkout of github.com/b-i-t-8/pyrtan")
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


# ---------------------------------------------------------------- a network's own names repository

REPO_FILE = "pyrtan-names.json"   # at a names repository's root: {"pyrtan-names-repo": 1, "domain": ...}
HOSTS = ("github", "gitlab", "plain")
PYRTAN_GIT = "https://github.com/b-i-t-8/pyrtan"

_README = """# Names at __DOMAIN__

A name here is `<name>@__DOMAIN__`, pointing at a pyrtan identity (a key:
`pyr:` and 56 characters). Anyone resolves it with

    pyrtan id resolve <name>@__DOMAIN__

which fetches `https://__DOMAIN__/.well-known/pyrtan/name/<name>.json`, checks
that the key signed it, and pins the answer. The names are the files in
`names/`; they are served by GitHub or GitLab Pages, or by any hub of the
network started `pyrtan hub --names names --names-domain __DOMAIN__`.

## Getting one

In the pyrtan console, Identity tab: *Claim a name*, with this repository and
`__DOMAIN__`. Or from a shell:

    pyrtan id name <name>@__DOMAIN__ --into <a checkout of this repository>

Then open a pull (or merge) request with `names/<name>.json`, and
`names/<name>.id.json` if you want hubs to find your devices here.

## Who may change a name

Names here are owned by their keys, and the gate checks only signatures:

- a new name is open to whoever signs it, while nobody holds it;
- a name moves to a new key only with a handover its old key signs
  (`pyrtan id name <name>@__DOMAIN__ --handover-from OLD-KEY`);
- a name is given up by replacing its claim with a release its key signs
  (`pyrtan id release <name>@__DOMAIN__`), never by deleting it;
- an identity document is replaced only by a newer one its key signs.

Nothing in this repository is secret: never put an invite, a token or anyone's
key backup here, not even encrypted.
"""

_GITHUB_GATE = """# Generated by `pyrtan names init`: the names gate for __DOMAIN__.
#
# pull_request_target, so the gate is always this file as it is on the default
# branch, and pyrtan comes from a pinned source, never from the pull request: a
# pull request cannot change what judges it. The pull request's tree is checked
# out as data and compared with its merge base; nothing in it is run. Read-only
# token, no secrets, no credentials left in the checkouts, no cache.
# Pin __REF__ to a commit of pyrtan you have read.

name: names-gate

on:
  pull_request_target:
    paths: ["names/**"]

permissions:
  contents: read

jobs:
  gate:
    runs-on: ubuntu-latest
    env:
      BASE_SHA: ${{ github.event.pull_request.base.sha }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          path: base-repo
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/checkout@v4
        with:
          repository: ${{ github.event.pull_request.head.repo.full_name }}
          ref: ${{ github.event.pull_request.head.sha }}
          path: pr
          fetch-depth: 0
          persist-credentials: false
      - name: The merge base, as data
        run: |
          git -C pr fetch --no-tags "$GITHUB_WORKSPACE/base-repo" "$BASE_SHA"
          git -C pr worktree add --detach "$GITHUB_WORKSPACE/base" "$(git -C pr merge-base "$BASE_SHA" HEAD)"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "pyrtan[identity] @ git+__PYRTAN__@__REF__"
      - run: pyrtan names gate pr/names --base base/names --domain __DOMAIN__
"""

_GITHUB_PAGES = """# Generated by `pyrtan names init`: serve names/ as https://__DOMAIN__/.well-known/pyrtan/.
# Set the repository's Pages source to "GitHub Actions" and its custom domain to __DOMAIN__.

name: names-pages

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "pyrtan[identity] @ git+__PYRTAN__@__REF__"
      - run: pyrtan id wellknown names --domain __DOMAIN__ --out site && echo __DOMAIN__ > site/CNAME
      # v3, not v4: v4 leaves out dot-directories, and the names live in .well-known/.
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
"""

_GITLAB = """# Generated by `pyrtan names init`: the names gate for __DOMAIN__, and Pages.
#
# On GitLab a merge request's pipeline runs the merge request's own copy of this
# file, so read any change to it before merging: the gate is pyrtan from a pinned
# source, not from the merge request, but this file says which. Protect the
# default branch and require the pipeline to pass. Pin __REF__ to a commit of
# pyrtan you have read.

variables:
  GIT_DEPTH: "0"

names-gate:
  image: python:3.12
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes: ["names/**/*"]
  script:
    - pip install "pyrtan[identity] @ git+__PYRTAN__@__REF__"
    - git fetch origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - git worktree add --detach /tmp/base "$(git merge-base FETCH_HEAD HEAD)"
    - pyrtan names gate names --base /tmp/base/names --domain __DOMAIN__

pages:
  image: python:3.12
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  script:
    - pip install "pyrtan[identity] @ git+__PYRTAN__@__REF__"
    - pyrtan id wellknown names --domain __DOMAIN__ --out public
  artifacts:
    paths: [public]
"""


def init_repo(directory: Path, domain: str, *, host: str = "github", pyrtan_ref: str = "main") -> tuple[list[Path], list[Path]]:
    """Write a names repository for ``domain`` into ``directory``: ``pyrtan-names.json``, a
    README, ``names/``, and the gate and Pages for ``host``. Returns ``(written, kept)``: a file
    that is already there is kept, never overwritten."""
    domain = identity.check_host(domain.lower())
    if host not in HOSTS:
        raise IdentityError(f"host is one of {', '.join(HOSTS)}")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", pyrtan_ref):
        raise IdentityError(f"{pyrtan_ref!r} is not a git ref")
    fill = lambda t: t.replace("__DOMAIN__", domain).replace("__REF__", pyrtan_ref).replace("__PYRTAN__", PYRTAN_GIT)  # noqa: E731
    files = {REPO_FILE: json.dumps({"pyrtan-names-repo": 1, "domain": domain}, indent=2) + "\n", "README.md": fill(_README), "names/.gitkeep": ""}
    if host == "github":
        files[".github/workflows/names-gate.yml"] = fill(_GITHUB_GATE)
        files[".github/workflows/names-pages.yml"] = fill(_GITHUB_PAGES)
    elif host == "gitlab":
        files[".gitlab-ci.yml"] = fill(_GITLAB)
    written, kept = [], []
    for rel, text in files.items():
        path = directory / rel
        if path.exists():
            kept.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written, kept


def repo_domain(checkout: Path) -> str | None:
    """The domain a names repository (``pyrtan-names.json``) is for, if ``checkout`` is one."""
    try:
        data = json.loads((checkout / REPO_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("domain") if isinstance(data, dict) and data.get("pyrtan-names-repo") == 1 and isinstance(data.get("domain"), str) else None

