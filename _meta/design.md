# Design — Pyrtan: a core set of expandable services that act on all forms of I/O through hardcoded decision trees

| | |
|---|---|
| **Status** | **Built, v0.1; extended in v0.2.** The core, the service, the hub and the command exist and are tested (a two-service swarm behind a hub runs on localhost in the tests). HTTP is the only form of I/O the core speaks yet. v0.2 adds tokens, CORS, persistent gaps, an egress policy, the catalog client and the console; see [`platform.md`](platform.md). |
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
writes, naming the endpoints it may touch, the requests it makes, the
values it extracts, and the comparisons that choose the next step. "Of the
web" means the tree encodes how one property behaves: what a 404 means
here, which JSON path holds the title, which weather code means rain. The
knowledge a model would hold implicitly, a tree holds explicitly, for one
property. The web was the brief's example and is the first form of I/O the
core speaks; the shape is the same for any other. An adapter names what it
may reach, a node observes or acts through it, and a `decide` reads what
came back. A file, a shell, a serial line or a queue is the same tree with
a different adapter under it.

**"Swarm of services."** Read as a core set of expandable services. A tree
is held by a service; a service is a small process that answers `/intent`
and `/run`; a hub routes between services and reports which are up. The
core is the runtime, the service and the hub; what expands is the set of
trees, the set of services holding them, and the forms of I/O they reach.
The swarm is the unit of deployment and of ownership: one team can own the
trees for one property, run them on their own box, and register with a
shared hub.

**"Similar to Claude skills but much more focused and dynamic."** A skill
is a prose instruction package a model reads and interprets. A tree is
narrower (one thing, one property, no interpretation) and livelier in three
specific ways: a service reloads a tree when its file changes; a tree can
delegate to any other tree in the swarm by sentence or by name, resolved at
run time; and a decision that meets a value no branch covers is recorded as
a *gap*, so the set of trees grows where reality disagreed with its author.

**"Total automation."** Read as the ambition, not as a claim. v0.1 speaks
HTTP: it reads APIs and pages and can POST. Other forms of I/O are adapters
still to come (Section 6); what is open is in Section 7.

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
- **Reaching an endpoint the author did not list.** `hosts` is checked at
  load time for literal URLs and at run time for templated ones. A tree
  that takes a host from its input must say `hosts = ["*"]` out loud. The
  same rule binds every later form of I/O: an adapter arrives with an
  allow-list of the same kind (paths, devices, queues), or it does not
  arrive.
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
3. **Endpoints are closed.** No request leaves to a host outside the
   tree's list, whether the URL was literal or built at run time, and no
   future adapter is admitted without an allow-list of the same kind.
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
sentence ──► nlp.py ──► intent.py ──► registry.py ──► runtime.py ──► Outcome + Trace
             (rules)    (triggers)    (trees, reload)   │
                                             ├─ template.py   {{ paths | filters }}
                                             ├─ conditions.py when = { eq, between, … }
                                             ├─ extract.py    json / regex / css
                                             │    └─ html.py  parser + selector subset
                                             └─ transport.py  urllib | recorded | recording

grow.py     an unanswered sentence -> a proposed tree, a scaffold, a log
service.py  one process, one registry, HTTP endpoints, hot reload, gaps,
            unanswered
