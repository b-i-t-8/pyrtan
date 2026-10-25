# Putting up pyrtan.com

pyrtan.com is static files: a landing page, the console, and the catalog.
Nothing runs server-side until there is paid hosting, so the whole site is
two GitHub repositories deployed to GitHub Pages (or Cloudflare Pages; the
files are the same). The design is in
[`_meta/platform.md`](../../_meta/platform.md).

What to create, in order:

| | Repository | Serves | From |
| :-- | :-- | :-- | :-- |
| 1 | `b-i-t-8/pyrtan-trees` | `pyrtan.com/catalog/` | its own `trees/` folder, checked and indexed by CI |
| 2 | `b-i-t-8/pyrtan-site` | `pyrtan.com/` and `pyrtan.com/console/` | `index.html` here, and `pyrtan console --out` |
| 3 | nothing | the rendezvous | `rendezvous/seeds.json` here, served by the site; your NAS is seed #1 |

Both can be one repository if you prefer; two keeps "I wrote a tree" pull
requests apart from "I changed the site" ones, and lets the catalog's
contributors never touch the site.

Files in this folder, ready to copy:

- [`catalog.yml`](catalog.yml): the workflow for the trees repository.
- [`site.yml`](site.yml): the workflow for the site repository.
- [`index.html`](index.html): the landing page.
- [`trees-README.md`](trees-README.md): the README for the trees repository.

## 1. The catalog repository (`pyrtan-trees`)

```
pyrtan-trees/
  README.md                 from trees-README.md here
  LICENSE                   what the *repository's* own files are under; each tree declares its own
  .github/workflows/catalog.yml
  trees/
    weather/
      weather.toml          license = "MIT" in the file
      fixture.json          recorded responses, from pyrtan run --record
    hacker-news/
      hacker-news.toml
      hacker-news-item.toml
      fixture.json
  tests/
    test_trees.py           replays each tree against its fixture
```

Steps:

1. Create the repository, empty.
2. Copy `trees-README.md` to `README.md` and `catalog.yml` to
   `.github/workflows/catalog.yml`.
3. Seed `trees/` from this repository's `examples/` (each already carries
   `license = "MIT"`), and `tests/fixtures/examples.json` as the fixtures,
   split per folder or kept as one file; the workflow only needs `pyrtan
   check` and `pytest` to pass.
4. In the repository's *Settings › Pages*, set *Source* to **GitHub
   Actions**.
5. Push. The workflow runs `pyrtan check trees`, the tests, then
   `pyrtan index trees --out site/index.json`, copies the tree files next
   to it, and publishes `site/` to Pages. The result is at
   `https://b-i-t-8.github.io/pyrtan-trees/`.

What the workflow enforces on every pull request: every tree loads and
checks clean, every tree has a `license`, no two trees share a name, and
the tests pass. `pyrtan index` exits 1 on any of those, which fails the
check. A reviewer reads the tree; nothing else is needed to accept one.

Bump `version` in the tree when a change should be noticed by people who
pulled it; `pyrtan pull NAME@VERSION` pins one.

## 2. The site repository (`pyrtan-site`)

```
pyrtan-site/
  index.html                from index.html here
  CNAME                     "pyrtan.com"
  .github/workflows/site.yml
```

Steps:

1. Create the repository; copy `index.html` and `site.yml` (to
   `.github/workflows/site.yml`); add a `CNAME` file containing
   `pyrtan.com`.
2. *Settings › Pages*: *Source* **GitHub Actions**; *Custom domain*
   `pyrtan.com`; tick *Enforce HTTPS* once DNS resolves.
3. Push. The workflow installs `pyrtan` from this repository (pinned to a
   tag; update the tag to ship a new console), writes
   `console/index.html` with `pyrtan console --out`, and publishes.

### DNS

At your DNS provider, for `pyrtan.com`:

| Record | Name | Value |
| :-- | :-- | :-- |
| `A` | `@` | `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153` |
| `AAAA` | `@` | `2606:50c0:8000::153`, `2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153` |
| `CNAME` | `www` | `b-i-t-8.github.io` |

(GitHub's current addresses; check *Pages › Custom domain* in the
repository settings if they have changed.)

### `/catalog/` on the same domain

The catalog is published from the other repository, at
`b-i-t-8.github.io/pyrtan-trees/`. The command's default is
`https://pyrtan.com/catalog/`, so the catalog must answer there. Two ways:

