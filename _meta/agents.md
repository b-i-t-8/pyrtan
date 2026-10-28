# Design — Agents: open-weights models beside the swarm, so the trees grow

| | |
|---|---|
| **Status** | **Built, v0.15.** Agents in the console (`agents.js`): WebLLM in the browser or a server of the person's own, probing, meanings for gaps, triggers for missed sentences, trees for unanswered ones, chat, memory and skills. `/suggestions` on services and hubs, `pyrtan suggestions`, `pyrtan grow --suggestions`. Tested with a scripted model in node and in Chromium. |
| **Date** | 2026-10-28 |
| **Decision** | A model may sit **beside** the swarm, never inside it. A person starts it in the console; it is a client of the public API like any caller; everything it proposes is data, checked by rules where it is made and again where it is written, and only a person accepts it. The trees stay deterministic; the model helps them grow. |
| **Where** | `src/pyrtan/js/agents.js` (the agent), `src/pyrtan/js/pyrtan.js` (`grow`: the rule drafts and the checks), `src/pyrtan/suggestions.py` (the checks, the store, writing a suggestion into a file), `src/pyrtan/page.py` (the Agents tab), `tests/agents.cjs`, `tests/test_agents.py`, `tests/browser/console_agents.cjs`. |

## 0. The brief, and the reading this design commits to

*"Open source LLM agents that can run in the browser to test for gaps and
make reports/suggestions to the deterministic tree, allowing our trees to
grow organically. Also allow the user to chat with these agents, and for
them to have memory/skills."*

[`design.md`](design.md) §1 already said where a model could go: "A
model may later sit *beside* the swarm, reading gaps and proposing
branches for a person to accept; that is an author's tool, not a
runtime." This is that tool. Every phrase was read against it:

- **"Open source, in the browser."** Open weights under open licences
  (Qwen2.5 and Qwen3 under Apache-2.0, Phi-3.5 under MIT, SmolLM2 under
  Apache-2.0), run by WebLLM on the browser's GPU. A browser without
  WebGPU can use a server of the person's own that speaks the OpenAI chat
  API (Ollama, llama.cpp, vLLM). There is no hosted model, and no key of
  pyrtan.com's anywhere.
- **"Test for gaps."** The agent writes sentences a person might say
  and runs the ones that reach a tree, through `/intent`, like any
  caller. A value no branch covers becomes a gap the way any gap does:
  recorded by the runtime, from a real run, with its trace. The model
  finds where to look. The runtime says what it found.
- **"Reports and suggestions."** Three kinds, and only three (§2). Each
  is checked by rules in the page before the person sees it, kept at the
  hub's `/suggestions` when the person accepts it, and checked again by
  `pyrtan grow --suggestions` before it is written into a file.
- **"Grow organically."** The loop is closed. In a browser that is its
  own hub, an accepted suggestion grows the tree in the page at once. On
  a hub, `pyrtan grow --suggestions HUB --trees DIR --apply` writes it,
  the service reloads the file, and the next run takes the new branch.
- **"Chat, memory, skills."** The person talks to the agent; the agent
  asks the swarm when a tree can answer and quotes it. It remembers what
  happened, what it was told, and what was accepted, dismissed or
  refused. It follows skills: instructions for each of its jobs, built
  in or written by the person, in the shape of a Claude skill.

## 1. What this design refuses

- **A model inside a run.** Nothing in the Python package and nothing in
  `pyrtan.js`'s runtime calls one. An agent reaches the swarm only
  through the endpoints a person's console uses; a tree's answer is the
  tree's, with its trace, and the console says which is which (the
  tree's name beside its words, the agent's beside its own).
- **A suggestion that adds reach.** No suggestion adds an `observe`, an
  `act`, a `delegate`, a host, a path, a command or a device. A branch's
  new node is a `done` or a `fail`. A suggested tree is the rules'
  scaffold, whose host is `example.com`; the hosts the agent guessed are
  a comment. Invariant 3 of `design.md` holds whatever the model says.
- **A suggestion that reads `env`.** What a model writes into a `say`
  may read `inputs`, `state` and `tree`, never `env`, so its words cannot
  be a way to carry a secret out.
