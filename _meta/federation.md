# Design — Federation: hubs that follow each other

| | |
|---|---|
| **Status** | **Built, v0.3; the directory redone in v0.4.** `src/pyrtan/federation.py`, the hub's federation routes, `pyrtan follow` / `unfollow` / `peers` / `directory`, the console's federation and directory tables, and tests of two and three hubs over real sockets. |
| **Date** | 2026-10-25 |
| **Decision** | A hub may follow other hubs, in the shape of ActivityPub (actor, inbox, Follow/Accept/Undo/Announce), and route sentences to the hubs it follows. Following is trust, in one direction, and federation is one hop. Hubs find each other through a directory every hub keeps and gossips; nothing central runs. |
| **Where** | `src/pyrtan/federation.py`, `src/pyrtan/hub.py`. |

## 0. The brief

*"Make Pyrtan federated, like ActivityPub / pubsub."* Read as: a swarm
should not stop at one operator's hub. Two people who each run a hub
should be able to let their swarms answer each other's sentences, without
either handing the other a token to their whole hub, without a central
registry of hubs, and with the same trace at the end.

The ActivityPub reading: a hub is an *actor* with an *inbox*; one hub
*follows* another; the followed hub *announces* to its followers
(pubsub: capabilities are the topic, followers are the subscribers); the
follower routes to what was announced. The parts of ActivityPub this
borrows are the parts that are about who hears from whom. The parts it
does not borrow are JSON-LD, HTTP Signatures with public keys, and
interoperation with the fediverse; see §4.

## 1. What this design refuses

- **Relaying.** A hub announces what its own services hold, never what it
  learned from the hubs it follows, and a request that has already crossed
  a hub is refused rather than forwarded again. Federation is one hop
  (`MAX_HOPS = 1`). There is no loop to detect because there is no path
  long enough to loop.
- **Routing to anyone the operator did not choose.** A hub routes only to
  hubs it follows, and following is an operator's act (`pyrtan follow`,
  `--follow`, or `POST /follow` with the hub's token). Being followed
  grants nothing: a follower receives announcements and may ask, but is
  never routed to.
- **A key infrastructure.** The standard library has HMAC and nothing
  asymmetric. A pair secret, made by the follower and carried in the
  Follow, is enough for two hubs whose operators chose each other. A
  shared federation secret turns an open hub into a closed group.
- **Trusting an inbox a stranger named.** A Follow says where to send
  Accepts and Announces; the followed hub fetches the follower's own
  `/actor` and takes the Follow only if that document names the same
  inbox. A Follow cannot point a hub at someone else's server.

## 2. The protocol

Every hub started with `--federate` (or `--advertise URL`) has an
identity, `id`, which is the URL other hubs reach it at.

| | Method, path | Who | What |
| :-- | :-- | :-- | :-- |
| Actor | `GET /actor` | anyone | `{type: "Hub", id, name, inbox, capabilities, follows: "open" \| "secret"}` |
| Inbox | `POST /inbox` | signed | an activity: `Follow`, `Accept`, `Reject`, `Undo`, `Announce` |
| Follow | `POST /follow {url}` | operator | start following `url` |
| Unfollow | `POST /unfollow {url}` | operator | stop |
| Who | `GET /following`, `/followers`, `/peers` | operator | the relationships, each with `state` and `link` |
| Log | `GET /outbox` | operator | the last fifty activities sent and received, with delivery results |

**Follow.** A (the follower) fetches `B/actor`, makes a 32-byte pair
secret, records B as `pending`, and posts to B's inbox:

```json
{"type": "Follow", "id": "A/activities/…", "actor": "A", "object": "B",
 "secret": "<pair secret>", "inbox": "A/inbox", "name": "alpha-hub"}
```

signed with B's federation secret if A was given one, else with the pair
secret (which proves nothing yet, but keeps every message signed).