- **Proxy it (Cloudflare).** If pyrtan.com's DNS is on Cloudflare, add a
  *Worker route* or an *Origin rule* that serves
  `pyrtan.com/catalog/*` from `b-i-t-8.github.io/pyrtan-trees/*`. A
  ten-line Worker does it:

  ```js
  export default {
    async fetch(req) {
      const u = new URL(req.url);
      if (!u.pathname.startsWith("/catalog/")) return fetch(req);
      const up = "https://b-i-t-8.github.io/pyrtan-trees/" + u.pathname.slice("/catalog/".length);
      const r = await fetch(up, { headers: { "User-Agent": "pyrtan.com" } });
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.headers.get("Content-Type") || "application/octet-stream", "Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*" } });
    },
  };
  ```

- **Or publish the catalog into the site.** Have `site.yml` check out
  `pyrtan-trees` too, run `pyrtan index` there, and copy the output to
  `catalog/` in the site's artifact; then one Pages deployment holds
  everything, at the cost of the site rebuilding when a tree merges. The
  commented block in `site.yml` shows this.

Either way, `curl https://pyrtan.com/catalog/index.json` should return the
index, and `pyrtan search weather` from anywhere should find it.

### `/console/`

Works as soon as the site is up. A visitor enters a hub URL and a token;
the hub must have been started with `--cors https://pyrtan.com`. A hub on
a LAN over plain `http://` cannot be reached from an `https://` page (the
browser blocks it); the console says so and points at the hub's own `/`,
which is the same console served locally.

## 3. The rendezvous: seeds, and your NAS

Nothing runs for this. Hubs keep and gossip the directory among
themselves (`_meta/federation.md` §7); the site only publishes a list
of hubs to follow first.

1. **Seeds.** Copy `rendezvous/seeds.json` into the site repository at
   `rendezvous/seeds.json` and put your hub's public URL in `hubs`.
   `site.yml` serves it at `https://pyrtan.com/rendezvous/seeds.json`,
   which is what `pyrtan follow` reads when given no URL. Others add
   theirs by pull request.
2. **Your NAS as seed #1.** Its hub needs a public address. Two ways
   that open no port and cost nothing:

   - *Cloudflare Tunnel:* `cloudflared tunnel --url http://127.0.0.1:8700`
     gives a `https://….trycloudflare.com` address at once; a named
     tunnel on a domain you own (`hub.example.net`) is a few more
     commands in Cloudflare's dashboard and survives restarts.
   - *Tailscale Funnel:* `tailscale funnel 8700` publishes the hub at
     `https://<machine>.<tailnet>.ts.net`.

   Then, in the compose file's environment: `PYRTAN_TOKEN` set,
   `PYRTAN_ADVERTISE` the public URL, `PYRTAN_FOLLOW: seed` (or another
   hub), `PYRTAN_LISTED: "true"`. The hub keeps its follows and the
   directory in the `hub-state` volume. `pyrtan directory --at
   https://<your hub>` shows what it knows.
3. **The page.** `index.html` fetches `rendezvous/seeds.json` and asks
   each seed's `/directory` from the visitor's browser (seeds run with
   `--cors https://pyrtan.com`, which the compose file sets), so
   pyrtan.com shows the live directory with no backend. The console link
   on each row opens `/console/?hub=<url>`.

## 4. Later: hosting

Nothing here needs to change. A tenant's hub is this repository's image
started with `--token`, `--cors https://pyrtan.com`, `--gaps` on a volume,
`--egress public` and `--federate --advertise https://<tenant>.pyrtan.com
--listed --state <volume>/federation.json`, behind TLS; the console at
pyrtan.com connects to it like to any other, a self-hosted hub can follow
it (or be followed by it) like any other hub, and it is one more seed. The control plane that provisions it is a separate,
private repository, per `_meta/platform.md` §2.6.

## Checklist

- [ ] `pyrtan-trees` created; Pages source = Actions; first run green;
      `…github.io/pyrtan-trees/index.json` answers.
- [ ] `pyrtan-site` created with `CNAME`; Pages custom domain set; DNS
      records in; HTTPS enforced.
- [ ] `https://pyrtan.com/` shows the landing page; `/console/` loads.
- [ ] `/catalog/index.json` answers on pyrtan.com (proxy or merged build).
- [ ] From a laptop: `pyrtan search weather` finds it; `pyrtan pull weather`
      writes `trees/weather.toml`.
- [ ] A hub started with `--token … --cors https://pyrtan.com` connects
      from `https://pyrtan.com/console/`.
- [ ] `rendezvous/seeds.json` in the site repo names your hub; `pyrtan
      follow --at <another hub>` with no URL follows it; `pyrtan
      directory` lists it; the page shows it.
