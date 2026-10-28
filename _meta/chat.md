# Design — Chat: strangers and bots, Omegle-style

| | |
|---|---|
| **Status** | **Built, v0.11; the typing indicator since v0.12.** The lobby, bots made of trees, strangers across a follow, the console's Chat tab and `pyrtan chat`; tests with two hubs over real sockets and the console driven in a browser. "Stranger is typing…", on by default, off per person or per hub (§4). |
| **Date** | 2026-10-27 |
| **Decision** | A hub started `--chat` keeps a lobby where anyone who can reach it is paired at random with another person, on this hub or on a hub it follows, or with a bot. A bot is a holder of trees, and what it says is what `/intent` would have said: the chat adds no model and no new kind of knowledge. Nothing said between people is kept; a line said to a bot is a sentence at `/intent`, and one no tree answers is recorded as unanswered, as it would be there (only callers with the token or a key granted `chat` meet bots). |
| **Where** | `src/pyrtan/chat.py` (the lobby), `src/pyrtan/hub.py` (`/chat`, `intent(only=…)`), `src/pyrtan/federation.py` (`Meet`, `Say`, `Part`), `src/pyrtan/page.py` (the Chat tab), `src/pyrtan/cli.py` (`pyrtan chat`, `hub --chat`), `examples/small-talk/`. |

## 0. The brief

*"A chat/peer section where you can talk Omegle-style with bots or random
users on the network."* Read as: press a button, be paired with someone
you do not know, talk, press it again for someone else. "Bots" are the
swarm's own: Pyrtan has no other kind. "The network" is the hub you are
on and the hubs it follows.

## 1. The lobby

A *seat* is a random id the client holds; holding it is being that seat.
A seat says whom it wants to meet (`anyone`, `people` or a `bot`) and up
to five interests, and waits. Pairing is at random among the seats
waiting here that want people, the ones sharing the most interests first;
the system line says what you share ("You both like chess."). A seat that
wants `anyone` and meets nobody in eight seconds meets a bot. A seat that
wants `people` never does.

Everything is a JSON request: `POST /chat/start`, `/chat/poll`,
`/chat/say`, `/chat/next`, `/chat/leave`, and `GET /chat` for the counts.
`poll` is a long poll (up to 25 s) on a condition the lobby notifies on
every event, and it wakes on its own when the seat is due to ask another
hub or meet a bot, so nothing runs in the background. A seat's own lines
come back to it as events too, so the log a client shows is the hub's, in
the hub's order. A seat nobody polls for a minute is let go and its
partner told, which is how a closed tab ends a conversation. One poll
waits per seat: a newer one answers the older at once, so a seat holds
one thread however many polls it sends. A caller without the token or a
key granted `chat` holds at most eight seats from one address, and
strangers together leave a hundred of the 500 seats for those who have
them. Lines lose their control characters, here and from other hubs, and
`pyrtan chat` strips them again from whatever a hub sends.

The lobby follows the hub's access rule: with a token, only callers with
the token chat, or with a key the hub grants `chat` (`identity.md` §7).
That is deliberate. A bot runs trees, so a lobby open to callers the hub
would refuse at `/intent` would be a way round the token; for the same
reason a `chat` grant reaches the bots too, and is to be given to whom
the operator would let ask.

**A public lobby (v0.13).** A hub that wants strangers from the
internet in its lobby, and its token on everything else, starts
`--chat-public` (`$PYRTAN_CHAT_PUBLIC`). `/chat` then answers without
the token, and a caller without it (or without a key granted `chat`)
is a *guest*: a guest meets people,
on this hub or on a hub it follows, and never a bot. `want: bot` from a
guest is refused (`bots-need-token`), `anyone` is read as `people`, and
`/chat` counts no bots for them. `/api` says `chat_public` and, for the
caller asking, `chat_bots`; the console offers a guest "people only" and
says why. A request signed with a key the hub grants nothing is a
guest too, not refused: a browser that holds a key is not shut out of a
lobby open to everyone. The rest of the hub (`/intent`, `/run`, `/gaps`, `/follow`,
`/keep`) still needs the token, so opening the lobby lends strangers
nothing but each other. A hub with no token has no guests: there,
anyone may meet a bot, as anyone may ask it at `/intent`.

**No centre.** There is no chat server, and pyrtan.com runs no hub. Every hub started
`--chat` is a lobby, and a seat that finds nobody at home asks the hubs
its hub follows (§3). A hub that joins the default network (`pyrtan
join`) follows a hub in it, and each follow is a way for the people
waiting on two hubs to meet; the network has as many lobbies as it has
hubs that chat. A
person runs one, or opens the Chat tab of any hub that is public.

## 2. Bots

