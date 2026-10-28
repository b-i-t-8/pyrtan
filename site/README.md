# pyrtan.com

pyrtan.com is this repository's GitHub Pages site. Nothing of ours runs:
on every push to `main`, and once an hour, the `pages` workflow builds a
static directory and publishes it.

| Path | What | Built from |
| :-- | :-- | :-- |
| `/` | the landing page, with the live directory read from the listed hubs in the visitor's browser | `site/index.html` |
| `/console/` | the console (installable: manifest, service worker and `pyrtan.js`, the tree runtime, beside it). Until it joins a hub the browser is one: it holds the catalog's trees (`/catalog/trees.json`) and runs them in the page. To join any hub started with `--cors https://pyrtan.com`, give its URL and token; its Member tab then joins that hub, and runs its trees when joined with the token or as a key the hub grants `run`; its Identity tab makes, backs up and restores an identity, claims names and pairs devices, with no shell | `pyrtan console --out` |
| `/catalog/` | the trees people can `pyrtan pull`, as `namespace/name.toml`, `index.json` the command reads, and `trees.json`, the same trees as JSON for the console | `pyrtan index examples catalog --copy`, through `site/gate.py` |
| `/theme.js` | the wordmark's colour picker the landing page and the catalog load (the console carries its own copy inline) | `src/pyrtan/js/theme.js` |
| `/rendezvous/network.json` | the default network: its name, and a secret that is public on purpose; built into pyrtan, published here as a mirror (and for the snapshot) | `site/rendezvous/network.json` |
| `/rendezvous/hubs.json` | the hub list: hubs operators added by pull request, which the snapshot asks first and `pyrtan join` tries first | `site/rendezvous/hubs.json` |
| `/rendezvous/directory.json` | a snapshot of the default network's directory, from every hub the build can reach, refreshed hourly | `site/snapshot.py` |
| `/rendezvous/reports.json` | the gap reports the hubs carry for catalog trees, refreshed hourly | `site/snapshot.py --reports` |
| `/.well-known/pyrtan/` | names: `name/<login>.json` (a claim signed by the identity it names), `id/<56>.json` (identity documents), `names.json` (every claim), for `pyrtan id resolve <login>@pyrtan.com` | `pyrtan id wellknown site/names`, after `site/gate.py`; see [`names/README.md`](names/README.md) |

`site/build.sh out` builds the same thing locally; open `out/index.html`.

## Turning it on

1. In the repository's *Settings › Pages*, set *Source* to **GitHub
   Actions**. Push to `main` (or run the `pages` workflow by hand); the
   site is up at `https://b-i-t-8.github.io/pyrtan/`.
2. *Settings › Pages › Custom domain*: `pyrtan.com` (`site/CNAME` carries
   it so a deploy never drops it). Tick *Enforce HTTPS* once DNS resolves.
3. At your DNS provider, for `pyrtan.com`:

   | Record | Name | Value |
   | :-- | :-- | :-- |
   | `A` | `@` | `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153` |
   | `AAAA` | `@` | `2606:50c0:8000::153`, `2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153` |
   | `CNAME` | `www` | `b-i-t-8.github.io` |

   (GitHub's current addresses; *Pages › Custom domain* in the settings
   says if they change.)

Then `curl https://pyrtan.com/catalog/index.json` returns the index,
`pyrtan search weather` from anywhere finds it, and `pyrtan pull weather`
writes `trees/weather.toml`.

## The catalog

The catalog is `examples/` (the `pyrtan` namespace) and `catalog/`
(everyone else's, one directory per namespace with an `OWNERS` file),
served as `/catalog/<namespace>/<name>.toml` beside `index.json`. A tree
goes in by pull request: `pyrtan publish my-tree.toml --into <checkout>
--owner <login>` places it and runs the gate, the `catalog-gate` workflow runs
main's copy of the same gate on the pull request, reading the pull request's
tree as data and running none of it (`site/gate.py`: clean, licensed,
namespaced, in the right place, unique id, changed only by an owner,
version bumped), and a maintainer merges. The `pages` workflow runs the
gate and the tests again before it publishes, so what is at `/catalog/`
always passes. See [`catalog/README.md`](../catalog/README.md) and
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

`rendezvous/reports.json` is baked beside the directory snapshot: the gap
reports the hubs carry (from hubs that opted in with `--report-gaps`),
merged hourly, which the catalog page shows beside each tree and `pyrtan
reports` reads when given no hub.

## The default network and the hub list

`site/rendezvous/network.json` is the default network: the name
`pyrtan` and a secret that is public on purpose (it names the network,
it guards nothing; see `_meta/federation.md` §7a). pyrtan has the same
two values built in, and a test holds them equal; this file is a mirror,
and what the snapshot keeps the network's part by. `pyrtan join` with
no invite, `pyrtan hub --join default` and `$PYRTAN_JOIN=default` put a
hub in it without reading it.

pyrtan.com runs no hub of its own; it is these static files, and the
hub list is how it learns that a hub exists. `site/rendezvous/hubs.json`
lists hubs operators put in by pull request: each a federated hub in the
default network (`--join default`), started with `--listed` and `--cors
https://pyrtan.com`, reachable from outside (a tunnel or a port forward
behind TLS; [`deploy/README.md`](../deploy/README.md) says how). None
need be up always. The landing page asks each listed hub's `/directory`
from the visitor's browser, so the directory on pyrtan.com is live and
needs no backend; the console link on each row opens
`/console/?hub=<url>`. When none answers, or the list is empty, the page
shows the snapshot instead, marked with its time: `site/snapshot.py`
asks the listed hubs on every build, hourly on the schedule, then every
other hub it knows of, keeps the default network's part of what they
say, keeps the last published snapshot's entries for any hub that is
down, and drops entries unheard of for a week. `pyrtan join` tries the
snapshot's hubs after the listed ones, and `pyrtan directory` falls back
to it, so a new hub can join through any hub the snapshot knows of that
happens to be up.

## Later: hosting

Nothing here changes. A hosted hub is this repository's image started
with `--token`, `--cors https://pyrtan.com`, `--gaps` on a volume,
`--egress public` and `--federate --advertise https://<tenant>.pyrtan.com
--listed --state <volume>/federation.json`, behind TLS; the console
connects to it like to any other, a self-hosted hub can follow it or be
followed by it, and it can go on the hub list like any other. The control plane that
provisions it is a separate, private repository, per
`_meta/platform.md`.
