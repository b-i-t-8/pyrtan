# The seed hub on Fly.io

`seed.pyrtan.com` is the first seed in
[`site/rendezvous/seeds.json`](../../site/rendezvous/seeds.json): the hub
`pyrtan follow` with no URL follows first, and the one the landing page
asks for the directory. It is an ordinary federated hub, started
`--listed` and `--cors https://pyrtan.com`, that never sleeps, keeps its
follows and directory on a volume, and holds the examples that only read
public APIs (weather, hacker-news, github) with `--egress public`. The
other examples stay off it: they run a shell, read files or a serial
line, or fetch any host.

Everything is in [`fly.toml`](fly.toml). The image is the root
Dockerfile's with one change: it starts as root to hand the volume at
`/data` to the `pyrtan` user (a Fly volume mounts root-owned), then runs
as that user with no new privileges.

## First deploy

From the repository root, with [`flyctl`](https://fly.io/docs/flyctl/)
signed in:

```
fly apps create pyrtan-seed
fly volumes create pyrtan_seed --app pyrtan-seed --region iad --size 1 --yes
fly secrets set --app pyrtan-seed --stage PYRTAN_TOKEN="$(openssl rand -hex 24)"
fly deploy . --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile
fly ips allocate-v4 --shared --app pyrtan-seed
fly ips allocate-v6 --app pyrtan-seed
fly certs add seed.pyrtan.com --app pyrtan-seed
```

Then at the DNS provider, `CNAME seed -> pyrtan-seed.fly.dev`, and
`fly certs check seed.pyrtan.com --app pyrtan-seed` until it says the
certificate is issued. Keep the token: it is what `pyrtan gaps`,
`unanswered` and the console need to see more than `/actor` and
`/directory`, which stay open without it, as do signed requests from
federated hubs.

Check it:

```
curl https://seed.pyrtan.com/health
curl https://seed.pyrtan.com/directory
pyrtan directory
```

## Updating

`fly deploy . --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile`
from a checkout of `main`. The volume keeps who the seed follows and is
followed by, the directory, its blobs and its unanswered sentences.
