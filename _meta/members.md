# Design — Members: a browser or a phone as part of the swarm, and what they keep

| | |
|---|---|
| **Status** | **Built, v0.5.** Members, mailboxes, `Keep`/`Kept`/`Fetch`, the blob store, the console as an installable member; tests with Python members and a browser driven end to end. Members do not run trees; that is v0.6. |
| **Date** | 2026-10-25 |
| **Decision** | A device that cannot be reached joins a hub by following it and polling a mailbox the hub keeps for it. It carries the directory like a hub, and it keeps the hub's blobs, so that a swarm's state lives on the devices of the people who run it and a dead box loses nothing. Nothing secret is ever a blob. |
| **Where** | `src/pyrtan/federation.py` (members, mailboxes, keeping), `src/pyrtan/store.py` (blobs), `src/pyrtan/page.py` (the member in the console, the manifest, the service worker). |

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
- **Members running trees** in this version. A member announces
  `trees: []`. Routing to a member through its mailbox (`Ask` in,
  `Answer` out, the hub holding the request open) is the next design,
  and needs the JavaScript runtime first.
- **A quorum.** One hub writes each of its blobs; the holders are
  copies, not voters. Last version wins, by the hub's own say.

## 5. Failure modes, honestly

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

- **v0.5, this:** members, mailboxes, keeping, the console as the member
  and the phone app.
- **v0.6:** `pyrtan.js`, the tree runtime in JavaScript, with the Python
  fixtures as shared conformance vectors; then `Ask`/`Answer` through
  the mailbox so a phone answers sentences for its hub.
- **Later:** encrypted private blobs, when a dependency for it is
  justified; members that keep for each other through a hub.
