"""The catalog: a directory of trees people can pull by name.

A catalog is static files behind any web server (or a directory on disk):
an ``index.json`` and the tree files it points to. There is no account, no
API and no server-side code; a mirror is a copy of the files, and a
private catalog is the same files on a private host. The one at
``https://pyrtan.com/catalog/`` is built from a public repository whose CI
runs ``pyrtan check`` on every tree and this module's :func:`build_index`
on every merge.

``index.json``::

    {"pyrtan-catalog": 1, "generated": "2026-10-25T12:00:00Z",
     "trees": [{"id": "pyrtan/weather", "namespace": "pyrtan", "name": "weather",
                "version": 3, "description": "...", "tags": [...], "hosts": [...],
                "sources": ["http"], "triggers": [...], "license": "MIT",
                "path": "pyrtan/weather.toml", "sha256": "...", "size": 1234}]}

``path`` is relative to the index. Beside it, ``trees.json`` carries every
tree the index lists as JSON (the TOML's own table), for a runtime that
reads JSON and not TOML: the console in a browser, which runs the
catalog's trees itself until it joins a hub::

    {"pyrtan-trees": 1, "generated": "...", "count": 1,
     "trees": [{"id": "pyrtan/weather", "sha256": "...", "tree": {...}}]}

``sha256`` is the TOML file's, as in the index. A tree's ``id`` is ``namespace/name``:
the namespace is who published it (the tree's own ``namespace`` line), so
two people can each have a ``weather``; ``pyrtan pull weather`` takes the
one there is and asks when there are two, ``pyrtan pull barry/weather``
says which. A pull fetches the file, checks its
``sha256`` against the index, loads it as a tree, checks that the name and
version inside match the entry, and only then writes it. A catalog cannot
hand out a file other than the one its index describes, and a tree a
service loads is still checked like any other: the catalog is a place to
find trees, not a channel that bypasses the checker.

Not confused with :class:`pyrtan.registry.Registry`, which is the set of
trees one running service holds. A catalog is where a registry's files
may have come from.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tree import Tree, TreeError, check, load_tree, load_tree_text

DEFAULT_CATALOG = "https://pyrtan.com/catalog/"
INDEX = "index.json"
FORMAT = 1
SPEC = re.compile(r"^(?:([a-z0-9][a-z0-9-]*)/)?([a-z0-9][a-z0-9-]*)(?:@(\d+))?$")


class CatalogError(RuntimeError):
    pass


def catalog_url() -> str:
    """The catalog to use when none is given: ``$PYRTAN_CATALOG`` or the public one."""
    return os.environ.get("PYRTAN_CATALOG") or DEFAULT_CATALOG


# ---------------------------------------------------------------- building


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_for(tree: Tree, data: bytes, path: str) -> dict[str, Any]:
    """One index entry: the tree's capability plus where its file is and what it hashes to."""
    cap = tree.capability()
    return {
        "id": tree.id,
        "namespace": tree.namespace,
        "name": tree.name,
        "version": tree.version,
        "description": tree.description,
        "tags": tree.tags,
        "hosts": tree.hosts,
        **{k: cap[k] for k in ("paths", "commands", "devices", "topics") if k in cap},
        "sources": cap["sources"],
        "triggers": cap["triggers"],
        "inputs": cap["inputs"],
        "license": tree.license,
        "path": path,
        "sha256": sha256(data),
        "size": len(data),
    }


