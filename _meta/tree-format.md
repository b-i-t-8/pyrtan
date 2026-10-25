# The tree format

A tree is one TOML file. This is the whole of it.

## Top level

| Key | Required | Meaning |
| :-- | :-- | :-- |
| `name` | yes | Lowercase letters, digits and dashes. The tree's identity in the swarm. |
| `description` | should | One line. Shown by `pyrtan list` and in `/capabilities`. |
| `version` | no | Positive integer, default 1. |
| `hosts` | if any node speaks HTTP | The hosts the tree may reach: exact names, `*.example.com`, or `*` for a tree whose host comes from its input. |
| `start` | yes | The first node. |
| `tags` | no | Free strings, for people and for the hub's listing. |
| `license` | for a catalog | An SPDX identifier such as `MIT`, `Apache-2.0` or `CC0-1.0`: what others may do with the file. A tree without one runs anywhere but is left out of a catalog. |
| `triggers` | no | Sentences the tree answers to (below). |
| `inputs` | no | What the tree needs (below). |
| `nodes` | yes | The nodes (below). |

TOML puts top-level keys before the first `[table]`; `start` after
`[inputs.x]` lands inside `inputs.x`, and the loader says so.

## Triggers

```toml
[[triggers]]
pattern = "[what's the] weather in {place}"

[[triggers]]
pattern = "top {count:int} stories on hacker news"
inputs = { count = 1 }       # fixed inputs this sentence implies
```

A slot is `{name}` (any text), `{name:int}`, `{name:float}` or `{name:word}`
(no spaces). Every slot must be a declared input. `[words]` are optional;
`(a|b)` is a required choice; `|` separates alternatives inside either.
Matching ignores case, surrounding punctuation and repeated spaces; a slot's
text keeps its case. A pattern needs at least one literal word.

When several trees match a sentence, the trigger with the most literal
characters wins. An exact tie is answered as `ambiguous`, naming the
candidates, rather than resolved by load order.

## Inputs

```toml
[inputs.count]
type = "int"              # str | int | float | bool | list | dict
default = 1               # an input with a default is optional
min = 1
max = 30
one_of = [1, 5, 10]
pattern = "^[a-z]+$"      # for str
description = "How many"
required = false          # optional with no default: present as null
```

Inputs are coerced (`"5"` to 5, `"yes"` to true) and checked before the
first node runs; a bad input is a `bad-input` error, never a half run.

## Nodes

Every node has `kind`. Every non-terminal node except `decide` has `next`.
Node ids are lowercase letters, digits and dashes.

### `observe`: read

```toml
[nodes.fetch]
kind = "observe"
http = { method = "GET", url = "https://api.example.com/x/{{ inputs.id }}", headers = { Accept = "application/json" } }
into = "resp"             # state.resp = { status, ok, headers, body, json, url, elapsed_ms }
extract = [
  { into = "title", json = "json.title" },
  { into = "price", regex = "\\$([0-9.]+)", group = 1 },
  { into = "heading", css = "h1" },
  { into = "links", css = "a.story", attr = "href", all = true },
  { into = "rows", css = "table tr", all = true, fields = { name = "td:nth-child(1)", href = { css = "a", attr = "href" } } },
]
timeout = 10              # seconds
retries = 1               # on transport failure only; never on a status code
next = "status"
```

`method` must be GET, HEAD or OPTIONS. Instead of `http`, `from = "resp"`
extracts from state already held. An extraction that finds nothing sets
nothing; decide on absence with `{ exists = false }`.

Selectors support tag, `#id`, `.class`, `[attr]`, `[attr=v]`, `[attr^=v]`,
`[attr$=v]`, `[attr*=v]`, `:first-child`, `:last-child`, `:nth-child(n)`,
`:contains(text)`, the descendant and `>` combinators, and `,` groups.
Anything else fails `pyrtan check`.

### `act`: change

Same shape as `observe` with any method, or an event:

```toml
[nodes.notify]
kind = "act"
emit = { event = "order.placed", data = { id = "{{ state.order.id }}" } }
into = "sent"
next = "reply"
```

An act is never retried. A GET with no body in an act is a warning: it is a
read, and `observe` would retry it safely.

### `decide`: branch

```toml
[nodes.status]
kind = "decide"
on = "resp.status"        # a state path; "resp.status" means "state.resp.status"
branches = [
  { when = { eq = 200 }, then = "reply" },
  { when = { between = [500, 599] }, then = "down" },
  { else = "unexpected" },
]
```

