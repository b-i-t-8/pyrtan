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
answers is recorded as **unanswered** with a proposed tree, and a builder
that asks the few things the proposal cannot know. That is how the swarm
tells its authors where to grow next. An **agent**, an open-weights
model you start in the console, goes looking for both and suggests what
to write; it sits beside the trees, never inside a run, and a person
accepts what it suggests.

```
$ pyrtan run "what's the weather in Oslo" --trees examples
trace: weather
  geocode (observe): GET https://geocoding-api.open-meteo.com/v1/search?name=Oslo&… -> 200; hit={…}, lat=59.91, lon=10.75, name=Oslo, country=Norway -> found
  found (decide) [exists True]: state.hit = {…} -> forecast
  forecast (observe): GET https://api.open-meteo.com/v1/forecast?latitude=59.91&… -> 200; temp=4.2, humidity=80, wind=12.3, code=61, unit=°C -> have-temp
  have-temp (decide) [type 'number']: state.temp = 4.2 -> sky
  sky (lookup) [between [51, 67]]: state.code = 61; sky=raining -> reply
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
expect = { "json.tag_name" = { type = "str" } }     # a 200 without one is drift, not a release called "None"
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
`decide` (one value, plain conditions, one branch), `set`, `lookup` (one
value, plain conditions, one row of a table read into state), `each` (walk a
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
| `memory = { key }` or `{ key, write / add / delete, ttl }` | `keys` | what a tree keeps across runs: the last reading, a count of checks in a row, whether it already said so |

The trace reads the same whichever source a node used (`shell df -P / ->
exit 0`, `file /var/log/syslog -> 48213 B`), and one fixture file answers
for all of them, so every example runs offline in the test suite and the
same vectors hold in the JavaScript runtime. A service started with
`--egress public` refuses files, programs, serial lines and memory
outright: a hosted box is not lent out. Memory lives in the file a
service's `--memory` names, and nowhere else: it is not served, not
gossiped and not handed to members. The design is in [`_meta/io.md`](_meta/io.md).

Text that is not JSON can be read with the same patterns triggers use,
instead of a regex: `{ into = "load", pattern = "load average: {l:number}" }`
finds the words in what was observed and reads the slot with its kind's
parser (`number`, `date`, `duration`, `host`…); a pattern with several
slots gives a table of them.

A tree that others delegate to can say what its output carries,
`returns = { title = "str", score = "int?" }`, and `pyrtan check` then
holds both sides to it: the tree's own endings, and every caller that
reads a field of that output. A run that would hand back something else
stops as `contract` instead ([`_meta/tree-format.md`](_meta/tree-format.md#returns)).

## The command

| Command | What it does |
| :-- | :-- |
| `pyrtan check DIR` | Validates every tree: references, conditions, selectors, hosts, templates, reachability, cycles. Warns about a branch an earlier one already covers and a condition no value meets, and names the numbers a decision without `else` leaves to a gap. Exit 1 on any error. |
| `pyrtan list DIR` | What is loaded and the sentences it answers to. |
| `pyrtan run "…" --trees DIR` | Answers a sentence in this process, printing the trace. `--replay FIXTURE` answers HTTP from a recording; `--record FIXTURE` makes one. `--dry-run` reads for real but stops before the first act and says what it would do. An act with `confirm` asks first, at the terminal. |
| `pyrtan run --tree NAME --input k=v` | Runs a named tree. |
| `pyrtan test DIR` | Runs each tree's own `[[tests]]` against their recordings: a sentence or inputs, and the status, answer, output and path the run must come to. Exit 1 if any misses. |
| `pyrtan diff FILE` | What an edit changes: every test the file and its last committed version carry (`--against REF`, or `--old FILE`), run on both against the same recordings; names each run whose answer or path moved. Exit 1 if any did. |
| `pyrtan memory FILE` | What memory nodes keep in a `--memory` file (`serve`, `hub`, `watch` and `run` take `--memory FILE`, or `$PYRTAN_MEMORY`): every key, its value, when it was written and until when. `--forget KEY` drops one. |
| `pyrtan match "…" --trees DIR` | How a sentence reads, without running anything: the rewrites, every trigger's verdict, the winner. The tool for writing triggers. |
| `pyrtan serve DIR --port 8701` | Hosts the trees as a service: `/health`, `/capabilities`, `/intent`, `/run`, `/resume`, `/gaps`, `/unanswered`, `/suggestions`. Reloads trees when their files change. `--gaps FILE` keeps gaps across restarts; `--egress public` refuses private addresses. |
| `pyrtan hub --port 8700` | Routes between services. Services register with `--hub URL`; peers carry a `link` of `up`, `stale` or `down`. `--trees DIR` holds trees in the hub itself; `--federate` lets other hubs follow it and it follow them; `--listed` puts it in the directory; `--lan` finds hubs on the local network by multicast, with nobody to ask; `--chat` keeps a lobby where people meet strangers, agents and bots (`--no-chat-typing` turns its typing indicator off, `--chat-public` lets callers without the token in, to meet people only; `--chat-reach N` links an open lobby with up to N others, for chat only). |
| `pyrtan keep KEY [FILE] --at HUB` | Gives your hub a blob to keep; its keepers get copies. `kept` shows who holds what; `fetch KEY` gets a blob back from your hub or whoever holds it. |
| `pyrtan follow [URL] --at HUB` | Makes your hub follow another: it then routes sentences to that hub's trees too. With no URL, `pyrtan join`. `--network NAME` follows within a network of yours; `--listed` also puts your hub in the directory. `unfollow` undoes it; `peers` lists services, hubs followed and followers (members marked, the trusted ones too); `drop ID` lets a follower go (a lost phone); `directory` lists every hub that asked to be found, as the swarm knows it. |
| `pyrtan join [INVITE] --at HUB` | Puts your hub in a network and follows a hub in it: the default network (built in), or the one an invite (`NAME:SECRET@HUB`) names. |
| `pyrtan network new NAME --at HUB` | Starts a network of your own, a network of one, and prints its invite. `network list` shows each network with its invite; `network leave NAME` cuts the links made in it. |
| `pyrtan ask "…" --at URL` | Answers a sentence through a running service or hub. `--dry-run` asks it to stop before every act. |
| `pyrtan chat --at HUB` | Talks to a random stranger, an agent or a bot through a hub started `--chat`. `--bot` for a bot, `--agent` for an agent, `--people` for people only, `--game` to play "person or agent?" (`/guess person` or `/guess agent`), `--interests` to meet someone who shares one, `--trace` to see the path behind each bot line, `--no-typing` to hide "Stranger is typing…". |
| `pyrtan gaps --at URL` | Every decision that met a value with no branch: the branches to write next. |
| `pyrtan unanswered --at URL` | Every sentence no tree answered, with the tree that would: the trees to write next. `--scaffold DIR` writes them; `--build DIR` asks you about each one first. |
| `pyrtan suggestions --at URL` | What agents beside the swarm suggested: a branch's meaning, a trigger, a tree. `pyrtan grow --suggestions URL_OR_FILE --trees DIR --apply` writes them, through the rules. |
| `pyrtan new --from "…"` | Scaffolds a tree file from a sentence: numbers and hosts in it become slots, and the file passes `pyrtan check`. `pyrtan new NAME` scaffolds from a name. |
| `pyrtan build --from "…"` | The builder: asks what the tree reads (an API or page, a file, a program, a browser, another tree, or nothing), what to pull out, what it decides on and which branches, and what it says; every answer has a default drawn from the sentence by rules, and the file it writes passes `pyrtan check`. `--answer k=v` answers ahead, `--yes` takes the defaults, `--show` prints instead of writing. The console offers the same questions on any unanswered sentence. |
| `pyrtan search WORDS` | Finds trees in a catalog (default: the one at pyrtan.com). |
| `pyrtan pull NAME[@VERSION]` | Fetches a tree from a catalog into your trees folder, verified against the catalog's index. |
| `pyrtan index DIR` | Builds a catalog `index.json` from a directory of trees, and `trees.json` beside it (the same trees as JSON, for the console in a browser); what a catalog's CI runs. |
| `pyrtan console --out FILE` | Writes the console as one static HTML file, to host anywhere; there the browser is a hub of its own until it joins one. It carries the default trees, so it holds them with no network; `--trees DIR` carries others instead. |
| `pyrtan id new` | Makes your identity: a root key and the signed document hubs will hold. `id device add pyr:…` delegates to a device key, `id revoke` takes it back, `id route add HOST` says where you can be reached, `id name alice@domain` signs a name claim for a domain to serve (`--into` a checkout for a name at pyrtan.com), `id check` checks an identifier or any of those documents, `id show` shows them. |
| `pyrtan login --at URL --user NAME` | Signs in to a service or hub started with `--ldap`, with your directory account, and prints a session token to use as `--token` (or `$PYRTAN_TOKEN`) until it expires; `pyrtan logout` ends it. The password comes from a prompt, or `$PYRTAN_PASSWORD`. |
| `pyrtan allow WHO --may ABILITIES --at HUB` | Grants an identity (`pyr:…`, or a name the hub resolves and pins) abilities on your hub: `ask`, `chat`, `run`, `operate`. `allowed` lists the grants; `disallow` takes them back. Every command that calls a hub takes `--sign` to sign as your identity instead of (or beside) `--token`. |
| `pyrtan names init DIR --domain D` | Writes a names repository for a network: a README, `names/`, and for GitHub (or GitLab) the gate and Pages. `names gate` judges a change to names owned by their keys; `hub --names DIR` serves them. `id backup` / `restore` keep your key encrypted with a passphrase; `id release` gives a name up. |
| `pyrtan id resolve NAME@DOMAIN` | What identity a name is, checked against the key's own signature and pinned the first time; a changed answer is refused until `--repin`. `id pins` lists the pins; `id wellknown DIR --domain D` builds `/.well-known/pyrtan/` for a domain to serve names. |

`serve` and `hub` take `--token` (or `$PYRTAN_TOKEN`) to require
`Authorization: Bearer` on everything but `/health` and the console, and
`--cors ORIGIN` to let a browser at another origin call. `ask`, `gaps` and
`unanswered` take `--token` too. With a token, `--ldap URL` lets people in
with their directory account as well (*LDAP users*, below).

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
<https://pyrtan.com/console/>, and there it needs no hub at all: until you
join one, the browser is the hub. It holds trees with no network to
fetch them from: every console carries the default trees (the examples and
the catalog, which a release ships inside the package; `pyrtan console
--trees DIR` carries others), and takes a fresher catalog only from its own
site (`../catalog/`, or one on the same origin named in the address,
`?catalog=`). It reads the
sentence and runs the tree in the page (`pyrtan.js`, the same runtime and
the same reading as Python's), and keeps gaps and unanswered sentences
in the browser across reloads, under the device that met them: the
browser's key when its Identity tab has made one, else an id of its own.
Make or forget a key and they move with it; the Gaps tab names the
device and forgets them. The devices of one identity share them with each
other, browser to browser and with no hub: the Gaps tab links two on the
same network by a code carried by hand or a QR (a WebRTC link, asking no
server, that carries nothing until each side has proved it is a device of
the same identity), or carries a signed file between any two; each lists
the others' gaps beside its own, by device
([`_meta/identity.md`](_meta/identity.md) §5b). Every example runs there but six: weather
today and tomorrow, sunrise, UV and air quality, local time, elevation,
postcodes, capitals, pollen, waves, Wikipedia, words (definitions,
synonyms, antonyms, rhymes), exchange rates and crypto prices, Hacker
News, GitHub, packages (npm versions and downloads, PyPI, Go, Homebrew,
NuGet), DNS, books, recipes and cocktails, food barcodes, earthquakes,
NASA's natural events, the ISS and who is in space, launches, space
news, NASA's picture of the day, Formula 1, holidays, the Wayback
Machine, Pokémon, your IP and where it is, quotes, jokes, advice and cat
facts. The six that need a hub reach a box or any site at all:
`disk-space`, `log-errors`, `modem-check`, `room-temperature`,
`rendered-headline` and `site-status`. Trees that need a box (files, programs, serial
lines, brokers, a browser to drive), a site that does not let a page
read it, or the operator's credentials (`--env`, as the Postfinite trees
need) say so; for those, join a hub started with
`--cors https://pyrtan.com` by giving its URL and token. Forget it and the
browser is its own hub again. The wordmark is two colours, **pyr** the
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
networks, and members (phones, browsers) only with its token or a key it
grants `run`, so a stranger can join neither as a hub nor as a member. `--state FILE` keeps the relationships and networks across
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

