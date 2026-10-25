# Design — pyrtan.com: the console, the catalog, and hosting

| | |
|---|---|
| **Status** | **Phase 1 built, v0.2.** Tokens, CORS, persistent gaps, the egress policy, the catalog client and the console are in this repository and tested. The site and the catalog repository are described in [`../deploy/pyrtan.com/README.md`](../deploy/pyrtan.com/README.md). Hosting is designed, not built. |
| **Date** | 2026-10-25 |
| **Decision** | pyrtan.com is three separate things on one invariant: a static catalog of trees anyone can pull, a static console that talks only to the public hub API, and (later) paid hosting that runs the unmodified open-source hub per tenant. A caller cannot tell a hosted hub from a self-hosted one, so every piece works for both. |
| **Where** | This repository for the runtime, client and console. `pyrtan-trees` for the catalog. `pyrtan-site` for the pages. A private repository for the control plane, when there is one. |

## 0. The one invariant

[`design.md`](design.md) §2.6 says a caller cannot tell a service from a
hub, because both answer the same endpoints with the same shapes. This
design extends it one step: **a caller cannot tell a hosted Pyrtan from a
self-hosted one.** The console, the command, the catalog client and any
future integration speak only the public API (`/api`, `/health`,
`/capabilities`, `/peers`, `/intent`, `/run`, `/resume`, `/gaps`), with a
bearer token when the operator set one. Nothing hosted gets a private
endpoint.

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

### 2.5 The console (`page.py`; built)

One HTML file, no build, no external assets. Served by a hub or service
at `/` and `/console`, it talks to that origin. Written out by
`pyrtan console --out` and hosted anywhere, it finds out on load that its
own origin is not a Pyrtan (`/api` does not answer as one), shows a
connect box for a hub URL and a token, keeps them in local storage, and
sends the token as a bearer. A hub that requires a token and serves the
console itself shows the same box with only the token field.

Tabs: Ask (sentence, answer, output, trace, and a pointer when the run met
a gap), Trees (capabilities, with a link to the catalog), Peers (hub only;
`link` per peer), Gaps, API.

A browser will refuse a `http://` hub from an `https://` page. The console
says so and points at the hub's own `/`, which is the same console.

### 2.6 The rendezvous (built; nothing runs)

pyrtan.com runs nothing. Hubs find each other through the directory
every federated hub keeps and gossips ([`federation.md`](federation.md)
§7); the site publishes `rendezvous/seeds.json`, a static list of hubs
to follow first, and its page reads the live directory from those seeds
in the visitor's browser. The first seed is the author's own NAS,
reachable through a tunnel. A hosted hub, when there is one, is just
another seed.

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
2. **Phase 2.** Namespaces in the catalog (`someone/tree`), publishing
   through pull requests with the checker as the gate, and opt-in gap
   reporting: a hub that holds a catalog tree can send that tree's gaps to
   the catalog, so an author learns where reality disagreed with their
   tree across every hub that runs it. This needs the first small API.
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
