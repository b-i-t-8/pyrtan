# Design — Federation: hubs that follow each other

| | |
|---|---|
| **Status** | **Built, v0.3.** `src/pyrtan/federation.py`, the hub's federation routes, `pyrtan follow` / `unfollow` / `peers`, the console's federation table, and tests of two and three hubs over real sockets. |
| **Date** | 2026-10-25 |
| **Decision** | A hub may follow other hubs, in the shape of ActivityPub (actor, inbox, Follow/Accept/Undo/Announce), and route sentences to the hubs it follows. Following is trust, in one direction, and federation is one hop. |
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

**A directory of hubs.** A place where hubs register so others can find
them. Rejected for now: it is the catalog's job to make trees findable,
and a hub's job to answer for what it holds. An operator who wants to be
followed publishes their hub's URL; the catalog could carry "hubs that
hold this tree" later, as metadata, without the runtime changing.

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

## 6. What this design does not claim

It does not claim a follower cannot see what the followed hub holds; that
is the point of following. It does not claim a hub behind a federation
secret is private from its followers, only from strangers. It does not
claim to defend against a followed hub that lies in its announcements
(it can claim to answer "weather" and will be asked); following is
trust, and the trace shows `via` so the lie has a name. It does not
claim to be ActivityPub.