The **default network** is built into pyrtan: `pyrtan`, with a secret
that is public on purpose. It names the network rather than guarding
it: anyone may join, and what keeps a stranger's hub harmless is that
following is still the one act of trust. `pyrtan join` (or `pyrtan hub
--join default`, or `$PYRTAN_JOIN=default`) puts a hub in it and follows
the first hub that accepts, trying what the hub already knows before
asking anyone: the hubs it follows in the network, then the ones its
directory has heard in it, then the hub list, then the snapshot, each
fetched only if the ones before it did not do. So a hub that has been in
the network once never needs pyrtan.com again; when nobody accepts, the
hub is the network so far, and others join through it. `$PYRTAN_HUB_LIST`
takes any number of sources (a hub list, any hub's `/directory`, a
snapshot or a mirror of one), so pyrtan.com is one starting point among
any others. On one LAN not even that is needed: hubs started `--lan`
(with `--advertise http://<LAN address>:PORT`) announce themselves over
UDP multicast that never passes a router, and `pyrtan join` tries the
hubs heard there right after the ones it follows, so two boxes in one
house find each other with the internet unplugged. pyrtan.com runs no hub: it is static files on GitHub Pages,
and publishes the default network's name and secret only as a mirror. Every
hour its build asks every hub it knows of for its directory (the hub
list, then everyone the last snapshot or a hub names), keeps the default
network's part, and publishes it as `rendezvous/directory.json`, so the
directory stays current while any hub it knows of is up, and no hub has
to be always up. To be on the hub list, a hub in the default network,
started `--listed` and `--cors https://pyrtan.com`, goes into
`site/rendezvous/hubs.json` by pull request.

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
on, generated from the default trees by the Python runtime and checked
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
anything by it: each hub keeps its own list of who may do what.