**Accept.** B checks its policy: with a federation secret, the Follow
must be signed with it (401 otherwise); without one, open follows are
taken unless `--closed` (403). B fetches `A/actor` and refuses unless
`id` and `inbox` match (400). B records A as a follower with the pair
secret, then posts to A's inbox an `Accept` and an `Announce`, both
signed with the pair secret. A marks B `accepted`. The whole handshake is
synchronous inside A's `POST /follow`, so `pyrtan follow` reports the
outcome.

**Announce** (the pubsub). `{"type": "Announce", "object": {"type":
"Capabilities", "name", "trees": [...]}}`, the same shape as
`/capabilities`, sent to every follower whenever what the hub's own
services hold changes (checked on every poll by a hash of the trees'
names, versions, triggers, inputs, language and hosts). A follower also
polls `B/capabilities` (signed) on its own poll interval, so B's `link`
is `up`, `stale` or `down` by the same clock as a service's.

**Undo.** `{"type": "Undo", "object": {"type": "Follow", …}}` from the
follower ends the relationship on both sides.

**Signatures.** Three headers on every hub-to-hub request:
`X-Pyrtan-Actor` (the sender's id), `X-Pyrtan-Date` (unix seconds, within
five minutes), `X-Pyrtan-Signature` (HMAC-SHA256 over
`METHOD\npath\ndate\nsha256(canonical body)`). The body on the wire *is*
the canonical JSON (sorted keys, no spaces), so the receiver verifies from
what it parsed. A signed request may reach only `/inbox`, `/run`,
`/intent`, `/resume` and `/capabilities`; the operator's paths still need
the bearer token.

**Which secret.** With a mutual follow there are two pairs. A request is
signed with the secret of the relationship in which the requester follows
the target, because asking is what following permits; the target
verifies with its followers' secrets first.

## 3. Routing

