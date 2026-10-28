# Design — Identity: one key, every hub, any domain

| | |
|---|---|
| **Status** | **Built, v0.14 and v0.17, in four steps** (§10): identifiers, the three documents and `pyrtan id`; names (§6), resolved, pinned and served, with pyrtan.com's by pull request; the wire (§7): signed requests, grants on hubs, members as identities, and the console's device key; and, in v0.17, all of it from the console with no shell, a root's encrypted backup (§5a), and names any network runs in any git repository (§6a). What is left is in §10, *Later*. |
| **Date** | 2026-10-27 |
| **Decision** | A person (or a hub, or a device) is a public key. The identifier is that key, written so that anyone can check a signature from it without asking anybody. Domains, hubs and onion services are where an identity can be *reached* and what it can be *called*, never what it *is*: `pyr:<key>` is the identity, `pyr:<key>@host` says where to find it, and `alice@pyrtan.com` is a name a domain vouches for. pyrtan.com hands out names by pull request, as one name provider among any number. An identity grants nothing by itself: each hub keeps its own list of who may do what. |
| **Where** | Built: `src/pyrtan/identity.py` (the encoding, documents, signing and checking), `src/pyrtan/names.py` (resolving, pins, `/.well-known/pyrtan/`, pyrtan.com's gate), `pyrtan id` in `src/pyrtan/cli.py`, `site/names/`, `site/gate.py`, `tests/test_identity.py`, `tests/test_names.py`. `src/pyrtan/grants.py` (who may do what on a hub), `src/pyrtan/hub.py`, `federation.py` and `service.py` (signed requests, `/allow`, `/identity`, members as identities), `src/pyrtan/js/identity.js` and `page.py` (the console's key), `tests/test_grants.py`, `tests/conformance/identity.json` and `identity.cjs`, `tests/browser/console_identity.cjs`; since v0.17, the console's Identity and Access tabs, `pyrtan id backup|restore|release`, `pyrtan names init|gate`, `pyrtan hub --names`, `tests/test_console_identity.py` and `tests/browser/console_gui.cjs`. |

## 0. The brief

*"Can we create universal users for the whole pyrtan network with just
our GitHub pages? i.e. hash_id:pyrtan.com or other domains, onion routes
etc., basically all key based."*

Read as: someone should be one person across every hub they use, without
an account on any of them and without pyrtan.com running a server; the
part that identifies them should be a key, so that no domain owns them;
and a domain (pyrtan.com, their own, an onion service) should be a place
to find them and a name for them, not the identity itself.

Today there is no such thing. A hub's identity is its URL. A member's is
`urn:pyrtan:member:<hex>`, made fresh by each browser for each hub, with
an HMAC pair secret only that hub knows (`members.md` §2). Being trusted
means having been given the hub's bearer token, which is one secret for
the whole swarm (`design.md` §7). Chat has no names at all (`chat.md`
§4). Each of those was right for what it was built for; none of them
lets a hub say "Alice may ask, and Alice's phone may run trees" without
handing Alice the token.

## 1. What this design refuses

- **An account anywhere.** No hub, and not pyrtan.com, holds a user
  record that the user's identity depends on. A key is made on the
  person's own machine and never leaves it.
- **A domain that owns a person.** `user@server` identifiers (email,
  Matrix, ActivityPub) belong to whoever runs the server; lose the
  server and you are someone else. Here a domain can stop vouching for a
  name, and the identity is untouched.
- **pyrtan.com as the authority.** pyrtan.com is static files (`platform.md`).
  It can publish names; it cannot log anyone in, and the network must
  work for an identity pyrtan.com has never heard of.
- **Identity as a grant.** A valid signature proves who is asking. What
  they may do is the hub's own list (§7), the same way following is the
  one act of trust between hubs (`federation.md` §1). There is no
  network-wide permission.
- **Passwords, recovery codes, and recovery by anyone but the holder.**
  A lost root key is a lost identity (§9). Recovery by a third party is
  a third party who can become you.