hub.py      peers, link up/stale/down, routing by capability, forwarding
cli.py      check · list · run · match · ask · serve · hub · gaps · unanswered · new
```

**Reading a sentence** (`nlp.py`, `intent.py`) is rules all the way down,
and the trace's first step (`intent`) names each rule that fired. The
rules: fold unicode; drop greetings, politeness and request wrappers from
the edges (kept when a trigger in the swarm starts or ends with the word);
expand contractions; turn number words into digits; match a literal word
to its plural or singular with a conservative stemmer and an exception
list. A tree adds `synonyms` and may opt into a `typos` rule (one edit from
exactly one pattern word; a word close to two is left alone). Typed slots
(`date`, `time`, `duration`, `number`, `host`, `url`, `email`) each have a
fixed parser; a value a parser cannot read, or could read two ways, makes
the trigger refuse the sentence. A `date` is read against the runner's
clock, which is the same clock `{{ now }}` reads, so invariant 1 holds:
same tree, same inputs, same clock, same path. There is no scoring, no
weighting, no fallback; the most literal characters still win and a tie is
still reported. `pyrtan match` prints the whole reading.

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
`pyrtan run --record` produces fixtures through the third. The transport
is also the seam where forms of I/O plug in. A node names a source (`http`
today), the runtime enforces that source's allow-list, and the transport
moves the bytes. A file, shell, serial or queue adapter is a new node
source with the same three parts, and the same recorded and recording
wrappers around it.

**Growth** (`grow.py`) is where a dead end becomes the next tree. A
sentence no tree answers is recorded as *unanswered*, counted, and
answered with a proposal: a name drawn from the words, a trigger with slots
where the sentence had numbers or hosts, the inputs those slots need, and
the `pyrtan new … --from` command that writes a scaffold which passes
`check`. The proposal is derived by rules, not by a model, and it is a
first draft for an author, not an answer for a caller. A service without a
hub keeps its own unanswered log; one with a hub leaves the record to the
hub, which also collects its peers' at `/unanswered`.

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

**A model, or a classifier, for intent.** The obvious way to make "could
you tell me how warm it is in Oslo" find the weather tree is to embed
sentences and triggers and pick the nearest. Rejected, for the same reason
a model is rejected inside a run: the choice would be a score nobody can
read, and the trace would begin with a guess. What was built instead is a
short list of rewrites and a short list of parsers, each of which either
fires and says so or does nothing. The cost is that a sentence outside
the rules is not read, and is recorded as unanswered with a proposed
trigger; the benefit is that `pyrtan match` can show, line by line, why a
sentence went where it went, and that the same sentence always goes there.

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
against. Not built. A sentence no tree answers returns `no-match`, is
recorded as unanswered, and comes back with the tree that would answer it.
The honest response to a frequent `no-match` is a new tree, and the swarm
now hands the author its first draft rather than an excuse.

**A message bus between services.** NATS or similar would give the swarm
events and fan-out. v0.1 has `act.emit`, which records an event on the
service; nothing consumes it yet. Request/response over HTTP covers every
case the examples needed, and a bus would be a dependency and an
operational component before there is a swarm large enough to need one.

**Persisting gaps and unanswered sentences.** In v0.1 both lived in a
service's or hub's memory and were lost on restart: enough to prove the
two loops (run, see gap, add branch, reload; ask, see unanswered,
scaffold, reload), not enough for a fleet. v0.2 keeps gaps in a JSON-lines
file per service (`--gaps`), deduplicated with a count; the hub still
collects from peers rather than keeping its own. Unanswered sentences are
still in memory; the same store is the obvious next step.

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
2. **v0.2, built: a swarm that can leave the LAN, and trees as a shared
   thing.** A shared token between hub and services, CORS for the console,
   gaps persisted to a file per service, an opt-in egress policy, the
   console with a peers view off `/peers`, and the catalog (`pyrtan
   search`, `pull`, `index`) with `version` pinning a pull. Design in
   [`platform.md`](platform.md). Still to come from the original v0.2: a
   `pyrtan watch` that re-runs a set of sentences on a schedule and
   reports when a path changes (the way to learn a site has changed before
   a person does).
3. **v0.3: more forms of I/O.** First the browser: `observe` with
   `browser = { url, wait_for, actions }` on Playwright as an optional
   extra, for pages that need a script to render, with the same host rule
   and the same extraction. Then file, shell, serial and queue adapters,
   each a node source with its own allow-list (paths, commands, devices,
   topics), so a tree reads a log, runs a named command or polls a device
   by the same rules it reads a page, and the trace reads the same.
4. **v0.4: the catalog grows up.** Namespaces, publishing through pull
   requests with the checker as the gate, and opt-in gap reporting from
   hubs back to a tree's author.
5. **Beside the swarm, never inside it:** an authoring aid that reads gaps
   and drafts the branch for a person to accept.

## 7. Open questions

- **Authentication between services.** v0.2 answers the first half: a
  shared bearer token, presented by services on registration and by the
  hub on every forward, with `/register` behind it. mTLS, and a token per
  service rather than per swarm, wait for a swarm that needs them.
- **What a tree's `version` governs.** In the catalog it is what a pull
  pins. On the hub it is still undecided: a hub could prefer the highest
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

It does not claim to answer sentences nobody wrote a tree for; it claims
to say which tree would, and to write that tree's first draft. It does not
claim to reach anything beyond HTTP yet. It does not claim trees are quick
to write: the weather tree is 15 nodes for a two-call
API, and that is the price of leaving nothing implicit. It does not claim
the swarm is secure across a network boundary (Section 7). It does not
claim the examples will keep working as the sites they read change; it
claims that when they stop, the trace will say exactly where, and a gap
will say what was seen.

## The name

The name Pyrtan was used earlier for an unrelated building-management
product. None of its code is in this repository. The one idea carried over
is that a fleet's health is one pollable field, which is the hub's `link`.
