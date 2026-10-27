# Design — pyrtan.com: the console, the catalog, and hosting

| | |
|---|---|
| **Status** | **Phases 1 and 2 built (v0.2, v0.8).** Tokens, CORS, persistent gaps, the egress policy, the catalog client and the console (Phase 1); namespaces, publishing by pull request through the gate, and gap reports gossiped back to authors (Phase 2, §2.8). The site is this repository's GitHub Pages deployment, described in [`../site/README.md`](../site/README.md). Hosting is designed, not built. |
| **Date** | 2026-10-25 |
| **Decision** | pyrtan.com is three separate things on one invariant: a static catalog of trees anyone can pull, a static console that talks only to the public hub API, and (later) paid hosting that runs the unmodified open-source hub per tenant. A caller cannot tell a hosted hub from a self-hosted one, so every piece works for both. |
| **Where** | This repository for everything open: the runtime, the client, the console, the catalog (`examples/`) and the site (`site/`, published by the `pages` workflow). A private repository for the control plane, when there is one. |

## 0. The one invariant

[`design.md`](design.md) §2.6 says a caller cannot tell a service from a
hub, because both answer the same endpoints with the same shapes. This
design extends it one step: **a caller cannot tell a hosted Pyrtan from a
self-hosted one.** The console, the command, the catalog client and any
future integration speak only the public API (`/api`, `/health`,
`/capabilities`, `/peers`, `/intent`, `/run`, `/resume`, `/gaps`), with a
bearer token when the operator set one. Nothing hosted gets a private
endpoint. A browser that has joined no hub answers the same API itself
(§2.5), so the console cannot tell that apart either.

The consequence for the business: what people pay for lives *around* the
runtime, not in it. The runtime stays whole and AGPL; the paid product is
provisioning, keeping, watching and billing.

## 1. What this design refuses

- **A fork of the hub for the paid product.** One codebase, one image. A
  tenant's hub is `pyrtan hub --token … --cors https://pyrtan.com` in a
  container, the same command a self-hoster runs on a NAS.
- **Billing or tenancy in `src/pyrtan`.** No `plan`, no `tenant_id`, no
  licence check. The control plane talks to hubs over HTTP like any client.
- **An account to read or pull trees.** The catalog is static files. A
  mirror is `cp -r`. A private catalog is the same files on a private host
  with `--catalog` pointed at it.
- **A console built only for the paid product.** The file at pyrtan.com is
  the file the hub serves at `/`. They cannot drift apart because they are
  the same bytes.
- **A model in the catalog's review.** Trees are accepted by `pyrtan check`
  and a replayed fixture, and read by a person.

## 2. The pieces

### 2.1 Access (`service.py`, `hub.py`; built)

`--token` (or `$PYRTAN_TOKEN`) on `serve` and `hub`. With a token, every
request except `GET /health`, the console and `/api` must carry
`Authorization: Bearer`. `/health` without the token answers status and
counts only. A swarm shares one token: a service presents it when it
registers, the hub presents it when it polls and forwards. `/register` is
behind the token, so a stranger cannot join a swarm.

`--cors ORIGIN` names the browser origins allowed to call (repeatable; `*`
for any). Off by default. The console at pyrtan.com needs
`--cors https://pyrtan.com` on the hub it talks to.

Neither does TLS. A hub that leaves the LAN sits behind a proxy that does.
mTLS between services is still open (design.md §7); a shared token is
enough for the first swarm that crosses a network boundary.

### 2.2 Gaps that persist (`gaps.py`; built)

`serve --gaps FILE` appends every gap to a JSON-lines file and reads it
back on start. The same gap seen twice is one entry with a `count`, first
and last seen: the list is what to write next, not a log. The hub's
`/gaps` still collects from peers; a store on the hub itself is deferred
until a swarm needs gaps to outlive its services.

### 2.3 Egress (`transport.py`; built)

`serve --egress public` refuses, before connecting, any URL whose host
resolves to a loopback, private, link-local, multicast, reserved or
unspecified address, and applies the same rule to every redirect. A tree
with `hosts = ["*"]` (the `site-status` example) is otherwise a way to
probe the host's own network. Self-hosters on a LAN leave it off; a host
that runs other people's trees turns it on and firewalls as well, because
the check resolves the name once and the client resolves it again.

