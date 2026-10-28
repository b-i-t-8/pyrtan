# Design — Pyrtan: a core set of expandable services that act on all forms of I/O through inspectable decision trees

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
  that is an author's tool, not a runtime. Since v0.18 it does: an agent
  in the console, checked by rules, accepted by a person
  ([`agents.md`](agents.md)).
- **Code in trees.** Templates are paths and filters. Conditions are
  comparisons. There is no expression language, no `if` in a template, no
  script node. Everything that branches is a `decide` node, which the trace
  shows, or a `lookup` node, which is the same plain conditions with a
  value per row instead of a node per branch. The cost is verbosity; the
  benefit is that a tree can be audited by someone who does not program.
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
cases.py    a tree's own [[tests]] against recordings; two versions diffed over them
service.py  one process, one registry, HTTP endpoints, hot reload, gaps,
            unanswered
hub.py      peers, link up/stale/down, routing by capability, forwarding
federation.py  actor, inbox, Follow/Accept/Undo/Announce, signed; one hop
cli.py      check · list · run · test · diff · match · ask · serve · hub · follow ·
            unfollow · peers · gaps · unanswered · new · search · pull · index ·
            console
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
is also the seam where forms of I/O plug in. A node names a source, the
runtime enforces that source's allow-list, and the adapter moves the
bytes. Since v0.7 `adapters.py` holds five more (`browser`, `file`,
`shell`, `serial`, `queue`), each a node table with a list of its own
kind, and the recorded and recording fixtures answer for all of them
under one key scheme (`io.md`).

**Growth** (`grow.py`) is where a dead end becomes the next tree. A
sentence no tree answers is recorded as *unanswered*, counted, and
answered with a proposal: a name drawn from the words, a trigger with slots
where the sentence had numbers or hosts, the inputs those slots need, and
the `pyrtan new … --from` command that writes a scaffold which passes
`check`. The proposal is derived by rules, not by a model, and it is a
first draft for an author, not an answer for a caller. A service without a
hub keeps its own unanswered log; one with a hub leaves the record to the
hub, which also collects its peers' at `/unanswered`.

