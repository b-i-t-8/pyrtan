# Design — Members: a browser or a phone as part of the swarm, and what they keep

| | |
|---|---|
| **Status** | **Built, v0.5; members run trees since v0.6.** Members, mailboxes, `Keep`/`Kept`/`Fetch`, the blob store, the console as an installable member; tests with Python members and a browser driven end to end. Since v0.6 a *trusted* member (one that joined with the hub's token) holds the hub's trees and runs them on `Ask`; see §7. |
| **Date** | 2026-10-25 |
| **Decision** | A device that cannot be reached joins a hub by following it and polling a mailbox the hub keeps for it. It carries the directory like a hub, and it keeps the hub's blobs, so that a swarm's state lives on the devices of the people who run it and a dead box loses nothing. Nothing secret is ever a blob. |
| **Where** | `src/pyrtan/federation.py` (members, mailboxes, keeping, hand-out, `Ask`/`Answer`), `src/pyrtan/store.py` (blobs), `src/pyrtan/page.py` (the member in the console, the manifest, the service worker), `src/pyrtan/js/pyrtan.js` (the runtime the member runs trees with), `tests/conformance/` (what both runtimes must say). |

## 0. The brief

*"Browsers and phone apps as part of the cluster, and distributed storage."*
Read as: the things people already carry should be nodes, and the swarm
should not depend on any one box for its memory.

## 1. What a browser can and cannot be

A tab or a phone app can make outbound HTTP, store a few hundred
megabytes locally, run JavaScript and hold a secret. It **cannot
listen**: nobody can reach it. In a browser it can only fetch hosts that
send CORS headers; a phone app wrapping the same page can fetch anything.

So a member is a federation actor without an inbox, and everything in
this design follows from that one fact.

## 2. Members

A member has an id `urn:pyrtan:member:<hex>` and a pair secret it makes
itself. It follows a hub with the ordinary `Follow`, `inbox: "mailbox"`.
The hub accepts under the same policy as any follow (open, or signed
with the federation secret), records the member with `kind: member`,
and from then on everything it would post to the member's inbox goes
into a bounded mailbox instead (`MAILBOX_MAX`, two hundred activities;
older ones fall off). The member polls `GET /mailbox`, signed, which
hands over what is waiting and forgets it; it posts its own activities
to the hub's `/inbox` like any hub. Same signatures, same one-way trust:
a member may ask the hub it follows, carries and gossips the directory,
is listed in it if it wants to be (`kind: member`), and is never routed
to.

The console is the member. Its Member tab makes the identity (kept in
local storage, one per hub origin), follows, and polls every twenty
seconds while the page is open, announcing upward every minute. The
page carries its own SHA-256 and HMAC, because `crypto.subtle` is not
available on a plain `http://` address on a LAN, which is where most
hubs are. Installed to the home screen (the manifest and a service
worker that caches the shell), it is the phone app: it opens offline and
polls when it can. There is no native app and no app store; the design
does not want a build step between a person and their swarm.

## 3. Keeping

A *blob* is a JSON value under a key, at most 256 KB. A hub keeps its
own blobs (`--store DIR`, `own/`) and keeps blobs for others (`kept/`,
at most 200 keys and 64 MB per actor). Three activities:

- **`Keep`** `{key, sha256, size, updated, data}`: "hold this for me."
  A hub keeper stores it and confirms in the same round trip; a member
  stores it in IndexedDB on its next poll and posts **`Kept`** `{key,
  sha256}` to confirm.
- **`Fetch`** `{key}`: "hand it back." A hub keeper answers with the
  blob at once; a member answers on its next poll with a `Kept` carrying
  `data`, which the hub adopts if it has no copy.
- Every `Announce` from a keeper carries `kept`, the keys it holds for
  the receiver. A hub that finds a key it no longer has sends `Fetch`.
  This is how a box that died and came back with the same name gets its
  blobs without anyone doing anything: its members' next announces name
  what they kept.

**Replication.** `--replicas N` (default 2) is how many fresh holders
each of the hub's own blobs should have; a holder is fresh for a day
after confirming the current version. On every poll the hub sends `Keep`
for each blob short of that, to keepers that do not hold it, members
first, someone new before a holder gone stale; a member asked within
ten minutes counts as covered until it confirms, so one blob does not go
to every phone at once. A new version of a blob starts the count over.

**What is kept automatically:** the hub's directory, its collected gaps
and its unanswered sentences, each re-kept when it changes. **What is
never kept:** the state file. It holds the pair secrets, and a blob
goes to other people's devices.

`pyrtan keep KEY FILE` gives a hub a blob (a recording, a list,
anything JSON); `pyrtan kept` shows what it keeps and who holds each;
`pyrtan fetch KEY` gets one back, from the hub or from whoever holds
it. The operator paths are behind the token; the keeper paths
(`/mailbox`, `/kept/<key>`) are signed.

## 4. What this design refuses

- **A member that can be reached.** No WebRTC, no push service, no
  relay that keeps a socket open. A member is reached by being polled,
  and a hub that wants something from a member waits for its next poll.
  That is the honest shape of a device that sleeps.
- **Secrets as blobs.** Nothing that could let a keeper become the hub.
- **Encryption of blobs.** A blob goes to a keeper that follows the hub
  or is followed by it, over TLS, and holds what the hub's own
  `/directory`, `/gaps` and `/unanswered` already show the hub's
  operator. A private blob given to `pyrtan keep` is the operator's
  call; the standard library has no authenticated encryption, and
  HMAC-CTR of my own is not something to hand people's data to. If
  private blobs matter, that is where a dependency would be justified,
  and the design note should say so when it is taken.
- **Any member running trees.** Only a member that joined with the
  hub's token runs them (§7); a keeper announces `trees: []`.
- **A quorum.** One hub writes each of its blobs; the holders are
  copies, not voters. Last version wins, by the hub's own say.

## 5. Failure modes, honestly

- On an open hub anyone who can reach it may join as a keeper and be
  handed its blobs (its directory, gaps and unanswered sentences). A hub
  that wants members from its own people only is started `--closed`
  (`$PYRTAN_CLOSED`): it takes a member only with its token or a key it
  grants `run`, and follows only within its networks.
- A lost phone that joined as a trusted member keeps its own pair
  secret, so a new token does not shut it out. The operator lets it go:
  `pyrtan drop <its id>` (or *drop* in the console's Peers tab), `POST
  /drop` behind the token. The hub forgets it, its mailbox, what it
  handed it and what it keeps for it, and anything it signs afterwards is
  from an actor it does not know. `pyrtan peers` marks members, and the
  trusted ones, to find it by.
- A hub with no members and no followers keeps its blobs for nobody.
  `pyrtan kept` says "held by 0 of 2" so the operator knows.
- A member that never polls holds nothing new; after a day it is not a
  fresh holder and another is asked. Its mailbox fills to two hundred
  activities and drops the oldest, which for `Keep`s means the hub asks
  again later.
- A hub whose state file is lost loses the pair secrets; its members
  cannot sign to it any more. They re-join from the Member tab (one
  tap), and their next announce names what they kept.
- A blob changed on the hub between a member's `Keep` and its `Kept` is
  confirmed at the old version and re-sent; nothing stale is counted.
- A phone's storage can be cleared by the browser under pressure. The
  replica count is what defends against it: two holders, and the hub's
  own copy.

## 6. Sequencing

- **v0.5:** members, mailboxes, keeping, the console as the member and
  the phone app.
- **v0.6, built:** `pyrtan.js`, the tree runtime in JavaScript, with the
  Python fixtures as shared conformance vectors; `Ask`/`Answer` through
  the mailbox so a phone answers sentences for its hub (§7).
- **Later:** encrypted private blobs, when a dependency for it is
  justified; members that keep for each other through a hub.

## 7. Members that run trees (v0.6)

**The runtime.** `src/pyrtan/js/pyrtan.js` is the tree runtime again,
in JavaScript, served by every hub and service at `/pyrtan.js` and
written beside the console by `pyrtan console --out`. It reads the same
tree (the TOML's own table, as JSON), walks the same nodes with the same
templates, conditions, extraction and summaries, and returns the same
outcome and trace. It is not allowed to drift: `tests/conformance/
make_vectors.py` runs every example through the Python runtime against
the recorded responses with a fixed clock and writes `vectors.json`;
`tests/conformance/run.cjs` runs the same trees and inputs through
`pyrtan.js` and compares status, say, reason, output, path and gaps;
`tests/test_conformance.py` runs both and fails when the vectors are
stale. The differences are documented in the file's header. Sentences
are read the same way too: the language rules, the triggers and the
proposal for an unanswered sentence are ported and in the vectors, so a
member reads among the trees it holds before it asks its hub, and a
browser that has joined no hub is a hub of its own
([`platform.md`](platform.md) §2.5).

**Trust.** A member that follows with the hub's bearer token on the
`Follow` is recorded `trusted`; one without is a keeper. Trust is what
decides whether the hub puts its trees, and the sentences they answer,
on a device: a keeper holds copies, a trusted device acts. The console
sends the token it was connected with, so joining from the hub's own
console with the token entered is enough. Since 0.14 a member may
instead follow as a key (`identity.md` §7): one whose root the hub
grants `run` is trusted the same way (`trusted_by: key`), and is dropped
when its note, its root's document or the grant says it may no longer
be. The console's Identity tab holds such a key.

**Hand-out.** On a trusted follow, and on every refresh after, the hub
gives each trusted member every tree it should run as a `Keep` of
`trees/<name>` carrying the raw tree: its own `--trees`, and any
`--handout DIR` (`$PYRTAN_HANDOUT`), which are trees the hub hands out
and does not run itself. The member's `Kept` confirms; a changed tree is
sent again. The member holds them in its store like any blob, so they
survive a reload, and announces their names as its capabilities. Only
trees whose sources are `http` are handed out: a phone has no files,
programs, serial lines or broker sockets of the box's, and no browser of
its own to drive, so those stay on the hub (`io.md`). The hub
takes the names it handed out and ignores the rest, and the triggers it
routes by are its own copy's, never the member's word.

**Routing.** A trusted member with trees is a holder like a service: its
trees show in `/capabilities` with `via: member`, a sentence or a `/run`
for one of them reaches it, and the hub announces them to the hubs that
follow it. Order is unchanged, our own trees, then a service of ours,
then a trusted member, then a hub we follow, so a tree the hub runs
itself stays on the hub and the member is the fallback for it; a
`--handout` tree runs only on members. `Ask` goes into the member's
mailbox with a run id, the tree and the inputs; the hub holds the
request open until the member's `Answer` names that run id (thirty
seconds; a trusted member polls every four seconds), then returns the
outcome with `peer: <member id>` and `via: ["member"]`. An `Answer` for
a run nobody waits for, or from a member other than the one asked, is
refused. A member that does not answer in time gets a `member-timeout`
error, and the `Ask` still in its mailbox is answered to nobody.

**Why a phone would run a tree at all.** It has the network the box
does not: the internet when the NAS is egress-restricted, the LAN the
person is on, the geolocation and the camera of v0.7. And it is the
person's own device, acting by the same inspectable tree, with the same
trace.