- **Making anything that works today need an identity.** HMAC follows,
  networks, the token, anonymous members and anonymous chat all stay as
  they are. Identity is opt-in at each end.
- **This author's own signing code.** A private key is handled only by
  a maintained library or the browser (§8).

## 2. The identifier

An identity is an Ed25519 public key, 32 bytes, written as:

```
pyr:<base32( key || checksum || version )>
checksum = SHA3-256( ".pyrtan id" || key || version )[:2]
version  = 0x01                     (Ed25519)
base32   = RFC 4648, lowercase, no padding
```

That is 35 bytes, 56 characters after the prefix. The layout is Tor's
v3 onion address with Pyrtan's own checksum string and version, so it
is a known shape, and base32 and SHA3-256 are both in the standard
library: encoding and checking an identifier needs no dependency, only
signatures do. A vector, from RFC 8032 §7.1 test 1's public key
`d75a9801…f707511a`:

```
pyr:25njqamcweflpvkl73j4szahhihoc4xt3ktcgjnpaingr5yhkenaooab
```

Rules:

- **Compare the whole string,** after lowercasing. Nothing shorter is an
  identifier. A display may shorten to `pyr:25nj…ooab`; a program never
  matches on that.
- **Refuse on a bad checksum,** a version other than one it knows, or a
  length other than 56. A typo is caught before any network call.
- **The key, not a hash of it.** For Ed25519 the two are the same length,
  and the key lets a verifier check a signature with nothing else in
  hand. Agility comes from the version byte: another algorithm is another
  version, with its own length.
- **It is a DNS label.** 56 lowercase base32 characters fit in 63, so an
  identifier can stand where a host name part does, should that ever be
  wanted. Nothing in this design depends on it.
- **Not an onion address.** The checksum string differs on purpose, so a
  `pyr:` identifier never parses as a `.onion` and back. An identity that
  is reachable over Tor says so as a route (§3), with the onion service's
  own key; one key for two jobs is how a compromise of one becomes both.

An identity is a **root** (a person or a hub), or a **device** (a key a
root has delegated to, §5). Both are written the same way; which one a
key is depends on the documents around it, not on the string.

## 3. Three forms

```
identity = "pyr:" key56
address  = identity "@" locator
locator  = host [ ":" port ]          ; a DNS name, an .onion, or [ip literal]
name     = local "@" domain
local    = [a-z0-9] [a-z0-9._-]{0,62}
```

A string that starts `pyr:` is an identity or an address; anything else
with an `@` is a name. `@` and not `:` or `|`, because `:` already
separates ports and IPv6 groups and starts URLs, and `|` is a pipe in
every shell.

- **`pyr:<key>`** is who. Everything that grants, pins or logs uses this
  form and only this form.
- **`pyr:<key>@hub.example`**, **`pyr:<key>@<56>.onion`** say where. A
  locator is a host that will take messages for the identity: the hub
  that *is* the identity, or a hub that keeps a mailbox for it as a
  member. An identity may have any number; they are hints, carried in its
  document (§4), and a wrong one costs a failed delivery, never a
  forgery, because what comes back is checked against the key. A locator
  is reached at `https://<locator>`; an `.onion` at `http://`, since Tor
  already authenticates and encrypts to the onion's key.
- **`alice@pyrtan.com`**, **`alice@alice.example`**,
  **`alice@<56>.onion`** say what to call someone. A name resolves to an
  identity through the domain (§6), and the first answer is pinned.

## 4. Documents

Three kinds of signed JSON, each one standing alone, so that anyone can
hold a copy and nobody has to trust the holder:

```json
{"pyrtan-identity": 1, "id": "pyr:…", "kind": "person",
 "routes": ["hub.example", "<56>.onion"],
 "devices": [ <device notes, §5> ], "revoked": [ "pyr:…" ],
 "updated": 1761580800, "sig": "…"}
```

