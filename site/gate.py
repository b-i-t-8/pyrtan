"""The catalog gate, as the workflows run it.

    uv run python site/gate.py [--base origin/main] [--author LOGIN] [--changed FILE ...]

Runs :func:`pyrtan.catalog.gate` on this checkout: every tree under
``examples/`` and ``catalog/`` loads clean with a licence and a namespace,
sits where its namespace and name say, ids are unique, each namespace has
owners. With ``--base`` it asks git which catalog files the pull request
changed and what version each had before, so a change must come from an
owner (``--author``, the pull request's login) and must bump the version.
Exit 1 with the problems listed, 0 when the gate is open.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pyrtan.catalog import gate  # noqa: E402


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="the base ref of the pull request (e.g. origin/main); changes and prior versions come from git")
    ap.add_argument("--author", help="the pull request's GitHub login; must own any namespace it changes")
    ap.add_argument("--changed", nargs="*", default=None, help="changed paths, when not taken from git")
    args = ap.parse_args(argv)
    changed = args.changed
    base: dict[str, tuple[int, bytes]] = {}
    if args.base:
        out = git("diff", "--name-only", f"{args.base}...HEAD")
        if out is None:
            print(f"gate: cannot diff against {args.base}; checking the tree without change information", file=sys.stderr)
        else:
            changed = [line.strip() for line in out.splitlines() if line.strip()] if changed is None else changed
            for path in changed:
                if not (path.startswith(("catalog/", "examples/")) and path.endswith(".toml")):
                    continue
                prior = subprocess.run(["git", "show", f"{args.base}:{path}"], cwd=ROOT, capture_output=True)
                if prior.returncode != 0:
                    continue  # new on this branch
                try:
                    version = int(tomllib.loads(prior.stdout.decode("utf-8")).get("version", 1))
                except Exception:
                    version = 1
                base[path] = (version, prior.stdout)
    problems = gate(ROOT, changed=changed, author=args.author, base=base)
    for p in problems:
        print(f"gate: {p}")
    scope = f"{len(changed)} changed file(s)" if changed is not None else "the whole checkout"
    print(f"gate: {'refused' if problems else 'open'} ({scope}" + (f", author {args.author}" if args.author else "") + ")")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