**No shell needed.** The console's **Identity** tab makes your identity
in the browser and hands you its backup at once, encrypted with your
passphrase, to download or keep in your own private GitHub repository;
restores it in another browser; claims a name, at pyrtan.com (GitHub's
own page opens the pull request from your account) or at any network's
names repository; and pairs your other devices, by a QR code the new
device's camera opens and six digits both screens show (a link straight
between the two browsers, `_meta/identity.md` §5b), or by a code each
shows and a note it gets back. The **Access** tab keeps a hub's grants. The same,
from a shell:

```
pyrtan id new                                       # your root key, ~/.config/pyrtan/identity/
pyrtan id backup --out backup.json                  # encrypted with a passphrase; restore with pyrtan id restore
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

**Any network can run its own names**, in any git repository, owned by
their keys: a new name is open to whoever signs it, it moves only with a
handover its old key signs, and is given up only by a release it signs.
The gate reads nothing but signatures, so it runs on any git host.

```
pyrtan names init names-repo --domain names.example.org --host github   # or gitlab, or plain
pyrtan id name alice@names.example.org --into names-repo               # the first name
pyrtan hub --names names-repo/names --names-domain names.example.org   # hubs can serve them too
```

**Grants** replace handing out the token. A hub grants identities
abilities: `ask` (the routed paths), `chat` (the lobby), `run` (be a
trusted member and run its trees) and `operate` (what the token allows);
a device gets what its root is granted, capped by its note.

```
pyrtan hub --token … --allow alice@pyrtan.com=ask,run     # a name is resolved and pinned now
pyrtan allow bob@example.org --may chat --at HUB           # or later, as the operator
pyrtan ask "how full is the disk" --at HUB --sign          # signed with your key; no token
pyrtan id push --at HUB                                    # your newest devices and revocations
```

A request signed with a key is checked against the hub's own id (`GET
/api` names it), so it cannot be replayed at another hub. The console's
**Identity** tab makes a key for the browser (WebCrypto, kept in the
browser, never readable by the page), takes the note `pyrtan id device
add` prints for it, and from then on signs what it sends; a member
joined from there follows as that key, and is trusted when its root
holds `run`. A revoked or expired device, or one whose grant is taken
back, is dropped. The design is in
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
`catalog-gate` workflow runs main's copy of the same gate (every tree loads clean with a
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

### Postfinite's data

[Postfinite](https://postfinite.com) keeps published reference data
(electricity grids and their carbon, rivers, tides, buoys, space weather,
power prices, central-bank rates, trade, freight, housing, health,
release calendars and more, each from a free, keyless public source under
its own licence) in every entitled account's database, beside the
account's own devices, with horizon tables that forecast each series and
score the forecast against what happened. The `postfinite` namespace in
the catalog reads all of it through Postfinite's API, as the `pyrtan`
tenant, an internal account entitled to every published dataset:

| Tree | Answers | Reads |
| :-- | :-- | :-- |
| `postfinite-overview` | "how is postfinite doing" | what the account holds |
| `postfinite-datasets` | "what data does postfinite have" | every dataset: source, licence, cadence, theme |
| `postfinite-search` | "search postfinite for rotterdam" | entities by id, name or place |
| `postfinite-readings` | "latest readings for pf:grid:gb on postfinite" | every metric an entity reports, latest value |
| `postfinite-reading` | "carbon_intensity for pf:grid:gb on postfinite" | one metric's latest value |
| `postfinite-history` | "carbon_intensity for pf:grid:gb over the last 2 days on postfinite" | a metric over a span: readings, range, latest bucket |
| `postfinite-forecasts` | "postfinite forecasts" | each horizon table and its best-scoring method |

Each signs in on every run (an `act`, never retried), then decides on
every answer Postfinite can give: a refused, locked or identity-provider
sign-in, a session not accepted, an account without access, rate limits,
an outage, an empty result, a reading without a unit. Give the service an
account on the tenant; the credentials go into the request and nowhere
else, since a `decide` on an `env` value records only that it is `<set>`:

```
pyrtan serve catalog/postfinite --env postfinite_email=… --env postfinite_password=… --token …
```

Anyone who can ask that service reads what the account reads, hence the
token. Every console carries these trees too, but a page has no
credentials to give, so there they are marked *needs a hub*.

## Leaving the LAN

Nothing authenticates by default. When a hub is reachable from outside:

```
pyrtan hub --host 0.0.0.0 --token "$(openssl rand -hex 24)" --cors https://pyrtan.com
pyrtan serve trees --hub http://hub:8700 --token … --gaps /var/lib/pyrtan/gaps.jsonl --egress public
```

A swarm shares one token: a service presents it when it registers, the hub
presents it when it forwards, and a stranger can neither join nor ask.
People need not share it: grant their identities what each may do
(`--allow`, `pyrtan allow`; see *Identity* above), or let them sign in
with the account they already have.
`--egress public` makes a service refuse any host that resolves to a
loopback, private, link-local or metadata address, on the first URL and on
every redirect, so a tree whose host comes from its input cannot be
pointed at your own network. Put TLS in front with whatever proxy you
already run; Pyrtan does not do it itself.

### LDAP users

On top of the token, a service or hub takes a person's directory account
(OpenLDAP, Active Directory, FreeIPA, anything that speaks LDAPv3), and
what each person may do comes from the directory's groups, in the same
four abilities a key is granted: `ask`, `chat`, `run`, `operate`.

```
export PYRTAN_TOKEN="$(openssl rand -hex 24)"
pyrtan hub --host 0.0.0.0 --ldap ldaps://ldap.example.org \
    --ldap-user-dn 'uid={user},ou=people,dc=example,dc=org' \
    --ldap-group 'cn=pyrtan,ou=groups,dc=example,dc=org=ask,chat' \
    --ldap-group 'cn=admins,ou=groups,dc=example,dc=org=operate'

