#!/bin/sh
# Assemble pyrtan.com into one static directory: the landing page, the
# console, the catalog (built from examples/ and catalog/), the seed list, the source. Run from the
# repository root with pyrtan installed (`uv run site/build.sh`); the Pages
# workflow runs the same thing.
set -eu
out="${1:-out}"
rm -rf "$out"
mkdir -p "$out/console" "$out/catalog" "$out/rendezvous" "$out/source"
out="$(cd "$out" && pwd)"
cp site/CNAME "$out/"
# The source: files, releases, branches and history, from this checkout's git.
python3 site/source.py "$out/source"
# The landing page carries the version of the pyrtan it was built with.
version="$(pyrtan --version | cut -d' ' -f2)"
sed "s/__VERSION__/$version/g" site/index.html > "$out/index.html"
# The wordmark's colour picker, shared by the landing page and the catalog (the console inlines it).
cp src/pyrtan/js/theme.js "$out/"
cp site/rendezvous/seeds.json "$out/rendezvous/"
# The directory as the seeds know it now, keeping the last published
# snapshot's entries when a seed is down (SNAPSHOT_PREVIOUS may add more).
python3 site/snapshot.py site/rendezvous/seeds.json "$out/rendezvous/directory.json" \
  --previous https://pyrtan.com/rendezvous/directory.json \
  --previous https://b-i-t-8.github.io/pyrtan/rendezvous/directory.json \
  ${SNAPSHOT_PREVIOUS:+--previous "$SNAPSHOT_PREVIOUS"} \
  --reports "$out/rendezvous/reports.json" \
  --previous-reports https://pyrtan.com/rendezvous/reports.json \
  --previous-reports https://b-i-t-8.github.io/pyrtan/rendezvous/reports.json
cp site/catalog/index.html "$out/catalog/"
pyrtan console --out "$out/console/index.html"
pyrtan check examples catalog --quiet
python3 site/gate.py
# The index and every tree file beside it, as <namespace>/<name>.toml.
pyrtan index examples catalog --copy "$out/catalog" --require-namespace
touch "$out/.nojekyll"
echo "built $out: $(find "$out" -type f | wc -l) files"