@dataclass
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def build_index(root: str | Path | list[str | Path], *, require_license: bool = True, copy_to: str | Path | None = None, require_namespace: bool = False) -> tuple[dict[str, Any], list[Problem]]:
    """Walk ``root`` (one directory, or several) for tree files and describe
    each in an index.

    With one root and no ``copy_to``, paths are relative to that root and
    the index belongs beside it. With ``copy_to``, every tree file is copied
    there as ``<namespace>/<name>.toml`` (``_/<name>.toml`` without a
    namespace) and the index describes that layout: the shape the public
    catalog is served in, from any number of source directories.

    Returns the index and the problems that kept a file out of it: a file
    that does not load, two files with the same id, (when required) a tree
    with no ``license`` or no ``namespace``. The caller decides whether
    problems fail the build; the catalog's CI does.
    """
    roots = [Path(r) for r in (root if isinstance(root, list) else [root])]
    if len(roots) > 1 and copy_to is None:
        raise ValueError("an index over several directories needs copy_to: its paths must be relative to one place")
    trees: list[dict[str, Any]] = []
    problems: list[Problem] = []
    seen: dict[str, str] = {}
    for base in roots:
        for path in sorted(base.rglob("*.toml")):
            if path.name == "pyproject.toml" or any(part.startswith(".") for part in path.relative_to(base).parts):
                continue
            rel = path.relative_to(base).as_posix()
            where = rel if len(roots) == 1 else f"{base}/{rel}"
            try:
                tree = load_tree(path)
            except TreeError as e:
                problems.append(Problem(where, str(e)))
                continue
            errors = [p for p in check(tree) if p.level == "error"]
            if errors:
                problems.append(Problem(where, "; ".join(f"{p.where}: {p.message}" for p in errors)))
                continue
            if tree.id in seen:
                problems.append(Problem(where, f"a tree with id {tree.id!r} is already at {seen[tree.id]}"))
                continue
            if require_license and not tree.license:
                problems.append(Problem(where, "no 'license': a catalog tree must say what others may do with it"))
                continue
            if require_namespace and not tree.namespace:
                problems.append(Problem(where, "no 'namespace': a catalog tree says who publishes it"))
                continue
            seen[tree.id] = where
            data = path.read_bytes()
            if copy_to is not None:
                rel = f"{tree.namespace or '_'}/{tree.name}.toml"
                target = Path(copy_to) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            trees.append(entry_for(tree, data, rel))
    index = {
        "pyrtan-catalog": FORMAT,
        "generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(trees),
        "trees": trees,
    }
    return index, problems


def build_bundle(index: dict[str, Any], base: str | Path) -> dict[str, Any]:
    """``trees.json`` for an index whose files are under ``base``: each tree's
    table as JSON, in the index's order, with the file's ``sha256``."""
    trees = []
    for entry in index.get("trees", []):
        path = Path(base) / entry["path"]
        trees.append({"id": entry["id"], "sha256": entry["sha256"], "tree": load_tree(path).raw})
    return {"pyrtan-trees": FORMAT, "generated": index.get("generated"), "count": len(trees), "trees": trees}


