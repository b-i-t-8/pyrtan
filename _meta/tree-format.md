# The tree format

A tree is one TOML file. This is the whole of it.

## Top level

| Key | Required | Meaning |
| :-- | :-- | :-- |
| `name` | yes | Lowercase letters, digits and dashes. The tree's identity in the swarm. |
| `description` | should | One line. Shown by `pyrtan list` and in `/capabilities`. |
| `version` | no | Positive integer, default 1. |
| `hosts` | if any node speaks HTTP, drives a browser or talks to a queue | The hosts the tree may reach: exact names, `*.example.com`, or `*` for a tree whose host comes from its input. |
| `paths` | if any node reads or writes a file | Absolute paths or globs (`/var/log/*`); a directory covers everything under it; `*` is everything. |
| `commands` | if any node runs a program | Program names (`df`) or full paths (`/usr/bin/df`); `*` is everything. |
| `devices` | if any node opens a serial line | Device paths or globs (`/dev/ttyUSB*`). |
| `topics` | if any node talks to a queue | MQTT topic filters: `+` one level, `#` the rest (`home/+/temperature`). |
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

A slot is `{name}` (any text) or `{name:kind}`. Every slot must be a
declared input whose type fits the kind. `[words]` are optional; `(a|b)`
is a required choice; `|` separates alternatives inside either. A pattern
needs at least one literal word.

| Kind | Reads | Produces | Input type |
| :-- | :-- | :-- | :-- |
| `text` (the default) | any text | as written | `str` |
| `word` | one word, no spaces | as written | `str` |
| `int`, `float` | digits | a number | `int` / `float` |
| `number` | digits or words: `3`, `twenty-one`, `a dozen`, `1,000`, `2.5` | a number | `int` or `float` |
| `date` | `tomorrow`, `friday`, `next friday`, `in 3 days`, `2 weeks ago`, `oct 25`, `25 october 2026`, `2026-10-25`, `13/10` (day first), `end of month` | `YYYY-MM-DD` | `str` |
| `time` | `3pm`, `15:30`, `3:05 pm`, `noon`, `midnight`, `9 o'clock` | `HH:MM` | `str` |
| `duration` | `2 hours`, `30 min`, `1h30m`, `an hour and a half` | seconds | `int` |
| `host` | a host name or a URL | the host, lowercase | `str` |
| `url` | `http(s)://…` | as written | `str` |
| `email` | an address | lowercase | `str` |

A slot's text keeps its case. A `date` is read against the service's clock
(`{{ now }}` in templates is the same clock), so a run is deterministic
given its observations; the trace records what the words were read as.
A value the kind cannot read (`3/10/2026`, which could be either order;
`someday`) makes the trigger refuse the sentence, and the reading says so.

### How a sentence is read

Before any trigger sees a sentence, fixed rules rewrite it, and the trace's
first step (`intent`) names each rule that fired:

1. **Fold:** unicode normalised, curly quotes straightened, spaces collapsed,
   punctuation around the sentence dropped.
2. **Fillers:** greetings, politeness and request wrappers dropped from the
   edges (`hey pyrtan`, `please`, `can you`, `could you`, `thanks`…), unless
   a trigger in the swarm starts or ends with that word.
3. **Contractions:** `what's` → `what is`, `isn't` → `is not`, `gimme` →
   `give me`…
4. **Numbers:** number words become digits: `three` → `3`, `twenty-one` →
   `21`, `two hundred and five` → `205`.

Patterns get the first and third rule at load time, so `[what's the]` and
`what is the` are the same trigger. Matching ignores case, and a literal
word matches its plural or singular (`story` / `stories`; `news` stays
`news`). None of this is a model: every rule is a table in
`src/pyrtan/nlp.py`, and `pyrtan match "sentence"` shows what each one did.

When several trees match a sentence, the trigger with the most literal
characters wins. An exact tie is answered as `ambiguous`, naming the
candidates, rather than resolved by load order.

### `[language]`

What a tree adds to the fixed rules:

```toml
[language]
synonyms = { weather = ["forecast", "temperature"], hacker = ["hn", "y combinator"] }
typos = true
```

`synonyms` maps a literal word in this tree's patterns to other words (or
phrases) that read the same; the plural rule applies to them too. `typos`
(default `false`) lets a sentence word of five letters or more that is one
edit away from exactly one word in this tree's patterns be read as that
word; a word close to two is left alone, because choosing would be a guess.
The trace shows every correction (`typo: wether->weather`). A hub applies a
tree's `[language]` when it routes, because `/capabilities` carries it.

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

### The other sources

Instead of `http`, an `observe` or `act` may name one of these; each
needs the matching allow-list at the top of the tree, checked at load
time when the value is literal and at run time when it is templated. The
result lands under `into` and `extract` reads it the same way (every
source leaves `body`). The design is in [`io.md`](io.md).

```toml
file   = { path = "/var/log/syslog", tail = 200 }                 # observe: path, exists, size, modified, body, json, lines
file   = { path = "./notes.txt", append = "visited\n" }          # act: … plus written  (or write = "…")
shell  = { command = "df", args = ["-P", "/"] }                    # command, args, status (exit code), ok, stdout, stderr, body, json, lines
serial = { device = "/dev/ttyUSB0", baud = 9600, write = "AT\r\n", read_until = "\n" }   # device, baud, sent, body, bytes, complete, json, lines
queue  = { url = "mqtt://broker.local", topic = "home/kitchen/temperature" }               # observe: received, message { topic, body, json }, body, json
queue  = { url = "mqtt://broker.local", topic = "home/kitchen/light", publish = "on" }     # act: … plus published
browser = { url = "https://app.example/", wait_for = "h1", actions = [{ click = "#more" }, { fill = { selector = "#q", value = "x" } }] }  # url, status, ok, title, body (rendered), elapsed_ms
```

`shell` runs one program with its arguments as a list; no shell reads the
line. `serial` reads until `read_until`, `read` bytes or `timeout`.
`queue` is MQTT 3.1.1 at QoS 0; `mqtts://` for TLS, `username` and
`password` if the broker wants them. `browser` needs Playwright
(`pip install 'pyrtan[browser]'`); its actions are `click`, `fill`,
`press`, `select`, `wait_for` and `wait` (ms), and a navigation to a host
outside `hosts` fails the node. A service started with `--egress public`
refuses `file`, `shell` and `serial` altogether.

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
