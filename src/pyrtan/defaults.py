"""The default trees: what every console carries, so it needs no network.

A release ships the repository's ``examples/`` (the ``pyrtan`` namespace)
and ``catalog/`` (every other namespace) inside the package, under
``pyrtan/trees/``. In a checkout, where the package is ``src/pyrtan``, they
are read from the repository itself. :func:`bundle` turns them into what
the console embeds: the shape of a catalog's ``trees.json``, each tree's
own table as ``pyrtan check`` loaded it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .catalog import FORMAT
from .tree import TreeError, load_tree

PACKAGED = Path(__file__).resolve().parent / "trees"
CHECKOUT = Path(__file__).resolve().parents[2]


def default_tree_dirs() -> list[Path]:
    """The folders the default trees are in, examples first."""
    for root in (PACKAGED, CHECKOUT):
        found = [root / name for name in ("examples", "catalog") if (root / name).is_dir()]
        if found:
            return found
    return []


def bundle(paths: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Every tree under ``paths`` (the default trees when none are given), as
    ``trees.json`` holds them, by id. A file that does not load is left out,
    as a registry leaves it out; of two trees with one name, the later wins,
    as in a registry."""
    by_name: dict[str, dict[str, Any]] = {}
    for root in [Path(p) for p in (paths if paths is not None else default_tree_dirs())]:
        files = sorted(root.rglob("*.toml")) if root.is_dir() else [root]
        for path in files:
            if path.name == "pyproject.toml":
                continue
            try:
                tree = load_tree(path)
            except TreeError:
                continue
            by_name[tree.name] = {"id": tree.id, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "tree": tree.raw}
    trees = sorted(by_name.values(), key=lambda e: e["id"])
    return {"pyrtan-trees": FORMAT, "count": len(trees), "trees": trees}
