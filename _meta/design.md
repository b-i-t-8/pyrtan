# Design — Pyrtan: a core set of expandable services that act on all forms of I/O through hardcoded decision trees

| | |
|---|---|
| **Status** | **Built, v0.1.** The core, the service, the hub and the command exist and are tested (111 tests, including a two-service swarm behind a hub on localhost). No deployment, no users, no browser adapter yet. |
| **Date** | 2026-10-24 |
| **Decision** | Pyrtan is an open-source automation tool: a core set of expandable services, each holding declarative decision trees that read and act on all forms of I/O, routed by a hub, with no model in the loop and a complete trace of every run. |
| **Where** | This repository: `src/pyrtan`, `examples/`, `tests/`. AGPL-3.0-or-later. |

## 0. The brief, and the reading this design commits to

The brief: *"an open source total automation tool; start over from scratch;
a swarm of services that are AI-LLM-like but with hardcoded decision trees
of the web, similar to Claude skills but much more focused and dynamic."*

Each phrase was read as follows, and the reading is the design.

**"LLM-like."** The interface, not the mechanism. A person or a program
gives Pyrtan a sentence and gets back an answer in words, a structured
output, and an explanation. Inside there is no model. The explanation is
not a model's account of itself; it is the literal path through the tree.

**"Hardcoded decision trees of the web."** A tree is a file an author
writes, naming the hosts it may touch, the requests it makes, the values it
extracts, and the comparisons that choose the next step. "Of the web" means
the tree encodes how one web property behaves: what a 404 means here, which
JSON path holds the title, which weather code means rain. The knowledge a
model would hold implicitly, a tree holds explicitly, for one site.

**"Swarm of services."** A tree is held by a service; a service is a small
process that answers `/intent` and `/run`; a hub routes between services
and reports which are up. The swarm is the unit of deployment and of
ownership: one team can own the trees for one property, run them on their
own box, and register with a shared hub.

**"Similar to Claude skills but much more focused and dynamic."** A skill
is a prose instruction package a model reads and interprets. A tree is
narrower (one thing, one property, no interpretation) and livelier in three
specific ways: a service reloads a tree when its file changes; a tree can
delegate to any other tree in the swarm by sentence or by name, resolved at
run time; and a decision that meets a value no branch covers is recorded as
a *gap*, so the set of trees grows where reality disagreed with its author.

**"Total automation."** Read as the ambition, not as a claim. v0.1 reads
APIs and pages and can POST. What it does not yet do is in Section 7.

## 1. What this design refuses

Four things a reader might expect, and will not find.

- **A model inside a run.** Nothing in `src/pyrtan` calls one. The entire
  value of a tree is that two runs with the same observations take the same
  path, and that the path can be read. A model anywhere in the path makes
  the trace a story rather than a record. A model may later sit *beside*
  the swarm, reading gaps and proposing branches for a person to accept;
  that is an author's tool, not a runtime.
- **Code in trees.** Templates are paths and filters. Conditions are
  comparisons. There is no expression language, no `if` in a template, no
  script node. Everything that branches is a `decide` node, which the trace
  shows. The cost is verbosity (the weather tree spends seven `set` nodes
  turning a code into a word); the benefit is that a tree can be audited by
  someone who does not program.
- **Reaching a host the author did not list.** `hosts` is checked at load
  time for literal URLs and at run time for templated ones. A tree that
  takes a host from its input must say `hosts = ["*"]` out loud.
- **Loops.** A tree is a directed acyclic graph; `pyrtan check` names the
  cycle and refuses. Repetition is `each` over a list with a limit, so
  every run is finite and every trace is complete.

## 2. Invariants

Everything after this section is arranged so that none of these can be
traded away by a convenient feature.

1. **Determinism given observations.** The same tree, inputs and responses
   produce the same path, outcome and trace. Tested by running every
   example against a recorded fixture.
2. **The trace is complete.** Every node visited appears, with what it
   observed and, for a decision, which condition held. Delegated runs
   appear inside the caller's trace one level deeper.
3. **Hosts are closed.** No request leaves to a host outside the tree's
   list, whether the URL was literal or built at run time.
