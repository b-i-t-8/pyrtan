# pyrtan.com

pyrtan.com is this repository's GitHub Pages site. Nothing of ours runs:
on every push to `main`, and once an hour, the `pages` workflow builds a
static directory and publishes it.

| Path | What | Built from |
| :-- | :-- | :-- |
| `/` | the landing page, with the live directory read from the seeds in the visitor's browser | `site/index.html` |
| `/console/` | the console; point it at any hub started with `--cors https://pyrtan.com` | `pyrtan console --out` |
| `/catalog/` | the trees people can `pyrtan pull`, and `index.json` the command reads | `pyrtan index examples`, and the tree files |
| `/rendezvous/seeds.json` | hubs to follow first | `site/rendezvous/seeds.json` |
| `/rendezvous/directory.json` | a snapshot of the directory as the seeds know it, refreshed hourly | `site/snapshot.py` |

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

The catalog is `examples/`. A tree goes in by pull request to this
repository, under `examples/<name>/`, with a `license` line and a test
in `tests/test_examples.py` against a recording (see
[`CONTRIBUTING.md`](../CONTRIBUTING.md)). `pyrtan index` refuses a tree
with no licence or a duplicate name, and the workflow runs the tests
before it publishes, so what is at `/catalog/` always passes.

## The seeds

`site/rendezvous/seeds.json` lists hubs to follow first. A seed is any
federated hub its operator keeps up, started with `--listed` and `--cors
https://pyrtan.com`, reachable from outside (a tunnel or a port forward
behind TLS; [`deploy/README.md`](../deploy/README.md) says how). Put
yours in by pull request. The landing page asks each seed's `/directory`
from the visitor's browser, so the directory on pyrtan.com is live and
needs no backend; the console link on each row opens
`/console/?hub=<url>`. When no seed answers (every seed is a home box,
and home boxes go off), the page shows the snapshot instead, marked with
its time: `site/snapshot.py` asks the seeds on every build, hourly on
the schedule, keeps the last published snapshot's entries for any seed
that is down, and drops entries unheard of for a week. `pyrtan follow`
with no URL tries the snapshot's hubs after the seeds, and `pyrtan
directory` falls back to it, so a new hub can bootstrap from any listed
hub that happens to be up, not only from a seed.

## Later: hosting

Nothing here changes. A hosted hub is this repository's image started
with `--token`, `--cors https://pyrtan.com`, `--gaps` on a volume,
`--egress public` and `--federate --advertise https://<tenant>.pyrtan.com
--listed --state <volume>/federation.json`, behind TLS; the console
connects to it like to any other, a self-hosted hub can follow it or be
followed by it, and it is one more seed. The control plane that
provisions it is a separate, private repository, per
`_meta/platform.md`.