# ---------------------------------------------------------------- reading


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file" or not parts.scheme:
        path = Path(urllib.request.url2pathname(parts.path) if parts.scheme else url)
        try:
            return path.read_bytes()
        except OSError as e:
            raise CatalogError(f"{url}: {e}") from e
    if parts.scheme not in ("http", "https"):
        raise CatalogError(f"{url}: a catalog is reached over https (or a file: path)")
    req = urllib.request.Request(url, headers={"User-Agent": "pyrtan-catalog/0.9", "Accept": "application/json, application/toml, text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise CatalogError(f"{url}: {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise CatalogError(f"{url}: {e}") from e


def _join(base: str, rel: str) -> str:
    base = base if base.endswith("/") else base + "/"
    if urllib.parse.urlsplit(base).scheme in ("http", "https", "file"):
        if "://" in rel or rel.startswith("/") or ".." in rel.split("/"):
            raise CatalogError(f"index points outside the catalog: {rel!r}")
        return urllib.parse.urljoin(base, rel)
    return str(Path(base) / rel)


class Catalog:
    """A catalog at a URL (or a directory), read lazily."""

    def __init__(self, base: str | None = None) -> None:
        self.base = (base or catalog_url()).rstrip("/") + "/"
        self._index: dict[str, Any] | None = None

    @property
    def index(self) -> dict[str, Any]:
        if self._index is None:
            raw = _fetch(_join(self.base, INDEX))
            try:
                index = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise CatalogError(f"{self.base}{INDEX}: not JSON: {e}") from e
            if not isinstance(index, dict) or index.get("pyrtan-catalog") != FORMAT or not isinstance(index.get("trees"), list):
                raise CatalogError(f"{self.base}{INDEX}: not a pyrtan catalog index (format {FORMAT})")
            self._index = index
        return self._index

    def entries(self) -> list[dict[str, Any]]:
        out = []
        for e in self.index["trees"]:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                e.setdefault("namespace", "")
                e.setdefault("id", f"{e['namespace']}/{e['name']}" if e["namespace"] else e["name"])
                out.append(e)
        return out

    def search(self, query: str = "") -> list[dict[str, Any]]:
        """Entries whose id, description, tags, hosts, sources or triggers
        mention every word of ``query``. An empty query lists everything."""
        words = [w.lower() for w in query.split()]
        out: list[dict[str, Any]] = []
        for e in self.entries():
            hay = " ".join([e["id"], str(e.get("description", "")), " ".join(e.get("tags", [])), " ".join(e.get("hosts", [])), " ".join(e.get("sources", [])), " ".join(e.get("triggers", []))]).lower()
            if all(w in hay for w in words):
                out.append(e)
        return out

    def find(self, spec: str) -> dict[str, Any]:
        """The entry for ``[namespace/]name[@version]``. A bare name is enough
        when one namespace has it; with two, the caller is told which."""
        m = SPEC.match(spec)
        if not m:
            raise CatalogError(f"{spec!r}: expected [NAMESPACE/]NAME[@VERSION]")
        namespace, name, version = m.group(1), m.group(2), m.group(3)
        have = [e for e in self.entries() if e["name"] == name and (namespace is None or e["namespace"] == namespace)]
        if not have:
            raise CatalogError(f"{spec!r} is not in the catalog at {self.base}")
        if len({e["id"] for e in have}) > 1:
            raise CatalogError(f"{name!r} is published by more than one namespace: {', '.join(sorted({e['id'] for e in have}))}; say which")
        for e in have:
            if version is None or e.get("version") == int(version):
                return e
        raise CatalogError(f"{have[0]['id']}: the catalog has version {have[0].get('version')}, not {version}")

    def fetch(self, entry: dict[str, Any]) -> tuple[Tree, bytes]:
        """The tree file an entry describes, verified against the entry."""
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            raise CatalogError(f"{entry['name']}: the index entry has no path")
        data = _fetch(_join(self.base, rel))
        digest = sha256(data)
        if entry.get("sha256") and digest != entry["sha256"]:
            raise CatalogError(f"{entry['name']}: the file at {rel} does not match the index (sha256 {digest[:12]}… vs {str(entry['sha256'])[:12]}…); refusing it")
        try:
            tree = load_tree_text(data.decode("utf-8"), source=f"{self.base}{rel}")
        except (TreeError, UnicodeDecodeError) as e:
            raise CatalogError(f"{entry['name']}: the file does not load as a tree: {e}") from e
        if tree.name != entry["name"]:
            raise CatalogError(f"{entry['name']}: the file says it is {tree.name!r}; refusing it")
        if entry.get("namespace") and tree.namespace != entry["namespace"]:
            raise CatalogError(f"{entry.get('id', entry['name'])}: the file says its namespace is {tree.namespace!r}; refusing it")
        if entry.get("version") is not None and tree.version != entry["version"]:
            raise CatalogError(f"{entry['name']}: the file is version {tree.version}, the index says {entry['version']}; refusing it")
        return tree, data

    def pull(self, spec: str, into: str | Path, *, force: bool = False) -> Path:
        """Fetch ``spec`` and write it as ``into/NAME.toml``. Refuses to
        overwrite a file unless ``force``; a tree someone edited locally is
        theirs."""
        entry = self.find(spec)
        tree, data = self.fetch(entry)
        into = Path(into)
        target = into / f"{tree.name}.toml"
        if target.exists() and not force:
            if target.read_bytes() == data:
                return target
            raise CatalogError(f"{target} exists and differs from the catalog's; pass --force to replace it")
        into.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target


# ---------------------------------------------------------------- publishing

OWNERS = "OWNERS"
EXAMPLES_NAMESPACE = "pyrtan"


def owners(namespace_dir: str | Path) -> list[str]:
    """The GitHub logins in ``catalog/<namespace>/OWNERS``: one per line,
    ``#`` starts a comment."""
    path = Path(namespace_dir) / OWNERS
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.lstrip("@").lower())
    return out


def gate(root: str | Path, *, changed: list[str] | None = None, author: str | None = None, base: dict[str, tuple[int, bytes]] | None = None) -> list[Problem]:
    """The checks a pull request to the catalog repository must pass. The
    repository is ``root``: its ``examples/`` are the ``pyrtan`` namespace,
    its ``catalog/<namespace>/<name>.toml`` files everyone else's, each
    namespace with an ``OWNERS`` file of GitHub logins.

    Every tree must load clean, carry ``license`` and ``namespace``, sit in
    the directory its namespace names under the file its name names, and
    ids must be unique across both places. Given ``changed`` (paths the
    pull request touches) and ``author`` (its GitHub login), a change under
    ``catalog/<namespace>/`` must come from one of that namespace's owners.
    Given ``base`` (``path -> (version, bytes)`` on the base branch), a tree
    whose file changed must have bumped its version. Returns the problems;
    none means the gate is open.
    """
    import tempfile

    root = Path(root)
    problems: list[Problem] = []
    examples = root / "examples"
    catalog = root / "catalog"
    roots = [d for d in (examples, catalog) if d.is_dir()]
    if not roots:
        return [Problem(str(root), "no examples/ or catalog/ directory: is this a checkout of the pyrtan repository?")]
    with tempfile.TemporaryDirectory() as tmp:
        _, problems = build_index(roots, require_license=True, require_namespace=True, copy_to=tmp)
    if examples.is_dir():
        for path in sorted(examples.rglob("*.toml")):
            try:
                tree = load_tree(path)
            except TreeError:
                continue  # already reported
            if tree.namespace != EXAMPLES_NAMESPACE:
                problems.append(Problem(path.relative_to(root).as_posix(), f"a tree under examples/ is published by {EXAMPLES_NAMESPACE!r}, not {tree.namespace!r}"))
    if catalog.is_dir():
        for ns_dir in sorted(catalog.iterdir()):
            rel = ns_dir.relative_to(root).as_posix()
            if ns_dir.name.startswith(".") or ns_dir.name == "README.md":
                continue
            if not ns_dir.is_dir():
                problems.append(Problem(rel, "only namespace directories belong directly under catalog/"))
                continue
            if not re.match(r"^[a-z0-9][a-z0-9-]*$", ns_dir.name):
                problems.append(Problem(rel, "a namespace is lowercase letters, digits and dashes"))
            if not owners(ns_dir):
                problems.append(Problem(f"{rel}/{OWNERS}", "a namespace needs an OWNERS file with at least one GitHub login"))
            for path in sorted(ns_dir.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                prel = path.relative_to(root).as_posix()
                if path.name == OWNERS and path.parent == ns_dir:
                    continue
                if path.suffix != ".toml" or path.parent != ns_dir:
                    problems.append(Problem(prel, "only <name>.toml files and OWNERS belong in a namespace directory"))
                    continue
                try:
                    tree = load_tree(path)
                except TreeError:
                    continue
                if tree.namespace != ns_dir.name:
                    problems.append(Problem(prel, f"the tree says namespace {tree.namespace!r}; it sits under catalog/{ns_dir.name}/"))
                if path.stem != tree.name:
                    problems.append(Problem(prel, f"the tree is named {tree.name!r}; its file must be {tree.name}.toml"))
    if changed and author:
        login = author.lstrip("@").lower()
        for c in changed:
            parts = Path(c).as_posix().split("/")
            if len(parts) >= 3 and parts[0] == "catalog":
                own = owners(catalog / parts[1])
                if own and login not in own:
                    problems.append(Problem(c, f"{author} is not in catalog/{parts[1]}/{OWNERS}; only a namespace's owners change it"))
    for c, (base_version, base_bytes) in (base or {}).items():
        path = root / c
        if not path.is_file():
            continue  # a removal
        if path.read_bytes() == base_bytes:
            continue
        try:
            tree = load_tree(path)
        except TreeError:
            continue
        if tree.version <= base_version:
            problems.append(Problem(c, f"the file changed but version is still {tree.version}; bump it so pulls notice"))
    return problems


def publish(file: str | Path, checkout: str | Path, *, owners: list[str] | None = None) -> tuple[Path, list[str]]:
    """Put a tree into ``checkout/catalog/<namespace>/<name>.toml``, write
    OWNERS for a new namespace, and run the gate. Returns the placed path
    and notes for the person. The pull request they open is the publishing."""
    logins = [o.lstrip("@").lower() for o in (owners or [])]
    src = Path(file)
    try:
        tree = load_tree(src)
    except TreeError as e:
        raise CatalogError(str(e)) from e
    errors = [p for p in check(tree) if p.level == "error"]
    if errors:
        raise CatalogError(f"{src}: " + "; ".join(f"{p.where}: {p.message}" for p in errors))
    if not tree.namespace:
        raise CatalogError(f"{src}: add a line  namespace = \"you\"  (lowercase; it is who publishes the tree, and the catalog id becomes you/{tree.name})")
    if not tree.license:
        raise CatalogError(f"{src}: add a line  license = \"MIT\"  (an SPDX identifier: what others may do with it)")
    catalog = Path(checkout) / "catalog"
    if not catalog.is_dir():
        raise CatalogError(f"{checkout}: no catalog/ directory; is this a checkout of github.com/b-i-t-8/pyrtan?")
    ns_dir = catalog / tree.namespace
    notes: list[str] = []
    if not ns_dir.is_dir():
        if not logins:
            raise CatalogError(f"catalog/{tree.namespace}/ is new: say who owns it with --owner YOUR-GITHUB-LOGIN")
        ns_dir.mkdir(parents=True)
        (ns_dir / OWNERS).write_text("# GitHub logins allowed to change this namespace, one per line.\n" + "".join(f"{o}\n" for o in logins), encoding="utf-8")
        notes.append(f"new namespace catalog/{tree.namespace}/ owned by {', '.join(logins)}")
    elif logins:
        have = globals()["owners"](ns_dir)
        missing = [o for o in logins if o not in have]
        if missing:
            with (ns_dir / OWNERS).open("a", encoding="utf-8") as f:
                f.write("".join(f"{o}\n" for o in missing))
            notes.append(f"added {', '.join(missing)} to catalog/{tree.namespace}/{OWNERS}")
    target = ns_dir / f"{tree.name}.toml"
    data = src.read_bytes()
    if target.exists():
        if target.read_bytes() == data:
            notes.append(f"{target.relative_to(checkout)} already holds this exact file")
        else:
            old = load_tree(target)
            if tree.version <= old.version:
                raise CatalogError(f"catalog/{tree.namespace}/{tree.name}.toml is at version {old.version}; bump this file's version past it")
            notes.append(f"replaces version {old.version} with {tree.version}")
    target.write_bytes(data)
    problems = gate(checkout)
    if problems:
        raise CatalogError("the gate refuses the checkout as it now stands:\n  " + "\n  ".join(str(p) for p in problems))
    return target.resolve(), notes