4. **Reads retry; writes do not.** An `observe` may be retried on transport
   failure; an `act` is attempted once. The checker refuses `retries` on an
   act and refuses a write method on an observe.
5. **Absence is decided, not pasted.** A template whose value is missing
   fails the node, except in `done` and `fail`. A tree cannot build a URL
   from an empty string by accident; it must `decide` on `exists`.
6. **A caller cannot tell a service from a hub.** Both answer `/intent`,
   `/run`, `/resume`, `/capabilities`, `/health` and `/gaps` with the same
   shapes, so a swarm can start as one process and split later.

## 3. Architecture as built

```
sentence ──► intent.py ──► registry.py ──► runtime.py ──► Outcome + Trace
             (triggers)    (trees, reload)   │
                                             ├─ template.py   {{ paths | filters }}
                                             ├─ conditions.py when = { eq, between, … }
                                             ├─ extract.py    json / regex / css
                                             │    └─ html.py  parser + selector subset
                                             └─ transport.py  urllib | recorded | recording

service.py  one process, one registry, HTTP endpoints, hot reload, gaps
hub.py      peers, link up/stale/down, routing by capability, forwarding
cli.py      check · list · run · ask · serve · hub · gaps · new
```

**The tree** (`tree.py`) is TOML, loaded into dataclasses and checked on
load. `check()` returns problems at three levels; an error refuses the
load. It verifies references, conditions, regexes, selectors, input
declarations against template use, hosts against literal URLs,
reachability, and acyclicity. A `decide` without `else` is an `info`: it is
the author's statement that an unforeseen value should stop the run and be
reported, which is often right.

**Intent matching** (`intent.py`) compiles trigger patterns to regexes.
Specificity is the count of literal characters; the most specific match
wins, and a tie is an `ambiguous` outcome. A pattern with no literal
characters is refused because it would match everything.

**The runtime** (`runtime.py`) is one loop over node kinds. State is a
dictionary; scope adds `inputs`, `item`, `index`, `tree`, `now` and `env`.
`each` runs another tree per item (through the registry, then the
resolver) or a local sub-flow (the same tree from another start node, with
shared state). `delegate` does the same for one sentence or name. `ask`
returns a snapshot the caller can `resume`; only a top-level run can ask,
because a paused sub-run would need a call stack the snapshot does not
carry.

**Transport** (`transport.py`) is a protocol with three implementations:
the standard library's client, a recorded fixture that raises on any
request it did not foresee, and a recorder that writes such fixtures.
Every example tree is tested offline through the second, and
`pyrtan run --record` produces fixtures through the third.

**The service** (`service.py`) wraps a registry in the standard library's
threading HTTP server. It reloads trees from disk on every request that
needs them (a stat per file; cheap), keeps the gaps it has seen, and, when
given a hub, registers itself and forwards unknown trees there.

**The hub** (`hub.py`) holds no trees. It polls peers' `/capabilities`,
derives each peer's `link` from the age of its last good answer (`up` under
30 s, `stale` under 90 s, `down` after), routes a sentence to the peer
whose tree matches best, and forwards. The `link` field is the one thing an
operator reads; it is an idea carried over from an earlier product of
the same name, kept because it was right.

**No dependencies.** The runtime imports only the standard library:
`tomllib`, `html.parser`, `urllib`, `http.server`, `re`, `json`. This was a
deliberate choice over a web framework and an HTML library. A tool that
asks to be installed next to someone's existing automation should not
bring a dependency tree with it, and the HTML selector subset trees
actually need is small enough to own (`html.py`, about 250 lines, tested).
A browser adapter will be an optional extra, not a requirement.

## 4. Paths considered and not taken

**Rust.** The earlier product of this name chose Rust for a 6 MB resident
footprint on site boxes. Pyrtan's services run wherever an operator already
has Python, and its contributors are people who write trees, not people
who write runtimes. Python with no dependencies installs anywhere, and
`uv` makes a checkout usable in one command. If a service ever needs to
run on a constrained box, the tree format is language-neutral and a second
runtime can read the same files.

**YAML or JSON for trees.** TOML has comments, which JSON lacks, and a
standard-library parser, which YAML lacks. Its one trap, top-level keys
after a table header, is caught by the loader with a hint.