- **Trusting the model.** Every check is a rule: the shape
  (`suggestions.problem`, `Pyrtan.grow.problem`, the same messages in
  both), the condition and node id (`grow.draft_branch`'s rules, not the
  model's), a pattern's reading (the same reader the swarm uses), a tree
  that still loads and checks clean. A suggestion that fails a rule is
  refused, and the refusal goes into the agent's memory, so it learns.
- **Applying without a person.** Nothing is written until someone
  accepts it, and a file is written only by `pyrtan grow --suggestions
  --apply`, which puts the file back when the result does not check.
- **Sending the conversation anywhere.** Memory, skills and the
  conversation live in the browser's storage. What leaves it is what the
  agent asks the swarm, what it sends to `/suggestions`, and, with a
  server of the person's own, the prompts it sends that server.

## 2. What an agent may suggest

| Kind | Carries | The rules check | Written by `pyrtan grow --suggestions` as |
| :-- | :-- | :-- | :-- |
| `branch` | `tree`, `node`, `on`, `observed`, `meaning: {kind: done\|fail, say, reason}` | a decide node of that tree, a value no branch covers, a say of at most 300 characters reading only `inputs`, `state`, `tree`, a slug for a reason | the branch `grow` drafts by rules (the hole between ranges, `exists = false`, else `eq`), before the `else`, and a node with the agent's kind and words |
| `trigger` | `tree`, `pattern`, `covers` | the pattern compiles, has literal words, names only the tree's inputs, is new, and reads at least one sentence it covers to this tree that did not read before, without a tie; in the page, also that it takes no sentence the agent has seen read to another tree | `[[triggers]]` after the tree's last one |
| `tree` | `text`, `description`, `hosts_hint` | the sentence, one line, hints as strings | the scaffold `pyrtan new --from` writes, with the description, and the hints as a comment |

Every suggestion also carries `why` (the model's reason, as a comment in
the file), `by` (`agent`, `model`, `engine`) and `evidence` (the
sentences, readings or gap it came from). A service or hub keeps them at
`/suggestions` like gaps, the same suggestion twice as one with a count,
at most 200, `--suggestions FILE` across restarts. `POST /suggestions`
needs what an ask needs (`ask`, or the token): an agent's report is as
much as its asks, which record gaps anyway. `GET /suggestions` is the
operator's.

## 3. The agent

`agents.js` is one file, inlined into the console after `pyrtan.js`.

- **Engines.** `WebLLMEngine` imports WebLLM from jsDelivr (pinned) only
  when a person presses Start, and the model's weights download once
  into the browser's cache. `OpenAIEngine` calls `/chat/completions` on
  a URL the person gives. `ScriptedEngine` is for tests. Small models
  are bad at JSON, so `parseJSON` finds the object in what they say
  (fences, a preamble, a thinking block) and every caller copes with
  none.
- **Jobs.** `probe` (sentences for a tree, read here; the ones that reach
  it run; misses go to `trigger`; gaps go to `meaning`), `meaning`,
  `trigger`, `tree` (for each unanswered sentence: a tree that should
  read it, which goes to `trigger`, or a new tree), and `patrol`, all of
  them over every tree. Limits on sentences and runs per tree keep a
  patrol from hammering the sites the trees read, with a pause between
  runs. Sentences that read to nothing are never sent, so the agent does
  not fill `/unanswered` with sentences nobody asked.
- **Chat.** `/commands` are rules (`/ask`, `/read`, `/probe`, `/gaps`,
  `/unanswered`, `/patrol`, `/remember`, `/memory`, `/forget`,
  `/skills`, `/suggestions`, `/clear`). Anything else goes to the model
  with the trees, what it remembers and the conversation; it may end its
  reply with `ASK:`, `REMEMBER:` or `PROBE:` lines, at most three, which
  the agent carries out; after an `ASK`, it answers again with the
  trees' words in front of it, told to quote them as they are. Lines
  rather than tool calls, because a one-billion-parameter model writes a
  line far more reliably than a call.
- **Memory**, per agent and per hub: notes (the person's, the agent's,
  and the rules' record of what happened: refusals, verdicts and their
  reasons, patrols, sentences that read to the wrong tree), the
  sentences it probed and how each read, its verdicts (so a dismissed
  suggestion is not made again), and the last forty lines of the
  conversation. Recall is by shared words, the person's notes and the
  newest first; it is not a model's choice either.
- **Skills**, shared by every agent in the browser: the five built-in
  ones are the jobs' instructions; a person's own name a job (or
  `always`) and are added to that job's prompt, and a chat skill is
  called as `/name`. A skill file is Markdown with `name`, `description`
  and `when` in its front matter, as a Claude skill is, or JSON; import
  and export are files.

## 4. Paths considered and not taken

**The model writes the branch.** It would be shorter to let the model
write the TOML. Rejected: the condition is the part a rule can get right
every time (the hole between `[1, 3]` and `[45, 48]` is `[4, 44]`), and a
model gets it wrong often enough to matter. The model says what a value
means, which is the part no rule can.

**Tool calls.** The OpenAI tool-call format is what a large model is
trained for. The models that run in a browser are one to four billion
parameters; asked for JSON with a shape and for action lines, they do
well enough, and both degrade into "nothing happened" rather than into a
wrong action.

**A model on the hub.** A hub with a model could patrol on its own. Not
built: it would put a model on the box that runs the trees, and make
someone pay for a GPU to run a swarm. An agent lives where a person is.

**Agents that apply their own suggestions.** The organic version of
growth would let an agent accept itself after enough evidence. Not
built: the trees' value is that a person can read why each branch is
there. What an agent can do is make the person's decision quick.

## 5. Open questions

- **Agents as members.** A trusted member already holds trees and runs
  them; an agent could travel with it and patrol the hub's trees from
  the device, reporting through the mailbox.
- **Federated suggestions.** Gap reports travel the federation
  (`--report-gaps`); suggestions for catalog trees could travel the same
  way, to the tree's author.
- **Synonyms.** A `[language] synonyms` suggestion would read many
  sentences a trigger misses ("forecast" for "weather"). Not built, for
  want of a rule that can tell a synonym from a different question.