**Signing.** `sig` is the root's Ed25519 signature over
`"pyrtan-doc-v1\n"` followed by the document without `sig`, as the
canonical JSON hubs already sign (`federation.py`, `canonical()`: sorted
keys, no spaces, UTF-8). Encoded base64url without padding. Documents
hold strings, integers, lists and objects, never floats, so the Python
and JavaScript runtimes produce the same bytes. The domain string means
a document signature can never be replayed as a request signature (§7)
or the other way round.

**Which copy wins.** The highest `updated` among copies whose signature
holds. A copy from the future (more than the signature window, five
minutes, ahead) is refused rather than clamped: unlike a directory entry,
a document that wins forever would be a way to freeze revocation.

**Where copies live.** Any host may serve
`/.well-known/pyrtan/id/<56>.json`: the identity's own hub, the hubs it is
a member of, pyrtan.com for an identity with a name there (§6). An
identity document is not secret, so it is a fine blob (`members.md` §3):
a hub that has granted an identity keeps its document and hands it to its
keepers like any other.

`kind` is `person` or `hub`. It is what the identity says of itself, for
display; nothing branches on it.

## 5. Devices

The root key should live somewhere quiet: a file on one machine, mode
600, or offline. Each place the person actually uses (a laptop's CLI, a
phone's console) gets its own key, and the root signs a note for it:

```json
{"pyrtan-device": 1, "root": "pyr:…", "device": "pyr:…",
 "label": "phone", "may": ["ask", "chat", "run"],
 "issued": 1761580800, "expires": 1793116800, "sig": "<root's>"}
```

- **`may`** caps what the device can ever be granted; a hub grants the
  intersection of its own list and this (§7). A phone that only chats
  says `["chat"]`, and stealing it gets nobody further.
- **`expires`** is at most a year after `issued`. A note past it is
  refused; the root renews by signing a new one.
- **Revocation** is the device id in the identity document's `revoked`,
  under a newer `updated`. A hub that holds the document re-reads it
  (from the name's domain and the document's routes) at least hourly and
  on any request from a device it has not seen in a day, and takes a
  newer one pushed to it (`pyrtan id push --at HUB`) at once; a revoked
  device's links are dropped and its requests refused.
- **A root may also sign requests itself.** That is the simple case for
  one machine; devices are for everything else.

The console's device key is a non-extractable `CryptoKey` in IndexedDB.
Pairing a phone: on the phone, the console's Identity tab (*Use this
browser as a device*) makes its key and shows its code; on the browser
that holds the root (§5a), *Devices* takes the code, the abilities and a
label, and shows the signed note, which the phone takes pasted. `pyrtan
id device add pyr:… --may chat,run --label phone` does the same from a
shell. An unpaired key signs as itself, so a hub can also grant the key
directly. A QR code of the code and of the note, and picking the note up
from the root's hub instead of pasting it, are later (§10).

## 5a. A root in a browser, and its backup (v0.17)

The root was meant to live somewhere quiet: a file on one machine, or
offline. That asks for a shell, and a shell turns people away. So the
console can hold a root too, and everything `pyrtan id` does is a button.

**Making one.** WebCrypto makes the Ed25519 key *extractable once*: its
seed is read out, sealed into a backup with the person's passphrase, and
the key is imported again non-extractable; the seed is wiped and the
page never holds an extractable key again. The tab says *save your
backup now* until it is downloaded or committed. Restoring is the
reverse: the backup file and its passphrase, opened by WebCrypto, the key
imported non-extractable.

**The backup** is one JSON file, the same from the console
(`identity.js`) and the shell (`pyrtan id backup`), each opening the
other's:

```json
{"pyrtan-key-backup": 1, "id": "pyr:…", "made": 1761580800,
 "kdf": {"name": "PBKDF2-SHA256", "iterations": 600000, "salt": "…"},
 "cipher": {"name": "AES-256-GCM", "iv": "…"}, "data": "…",
 "document": { the identity document, signed, in the clear }}
```