### 2.4 The catalog (`catalog.py`; built)

A catalog is `index.json` plus the tree files it points to, on any static
host. `pyrtan index DIR` builds the index: one entry per tree with its
capability, licence, path, size and SHA-256. `pyrtan search WORDS` and
`pyrtan pull NAME[@VERSION]` read one; the default is
`https://pyrtan.com/catalog/`, overridden by `--catalog` or
`$PYRTAN_CATALOG`.

A pull fetches the file, checks its hash against the index, loads it as a
tree, and checks that the name and version inside match the entry before
writing it. An index entry that points outside the catalog (`..`, an
absolute path, another host) is refused. A file that exists locally and
differs is not overwritten without `--force`.

`version` now has one meaning the catalog relies on: it is the integer an
author bumps when the file changes in a way a puller should notice, and
`NAME@VERSION` pins it. Two services holding different versions of a name
is still the hub's open question.

A catalog tree carries a `license` (SPDX). The public catalog requires
one; `pyrtan index` leaves out a tree without one unless told otherwise.
Trees are data, not part of the program, so the AGPL does not reach them;
a declared licence is what lets a stranger use what they pulled.

Since v0.8 a catalog tree also carries a `namespace`: who publishes it.
The id is `namespace/name`; `pyrtan pull weather` takes the one there is
and asks when two namespaces have it; `pyrtan pull barry/weather` says
which. The index is built over several directories with `--copy`, which
lays the files out as `namespace/name.toml` so the served catalog has one
shape whatever the sources look like.

### 2.8 Publishing and gap reports (`catalog.py`, `federation.py`; built, v0.8)

**Publishing is a pull request.** The repository holds `examples/` (the
`pyrtan` namespace) and `catalog/<namespace>/<name>.toml`, each namespace
with an `OWNERS` file of GitHub logins. `pyrtan publish FILE --into
CHECKOUT --owner LOGIN` places the file, writes `OWNERS` for a new
namespace, and runs the gate; the `catalog` workflow runs the same gate
on every pull request with what git adds: the changed files, the
author, the version each tree had on `main`. The gate (`catalog.gate`,
`site/gate.py`) refuses a tree that does not load clean, lacks a licence
or a namespace, sits under the wrong directory or file name, shares an id
with another, is changed by someone not in its namespace's `OWNERS`, or
changed without a version bump. A maintainer merges; the `pages`
workflow publishes. There is no account, no API and no model in the
review; the "first small API" Phase 2 expected turned out to be GitHub's
pull request, which already exists.

**Gaps come back to the author** as a federation topic, the way the
directory does. A hub started with `--report-gaps` shapes the gaps its
catalog trees met into reports (`namespace/name@version`, node, the path
decided on, the value no branch covered, count, when; a value longer
than eighty characters or not a scalar becomes its type; never the
inputs or the rest of the state) and puts them in its announces. Every
federated hub keeps what it hears, one row per reporter and gap, with
the merge rules the directory uses (newer wins, nothing from the future,
thirty days, two thousand rows), passes it on, and serves `/reports`
publicly with counts and reporters summed. The site's snapshot asks the
listed hubs, and every hub they know of, for `/reports` hourly and bakes `rendezvous/reports.json`; the
catalog page shows each tree's gaps beside it; `pyrtan reports
NAMESPACE/NAME` reads a hub or the baked file. A tree without a namespace
reports nothing: there is nobody to report to. Opting in is the
operator's, per hub, and off by default.

### 2.5 The console (`page.py`; built)

One HTML file, no build, no external assets. Served by a hub or service
at `/` and `/console`, it talks to that origin. Written out by
`pyrtan console --out` and hosted anywhere, it finds out on load that its
own origin is not a Pyrtan (`/api` does not answer as one), and then the
browser is the hub, for as long as the page is open: `pyrtan.js`'s
`LocalHub` answers `/api`, `/health`, `/capabilities`, `/intent`, `/run`,
`/resume`, `/gaps`, `/unanswered` and `/watches` inside the page, with the
shapes a service answers them with, and the console calls it through the
same function it calls a hub through. It holds the catalog's trees, read
from the catalog's `trees.json` (`../catalog/` beside the console, else
pyrtan.com's, else the copy the browser kept last time; `?catalog=URL`
names another), reads sentences with the same rules as the Python
runtime (the conformance vectors cover the reading, the value parsers,
the proposals and a run of requests against a service), runs the trees,
and keeps gaps and unanswered sentences in memory. What a page cannot
reach it reports instead of attempting: a `file`, `shell`, `serial`,
`queue` or `browser` node answers `source-unsupported`, and a site that
does not let a page read it fails as `transport` with that reason; the
Trees tab marks both, and "Try one" offers only trees that can answer.
Nothing in it is sent anywhere but the requests the trees make.

Joining is explicit: a hub URL and a token in the connect box, kept in
local storage, the token sent as a bearer (and `?hub=URL` for a link).
From then on everything goes to that hub, as before. "Forget" drops it,
and the browser is its own hub again. A hub that requires a token and
serves the console itself shows the same box with only the token
field.

Tabs: Ask (sentence, answer, output, trace, and a pointer when the run met
a gap), Trees (capabilities, with a link to the catalog), Peers (hub only;
`link` per peer), Gaps, API.

A browser will refuse a `http://` hub from an `https://` page. The console
says so and points at the hub's own `/`, which is the same console.

