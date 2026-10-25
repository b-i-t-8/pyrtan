# Pyrtan

**Open-source total automation: a core set of expandable services that act on all forms of I/O
through hardcoded, inspectable decision trees.**

Pyrtan looks like an assistant from the outside. You give it a sentence; it
does something on the web and tells you what happened. Inside there is no
model. Every service is a set of **trees**: declarative files that say which
sentences they answer to, which hosts they may touch, what they observe, and
exactly how they decide what to do next. Every run produces a **trace** that
names each observation and the condition that chose each branch.

That is the trade. A model generalises to sentences nobody wrote a tree for;
Pyrtan does not, and says so. In return, Pyrtan is deterministic, costs
nothing per run, works offline against recorded responses, cannot reach a
host its author did not list, and can always explain itself.

Trees are close in spirit to Claude's skills, with two differences. They are
much narrower: a tree does one thing against one web property. And they are
live: a service reloads a tree the moment its file changes, a tree can hand
part of its work to any other tree in the swarm, and every decision that
meets a value no branch covers is recorded as a **gap**, which is how a tree
tells its author where to grow next.

```
$ pyrtan run "what's the weather in Oslo" --trees examples
trace: weather
  geocode (observe): GET https://geocoding-api.open-meteo.com/v1/search?name=Oslo&… -> 200; hit={…}, lat=59.91, lon=10.75, name=Oslo, country=Norway -> found
  found (decide) [exists True]: state.hit = {…} -> forecast
  forecast (observe): GET https://api.open-meteo.com/v1/forecast?latitude=59.91&… -> 200; temp=4.2, humidity=80, wind=12.3, code=61, unit=°C -> have-temp
  have-temp (decide) [type 'number']: state.temp = 4.2 -> sky
  sky (decide) [between [51, 67]]: state.code = 61 -> rain
  rain (set): sky=raining -> reply
  reply (done): Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h.

Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h.
```

## Install

Python 3.12 or later. No runtime dependencies. Not yet on PyPI; install
from the repository:

```
uv tool install git+https://github.com/b-i-t-8/pyrtan      # or: pip install git+https://github.com/b-i-t-8/pyrtan
pyrtan --version
```

From a checkout, `uv sync` then `uv run pyrtan …`, and `uv run pytest`.

## A tree

```toml
name = "github-release"
description = "The latest release of a GitHub repository."
hosts = ["api.github.com"]
start = "fetch"

[[triggers]]
pattern = "[what's the] latest release of {repo:word}"

[inputs.repo]
type = "str"
pattern = "^[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+$"

[nodes.fetch]
kind = "observe"
http = { method = "GET", url = "https://api.github.com/repos/{{ inputs.repo }}/releases/latest" }
into = "resp"
extract = [{ into = "tag", json = "json.tag_name" }, { into = "url", json = "json.html_url" }]
next = "status"

[nodes.status]
kind = "decide"
on = "resp.status"
branches = [
  { when = { eq = 200 }, then = "reply" },
  { when = { eq = 404 }, then = "none" },
  { when = { in = [403, 429] }, then = "rate-limited" },
  { else = "unexpected" },
]

[nodes.reply]
kind = "done"
say = "{{ inputs.repo }}: {{ state.tag }} {{ state.url }}"
output = { repo = "{{ inputs.repo }}", tag = "{{ state.tag }}", url = "{{ state.url }}" }

[nodes.none]
kind = "fail"
reason = "no-release"
say = "{{ inputs.repo }} has no releases, or does not exist."

[nodes.rate-limited]
kind = "fail"
reason = "rate-limited"
say = "GitHub is rate-limiting unauthenticated requests."

[nodes.unexpected]
kind = "fail"
reason = "unexpected-response"
say = "GitHub answered {{ state.resp.status }}."
```

Node kinds: `observe` (read; retried), `act` (change; never retried),
`decide` (one value, plain conditions, one branch), `set`, `each` (walk a
list with a limit, through another tree or a local sub-flow), `delegate`
(hand a sentence or a tree name to the swarm), `ask` (pause for an input),
`done` and `fail`. Trees do not loop; `pyrtan check` refuses a cycle. The
whole format is in [`_meta/tree-format.md`](_meta/tree-format.md).