`Hub.holders()` is the hub's services followed by the hubs it follows;
`candidates()` reads a sentence against every holder's triggers with the
same rules as before (`intent.py`, with each tree's `[language]`). On a
tie between two holders of the *same* tree name, `_rank` prefers a
service of ours over a hub we follow, then the healthier `link`. A tie
between different tree names is `ambiguous`, as it always was.

Forwarding to a hub we follow goes to its `/run`, signed, with
`hops: [our id]`. The answer comes back with `peer` (the service that
ran it, as the remote hub set it) and `via: [the remote hub]`. The
reading step at the top of the trace is ours; the rest is the remote's.

A hub receiving `/run` with `hops` already set routes only to its own
services; if the tree is held by a hub *it* follows, it answers
`hop-limit` and says so. `pyrtan run --tree NAME` on a hub that only
knows the tree through two hops answers `no-such-tree`, because the hub
never learned of it: nothing is announced that was not held.

## 4. Paths considered and not taken

**ActivityPub proper.** JSON-LD, `@context`, HTTP Signatures with RSA
keys, WebFinger, the Activity Streams vocabulary. Each would be a
dependency (there is no JSON-LD or RSA in the standard library) and none
would buy interoperation that means anything: a Mastodon server cannot
run a tree. The shape is kept because it is the right shape for "who
hears from whom"; the wire format is Pyrtan's own and says so.

**A directory of hubs that routes.** A place hubs register with, which
then answers for all of them. Rejected: it would route to hubs nobody
chose. **A rendezvous hub that holds the directory** (v0.3) was built
and then replaced: it was a server, and the point of a swarm is that
there is none. What stands is §7: a directory every hub keeps and
gossips, a static seed list, and routing that only ever follows an
operator's follow.

**Multi-hop routing.** Announcing transitive capabilities with a hop
count, and forwarding with a TTL. It would let a sentence cross three
hubs, and it would make "why did this go there" a path through other
people's follow lists. One hop keeps every route explainable by one
operator's own choices. `MAX_HOPS` is a constant so the decision is
visible, not so it is cheap to change.

**Sharing gaps and unanswered sentences over federation.** The natural
next topic for the pubsub, and the one that makes the catalog loop close
(platform.md §3, phase 2). Not in v0.3: the shape is settled (another
`Announce` object type), the question is what a hub does with a
stranger's gaps.

## 5. Running it

```
# hub A, reachable at https://a.example (through your TLS proxy)
pyrtan hub --host 0.0.0.0 --token "$A_TOKEN" --advertise https://a.example --state /var/lib/pyrtan/federation.json

# hub B likewise, then on A's operator's shell:
pyrtan follow https://b.example --at https://a.example --token "$A_TOKEN"
pyrtan peers --at https://a.example --token "$A_TOKEN"
pyrtan ask "a sentence only B's trees answer" --at https://a.example --token "$A_TOKEN"
```

To let only hubs you know follow yours, start it with
`--federation-secret` (or `$PYRTAN_FEDERATION_SECRET`) and give the
secret to those operators; their hubs must carry the same secret to
follow. `--closed` takes no follows at all while still following others.
`--state FILE` keeps the relationships across restarts; without it a
restart forgets them and the other side's `link` goes `down` until the
follow is redone.

## 7. The directory, and why nothing runs at pyrtan.com

v0.3 had a *rendezvous hub*: one public hub at `hub.pyrtan.com` that
kept a list of the followers who asked to be listed. It worked and it
was wrong, for the reason §1 gives: it was a server somebody had to run,
and a swarm that needs a server to find itself is not a swarm. v0.4
replaces it with a directory that every federated hub keeps.

**Entries.** A hub started `--listed` (or told `pyrtan follow --listed`,
or `POST /listed`) makes an entry for itself: its id, name, the names of
the trees it holds, and `seen`, the time it made the entry. That is all a
directory knows about a hub.

**Gossip.** Every `Announce` carries the sender's directory: its own
entry if listed, and every live entry it has heard. Announces now go
both ways: down to followers as before, and *up* from a follower to the
hubs it follows (`announce_up`), signed with the pair secret. The
followed hub routes none of what a follower announces, being followed
grants nothing, but it keeps the directory the announce carries. An
inbox answers an Announce with its own gossip, so one round trip
exchanges both ways. Announces go out when the trees or the directory
change and on a heartbeat (`ANNOUNCE_EVERY`, a minute), which is what
keeps `seen` fresh across the swarm.

**Merging.** A newer `seen` replaces an older one; a hub's own entry is
never overwritten by what others say about it; a `seen` from the future
is clamped to now; an entry unheard of for `DIRECTORY_TTL` (a week) is
dropped; the table is bounded (`DIRECTORY_MAX`, the oldest go first).
Each entry remembers `via`, the hub it was last heard from.

**Reading it.** `GET /directory` (public) on any federated hub lists what
that hub knows, each entry with `link` computed from `seen`: `up` within
ten minutes, `stale` within a day, `down` after. `pyrtan directory` asks
your own hub first and falls back to the seeds; the console shows it on
every federated hub; the page at pyrtan.com asks the seeds from the
visitor's browser.

**Seeds.** pyrtan.com publishes `rendezvous/seeds.json`, a static list
of hub URLs maintained by pull request, the role of a torrent's bootstrap
nodes. `pyrtan join` (and `pyrtan follow` with no URL) puts a hub in the
default network (§7a) and follows the first seed whose `/actor` answers;
`pyrtan hub --join default` (or `--follow seed`) does the same on start.
A seed is an ordinary federated hub in the default network, up when its
operator's box is; nothing needs it to be up always (below). `$PYRTAN_SEEDS` points at another list (a URL or a file),
`$PYRTAN_RENDEZVOUS` at one hub.

**The snapshot.** Seeds are home boxes, and home boxes go off. So the
site's build, which GitHub runs on every push and once an hour, asks each
seed's `/directory`, then every other hub it knows of (from the last
snapshot, and from what the hubs it asked named), in parallel rounds up
to a bound, and bakes the merged answer into `rendezvous/directory.json`
with the time, keeping the default network's part of each directory (and
what was heard outside any network), keeping the last snapshot's entries
for hubs that did not answer and dropping anything a week old
(`site/snapshot.py`, standard library, no pyrtan). The build is the
seed's directory work, done by GitHub: the directory stays current while
any listed hub is up, whether or not a seed is. The landing page
shows it when no seed answers the visitor's browser; `pyrtan directory`
falls back to it; `pyrtan follow` with no URL tries its hubs after the
seeds (`$PYRTAN_SNAPSHOT` overrides). A new hub can therefore bootstrap
from any listed hub that is up, and the only way the directory is empty
is if nobody has been up for a week. Nothing of ours polls: GitHub does.