### 2.6 The rendezvous (built; nothing runs)

pyrtan.com runs nothing: it is static files on GitHub Pages, and runs no
hub of its own. Hubs find each other through the directory every
federated hub keeps and gossips ([`federation.md`](federation.md) §7);
the site publishes `rendezvous/hubs.json`, a static list of hubs their
operators added by pull request, and its page reads the live directory
from them in the visitor's browser, falling back to the snapshot GitHub
bakes hourly. A hosted hub, when there is one, goes on the list like
any other.

### 2.7 Hosting (designed)

A tenant gets a hub and one or more services as containers from this
repository's image, with a token, `--cors https://pyrtan.com`, `--gaps` on
a volume, `--egress public`, `--federate --advertise https://<tenant>.pyrtan.com --listed`
with `--state` on the volume, and TLS from the ingress. A hosted hub and
a self-hosted one follow each other like any two hubs
([`federation.md`](federation.md)): a customer's NAS can answer for their
hosted swarm and the other way round, which is the invariant in §0 made
concrete. The control plane
(separate, private) does what the runtime does not: sign-up and teams,
provisioning, the secrets that reach trees as `env`, trace retention, a
scheduler that re-runs sentences and reports when a path changes (the
`pyrtan watch` of design.md §6), uptime from `link`, and billing.

Tenants are isolated by process and network, never by a flag in the
runtime. A tenant who leaves takes their trees, their gaps file and their
fixtures and runs the same commands on their own box.

## 3. Sequencing

1. **Phase 1 (this change).** Everything in §2.1–2.5, in the open-source
   repository. The site: pyrtan.com as static pages, `/console/` from
   `pyrtan console --out`, `/catalog/` from the catalog repository's CI.
   Hosting needs nothing yet.
2. **Phase 2 (built, v0.8).** Namespaces in the catalog
   (`someone/tree`), publishing through pull requests with the checker as
   the gate, and opt-in gap reporting carried through the federation to
   pyrtan.com, so an author learns where reality disagreed with their
   tree across every hub that runs it. No API was needed after all: the
   pull request and the gossip were enough (§2.8).
3. **Phase 3.** The control plane: per-tenant hubs, secrets, hosted watch,
   retention, billing.

## 4. What to settle before Phase 2

Not engineering, and not legal advice:

- **A contributor licence agreement**, or a standing rule that nothing
  closed ever enters `src/pyrtan`. Without one of the two, contributed
  runtime code cannot be relicensed later. The second is this design's
  choice; a CLA keeps the first open.
- **A licence requirement for catalog trees.** This design requires an
  SPDX identifier and recommends permissive ones (MIT, Apache-2.0, CC0).
- **The trademark.** "Pyrtan" as a mark is what stops someone else hosting
  a lookalike under the name; the AGPL does not.
- **The word.** The code says *trees* and compares them to skills. The
  site should too; calling them skills sells the comparison and loses the
  claim that no model is in the loop.
