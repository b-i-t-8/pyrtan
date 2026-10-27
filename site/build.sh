#!/bin/sh
# Assemble pyrtan.com into one static directory: the landing page, the
# console, the catalog (built from examples/ and catalog/), the seed list. Run from the
# repository root with pyrtan installed (`uv run site/build.sh`); the Pages
# workflow runs the same thing.
set -eu
out="${1:-out}"
rm -rf "$out"
mkdir -p "$out/console" "$out/catalog" "$out/rendezvous"
out="$(cd "$out" && pwd)"
cp site/index.html site/CNAME "$out/"
cp site/rendezvous/seeds.json site/rendezvous/network.json "$out/rendezvous/"
# The directory as the hubs know it now (the seeds, then every hub the
# last snapshot or a hub names), the default network's part only, keeping
# the last published snapshot's entries for hubs that are down
# (SNAPSHOT_PREVIOUS may add more).
python3 site/snapshot.py site/rendezvous/seeds.json "$out/rendezvous/directory.json" \
  --network site/rendezvous/network.json \
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