**An expression language in templates.** Rejected per Section 1. The
weather tree is longer for it; it is also readable by a non-programmer end
to end.

**A model fallback for unmatched sentences.** The obvious feature, and the
one that would quietly turn the product into the thing it is defined
against. Not built. A sentence no tree answers returns `no-match`, and the
honest response to a frequent `no-match` is a new tree.

**A message bus between services.** NATS or similar would give the swarm
events and fan-out. v0.1 has `act.emit`, which records an event on the
service; nothing consumes it yet. Request/response over HTTP covers every
case the examples needed, and a bus would be a dependency and an
operational component before there is a swarm large enough to need one.

**Persisting gaps.** Gaps live in a service's memory and are lost on
restart. Enough to prove the loop (run, see gap, add branch, reload), not
enough for a fleet. Deferred to Section 7.

## 5. What the examples show

Five trees, all against real public APIs, all tested offline:

| Tree | Shows |
| :-- | :-- |
| `hacker-news` + `hacker-news-item` | `each` over a list through a second tree; a decision on status before one on content; a self-post with no URL handled by `set` rather than by an empty string |
| `weather` | Two hops where the second depends on the first; `exists` deciding on absence; a code-to-word table as seven decisions, not an expression |
| `site-status` | A host from the sentence, so `hosts = ["*"]` and a run-time host check; `between` on status ranges; a client error reported as "up but refusing" rather than "down" |
| `github-release` | Headers; a decision that names rate-limiting as its own branch |
| `picnic` (in the tests) | One service delegating a sentence through the hub to another, and the other's steps returned inside its trace |

## 6. Sequencing

1. **v0.1, built.** Trees, checker, runtime, trace, service, hub, command,
   examples, tests. This document.
2. **v0.2: first real swarm.** Three or more services on separate boxes
   behind one hub, with a status page off `/peers`. Gaps persisted to a
   file per service and collected by the hub. A `pyrtan watch` that
   re-runs a set of sentences on a schedule and reports when a path changes
   (the way to learn a site has changed before a person does).
3. **v0.3: browser adapter.** `observe` with `browser = { url, wait_for,
   actions }` on Playwright as an optional extra, for pages that need a
   script to render, with the same host rule and the same extraction.
4. **v0.4: trees as a shared thing.** A directory of trees people can pull
   by name, with the checker run on contribution, and `version` meaning
   something.
5. **Beside the swarm, never inside it:** an authoring aid that reads gaps
   and drafts the branch for a person to accept.

## 7. Open questions

- **Authentication between services.** None in v0.1; the swarm is assumed
  to sit on a trusted network behind an operator's ingress. The first
  swarm that crosses a network boundary needs a shared secret or mTLS at
  the hub, and `env` already carries per-service secrets for upstream APIs.
- **What a tree's `version` governs.** A hub could prefer the highest
  version of a name across peers, or refuse to route to a peer whose
  version is below the caller's. Undecided until two services hold the
  same tree at different versions.
- **Whether `ask` belongs in a swarm at all.** A pause needs a place to
  keep the snapshot; v0.1 hands it back to the caller. That is right for a
  command and awkward for a service behind a hub. A `pending` store on the
  hub is the likely answer.
- **How far `html.py` should go.** The selector subset covers every example
  and every test. Sites that need `:not()`, sibling combinators or
  attribute case-folding will push on it; the alternative is an optional
  dependency on a full selector engine, which the no-dependency rule
  allows as an extra but not as a requirement.

## 8. What this design does not claim

It does not claim to answer sentences nobody wrote a tree for. It does not
claim trees are quick to write: the weather tree is 15 nodes for a two-call
API, and that is the price of leaving nothing implicit. It does not claim
the swarm is secure across a network boundary (Section 7). It does not
claim the examples will keep working as the sites they read change; it
claims that when they stop, the trace will say exactly where, and a gap
will say what was seen.

## The name

The name Pyrtan was used earlier for an unrelated building-management
product. None of its code is in this repository. The one idea carried over
is that a fleet's health is one pollable field, which is the hub's `link`.