Conditions, all of which must hold within one `when`: `eq`, `ne`, `lt`,
`lte`, `gt`, `gte`, `between = [lo, hi]`, `matches = "regex"`, `contains`,
`startswith`, `endswith`, `in = [...]`, `exists = true|false`,
`empty = true|false`, `type = "str|int|float|number|bool|list|dict|null"`.
Numbers compare loosely with numeric strings; booleans compare strictly.

The first branch whose condition holds is taken. `else` must be last. A
`decide` with no `else` that meets a value no branch covers records a
**gap** (`tree`, `node`, `on`, `observed`) and ends the run with reason
`gap`. `pyrtan gaps --at URL` lists them.

### `set`: compute

```toml
[nodes.self-post]
kind = "set"
set = { url = "https://news.ycombinator.com/item?id={{ inputs.id }}", n = "{{ state.ids | length }}" }
next = "reply"
```

### `each`: walk a list

```toml
[nodes.stories]
kind = "each"
over = "ids"
limit = "{{ inputs.count }}"
tree = "hacker-news-item"          # run this tree per item …
with = { id = "{{ item }}" }
into = "stories"                   # … collecting each output (null where one failed)
stop_on_fail = false
next = "reply"
```

Or `start = "node"` instead of `tree`, to run a sub-flow of this tree per
item with `{{ item }}` and `{{ index }}` in scope, ending at the sub-flow's
first `done` (its `output` is collected) or `fail`. The sub-flow shares the
run's state. An `each` without `limit` is a warning.

### `delegate`: ask the swarm

```toml
[nodes.weather]
kind = "delegate"
intent = "weather in {{ inputs.place }}"     # or: tree = "weather", with = { place = "…" }
into = "w"                                   # state.w = { status, say, output, reason, tree }
next = "check"
```

A delegate is resolved locally first, then through the service's hub. The
delegated run's steps appear in the caller's trace one level deeper. A
delegated tree cannot `ask`.

### `ask`: pause for a person

```toml
[nodes.which]
kind = "ask"
prompt = "Which city?"
into = "inputs.city"          # or a state path
next = "geocode"
```

The run returns `asked` with the prompt and a snapshot; `/resume` (or
`pyrtan run` interactively) continues it. Only a top-level run can ask.

### `done` and `fail`: finish

```toml
[nodes.reply]
kind = "done"
say = "{{ state.name }}: {{ state.temp }}°"
output = { place = "{{ state.name }}", temperature = "{{ state.temp }}" }

[nodes.unknown-place]
kind = "fail"
reason = "unknown-place"      # a short code, for callers
say = "I could not find {{ inputs.place }}."
```

In `say` and `output` a missing value renders as empty rather than failing
the run, since the run is over.

## Templates

`{{ path }}` with optional filters: `{{ inputs.city | url }}`. Roots:
`inputs`, `state`, `item`, `index` (inside `each`), `tree` (`name`,
`version`), `now` (`iso`, `date`, `epoch`, `year`), `env` (values the
service was started with, for tokens). Paths take `.key`, `[0]`, `[-1]`,
`["key"]`.

Filters: `url`, `json`, `int`, `float`, `str`, `lower`, `upper`, `strip`,
`title`, `first`, `last`, `length`.

A string that is exactly one placeholder keeps the value's type (a list
stays a list). A placeholder that resolves to nothing fails the node, except
in `done` and `fail`, so a tree must decide on absence rather than paste an
empty string into a URL.

## Outcomes

| `status` | Meaning | Carries |
| :-- | :-- | :-- |
| `done` | a `done` node was reached | `say`, `output` |
| `failed` | a `fail` node was reached | `reason`, `say` |
| `asked` | an `ask` node paused the run | `question`, `snapshot` |
| `error` | the run could not proceed | `reason`: `no-match`, `ambiguous`, `no-such-tree`, `bad-input`, `host-not-allowed`, `transport`, `gap`, `template`, `too-deep`, `too-many-steps`, `not-a-list`, `each-failed`, `ask-in-delegate`, `ask-in-subflow`, `dead-end` |

Every outcome carries `trace`: `steps` (node, kind, summary, observed,
chosen, next, elapsed_ms, depth) and `gaps`.

A `no-match` error also carries `output.proposal`: the tree that would
answer the sentence, as `name`, `trigger` (slots where the sentence had
numbers or hosts), `inputs`, and `scaffold`, the `pyrtan new … --from`
command that writes it. The sentence is recorded at `/unanswered`.
