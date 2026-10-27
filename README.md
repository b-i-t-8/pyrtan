# Pyrtan

**Open-source total automation: a core set of expandable services that act on all forms of I/O
through inspectable decision trees.**

Pyrtan looks like an assistant from the outside. You give it a sentence; it
reads or changes something through whatever form of I/O it has an adapter
for, and tells you what happened. Today that form is HTTP: APIs and pages.
The core is built so that files, shells, serial lines, queues and devices
arrive as further adapters under the same rules. Inside there is no model.
Every service is a set of **trees**: declarative files that say which
sentences they answer to, which endpoints they may touch, what they observe,
and exactly how they decide what to do next. Every run produces a **trace**
that names each observation and the condition that chose each branch.

That is the trade. A model generalises to sentences nobody wrote a tree for;
Pyrtan does not, says so, and hands back the tree that would, ready to fill
in. In return, Pyrtan is deterministic, costs nothing per run, works offline
against recorded responses, cannot reach an endpoint its author did not
list, and can always explain itself.

Trees are close in spirit to Claude's skills, with two differences. They are
much narrower: a tree does one thing against one property, whether that is
an API, a site, a device or any other source or sink of I/O. And they are
live: a service reloads a tree the moment its file changes, a tree can hand
part of its work to any other tree in the swarm, every decision that meets a
value no branch covers is recorded as a **gap**, and every sentence nobody
answers is recorded as **unanswered** with a proposed tree. That is how the
swarm tells its authors where to grow next.

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

Trees that drive a browser need the one optional extra, Playwright:
`pip install 'pyrtan[browser]'` and `playwright install chromium` (or
point `PYRTAN_BROWSER` at a Chromium you have).
Signing and checking identities (`pyrtan id`) needs the other,
`cryptography`: `pip install 'pyrtan[identity]'`.

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

## Forms of I/O

HTTP is one source. An `observe` or `act` node may instead name a file,
a program, a serial line, a queue or a browser, and every one of them
follows the rule `hosts` set: the tree lists what the source may reach,
`pyrtan check` refuses a literal outside the list, the runtime refuses a
templated one, and nothing else is reachable.

```toml
commands = ["df"]
[nodes.df]
kind = "observe"
shell = { command = "df", args = ["-P", "{{ inputs.mount }}"] }     # one program, arguments as a list, no shell in between
into = "df"                                                          # status (exit code), ok, stdout, stderr, body, json, lines
extract = [{ into = "percent", regex = "(\\d+)%", group = 1 }]
```

| Source | Allow-list | Example |
| :-- | :-- | :-- |
| `file = { path, tail }` or `{ path, write / append }` | `paths` | [`log-errors`](examples/log-errors/log-errors.toml): error lines in the tail of a log |
| `shell = { command, args }` | `commands` | [`disk-space`](examples/disk-space/disk-space.toml): how full a filesystem is |
| `serial = { device, baud, write, read_until }` | `devices` | [`modem-check`](examples/modem-check/modem-check.toml): does the modem answer AT |
| `queue = { url, topic }` or `{ …, publish }` | `hosts`, `topics` | [`room-temperature`](examples/room-temperature/room-temperature.toml): a sensor's last reading over MQTT |
| `browser = { url, wait_for, actions }` | `hosts` | [`rendered-headline`](examples/rendered-headline/rendered-headline.toml): the heading of a page that needs a script to render |

The trace reads the same whichever source a node used (`shell df -P / ->
exit 0`, `file /var/log/syslog -> 48213 B`), and one fixture file answers
for all of them, so every example runs offline in the test suite and the
same vectors hold in the JavaScript runtime. A service started with
`--egress public` refuses files, programs and serial lines outright: a
hosted box is not lent out. The design is in [`_meta/io.md`](_meta/io.md).

## The command

