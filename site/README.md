# pyrtan.com

pyrtan.com is this repository's GitHub Pages site. Nothing of ours runs:
on every push to `main`, and once an hour, the `pages` workflow builds a
static directory and publishes it.

| Path | What | Built from |
| :-- | :-- | :-- |
| `/` | the landing page, with the live directory read from the seeds in the visitor's browser | `site/index.html` |
| `/console/` | the console (installable: manifest, service worker and `pyrtan.js`, the tree runtime, beside it); point it at any hub started with `--cors https://pyrtan.com`; its Member tab joins that hub, and runs its trees when joined with the token | `pyrtan console --out` |
| `/catalog/` | the trees people can `pyrtan pull`, as `namespace/name.toml`, and `index.json` the command reads | `pyrtan index examples catalog --copy`, through `site/gate.py` |
| `/theme.js` | the wordmark's colour picker the landing page and the catalog load (the console carries its own copy inline) | `src/pyrtan/js/theme.js` |
| `/rendezvous/network.json` | the default network: its name, and a secret that is public on purpose | `site/rendezvous/network.json` |
| `/rendezvous/seeds.json` | hubs to follow first | `site/rendezvous/seeds.json` |
| `/rendezvous/directory.json` | a snapshot of the default network's directory, from every hub the build can reach, refreshed hourly | `site/snapshot.py` |
| `/rendezvous/reports.json` | the gap reports the seeds carry for catalog trees, refreshed hourly | `site/snapshot.py --reports` |

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
--owner <login>` places it and runs the gate, the `catalog` workflow runs
the same gate on the pull request (`site/gate.py`: clean, licensed,
namespaced, in the right place, unique id, changed only by an owner,
version bumped), and a maintainer merges. The `pages` workflow runs the
gate and the tests again before it publishes, so what is at `/catalog/`
always passes. See [`catalog/README.md`](../catalog/README.md) and
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

`rendezvous/reports.json` is baked beside the directory snapshot: the gap
reports the seeds carry (from hubs that opted in with `--report-gaps`),
merged hourly, which the catalog page shows beside each tree and `pyrtan
reports` reads when given no hub.

## The default network and the seeds

`site/rendezvous/network.json` is the default network: the name
`pyrtan` and a secret that is public on purpose (it names the network,
it guards nothing; see `_meta/federation.md` §7a). `pyrtan join` with
no invite, `pyrtan hub --join default` and `$PYRTAN_JOIN=default` put a
hub in it.

`site/rendezvous/seeds.json` lists hubs to follow first. A seed is a
federated hub in the default network (`--join default`), started with
`--listed` and `--cors https://pyrtan.com`, reachable from outside (a
tunnel or a port forward behind TLS; [`deploy/README.md`](../deploy/README.md)
says how). It need not be up always. Put yours in by pull request. The
landing page asks each seed's `/directory` from the visitor's browser,
so the directory on pyrtan.com is live and needs no backend; the
console link on each row opens `/console/?hub=<url>`. When no seed
answers, the page shows the snapshot instead, marked with its time:
`site/snapshot.py` asks the seeds on every build, hourly on the
schedule, then every other hub it knows of, keeps the default network's
part of what they say, keeps the last published snapshot's entries for
any hub that is down, and drops entries unheard of for a week. `pyrtan
join` tries the snapshot's hubs after the seeds, and `pyrtan directory`
falls back to it, so a new hub can join through any listed hub that
happens to be up, not only a seed.

## Later: hosting

Nothing here changes. A hosted hub is this repository's image started
with `--token`, `--cors https://pyrtan.com`, `--gaps` on a volume,
`--egress public` and `--federate --advertise https://<tenant>.pyrtan.com
--listed --state <volume>/federation.json`, behind TLS; the console
connects to it like to any other, a self-hosted hub can follow it or be
followed by it, and it is one more seed. The control plane that
provisions it is a separate, private repository, per
`_meta/platform.md`.