# Active Directory: search for the person as a service account, then bind as them
export PYRTAN_LDAP_BIND_PASSWORD=…
pyrtan hub --ldap ldaps://dc.corp.example --ldap-base 'dc=corp,dc=example' \
    --ldap-filter '(sAMAccountName={user})' --ldap-bind-dn 'cn=pyrtan,ou=service,dc=corp,dc=example' \
    --ldap-group 'cn=Pyrtan Users,ou=groups,dc=corp,dc=example=ask' --ldap-nested
```

A person sends `Authorization: Basic` with their name and password, or
signs in once (`POST /login`, `pyrtan login`, or the console's connect
box, which shows a user name and password beside the token) and sends the
session token it answers with as a bearer. A person in no listed group may
what `--ldap-may` says: `ask` when no group is listed, nothing otherwise.
A group's members are those its entry names in `member`, `uniqueMember` or
`memberUid`. With `--ldap-nested`, members of groups nested in a listed
group count too, to any depth: Active Directory follows the chain itself
(`LDAP_MATCHING_RULE_IN_CHAIN`, one search per group, cycles included).
A directory without that rule still counts direct members, and a primary
group (`primaryGroupID`, usually Domain Users) never counts. An empty password is refused
before it reaches the directory (most take it as an anonymous bind), five
wrong passwords for a name rest it for a minute, and the token is still
the swarm's: services register and hubs forward with it, never with a
person's account. Use `ldaps://` or `--ldap-starttls`; every flag has a
`$PYRTAN_LDAP…` variable. See `src/pyrtan/ldap.py`.

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