`data` is the 32-byte seed under AES-256-GCM, with
`pyrtan-key-backup-v1\n` and the identifier as associated data, so a
ciphertext cannot be passed off as another identity's. 600,000 rounds of
PBKDF2-SHA256 is what OWASP asks of it; a backup claiming fewer than
100,000 is refused, so a doctored file cannot make a passphrase cheap to
guess. PBKDF2 and not Argon2, because WebCrypto has PBKDF2 and nothing
memory-hard, and the console writes nothing cryptographic of its own.

**Where it may live.** Somewhere only its owner can read: a file on their
disk, or their own *private* repository (the console opens GitHub's page
to make one, and its new-file page to commit the backup; git keeps every
version, which here is a feature). Never anywhere public, and never as a
hub's blob (`members.md` §4: nothing secret is ever a blob): an encrypted
key that anyone can copy can be guessed at offline, forever, and git
history means it is never really gone. The tab says so beside both
buttons.

**What this trades.** A root in a browser is a key that script on the
console's origin could *use*, though never read out: an injected script
on that page could sign as you while the page is open. The console is
static, carries no third-party script and is served by your own hub or by
pyrtan.com's Pages; that is the bargain, and the shell's root, a file
that never meets a browser, remains for whoever wants it quiet.

**Later:** passkeys. WebAuthn's PRF extension can give the console a
secret from a passkey (Face ID, a fingerprint, a security key), synced by
the platform's own keychain, to seal the backup instead of a passphrase.
Support is uneven across browsers today; the passphrase stays, and a
passkey would be a second way to open the same backup.

## 6. Names

`alice@example.org` resolves by fetching

```
https://example.org/.well-known/pyrtan/name/alice.json
```

which is a claim signed by the identity it names:

```json
{"pyrtan-name": 1, "name": "alice", "domain": "example.org",
 "id": "pyr:…", "issued": 1761580800, "sig": "<id's>"}
```

The resolver checks that `name` and `domain` are what it asked for and
that `sig` holds under `id`. The self-signature is what stops a domain,
or someone registering at it, from pointing a name at a key whose holder
never claimed it. The domain's part is serving the file; the key's part
is signing it; both have to agree.

`pyrtan id name alice@example.org` signs a claim with your root key, for
the domain to serve.

**Pinning.** The first identity a name resolves to is remembered (in the
hub's state file, the console's storage, `pins.json` beside the CLI's
key). `pyrtan id resolve alice@example.org` resolves, pins and shows the
identity document if the domain serves one; `--repin` takes a changed
answer; `pyrtan id pins` lists them and `--forget` drops one. A later
answer naming a different identity is not followed: it is shown beside
the pinned one, and the operator re-pins by hand. This is SSH's
known-hosts rule, and it is what makes a domain's word safe to take:
pyrtan.com, or anyone, can remap a name, and every client that saw the
old answer notices.

**pyrtan.com's names.** GitHub Pages serves `/.well-known/` like any other
path and sends `Access-Control-Allow-Origin: *`, so the console can
resolve names from a browser. The names are files in this repository,
`site/names/<login>.json`, each a name claim with two rules only
pyrtan.com adds, and beside it, optionally, `site/names/<login>.id.json`,
the identity's document:

- the name is the author's **GitHub login**, lowercased. One person, one
  name, no disputes to judge, and GitHub has already checked it;
- `"github": <numeric user id>`. GitHub frees a login when its owner
  renames, so the gate (`site/gate.py`, main's copy, run by the
  `catalog-gate` workflow as it is for namespaces) checks the pull request author's *numeric* id
  (`github.event.pull_request.user.id`) against the file, not the login.
  A new name must be the author's own login carrying the author's id; a
  name, or its document, is changed or removed only by the account whose
  id the claim carried on the base branch, and its `github` never
  changes. One name per account. To follow a rename, remove the old name
  and add the new one in the same pull request. A maintainer may merge
  over the gate to remove a name on request or for abuse.