## The command

| Command | What it does |
| :-- | :-- |
| `pyrtan check DIR` | Validates every tree: references, conditions, selectors, hosts, templates, reachability, cycles. Exit 1 on any error. |
| `pyrtan list DIR` | What is loaded and the sentences it answers to. |
| `pyrtan run "…" --trees DIR` | Answers a sentence in this process, printing the trace. `--replay FIXTURE` answers HTTP from a recording; `--record FIXTURE` makes one. |
| `pyrtan run --tree NAME --input k=v` | Runs a named tree. |
| `pyrtan serve DIR --port 8701` | Hosts the trees as a service: `/health`, `/capabilities`, `/intent`, `/run`, `/resume`, `/gaps`. Reloads trees when their files change. |
| `pyrtan hub --port 8700` | Routes between services. Services register with `--hub URL`; peers carry a `link` of `up`, `stale` or `down`. |
| `pyrtan ask "…" --at URL` | Answers a sentence through a running service or hub. |
| `pyrtan gaps --at URL` | Every decision that met a value with no branch: the list of what to write next. |
| `pyrtan new NAME` | Scaffolds a tree file. |

## A swarm

```
pyrtan hub --port 8700
pyrtan serve examples/weather --port 8701 --hub http://127.0.0.1:8700
pyrtan serve examples/hacker-news --port 8702 --hub http://127.0.0.1:8700
pyrtan ask "top 3 stories on hacker news"
```

Open <http://127.0.0.1:8700/> for the demo page: a sentence box, the
answer, the output and the trace, and everything the swarm can answer. To
run the demo on a NAS or any Docker host, see [`deploy/README.md`](deploy/README.md).

A service and a hub answer the same requests, so a caller never needs to
know which it is talking to. A swarm starts as one process holding every
tree and splits into many without the callers changing. A tree that
`delegate`s a sentence it cannot answer itself sends it to its hub, which
routes it to whichever peer can; the peer's steps come back inside the
caller's trace.

## What Pyrtan refuses

- **No model in the loop.** Nothing in this repository calls an LLM. A model
  may one day sit *beside* the swarm, proposing branches for recorded gaps;
  it will not sit inside a run deciding anything.
- **No host a tree did not name.** Every HTTP node's host is checked against
  the tree's `hosts` at load time where it is literal and at run time where
  it is templated.
- **No code in trees.** Templates are paths and filters; conditions are
  comparisons. Branching is only ever a `decide` node, visible in the trace.
- **No loops.** A path through a tree is finite by construction.
- **No silent retries of actions.** An `observe` is retried; an `act` is not.

## Layout

```
src/pyrtan/     the package: tree, intent, template, conditions, extract,
                html, transport, runtime, trace, registry, service, hub, cli
examples/       trees that run against real public APIs, and offline against
                tests/fixtures/examples.json
tests/          pytest; a swarm of two services behind a hub runs on localhost
_meta/          the design record and the tree format reference
deploy/         Dockerfile at the root; a compose stack for OpenMediaVault
                and a systemd unit, with the steps for each
```

## Status

0.1.0. The core, the service, the hub and the command are built and tested.
Not yet built: a browser adapter (for pages that need a script to render),
authentication between services, persistent gap storage, and a tree
marketplace. See [`_meta/design.md`](_meta/design.md) for the sequencing.

Pyrtan is a product of [Hillvon.io](https://hillvon.io).

## License

Pyrtan is free software under the GNU Affero General Public License,
version 3 or (at your option) any later version: `AGPL-3.0-or-later`. The
full text is in [LICENSE](LICENSE).

In short: you may use, change and share it. If you distribute a modified
Pyrtan, or run one as a network service that other people use, you must
offer those users its source under the same license. Trees you write and
run with Pyrtan are your own; they are data the program reads, not part of
the program.