| Command | What it does |
| :-- | :-- |
| `pyrtan check DIR` | Validates every tree: references, conditions, selectors, hosts, templates, reachability, cycles. Exit 1 on any error. |
| `pyrtan list DIR` | What is loaded and the sentences it answers to. |
| `pyrtan run "…" --trees DIR` | Answers a sentence in this process, printing the trace. `--replay FIXTURE` answers HTTP from a recording; `--record FIXTURE` makes one. |
| `pyrtan run --tree NAME --input k=v` | Runs a named tree. |
| `pyrtan match "…" --trees DIR` | How a sentence reads, without running anything: the rewrites, every trigger's verdict, the winner. The tool for writing triggers. |
| `pyrtan serve DIR --port 8701` | Hosts the trees as a service: `/health`, `/capabilities`, `/intent`, `/run`, `/resume`, `/gaps`, `/unanswered`. Reloads trees when their files change. `--gaps FILE` keeps gaps across restarts; `--egress public` refuses private addresses. |
| `pyrtan hub --port 8700` | Routes between services. Services register with `--hub URL`; peers carry a `link` of `up`, `stale` or `down`. `--trees DIR` holds trees in the hub itself; `--federate` lets other hubs follow it and it follow them; `--listed` puts it in the directory; `--chat` keeps a lobby where people meet strangers and bots. |
| `pyrtan keep KEY [FILE] --at HUB` | Gives your hub a blob to keep; its keepers get copies. `kept` shows who holds what; `fetch KEY` gets a blob back from your hub or whoever holds it. |
| `pyrtan follow [URL] --at HUB` | Makes your hub follow another: it then routes sentences to that hub's trees too. With no URL, `pyrtan join`. `--network NAME` follows within a network of yours; `--listed` also puts your hub in the directory. `unfollow` undoes it; `peers` lists services, hubs followed and followers; `directory` lists every hub that asked to be found, as the swarm knows it. |
| `pyrtan join [INVITE] --at HUB` | Puts your hub in a network and follows a hub in it: the default network pyrtan.com publishes, or the one an invite (`NAME:SECRET@HUB`) names. |
| `pyrtan network new NAME --at HUB` | Starts a network of your own, a network of one, and prints its invite. `network list` shows each network with its invite; `network leave NAME` cuts the links made in it. |
| `pyrtan ask "…" --at URL` | Answers a sentence through a running service or hub. |
| `pyrtan chat --at HUB` | Talks to a random stranger or a bot through a hub started `--chat`. `--bot` for a bot, `--people` for people only, `--interests` to meet someone who shares one, `--trace` to see the path behind each bot line. |
| `pyrtan gaps --at URL` | Every decision that met a value with no branch: the branches to write next. |
| `pyrtan unanswered --at URL` | Every sentence no tree answered, with the tree that would: the trees to write next. `--scaffold DIR` writes them. |
| `pyrtan new --from "…"` | Scaffolds a tree file from a sentence: numbers and hosts in it become slots, and the file passes `pyrtan check`. `pyrtan new NAME` scaffolds from a name. |
| `pyrtan search WORDS` | Finds trees in a catalog (default: the one at pyrtan.com). |
| `pyrtan pull NAME[@VERSION]` | Fetches a tree from a catalog into your trees folder, verified against the catalog's index. |
| `pyrtan index DIR` | Builds a catalog `index.json` from a directory of trees; what a catalog's CI runs. |
| `pyrtan console --out FILE` | Writes the console as one static HTML file, to host anywhere. |
| `pyrtan id new` | Makes your identity: a root key and the signed document hubs will hold. `id device add pyr:…` delegates to a device key, `id revoke` takes it back, `id route add HOST` says where you can be reached, `id name alice@domain` signs a name claim for a domain to serve (`--into` a checkout for a name at pyrtan.com), `id check` checks an identifier or any of those documents, `id show` shows them. |
| `pyrtan id resolve NAME@DOMAIN` | What identity a name is, checked against the key's own signature and pinned the first time; a changed answer is refused until `--repin`. `id pins` lists the pins; `id wellknown DIR --domain D` builds `/.well-known/pyrtan/` for a domain to serve names. |

`serve` and `hub` take `--token` (or `$PYRTAN_TOKEN`) to require
`Authorization: Bearer` on everything but `/health` and the console, and
`--cors ORIGIN` to let a browser at another origin call. `ask`, `gaps` and
`unanswered` take `--token` too.

## A swarm

