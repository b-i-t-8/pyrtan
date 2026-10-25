#!/bin/sh
# Assemble pyrtan.com into one static directory: the landing page, the
# console, the catalog (built from examples/), the seed list. Run from the
# repository root with pyrtan installed (`uv run site/build.sh`); the Pages
# workflow runs the same thing.
set -eu
out="${1:-out}"
rm -rf "$out"
mkdir -p "$out/console" "$out/catalog" "$out/rendezvous"
out="$(cd "$out" && pwd)"
cp site/index.html site/CNAME "$out/"
cp site/rendezvous/seeds.json "$out/rendezvous/"
# The directory as the seeds know it now, keeping the last published
# snapshot's entries when a seed is down (SNAPSHOT_PREVIOUS may add more).
python3 site/snapshot.py site/rendezvous/seeds.json "$out/rendezvous/directory.json" \
  --previous https://pyrtan.com/rendezvous/directory.json \
  --previous https://b-i-t-8.github.io/pyrtan/rendezvous/directory.json \
  ${SNAPSHOT_PREVIOUS:+--previous "$SNAPSHOT_PREVIOUS"}
cp site/catalog/index.html "$out/catalog/"
pyrtan console --out "$out/console/index.html"
pyrtan check examples --quiet
pyrtan index examples --out "$out/catalog/index.json"
# Every tree file at the path the index names, next to it.
(cd examples && find . -name '*.toml' -not -name 'pyproject.toml' -exec sh -c 'mkdir -p "$0/$(dirname "$1")" && cp "$1" "$0/$1"' "$out/catalog" {} \;)
touch "$out/.nojekyll"
echo "built $out: $(find "$out" -type f | wc -l) files"