A bot is one *holder* of trees, picked at random from everyone the hub
routes to and is up: its own trees, a service, a trusted member, a hub it
follows. A holder whose trees share a word (name, description, tags,
triggers) with the seat's interests is picked first, and the
introduction suggests a sentence from those trees. A bot's line is
produced by rules, in this order:

1. If the bot's last line was an `ask` node's question, the line is the
   answer: the hub resumes the snapshot with it. An `ask` is a
   conversation already; chat is where it reads like one.
2. Otherwise the line is read against the bot's own triggers only
   (`Hub.intent(only=holder)`). A match runs there, exactly as `/intent`
   would.
3. A line that reads as nothing (the fillers rule dropped all of it:
   "hi", "hello pyrtan", "thanks") gets the bot's introduction, and the
   swarm is asked nothing. Greetings are fillers to every tree in the
   swarm; making them triggers would change how "hi, what's the weather"
   reads everywhere.
4. Otherwise it is asked of the whole swarm, as `/intent`. When nobody
   answers, the bot says so with the tree that would, and the sentence is
   recorded as unanswered like any other. A chat is a stream of real
   sentences; that is where the next trees come from.

Every bot line carries the outcome's status, tree, peer, output and trace,
and the console shows the trace under the line. `examples/small-talk`
answers what is left once greetings are fillers: "how are you", "who are
you", "are you a bot", "what can you do", "bye", with one `decide` on the
topic its trigger fixes.

**A fix this needed.** A trigger may fix inputs (`inputs = { topic =
"how" }`), but `/capabilities` carried only the patterns, so a hub
routing a sentence to another holder dropped them. Capabilities now carry
`trigger_inputs`, and the hub reads triggers with them.

## 3. Strangers across hubs