```
pyrtan hub --port 8700
pyrtan serve examples/weather --port 8701 --hub http://127.0.0.1:8700
pyrtan serve examples/hacker-news --port 8702 --hub http://127.0.0.1:8700
pyrtan ask "top 3 stories on hacker news"
```

Open <http://127.0.0.1:8700/> for the console: a sentence box, the answer,
the output and the trace; everything the swarm can answer; each peer and
its `link`; and the gaps. The same console lives at
<https://pyrtan.com/console/> and can be pointed at any hub started with
`--cors https://pyrtan.com`. The wordmark is two colours, **pyr** the
primary and **tan** the secondary: click either half, right-click it, or
press and hold it on a phone to pick that half's colour; tan's picker also
sets light, dark or auto mode. The choice is kept in the browser. To run the swarm on a NAS or any Docker host,
see [`deploy/README.md`](deploy/README.md).

## A federation

A hub can follow other hubs, the way one account follows another on the
fediverse, and answer for their trees too:

```
pyrtan hub --port 8700 --name alpha --federate            # on one box
pyrtan hub --port 8700 --name beta --federate             # on another
pyrtan follow https://beta.example --at https://alpha.example
pyrtan ask "top 3 stories on hacker news" --at https://alpha.example   # answered by beta's service
```

Each federated hub has an actor document at `/actor` and an inbox; a
follow is a signed `Follow` activity, answered with `Accept` and an
`Announce` of what the followed hub's services hold, and a new `Announce`
whenever that changes. The shape is ActivityPub's; the wire format is
Pyrtan's own, signed with a secret the two hubs share, and nothing here
federates with Mastodon.

Three rules make it safe to run with strangers. **Following is trust, in
one direction:** a hub routes only to hubs its operator chose to follow,
and being followed grants nothing but announcements and the right to
ask. **Federation is one hop:** a hub announces what it holds itself,
never what it learned from others, and refuses to forward a request that
already crossed a hub, so there is nothing to loop. **Every message is
signed**, and a hub started with `--federation-secret` takes follows only
from hubs that know the secret; `--closed` takes none outside its
networks. `--state FILE` keeps the relationships and networks across
restarts.

