"""The trees a service knows about, and how a sentence finds one.

A registry loads every ``*.toml`` under one or more directories, reloads a
file when its modification time changes (so trees can be edited while a
service runs), and matches intents against the triggers of everything
loaded.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .intent import Match, Trigger, best_matches
from .tree import Tree, TreeError, load_tree


class Ambiguous(LookupError):
    def __init__(self, text: str, matches: list[Match]) -> None:
        self.text = text
        self.matches = matches
        names = ", ".join(f"{m.tree} ({m.trigger.pattern!r})" for m in matches)
        super().__init__(f"{text!r} matches more than one tree equally well: {names}")


class NoMatch(LookupError):
    pass


@dataclass
class _Loaded:
    tree: Tree
    path: Path | None
    mtime: float


@dataclass
class Registry:
    directories: list[Path] = field(default_factory=list)
    _trees: dict[str, _Loaded] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    errors: dict[str, str] = field(default_factory=dict, init=False)

    @classmethod
    def from_paths(cls, paths: Iterable[str | Path]) -> "Registry":
        reg = cls()
        for p in paths:
            p = Path(p)
            if p.is_dir():
                reg.directories.append(p)
            else:
                reg.add_file(p)
        reg.reload()
        return reg

    # ------------------------------------------------------------ loading

    def add(self, tree: Tree) -> None:
        with self._lock:
            self._trees[tree.name] = _Loaded(tree, None, 0.0)

    def add_file(self, path: str | Path) -> Tree:
        path = Path(path)
        tree = load_tree(path)
        with self._lock:
            self._trees[tree.name] = _Loaded(tree, path, path.stat().st_mtime)
            self.errors.pop(str(path), None)
        return tree

    def reload(self) -> None:
        """Load new files, reload changed ones, drop deleted ones."""
        with self._lock:
            seen: set[Path] = set()
            for d in self.directories:
                for path in sorted(d.rglob("*.toml")):
                    if path.name == "pyproject.toml":
                        continue
                    seen.add(path)
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    current = next((l for l in self._trees.values() if l.path == path), None)
                    if current is not None and current.mtime == mtime:
                        continue
                    try:
                        tree = load_tree(path)
                    except TreeError as e:
                        self.errors[str(path)] = str(e)
                        continue
                    self.errors.pop(str(path), None)
                    if current is not None and current.tree.name != tree.name:
                        del self._trees[current.tree.name]
                    self._trees[tree.name] = _Loaded(tree, path, mtime)
            for name, loaded in list(self._trees.items()):
                if loaded.path is not None and loaded.path not in seen and any(loaded.path.is_relative_to(d) for d in self.directories):
                    if not loaded.path.exists():
                        del self._trees[name]

    # ------------------------------------------------------------ lookup

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._trees)

    def trees(self) -> list[Tree]:
        with self._lock:
            return [l.tree for _, l in sorted(self._trees.items())]

    def get(self, name: str) -> Tree | None:
        with self._lock:
            loaded = self._trees.get(name)
            return loaded.tree if loaded else None

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __len__(self) -> int:
        return len(self._trees)

    def candidates(self) -> list[tuple[str, Trigger]]:
        return [(t.name, trig) for t in self.trees() for trig in t.triggers]

    def match(self, text: str) -> Match:
        """The one tree that answers ``text``, or :class:`NoMatch` /
        :class:`Ambiguous`."""
        matches = best_matches(text, self.candidates())
        if not matches:
            raise NoMatch(f"no tree answers {text!r}")
        if len(matches) > 1:
            # Same tree matched twice at equal specificity: take the first.
            if len({m.tree for m in matches}) == 1:
                return matches[0]
            raise Ambiguous(text, matches)
        return matches[0]

    def capabilities(self) -> list[dict[str, Any]]:
        return [t.capability() for t in self.trees()]

    def gather(self, root: str | Path) -> int:
        """Add a directory and load it. Returns how many trees are loaded."""
        root = Path(root)
        if root not in self.directories:
            self.directories.append(root)
        self.reload()
        return len(self)


def default_paths() -> list[Path]:
    """Where ``pyrtan`` looks when no path is given: ``$PYRTAN_TREES`` (colon
    separated) or ``./trees``."""
    env = os.environ.get("PYRTAN_TREES")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    return [Path("trees")]
