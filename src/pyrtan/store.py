"""Blobs a hub keeps: its own, and the ones it keeps for others.

A *blob* is a JSON value under a key (``directory``, ``gaps``,
``traces/2026-10-25``), with its SHA-256, size and time. A hub keeps its
own blobs so it can hand them to the members and hubs that follow it, and
keeps blobs *for* the hubs and members it shares a secret with, so that
when their box dies the blobs are still somewhere. Nothing secret is ever
a blob: the state file, with the pair secrets, never goes through here.

On disk (``--store DIR``) the layout is ``own/<key>.json`` and
``kept/<actor-hash>/<key>.json``; without a directory the store lives in
memory and is lost with the process, which is enough to try the loop.
Limits are fixed and small on purpose: a blob is at most 256 KB, an
actor may leave at most 200 keys and 64 MB here. A keeper that is asked
for more says so and keeps nothing extra.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

MAX_BLOB = 256 * 1024
MAX_KEYS_PER_ACTOR = 200
MAX_BYTES_PER_ACTOR = 64 * 1024 * 1024
KEY = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,99}$")


class StoreError(ValueError):
    pass


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by its owner only (0600), whole or not at
    all: a temporary file beside it, replaced in one step. For files that hold
    secrets: federation state (pair and network secrets), grants."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(temp, 0o600)  # in case the file was there already, with other modes
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def digest(data: Any) -> tuple[str, int]:
    raw = canonical(data)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def check_key(key: Any) -> str:
    if not isinstance(key, str) or not KEY.match(key) or ".." in key:
        raise StoreError(f"{key!r} is not a key: lowercase letters, digits, dot, dash, underscore, slash; at most 100 characters")
    return key


def _safe(key: str) -> str:
    return key.replace("/", "__")


def _actor_dir(actor: str) -> str:
    return hashlib.sha256(actor.rstrip("/").encode("utf-8")).hexdigest()[:16]


class BlobStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else None
        self._own: dict[str, dict[str, Any]] = {}                 # key -> blob
        self._kept: dict[str, dict[str, dict[str, Any]]] = {}      # actor -> key -> blob
        self._lock = threading.RLock()
        if self.root is not None:
            self._load()

    # ------------------------------------------------------------ disk

    def _load(self) -> None:
        assert self.root is not None
        for path in sorted((self.root / "own").glob("*.json")) if (self.root / "own").is_dir() else []:
            blob = self._read(path)
            if blob:
                self._own[blob["key"]] = blob
        kept = self.root / "kept"
        for actor_dir in sorted(kept.iterdir()) if kept.is_dir() else []:
            for path in sorted(actor_dir.glob("*.json")):
                blob = self._read(path)
                if blob and isinstance(blob.get("actor"), str):
                    self._kept.setdefault(blob["actor"], {})[blob["key"]] = blob

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return blob if isinstance(blob, dict) and isinstance(blob.get("key"), str) else None

    def _write(self, path: Path, blob: dict[str, Any]) -> None:
        if self.root is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _remove(self, path: Path) -> None:
        if self.root is not None:
            try:
                path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ own

    @staticmethod
    def meta(blob: dict[str, Any]) -> dict[str, Any]:
        return {k: blob[k] for k in ("key", "sha256", "size", "updated") if k in blob}

    def put_own(self, key: str, data: Any) -> dict[str, Any]:
        key = check_key(key)
        sha, size = digest(data)
        if size > MAX_BLOB:
            raise StoreError(f"{key}: {size} bytes; a blob is at most {MAX_BLOB}")
        with self._lock:
            current = self._own.get(key)
            if current is not None and current["sha256"] == sha:
                return self.meta(current)
            blob = {"key": key, "sha256": sha, "size": size, "updated": time.time(), "data": data}
            self._own[key] = blob
            if self.root is not None:
                self._write(self.root / "own" / f"{_safe(key)}.json", blob)
            return self.meta(blob)

    def get_own(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            blob = self._own.get(key)
            return dict(blob) if blob else None

    def list_own(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.meta(b) for _, b in sorted(self._own.items())]

    # ------------------------------------------------------------ kept for others

    def put_kept(self, actor: str, key: str, data: Any, *, sha256: str | None = None, updated: float | None = None) -> dict[str, Any]:
        key = check_key(key)
        actor = actor.rstrip("/")
        sha, size = digest(data)
        if sha256 is not None and sha256 != sha:
            raise StoreError(f"{key}: the data does not match its sha256")
        if size > MAX_BLOB:
            raise StoreError(f"{key}: {size} bytes; a blob is at most {MAX_BLOB}")
        with self._lock:
            mine = self._kept.setdefault(actor, {})
            others = sum(b["size"] for k, b in mine.items() if k != key)
            if key not in mine and len(mine) >= MAX_KEYS_PER_ACTOR:
                raise StoreError(f"{actor} already has {MAX_KEYS_PER_ACTOR} keys here")
            if others + size > MAX_BYTES_PER_ACTOR:
                raise StoreError(f"{actor} would have more than {MAX_BYTES_PER_ACTOR} bytes here")
            blob = {"actor": actor, "key": key, "sha256": sha, "size": size, "updated": float(updated or time.time()), "kept_at": time.time(), "data": data}
            mine[key] = blob
            if self.root is not None:
                self._write(self.root / "kept" / _actor_dir(actor) / f"{_safe(key)}.json", blob)
            return {**self.meta(blob), "actor": actor}

    def get_kept(self, actor: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            blob = self._kept.get(actor.rstrip("/"), {}).get(key)
            return dict(blob) if blob else None

    def list_kept(self, actor: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            actors = [actor.rstrip("/")] if actor else sorted(self._kept)
            return [{**self.meta(b), "actor": a} for a in actors for _, b in sorted(self._kept.get(a, {}).items())]

    def drop_kept(self, actor: str) -> int:
        actor = actor.rstrip("/")
        with self._lock:
            mine = self._kept.pop(actor, {})
            for key in mine:
                self._remove((self.root or Path(".")) / "kept" / _actor_dir(actor) / f"{_safe(key)}.json") if self.root else None
            return len(mine)

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return {
                "own": len(self._own),
                "kept": sum(len(m) for m in self._kept.values()),
                "kept_bytes": sum(b["size"] for m in self._kept.values() for b in m.values()),
                "actors": len(self._kept),
                "root": str(self.root) if self.root else None,
            }
