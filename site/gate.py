"""The catalog gate, as the workflows run it.

    uv run python site/gate.py [--root CHECKOUT] [--base origin/main] [--author LOGIN] [--author-id ID] [--changed FILE ...]

Runs :func:`pyrtan.catalog.gate` on a checkout (``--root``, default this
one): every tree under
``examples/`` and ``catalog/`` loads clean with a licence and a namespace,
sits where its namespace and name say, ids are unique, each namespace has
owners. With ``--base`` it asks git which catalog files the pull request
changed and what version each had before, so a change must come from an
owner (``--author``, the pull request's login, and ``--author-id``, its
numeric id, which is what an OWNERS line with an id is matched by) and
must bump the version. Who owns a namespace, or ``examples/``, is what
its OWNERS says on the base branch, not in the pull request, which may
rewrite or remove that file.

Then :func:`pyrtan.names.gate` on ``site/names/``: every name claim and
identity document holds, a name is a GitHub login, one per account. With
``--base``, a new name must be the author's own login carrying the
author's numeric id (``--author-id``), and a name may be changed or
removed only by the account whose id it carried on the base branch.
Exit 1 with the problems listed, 0 when the gate is open.

The code that judges is always this file's own checkout (its ``src/``);
``--root`` is only what is judged. The ``catalog-gate`` workflow runs
main's copy of this file against a pull request's tree, so a pull request
cannot change the gate that judges it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pyrtan.catalog import OWNERS, gate, parse_owner_entries  # noqa: E402
from pyrtan.names import NAMES_DIR, gate as names_gate  # noqa: E402


def git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="the checkout to judge (default: this file's own); the gate's code is always this file's checkout")
    ap.add_argument("--base", help="the base ref of the pull request (e.g. origin/main); changes and prior versions come from git")
    ap.add_argument("--author", help="the pull request's GitHub login; must own any namespace it changes")
    ap.add_argument("--author-id", type=int, help="the pull request author's numeric GitHub id; must own any name it changes")
    ap.add_argument("--changed", nargs="*", default=None, help="changed paths, when not taken from git")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    changed = args.changed
    base: dict[str, tuple[int, bytes]] = {}
    base_owners: dict[str, list] | None = None
    base_examples: list | None = None
    names_base: dict[str, bytes] = {}
    if args.base:
        # --no-renames: a rename is a removal and an addition, and both are judged.
        out = git(root, "diff", "--name-only", "--no-renames", f"{args.base}...HEAD")
        if out is None:
            print(f"gate: cannot diff against {args.base}; checking the tree without change information", file=sys.stderr)
        else:
            changed = [line.strip() for line in out.splitlines() if line.strip()] if changed is None else changed
            base_owners = {}
            for path in changed:
                parts = path.split("/")
                if len(parts) >= 3 and parts[0] == "catalog" and parts[1] not in base_owners:
                    # The namespace's owners as the base branch has them: the pull request's own OWNERS is part of what is judged.
                    prior = subprocess.run(["git", "show", f"{args.base}:catalog/{parts[1]}/{OWNERS}"], cwd=root, capture_output=True)
                    if prior.returncode == 0:
                        base_owners[parts[1]] = parse_owner_entries(prior.stdout.decode("utf-8", "replace"))
                if parts[0] == "examples" and base_examples is None:
                    # Likewise for examples/: the trees every console carries are its owners' to change.
                    prior = subprocess.run(["git", "show", f"{args.base}:examples/{OWNERS}"], cwd=root, capture_output=True)
                    if prior.returncode == 0:
                        base_examples = parse_owner_entries(prior.stdout.decode("utf-8", "replace"))
                if path.startswith(f"{NAMES_DIR}/"):
                    # A name's claim on the base branch says whose it is, for its .id.json too.
                    claim = path[: -len(".id.json")] + ".json" if path.endswith(".id.json") else path
                    prior = subprocess.run(["git", "show", f"{args.base}:{claim}"], cwd=root, capture_output=True)
                    if prior.returncode == 0:
                        names_base[claim] = prior.stdout
                    continue
                if not (path.startswith(("catalog/", "examples/")) and path.endswith(".toml")):
                    continue
                prior = subprocess.run(["git", "show", f"{args.base}:{path}"], cwd=root, capture_output=True)
                if prior.returncode != 0:
                    continue  # new on this branch
                try:
                    version = int(tomllib.loads(prior.stdout.decode("utf-8")).get("version", 1))
                except Exception:
                    version = 1
                base[path] = (version, prior.stdout)
    problems = gate(root, changed=changed, author=args.author, base=base, base_owners=base_owners, author_id=args.author_id, base_examples=base_examples)
    problems += names_gate(root, changed=changed, author=args.author, author_id=args.author_id, base=names_base)
    for p in problems:
        print(f"gate: {p}")
    scope = f"{len(changed)} changed file(s)" if changed is not None else "the whole checkout"
    print(f"gate: {'refused' if problems else 'open'} ({scope}" + (f", author {args.author}" if args.author else "") + ")")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
