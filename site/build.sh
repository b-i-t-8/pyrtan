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
cp site/catalog/index.html "$out/catalog/"
pyrtan console --out "$out/console/index.html"
pyrtan check examples --quiet
pyrtan index examples --out "$out/catalog/index.json"
# Every tree file at the path the index names, next to it.
(cd examples && find . -name '*.toml' -not -name 'pyproject.toml' -exec sh -c 'mkdir -p "$0/$(dirname "$1")" && cp "$1" "$0/$1"' "$out/catalog" {} \;)
touch "$out/.nojekyll"
echo "built $out: $(find "$out" -type f | wc -l) files"