**The builder** (`build.py`) is the next step after the proposal: the
questions the skeleton cannot answer by itself. What does the tree read
(an API or page, a file, a program, a browser, another tree, or nothing),
where, what does it pull out, what does it decide on and which branches,
what does it say. The questions are a decision tree of their own: which
apply depends on the answers before them, every one has a default drawn
from the sentence by rules (a host in the sentence becomes the URL, an
`api.` host means JSON, a program's exit code is decided on), and a build
with no answers at all passes `check`. An answer that will not do names
its question, so `pyrtan build` asks again and the console's form points
at the field. `POST /build` on a service or hub answers the questions and
the checked file for a form; nothing is written over the network, the
author saves the file. No model: a builder that guessed what the tree
should observe would be the model fallback of §4 wearing a form.

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

**A map filter, or a `set` that branches, for code-to-word tables.**
Before 0.20 the weather tree turned a weather code into a word with one
`decide` and seven `set` nodes, one per word. A filter such as
`{{ state.code | map(...) }}` would have been shorter and would have put a
decision inside a template, where the trace cannot see it. `lookup` is
the same conditions a `decide` uses, a value per row, the chosen row in
the trace, and a gap when no row holds; it does not branch, so it adds no
paths to the graph.

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
| `weather` | Two hops where the second depends on the first; `exists` deciding on absence; a code-to-word table as one `lookup`, not an expression |
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
   [`platform.md`](platform.md). The `pyrtan watch` the original v0.2
   promised (re-run on a schedule, report when a path changes) came in
   v0.9, as `[[watches]]`.
3. **v0.3, built: sentences read by rules, and hubs that follow each
   other.** The language rules and typed slots (§3, "Reading a
   sentence"), and federation in the shape of ActivityPub: a hub follows
   other hubs and routes to them, one hop, every message signed, following
   as the only trust. Design in [`federation.md`](federation.md).
4. **v0.4, built: no server anywhere.** The directory hubs find each
   other by is kept and gossiped by every federated hub, with a static
   list of hubs at pyrtan.com in place of a rendezvous hub (then
   `seeds.json`, now `hubs.json`); a hub can hold
   trees itself (`--trees`), so a federated swarm can be one process.
5. **v0.5, built: members and what they keep.** A browser or a phone
   joins a hub through a mailbox, carries the directory, and keeps the
   hub's blobs so a dead box loses nothing; the console installs as the
   phone app. Design in [`members.md`](members.md).
6. **v0.6, built: trees in the browser.** `pyrtan.js`, a second runtime
   for the same tree files, with the Python fixtures as shared
   conformance vectors (`tests/conformance/`), so a trusted member
   answers sentences through its hub's mailbox (`Ask`/`Answer`); a hub
   hands its own trees and `--handout` ones to the devices that joined
   with its token. Design in [`members.md`](members.md) §7.
7. **v0.7, built: more forms of I/O.** `browser = { url, wait_for,
   actions }` on Playwright as the one optional extra, for pages that need
   a script to render, with the same host rule and the same extraction;
   and `file`, `shell`, `serial` and `queue` (MQTT) as node sources, each
   with its own allow-list (`paths`, `commands`, `devices`, `topics`), so
   a tree reads a log, runs a named program or polls a device by the same
   rules it reads a page, and the trace reads the same. One fixture
   answers for every source, in both runtimes. Design in
   [`io.md`](io.md).
8. **v0.8, built: the catalog grows up.** Namespaces (`namespace/name`,
   the tree's own line), publishing through pull requests with the
   checker as the gate (`pyrtan publish`, `site/gate.py`, the `catalog`
   workflow, `OWNERS` per namespace), and opt-in gap reporting
   (`--report-gaps`) carried as a federation topic like the directory,
   baked hourly into pyrtan.com and shown beside each catalog tree.
   Design in [`platform.md`](platform.md) §2.8.
9. **v0.9, built: watches, and growing from a gap.** A tree carries
   `[[watches]]`: a clock, a file, a topic or a serial line starts a run
   (`watch.py`; `--watch` on a service or hub, `pyrtan watch` alone), the
   trace begins with a `watch` step, and `/watches` says what each last
   did and whether the path changed between runs. Beside the swarm, never
   inside it: `pyrtan grow` reads gaps (a service, a file, the
   federation's reports) and drafts the branch each asks for by rules
   (`grow.py`: a missing value, the hole between ranges, else the value),
   `--apply` writes it into the file, and a person says what it means.
   Unanswered sentences persist with `--unanswered FILE`.
10. **v0.18, built: agents beside the swarm.** Open-weights models a
    person starts in the console (WebLLM in the browser, or a server of
    their own) probe the trees with sentences of their own, say what a
    gap's value means, find the patterns trees missed and the trees
    nobody wrote, chat, and keep a memory and skills. What they suggest
    is checked by rules in the page, kept at `/suggestions`, and written
    by `pyrtan grow --suggestions` through the same rules. Design in
    [`agents.md`](agents.md).
11. **v0.20, built: lookup tables.** A `lookup` node reads one value
    against a table of rows (a condition and a value each), puts the
    first row's value into state, and records a gap when no row holds
    and there is no `default`; `pyrtan grow` drafts the missing row. The
    weather tree's code-to-word table went from eight nodes to one. Both
    runtimes, with conformance vectors.
12. **v0.22, built: the builder.** An unanswered sentence is answered
    with questions as well as a proposal: `pyrtan build --from "…"`, the
    same questions as `POST /build` and the console's Unanswered tab, each
    with a default by rules, and a file that passes `check` (`build.py`).
13. **v0.23, built: a tree's own tests, and what an edit changes.** A tree
    carries `[[tests]]`: a sentence or inputs, a recording beside the
    file, and the status, answer, output and path the run must come to.
    Invariant 1 makes the path a result worth asserting. `pyrtan test`
    runs them (and the catalog tests job does, for every pull request
    that touches a tree); `pyrtan diff` runs every test both versions of
    a file carry on both, against the same recordings, and names each
    run whose answer or path moved (`cases.py`).
14. **v0.25, built: memory across runs.** A seventh source, `memory`, with
    its own allow-list, `keys`: an observe reads a key, an act writes,
    counts on or forgets one, with an optional `ttl`. A recording keeps
    what was read, so invariant 1 holds and a tree's tests replay it. The
    store is the box's own file (`--memory`), refused under `--egress
    public`, never served or gossiped (`memory.py`, `io.md`).
15. **Later:** hosting (`platform.md` Phase 3); a serial line held open
    across the nodes of one run, for devices with a session.

## 7. Open questions

- **Authentication between services.** v0.2 answers the first half: a
  shared bearer token, presented by services on registration and by the
  hub on every forward, with `/register` behind it. mTLS, and a token per
  service rather than per swarm, wait for a swarm that needs them. For
  people and their devices, 0.14 answers it with identities: a key per
  person, and per-hub grants of what each may do (`identity.md` §7).
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
to write: the weather tree is 8 nodes for a two-call
API, and that is the price of leaving nothing implicit. It does not claim
the swarm is secure across a network boundary (Section 7). It does not
claim the examples will keep working as the sites they read change; it
claims that when they stop, the trace will say exactly where, and a gap
will say what was seen.

## The name

The name Pyrtan was used earlier for an unrelated building-management
product. None of its code is in this repository. The one idea carried over
is that a fleet's health is one pollable field, which is the hub's `link`.