**What an entry is not.** A hub two follows away cannot verify who made
an entry: there are no public keys in the standard library, and HMAC
proves things only between two hubs that share a secret. So an entry is
a *hint*: a name, a URL, a claim about trees. It is verified the one way
that matters, by following the hub, which fetches its `/actor` and runs
the handshake. The directory never routes. Signing entries with a
pure-Python Ed25519 would be three hundred lines of this author's own
cryptography for a property a follow already gives; not taken.

**Reachability.** A hub behind NAT can follow, announce upward, keep the
directory and pass it on, all outbound. It cannot be followed or asked,
because nobody can reach its inbox. To be routable it needs an address:
a port forward behind TLS, or a tunnel (Cloudflare Tunnel, Tailscale
Funnel) that costs nothing and opens no port. The deploy guide says how.

## 7a. Networks

v0.10 lets one hub be in several federations at once and lets any hub
start one. A **network** is a name and a secret. `pyrtan network new
home` makes one on a hub, a network of one, and prints its invite,
`home:<secret>@<the hub's URL>`; `pyrtan join <invite>` on another hub
puts it in the network and follows the hub the invite names in it. A
hub keeps its networks in its state file.

**Follows.** A Follow made in a network carries `network`, an id drawn
from the name and the secret (`network_id`, a hash: it gives neither
away, and two networks that share a name have different ids), and is
signed with the network's secret, not the pair secret. The followed hub
takes it only if it is in a network with that id and the signature holds
under its secret; otherwise `403 not-in-network` or `401 unsigned`. A
Follow without `network` is judged as before (§2): open, the
`--federation-secret`, or refused by `--closed`, which now means "no
follows outside my networks". Both sides remember the network on the
link (`Remote.network`). A pair of hubs has one link, so a follow in a
second network moves the link there.

**Directory.** Each entry remembers the network of the link it arrived
over, and gossip over a link carries only that network's entries (and
the hub's own, if listed: being listed is the hub's choice, everywhere).
So what a hub hears in a private network is passed on only within it.
`GET /directory` without the operator's token shows what was heard
outside any network or in a public one; with it, everything, each row
naming its networks. The `directory` blob a hub's keepers hold is the
public part only. `pyrtan network leave` unfollows the hubs followed in
the network, drops those that followed in it and forgets its entries.

**The default network.** pyrtan.com publishes `rendezvous/network.json`:
the name `pyrtan` and a secret that is public on purpose. A secret on a
public page guards nothing, and is not meant to: it names the network,
so the public directory is one network among others and a private one
never mixes into it. What makes a stranger's hub harmless is what always
did: following is the one act of trust (§1), being followed grants only
the right to ask, and the operator's token guards the rest. A hub whose
operator wants a guarded federation makes a network and shares its
invite privately.

**Not taken.** Keys per network (an Ed25519 key a network signs members
with) would let a network expel a member without changing its secret.
Today expelling means a new network and new invites. That is the price
of HMAC, and small for networks of friends.

## 8. Members

A browser or a phone cannot be followed, because nobody can reach it. It
can follow. v0.5 adds *members*: actors with no inbox, which poll a
mailbox the hub keeps for them, and the blobs hubs and members keep for
each other. Both are in [`members.md`](members.md); the rules above
hold unchanged for them.

## 9. What this design does not claim

It does not claim a follower cannot see what the followed hub holds; that
is the point of following. It does not claim a hub behind a federation
secret is private from its followers, only from strangers. It does not
claim to defend against a followed hub that lies in its announcements
(it can claim to answer "weather" and will be asked); following is
trust, and the trace shows `via` so the lie has a name. It does not
claim to be ActivityPub.