The gate also checks every claim's signature, that it is for
`<its file's name>@pyrtan.com`, and that a document beside it is signed
by the same key. `pyrtan id name alice@pyrtan.com --into <checkout>`
writes both files, looking the numeric id up from the login (or taking
`--github`), and runs the gate. The `pages` build runs `pyrtan id
wellknown site/names --domain pyrtan.com`, which publishes each claim at
`/.well-known/pyrtan/name/<login>.json`, an index of all of them at
`/.well-known/pyrtan/names.json` for mirrors, and each document at
`/.well-known/pyrtan/id/<56>.json`. A change is live within the hour,
the build's cadence. The Pages upload stays on
`actions/upload-pages-artifact@v3`: v4 leaves out dot-directories.

**Other domains** do the same by serving the same paths, and `pyrtan id
wellknown DIR --domain D --out SITE` builds them from a directory of
claims; nothing about them is pyrtan.com's business. An operator can serve names from their own
hub. An onion service can serve names to anyone who fetches over Tor.

## 6a. Any network's names, in any git repository (v0.17)

pyrtan.com is one name provider. A network can be another: its names are
a git repository on any host, served by any static host or by its own
hubs, judged by a rule that needs nothing from the host.

**Names owned by their keys** (`names.gate_key`, `pyrtan names gate`). The
gate compares two trees, the change and its merge base, and reads only
signatures, so it runs in any CI, or on a patch that arrived by mail:

- a new name is open to whoever signs a claim to it, while nobody holds it;
- a name moves to a new key only with a **handover**: the old key signs the
  new claim's body first (`pyrtan id name … --handover-from OLD-KEY`), and
  the new key signs the claim with the handover in it;
- a name is given up only by a **release** its key signs, put in place of
  the claim (`pyrtan id release`); a released name is free, and is not
  served;
- a name's identity document is replaced only by a newer one, and not
  deleted while the name stands with the same key: an older document, or
  none, could bring back a device its owner revoked.

pyrtan.com keeps its own rule (§6): a name is your GitHub login, owned by
your numeric GitHub id.

**A repository** (`pyrtan names init DIR --domain D --host github|gitlab|plain`)
is `pyrtan-names.json` (the domain), a README, `names/`, and for its host:

- *GitHub*: a gate on `pull_request_target`, so the workflow and pyrtan
  come from the default branch and a pinned ref, never from the pull
  request, comparing the pull request with its merge base; and Pages,
  building `/.well-known/pyrtan/` with `pyrtan id wellknown`.
- *GitLab*: a `.gitlab-ci.yml` with the gate (against the merge base) and
  Pages. A merge request's pipeline runs the merge request's own copy of
  the file, so a reviewer reads any change to it; the generated file says
  so.
- *plain*: the files, and the README's commands for whatever runs CI.

The gate installs pyrtan from a ref the repository pins (`--pyrtan-ref`,
`main` by default, with a comment to pin a commit). Nothing in such a
repository is secret: no invite, no token, no backup.

**Served by hubs.** `pyrtan hub --names DIR --names-domain D` answers
`/.well-known/pyrtan/name/<name>.json`, `names.json` and the identity
documents from a checkout of the repository, reread when a file changes,
so a network with no static host needs none: its hubs are its name
providers, reachable at `https://D`.

**From the console**, *Claim a name* takes the network's name and its
repository; for one on GitHub it opens the new-file page filled in, and
for another host it gives the file to add.

## 7. On the wire

**Signed requests.** A request made as an identity carries, beside or
instead of the HMAC headers:

```
X-Pyrtan-Key:            pyr:…               the signing key (a device or a root)
X-Pyrtan-Date:           1761580800          as today
X-Pyrtan-Key-Signature:  <base64url>         Ed25519 over the message below
X-Pyrtan-Device:         <base64url note>    when the key is a device
```

```
message = "pyrtan-req-v1\n" + audience + "\n" + METHOD + "\n" + path + "\n" + date + "\n" + sha256(canonical body)
```

which is the HMAC message (`federation.py`, `sign()`) with a domain
string and the **audience**, the receiving hub's own `id`, in front. A
request signed for one hub cannot be replayed at another; within one hub
the five-minute window is the same as HMAC's. The hub checks the
signature, the device note (signed by its root, unexpired, not revoked),
and then its grants.