## Chat: strangers, agents and bots

A hub started `--chat` keeps a lobby, Omegle-style: press Start in the
console's **Chat** tab (or run `pyrtan chat`) and you are paired at random
with someone else on the hub, with someone waiting on a hub it follows,
with an agent someone seated there, or with a bot. Press Stop twice (or Esc twice) to end it and meet someone
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

**Agents.** Start an agent in the **Agents** tab, then pick *as my
agent* in the Chat tab, and your agent takes the seat: it reads the
stranger and answers in its own words while you watch, and meets one
stranger after another until you stop it. A line of persona ("a
night-shift nurse who loves chess") and your skills for its `lobby` and
`game` jobs say how it behaves. The hub runs no model: it relays the
lines and names the agent, so whoever meets it reads "You're now
chatting with an agent: scout (Qwen2.5 1.5B)", and its lines say
`Agent (scout):`. In the lobby an agent asks the swarm nothing and keeps
nothing. `pyrtan chat --agent` (or *an agent* in the Chat tab) meets
one; *people only* never does; a stranger who would meet anyone meets
people first, then an agent, and a bot last.

**Person or agent?** Tick *Play* in the Chat tab (or `pyrtan chat
--game`) and you are paired only with others playing: a person or an
agent, at even odds when both are waiting, unlabelled. Talk, then guess
(the buttons, or `/guess person` and `/guess agent`), once. When the
conversation ends, or you leave the lobby, you learn what it was,
whether you were right, your score, and what they guessed about you (a
tab that closed mid-game is told when the page opens again, within ten
minutes); an agent seated in the game plays to pass as a person, and its
console is told what you guessed.

```
$ pyrtan chat --game
  You're now chatting with someone: a person or an agent. Talk, then guess which (/guess person or /guess agent); the answer comes when the conversation ends.
Stranger: hey whats up
not much, you?
Stranger: nm, just got off a night shift. you?
/guess person
  You guessed a person. The answer comes when the conversation ends.
/next
  It was an agent: scout (Qwen2.5-1.5B-Instruct-q4f16_1-MLC). You guessed person: wrong. 0 of 1 right so far.
```

**Stranger is typing…** While someone types, their partner sees it, and
while a bot's trees run, "Bot is thinking…". It works both ways or not
at all: untick *Typing indicator* in the Chat tab (remembered in that
browser), or `pyrtan chat --no-typing`, and you neither share your
typing nor see anyone else's. `pyrtan chat` at a terminal reads keys as
you type them, so it shows your typing too (from a pipe it reads whole
lines and shows none), and Up and Down there bring back the lines you
sent this chat (kept in memory only, gone when it ends). An operator turns it off for the whole
hub with `pyrtan hub --chat --no-chat-typing` (`$PYRTAN_CHAT_TYPING=0`).
It is never stored: it goes out with the next line, or after five
seconds without news.

**Strangers across hubs.** A seat that finds nobody waiting asks the hubs
its hub follows with a signed `Meet`; the lines travel as `Say` and the
end as `Part`, typing as `Typing`, hub to hub, signed with the pair's
secret; the outbox logs a `Say` without its text and leaves `Typing` out. Players of *person or
agent?* meet across hubs the same way, with `Play` in place of `Meet`
and `Guess` carrying a guess to the other hub; the reveal says which hub
the stranger was on. A hub answers a `Meet` or a `Play` only from a
hub that follows it, and never passes one on. Following is trust (it
routes sentences to the other hub's trees), so to reach more strangers
without it, start an open lobby with `--chat-reach N`: the hub keeps up to
N *chat links* with other open lobbies it hears of (on the LAN with
`--lan`, or in its directory), for chat and nothing else; nothing is
routed, announced or kept over a link, and a hub takes one only when its
operator also gave `--chat-reach`.

The lobby follows the hub's token: with one, only callers who have it
chat, or a key the hub grants `chat` (a bot runs trees, so a lobby must
not be a way round it, and a `chat` grant is as good as `ask`). To let
strangers in and keep the token on everything else, start the hub
`--chat-public`: callers without the token are guests, who meet people
(here, or on a hub this one follows) and agents, and never a bot. There is no chat
server and no hub of pyrtan.com's: every hub started `--chat` is a lobby, and hubs
that follow each other share their strangers. It is
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
otherwise, and a new `fail` node that says what was seen. A `lookup`
with no `default` gets a row instead: the same condition, with the value
seen as its value. `--apply` writes it into the file, which must still
check clean; the person then says what the value means.

```
$ pyrtan grow --at http://127.0.0.1:8700 --trees trees --apply
# weather: in [nodes.sky], add to table:
  { between = [68, 70], value = 70 },
# why: the value falls in the hole between 67 and 71, which the ranges around it leave open
# written into trees/weather.toml; now edit the new row's value in [nodes.sky] to say what the value means
```

Unanswered sentences survive a restart too, with `--unanswered FILE` on a
service or hub.

## Agents: models beside the swarm

The console's **Agents** tab starts an agent: an open-weights model
(Qwen2.5, Qwen3, Phi-3.5 or SmolLM2, under Apache-2.0 or MIT) running
in the browser on WebGPU through WebLLM, in the browser on its CPU
through wllama (llama.cpp in WebAssembly), or on a server of your own
that speaks the OpenAI chat API (Ollama, llama.cpp). Nothing downloads
until you press Start.

Which browser:

| Browser | On its GPU (WebGPU) | On its CPU |
|---|---|---|
| Chrome, Edge 113+ (Windows, macOS, ChromeOS, Android) | yes | yes |
| Chrome on Linux | depends on the GPU and driver | yes |
| Safari 26+ | yes | yes |
| Firefox 141+ on Windows, 147+ on Apple silicon Macs | yes | yes |
| Firefox on Linux or Android | Nightly only, so far | yes |

The GPU is the quick one; Chrome or Edge is the safe choice. Where there
is no WebGPU (Firefox on Linux, say), the console picks **on its CPU**
for you: the same open weights as GGUF files (Qwen2.5 0.5B or 1.5B,
SmolLM2 1.7B), slower (seconds to a minute a reply, on one thread),
nothing to install. The agent is a client of the swarm like you are:

- **It tests the trees.** *Patrol* (or `/probe weather`) has it write
  sentences a person might say, read them here by the same rules the
  swarm reads them, and run the ones that reach the tree. A value no
  branch covers is a real gap, with a real trace. Sentences that read to
  nothing, or to the wrong tree, are what it reports next.
- **It suggests.** What a gap's value means (a `done` or a `fail`, and
  its words), a pattern that would let a tree read the sentences it
  missed, a tree for a sentence nobody answers. Each is checked by rules
  before you see it: the branch's condition is the one `pyrtan grow`'s
  rules draft, not the model's; a pattern is kept only if the reading
  says it reads the sentences and takes none from another tree; no
  suggestion adds a host, any I/O, or a read of `env`.
- **You accept.** In a browser that is its own hub, the tree grows in
  the page at once. On a hub, the suggestion is kept at `/suggestions`;
  `pyrtan grow --suggestions HUB --trees DIR --apply` checks it again
  and writes it (or download the accepted ones and point `--suggestions`
  at the file). Dismiss one and say why, and the agent remembers.
- **You can talk to it.** It answers in its own words and, when a tree
  can answer, asks the swarm and quotes the tree, whose name and path are
  shown beside what it said. `/help` lists the commands (`/ask`, `/read`,
  `/probe`, `/gaps`, `/unanswered`, `/patrol`, `/remember`, `/memory`).
- **It remembers and has skills.** Its memory (your notes, its own,
  what it probed, what was accepted, dismissed or refused, the
  conversation) is kept in the browser per hub, and the relevant part
  goes into every prompt. Its skills are instructions for each job, the
  way a Claude skill is: five built in, and your own (a name, when it
  applies, and the text; Markdown with front matter, or JSON), called as
  `/name` in chat.
- **It can meet strangers.** Seated in a hub's chat lobby from the Chat
  tab, it talks with strangers as an agent, named as one, or plays
  "person or agent?" (see [Chat](#chat-strangers-agents-and-bots)).

```
$ pyrtan suggestions --at http://127.0.0.1:8700
branch  weather.sky: code = 100 means fail: 'Code 100 is not a weather code we know.'  x1
        by scout (Qwen2.5-1.5B-Instruct-q4f16_1-MLC): WMO codes stop at 99
trigger weather: 'is it raining in {place}', covering 'is it raining in Oslo'  x2
        by scout (Qwen2.5-1.5B-Instruct-q4f16_1-MLC): people ask about rain
$ pyrtan grow --suggestions http://127.0.0.1:8700 --trees trees --apply
```

The design is in [`_meta/agents.md`](_meta/agents.md).

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

- **No model in the loop.** No run calls an LLM, and nothing in the
  Python package does. The reading of a sentence is tables and rules,
  each named in the trace when it fires. A model sits only *beside* the
  swarm, as an agent a person starts in the console: it asks like any
  caller and proposes branches, triggers and trees, which rules check
  and a person accepts. It never sits inside a run deciding anything.
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
                service, hub, federation, identity, names, grants, page (the console),
                suggestions, defaults (the trees every console carries),
                catalog, cli; js/ holds pyrtan.js (the runtime
                again), identity.js and agents.js (agents beside the swarm)
catalog/        trees by namespace, published by pull request; postfinite/
                reads Postfinite's data
examples/       trees that run against real public APIs, and offline against
                tests/fixtures/examples.json
tests/          pytest; a swarm of two services behind a hub runs on localhost
_meta/          the design record, the tree format reference, and the
                design for pyrtan.com
deploy/         Dockerfile at the root; a compose stack for OpenMediaVault
                and a systemd unit
site/           pyrtan.com: the landing page, the hub list, the catalog
                viewer, the source browser (files, releases, branches and
                history), and the build the pages workflow publishes
```

## Status

0.42.0. The core, the service, the hub, the command, the console (which
carries the default trees, so it holds them with no network), the
Postfinite trees (every published dataset, entity, reading and forecast
on Postfinite, as the pyrtan tenant), the
catalog (namespaces, publishing by pull request through the gate, gap
reports gossiped back to authors), the language rules, federation
between hubs, networks (any hub starts one; the default one is built
in; hubs on one LAN find each other by multicast), the gossiped directory, members (a browser or a phone as
part of the swarm), the blobs they keep and the trees they run
(`pyrtan.js`, conformant with the Python runtime), six forms of I/O
(HTTP, a browser, files, programs, serial lines, MQTT queues), each
behind an allow-list, watches (a tree that runs when a clock, a file, a
topic or a serial line speaks), `pyrtan grow` (the branch a gap asks
for, drafted by rules), lookup tables (a code into a word in one node),
acts that ask first (`confirm`) and dry runs that change nothing,
a check that reads a decision's order (dead branches, conditions nothing
meets, the holes a gap will fall into),
the shape of a good answer (`expect`, and drift when a source changes),
`pyrtan build` (the tree an unanswered sentence asks for, through a few
questions with defaults by rules), a tree's own tests and the diff an edit
makes to them (`pyrtan test`, `pyrtan diff`), memory across runs (a
`memory` source with its own `keys` list), and the chat lobby (strangers on the hub and the
hubs it follows, bots made of trees, agents in the seats, the game "person or agent?", a typing indicator, lobbies open to the public, and chat links between open lobbies) are built and tested; a swarm can require a
token, let people in with their LDAP accounts on top of it, keep its gaps and unanswered sentences, refuse private addresses
and its own box, and follow other swarms. Identities (keys, device notes,
name claims, names at pyrtan.com and pins, grants on hubs, the console's
key, `pyrtan id` and `pyrtan allow`) are built, and all of it works from
the console with no shell; encrypted key backups, and names any network
runs in its own git repository, too. Agents beside the swarm
(open-weights models in the browser or on a server of your own that
probe the trees, suggest branches, triggers and trees, chat, and keep a
memory and skills; `/suggestions` and `pyrtan grow --suggestions`) are
built. Not yet built: hosting.
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