A seat that finds nobody here asks up to three of the hubs this one
follows, every five seconds while it waits, with a signed `Meet {ref,
interests}`. The followed hub pairs it with one of its own waiting seats
or answers `met: false`. From then on each side's lines go as `Say {to,
from, text}` and the end as `Part {to, from}`, delivered to the other
hub's inbox and signed with the pair's secret, both ways. `ref` is a
second random id per seat, so another hub never learns the id that would
let it act as the seat, and it changes on `next`, so the last stranger
cannot reach the next conversation.

The trust rules are federation's. A hub answers a `Meet` only from a hub
that follows it: following is the operator's choice, and a hub that is
followed serves its followers. A lobby behind the hub's token (not
public) answers a `Meet` only from a follower its operator follows back:
anyone may follow an open hub, and following is not an invitation into a
private lobby. A `Say` or `Part` is taken only for a conversation that
hub is in, and a stranger's lines from another hub have the same burst
limit as a person's here (five at once, then one a second). The more
seats wait, the less often each asks other hubs, so a busy lobby does
not turn into a stream of `Meet`s. A hub pairs a `Meet` only with its own seats
and never passes one on. Members do not meet across hubs: a member is a
browser that chats through its hub's lobby like anyone.

While a seat is asking other hubs it is *busy*, and nobody can pair it
meanwhile; two seats that ask each other's hubs at once both get
`met: false` and try again on their next round.

**Chat links (v0.17).** A hub follows few hubs, because following routes
sentences to their trees; so the strangers a lobby could reach were
few. A *chat link* is the relationship chat needs and nothing more. A
hub started `--chat-reach N` (`$PYRTAN_CHAT_REACH`) keeps up to N of
them: on every poll it tries a hub or two it heard of on the LAN
(`--lan`) or in its directory (public networks only), that it neither
follows nor is followed by, with a `ChatLink` carrying a fresh pair
secret and signed with it, as an open `Follow` is. The other hub checks
the signature and that the inbox is the one the sender's `/actor`
names, and takes the link only when its operator also gave
`--chat-reach` and its lobby is open to strangers anyway (`--chat-public`,
or no token); a closed federation (`--closed`, a federation secret)
takes none, and a hub takes at most twice its own reach. Then either
end sends `Meet`, `Say`, `Typing` and `Part` over it, exactly as over a
follow, and the two lobbies share their strangers.

A link is kept apart from following on purpose: it lives in the lobby,
not in `following` or `followers`, so nothing is routed over it, no
trees or directory are announced on it, and no blob is kept; the inbox
looks a link up only for the four chat activities. It lives in memory
and is made again after a restart. A hub that refused is not asked again
for a day (`GET /peers` shows the links and each refusal's reason), and
a link whose hub fails three `Meet`s in a row is dropped and replaced.
With `--lan`, two open lobbies in one house link with each other and
nothing else needed.

## 4. Typing (v0.12)

While a person types, the client says so (`POST /chat/typing {seat,
typing: true}`), again every two seconds while they go on, and `false`
when the box empties, loses focus or the line is sent. `pyrtan chat`
does the same at a terminal (v0.29): it reads keys one by one
(`pyrtan.term`, cbreak mode through `termios`, the terminal restored on
the way out), sends typing from a thread of its own so no key waits on
the network, and draws lines that arrive above the one being typed. Up
and Down go through the lines sent (v0.30): the last hundred, kept in
the process for this one chat and never written down, as the hub keeps
no transcript either; a recalled line shows as typing like any other. From
a pipe, or where there is no `termios`, it reads whole lines and shows
no typing. The partner's poll answers with `partner_typing: true` and
the console shows
"Stranger is typing…". While a bot's trees run, its partner reads
`partner_typing: true` too, and the console says "Bot is thinking…".

It is a state, not an event: nothing about it enters the seat's events,
so a client that was away sees the conversation and not a history of
keystrokes. It goes out by itself five seconds after the last word of
it (a closed tab, a lost `false`), and at once with the next line. A
poll that sends `partner_typing` (what the client is showing) is woken
when that is no longer true, both when the partner starts and when
the indicator goes out; a poll that does not send it is never woken by
typing, so a client that does not show the indicator cannot be made to
spin by someone else's keystrokes.

**Enabling and disabling.** Two switches, both on by default:

- *Per person.* `typing_indicator: false` on `/chat/start`, `/chat/next`
  or `/chat/settings` (the console's "Typing indicator" box, remembered
  in that browser; `pyrtan chat --no-typing`). It works both ways, like
  read receipts: a seat with it off shares nothing and sees nothing. A
  person who would rather not be watched typing should not get to watch.
  Turned off mid-word, what the partner was shown goes out at once.
- *Per hub.* `pyrtan hub --chat --no-chat-typing`
  (`$PYRTAN_CHAT_TYPING=0`): nobody's typing is shared, `/chat` says
  `typing: false`, and the console hides the box.

Across hubs it is a `Typing {to, from, typing}` activity, sent on a
change and at most every two seconds while typing holds. The sender's hub
sends it only for a seat with the indicator on and the receiver's shows
it only to a seat with it on, so the rule holds across hubs without
either hub knowing the other seat's setting. The outbox and inbox logs
leave `Typing` out: it would push everything else off them. A hub from
before v0.12 refuses the activity; the first refusal stops the sending
for that conversation.

## 5. What this design refuses

- **A model behind the bot.** A bot that "chats" by generating text
  would be the one place in Pyrtan whose output cannot be explained.
  The bot says what a tree says, or that no tree says anything.
- **Keeping anything.** Conversations live in memory while someone is in
  them. No names, no accounts, no history, no transcripts. The hub's
  outbox and inbox logs show that a `Say` went, with its length, never
  its text. A restart drops the lobby.
- **Pretending to moderate.** Omegle closed in 2023 over abuse it could
  not moderate at scale; this lobby is not built to do better. What it
  does: text only, no links rendered, opt-in per hub (`--chat`), behind
  the hub's token when it has one unless the operator opens it
  (`--chat-public`, people only), a line at most 1,000 characters, five
  lines in a burst and one a second after, at most 500 seats, and either
  side can leave at any moment. An operator who opens a lobby to the
  internet should know that is all.
- **A socket.** Long polls, as members poll a mailbox; no WebSocket, no
  WebRTC, nothing the standard library's server cannot hold.
- **A second hop.** A `Meet` reaches the hubs this one follows and stops.

## 6. Failure modes, honestly

- A hub that dies mid-conversation sends no `Part`. The stranger on the
  other hub sees nothing until they say something (the `Say` fails, and
  the conversation ends with a line saying so) or press New.
- A followed hub that is slow to answer a `Meet` slows the poll that asked
  it, by up to its delivery timeout, three hubs at most.
- A bot whose holder is slow answers late; lines to it are answered in
  order, one at a time, and a reply for a conversation that has moved on
  is dropped.
- Two hubs on different versions: one without a lobby answers a `Meet`
  `no-chat`, one from before v0.11 refuses the activity type; either is
  `met: false` to the asker.

## 7. Sequencing

- **v0.11, built:** the lobby, bots, strangers across a follow, the Chat
  tab, `pyrtan chat`, `small-talk`, `trigger_inputs`.
- **v0.12, built:** the typing indicator, per person and per hub (§4).
- **v0.13, built:** public lobbies: `--chat-public`, guests meet people
  only (§1).
- **v0.17, built:** chat links, `--chat-reach N`: open lobbies share
  their strangers without following each other (§3).
- **v0.29, built:** `pyrtan chat` at a terminal shows your typing (§4).
- **v0.30, built:** Up and Down at the terminal bring back the lines
  sent (§4).
- **Later:** a member's lobby that works offline between two phones of
  the same hub.