The audience is the hub's federation id, or the URL it was started at
(`--advertise`, else its own address), and `GET /api` names it, so a
client asks once and signs for it. The body is the canonical JSON the
request carries; a `POST` with no body is signed as `{}`, which is what
the hub reads it as. A bad signature is refused (401) on any hub. On a
hub with a token, a good one may do what its root is granted and nothing
else (403 otherwise, naming what it may do); on a hub without a token,
where anyone may do anything, a key only says who is asking, which is
what decides whether a member it signs for is trusted.

**Grants.** A hub keeps a list (`grants.json` beside its `--state`, or
`--grants FILE`, or in memory), from `--allow` at start and from its
operator at `/allow`:

```
pyrtan hub --allow alice@pyrtan.com=ask,run --allow pyr:…=operate
pyrtan allow bob@example.org --may chat --at HUB        # GET/POST /allow
pyrtan allowed --at HUB
pyrtan disallow bob@example.org --at HUB                # POST /disallow
```

A name is resolved and pinned when the grant is made; from then on the
grant is held against the identifier, and the name is never resolved
again. `/allow` and `/disallow` need the token or a key granted
`operate`, even on a hub with no token: a grant of `run` makes a trusted
member, which an open hub never had, so there grants come only from
`--allow` at start. What a request may do is the grant's abilities intersected with
the device note's `may` (`grants.py`, `ability_for` says which a path
needs):

| Ability | What it lets the identity do on this hub | What it stands in for today |
| :-- | :-- | :-- |
| `ask` | `/intent`, `/run`, `/resume` on a hub behind a token | the token, for asking |
| `chat` | a seat in a lobby behind a token; its bots answer what `/intent` would, so it is as good as `ask` | the token, for chat |
| `run` | follow as a member and be handed trees (`members.md` §7) | joining with the token |
| `operate` | everything the token allows | the token |

The bearer token keeps working. A hub may have a token, grants, both or
neither; a request with no key headers is judged exactly as it is today.
`POST /identity` takes a granted root's newer document from anyone (it
is signed); `GET /.well-known/pyrtan/id/<56>.json` serves the documents
the hub holds, so a hub is a route for the identities it grants. The
CLI signs with `--sign` (or `$PYRTAN_SIGN`) on every command that calls
a hub, as the root key in `$PYRTAN_IDENTITY`; device keys for the CLI
are later.

**Members.** A member may follow with its device key as its actor id.
The `Follow` is signed with the key as well as carrying the pair secret
as now; once accepted, the link runs on HMAC exactly as today, and the
key is not needed again until the hub re-reads the identity document.
That keeps signatures off the polling path and leaves `members.md` §2
otherwise unchanged. A member whose root holds `run` is trusted
(`trusted_by: key`); one without is a keeper that says who it is. When
its note expires, its root's document revokes it, or its root loses
`run`, the hub drops the link; a member trusted by the token is not
touched.

**Hubs.** A hub may have a root key too: `/actor` gains `key` and a
signature over `{id, key}`. A follower pins the key. A hub that moves
(new box, new domain, now an onion service) and serves the same key at
its new `/actor` is recognised as the same hub, which is the case
`members.md` §3 handles today only by keeping the same name.

## 8. Signatures: where the code comes from

The standard library has no public-key signatures, and `federation.md`
§7 already declined to write Ed25519 by hand for directory entries,
because a follow gave the same property. Here nothing else gives it: a
hub that checks Alice with a shared secret can also forge Alice.

- **Python: `cryptography`,** as an optional extra, `pyrtan[identity]`,
  the way Playwright is `pyrtan[browser]`. Imported lazily by
  `identity.py`. Without it, encoding and checking identifiers still
  works (§2); `pyrtan id new`, signing and verifying say how to install
  the extra; a hub started with any `--allow` refuses to start, loudly,
  rather than hold grants nobody could prove; a hub without it answers a
  key-signed request 401, saying why, and everything else as before. Pyrtan's core stays standard-library
  only, as `CONTRIBUTING.md` requires; identity is opt-in at each hub,
  and so is its dependency.