Hubs find each other without a server. A hub started `--listed` puts an
entry for itself (name, URL, the trees it holds) in every announce it
sends, every hub keeps the entries it hears and passes them on in both
directions of every follow, and `pyrtan directory` (or the console, or
any hub's `/directory`) shows the result. An entry is a hint, not a
route: to use a hub you found, follow it. A hub can also hold trees
itself, `pyrtan hub --trees DIR`, so a whole federated swarm can be one
process on one box.

### Networks

A **network** is a name and a secret its hubs sign their follows with. A
hub can be in any number of them, and any hub can start one:

```
pyrtan network new home --at https://alpha.example        # a network of one; prints its invite
pyrtan join home:Qx…@https://alpha.example --at https://beta.example   # beta is in home, following alpha
pyrtan join --at https://gamma.example                    # gamma is in the default network
```

A follow made in a network names it, by an id drawn from the name and
the secret, and is signed with the secret, so only a hub holding the
invite gets in, and two networks that happen to share a name never
meet. What a hub hears of the directory over a link stays in that
link's network: a private network's hubs are passed on only within it,
shown at `/directory` only to the operator, and never handed to a
keeper. Leaving a network (`pyrtan network leave`) cuts the links made
in it and forgets what was heard there. A follow made outside any
network, and a member's, works as it always has.

The **default network** is the one pyrtan.com publishes in
`rendezvous/network.json`: `pyrtan`, with a secret that is public on
purpose. It names the network rather than guarding it: anyone may join,
and what keeps a stranger's hub harmless is that following is still the
one act of trust. `pyrtan join` (or `pyrtan hub --join default`, or
`$PYRTAN_JOIN=default`) puts a hub in it and follows the first hub in
`rendezvous/seeds.json` that answers, then the first from the snapshot;
when none does, the hub is the network so far, and others join through
it. No hub has to be always up: every hour pyrtan.com's build asks
every hub it knows of for its directory (the seeds, then everyone the
last snapshot or a hub names), keeps the default network's part, and
publishes it as `rendezvous/directory.json`, so the directory stays
current while any listed hub is up, and pyrtan.com itself is static
files on GitHub Pages. To be a seed, a hub in the default network,
started `--listed` and `--cors https://pyrtan.com`, goes into
`seeds.json` by pull request.

## Members, and what they keep

A browser or a phone cannot be reached, so it joins a hub the other way
round: the console's **Member** tab makes an identity for the device,
follows the hub with it, and polls a mailbox the hub keeps for it. A
member carries the directory like any hub, is listed in it if it wants
to be, and **keeps blobs**: the hub hands its own blobs (its directory,
its gaps, its unanswered sentences, anything given to `pyrtan keep KEY
FILE`) to the members and hubs that follow it, until `--replicas` of
them have confirmed, and asks for them back when it needs them. When the
hub's box dies and a new one comes up with the same name, its members'
next announce says what they kept and the hub takes it back; `pyrtan
kept` shows who holds what, `pyrtan fetch KEY` gets a blob back from
whoever has it. Install the console to the home screen and it is the
phone app, offline-capable. Nothing secret is ever a blob.

A member that joined **with the hub's token** is trusted, and **runs
trees**: the hub hands it its own trees and any `--handout DIR` (trees
for the devices, not the box) as blobs, the device announces them, and a
sentence for one of them goes into the device's mailbox as an `Ask` and
comes back as its `Answer`, with `peer: <member>` and `via: ["member"]`.
The runtime that does it is `pyrtan.js`, the same tree runtime in
JavaScript, served at `/pyrtan.js` by every hub and written beside the
console; `tests/conformance/` holds the vectors both runtimes must agree
on, generated from the examples by the Python runtime and checked
against the JavaScript one in the test suite. The design is in
[`_meta/members.md`](_meta/members.md). The answer to a federated
sentence carries `via: [the hub it crossed]`, and the console's Peers tab
shows who follows whom. The design is in
[`_meta/federation.md`](_meta/federation.md).

## Identity

A person, a hub or a device is a key, and its identity is that key
written out: `pyr:` and 56 characters, which anyone can check a
signature against without asking anybody. `pyr:<key>@host` (a hub, an
`.onion`) says where it can be reached; `alice@pyrtan.com` is a name a
domain vouches for. No domain owns an identity, and none grants
anything by it: each hub will keep its own list of who may do what.

```
pyrtan id new                                       # your root key, ~/.config/pyrtan/identity/
pyrtan id device add pyr:… --may chat,run --label phone
pyrtan id name alice@pyrtan.com --github <your numeric GitHub id>
pyrtan id check pyr:…@hub.example
```

`pyrtan id resolve alice@pyrtan.com` fetches the claim the domain serves
at `/.well-known/pyrtan/name/alice.json`, checks that the key signed it,
and pins the answer: if the domain later names a different key, you are
told rather than switched. Any domain can serve names (`pyrtan id
wellknown DIR --domain D` builds the files). pyrtan.com hands out
`<your GitHub login>@pyrtan.com` by pull request: `pyrtan id name
<login>@pyrtan.com --into <checkout>`, then a pull request from that
account, and the gate checks the account's numeric id
([`site/names/README.md`](site/names/README.md)).

Built so far: identifiers, the signed documents (the identity document,
device notes, name claims), names and pins, and the command. Grants on
hubs come next. The design is in
[`_meta/identity.md`](_meta/identity.md).

## The catalog

Trees other people wrote are at <https://pyrtan.com/catalog/>, a directory
of static files anyone can mirror. Every tree there is `namespace/name`:
the namespace is who published it, so two people can each have a
`weather`.

```
pyrtan search weather
pyrtan pull pyrtan/weather --dir trees     # a bare name is enough when one namespace has it
pyrtan serve trees
```

A pull checks the file's hash against the catalog's index, loads it as a
tree, and checks the name, namespace and version inside before writing
it; a file you have edited is not overwritten without `--force`. Point
`--catalog` (or `$PYRTAN_CATALOG`) at a directory or URL of your own to
use a private catalog; `pyrtan index DIR...` builds one from any folders
of trees (`--copy` lays them out as `namespace/name.toml`). A tree in a
catalog says what others may do with it in a `license` line (an SPDX
identifier such as `MIT`) and who publishes it in a `namespace` line.

**Publishing is a pull request.** `pyrtan publish my-tree.toml --into
<checkout of this repository> --owner <your GitHub login>` puts the file
under `catalog/<namespace>/`, writes the namespace's `OWNERS` if it is
new, and runs the gate; push the branch and open a pull request. The
`catalog` workflow runs the same gate (every tree loads clean with a
licence and a namespace, sits where its namespace and name say, ids are
unique, only a namespace's owners change it, a changed file bumps its
`version`), a maintainer merges, and the next build serves it. No
account, no API, no model in the review: [`catalog/README.md`](catalog/README.md).

**Gaps come back to the author.** A hub started with `--report-gaps`
puts the gaps its catalog trees meet (the tree's `namespace/name@version`,
the node, the path decided on and the value no branch covered, with a
count; never the inputs) into its federation announces. Every federated
hub carries and gossips them like the directory, `/reports` shows them,
pyrtan.com bakes them hourly into `rendezvous/reports.json`, and the
catalog page shows each tree's beside it, so `pyrtan reports
pyrtan/weather` tells an author where reality disagreed with their tree
across every swarm that opted in.

## Leaving the LAN

Nothing authenticates by default. When a hub is reachable from outside:

```
pyrtan hub --host 0.0.0.0 --token "$(openssl rand -hex 24)" --cors https://pyrtan.com
pyrtan serve trees --hub http://hub:8700 --token … --gaps /var/lib/pyrtan/gaps.jsonl --egress public
```

A swarm shares one token: a service presents it when it registers, the hub
presents it when it forwards, and a stranger can neither join nor ask.
`--egress public` makes a service refuse any host that resolves to a
loopback, private, link-local or metadata address, on the first URL and on
every redirect, so a tree whose host comes from its input cannot be
pointed at your own network. Put TLS in front with whatever proxy you
already run; Pyrtan does not do it itself.

A service and a hub answer the same requests, so a caller never needs to
know which it is talking to. A swarm starts as one process holding every
tree and splits into many without the callers changing. A tree that
`delegate`s a sentence it cannot answer itself sends it to its hub, which
routes it to whichever peer can; the peer's steps come back inside the
caller's trace.

## Watches: trees that run on their own

A tree may carry `[[watches]]`: a clock, a file, a topic or a serial line
starts a run without anyone asking, with `inputs` rendered from what
spoke (`event`), `env` and `now`.

```toml
[[watches]]
name = "example.com"
every = "15m"
inputs = { host = "example.com" }

[[watches]]
file = "/var/log/syslog"                                   # each time lines are appended
inputs = { text = "{{ event.body | strip }}" }

[[watches]]
queue = { url = "mqtt://broker.local", topic = "home/+/temperature" }   # each message
```

`pyrtan serve trees --watch` (or `pyrtan hub --trees trees --watch`) runs
them beside the service; `pyrtan watch trees` runs them alone and prints
each run. A watched run's trace begins with a `watch` step, the way an
asked run's begins with `intent`. `/watches` and `pyrtan watches --at URL`
show each watch, what it last did, and, for a watch that runs the same
tree again and again, whether the **path changed** between two runs:
`site-status` going from `up` to `erroring` on a clock is how you learn a
site changed before anyone asks. Watches are static: the path, topic or
device is literal in the file and checked against the tree's lists by
`pyrtan check`, and a service started `--egress public` watches no file
and no serial line.

## Chat: strangers and bots

A hub started `--chat` keeps a lobby, Omegle-style: press Start in the
console's **Chat** tab (or run `pyrtan chat`) and you are paired at random
with someone else on the hub, with someone waiting on a hub it follows,
or with a bot. Press Stop twice (or Esc twice) to end it and meet someone
new. Nobody has a name, and nothing is kept.

```
$ pyrtan hub --trees examples --chat
$ pyrtan chat --bot --interests disk
  You're now chatting with a bot: hub, this hub's own trees. It is 11 trees, not a person and not a model, and every line it says comes with the path that produced it. Try “how full is the disk”.
hi
Bot: I'm hub, this hub's own trees. It is 11 trees, …
what's the weather in Oslo
Bot: Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h.
top 3 stories on lobsters
Bot: No service in the swarm answers 'top 3 stories on lobsters'. To write the tree that would: pyrtan new top-stories-lobsters --from "top 3 stories on lobsters"
```

A **bot is not a model**: it is one holder of trees on the network (the
hub's own, a service, a trusted member, a hub it follows), picked at
random, one whose trees share a word with your interests first. What you
say is read against its triggers, then asked of the whole swarm; when
nobody answers, it says so and names the tree that would, and the
sentence is recorded as unanswered like any other. An `ask` node's
question is its next line and your reply is the answer. Every line it
says carries its trace. [`small-talk`](examples/small-talk/small-talk.toml)
answers "how are you" and "are you a bot"; a bare "hi" is filler, and
the bot answers it with who it is.

**Strangers across hubs.** A seat that finds nobody waiting asks the hubs
its hub follows with a signed `Meet`; the lines travel as `Say` and the
end as `Part`, hub to hub, signed with the pair's secret, and the
outbox logs a `Say` without its text. A hub answers a `Meet` only from a
hub that follows it, and never passes one on.

The lobby follows the hub's token: with one, only callers who have it
chat (a bot runs trees, so a lobby must not be a way round it). It is
text only, a line is at most 1,000 characters, five in a burst and one a
second after; a seat nobody polls for a minute is let go. There is no
moderation but leaving. The design is in [`_meta/chat.md`](_meta/chat.md).

## When no service in the swarm answers

A sentence nobody answers is not a dead end; it is the next tree. The hub
(or a service on its own) records it at `/unanswered` and answers with a
**proposal**: a tree name drawn from the words, a trigger pattern with slots
where the sentence had numbers or hosts, the inputs those slots need, and
the one command that writes the file.

```
$ pyrtan ask "top 3 stories on lobsters"
error (no-match): No service in the swarm answers 'top 3 stories on lobsters'. To write the tree that would: pyrtan new top-stories-lobsters --from "top 3 stories on lobsters"

$ pyrtan unanswered --at http://127.0.0.1:8700 --scaffold trees/
'top 3 stories on lobsters'  x1
    would be: top-stories-lobsters  answering  top {count:int} stories on lobsters
    wrote trees/top-stories-lobsters.toml
now: pyrtan check trees/
```

The scaffold is a complete tree that checks clean: a trigger, its inputs, an
`observe`, a `decide` on the status and a `done` and `fail`. The author
fills in what it observes and how it decides; a service serving that
directory picks the file up the moment it is saved. No model drafts
anything: the proposal is derived from the words by rules, and the demo page
shows the same proposal and the same command whenever nothing answers.

A **gap** gets the same treatment one level down. `pyrtan grow` reads
gaps (from a running service or hub, a `--gaps` file, or the federation's
reports for your catalog trees) and drafts, by rules, the branch each one
asks for: `{ exists = false }` for a missing value, the range of the hole
a number fell into between the ranges the node already has, `{ eq = … }`
otherwise, and a new `fail` node that says what was seen. `--apply`
writes it into the file, which must still check clean; the person then
says what the value means.

```
$ pyrtan grow --at http://127.0.0.1:8700 --trees trees --apply
# weather: in [nodes.sky], add to branches (before the else, if any):
  { when = { between = [68, 70] }, then = "sky-70" },
...
# why: the value falls in the hole between 67 and 71, which the ranges around it leave open
# written into trees/weather.toml; now edit [nodes.sky-70] to say what the value means
```

Unanswered sentences survive a restart too, with `--unanswered FILE` on a
service or hub.

## Sentences

Pyrtan reads a sentence with rules, not a model, and the trace's first
step shows exactly what the rules did:

```
$ pyrtan match "Hey Pyrtan, what's the forecast in Oslo please?" --trees examples
sentence: "Hey Pyrtan, what's the forecast in Oslo please?"
read as:  'what is the forecast in Oslo'
    fillers: 'Hey Pyrtan', 'please'
    contractions: what's->what is
triggers:
    weather: "[what's the] weather in {place}"  18 literal chars  -> match
        inputs: {"place": "Oslo"}
winner: weather
```

The fixed rules: unicode folded; greetings, politeness and request
wrappers dropped from the edges; contractions expanded; number words
turned to digits (`top three stories` → `top 3 stories`); a literal word
matching its plural or singular. A tree adds its own in a `[language]`
table: `synonyms` (`weather = ["forecast", "temperature"]` is how the
example above matched) and an opt-in `typos` rule that reads a word one
edit from exactly one pattern word as that word, and never chooses between
two.

Slots can be typed, each with a fixed parser: `{when:date}` reads
`tomorrow`, `next friday`, `in 3 days` or `oct 25` as `YYYY-MM-DD` against
the service's clock; `{at:time}` reads `3pm` or `15:30`; `{for:duration}`
reads `an hour and a half` as seconds; `{n:number}` reads `twenty-one`;
`{site:host}`, `{u:url}` and `{to:email}` read what they say. A value a
parser cannot read, or could read two ways (`3/10/2026`), makes the
trigger refuse the sentence rather than guess, and the reading says which
slot refused. The whole set of rules is in
[`_meta/tree-format.md`](_meta/tree-format.md); the tables are in
`src/pyrtan/nlp.py`.

## What Pyrtan refuses

- **No model in the loop.** Nothing in this repository calls an LLM. The
  reading of a sentence is tables and rules, each named in the trace when
  it fires. A model may one day sit *beside* the swarm, proposing branches
  for recorded gaps; it will not sit inside a run deciding anything.
- **No endpoint a tree did not name.** Every HTTP node's host is checked
  against the tree's `hosts` at load time where it is literal and at run
  time where it is templated. Every further form of I/O arrives with an
  allow-list of the same kind, or does not arrive.
- **No code in trees.** Templates are paths and filters; conditions are
  comparisons. Branching is only ever a `decide` node, visible in the trace.
- **No loops.** A path through a tree is finite by construction.
- **No silent retries of actions.** An `observe` is retried; an `act` is not.

## Layout

```
src/pyrtan/     the package: tree, nlp, intent, template, conditions, extract,
                html, transport, runtime, trace, registry, gaps, grow,
                service, hub, federation, identity, names, page (the console),
                catalog, cli
examples/       trees that run against real public APIs, and offline against
                tests/fixtures/examples.json
tests/          pytest; a swarm of two services behind a hub runs on localhost
_meta/          the design record, the tree format reference, and the
                design for pyrtan.com
deploy/         Dockerfile at the root; a compose stack for OpenMediaVault
                and a systemd unit
site/           pyrtan.com: the landing page, the seed list, the catalog
                viewer, the source browser (files, releases, branches and
                history), and the build the pages workflow publishes
```

## Status

0.11.0. The core, the service, the hub, the command, the console, the
catalog (namespaces, publishing by pull request through the gate, gap
reports gossiped back to authors), the language rules, federation
between hubs, networks (any hub starts one; pyrtan.com publishes the
default), the gossiped directory, members (a browser or a phone as
part of the swarm), the blobs they keep and the trees they run
(`pyrtan.js`, conformant with the Python runtime), six forms of I/O
(HTTP, a browser, files, programs, serial lines, MQTT queues), each
behind an allow-list, watches (a tree that runs when a clock, a file, a
topic or a serial line speaks), `pyrtan grow` (the branch a gap asks
for, drafted by rules), and the chat lobby (strangers on the hub and the
hubs it follows, and bots made of trees) are built and tested; a swarm can require a
token, keep its gaps and unanswered sentences, refuse private addresses
and its own box, and follow other swarms. Identities (keys, device notes,
name claims, names at pyrtan.com and pins, `pyrtan id`) are built;
grants on hubs are not yet. Not yet built: hosting.
See [`_meta/design.md`](_meta/design.md) for the runtime's design and
[`_meta/platform.md`](_meta/platform.md) for pyrtan.com.

## License

Pyrtan is free software under the GNU Affero General Public License,
version 3 or (at your option) any later version: `AGPL-3.0-or-later`. The
full text is in [LICENSE](LICENSE).

In short: you may use, change and share it. If you distribute a modified
Pyrtan, or run one as a network service that other people use, you must
offer those users its source under the same license. Trees you write and
run with Pyrtan are your own; they are data the program reads, not part of
the program.
