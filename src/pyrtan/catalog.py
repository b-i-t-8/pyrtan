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
     "trees": [{"name": "weather", "version": 3, "description": "...",
                "tags": [...], "hosts": [...], "triggers": [...],
                "license": "MIT", "path": "weather/weather.toml",
                "sha256": "...", "size": 1234}]}

``path`` is relative to the index. A pull fetches the file, checks its
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
NAME_AT_VERSION = re.compile(r"^([a-z0-9][a-z0-9-]*)(?:@(\d+))?$")


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
        "name": tree.name,
        "version": tree.version,
        "description": tree.description,
        "tags": tree.tags,
        "hosts": tree.hosts,
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


def build_index(root: str | Path, *, require_license: bool = True) -> tuple[dict[str, Any], list[Problem]]:
    """Walk ``root`` for tree files and describe each in an index.

    Returns the index and the problems that kept a file out of it: a file
    that does not load, two files with the same name, or (when required) a
    tree with no ``license``. The caller decides whether problems fail the
    build; the catalog's CI does.
    """
    root = Path(root)
    trees: list[dict[str, Any]] = []
    problems: list[Problem] = []
    seen: dict[str, str] = {}
    for path in sorted(root.rglob("*.toml")):
        if path.name == "pyproject.toml" or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            tree = load_tree(path)
        except TreeError as e:
            problems.append(Problem(rel, str(e)))
            continue
        errors = [p for p in check(tree) if p.level == "error"]
        if errors:
            problems.append(Problem(rel, "; ".join(f"{p.where}: {p.message}" for p in errors)))
            continue
        if tree.name in seen:
            problems.append(Problem(rel, f"a tree named {tree.name!r} is already at {seen[tree.name]}"))
            continue
        if require_license and not tree.license:
            problems.append(Problem(rel, "no 'license': a catalog tree must say what others may do with it"))
            continue
        seen[tree.name] = rel
        trees.append(entry_for(tree, path.read_bytes(), rel))
    index = {
        "pyrtan-catalog": FORMAT,
        "generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(trees),
        "trees": trees,
    }
    return index, problems


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
    req = urllib.request.Request(url, headers={"User-Agent": "pyrtan-catalog/0.6", "Accept": "application/json, application/toml, text/plain, */*"})
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
        return [e for e in self.index["trees"] if isinstance(e, dict) and isinstance(e.get("name"), str)]

    def search(self, query: str = "") -> list[dict[str, Any]]:
        """Entries whose name, description, tags, hosts or triggers mention
        every word of ``query``. An empty query lists everything."""
        words = [w.lower() for w in query.split()]
        out: list[dict[str, Any]] = []
        for e in self.entries():
            hay = " ".join([e["name"], str(e.get("description", "")), " ".join(e.get("tags", [])), " ".join(e.get("hosts", [])), " ".join(e.get("triggers", []))]).lower()
            if all(w in hay for w in words):
                out.append(e)
        return out

    def find(self, spec: str) -> dict[str, Any]:
        """The entry for ``name`` or ``name@version``."""
        m = NAME_AT_VERSION.match(spec)
        if not m:
            raise CatalogError(f"{spec!r}: expected NAME or NAME@VERSION")
        name, version = m.group(1), m.group(2)
        for e in self.entries():
            if e["name"] == name and (version is None or e.get("version") == int(version)):
                return e
        have = [e for e in self.entries() if e["name"] == name]
        if have and version is not None:
            raise CatalogError(f"{name}: the catalog has version {have[0].get('version')}, not {version}")
        raise CatalogError(f"{name!r} is not in the catalog at {self.base}")

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