- **The browser: WebCrypto's `Ed25519`,** shipped in all three engines,
  available only in secure contexts. The console at
  `https://pyrtan.com/console/`, on any hub behind TLS, and on
  `localhost` has it; a console served over plain `http://` to a LAN
  address does not, and falls back to the token and today's anonymous
  member. That is the same limit that made the console carry its own
  SHA-256 and HMAC (`members.md` §2), and it is said in the Identity tab
  rather than worked around. SHA3-256, for the identifier's checksum, is
  not in WebCrypto at all, so `identity.js` carries it, beside the
  SHA-256 the console already had.
- **The fallback, if the dependency proves a problem:** a verify-only
  Ed25519 in pure Python, from RFC 8032's reference code. Verifying
  handles no secret, so timing does not leak anything; the risk is
  accepting a forgery through a missed check, which RFC 8032's and
  Wycheproof's vectors are for. Signing would still never be pure Python.
  Not taken now; written down so that the choice is a choice.

## 9. Paths considered and not taken

- **`user@server` as the identity** (email, Matrix, ActivityPub's
  actors). The server owns the user. Kept as the *name* form (§6), where
  pinning makes it safe.
- **A hash of the key** (`hash_id`). Same length for Ed25519, and a
  verifier then needs the key carried separately. The version byte gives
  the agility a hash would have.
- **`did:key`.** A standard, and a `pyr:` identifier converts to one and
  back mechanically. But it is base58btc (another codec, mixed case, not
  a DNS label) and arrives with the DID resolution machinery. A
  conversion can be offered later; it is not the format.
- **Nostr's `npub`.** Close in spirit; bech32 is a codec the standard
  library does not have, and base32 plus SHA3 is.
- **The onion encoding exactly.** Would make an identity and an onion
  address the same string, and invite one key for both (§2).
- **GitHub as the identity** (OAuth, or `github.com/<login>.keys` as the
  registry). OAuth needs a server for the exchange, or makes every hub
  ask GitHub who someone is; `.keys` makes GitHub the authority and has
  no place for a phone's key. GitHub is used once, by the gate, to say
  who opened a pull request, which is all pyrtan.com's names need.
- **A global namespace** (a chain, a DHT of names). Names are per domain
  on purpose: there is nothing global to squat on or to fight over.
- **`<id>.pyrtan.com`.** GitHub Pages serves no wildcard subdomains, and
  the path form does the same job.

## 10. Sequencing

- **First, built: the identifier and the documents.** `identity.py`:
  encode, decode, checksum, parse the three forms, canonical document
  signing and checking, reissuing a document strictly newer; `pyrtan id
  new`, `show`, `check`, `device add`, `revoke`, `route add|remove`, and
  `name` (signs a claim, ready for the second step); the root key in
  `$PYRTAN_IDENTITY`, else `~/.config/pyrtan/identity/root.key`, mode
  600, beside `identity.json`. Tested with RFC 8032's vectors and the
  vector in §2; CI installs the extra so signing is tested, and the
  suite still passes without it.
- **Second, built: names.** `names.py`: resolving, pins, building
  `/.well-known/pyrtan/` from a directory of claims, and pyrtan.com's
  gate; `pyrtan id resolve`, `pins`, `wellknown`, `name --into`;
  `site/names/` with its README, `site/gate.py --author-id` and
  `--root`, the `catalog-gate` workflow running main's gate on the pull
  request's tree with the author's numeric id, and the `pages`
  build serving the names. pyrtan.com hands out names; nothing yet
  grants by them.
- **Third, built: hubs.** `identity.py` signs and verifies requests;
  `grants.py` keeps the grants, their documents and their refresh; the
  hub checks keys against its audience, enforces the abilities behind a
  token, takes `/allow`, `/disallow` and `/identity`, serves
  `/.well-known/pyrtan/id/`, trusts members by key and drops them when
  that lapses; `pyrtan hub --allow/--grants`, `pyrtan allow`, `allowed`,
  `disallow`, `id push`, and `--sign`. The console: `identity.js`
  (SHA3-256, identifiers, canonical JSON, request signing with WebCrypto,
  the key in IndexedDB), inlined; the Identity tab; `api()` signing; a
  member joining as the key. `tests/conformance/identity.json` holds
  what Python says, and `identity.cjs` checks that `identity.js` says the
  same, signatures included; `tests/browser/console_identity.cjs` drives
  the console in Chromium end to end (a key, a note, a reload, an answer
  with no token, a trusted member). This is the step that replaces
  handing out the token.
- **Fourth, built (v0.17): no shell, backups, any network's names.**
  `identity.py` seals and opens backups, and signs releases and
  handovers; `names.py` judges names owned by their keys, writes a names
  repository for GitHub, GitLab or anything, and a hub serves one;
  `identity.js` signs every document a root signs and writes and opens the
  same backups, agreeing with Python byte for byte; the console's Identity
  tab makes, backs up and restores a root, claims names and pairs devices,
  and its Access tab keeps a hub's grants. `tests/browser/console_gui.cjs`
  drives all of it in three Chromium contexts, checked by Python's code.
- **Later:** passkeys to open a backup (§5a); a QR code for pairing, and the note picked up from the
  root's hub; device keys for the CLI; hub keys in `/actor` and moves; a chat seat that opts in to
  showing a verified name (chat stays anonymous by default, and still
  keeps nothing); reaching `.onion` locators, which needs a SOCKS proxy
  the transport does not have and an egress rule for it (`io.md`);
  signing directory entries, now that keys exist, if a reason appears.

## 11. Failure modes, honestly

- **A lost root key is a lost identity.** Device notes expire within a
  year and nothing can renew them. The person makes a new root, re-pairs
  their devices, and asks each name provider and each hub operator to
  point at it. The CLI says this when it makes a root, and names the
  file to back up.
- **A stolen device** works until it is revoked and the hubs re-read the
  document: up to an hour, plus however long the new document takes to
  reach a route the hub asks. `may` bounds what it can do meanwhile.
- **A stolen root** is the identity, stolen. The thief can revoke the
  real devices and sign a new document. Nothing in this design takes the
  identity back; the person starts again and tells people, out of band.
- **A name provider remaps a name.** Pinned clients refuse to follow and
  show both identities. A client that never resolved the name before
  gets the new answer; that is what a name is.
- **A GitHub login changes hands.** The gate holds the numeric id, so
  the new holder of the login cannot edit the old claim. The old claim
  stays until its owner or a maintainer removes it.
- **pyrtan.com is down or gone.** Identifiers, documents held by hubs,
  and every pin keep working. Only resolving a name for the first time
  needs the domain.
- **Clocks.** The same five-minute window as HMAC; a device whose clock
  is off is refused with `signature too old`, as a hub is today.
- **A console on plain `http://`** cannot hold a key (§8) and joins as an
  anonymous member.
- **A forgotten passphrase** leaves the backup a locked box: nothing in
  this design opens it. The identity lives on in any browser or machine
  that still holds the key; from there, a new backup with a new
  passphrase (`pyrtan id backup`, or a restore and a download).
- **A browser's data cleared before its backup was saved** loses a root
  made there. The tab says *save your backup now* until it is saved, and
  that is all it can do.
- **Script injected into the console's page** could sign as the root held
  there while the page is open (§5a); it cannot carry the key away.
- **A network's repository on GitLab** runs a merge request's own CI
  file; a reviewer who merges a change to it unread lets the change judge
  itself (§6a).

## 12. What this design does not claim

It does not claim anonymity: an identity is a stable pseudonym, linkable
across every hub it signs to, which is the point. A person who wants two
unlinkable selves makes two roots. It does not claim a name is a real
person, or a key a trustworthy one; it claims only that the same key
signed both times. It does not claim recovery. It does not claim that
an identity can do anything on a hub whose operator did not grant it, or
that granting it is safe; a grant is trust, as a follow is, and the trace
and the logs will name whom it was given to.
