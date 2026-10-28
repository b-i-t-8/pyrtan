"""The console: one HTML file a service or hub serves at ``/``, and the same
file hosted anywhere else (``pyrtan console --out``), where it is a hub of
its own until it joins one.

It asks a sentence and shows the answer, the output and the trace; lists
what the swarm can answer; shows the hub's peers and their ``link``; lists
the gaps; and, on a hub started with ``--chat``, pairs this browser with a
random stranger or a bot (``/chat``, see :mod:`pyrtan.chat`). Everything it does goes through the public endpoints
(``/api``, ``/health``, ``/capabilities``, ``/peers``, ``/gaps``,
``/intent``, ``/resume``), so it works the same against a hub and a
service, and the same whether it was served by that hub or by a static
host somewhere else.

Served by a hub or service, it talks to the origin it came from. Served
from elsewhere (it finds out by asking ``api`` on its own origin), the
browser is the hub: ``pyrtan.js``'s ``LocalHub`` answers the same
endpoints inside the page, with trees it needs no network for: every
console carries the default trees (:mod:`pyrtan.defaults`; or what
``pyrtan console --trees`` gave it), embedded in the shape of a catalog's
``trees.json``. A catalog named in the address (``?catalog=``, on the
page's own origin only) or one beside the page (``../catalog/``) is
fresher and taken first; only a page
that carries no trees asks pyrtan.com's, then the copy this browser kept.
It reads sentences and runs trees here, keeping gaps and unanswered
sentences in the browser's local storage, so they outlive the page, under
the device that met them: this browser's key when it has one (its
Identity tab), else an id of its own. Making or forgetting a key moves
them to the device it now is; the Gaps tab forgets them, and shares them
with the same identity's other devices, browser to browser with no hub
(``sync.js``, inlined: a WebRTC link opened by a code or QR carried by
hand, or a signed file; see _meta/identity.md §5b). Trees that need a box (files,
programs, devices, brokers, a browser to drive) or a site that will not
let a page read it say so. Joining a hub is explicit: its URL and token in
the connect box, kept in the browser's local storage, the token sent as a
bearer; the hub must allow the console's origin with ``--cors``.
Forgetting the hub makes the browser its own hub again. No external
assets, no build step.

Its Identity tab does all of ``pyrtan id`` without a shell
(``identity.js``, inlined: WebCrypto's Ed25519, PBKDF2 and AES-GCM). It
makes your identity in this browser, hands you its backup at once
(encrypted with your passphrase, to download or keep in your own private
repository; the page then holds the key non-extractable), restores one,
claims a name (pyrtan.com's, through GitHub's own new-file page, which
opens the pull request from your account; or any network's names
repository), and adds and revokes devices: by code and note, or paired by
a QR code over a link between the two browsers, the root signing only
once its person confirms the six digits both screens show (``sync.js``). A browser can instead be a
device of an identity held elsewhere: it makes its own key and takes the
note its root signs. On a hub of 0.14 or later the key signs what the
console sends, and a member joined from here follows as it; the Access
tab keeps the hub's grants. A page served over plain ``http://`` to
anything but localhost has no WebCrypto, holds no key, and says so.

Its Agents tab starts an agent beside the swarm (``agents.js``, inlined;
see _meta/agents.md): an open-weights model in this browser (WebLLM on
its GPU, or wllama on its CPU where there is no WebGPU; either imported
only when a person presses Start) or on a server of theirs. It
calls the same endpoints this page does, its suggestions are checked by
rules before they are shown, and accepting one grows the tree here when
the browser is its own hub, and keeps it at the hub's ``/suggestions``.
"""

from __future__ import annotations

import functools
import json
import urllib.parse
from pathlib import Path
from typing import Any

from . import __version__
from .defaults import bundle

# The tree runtime in JavaScript, served beside the console so a member can run trees.
PYRTAN_JS = (Path(__file__).parent / "js" / "pyrtan.js").read_text(encoding="utf-8")
# The wordmark's colour picker, inlined into the console and copied beside pyrtan.com's pages.
THEME_JS = (Path(__file__).parent / "js" / "theme.js").read_text(encoding="utf-8")
# Identifiers, canonical JSON and request signing, inlined into the console (_meta/identity.md).
IDENTITY_JS = (Path(__file__).parent / "js" / "identity.js").read_text(encoding="utf-8")
# A person's devices sharing their gaps, browser to browser, with no hub; and QR codes (sync.js).
SYNC_JS = (Path(__file__).parent / "js" / "sync.js").read_text(encoding="utf-8")
# Agents beside the swarm: open-weights models a person starts in the console (_meta/agents.md).
AGENTS_JS = (Path(__file__).parent / "js" / "agents.js").read_text(encoding="utf-8")

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="object-src 'none'; base-uri 'none'">
<title>Pyrtan</title>
<meta name="description" content="Pyrtan console: ask the swarm, read the trace, see what it can answer, find the gaps, and join a hub as a member that keeps its blobs.">
<link rel="manifest" href="manifest.webmanifest">
<script src="pyrtan.js"></script>
<meta name="theme-color" content="#c2410c">
<script>__THEME__</script>
<script>__IDENTITY__</script>
<script>__SYNC__</script>
<script>__AGENTS__</script>
<style>
  :root { --bg:#fbfaf7; --fg:#1d1d1b; --mute:#6b6a64; --line:#e3e1da; --card:#ffffff; --ok:#1f7a4d; --bad:#a3382b; --ask:#8a6400; --pyr:#c2410c; --pyr-fg:#fff; --tan:#a8804f; --tan-fg:#fff; --soft:#f1efe9 }
  @media (prefers-color-scheme: dark) { :root:not([data-theme=light]) { --bg:#141513; --fg:#ecebe6; --mute:#9d9c95; --line:#2b2c29; --card:#1c1d1b; --ok:#5fcf93; --bad:#f08a7a; --ask:#e2b84a; --pyr:#fb8a4c; --pyr-fg:#101318; --tan:#d2b48c; --tan-fg:#101318; --soft:#222420 } }
  :root[data-theme=dark] { --bg:#141513; --fg:#ecebe6; --mute:#9d9c95; --line:#2b2c29; --card:#1c1d1b; --ok:#5fcf93; --bad:#f08a7a; --ask:#e2b84a; --pyr:#fb8a4c; --pyr-fg:#101318; --tan:#d2b48c; --tan-fg:#101318; --soft:#222420 }
  :root { --accent:var(--pyr); --accent-fg:var(--pyr-fg) }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }
  main { max-width:920px; margin:0 auto; padding:24px 16px 64px }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px; margin-bottom:12px }
  h1 { font-size:22px; margin:0 } h1 small { color:var(--mute); font-weight:normal; font-size:14px; margin-left:8px }
  h2 { font-size:17px; margin:24px 0 8px }
  nav { display:flex; gap:4px; flex-wrap:wrap; margin:12px 0 16px }
  nav a { padding:6px 12px; border-radius:999px; color:var(--fg); text-decoration:none; border:1px solid var(--line); background:var(--card); font-size:14px }
  nav a:hover { border-color:var(--tan) }
  nav a[aria-current=page] { background:var(--accent); color:var(--accent-fg); border-color:var(--accent) }
  section[hidden] { display:none }
  p.lede { color:var(--mute); margin:0 0 16px }
  form.row { display:flex; gap:8px; margin:0 0 12px; flex-wrap:wrap }
  div.row { display:flex; gap:8px; flex-wrap:wrap }
  form.row input[type=text], form.row input[type=url], form.row input[type=password], div.row input[type=text] { flex:1 1 180px; min-width:0; font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  [hidden] { display:none !important }  /* even where an inline style sets display */
  #tab-identity input[type=text], #tab-identity input[type=password], #tab-identity input[type=file], #tab-access input[type=text] { flex:1 1 180px; min-width:0; font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  button { font:inherit; padding:10px 16px; border:0; border-radius:8px; background:var(--accent); color:var(--accent-fg); cursor:pointer }
  button.quiet { background:var(--soft); color:var(--fg) }
  button:disabled { opacity:.6; cursor:wait }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:0 0 14px }
  .status { font-weight:600 } .done{color:var(--ok)} .failed,.error{color:var(--bad)} .asked{color:var(--ask)}
  .say { font-size:18px; margin:6px 0 }
  pre { margin:8px 0 0; padding:10px 12px; background:var(--bg); border:1px solid var(--line); border-radius:8px; overflow:auto; font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap }
  .suggest { position:relative; flex:1 1 180px; min-width:0 }
  form.row .suggest input[type=text] { position:relative; width:100%; background:transparent }
  .suggest .ghost { position:absolute; inset:0; padding:10px 12px; border:1px solid transparent; border-radius:8px; background:var(--card); white-space:pre; overflow:hidden; pointer-events:none }
  .suggest .ghost .typed { visibility:hidden } .suggest .ghost .rest { color:var(--mute) }
  .suggestions { position:absolute; z-index:10; left:0; right:0; top:calc(100% + 4px); list-style:none; margin:0; padding:4px 0; background:var(--card); border:1px solid var(--line); border-radius:8px; box-shadow:0 6px 18px rgba(0,0,0,.12); max-height:320px; overflow-y:auto }
  .suggestions li { padding:7px 12px; cursor:pointer; display:flex; gap:8px; align-items:baseline }
  .suggestions li[aria-selected=true], .suggestions li:hover { background:var(--soft) }
  .suggestions li b { font-weight:600 } .suggestions li .text { flex:1; min-width:0; overflow-wrap:anywhere }
  .suggestions li .from { color:var(--mute); font-size:13px }
  details summary { cursor:pointer; color:var(--mute) }
  ul.tries { list-style:none; padding:0; margin:0; display:flex; flex-wrap:wrap; gap:8px }
  ul.tries li a { display:inline-block; padding:6px 10px; border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--fg); text-decoration:none; font-size:14px }
  table { width:100%; border-collapse:collapse; font-size:14px } td,th { text-align:left; padding:6px 8px; border-top:1px solid var(--line); vertical-align:top } th { color:var(--mute); font-weight:600; border-top:0 }
  code { font:13px ui-monospace,SFMono-Regular,Menlo,monospace }
  .mute { color:var(--mute) } .gap { color:var(--ask) }
  .link { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:middle; background:var(--mute) }
  .link.up { background:var(--ok) } .link.stale { background:var(--ask) } .link.down { background:var(--bad) }
  .notice { border-left:3px solid var(--ask); padding:8px 12px; background:var(--soft); border-radius:6px; margin:0 0 14px; font-size:14px }
  .notice.bad { border-color:var(--bad) }
  .sync-code { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-start; margin:10px 0 }
  .sync-code .qr { width:min(260px, 100%); flex:0 0 auto; background:#fff; border-radius:8px; line-height:0 }
  .sync-code .qr svg { width:100%; height:auto }
  .sync-code textarea, #sync-step textarea { flex:1 1 220px; min-width:0; font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); overflow-wrap:anywhere }
  #sync-step video { width:min(320px, 100%); border-radius:8px; margin-top:10px }
  footer { color:var(--mute); font-size:13px; margin-top:32px }
  footer a, .card a { color:var(--accent) }
  .pyr { color:var(--pyr) } .tan { color:var(--tan) }
  :focus-visible { outline:2px solid var(--tan); outline-offset:2px }
  ::selection { background:var(--tan); color:var(--tan-fg) }
  .chat-log { height:min(60vh,480px); overflow-y:auto; padding:2px 2px 8px; overflow-wrap:anywhere }
  .chat-log .line { margin:6px 0 }
  .chat-log .sys { color:var(--mute); font-size:14px }
  .chat-log .who { font-weight:600; margin-right:6px }
  .chat-log .you .who { color:var(--accent) } .chat-log .stranger .who { color:var(--bad) } .chat-log .bot .who { color:var(--ok) } .chat-log .tree .who { color:var(--tan) }
  .chat-log details { font-size:13px; margin-top:2px }
  .typing-line { min-height:20px; color:var(--mute); font-size:14px; font-style:italic }
  #chat-say { flex-wrap:nowrap } #chat-say input { flex-basis:0 }
  select { font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  .build-q { margin:0 0 12px } .build-q label { display:block; font-size:14px; font-weight:600; margin-bottom:2px }
  .build-q .help { color:var(--mute); font-size:13px; margin-bottom:4px }
  .build-q input[type=text], .build-q select { width:100%; font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  .build-q.bad input, .build-q.bad select { border-color:var(--bad) } .build-q .bad-say { color:var(--bad); font-size:13px; margin-top:2px }
  ul.problems { margin:8px 0 0; padding-left:20px; font-size:14px } ul.problems .error { color:var(--bad) } ul.problems .warn { color:var(--ask) } ul.problems .info { color:var(--mute) }
  .build-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px }
  @media (max-width:560px) { table thead { display:none } table td { display:block; border-top:0 } table tr { display:block; border-top:1px solid var(--line); padding:6px 0 } }
</style>
</head>
<body>
<main>
  <header>
    <h1><span class="pyr" data-theme-part="pyr">pyr</span><span class="tan" data-theme-part="tan">tan</span> <small id="who">…</small></h1>
    <span class="mute" id="where"></span>
  </header>
  <p class="lede">A core set of expandable services that act on all forms of I/O through inspectable decision trees. Ask a sentence a tree answers to; the answer comes back with the exact path that produced it. If nothing answers, you get the tree that would. No model is involved in an answer; an agent you start sits beside the trees, never inside them.</p>

  <section id="connect" hidden>
    <div class="card">
      <div style="margin-bottom:8px" id="connect-lede">This console is not being served by a Pyrtan. Point it at one:</div>
      <div class="mute" style="font-size:14px;margin:-4px 0 8px" id="connect-local" hidden></div>
      <form class="row" id="connect-form">
        <input type="url" id="hub-url" placeholder="https://hub.example.net or http://192.168.1.20:8700" autocomplete="off">
        <input type="password" id="hub-token" placeholder="token, if the hub needs one" autocomplete="off">
        <button type="submit" id="connect-go">Connect</button>
        <button type="button" class="quiet" id="forget">Forget</button>
      </form>
      <div class="mute" style="font-size:14px" id="connect-help">Kept in this browser only. The hub must allow this page's origin: <code>pyrtan hub --cors <span id="origin"></span></code>. A hub on your LAN over <code>http://</code> may be refused by the browser when this page is <code>https://</code>; open the hub's own <code>/</code> instead, which is this same console.</div>
    </div>
  </section>

  <div id="problem"></div>

  <nav id="tabs">
    <a href="#ask" data-tab="ask">Ask</a>
    <a href="#trees" data-tab="trees">Trees</a>
    <a href="#peers" data-tab="peers" id="peers-tab" hidden>Peers</a>
    <a href="#watches" data-tab="watches" id="watches-tab">Watches</a>
    <a href="#gaps" data-tab="gaps">Gaps</a>
    <a href="#unanswered" data-tab="unanswered">Unanswered</a>
    <a href="#agents" data-tab="agents">Agents</a>
    <a href="#directory" data-tab="directory" id="directory-tab" hidden>Directory</a>
    <a href="#member" data-tab="member" id="member-tab" hidden>Member</a>
    <a href="#identity" data-tab="identity" id="identity-tab" hidden>Identity</a>
    <a href="#access" data-tab="access" id="access-tab" hidden>Access</a>
    <a href="#chat" data-tab="chat" id="chat-tab" hidden>Chat</a>
    <a href="#api" data-tab="api">API</a>
  </nav>

  <section id="tab-ask">
    <form class="row" id="ask">
      <div class="suggest">
        <div class="ghost" id="ghost" aria-hidden="true"><span class="typed"></span><span class="rest"></span></div>
        <input type="text" id="text" placeholder="what's the weather in Oslo" autofocus autocomplete="off" spellcheck="false" role="combobox" aria-autocomplete="both" aria-expanded="false" aria-controls="suggestions">
        <ul class="suggestions" id="suggestions" role="listbox" aria-label="Suggestions" hidden></ul>
      </div>
      <button type="submit" id="go">Ask</button>
    </form>
    <div class="card" style="margin-bottom:24px">
      <div class="mute" style="margin-bottom:8px">Try one</div>
      <ul class="tries" id="tries"></ul>
    </div>
    <div id="out"></div>
  </section>

  <section id="tab-trees" hidden>
    <h2>What this swarm can answer</h2>
    <div class="card"><table id="caps"><thead><tr><th>Tree</th><th>Answers to</th><th>From</th></tr></thead><tbody></tbody></table></div>
    <p class="mute" style="font-size:14px" id="trees-more">Need a tree that is not here? Browse the <a href="https://pyrtan.com/catalog/" id="catalog-link">catalog</a>, then <code>pyrtan pull NAME</code> into a folder a service serves.</p>
  </section>

  <section id="tab-peers" hidden>
    <h2>Peers</h2>
    <div class="card"><table id="peers"><thead><tr><th>Link</th><th>Peer</th><th>Trees</th><th>Last</th></tr></thead><tbody></tbody></table></div>
    <p class="mute" style="font-size:14px"><span class="link up"></span>up: answered within 30 s · <span class="link stale"></span>stale: within 90 s · <span class="link down"></span>down: longer, or never.</p>
    <div id="federation" hidden>
      <h2>Federation</h2>
      <p class="mute" style="font-size:14px;margin-top:0">This hub is <code id="fed-id"></code>. It routes to the hubs it follows; hubs that follow it receive its announcements and may ask it.</p>
      <div class="card"><table id="hubs"><thead><tr><th>Link</th><th>Hub</th><th>Relation</th><th>Trees</th></tr></thead><tbody></tbody></table></div>
    </div>
  </section>

  <section id="tab-watches" hidden>
    <h2>Watches</h2>
    <p class="mute" style="font-size:14px;margin-top:0">Trees that run on their own: a clock, a file, a topic or a serial line starts them (<code>[[watches]]</code> in the tree, <code>--watch</code> on the service). Each row is what the watch last did; a path that changed between two runs of the same watch is how you learn a site changed before anyone asked.</p>
    <div class="card"><table id="watches"><thead><tr><th>Watch</th><th>On</th><th>Fired</th><th>Last run</th></tr></thead><tbody></tbody></table></div>
  </section>

  <section id="tab-gaps" hidden>
    <h2>Gaps</h2>
    <p class="mute" style="font-size:14px;margin-top:0">A gap is a decision that met a value no branch covers. Each one is the next branch to write, in the tree and node named.</p>
    <p class="mute" style="font-size:14px;margin-top:0" id="gaps-local" hidden>Kept in this browser for this device, <code id="gaps-device"></code>, across reloads; making or forgetting a key in the Identity tab takes them along. <button type="button" class="quiet" id="gaps-forget">Forget them</button></p>
    <div class="card"><table id="gaps"><thead><tr><th>Tree · node</th><th>Saw</th><th>Times</th><th>Where</th></tr></thead><tbody></tbody></table></div>
    <div class="card" id="sync" hidden>
      <h3 style="margin:0 0 6px">Your other devices</h3>
      <div class="mute" style="font-size:14px" id="sync-lede"></div>
      <div class="row" style="margin-top:10px" id="sync-actions" hidden>
        <button type="button" id="sync-offer">Link a device</button>
        <button type="button" class="quiet" id="sync-have">I have a code</button>
        <button type="button" class="quiet" id="sync-scan" hidden>Scan a code</button>
        <button type="button" class="quiet" id="sync-export">Download for another device</button>
        <button type="button" class="quiet" id="sync-import">Load a file from one</button>
        <input type="file" id="sync-file" accept="application/json,.json" hidden>
      </div>
      <div id="sync-step"></div>
      <div style="font-size:14px;margin-top:8px" id="sync-state"></div>
    </div>
  </section>

  <section id="tab-unanswered" hidden>
    <h2>Waiting to be written</h2>
    <p class="mute" style="font-size:14px;margin-top:0" id="unanswered-lede">Sentences nobody answered, each with the tree that would. <code>pyrtan unanswered --at <span class="at">URL</span> --scaffold DIR</code> writes the files.</p>
    <div class="card"><table id="unanswered"><thead><tr><th>Nobody answered</th><th>Would be</th><th>To write it</th></tr></thead><tbody></tbody></table></div>
    <div class="card" id="builder" hidden>
      <h2 style="margin-top:0" id="builder-title">Build</h2>
      <p class="mute" style="font-size:14px;margin-top:0">A tree, one question at a time: what it reads, what it pulls out, what it decides on and which branches, what it says. Every answer has a default drawn from the sentence by rules, and the file below is checked as you go. Nothing here is a model; the questions are the same ones <code>pyrtan build --from "…"</code> asks on a box.</p>
      <form id="builder-form"></form>
      <div id="builder-problems"></div>
      <pre id="builder-toml"></pre>
      <div class="build-actions"><button type="button" id="builder-download">Download the file</button><button type="button" class="quiet" id="builder-copy">Copy</button><button type="button" class="quiet" id="builder-close">Close</button></div>
      <div class="mute" style="font-size:14px;margin-top:8px">Put the file in a folder a service serves (<code>pyrtan serve DIR</code>, or a hub's <code>--trees DIR</code>): the service loads it the moment it lands, and the sentence is answered from then on. <code>pyrtan check DIR</code> and <code>pyrtan run "…" --trees DIR</code> show it working first.</div>
    </div>
  </section>

  <section id="tab-agents" hidden>
    <h2>Agents beside the swarm</h2>
    <p class="mute" style="font-size:14px;margin-top:0">An agent is an open-weights model you start here: in this browser, on its GPU, or on a server of your own (Ollama, llama.cpp). It tests the trees with sentences of its own, says what a gap's value means, finds the patterns a tree missed and the trees nobody has written, and talks with you. It never answers for a tree and never sits inside a run: what it suggests is checked by rules here, checked again by <code>pyrtan grow --suggestions</code> before it is written, and only you accept it. It remembers what it learns and follows its skills, in this browser only. Nothing is downloaded until you start one.</p>
    <div class="card" id="agent-setup">
      <form class="row" id="agent-start" style="margin:0 0 8px">
        <input type="text" id="agent-name" placeholder="a name, e.g. scout" autocomplete="off" style="flex:0 1 140px">
        <select id="agent-engine" aria-label="where the model runs"><option value="webllm">in this browser, on its GPU (WebGPU)</option><option value="wllama">in this browser, on its CPU (any browser, slower)</option><option value="openai">on a server of mine (OpenAI API)</option></select>
        <select id="agent-model" aria-label="model"></select>
        <input type="url" id="agent-url" placeholder="http://localhost:11434/v1" autocomplete="off" hidden>
        <input type="text" id="agent-server-model" placeholder="model, e.g. qwen2.5:3b" autocomplete="off" hidden>
        <button type="submit" id="agent-go">Start</button>
      </form>
      <div class="mute" style="font-size:14px" id="agent-help">In this browser: the weights download once (from Hugging Face, through WebLLM on the GPU or wllama on the CPU) and stay in the browser's cache. On the CPU a reply takes seconds to a minute; pick a small model. On a server of yours, it must let this page's origin call it (Ollama: <code>OLLAMA_ORIGINS=<span class="origin-here"></span></code>).</div>
      <div class="notice" id="agent-gpu" hidden style="margin:8px 0 0">This browser has no WebGPU, so a model cannot run on its GPU. Run it on the CPU instead (it works in any browser, Firefox included, only slower), point it at a server of yours, or open this page in a browser with WebGPU: Chrome or Edge 113 or later (Windows, macOS, ChromeOS, Android), Safari 26 or later, or Firefox 141 or later on Windows or 147 or later on a Mac with Apple silicon. Firefox on Linux and Android has WebGPU only in Nightly so far.</div>
      <div class="mute" style="font-size:14px;margin-top:6px" id="agent-progress"></div>
    </div>
    <div id="agent-on" hidden>
      <div class="card">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between">
          <div><span class="status done" id="agent-who"></span> <span class="mute" id="agent-what"></span></div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button type="button" id="agent-patrol" title="probe every tree, then the gaps, then the unanswered">Patrol</button>
            <button type="button" class="quiet" id="agent-stop" disabled>Stop</button>
            <button type="button" class="quiet" id="agent-off">Put away</button>
          </div>
        </div>
        <div class="chat-log" id="agent-log" aria-live="polite" style="height:min(50vh,420px);margin-top:8px"></div>
        <div class="typing-line" id="agent-typing" aria-live="polite"></div>
        <form class="row" id="agent-say" style="margin:8px 0 0;flex-wrap:nowrap">
          <input type="text" id="agent-text" placeholder="talk to the agent, or /help" autocomplete="off" maxlength="1000" style="flex-basis:0">
          <button type="submit">Send</button>
        </form>
      </div>
      <h2>Suggestions</h2>
      <p class="mute" style="font-size:14px;margin-top:0" id="agent-sug-lede">Each passed the rules in this page. Accept one and it is kept at the hub's <code>/suggestions</code>, for <code>pyrtan grow --suggestions <span class="at">URL</span> --trees DIR --apply</code>. Dismiss one and the agent remembers why.</p>
      <div id="agent-sugs"><p class="mute">Nothing yet. Press Patrol, or ask it to <code>/probe</code> a tree.</p></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px"><button type="button" class="quiet" id="agent-download">Download accepted</button></div>
      <details class="card" id="agent-memory"><summary>Memory</summary>
        <p class="mute" style="font-size:14px">What this agent keeps, for this hub, in this browser: your notes, its own, and what happened (refusals, your verdicts, patrols). The most relevant go into every prompt.</p>
        <form class="row" id="agent-note-form"><input type="text" id="agent-note" placeholder="a note for the agent, e.g. Oslo's weather API is in Celsius" autocomplete="off"><button type="submit">Remember</button></form>
        <table id="agent-notes"><tbody></tbody></table>
        <div style="margin-top:8px"><button type="button" class="quiet" id="agent-forget-all">Forget everything</button></div>
      </details>
      <details class="card" id="agent-skills-box"><summary>Skills</summary>
        <p class="mute" style="font-size:14px">Instructions for a job, the way a skill is for Claude: the built-in ones are the jobs, yours are added to the job you name (or to all of them). A chat skill of yours can be called as <code>/name</code>. Kept in this browser, shared by every agent; a skill file is Markdown with <code>name</code>, <code>description</code> and <code>when</code> in its front matter, or JSON.</p>
        <table id="agent-skills"><thead><tr><th>Skill</th><th>For</th><th></th></tr></thead><tbody></tbody></table>
        <form id="agent-skill-form" style="margin-top:10px">
          <div class="row"><input type="text" id="skill-name" placeholder="name, e.g. tourist" autocomplete="off"><select id="skill-when" aria-label="for which job"><option value="chat">chat</option><option value="probe">probe</option><option value="meaning">meaning</option><option value="trigger">trigger</option><option value="tree">tree</option><option value="always">always</option></select><input type="text" id="skill-desc" placeholder="one line: what it is for" autocomplete="off"></div>
          <textarea id="skill-body" rows="4" placeholder="the instructions, e.g. Write probe sentences the way a tourist would: short, a little misspelled, with place names in their own language." style="width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)"></textarea>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"><button type="submit">Add skill</button><label class="quiet" style="padding:10px 16px;border-radius:8px;background:var(--soft);cursor:pointer">Import…<input type="file" id="skill-import" accept=".md,.json,.txt" hidden></label><button type="button" class="quiet" id="skill-export">Export mine</button></div>
        </form>
      </details>
    </div>
  </section>

  <section id="tab-directory" hidden>
    <h2>Directory</h2>
    <p class="mute" style="font-size:14px;margin-top:0">Every hub that asked to be found, as this hub has heard it through the swarm. Entries are hints carried hub to hub; <em>link</em> is how recently each announced. Nothing here is routed to: to use one, follow it from your own hub, <code>pyrtan follow URL --at YOUR-HUB</code>. To be in it: <code>pyrtan hub --listed</code>.</p>
    <div class="card"><table id="directory"><thead><tr><th>Heard</th><th>Hub</th><th>Holds</th><th>Last announced</th></tr></thead><tbody></tbody></table></div>
  </section>

  <section id="tab-member" hidden>
    <h2>This browser as a member</h2>
    <p class="mute" style="font-size:14px;margin-top:0">A browser or a phone cannot be reached, so it joins a hub the other way round: it follows the hub, polls a mailbox the hub keeps for it, carries the directory, and keeps copies of the hub's blobs (its directory, gaps, unanswered sentences, anything given to <code>pyrtan keep</code>). When the hub's box dies, what it kept is still here. Install this page to the home screen and it is the phone app.</p>
    <div class="card">
      <div id="member-off">
        <form class="row" id="join-form">
          <input type="text" id="member-name" placeholder="a name for this device, e.g. barry's phone" autocomplete="off">
          <input type="password" id="member-fedsecret" placeholder="federation secret, if the hub takes follows only with one" autocomplete="off">
          <button type="submit">Join this hub</button>
        </form>
        <div class="mute" style="font-size:14px">Makes an identity for this browser (kept in local storage), follows the hub with it, and starts polling. Nothing leaves this device but signed activities to the hub. Joined with the hub's token (entered above), this device is <em>trusted</em>: the hub hands it its trees and routes sentences to it, and the trees run here.</div>
      </div>
      <div id="member-on" hidden>
        <div><span class="status done">member</span> <code id="member-id"></code> <span class="mute" id="member-state"></span></div>
        <div class="mute" style="font-size:14px;margin:6px 0">Name <code id="member-name-shown"></code> · <span id="member-trust"></span> · mailbox polled <span id="member-polled">never</span> · <label><input type="checkbox" id="member-listed"> listed in the directory</label></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
          <button type="button" class="quiet" id="member-poll">Poll now</button>
          <button type="button" class="quiet" id="member-leave">Leave</button>
        </div>
        <h2 style="font-size:15px;margin:14px 0 6px">Kept here for this hub</h2>
        <table id="kept"><thead><tr><th>Key</th><th>Size</th><th>Updated</th><th></th></tr></thead><tbody></tbody></table>
        <h2 style="font-size:15px;margin:14px 0 6px">Trees this device runs</h2>
        <table id="member-trees"><thead><tr><th>Tree</th><th>Answers to</th><th>Reaches</th></tr></thead><tbody></tbody></table>
        <form class="row" id="member-run" style="margin-top:8px">
          <select id="member-run-tree" style="font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)"></select>
          <input type="text" id="member-run-inputs" placeholder='inputs as JSON, e.g. {"place": "Oslo"}' autocomplete="off">
          <button type="submit">Run here</button>
        </form>
        <div id="member-out"></div>
        <h2 style="font-size:15px;margin:14px 0 6px">Activity</h2>
        <pre id="member-log" style="max-height:220px"></pre>
      </div>
    </div>
  </section>

  <section id="tab-identity" hidden>
    <h2>Your identity</h2>
    <p class="mute" style="font-size:14px;margin-top:0">An identity is a key, made here and held by this browser, which will not hand it over, not even to this page. No account anywhere: hubs grant your key what they choose, a name like <code>you@pyrtan.com</code> points at it, and your other devices speak for it with a note it signs. Lose the key and its backup and nobody can give it back.</p>
    <div id="ident-none" class="notice" hidden>This page cannot hold a key: it needs a secure page (https, or localhost) in a browser with Ed25519 in WebCrypto. It still asks, chats and joins, without a key: with the hub's token, or as an anonymous member.</div>

    <div class="card" id="ident-start" hidden>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" id="ident-new">Make my identity</button>
        <button type="button" class="quiet" id="ident-restore-open">Restore from a backup</button>
        <button type="button" class="quiet" id="ident-make">Use this browser as a device of an identity I have</button>
        <button type="button" class="quiet" id="pair-have">I have a pairing code</button>
      </div>
      <form id="ident-new-form" style="margin-top:12px" hidden>
        <div class="mute" style="font-size:14px;margin-bottom:6px">Your key is made here, then sealed with a passphrase into a backup you keep. Choose a long sentence you will remember: nobody can reset it, and the backup is only as strong as it is.</div>
        <div class="row"><input type="password" id="ident-pass" placeholder="passphrase (a long sentence)" autocomplete="new-password"><input type="password" id="ident-pass2" placeholder="the same again" autocomplete="new-password"><button type="submit">Make it</button></div>
      </form>
      <form id="ident-restore-form" style="margin-top:12px" hidden>
        <div class="mute" style="font-size:14px;margin-bottom:6px">The backup file this console or <code>pyrtan id backup</code> wrote, and its passphrase.</div>
        <div class="row"><input type="file" id="ident-file" accept="application/json,.json"><input type="password" id="ident-restore-pass" placeholder="passphrase" autocomplete="current-password"><button type="submit">Restore</button></div>
      </form>
    </div>

    <div class="card" id="ident-root" hidden>
      <div><span class="status done">identity</span> <code id="root-id"></code></div>
      <div id="root-backup-state" style="margin:8px 0"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button type="button" id="root-download">Download my backup</button>
        <input type="text" id="root-gh" placeholder="your GitHub login" autocomplete="off" style="max-width:220px">
        <button type="button" class="quiet" id="root-gh-go">Keep it in a private GitHub repository</button>
      </div>
      <div id="root-gh-out" class="mute" style="font-size:14px;margin-top:8px"></div>
    </div>

    <div class="card" id="ident-claim" hidden>
      <h2 style="font-size:15px;margin:0 0 8px">Claim a name</h2>
      <form id="claim-form">
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;font-size:14px">
          <label><input type="radio" name="claim-where" value="pyrtan" checked> at pyrtan.com (your GitHub login)</label>
          <label><input type="radio" name="claim-where" value="network"> at another network</label>
        </div>
        <div class="row" id="claim-pyrtan"><input type="text" id="claim-login" placeholder="your GitHub login" autocomplete="off"><button type="submit">Make my claim</button></div>
        <div id="claim-network" hidden>
          <div class="row"><input type="text" id="claim-name" placeholder="name@network-domain, e.g. alice@names.example.org" autocomplete="off"></div>
          <div class="row" style="margin-top:6px"><input type="text" id="claim-repo" placeholder="its names repository, e.g. https://github.com/example/names" autocomplete="off"><input type="text" id="claim-branch" value="main" style="max-width:120px" autocomplete="off"><button type="submit">Make my claim</button></div>
        </div>
      </form>
      <div id="claim-out"></div>
    </div>

    <div class="card" id="ident-devices" hidden>
      <h2 style="font-size:15px;margin:0 0 8px">Devices that speak for you</h2>
      <table id="dev-table"><thead><tr><th>Device</th><th>May</th><th>Until</th><th></th></tr></thead><tbody></tbody></table>
      <form id="dev-form" style="margin-top:10px">
        <div class="mute" style="font-size:14px;margin-bottom:6px">On the other device, open this console's Identity tab, choose <em>Use this browser as a device</em>, and paste its code here.</div>
        <div class="row"><input type="text" id="dev-code" placeholder="its code, pyr:…" autocomplete="off"><input type="text" id="dev-label" placeholder="what to call it: phone" style="max-width:200px" autocomplete="off"></div>
        <div style="display:flex;gap:14px;flex-wrap:wrap;margin:8px 0;font-size:14px;align-items:center" id="dev-may"><span class="mute">it may</span>
          <label><input type="checkbox" value="ask" checked> ask</label><label><input type="checkbox" value="chat" checked> chat</label><label><input type="checkbox" value="run"> run trees</label><label><input type="checkbox" value="operate"> operate hubs</label>
          <button type="submit">Sign its note</button></div>
      </form>
      <div id="dev-out"></div>
      <div style="margin-top:10px"><button type="button" id="pair-offer">Pair a device with a QR code</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
        <button type="button" class="quiet" id="doc-push" hidden>Give this hub my newest document</button>
        <button type="button" class="quiet" id="doc-download">Download my identity document</button>
      </div>
    </div>

    <div class="card" id="ident-on" hidden>
      <div><span class="status done">device</span> <code id="ident-id"></code> <button type="button" class="quiet" id="ident-copy">Copy its code</button></div>
      <div class="mute" style="font-size:14px;margin:6px 0" id="ident-paired"></div>
      <form id="ident-note-form" style="margin:10px 0">
        <div class="mute" style="font-size:14px;margin-bottom:6px">Give this code to the browser that holds your identity (its Identity tab, <em>Devices</em>), or run <code id="ident-howto"></code>; then paste the note it gives back here.</div>
        <div class="row"><input type="text" id="ident-note" placeholder='{"pyrtan-device": 1, "root": "pyr:…", ...}' autocomplete="off"><button type="submit">Use this note</button></div>
      </form>
      <div class="mute" style="font-size:14px">Or, quicker: <button type="button" class="quiet" id="pair-have-device">I have a pairing code</button> (the QR the browser that holds your identity shows under <em>Pair a device</em>).</div>
    </div>

    <div id="pair"><div id="pair-step"></div><div id="pair-step-scan"></div><div style="font-size:14px;margin:8px 0" id="pair-state"></div></div>

    <div id="ident-common" style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:8px" hidden>
      <label><input type="checkbox" id="ident-sign"> sign requests to the hub with this key</label>
      <button type="button" class="quiet" id="ident-forget">Forget it on this browser</button>
    </div>
  </section>

  <section id="tab-access" hidden>
    <h2>Who may do what here</h2>
    <p class="mute" style="font-size:14px;margin-top:0">The identities this hub grants anything, instead of handing out its token: <em>ask</em> (the trees), <em>chat</em> (the lobby, bots included), <em>run</em> (be a trusted member, running its trees) and <em>operate</em> (everything the token allows). A device gets what its identity is granted, capped by its note. A name is looked up once and pinned.</p>
    <div id="access-note" class="notice" hidden></div>
    <div class="card">
      <table id="grant-table"><thead><tr><th>Who</th><th>May</th><th>Document</th><th></th></tr></thead><tbody></tbody></table>
      <form id="grant-form" style="margin-top:10px">
        <div class="row"><input type="text" id="grant-who" placeholder="name@domain or pyr:…" autocomplete="off"><button type="submit">Grant</button></div>
        <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:14px" id="grant-may">
          <label><input type="checkbox" value="ask" checked> ask</label><label><input type="checkbox" value="chat"> chat</label><label><input type="checkbox" value="run"> run</label><label><input type="checkbox" value="operate"> operate</label>
        </div>
      </form>
    </div>
  </section>

  <section id="tab-chat" hidden>
    <h2>Talk to a stranger</h2>
    <p class="mute" style="font-size:14px;margin-top:0">You are paired at random: with someone else on this hub or on a hub it follows, or with a bot. A bot is a set of trees on the network, not a person and not a model; each of its lines carries the path that produced it, and what it cannot answer it says so, with the tree that would. Nobody has a name and nothing is kept: the conversation is gone when either of you leaves. <span id="chat-count"></span></p>
    <div class="notice" id="chat-guest" hidden>This hub's lobby is open to anyone, and you are here without its token: you meet people, not bots. A bot runs the hub's trees, and the token is what guards them.</div>
    <div class="card" id="chat-off">
      <form class="row" id="chat-start" style="margin:0">
        <input type="text" id="chat-interests" placeholder="interests, optional: weather, news, music" autocomplete="off">
        <select id="chat-want" aria-label="who to meet"><option value="anyone">a stranger, or a bot</option><option value="people">people only</option><option value="bot">a bot</option></select>
        <button type="submit">Start</button>
      </form>
    </div>
    <div class="card" id="chat-on" hidden>
      <div class="chat-log" id="chat-log" aria-live="polite"></div>
      <div class="typing-line" id="chat-typing-line" aria-live="polite"></div>
      <form class="row" id="chat-say" style="margin:8px 0 0">
        <button type="button" class="quiet" id="chat-next" title="Esc">New</button>
        <input type="text" id="chat-text" placeholder="say something" autocomplete="off" maxlength="1000">
        <button type="submit">Send</button>
      </form>
      <div class="mute" style="font-size:13px">Stop (or Esc), twice: end this and meet someone new · <a href="#" id="chat-stop">leave the lobby</a></div>
    </div>
    <label class="mute" style="font-size:14px" id="chat-typing-opt"><input type="checkbox" id="chat-typing" checked> Typing indicator: see when the stranger is typing, and let them see when you are. Off, neither of you sees it.</label>
  </section>

  <section id="tab-api" hidden>
    <h2>The same endpoints this page uses</h2>
    <div class="card">
      <pre id="api-pre">POST /intent   {"text": "…"}
POST /run      {"tree": "…", "inputs": {…}}
POST /resume   {"snapshot": {…}, "answer": …}
GET  /capabilities
GET  /health
GET  /gaps
GET  /unanswered
POST /build    {"text": "…", "answers": {…}}   the builder's questions and the checked file
GET  /peers    (hub only)
POST /chat/start {"want": "anyone"|"people"|"bot", "typing_indicator": true}   (hub with --chat)
POST /chat/typing {"seat": "…", "typing": true}</pre>
      <div class="mute" style="font-size:14px;margin-top:8px" id="api-help">With a token: <code>Authorization: Bearer …</code>. From the command line: <code>pyrtan ask "…" --at <span class="at">URL</span></code>, <code>pyrtan gaps --at <span class="at">URL</span></code>.</div>
    </div>
  </section>

  <footer><a href="https://pyrtan.com">pyrtan.com</a> · <a href="https://pyrtan.com/source/">source</a> · AGPL-3.0-or-later · console v__VERSION__<span id="hub-version"></span></footer>
</main>
<script id="pyrtan-trees" type="application/json">__TREES__</script>
<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const examples = {
  "weather": "what's the weather in Oslo",
  "hacker-news": "top 3 stories on hacker news",
  "site-status": "is example.com up",
  "github-release": "latest release of astral-sh/uv",
  "hacker-news-item": "hacker news item 1",
  "wikipedia": "tell me about Ada Lovelace",
  "define": "what does serendipity mean",
  "sun": "when is sunset in Oslo",
  "air-quality": "air quality in Delhi",
  "npm-package": "latest version of react on npm",
  "pypi-package": "latest requests on pypi",
  "hn-search": "search hacker news for sqlite",
  "books": "who wrote The Left Hand of Darkness",
  "holidays": "next holiday in US",
  "exchange-rate": "convert 100 USD to EUR",
  "synonyms": "synonyms for happy",
  "forecast": "weather in Oslo tomorrow",
  "uv-index": "uv index in Sydney",
  "local-time": "what time is it in Tokyo",
  "elevation": "how high is Quito",
  "postal-code": "where is zip 90210",
  "wayback": "is example.com archived",
  "rhymes": "what rhymes with moon",
  "github-repo": "how many stars does astral-sh/uv have",
  "go-module": "latest version of github.com/gorilla/mux on go",
  "homebrew": "brew formula wget",
  "pokemon": "pokemon pikachu",
  "antonyms": "opposite of happy",
  "capital": "what is the capital of Norway",
  "pollen": "pollen in Paris",
  "waves": "waves in Lisbon",
  "npm-downloads": "how many downloads does react have",
  "nuget": "latest version of Newtonsoft.Json on nuget",
  "dns": "mx record for gmail.com",
  "food-barcode": "what is barcode 3017624010701",
  "recipe": "recipe for lasagna",
  "cocktail": "cocktail margarita",
};
// Served by a Pyrtan, only its token is kept, under its own origin; served elsewhere, the hub URL and token.
const KEY = "pyrtan.console:" + location.origin;
const store = {
  get() { try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } },
  set(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} },
  clear() { try { localStorage.removeItem(KEY); } catch (e) {} },
};
// base: "" when this page is served by a Pyrtan (relative URLs); otherwise the hub URL the person gave.
// local: the hub in this page (pyrtan.js's LocalHub), while no hub is joined.
let base = "", token = "", remote = false, isHub = false, local = null, catalog = "";

async function api(path, init) {
  if (local) {
    const body = init && init.body ? JSON.parse(init.body) : undefined;
    return local.route((init && init.method) || "GET", path, body).then(([status, payload]) => new Response(JSON.stringify(payload), {status, headers: {"Content-Type": "application/json"}}));
  }
  const headers = Object.assign({"Accept": "application/json"}, (init && init.headers) || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const url = base ? base.replace(/\/+$/, "") + "/" + path : path;
  const method = ((init && init.method) || "GET").toUpperCase();
  const body = init && init.body ? JSON.parse(init.body) : (method === "POST" ? {} : null);
  Object.assign(headers, await ident.headers(method, "/" + path.split("?")[0].replace(/^\/+/, ""), body));
  return fetch(url, Object.assign({}, init, {headers}));
}
async function json(path, init) {
  const r = await api(path, init);
  if (r.status === 401) throw new Error("401: this Pyrtan needs a token");
  return r.json();
}
function problem(msg, bad) { $("#problem").innerHTML = msg ? `<div class="notice${bad ? " bad" : ""}">${esc(msg)}</div>` : ""; }

// ---------------------------------------------------------------- identity
// This browser's key: made and held by crypto.subtle (non-extractable, in
// IndexedDB), paired with a root by a device note, used to sign requests
// for the hub's audience: the URL this page calls, never one the hub names
// (a hub naming another's audience would collect signatures good there). See identity.js.
// pyrtan.com's names: files in this repository, added by pull request (site/names/README.md).
const PYRTAN_NAMES = {repo: "b-i-t-8/pyrtan", branch: "main", dir: "site/names", domain: "pyrtan.com"};
const githubNewFile = (repo, branch, dir, filename, text) => `https://github.com/${repo}/new/${encodeURIComponent(branch)}${dir ? "/" + dir : ""}?filename=${encodeURIComponent(filename)}&value=${encodeURIComponent(text)}`;
const githubRepo = (url) => { const m = /^https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?)(?:\.git)?\/?$/.exec(String(url || "").trim()); return m ? m[1] : null; };
function saveFile(name, data) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + "\n"], {type: "application/json"}));
  a.download = name; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
const pre = (data) => `<pre style="max-height:180px">${esc(JSON.stringify(data, null, 2))}</pre>`;

const ident = {
  ok: null, key: null, audience: null, sign: true,
  async init() {
    this.ok = await PyrtanIdentity.available();
    if (this.ok) { try { this.key = await PyrtanIdentity.store.get(); } catch (e) { this.key = null; } }
    try { this.sign = localStorage.getItem("pyrtan.identity.sign") !== "off"; } catch (e) {}
    this.render();
  },
  isRoot() { return !!(this.key && this.key.role === "root"); },
  active() { return !!(this.ok && this.key && this.sign && this.audience); },
  async headers(method, path, body) {
    if (!this.active()) return {};
    return PyrtanIdentity.signRequest(this.key, this.audience, method, path, body, this.key.note || null);
  },
  async keep() { await PyrtanIdentity.store.put(this.key); this.render(); moveKept(); },
  // ---- a device of an identity held elsewhere
  async make() {
    const k = await PyrtanIdentity.generate();
    this.key = {role: "device", id: k.id, privateKey: k.privateKey, publicKey: k.publicKey, note: null, made: Date.now()};
    await this.keep();
  },
  async useNote(text) {
    let note; try { note = JSON.parse(text); } catch (e) { problem("The note is JSON: paste what the browser holding your identity, or pyrtan id device add, gave you.", true); return; }
    const bad = PyrtanIdentity.noteProblem(note, this.key.id);
    if (bad) { problem(bad, true); return; }
    this.key.note = note; problem(""); await this.keep();
  },
  // ---- your identity, in this browser
  async makeRoot(pass) {
    const r = await PyrtanIdentity.createRoot();
    const sealed = await PyrtanIdentity.makeBackup(r.seed, r.id, pass);
    const key = await PyrtanIdentity.rootFromSeed(r.seed, r.id);  // non-extractable from here on; the seed is wiped
    const document = await PyrtanIdentity.makeIdentity(key);
    this.key = {role: "root", id: key.id, privateKey: key.privateKey, sealed, document, backedUp: false, note: null, made: Date.now()};
    await this.keep();
  },
  async restore(file, pass) {
    let b; try { b = JSON.parse(await file.text()); } catch (e) { throw new Error("that file is not a backup"); }
    const seed = await PyrtanIdentity.openBackup(b, pass);
    const key = await PyrtanIdentity.rootFromSeed(seed, b.id.toLowerCase());
    const sealed = Object.fromEntries(Object.entries(b).filter(([k]) => k !== "document"));
    const document = b.document || await PyrtanIdentity.makeIdentity(key);
    this.key = {role: "root", id: key.id, privateKey: key.privateKey, sealed, document, backedUp: true, note: null, made: Date.now()};
    await this.keep();
  },
  backup() { return Object.assign({}, this.key.sealed, {document: this.key.document}); },
  async backedUp() { if (!this.key.backedUp) { this.key.backedUp = true; await this.keep(); } },
  async addDevice(code, may, label) {
    const note = await PyrtanIdentity.makeDevice(this.key, code.trim(), may, {label});
    this.key.document = await PyrtanIdentity.withDevice(this.key.document, this.key, note);
    await this.keep();
    return note;
  },
  async revoke(device) {
    this.key.document = await PyrtanIdentity.withRevoked(this.key.document, this.key, device);
    await this.keep();
  },
  async forget() {
    const what = this.isRoot() ? "Forget your identity on this browser? Without its backup nobody can give it back." : "Forget this browser's key? A hub that granted it, or a member that joined as it, will not know this browser again.";
    if (!confirm(what)) return;
    await PyrtanIdentity.store.put(null); this.key = null; this.render(); moveKept();
  },
  render() {
    if (!$("#tab-identity")) return;
    const root = this.isRoot(), device = !!this.key && !root;
    $("#ident-none").hidden = this.ok !== false;
    $("#ident-start").hidden = !this.ok || !!this.key;
    for (const id of ["#ident-root", "#ident-claim", "#ident-devices"]) $(id).hidden = !this.ok || !root;
    $("#ident-on").hidden = !this.ok || !device;
    $("#ident-common").hidden = !this.ok || !this.key;
    if (!this.key) return;
    $("#ident-sign").checked = this.sign;
    $("#doc-push").hidden = !root || !this.audience;
    if (device) {
      $("#ident-id").textContent = this.key.id;
      const n = this.key.note;
      $("#ident-paired").innerHTML = n
        ? `speaks for <code>${esc(n.root)}</code> · may ${esc(n.may.join(", ") || "nothing")} · until ${esc(new Date(n.expires * 1000).toLocaleDateString())}${n.label ? " · " + esc(n.label) : ""}`
        : "not paired yet: it signs as itself, so a hub grants this key, or nothing";
      $("#ident-howto").textContent = `pyrtan id device add ${this.key.id} --may ask,chat --label browser`;
      return;
    }
    $("#root-id").textContent = this.key.id;
    $("#root-backup-state").innerHTML = this.key.backedUp
      ? `<span class="mute" style="font-size:14px">Backed up. Keep the file somewhere only you can read; never in a public repository.</span>`
      : `<div class="notice bad" style="margin:0">Save your backup now. It is the only way back to this identity if this browser's data is cleared. It is encrypted with your passphrase; keep it where only you can read it.</div>`;
    const tb = $("#dev-table tbody"); tb.innerHTML = "";
    const doc = this.key.document;
    for (const n of doc.devices) tb.insertAdjacentHTML("beforeend", `<tr><td>${esc(n.label || "-")}<div class="mute"><code>${esc(PyrtanIdentity.short(n.device))}</code></div></td><td>${esc(n.may.join(", "))}</td><td class="mute">${esc(new Date(n.expires * 1000).toLocaleDateString())}</td><td><button type="button" class="quiet" data-revoke="${esc(n.device)}">Revoke</button></td></tr>`);
    for (const r of doc.revoked) tb.insertAdjacentHTML("beforeend", `<tr><td class="mute"><code>${esc(PyrtanIdentity.short(r))}</code></td><td class="mute" colspan="3">revoked</td></tr>`);
    if (!doc.devices.length && !doc.revoked.length) tb.innerHTML = `<tr><td class="mute" colspan="4">none yet</td></tr>`;
  },
};

// ---- starting
$("#ident-make").addEventListener("click", () => ident.make().catch((e) => problem("Could not make a key: " + e.message, true)));
$("#ident-new").addEventListener("click", () => { $("#ident-new-form").hidden = false; $("#ident-restore-form").hidden = true; $("#ident-pass").focus(); });
$("#ident-restore-open").addEventListener("click", () => { $("#ident-restore-form").hidden = false; $("#ident-new-form").hidden = true; });
$("#ident-new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const a = $("#ident-pass").value, b = $("#ident-pass2").value;
  if (a !== b) { problem("The two passphrases differ.", true); return; }
  if (a.length < 8) { problem("A passphrase is at least 8 characters; a long sentence is better.", true); return; }
  const btn = e.submitter || e.target.querySelector("button"); btn.disabled = true; btn.textContent = "Making it…";
  try { await ident.makeRoot(a); problem(""); } catch (err) { problem("Could not make your identity: " + err.message, true); }
  btn.disabled = false; btn.textContent = "Make it"; $("#ident-pass").value = $("#ident-pass2").value = "";
});
$("#ident-restore-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = $("#ident-file").files[0]; if (!f) { problem("Choose the backup file.", true); return; }
  try { await ident.restore(f, $("#ident-restore-pass").value); problem(""); } catch (err) { problem(err.message, true); }
  $("#ident-restore-pass").value = "";
});
// ---- a device
$("#ident-note-form").addEventListener("submit", (e) => { e.preventDefault(); ident.useNote($("#ident-note").value.trim()); });
$("#ident-copy").addEventListener("click", () => { navigator.clipboard && navigator.clipboard.writeText(ident.key.id); });
// ---- backup
$("#root-download").addEventListener("click", async () => { saveFile("pyrtan-identity-backup.json", ident.backup()); await ident.backedUp(); });
$("#root-gh-go").addEventListener("click", async () => {
  const login = $("#root-gh").value.trim();
  if (!/^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$/.test(login)) { problem("Enter your GitHub login.", true); return; }
  const create = "https://github.com/new?name=pyrtan-identity&visibility=private&description=" + encodeURIComponent("My pyrtan identity backup (encrypted). Keep this repository private.");
  const file = githubNewFile(`${login}/pyrtan-identity`, "main", "", "pyrtan-identity-backup.json", JSON.stringify(ident.backup(), null, 2) + "\n");
  $("#root-gh-out").innerHTML = `1. <a href="${esc(create)}" target="_blank" rel="noopener">Make a <strong>private</strong> repository named pyrtan-identity</a> (check that it says Private). 2. <a href="${esc(file)}" target="_blank" rel="noopener" id="root-gh-file">Commit your backup to it</a>. GitHub keeps every version; the backup is only as strong as your passphrase, so never make that repository public.`;
  $("#root-gh-file").addEventListener("click", () => ident.backedUp());
});
// ---- claiming a name
for (const r of document.querySelectorAll('input[name="claim-where"]')) r.addEventListener("change", () => { const net = document.querySelector('input[name="claim-where"]:checked').value === "network"; $("#claim-pyrtan").hidden = net; $("#claim-network").hidden = !net; });
$("#claim-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("#claim-out"); out.innerHTML = "";
  try {
    let claim, repo, branch, dir, gh = null;
    if (document.querySelector('input[name="claim-where"]:checked').value === "pyrtan") {
      const login = $("#claim-login").value.trim().toLowerCase();
      if (!/^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}$/.test(login)) throw new Error("enter your GitHub login");
      const r = await fetch(`https://api.github.com/users/${encodeURIComponent(login)}`, {headers: {"Accept": "application/json"}});
      if (!r.ok) throw new Error(`GitHub does not know ${login} (${r.status})`);
      gh = (await r.json()).id;
      claim = await PyrtanIdentity.makeName(ident.key, `${login}@${PYRTAN_NAMES.domain}`, {extra: {github: gh}});
      ({repo, branch, dir} = PYRTAN_NAMES);
    } else {
      claim = await PyrtanIdentity.makeName(ident.key, $("#claim-name").value);
      repo = githubRepo($("#claim-repo").value); branch = $("#claim-branch").value.trim() || "main"; dir = "names";
      if (!repo && $("#claim-repo").value.trim()) repo = null;
    }
    const file = `${claim.name}.json`, docFile = `${claim.name}.id.json`, text = JSON.stringify(claim, null, 2) + "\n", docText = JSON.stringify(ident.key.document, null, 2) + "\n";
    let html = `<div style="margin-top:10px">Your claim to <code>${esc(claim.name)}@${esc(claim.domain)}</code>, signed by your key:</div>${pre(claim)}`;
    if (repo) {
      html += `<div>1. <a href="${esc(githubNewFile(repo, branch, dir, file, text))}" target="_blank" rel="noopener" id="claim-open">Propose it on GitHub</a>: GitHub opens <code>${esc(dir)}/${esc(file)}</code> filled in; choose <em>Propose changes</em>, then <em>Create pull request</em>${gh !== null ? `, from the GitHub account ${esc(claim.name)}` : ""}.</div>`;
      html += `<div>2. Optional, once that is merged: <a href="${esc(githubNewFile(repo, branch, dir, docFile, docText))}" target="_blank" rel="noopener">propose your identity document</a> (<code>${esc(docFile)}</code>), so hubs find your devices there.</div>`;
    } else {
      html += `<div>Add it to the network's names repository as <code>names/${esc(file)}</code> (and your identity document as <code>names/${esc(docFile)}</code>), and open a merge request there.</div>`;
    }
    html += `<div style="margin-top:6px"><button type="button" class="quiet" id="claim-save">Download the claim</button> <button type="button" class="quiet" id="claim-copy">Copy it</button></div>`;
    out.innerHTML = html;
    $("#claim-save").addEventListener("click", () => saveFile(file, claim));
    $("#claim-copy").addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(text));
    problem("");
  } catch (err) { problem("Could not make the claim: " + err.message, true); }
});
// ---- devices
$("#dev-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const may = Array.from(document.querySelectorAll("#dev-may input:checked")).map((i) => i.value);
  try {
    const note = await ident.addDevice($("#dev-code").value, may, $("#dev-label").value.trim());
    $("#dev-out").innerHTML = `<div style="margin-top:10px">Paste this on <code>${esc(note.label || PyrtanIdentity.short(note.device))}</code>, under <em>Use this note</em>:</div>${pre(note)}<button type="button" class="quiet" id="dev-copy">Copy it</button>`;
    $("#dev-copy").addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(JSON.stringify(note)));
    $("#dev-code").value = $("#dev-label").value = ""; problem("");
  } catch (err) { problem("Could not sign the note: " + err.message, true); }
});
$("#dev-table").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-revoke]"); if (!b) return;
  if (!confirm("Revoke this device? Hubs refuse it once they have your newer document.")) return;
  await ident.revoke(b.dataset.revoke);
  problem("Revoked. Give your newest document to the hubs you use, and to your name's repository, so they know now.");
});
$("#doc-push").addEventListener("click", async () => {
  const r = await api("identity", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({document: ident.key.document})});
  const b = await r.json().catch(() => ({}));
  problem(r.ok ? "This hub holds your newest document." : `The hub did not take it: ${b.say || r.status}`, !r.ok);
});
$("#doc-download").addEventListener("click", () => saveFile(`${ident.key.id.slice(4, 12)}.id.json`, ident.key.document));
$("#ident-forget").addEventListener("click", () => ident.forget());
$("#ident-sign").addEventListener("change", (e) => { ident.sign = e.target.checked; try { localStorage.setItem("pyrtan.identity.sign", ident.sign ? "on" : "off"); } catch (err) {} });

// ---------------------------------------------------------------- access: the hub's grants
async function loadGrants() {
  const tb = $("#grant-table tbody"), note = $("#access-note");
  let r; try { r = await api("allow"); } catch (e) { note.hidden = false; note.textContent = String(e); return; }
  if (r.status === 401 || r.status === 403) { note.hidden = false; note.textContent = "Only this hub's operator sees and changes its grants: connect with its token, or as an identity it grants operate."; tb.innerHTML = ""; return; }
  note.hidden = true;
  const body = await r.json().catch(() => ({grants: []}));
  tb.innerHTML = (body.grants || []).map((g) => `<tr><td>${g.name ? `<code>${esc(g.name)}</code>` : ""}<div class="mute"><code>${esc(PyrtanIdentity.short(g.id))}</code></div></td><td>${esc(g.may.join(", "))}</td><td class="mute">${g.document ? `${esc(String(g.document.devices))} device(s), ${esc(String(g.document.revoked))} revoked` : "none yet"}</td><td><button type="button" class="quiet" data-disallow="${esc(g.id)}">Remove</button></td></tr>`).join("") || `<tr><td class="mute" colspan="4">nobody yet</td></tr>`;
}
async function grantPost(path, payload) {
  const r = await api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const b = await r.json().catch(() => ({}));
  if (!r.ok) problem(b.say || `the hub answered ${r.status}`, true); else problem("");
  loadGrants();
}
$("#grant-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const may = Array.from(document.querySelectorAll("#grant-may input:checked")).map((i) => i.value);
  grantPost("allow", {who: $("#grant-who").value.trim(), may}).then(() => { $("#grant-who").value = ""; });
});
$("#grant-table").addEventListener("click", (e) => { const b = e.target.closest("button[data-disallow]"); if (b) grantPost("disallow", {who: b.dataset.disallow}); });

function showTab(name) {
  for (const a of document.querySelectorAll("#tabs a")) { if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  for (const s of document.querySelectorAll("main > section[id^=tab-]")) s.hidden = s.id !== "tab-" + name;
  if (name === "peers") loadPeers();
  if (name === "gaps") loadGaps();
  if (name === "watches") loadWatches();
  if (name === "unanswered") loadUnanswered();
  if (name === "directory") loadDirectory();
  if (name === "member") member.render();
  if (name === "identity") ident.render();
  if (name === "access") loadGrants();
  if (name === "chat") chat.count();
  if (name === "agents") agentsUI.render();
}

async function loadDirectory() {
  let d; try { d = await json("directory"); } catch (e) { problem(e.message, true); return; }
  const body = $("#directory tbody"); body.innerHTML = "";
  for (const h of d.hubs || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="link ${esc(h.link)}"></span>${esc(h.link)}</td><td><code>${esc(h.name||"")}</code>${h.via === "self" ? ' <span class="mute">(this hub)</span>' : ""}${h.kind === "member" ? ' <span class="mute">(member)</span>' : ""}<div class="mute">${esc(h.id)}</div></td><td>${(h.trees||[]).map(n => `<code>${esc(n)}</code>`).join(" ")}</td><td class="mute">${h.seen ? new Date(h.seen * 1000).toLocaleString() : ""}</td>`;
    body.appendChild(tr);
  }
  if (!(d.hubs || []).length) body.innerHTML = `<tr><td colspan="4" class="mute">Nobody heard of yet. Join the default network (<code>pyrtan join</code>), or start this hub with <code>--listed</code>.</td></tr>`;
}
window.addEventListener("hashchange", () => showTab(tabFromHash()));

// A trigger as a sentence: the first of each choice, an input's default or "…"
// for its blank, and the optional words kept or dropped.
function phrase(t, pat, optional) {
  return pat.replace(/\[([^\]|]*)[^\]]*\]/g, optional ? "$1" : "").replace(/\(([^|)]*)\|[^)]*\)/g, "$1").replace(/\{(\w+)(?::\w+)?\}/g, (m, n) => (t.inputs && t.inputs[n] && t.inputs[n].default != null) ? t.inputs[n].default : "…").replace(/\s+/g, " ").trim();
}
function sample(t) {
  if (examples[t.name]) return examples[t.name];
  const pat = (t.triggers || [])[0]; return pat ? phrase(t, pat, false) : null;
}
// Every sentence a tree answers to, for the ask box to suggest; one that starts with a blank suggests nothing.
function samples(t) {
  return [examples[t.name], ...(t.triggers || []).map(p => phrase(t, p, true))].filter(s => s && !s.startsWith("…"));
}

async function detect() {
  // Same origin first: a Pyrtan answers /api with {"pyrtan": "service"|"hub"}.
  try {
    const r = await fetch("api", {headers: {"Accept": "application/json"}});
    const a = r.ok ? await r.json() : null;
    if (a && a.pyrtan) {
      base = ""; remote = false; isHub = a.pyrtan === "hub";
      if (a.auth === "bearer") {
        // This Pyrtan wants a token: show the connect box with only the token field.
        token = store.get().token || "";
        $("#connect").hidden = false;
        $("#hub-url").hidden = true;
        $("#connect-lede").textContent = "This Pyrtan needs a token:";
        $("#connect-help").innerHTML = "Kept in this browser only. The token is the one the hub or service was started with (<code>--token</code> or <code>$PYRTAN_TOKEN</code>).";
        $("#hub-token").value = token;
        if (!token) problem(a.chat_public ? "Enter the token to see what this Pyrtan can answer, or open Chat: its lobby is open to anyone." : "Enter the token to see what this Pyrtan can answer.");
      }
      return a;
    }
  } catch (e) {}
  remote = true;
  const saved = store.get();
  const asked = new URLSearchParams(location.search).get("hub");
  base = asked || saved.url || "";
  // The saved token is the saved hub's: a link to another hub (?hub=) gets no token until the person enters one.
  const same = (a, b) => (a || "").replace(/\/+$/, "").toLowerCase() === (b || "").replace(/\/+$/, "").toLowerCase();
  token = (!asked || same(asked, saved.url)) ? (saved.token || "") : "";
  $("#connect").hidden = false;
  $("#origin").textContent = location.origin;
  $("#hub-url").value = base; $("#hub-token").value = token;
  local = null; showLocal(false);
  if (!base) return startLocal();
  try {
    const a = await json("api");
    if (a && a.pyrtan) { isHub = a.pyrtan === "hub"; return a; }
    problem(`${base} answered, but not as a Pyrtan.`, true);
  } catch (e) { problem(`${base}: ${e.message}. Is the hub up, and started with --cors ${location.origin}?`, true); }
  return null;
}

// ---------------------------------------------------------------- the hub in this page
// No hub joined: this browser is one. It holds trees and answers the same
// endpoints a hub does, and keeps its gaps and unanswered sentences in
// local storage under the device it is (below). The trees: a
// catalog named in the address or one beside this page (fresher, and no
// further than this page's own site), else the ones this page carries,
// which need no network at all; only a page that carries none asks
// pyrtan.com's catalog, then the copy this browser kept.
const params = new URLSearchParams(location.search);
// ?catalog= only on this page's own origin: a link elsewhere would have this browser fetch, run and keep trees a stranger chose.
const sameOrigin = (u) => { try { return new URL(u, location.href).origin === location.origin; } catch (e) { return false; } };
const CATALOGS = [params.get("catalog"), "../catalog/"].filter((c) => c && sameOrigin(c));
const FARTHER = ["https://pyrtan.com/catalog/"];
const CARRIED = (() => { try { const d = JSON.parse($("#pyrtan-trees").textContent); return Array.isArray(d.trees) ? d.trees : []; } catch (e) { return []; } })();
const TREES_KEY = "pyrtan.console.trees";
let carried = false;
async function catalogTrees() {
  carried = false;
  for (const c of CATALOGS.concat(CARRIED.length ? [] : FARTHER)) {
    try {
      const at = new URL(c.endsWith("/") ? c : c + "/", location.href);
      const r = await fetch(new URL("trees.json", at), {headers: {"Accept": "application/json"}});
      const d = r.ok ? await r.json() : null;
      if (d && Array.isArray(d.trees)) {
        catalog = at.href;
        try { localStorage.setItem(TREES_KEY, JSON.stringify({catalog, trees: d.trees})); } catch (e) {}
        return d.trees;
      }
    } catch (e) {}
  }
  if (CARRIED.length) { catalog = ""; carried = true; return CARRIED; }
  // Offline, or no catalog answers: the trees this browser held last time.
  try { const kept = JSON.parse(localStorage.getItem(TREES_KEY) || "null"); if (kept && Array.isArray(kept.trees)) { catalog = kept.catalog || ""; return kept.trees; } } catch (e) {}
  return [];
}
// A tree runs here when every source it uses is http. Hosts of "*" mean any site, and most sites refuse a page, so "Try one" leaves those out.
const runsHere = (t) => (t.sources || []).every(s => s === "http");
const anySite = (t) => (t.hosts || []).includes("*");
// A tree that reads the operator's env (credentials, say) has none to read in a page; a service started with --env runs it.
const needsEnv = (t) => { const held = local && local.runner.get(t.name); return !!held && /\{\{\s*env\./.test(JSON.stringify(held.raw)); };
// What the hub in this page keeps across reloads, under the device this
// browser is: its key when it has one, else an id of its own, made once.
// Gaps name the device that met them, as a hub's name the peer.
const DEVICE_KEY = "pyrtan.device";
function deviceId() {
  if (ident.key && ident.key.id) return ident.key.id;
  try {
    let d = localStorage.getItem(DEVICE_KEY);
    if (!d) { d = "browser-" + [...crypto.getRandomValues(new Uint8Array(8))].map(b => b.toString(16).padStart(2, "0")).join(""); localStorage.setItem(DEVICE_KEY, d); }
    return d;
  } catch (e) { return "browser"; }
}
const keptFor = (device) => {
  const k = "pyrtan.kept:" + device;
  return {
    load() { return JSON.parse(localStorage.getItem(k) || "null"); },
    save(v) { localStorage.setItem(k, JSON.stringify(v)); },
    clear() { localStorage.removeItem(k); },
  };
};
const deviceName = (d) => d && d.startsWith("pyr:") ? PyrtanIdentity.short(d) : d || "";
// The key changed (made, restored, paired or forgotten): what this browser kept goes to the device it is now.
function moveKept() {
  if (!local) return;
  const d = deviceId();
  if (d !== local.device) local.rekey(d, keptFor(d));
  sync.identityChanged();
  showLocal(true);
}
async function startLocal() {
  isHub = false;
  const device = deviceId();
  local = new Pyrtan.LocalHub({name: "this browser", version: "__VERSION__", device, keep: keptFor(device)});
  local.onOwnChange = () => sync.changed();
  const trees = await catalogTrees();
  for (const e of trees) local.add(e.tree, e.id);
  showLocal(true);
  return (await local.route("GET", "/api"))[1];
}
function showLocal(on) {
  $("#connect-local").hidden = !on;
  $("#connect-lede").textContent = on ? `This browser is a hub of its own: it holds ${carried ? "the trees this console carries" : "the catalog's trees"}, reads your sentences and runs the trees right here.` : "This console is not being served by a Pyrtan. Point it at one:";
  $("#connect-local").innerHTML = on ? `Gaps and unanswered sentences are kept in this browser, across reloads, for this device (<code>${esc(deviceName(local.device))}</code>). Trees that need a box (files, programs, devices) or a site that will not let a page read it need a hub: to join one, give its URL and token.` : "";
  $("#gaps-local").hidden = !on;
  if (on) $("#gaps-device").textContent = deviceName(local.device);
  sync.render();
  $("#connect-go").textContent = on ? "Join" : "Connect";
  $("#watches-tab").hidden = on;
  $("#unanswered-lede").innerHTML = on ? "Sentences nobody answered, each with the tree that would. Each command writes that tree's file; serve it from a hub and join the hub here, or publish it to the catalog and this page will hold it too." : 'Sentences nobody answered, each with the tree that would. <code>pyrtan unanswered --at <span class="at">URL</span> --scaffold DIR</code> writes the files.';
  $("#api-help").innerHTML = on ? "Answered inside this page by <code>pyrtan.js</code>: the same paths and the same answers as a hub, with no server behind them. Join a hub and they go to the hub." : 'With a token: <code>Authorization: Bearer …</code>. From the command line: <code>pyrtan ask "…" --at <span class="at">URL</span></code>, <code>pyrtan gaps --at <span class="at">URL</span></code>.';
  $("#trees-more").innerHTML = on ? (carried ? "These are the trees this console carries, so it needs no network to hold them; newer ones are in the <a href=\"https://pyrtan.com/catalog/\">catalog</a>." : `These are the <a href="${esc(catalog || "https://pyrtan.com/catalog/")}">catalog</a>'s trees.`) + " Rows marked <em>needs a hub</em> reach what a page cannot; a service runs them." : 'Need a tree that is not here? Browse the <a href="https://pyrtan.com/catalog/" id="catalog-link">catalog</a>, then <code>pyrtan pull NAME</code> into a folder a service serves.';
}

async function load() {
  problem("");
  const a = await detect();
  for (const el of document.querySelectorAll(".at")) el.textContent = base || location.origin;
  if (!a) { $("#who").textContent = remote ? "not connected" : "unreachable"; return; }
  $("#peers-tab").hidden = !isHub;
  $("#directory-tab").hidden = !(isHub && a.federation);
  $("#member-tab").hidden = !(isHub && a.federation);
  ident.audience = a.audience ? (base ? base.replace(/\/+$/, "") : location.origin) : null;  // the URL this page calls, not what the hub says
  $("#identity-tab").hidden = false;  // an identity needs no hub; signing does, when there is one
  $("#access-tab").hidden = !(isHub && a.audience);
  $("#chat-tab").hidden = !(isHub && a.chat);
  if (a.chat && a.chat_bots === false && token) {
    try { a.chat_bots = (await json("api")).chat_bots; } catch (e) {}  // the first ask went without the token
  }
  if (a.chat && a.chat_bots === false) {
    // A public lobby, and no token here: people only, since a bot runs the hub's trees.
    $("#chat-want").innerHTML = `<option value="people">people only</option>`;
    $("#chat-guest").hidden = false;
  }
  member.hub = a.federation || null;
  if (member.hub) member.start();
  $("#where").textContent = local ? "running in this browser" : (base || location.origin) + (a.auth === "bearer" ? " · token" : "");
  $("#hub-version").textContent = a.version ? ` · ${a.pyrtan} v${a.version}` : "";
  try {
    const h = await json("health");
    $("#who").textContent = (h.name ? h.name + " · " : "") + (h.peers != null ? `${h.up}/${h.peers} peers up` + (h.trees != null ? `, ${h.trees} trees` : "") : (h.trees ? `${h.trees.length} trees` : ""));
  } catch (e) { $("#who").textContent = "unreachable"; }
  for (const el of document.querySelectorAll(".at")) el.textContent = base || location.origin;
  if (a.auth === "bearer" && !token) {
    if (a.chat_public && location.hash === "#chat") showTab("chat");  // the one tab open without the token
    return;
  }
  let c;
  try { c = await json("capabilities"); } catch (e) { problem(e.message + (a.auth === "bearer" ? " (is it the right one?)" : ""), true); return; }
  problem(local && !(c.trees || []).length ? "No catalog answered and none was kept in this browser, so it holds no trees yet. Join a hub above, or try again online." : "", true);
  renderCaps(c);
  showTab(tabFromHash());
}
function renderCaps(c) {
  const body = $("#caps tbody"); const tries = $("#tries");
  body.innerHTML = ""; tries.innerHTML = "";
  suggest.from((c.trees || []).filter(t => !local || (runsHere(t) && !anySite(t) && !needsEnv(t))));
  for (const t of c.trees || []) {
    const tr = document.createElement("tr");
    const here = !local ? "" : !runsHere(t) ? `<div class="gap">needs a hub: ${esc((t.sources||[]).filter(x => x !== "http").join(", "))}</div>` : needsEnv(t) ? '<div class="gap">needs a hub: credentials, given with --env</div>' : anySite(t) ? '<div class="gap">any site; most will not let a page read them</div>' : "";
    tr.innerHTML = `<td><code>${esc(t.name)}</code> <span class="mute">v${esc(t.version||1)}</span><div class="mute">${esc(t.description||"")}</div></td><td>${(t.triggers||[]).map(p => `<div><code>${esc(p)}</code></div>`).join("")}</td><td class="mute">${esc((t.hosts||[]).join(", "))}${t.peer ? `<div><span class="link ${esc(t.link||"")}"></span>${esc(t.peer)}</div>` : ""}${here}</td>`;
    body.appendChild(tr);
    const s = sample(t); if (s && !s.includes("…") && (!local || (runsHere(t) && !anySite(t) && !needsEnv(t)))) { const li = document.createElement("li"); li.innerHTML = `<a href="#" data-q="${esc(s)}">${esc(s)}</a>`; tries.appendChild(li); }
  }
  // The greyed suggestion in the empty box (Tab takes it) is one this swarm answers.
  const first = tries.querySelector("a[data-q]"), offered = [...tries.querySelectorAll("a[data-q]")].map(a => a.dataset.q);
  if (first && !offered.includes($("#text").placeholder)) $("#text").placeholder = first.dataset.q;
}

async function loadPeers() {
  if (!isHub) return;
  let p; try { p = await json("peers"); } catch (e) { problem(e.message, true); return; }
  const body = $("#peers tbody"); body.innerHTML = "";
  for (const peer of p.peers || []) {
    const tr = document.createElement("tr");
    const last = peer.last_ok ? new Date(peer.last_ok * 1000).toLocaleTimeString() : "never";
    tr.innerHTML = `<td><span class="link ${esc(peer.link)}"></span>${esc(peer.link)}</td><td><code>${esc(peer.name||"")}</code><div class="mute">${esc(peer.url)}</div></td><td>${(peer.trees||[]).map(n => `<code>${esc(n)}</code>`).join(" ")}</td><td class="mute">${esc(last)}${peer.last_error ? `<div class="failed">${esc(peer.last_error)}</div>` : ""}</td>`;
    body.appendChild(tr);
  }
  if (!(p.peers || []).length) body.innerHTML = `<tr><td colspan="4" class="mute">No peers yet. Start a service with <code>--hub ${esc(base || location.origin)}</code>.</td></tr>`;
  const fed = $("#federation");
  fed.hidden = !p.id;
  if (p.id) {
    $("#fed-id").textContent = p.id;
    const hb = $("#hubs tbody"); hb.innerHTML = "";
    for (const [rel, list] of [["follows", p.following || []], ["follower", p.followers || []]]) {
      for (const r of list) {
        const tr = document.createElement("tr");
        const what = rel === "follower" && r.kind === "member" ? "member" + (r.trusted ? " (trusted)" : "") : rel;
        const drop = rel === "follower" ? ` <a href="#" data-drop="${esc(r.url)}" data-name="${esc(r.name || r.url)}">drop</a>` : "";
        tr.innerHTML = `<td><span class="link ${esc(r.link)}"></span>${esc(r.link)}</td><td><code>${esc(r.name||"")}</code><div class="mute">${esc(r.url)}</div></td><td>${what}${r.state && r.state !== "accepted" ? ` <span class="mute">(${esc(r.state)})</span>` : ""}${drop}</td><td>${(r.trees||[]).map(n => `<code>${esc(n)}</code>`).join(" ")}${r.last_error ? `<div class="failed">${esc(r.last_error)}</div>` : ""}</td>`;
        hb.appendChild(tr);
      }
    }
    if (!hb.children.length) hb.innerHTML = `<tr><td colspan="4" class="mute">Following nobody, followed by nobody. <code>pyrtan follow https://other-hub --at ${esc(base || location.origin)}</code>.</td></tr>`;
  }
}

async function loadWatches() {
  let w; try { w = await json("watches"); } catch (e) { problem(e.message, true); return; }
  const body = $("#watches tbody"); body.innerHTML = "";
  for (const x of w.watches || []) {
    const last = x.last || {};
    const when = x.last_at ? new Date(x.last_at * 1000).toLocaleTimeString() : "never";
    const changed = x.path_changed_at ? `<div class="mute">path changed ${esc(new Date(x.path_changed_at * 1000).toLocaleString())}: <code>${esc((x.previous_path||[]).join(" > "))}</code> → <code>${esc((x.path||[]).join(" > "))}</code></div>` : "";
    const err = x.last_error ? `<div class="mute">error: ${esc(x.last_error)}</div>` : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><code>${esc(x.name)}</code>${x.peer && x.peer !== "local" ? `<div class="mute">${esc(x.peer)}</div>` : ""}</td><td>${esc(x.kind)} <code>${esc(x.what)}</code></td><td>${esc(x.fires||0)}× <span class="mute">${esc(when)}</span></td><td>${last.status ? `<span class="status ${esc(last.status)}">${esc(last.status)}</span>${last.reason ? ` <span class="mute">(${esc(last.reason)})</span>` : ""} ${esc(last.say||"")}` : '<span class="mute">not yet</span>'}${changed}${err}</td>`;
    body.appendChild(tr);
  }
  if (!(w.watches || []).length) body.innerHTML = `<tr><td colspan="4" class="mute">Nothing watched. Give a tree <code>[[watches]]</code> and start the service with <code>--watch</code>.</td></tr>`;
}

async function loadGaps() {
  let g; try { g = await json("gaps"); } catch (e) { problem(e.message, true); return; }
  const body = $("#gaps tbody"); body.innerHTML = "";
  for (const gap of g.gaps || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><code>${esc(gap.tree)}</code> · <code>${esc(gap.node)}</code></td><td><code>${esc(gap.on)}</code> = <code class="gap">${esc(JSON.stringify(gap.observed))}</code></td><td>${esc(gap.count||1)}</td><td class="mute">${esc(gap.peer || whose(gap.device))}</td>`;
    body.appendChild(tr);
  }
  if (!(g.gaps || []).length) body.innerHTML = `<tr><td colspan="4" class="mute">No gaps: every decision so far had a branch.</td></tr>`;
}

// ---------------------------------------------------------------- your other devices
// Devices of one identity share their gaps browser to browser (sync.js): a
// WebRTC link opened by a code carried by hand or a QR, or a signed file.
// Only while this browser is its own hub; a joined hub keeps gaps itself.
const whose = (d) => !d ? "" : local && d === local.device ? "this device" : deviceName(d);
// The tab an address names; #sync=CODE answers a link, then shows the gaps.
function tabFromHash() {
  const h = location.hash.slice(1);
  if (h.startsWith("sync=")) {
    history.replaceState(null, "", location.pathname + location.search + "#gaps");
    sync.answer(decodeURIComponent(h.slice(5)));
    return "gaps";
  }
  if (h.startsWith("pair=")) {
    history.replaceState(null, "", location.pathname + location.search + "#identity");
    pair.answer(decodeURIComponent(h.slice(5)));
    return "identity";
  }
  return h || "ask";
}
const sync = {
  link: null, session: null, timer: null,
  root() { return PyrtanSync.rootOf(ident.key); },
  ready() { return !!(local && ident.ok && this.root()); },
  // The devices the root's document revokes: the root's own, or the one its bundle carried here.
  revoked() {
    const k = ident.key;
    const doc = !k ? null : k.role === "root" ? k.document : ((local && local.others.get(this.root())) || {}).document;
    return doc && Array.isArray(doc.revoked) ? doc.revoked : [];
  },
  held() { return local ? [...local.others.values()] : []; },
  take(b) { const took = local.takeOther(b); if (took && b.document) this.prune(); return took; },
  prune() { const root = this.root(), gone = this.revoked(); return local ? local.dropOthers((b) => b.root !== root || gone.includes(b.device)) : 0; },
  state(html, bad) { $("#sync-state").innerHTML = html ? `<span class="${bad ? "failed" : "mute"}">${html}</span>` : ""; },
  step(html) { this.stopScan(); $("#sync-step").innerHTML = html || ""; },
  render() {
    const card = $("#sync");
    card.hidden = !local;
    if (!local) { this.close(); return; }
    const ready = this.ready(), n = local.others.size;
    $("#sync-actions").hidden = !ready;
    $("#sync-scan").hidden = !ready || !("BarcodeDetector" in window);
    $("#sync-lede").innerHTML = !ident.ok
      ? "This browser cannot hold a key (it has no WebCrypto here), so it cannot share its gaps with your other devices."
      : !ready ? "To share these gaps with your other devices, make your identity in the <a href=\"#identity\">Identity</a> tab, or pair this browser as a device of the identity you have. Only devices of one identity link, and each checks the other's signature."
      : `The devices of your identity, <code>${esc(PyrtanIdentity.short(this.root()))}</code>, share their gaps and unanswered sentences directly, browser to browser, with no hub. Link two on the same network (a code to paste, or a QR to scan), or carry a file between any two. ${n ? `Held here from ${n} other device${n === 1 ? "" : "s"}.` : "Nothing held from another device yet."}`;
  },
  // A new key or another root closes the link; a revocation drops what the revoked device gave.
  identityChanged() {
    if (this.session && (this.session.root !== this.root() || this.revoked().includes(this.session.peer))) { this.close(); this.state("The link closed: this browser's identity changed, or the other device was revoked."); }
    if (local) { this.prune(); this.render(); }
  },
  changed() { clearTimeout(this.timer); this.timer = setTimeout(() => { if (this.session) this.session.push(false).catch(() => {}); }, 1000); },
  close() { if (this.link) this.link.close(); this.link = null; this.session = null; },
  handlers(link) {
    return {
      onopen: (id) => this.begin(link, id),
      onmessage: (m) => { if (this.link === link && this.session) this.session.receive(m).catch((e) => this.state(esc(e.message), true)); },
      onclose: () => { if (this.link !== link) return; this.link = null; this.session = null; this.state("The link closed. What came over it is kept here."); },
    };
  },
  begin(link, id) {
    if (this.link !== link) return;
    this.session = new PyrtanSync.Session({key: ident.key, revoked: () => this.revoked(), own: () => local.own(), held: () => this.held(), take: (b) => this.take(b), send: (m) => link.send(m), note: (e, d) => this.noted(e, d)});
    this.state("Linked. Checking the other device is yours…");
    this.session.start(id).catch((e) => this.state(esc(e.message), true));
  },
  open() {
    this.close();
    const link = new PyrtanSync.Link();
    link.on = this.handlers(link);
    this.link = link;
    return link;
  },
  // A link that paired a device goes on to share gaps.
  adopt(link) {
    this.close();
    if (!local || !this.ready()) { link.close(); return; }
    this.link = link; link.on = this.handlers(link);
    this.begin(link, link.id);
  },
  noted(event, detail) {
    if (event === "linked") { this.step(""); this.state(`Linked with <code>${esc(deviceName(detail))}</code>, a device of yours. New gaps go both ways while both pages are open.`); }
    if (event === "refused") { this.state(`Not linked: ${esc(detail)}.`, true); this.close(); }
    if (event === "took") this.took(detail);
  },
  took(r) {
    const from = [...new Set(r.from)].map((d) => `<code>${esc(deviceName(d))}</code>`).join(", ");
    const refused = r.refused.length ? ` Refused ${r.refused.length}: ${esc([...new Set(r.refused)].join("; "))}.` : "";
    if (r.took || refused) this.state((r.took ? `Took the newest gaps of ${from}.` : "Nothing newer.") + refused, !!refused && !r.took);
    if (r.took) { this.render(); if (!$("#tab-gaps").hidden) loadGaps(); }
  },
  codeBlock(code, offer) { return codeBlock(code, offer ? "sync" : null).replace("<textarea readonly", '<textarea id="sync-code" readonly'); },
  async offer() {
    if (!this.ready()) return;
    const link = this.open();
    this.step('<p class="mute">Making a code…</p>'); this.state("");
    try {
      const code = await link.offer();
      this.step(`<p>On your other device, open this console's Gaps tab and press <em>I have a code</em>; paste this there${/^https?:$/.test(location.protocol) ? ", or point its camera at the QR" : ""}. Both devices need to be on the same network.</p>${this.codeBlock(code, true)}
        <form class="row" id="sync-accept" style="margin-top:12px"><input type="text" id="sync-answer" placeholder="then paste the code it gives back" autocomplete="off"><button type="submit">Connect</button></form>`);
    } catch (e) { this.step(""); this.state(`Could not make a code: ${esc(e.message)}`, true); }
  },
  have() {
    if (!this.ready()) return;
    this.step(`<form id="sync-answer-form" style="margin-top:10px"><textarea id="sync-offer-code" rows="4" style="width:100%" placeholder="the code your other device shows"></textarea><div class="row" style="margin-top:8px"><button type="submit">Answer it</button></div></form>`);
    $("#sync-offer-code").focus();
  },
  async answer(code) {
    if (!local) { problem("A link between your devices needs this browser to be its own hub: forget the hub it joined, then open the link again.", true); return; }
    if (!this.ready()) { this.render(); this.state("This browser holds no key of your identity yet, so it cannot answer the link: pair it in the Identity tab, then open the link again.", true); return; }
    const link = this.open();
    this.step('<p class="mute">Answering…</p>'); this.state("");
    try {
      const back = await link.answer(code);
      this.step(`<p>Paste this on the device that showed you the code${"BarcodeDetector" in window ? ", or scan it there" : ""}. The link opens when it does.</p>${this.codeBlock(back, false)}`);
    } catch (e) { this.close(); this.step(""); this.state(`Could not answer: ${esc(e.message)}`, true); }
  },
  async accept(code) {
    try { await this.link.accept(code); this.state("Connecting…"); }
    catch (e) { this.state(`Could not connect: ${esc(e.message)}`, true); }
  },
  // A code by camera, where the browser can read one: an offer is answered, an answer opens the link this page offered.
  async scan() {
    const code = await scanCode($("#sync-step"), (m) => this.state(m, true));
    if (!code) return;
    let d; try { d = await PyrtanSync.decodeCode(code); } catch (e) { this.state(esc(e.message), true); return; }
    if (d.kind === "o") return this.answer(code);
    if (this.link) { this.step(""); return this.accept(code); }
    this.state("That is an answer, and this page made no offer: press Link a device first.", true);
  },
  stopScan() { stopScanning(); },
  async exportFile() {
    const f = await PyrtanSync.makeFile(ident.key, local.own(), this.held());
    saveFile(`pyrtan-gaps-${new Date().toISOString().slice(0, 10)}.json`, f);
    this.state(`Saved the gaps of this device${f.bundles.length > 1 ? ` and ${f.bundles.length - 1} other${f.bundles.length > 2 ? "s" : ""}` : ""}, signed. Load the file on another device of yours.`);
  },
  async importFile(file) {
    try {
      const f = JSON.parse(await file.text());
      this.took(await PyrtanSync.readFile(f, ident.key, () => this.revoked(), (b) => this.take(b)));
      if (!$("#sync-state").textContent) this.state("Nothing newer in that file.");
    } catch (e) { this.state(`Could not load it: ${esc(e.message)}`, true); }
  },
};
$("#sync-offer").addEventListener("click", () => sync.offer());
$("#sync-have").addEventListener("click", () => sync.have());
$("#sync-scan").addEventListener("click", () => sync.scan());
$("#sync-export").addEventListener("click", () => sync.exportFile().catch((e) => sync.state(esc(e.message), true)));
$("#sync-import").addEventListener("click", () => $("#sync-file").click());
$("#sync-file").addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) { sync.state(""); sync.importFile(f); } });
$("#sync-step").addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.target.id === "sync-accept") sync.accept($("#sync-answer").value);
  if (e.target.id === "sync-answer-form") sync.answer($("#sync-offer-code").value);
});
$("#sync-step").addEventListener("click", (e) => {
  if (e.target.id === "sync-scan-stop") sync.step("");
});

// A link code by camera, into el, where the browser can read QR codes: the code, or null when stopped or refused.
let scanning = null;
function stopScanning() { const s = scanning; scanning = null; if (s) s.getTracks().forEach((t) => t.stop()); }
async function scanCode(el, fail) {
  stopScanning();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({video: {facingMode: "environment"}}); }
  catch (e) { fail(`No camera: ${esc(e.message)}`); return null; }
  el.innerHTML = '<p class="mute">Point the camera at the other device\'s code.</p><video playsinline muted></video><div><button type="button" class="quiet" data-scan-stop>Stop</button></div>';
  el.querySelector("[data-scan-stop]").addEventListener("click", () => { stopScanning(); el.innerHTML = ""; });
  const video = el.querySelector("video"); video.srcObject = stream; await video.play();
  scanning = stream;
  const detector = new BarcodeDetector({formats: ["qr_code"]});
  while (scanning === stream) {
    let found = null;
    try { found = (await detector.detect(video)).find((c) => /pyrgaps1\./.test(c.rawValue)); } catch (e) {}
    if (found) { stopScanning(); el.innerHTML = ""; return found.rawValue; }
    await new Promise((r) => setTimeout(r, 250));
  }
  return null;
}
// A code to carry, as text and a QR. An offer's QR is this page's address when it has one (with #sync= or #pair=), so a phone's camera opens it here.
function codeBlock(code, hash) {
  const scan = hash && /^https?:$/.test(location.protocol) ? `${location.origin}${location.pathname}#${hash}=${code}` : code;
  let qr = ""; try { qr = PyrtanSync.qrSvg(scan); } catch (e) {}
  return `<div class="sync-code">${qr ? `<div class="qr">${qr}</div>` : ""}<textarea readonly rows="6" data-code>${esc(code)}</textarea></div><button type="button" class="quiet" data-copy>Copy the code</button>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]"); if (!b) return;
  const t = b.parentElement.querySelector("textarea[data-code]"); t.select();
  try { navigator.clipboard.writeText(t.value); } catch (err) { document.execCommand("copy"); }
  b.textContent = "Copied";
});

// ---------------------------------------------------------------- pairing a device by QR
// The root offers a link (a QR whose address is this console with #pair=);
// the new browser makes its key, answers, and asks over the link, signed
// for it; the root shows six digits both screens show and signs the note
// only when the person says they match; then the link shares gaps (sync.js).
const pair = {
  link: null, pairing: null, pending: [],
  step(html) { stopScanning(); $("#pair-step").innerHTML = html || ""; },
  state(html, bad) { $("#pair-state").innerHTML = html ? `<span class="${bad ? "failed" : "mute"}">${html}</span>` : ""; },
  label() { return /Mobi|Android|iPhone|iPad/.test(navigator.userAgent) ? "phone" : "browser"; },
  close() { if (this.link) this.link.close(); this.link = null; this.pairing = null; },
  open(role) {
    this.close();
    this.pending = [];
    const link = new PyrtanSync.Link();
    link.on = {
      onopen: (id) => {
        if (this.link !== link) return;
        this.pairing = new PyrtanSync.Pairing({key: ident.key, role, label: this.label(), send: (m) => link.send(m), note: (e, d) => this.noted(e, d),
          sign: async (device, may, label) => ({note: await ident.addDevice(device, may, label), document: ident.key.document})});
        this.pairing.start(id).catch((e) => this.state(esc(e.message), true));
      },
      onmessage: (m) => {
        if (this.link !== link || !this.pairing) return;
        // The root goes on to share gaps as soon as it has signed: what it sends meanwhile waits for this side to catch up.
        if (this.pairing.handles(m)) this.pairing.receive(m).catch((e) => this.state(esc(e.message), true));
        else if (this.pending.length < 20) this.pending.push(m);
      },
      onclose: () => { if (this.link !== link) return; this.link = null; if (this.pairing && !this.pairing.done) this.state("The link closed before the pairing was done.", true); this.pairing = null; },
    };
    this.link = link;
    return link;
  },
  async noted(event, d) {
    if (event === "waiting") {
      this.step(`<p>Linked. Your identity's browser now asks whether to pair this one. Check that it shows <strong class="pair-words">${esc(d)}</strong>, the digits shown here.</p>`);
      this.state("");
    }
    if (event === "request") {
      const [ask, chat] = ["ask", "chat"];
      this.step(`<form id="pair-confirm" style="margin-top:10px">
        <p>Pair <code>${esc(PyrtanIdentity.short(d.device))}</code> as a device of yours? Only if the other screen shows the same digits: <strong class="pair-words">${esc(d.words)}</strong></p>
        <div class="row"><input type="text" id="pair-label" value="${esc(d.label || "phone")}" placeholder="what to call it" style="max-width:220px" autocomplete="off"></div>
        <div style="display:flex;gap:14px;flex-wrap:wrap;margin:8px 0;font-size:14px;align-items:center" id="pair-may"><span class="mute">it may</span>
          <label><input type="checkbox" value="${ask}" checked> ask</label><label><input type="checkbox" value="${chat}" checked> chat</label><label><input type="checkbox" value="run"> run trees</label><label><input type="checkbox" value="operate"> operate hubs</label></div>
        <div class="row"><button type="submit">They match: pair it</button><button type="button" class="quiet" id="pair-refuse">They do not match</button></div></form>`);
    }
    if (event === "paired") {
      ident.key.note = d.note;
      await ident.keep();
      this.done(`Paired: this browser now speaks for <code>${esc(PyrtanIdentity.short(d.note.root))}</code>, as <em>${esc(d.note.label || "a device")}</em>.`);
    }
    if (event === "refused") { this.step(""); this.state(`Not paired: ${esc(d)}.`, true); this.close(); }
  },
  async approve() {
    const may = [...document.querySelectorAll("#pair-may input:checked")].map((i) => i.value);
    if (!may.length) { this.state("Choose at least one thing it may do.", true); return; }
    try {
      const {note} = await this.pairing.approve(may, $("#pair-label").value.trim());
      this.done(`Paired <code>${esc(PyrtanIdentity.short(note.device))}</code> as <em>${esc(note.label || "a device")}</em>. Give a hub your newest document (below) for it to know.`);
    } catch (e) { this.state(`Could not pair it: ${esc(e.message)}`, true); }
  },
  // Paired: the same link goes on to share gaps, when this browser is its own hub.
  done(html) {
    const link = this.link; this.link = null; this.pairing = null;
    this.step("");
    this.state(html + (local ? " Now sharing gaps with it over the same link (the Gaps tab)." : ""));
    const pending = this.pending; this.pending = [];
    if (link && local) {
      sync.adopt(link);
      for (const m of pending) if (sync.session) sync.session.receive(m).catch((e) => sync.state(esc(e.message), true));
    } else if (link) link.close();
  },
  async offer() {
    if (!ident.isRoot()) return;
    const link = this.open("root");
    this.step('<p class="mute">Making a code…</p>'); this.state("");
    try {
      const code = await link.offer();
      this.step(`<p>On the new device, point its camera at this code${/^https?:$/.test(location.protocol) ? "" : " (or paste the code in its Identity tab, under <em>I have a pairing code</em>)"}. Both need to be on the same network.</p>${codeBlock(code, "pair")}
        <form class="row" id="pair-accept" style="margin-top:12px"><input type="text" id="pair-answer" placeholder="then paste the code it gives back" autocomplete="off"><button type="submit">Connect</button>${"BarcodeDetector" in window ? '<button type="button" class="quiet" id="pair-scan">Scan it</button>' : ""}</form>`);
    } catch (e) { this.step(""); this.state(`Could not make a code: ${esc(e.message)}`, true); }
  },
  async accept(code) {
    try { await this.link.accept(code); this.state("Connecting…"); }
    catch (e) { this.state(`Could not connect: ${esc(e.message)}`, true); }
  },
  have() {
    this.step(`<form id="pair-answer-form" style="margin-top:10px"><textarea id="pair-offer-code" rows="4" style="width:100%" placeholder="the code your identity's browser shows"></textarea><div class="row" style="margin-top:8px"><button type="submit">Answer it</button></div></form>`);
    $("#pair-offer-code").focus();
  },
  // The new device: a key of its own (made now if it has none), then the answer to carry back.
  async answer(code) {
    if (!ident.ok) { this.state("This browser cannot hold a key, so it cannot be paired.", true); return; }
    if (ident.key && (ident.isRoot() || ident.key.note)) { this.state(`This browser already speaks for an identity (${esc(PyrtanIdentity.short(PyrtanSync.rootOf(ident.key)))}); forget its key first to pair it with another.`, true); return; }
    if (!ident.key) await ident.make();
    const link = this.open("device");
    this.step('<p class="mute">Answering…</p>'); this.state("");
    try {
      const back = await link.answer(code);
      this.step(`<p>Paste this on the browser that holds your identity, or let it scan the code. The link opens when it does.</p>${codeBlock(back, null)}`);
    } catch (e) { this.close(); this.step(""); this.state(`Could not answer: ${esc(e.message)}`, true); }
  },
};
$("#pair-offer").addEventListener("click", () => pair.offer());
for (const id of ["#pair-have", "#pair-have-device"]) $(id).addEventListener("click", () => pair.have());
$("#pair-step").addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.target.id === "pair-accept") pair.accept($("#pair-answer").value);
  if (e.target.id === "pair-answer-form") pair.answer($("#pair-offer-code").value);
  if (e.target.id === "pair-confirm") pair.approve();
});
$("#pair-step").addEventListener("click", async (e) => {
  if (e.target.id === "pair-refuse") pair.pairing && pair.pairing.refuse("the digits did not match");
  if (e.target.id === "pair-scan") { const c = await scanCode($("#pair-step-scan"), (m) => pair.state(m, true)); if (c) pair.accept(c); }
});

$("#gaps-forget").addEventListener("click", () => {
  if (!local || !confirm("Forget the gaps and unanswered sentences this browser kept?")) return;
  local.forget(); loadGaps();
});

async function loadUnanswered() {
  let u; try { u = await json("unanswered"); } catch (e) { problem(e.message, true); return; }
  const body = $("#unanswered tbody"); body.innerHTML = "";
  for (const e of u.unanswered || []) {
    const p = e.proposal || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(e.text)} <span class="mute">×${esc(e.count||1)}${e.peer ? " · " + esc(e.peer) : e.device && local && e.device !== local.device ? " · " + esc(whose(e.device)) : ""}</span></td><td><code>${esc(p.name||"")}</code><div class="mute"><code>${esc(p.trigger||"")}</code></div></td><td><code>${esc(p.scaffold||"")}</code>${local ? "" : `<div style="margin-top:6px"><button type="button" class="quiet build" data-text="${esc(e.text)}" data-name="${esc(p.name||"")}">Build it here</button></div>`}</td>`;
    body.appendChild(tr);
  }
  if (!(u.unanswered || []).length) body.innerHTML = `<tr><td colspan="3" class="mute">Nothing yet: every sentence so far found a tree.</td></tr>`;
}
$("#unanswered tbody").addEventListener("click", (e) => { const b = e.target.closest("button.build"); if (b) builder.open(b.dataset.text, b.dataset.name); });

// ---------------------------------------------------------------- the builder
// POST /build answers with the questions that apply so far (each with its
// default and the answer taken) and the file those answers describe,
// checked. The form is rebuilt only when the set of questions changes
// (the source changed); otherwise the preview is refreshed in place.
const builder = {
  text: "", name: "", answers: {}, keys: "", built: null,
  open(text, name) {
    this.text = text; this.name = name || ""; this.answers = {}; this.keys = ""; this.built = null;
    $("#builder-form").innerHTML = ""; $("#builder-toml").textContent = ""; $("#builder-problems").innerHTML = "";
    $("#builder").hidden = false;
    showTab("unanswered");
    this.refresh().then(() => $("#builder").scrollIntoView({behavior: "smooth", block: "start"}));
  },
  close() { $("#builder").hidden = true; },
  async refresh() {
    let r;
    try { r = await json("build", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: this.text, name: this.name || undefined, answers: this.answers})}); }
    catch (e) { problem(e.message, true); return; }
    for (const q of document.querySelectorAll("#builder-form .build-q")) { q.classList.remove("bad"); const m = q.querySelector(".bad-say"); if (m) m.remove(); }
    if (r.status === "error" && r.reason === "bad-answer") {
      const q = document.querySelector(`#builder-form .build-q[data-key="${CSS.escape(r.key)}"]`);
      if (q) { q.classList.add("bad"); const m = document.createElement("div"); m.className = "bad-say"; m.textContent = r.say; q.appendChild(m); q.querySelector("input,select").focus(); }
      else problem(r.say, true);
      return;
    }
    if (r.status !== "ok") { problem(r.say || r.reason || "The builder did not answer.", true); return; }
    this.built = r;
    $("#builder-title").textContent = `Build ${r.name}`;
    const keys = r.questions.map((q) => q.key).join(",");
    if (keys !== this.keys) { this.keys = keys; this.renderForm(r.questions); }
    else for (const q of r.questions) { const el = document.querySelector(`#builder-form [name="${CSS.escape(q.key)}"]`); if (el && !(q.key in this.answers)) el.value = q.default; }
    const probs = r.problems || [];
    $("#builder-problems").innerHTML = probs.length ? `<ul class="problems">${probs.map((p) => `<li class="${esc(p.level)}"><b>${esc(p.level)}</b> ${esc(p.where)}: ${esc(p.message)}</li>`).join("")}</ul>` : `<div class="mute" style="font-size:14px">Passes <code>pyrtan check</code>.</div>`;
    $("#builder-toml").textContent = r.toml;
    $("#builder-download").textContent = `Download ${r.name}.toml`;
  },
  renderForm(questions) {
    const form = $("#builder-form"); form.innerHTML = "";
    for (const q of questions) {
      const div = document.createElement("div"); div.className = "build-q"; div.dataset.key = q.key;
      const value = q.key in this.answers ? this.answers[q.key] : q.default;
      let field;
      if (q.choices) field = `<select name="${esc(q.key)}">${q.choices.map((c) => `<option value="${esc(c.value)}"${c.value === value ? " selected" : ""}>${esc(c.value)}: ${esc(c.label)}</option>`).join("")}</select>`;
      else field = `<input type="text" name="${esc(q.key)}" value="${esc(value)}" placeholder="${esc(q.default)}" autocomplete="off" spellcheck="false">`;
      div.innerHTML = `<label>${esc(q.prompt)}</label>${q.help ? `<div class="help">${esc(q.help)}</div>` : ""}${field}`;
      form.appendChild(div);
    }
  },
  download() {
    if (!this.built) return;
    const blob = new Blob([this.built.toml], {type: "application/toml"});
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${this.built.name}.toml`;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  },
};
$("#builder-form").addEventListener("change", (e) => {
  const el = e.target; if (!el.name) return;
  if (el.value === "" || (el.tagName === "INPUT" && el.value === el.placeholder)) delete builder.answers[el.name]; else builder.answers[el.name] = el.value;
  builder.refresh();
});
$("#builder-form").addEventListener("submit", (e) => e.preventDefault());
$("#builder-download").addEventListener("click", () => builder.download());
$("#builder-copy").addEventListener("click", async () => { if (!builder.built) return; try { await navigator.clipboard.writeText(builder.built.toml); $("#builder-copy").textContent = "Copied"; setTimeout(() => { $("#builder-copy").textContent = "Copy"; }, 1500); } catch (e) { problem("Could not copy: " + e.message, true); } });
$("#builder-close").addEventListener("click", () => builder.close());

function renderTrace(tr) {
  if (!tr || !tr.steps) return "";
  const lines = tr.steps.map(s => `${"  ".repeat((s.depth||0)+1)}${s.node} (${s.kind})${s.chosen ? " [" + s.chosen + "]" : ""}: ${s.summary}${s.next ? " -> " + s.next : ""}`);
  for (const g of tr.gaps || []) lines.push(`  gap: ${g.node} saw ${g.on} = ${JSON.stringify(g.observed)} and had no branch for it`);
  return `trace: ${tr.tree}\n` + lines.join("\n");
}

async function ask(snapshot, answer) {
  const text = $("#text").value.trim(); if (!text && !snapshot) return;
  if (!snapshot) { suggest.remember(text); suggest.close(); }
  $("#go").disabled = true;
  const t0 = performance.now();
  let r;
  try {
    const res = snapshot ? await api("resume", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({snapshot, answer})})
                         : await api("intent", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text})});
    r = res.status === 401 ? {status:"error", reason:"unauthorised", say:"This Pyrtan needs a token."} : await res.json();
  } catch (e) { r = {status:"error", reason:"unreachable", say:String(e)}; }
  const ms = Math.round(performance.now() - t0);
  const out = $("#out");
  const card = document.createElement("div"); card.className = "card";
  let html = `<div><span class="status ${esc(r.status)}">${esc(r.status)}</span>${r.reason ? ` <span class="mute">(${esc(r.reason)})</span>` : ""} <span class="mute">· ${esc(r.tree||"")} · ${ms} ms${r.peer ? " · " + esc(r.peer) : ""}${local ? " · ran in this browser" : ""}</span></div>`;
  if (r.say) html += `<div class="say">${esc(r.say)}</div>`;
  if (r.output && r.output.reading && r.output.reading.steps && r.output.reading.steps.length) html += `<div class="mute" style="font-size:14px">Read as <code>${esc(r.output.reading.cleaned)}</code>: ${r.output.reading.steps.map(esc).join("; ")}</div>`;
  if (r.reason === "no-match" && r.output && r.output.proposal) {
    const p = r.output.proposal;
    html += `<div class="gap">No tree answers this yet. The one that would: <code>${esc(p.name)}</code>, answering <code>${esc(p.trigger)}</code>. It is listed under <a href="#unanswered">Unanswered</a>.</div><pre>${esc(p.scaffold)}</pre>`;
    html += local ? `<div class="mute" style="font-size:14px;margin-top:8px">To be asked what it reads, decides and says, and get the file: <code>pyrtan build --from ${esc(JSON.stringify(r.output.unanswered || text))}</code> on a box, or join a hub above and build it here.</div>`
                  : `<div style="margin-top:8px"><button type="button" class="quiet build-here">Build it here</button> <span class="mute" style="font-size:14px">a few questions: what it reads, decides and says; the file comes out checked.</span></div>`;
  }
  if (r.status === "asked") html += `<form class="row resume"><input type="text" placeholder="${esc(r.question||"")}" autofocus><button>Answer</button></form>`;
  if (r.output != null) html += `<details open><summary>output</summary><pre>${esc(JSON.stringify(r.output, null, 2))}</pre></details>`;
  if (r.trace) html += `<details open><summary>trace</summary><pre>${esc(renderTrace(r.trace))}</pre></details>`;
  if (local && (r.reason === "source-unsupported" || (r.reason === "transport" && /may not let a page/.test(r.say || "")))) html += `<div class="mute" style="font-size:14px;margin-top:8px">This one needs a hub: a page cannot reach it. Join a hub above that runs <code>${esc(r.tree||"")}</code>, and the same sentence goes there.</div>`;
  if (r.reason === "gap" && r.output && r.output.gap) html += `<div class="mute" style="font-size:14px;margin-top:8px">This run met a gap. It is listed under <a href="#gaps">Gaps</a>; the branch to write is in <code>${esc(r.output.gap.tree)}</code>, node <code>${esc(r.output.gap.node)}</code>.</div>`;
  card.innerHTML = html;
  out.prepend(card);
  const rf = card.querySelector("form.resume");
  if (rf) rf.addEventListener("submit", (e) => { e.preventDefault(); ask(r.snapshot, rf.querySelector("input").value); });
  const bh = card.querySelector("button.build-here");
  if (bh) bh.addEventListener("click", () => builder.open(r.output.unanswered || text, r.output.proposal.name));
  if (r.reason === "no-match") loadUnanswered();
  $("#go").disabled = false;
}

$("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask(); });

// ---------------------------------------------------------------- suggestions in the ask box
// The sentences this swarm's trees answer to, and the ones asked in this
// browser before. The best that starts with what is typed shows greyed after
// it (Tab or → takes it; Tab in the empty box takes the placeholder), and up to
// eight drop down under the box: ↑ ↓ pick, Enter asks, Esc closes. A sentence
// with a blank ("latest release of …") fills the box up to the blank instead of asking.
const ASKED_KEY = "pyrtan.console.asked";
const suggest = {
  pool: [], items: [], active: -1, typed: "", open: false,
  input: $("#text"), list: $("#suggestions"),
  recent() { try { const r = JSON.parse(localStorage.getItem(ASKED_KEY) || "[]"); return Array.isArray(r) ? r.filter(s => typeof s === "string") : []; } catch (e) { return []; } },
  remember(text) {
    const r = [text, ...this.recent().filter(s => s.toLowerCase() !== text.toLowerCase())].slice(0, 20);
    try { localStorage.setItem(ASKED_KEY, JSON.stringify(r)); } catch (e) {}
  },
  from(trees) {
    const seen = new Set(); this.pool = [];
    for (const t of trees) for (const text of samples(t)) { const k = text.toLowerCase(); if (!seen.has(k)) { seen.add(k); this.pool.push({text, tree: t.name}); } }
  },
  // Recent first, then the trees': those that start with what is typed, then those with every typed word starting one of theirs.
  match(q) {
    const low = q.toLowerCase(), words = low.split(/\s+/).filter(Boolean), seen = new Set(), out = [];
    const add = (e) => { const k = e.text.toLowerCase(); if (!seen.has(k) && out.length < 8) { seen.add(k); out.push(e); } };
    const all = [...this.recent().map(text => ({text, recent: true})), ...this.pool];
    for (const e of all) if (e.text.toLowerCase().startsWith(low)) add(e);
    for (const e of all) { const ws = e.text.toLowerCase().split(/\s+/); if (words.every(w => ws.some(x => x.startsWith(w)))) add(e); }
    return out;
  },
  fill(e) { const i = e.text.indexOf("…"); return i < 0 ? e.text : e.text.slice(0, i); },
  // The rest of the first suggestion that starts with what is typed, while the caret is at the end.
  completion() {
    const el = this.input, v = el.value, low = v.toLowerCase();
    if (!v || document.activeElement !== el || el.selectionStart !== v.length || el.selectionEnd !== v.length) return "";
    const e = this.items.find(e => { const f = this.fill(e); return f.length > v.length && f.toLowerCase().startsWith(low); });
    return e ? this.fill(e).slice(v.length) : "";
  },
  ghost() {
    const rest = this.input.scrollWidth <= this.input.clientWidth ? this.completion() : "";  // scrolled text would not line up
    $("#ghost .typed").textContent = rest ? this.input.value : "";
    $("#ghost .rest").textContent = rest;
  },
  render() {
    const v = this.typed, low = v.toLowerCase();
    this.list.innerHTML = "";
    this.items.forEach((e, i) => {
      const li = document.createElement("li");
      li.id = "suggestion-" + i; li.setAttribute("role", "option"); li.setAttribute("aria-selected", String(i === this.active));
      const head = v && e.text.toLowerCase().startsWith(low) ? v.length : v ? 0 : e.text.length;  // what is typed plain, the rest bold
      li.innerHTML = `<span class="text">${esc(e.text.slice(0, head))}<b>${esc(e.text.slice(head))}</b></span><span class="from">${e.recent ? "recent" : esc(e.tree)}</span>`;
      li.addEventListener("click", () => this.choose(e));
      this.list.appendChild(li);
    });
    const shown = this.open && this.items.length > 0;
    this.list.hidden = !shown; this.input.setAttribute("aria-expanded", String(shown));
    if (shown && this.active >= 0) this.input.setAttribute("aria-activedescendant", "suggestion-" + this.active); else this.input.removeAttribute("aria-activedescendant");
  },
  refresh() {
    this.typed = this.input.value; this.active = -1;
    this.items = this.open ? this.match(this.typed) : [];
    this.render(); this.ghost();
  },
  close() { this.open = false; this.refresh(); },
  choose(e) {
    this.input.value = this.fill(e); this.input.focus();
    if (e.text.includes("…")) { this.open = true; this.refresh(); } else ask();
  },
  key(ev) {
    if (ev.isComposing || ev.ctrlKey || ev.altKey || ev.metaKey) return;
    const shown = !this.list.hidden;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!shown) { this.open = true; this.refresh(); return; }
      // -1 is what was typed: the list wraps through it, as a search box's does.
      const n = this.items.length + 1;
      this.active = ((this.active + 1 + (ev.key === "ArrowDown" ? 1 : -1)) % n + n) % n - 1;
      this.input.value = this.active < 0 ? this.typed : this.fill(this.items[this.active]);
      this.render(); this.ghost();
    } else if ((ev.key === "Tab" && !ev.shiftKey) || ev.key === "ArrowRight") {
      const v = this.input.value, rest = this.completion();
      const take = rest ? v + rest : ev.key === "Tab" && !v ? this.input.placeholder : "";
      if (!take) return;  // nothing to complete: Tab moves on, → moves the caret
      ev.preventDefault(); this.input.value = take; this.open = true; this.refresh();
    } else if (ev.key === "Escape" && shown) {
      ev.preventDefault(); this.input.value = this.typed; this.close();
    } else if (ev.key === "Enter" && shown && this.active >= 0 && this.items[this.active].text.includes("…")) {
      ev.preventDefault(); this.choose(this.items[this.active]);
    }
  },
};
suggest.input.addEventListener("input", () => { suggest.open = true; suggest.refresh(); });
suggest.input.addEventListener("keydown", (e) => suggest.key(e));
suggest.input.addEventListener("keyup", () => suggest.ghost());
suggest.input.addEventListener("mousedown", () => { if (!suggest.open) { suggest.open = true; suggest.refresh(); } });
suggest.input.addEventListener("click", () => suggest.ghost());
suggest.input.addEventListener("focus", () => suggest.ghost());
suggest.input.addEventListener("blur", () => suggest.close());
suggest.list.addEventListener("mousedown", (e) => e.preventDefault());  // keep the box focused, so the click lands
window.addEventListener("resize", () => suggest.ghost());
$("#hubs").addEventListener("click", async (e) => {
  // A follower the operator lets go (a lost phone): it is no longer known here, and what was kept for it goes.
  const a = e.target.closest("a[data-drop]"); if (!a) return; e.preventDefault();
  if (!confirm(`Let ${a.dataset.name} go? It stops following this hub, and what the hub handed or kept for it is deleted.`)) return;
  try {
    const r = await (await api("drop", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({follower: a.dataset.drop})})).json();
    problem(r.status === "ok" ? "" : (r.say || r.reason), r.status !== "ok");
  } catch (err) { problem(err.message, true); }
  loadPeers();
});
$("#tries").addEventListener("click", (e) => { const a = e.target.closest("a[data-q]"); if (!a) return; e.preventDefault(); $("#text").value = a.dataset.q; location.hash = "#ask"; ask(); });
$("#connect-form").addEventListener("submit", (e) => {
  e.preventDefault();
  agentsUI.putAway();
  token = $("#hub-token").value;
  store.set(remote ? {url: $("#hub-url").value.trim(), token} : {token});
  load();
});
// ---------------------------------------------------------------- member
// A browser as a federation actor without an inbox. Everything it sends is
// signed the way hubs sign: HMAC-SHA256 over method, path, date and the
// SHA-256 of the canonical JSON body. HMAC is written out here (and SHA-256
// in identity.js) because crypto.subtle is not available on a plain http://
// LAN address. With a key (the Identity tab), it follows as that key.
const {sha256, canonical} = PyrtanIdentity;
const enc = new TextEncoder();
const hex = (b) => Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
function hmac(keyText, msgBytes) {
  let key = enc.encode(keyText); if (key.length > 64) key = sha256(key);
  const k = new Uint8Array(64); k.set(key);
  const ipad = k.map(x => x ^ 0x36), opad = k.map(x => x ^ 0x5c);
  const inner = sha256(new Uint8Array([...ipad, ...msgBytes]));
  return hex(sha256(new Uint8Array([...opad, ...inner])));
}
function signHeaders(secret, actor, method, path, body) {
  const date = String(Math.floor(Date.now() / 1000));
  const digest = hex(sha256(enc.encode(body === null || body === undefined ? "" : canonical(body))));
  const mac = hmac(secret, enc.encode([method.toUpperCase(), path, date, digest].join("\n")));
  return {"X-Pyrtan-Actor": actor, "X-Pyrtan-Date": date, "X-Pyrtan-Signature": mac};
}
const randomHex = (n) => hex(crypto.getRandomValues(new Uint8Array(n)));

const member = {
  hub: null, me: null, timer: null, log: [], directory: new Map(), db: null, runner: null, runs: 0,
  key() { return "pyrtan.member:" + (this.hub || ""); },
  load() { try { this.me = JSON.parse(localStorage.getItem(this.key()) || "null"); } catch (e) { this.me = null; } },
  save() { try { if (this.me) localStorage.setItem(this.key(), JSON.stringify(this.me)); else localStorage.removeItem(this.key()); } catch (e) {} },
  note(line) { this.log.unshift(new Date().toLocaleTimeString() + "  " + line); this.log = this.log.slice(0, 40); const el = $("#member-log"); if (el) el.textContent = this.log.join("\n"); },
  async open() {
    if (this.db) return this.db;
    this.db = await new Promise((res, rej) => { const r = indexedDB.open("pyrtan-member", 1); r.onupgradeneeded = () => r.result.createObjectStore("kept", {keyPath: "id"}); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
    return this.db;
  },
  async keptAll() { const db = await this.open(); return new Promise((res, rej) => { const r = db.transaction("kept").objectStore("kept").getAll(); r.onsuccess = () => res(r.result.filter(b => b.hub === this.hub)); r.onerror = () => rej(r.error); }); },
  async keptGet(key) { const db = await this.open(); return new Promise((res, rej) => { const r = db.transaction("kept").objectStore("kept").get(this.hub + "|" + key); r.onsuccess = () => res(r.result || null); r.onerror = () => rej(r.error); }); },
  async keptPut(blob) { const db = await this.open(); return new Promise((res, rej) => { const t = db.transaction("kept", "readwrite"); t.objectStore("kept").put({...blob, id: this.hub + "|" + blob.key, hub: this.hub}); t.oncomplete = () => res(); t.onerror = () => rej(t.error); }); },
  async keptClear() { const all = await this.keptAll(); const db = await this.open(); await new Promise((res) => { const t = db.transaction("kept", "readwrite"); for (const b of all) t.objectStore("kept").delete(b.id); t.oncomplete = () => res(); }); },
  async send(method, path, body) {
    if (!this.me) throw new Error("not a member");
    const headers = Object.assign({"Accept": "application/json"}, signHeaders(this.me.secret, this.me.id, method, path, body === undefined ? null : body));
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const r = await fetch((base ? base.replace(/\/+$/, "") : "") + path, {method, headers, body: body === undefined ? undefined : canonical(body)});
    let data = null; try { data = await r.json(); } catch (e) {}
    return {status: r.status, data};
  },
  activity(type, object, extra) { return Object.assign({type, id: this.me.id + "/activities/" + randomHex(8), actor: this.me.id, published: new Date().toISOString().replace(/\.\d+Z$/, "Z"), object}, extra || {}); },
  async join(name, fedSecret) {
    const keyed = ident.active();
    this.me = {id: keyed ? ident.key.id : "urn:pyrtan:member:" + randomHex(16), secret: randomHex(32), name: name || "browser", state: "pending", listed: true, joined: Date.now(), trusted: false};
    this.save();
    const act = this.activity("Follow", this.hub, {secret: this.me.secret, inbox: "mailbox", name: this.me.name});
    const headers = Object.assign({"Accept": "application/json", "Content-Type": "application/json"}, signHeaders(fedSecret || this.me.secret, this.me.id, "POST", "/inbox", act));
    if (keyed) Object.assign(headers, await ident.headers("POST", "/inbox", act));  // a hub that grants this key, or its root, run makes it trusted
    if (token) headers["Authorization"] = "Bearer " + token;  // the operator's token makes this device trusted: it runs the hub's trees
    const r = await fetch((base ? base.replace(/\/+$/, "") : "") + "/inbox", {method: "POST", headers, body: canonical(act)});
    const data = await r.json().catch(() => ({}));
    if (r.status !== 202) { this.me = null; this.save(); this.note(`follow refused: ${data.say || r.status}`); problem(`The hub did not take the follow: ${data.say || r.status}`, true); this.render(); return; }
    this.me.trusted = !!data.trusted; this.me.identity = data.identity || null; this.save();
    this.note(`followed ${this.hub}` + (this.me.trusted ? " as a trusted device" : ""));
    await this.poll();
    this.start();
    this.render();
  },
  async leave() {
    try { await this.send("POST", "/inbox", this.activity("Undo", {type: "Follow", actor: this.me.id, object: this.hub})); } catch (e) {}
    await this.keptClear();
    this.me = null; this.save(); this.stop(); this.note("left"); this.render();
  },
  async poll() {
    if (!this.me) return;
    let r; try { r = await this.send("GET", "/mailbox"); } catch (e) { this.note("poll failed: " + e.message); return; }
    if (r.status === 401) { this.note("the hub no longer knows this member"); this.me.state = "refused"; this.save(); this.render(); return; }
    this.me.polled = Date.now();
    for (const a of (r.data && r.data.activities) || []) {
      if (a.type === "Accept") { this.me.state = "accepted"; this.note("accepted"); }
      else if (a.type === "Announce") { for (const e of a.object.directory || []) this.directory.set(e.id, e); this.note(`announce: ${(a.object.trees||[]).length} trees, ${(a.object.directory||[]).length} directory entries`); }
      else if (a.type === "Keep") { await this.keptPut(a.object); if (a.object.key.startsWith("trees/")) this.hold(a.object.data); await this.send("POST", "/inbox", this.activity("Kept", {key: a.object.key, sha256: a.object.sha256})); this.note(`keeping ${a.object.key} (${a.object.size} B)`); }
      else if (a.type === "Ask") { await this.answer(a.object); }
      else if (a.type === "Fetch") { const b = await this.keptGet(a.object.key); if (b) { await this.send("POST", "/inbox", this.activity("Kept", {key: b.key, sha256: b.sha256, data: b.data})); this.note(`handed back ${b.key}`); } else this.note(`asked for ${a.object.key}, not kept here`); }
      else if (a.type === "Reject") { this.me.state = "rejected"; this.note("rejected: " + (a.summary || "")); }
    }
    this.save(); this.render();
  },
  // ---- running trees: the hub hands them over as trees/<name> blobs; Ask runs one here, Answer goes back.
  ensureRunner() {
    if (this.runner) return this.runner;
    const self = this;
    const resolver = {  // a member may ask the hub it follows: delegate goes there, signed
      async runTree(name, inputs, depth) { const r = await self.send("POST", "/run", {tree: name, inputs, depth}); return r.data; },
      async runIntent(text, depth) { const r = await self.send("POST", "/intent", {text, depth}); return r.data; },
    };
    this.runner = new Pyrtan.Runner({transport: new Pyrtan.FetchTransport(), resolver});
    return this.runner;
  },
  hold(raw) { try { this.ensureRunner().add(raw); } catch (e) { this.note(`tree refused: ${e.message}`); } },
  async restoreTrees() { for (const b of await this.keptAll()) if (b.key.startsWith("trees/")) this.hold(b.data); },
  capabilities() { return this.runner ? Object.values(this.runner.trees).map(t => t.capability()) : []; },
  async answer(ask) {
    const runner = this.ensureRunner();
    let outcome;
    try { outcome = Pyrtan.outcomeDict(await runner.runTree(ask.tree, ask.inputs || {}, ask.depth || 0)); }
    catch (e) { outcome = {status: "error", tree: ask.tree, reason: "member-error", say: String(e && e.message || e)}; }
    this.runs++;
    await this.send("POST", "/inbox", this.activity("Answer", {run: ask.run, outcome}));
    this.note(`ran ${ask.tree} for the hub: ${outcome.status}` + (outcome.say ? ` · ${String(outcome.say).slice(0, 80)}` : ""));
    this.render();
  },
  async announce() {
    if (!this.me || this.me.state !== "accepted") return;
    const kept = (await this.keptAll()).map(b => b.key).sort();
    const trees = this.me.trusted ? this.capabilities() : [];
    const own = this.me.listed ? [{id: this.me.id, name: this.me.name, kind: "member", trees: trees.map(t => t.name), seen: Date.now() / 1000}] : [];
    const heard = [...this.directory.values()].filter(e => e.id !== this.me.id);
    try {
      const r = await this.send("POST", "/inbox", this.activity("Announce", {type: "Capabilities", name: this.me.name, trees, directory: own.concat(heard), kept}));
      for (const e of (r.data && r.data.directory) || []) this.directory.set(e.id, e);
    } catch (e) { this.note("announce failed: " + e.message); }
  },
  start() {
    this.load();
    if (!this.me || this.timer) { this.render(); return; }
    // A trusted device answers Asks, so it polls faster: a sentence waits one poll.
    this.timer = setInterval(() => this.poll(), this.me.trusted ? 4000 : 20000);
    this.announceTimer = setInterval(() => this.announce(), 60000);
    this.restoreTrees().then(() => this.poll()).then(() => this.announce());
  },
  stop() { clearInterval(this.timer); clearInterval(this.announceTimer); this.timer = null; },
  async render() {
    if (!$("#member-on")) return;
    $("#member-off").hidden = !!this.me; $("#member-on").hidden = !this.me;
    if (!this.me) return;
    $("#member-id").textContent = this.me.id; $("#member-state").textContent = this.me.state;
    $("#member-name-shown").textContent = this.me.name; $("#member-polled").textContent = this.me.polled ? new Date(this.me.polled).toLocaleTimeString() : "never";
    $("#member-listed").checked = !!this.me.listed;
    $("#member-trust").textContent = this.me.trusted ? "trusted: runs the hub's trees" + (this.me.identity ? ` (as ${PyrtanIdentity.short(this.me.identity)})` : "") : "keeper only (join with the hub's token, or as a key it grants run, to run trees)";
    const tb = $("#member-trees tbody"); tb.innerHTML = "";
    const sel = $("#member-run-tree"); const chosen = sel.value; sel.innerHTML = "";
    for (const t of this.capabilities()) {
      const tr = document.createElement("tr"); tr.innerHTML = `<td><code>${esc(t.name)}</code> <span class="mute">v${esc(t.version)}</span></td><td>${(t.triggers||[]).map(p => `<div><code>${esc(p)}</code></div>`).join("")}</td><td class="mute">${esc((t.hosts||[]).join(", "))}</td>`; tb.appendChild(tr);
      const o = document.createElement("option"); o.value = t.name; o.textContent = t.name; sel.appendChild(o);
    }
    if (chosen) sel.value = chosen;
    if (!tb.children.length) tb.innerHTML = `<tr><td colspan="3" class="mute">${this.me.trusted ? "None handed over yet; the hub sends what this device should run, and this device picks it up on its next poll." : "A keeper runs nothing."}</td></tr>`;
    const blobs = await this.keptAll();
    const body = $("#kept tbody"); body.innerHTML = "";  // cleared after the await, so two renders at once do not double the rows
    for (const b of blobs) { const tr = document.createElement("tr"); tr.innerHTML = `<td><code>${esc(b.key)}</code></td><td>${esc(b.size)} B</td><td class="mute">${b.updated ? new Date(b.updated * 1000).toLocaleString() : ""}</td><td><a href="#" data-show="${esc(b.key)}">show</a></td>`; body.appendChild(tr); }
    if (!body.children.length) body.innerHTML = `<tr><td colspan="4" class="mute">Nothing yet. The hub sends its blobs as it needs keepers; "Poll now" asks sooner.</td></tr>`;
  },
};
$("#join-form").addEventListener("submit", (e) => { e.preventDefault(); member.join($("#member-name").value.trim(), $("#member-fedsecret").value); });
$("#member-poll").addEventListener("click", () => member.poll().then(() => member.announce()));
$("#member-leave").addEventListener("click", () => member.leave());
$("#member-run").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#member-run-tree").value; if (!name) return;
  let inputs = {}; try { inputs = JSON.parse($("#member-run-inputs").value || "{}"); } catch (err) { problem("inputs must be JSON", true); return; }
  const t0 = performance.now();
  const r = Pyrtan.outcomeDict(await member.ensureRunner().runTree(name, inputs));
  const card = document.createElement("div"); card.className = "card";
  card.innerHTML = `<div><span class="status ${esc(r.status)}">${esc(r.status)}</span>${r.reason ? ` <span class="mute">(${esc(r.reason)})</span>` : ""} <span class="mute">· ${esc(r.tree||"")} · ${Math.round(performance.now() - t0)} ms · ran in this browser</span></div>${r.say ? `<div class="say">${esc(r.say)}</div>` : ""}${r.output != null ? `<details open><summary>output</summary><pre>${esc(JSON.stringify(r.output, null, 2))}</pre></details>` : ""}${r.trace ? `<details open><summary>trace</summary><pre>${esc(renderTrace(r.trace))}</pre></details>` : ""}`;
  $("#member-out").prepend(card);
});
$("#member-listed").addEventListener("change", (e) => { member.me.listed = e.target.checked; member.save(); member.announce(); });
$("#kept").addEventListener("click", async (e) => { const a = e.target.closest("a[data-show]"); if (!a) return; e.preventDefault(); const b = await member.keptGet(a.dataset.show); alert(b ? JSON.stringify(b.data, null, 2).slice(0, 4000) : "not kept"); });
if ("serviceWorker" in navigator && (location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname))) { navigator.serviceWorker.register("sw.js").catch(() => {}); }

// ------------------------------------------------------------ chat
// A seat in the hub's lobby: start, then long-poll for events (the hub
// echoes our own lines too, so the log is the hub's word, in its order).
const chat = {
  seat: null, after: 0, gen: 0, state: null, partner: null, sure: 0,
  indicator: false, partnerTyping: false, sentTyping: false, sentAt: 0,
  pref() { try { return localStorage.getItem("pyrtan.chat.typing") !== "0"; } catch (e) { return true; } },
  async post(path, body, extra) {
    const r = await api(path, Object.assign({method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}, extra || {}));
    if (r.status === 401) throw new Error("401: this Pyrtan needs a token");
    return r.json();
  },
  async count() {
    try { const s = await json("chat"); $("#chat-count").textContent = `Now: ${s.waiting} waiting, ${s.talking} talking, ${s.bots} bot${s.bots === 1 ? "" : "s"}` + (s.hubs ? `, and ${s.hubs} other hub${s.hubs === 1 ? "" : "s"} to meet strangers on.` : "."); $("#chat-typing-opt").hidden = s.typing !== true; } catch (e) {}
  },
  async start() {
    const interests = $("#chat-interests").value.split(/[,\s]+/).filter(Boolean);
    let r; try { r = await this.post("chat/start", {want: $("#chat-want").value, interests, typing_indicator: $("#chat-typing").checked}); } catch (e) { problem(e.message, true); return; }
    if (!r.seat) { problem(r.say || "The lobby did not take a seat.", true); return; }
    problem(""); this.seat = r.seat; this.after = 0; $("#chat-log").innerHTML = "";
    $("#chat-off").hidden = true; $("#chat-on").hidden = false;
    this.take(r); this.loop(++this.gen); $("#chat-text").focus();
  },
  take(r) {
    for (const e of r.events || []) if (e.n > this.after) { this.render(e); this.after = e.n; }
    this.state = r.state; this.partner = r.partner;
    this.indicator = !!r.typing_indicator; this.partnerTyping = !!r.partner_typing;
    $("#chat-typing-line").textContent = this.partnerTyping ? (r.partner && r.partner.kind === "bot" ? "Bot is thinking…" : "Stranger is typing…") : "";
    $("#chat-text").disabled = r.state !== "talking";
    $("#chat-text").placeholder = r.state === "talking" ? (r.partner && r.partner.kind === "bot" ? "ask the bot something" : "say something") : r.state === "waiting" ? "waiting for someone…" : "press New for someone else";
    this.resetNext();
  },
  async loop(gen) {
    while (this.seat && gen === this.gen) {
      // Saying what is shown lets the hub answer as soon as the stranger starts or stops typing.
      const ask = {seat: this.seat, after: this.after, wait: 20}; if (this.indicator) ask.partner_typing = this.partnerTyping;
      let r; try { r = await this.post("chat/poll", ask); } catch (e) { await new Promise(res => setTimeout(res, 2000)); continue; }
      if (gen !== this.gen) return;
      if (r.state === "left") return;
      if (r.reason === "no-seat") { this.line("sys", "This seat is gone (nobody polled it for a minute). Start again."); this.seat = null; this.take({state: "ended"}); return; }
      this.take(r);
    }
  },
  render(e) {
    if (e.kind === "system") return this.line("sys", e.text);
    const el = this.line(e.kind, e.text, {you: "You", stranger: "Stranger", bot: "Bot"}[e.kind] || e.kind);
    if (e.kind === "bot" && (e.trace || e.tree)) {
      const d = document.createElement("details");
      d.innerHTML = `<summary>${esc([e.status, e.tree, e.peer && e.peer !== "local" ? "from " + e.peer : ""].filter(Boolean).join(" · "))}</summary>${e.trace ? `<pre>${esc(renderTrace(e.trace))}</pre>` : ""}`;
      el.appendChild(d);
    }
  },
  line(kind, text, who) {
    const log = $("#chat-log"), stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    const el = document.createElement("div"); el.className = "line " + kind;
    el.innerHTML = (who ? `<span class="who">${esc(who)}:</span>` : "") + esc(text);
    log.appendChild(el); if (stick) log.scrollTop = log.scrollHeight;
    return el;
  },
  async say() {
    const text = $("#chat-text").value.trim(); if (!text || !this.seat) return;
    $("#chat-text").value = ""; this.sentTyping = false;  // the line ends the typing, on the hub too
    try { const r = await this.post("chat/say", {seat: this.seat, text}); if (r.status !== "ok") this.line("sys", r.say || r.reason); } catch (e) { this.line("sys", e.message); }
  },
  typed(on) {
    // Typing goes out when it starts or stops, and again every two seconds while it goes on.
    if (!this.seat || !this.indicator || this.state !== "talking" || (this.partner && this.partner.kind === "bot")) return;
    const now = Date.now();
    if (on === this.sentTyping && (!on || now - this.sentAt < 2000)) return;
    this.sentTyping = on; this.sentAt = now;
    this.post("chat/typing", {seat: this.seat, typing: on}).catch(() => {});
  },
  async setIndicator(on) {
    try { localStorage.setItem("pyrtan.chat.typing", on ? "1" : "0"); } catch (e) {}
    if (!this.seat) return;
    this.sentTyping = false;
    try { this.take(await this.post("chat/settings", {seat: this.seat, typing_indicator: on, after: this.after})); } catch (e) { this.line("sys", e.message); }
  },
  resetNext() { this.sure = 0; $("#chat-next").textContent = this.state === "talking" ? "Stop" : "New"; },
  async next() {
    // Like Omegle: while talking, the first press asks, the second ends it and finds someone new.
    if (this.state === "talking" && !this.sure) { this.sure = 1; $("#chat-next").textContent = "Really?"; setTimeout(() => this.resetNext(), 3000); return; }
    if (!this.seat) return this.start();
    this.sentTyping = false;
    try { this.take(await this.post("chat/next", {seat: this.seat, after: this.after, typing_indicator: $("#chat-typing").checked})); } catch (e) { this.line("sys", e.message); }
  },
  leave(keepalive) {
    if (!this.seat) return;
    const seat = this.seat; this.seat = null; this.gen++;
    this.post("chat/leave", {seat}, keepalive ? {keepalive: true} : {}).catch(() => {});
  },
};
$("#chat-start").addEventListener("submit", (e) => { e.preventDefault(); chat.start(); });
$("#chat-say").addEventListener("submit", (e) => { e.preventDefault(); chat.say(); });
$("#chat-text").addEventListener("input", () => chat.typed(!!$("#chat-text").value.trim()));
$("#chat-text").addEventListener("blur", () => chat.typed(false));
$("#chat-typing").checked = chat.pref();
$("#chat-typing").addEventListener("change", (e) => chat.setIndicator(e.target.checked));
$("#chat-next").addEventListener("click", () => chat.next());
$("#chat-stop").addEventListener("click", (e) => { e.preventDefault(); chat.leave(); $("#chat-on").hidden = true; $("#chat-off").hidden = false; chat.count(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#tab-chat").hidden && chat.seat) { e.preventDefault(); chat.next(); } });
window.addEventListener("pagehide", () => chat.leave(true));

// ---------------------------------------------------------------- agents
// A model beside the swarm, never inside it (agents.js, _meta/agents.md).
// It reaches the swarm only through this page's own calls, and what it
// suggests is applied here only when you accept it, and only to the trees
// this browser holds; a hub keeps it at /suggestions for pyrtan grow.
const agentsUI = {
  agent: null, config: {}, skills: null, busy: false,
  init() {
    try { this.config = JSON.parse(localStorage.getItem("pyrtan.agents.config") || "{}"); } catch (e) { this.config = {}; }
    $("#agent-name").value = this.config.name || "scout";
    $("#agent-engine").value = this.config.engine || (PyrtanAgents.WebLLMEngine.available() ? "webllm" : PyrtanAgents.WllamaEngine.available() ? "wllama" : "openai");
    $("#agent-url").value = this.config.url || "http://localhost:11434/v1";
    $("#agent-server-model").value = this.config.serverModel || "qwen2.5:3b";
    for (const el of document.querySelectorAll(".origin-here")) el.textContent = location.origin;
    this.skills = new PyrtanAgents.Skills(PyrtanAgents.browserStorage("pyrtan.agents:"));
    this.engineChanged();
  },
  models(kind) { return kind === "wllama" ? PyrtanAgents.CPU_MODELS : PyrtanAgents.MODELS; },
  engineChanged() {
    const kind = $("#agent-engine").value, web = kind !== "openai";
    if (web && $("#agent-model").dataset.kind !== kind) {
      $("#agent-model").innerHTML = this.models(kind).map(m => `<option value="${esc(m.id)}">${esc(m.label)} · ${esc(m.licence)} · ${esc(m.size)}</option>`).join("");
      $("#agent-model").dataset.kind = kind;
      $("#agent-model").value = (kind === "wllama" ? this.config.cpuModel : this.config.model) || (kind === "wllama" ? PyrtanAgents.DEFAULT_CPU_MODEL : PyrtanAgents.DEFAULT_MODEL);
    }
    $("#agent-model").hidden = !web; $("#agent-url").hidden = web; $("#agent-server-model").hidden = web;
    $("#agent-gpu").hidden = kind !== "webllm" || PyrtanAgents.WebLLMEngine.available();
  },
  where() { return local ? "this browser" : (base || location.origin); },
  hub(method, path, body) { return json(path, method === "GET" ? undefined : {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})}); },
  raw(name) { const t = local && local.runner.get(name); return t ? t.raw : null; },
  async start() {
    const engineKind = $("#agent-engine").value;
    const picked = $("#agent-model").value;
    this.config = {name: $("#agent-name").value.trim() || "scout", engine: engineKind, model: engineKind === "webllm" ? picked : this.config.model, cpuModel: engineKind === "wllama" ? picked : this.config.cpuModel, url: $("#agent-url").value.trim(), serverModel: $("#agent-server-model").value.trim()};
    try { localStorage.setItem("pyrtan.agents.config", JSON.stringify(this.config)); } catch (e) {}
    const progress = (text) => { $("#agent-progress").textContent = text; };
    const engine = engineKind === "webllm" ? new PyrtanAgents.WebLLMEngine({model: picked, onProgress: (text) => progress(text)})
      : engineKind === "wllama" ? new PyrtanAgents.WllamaEngine({model: picked, onProgress: (text) => progress(text)})
      : new PyrtanAgents.OpenAIEngine({url: this.config.url, model: this.config.serverModel});
    $("#agent-go").disabled = true;
    progress(engineKind === "webllm" ? "Loading WebLLM and the model; the first time, this is a download…" : engineKind === "wllama" ? "Loading llama.cpp (WebAssembly) and the model; the first time, this is a download…" : `Asking ${this.config.url}…`);
    try {
      await engine.load();
      if (engineKind === "openai") await engine.complete([{role: "user", content: "Say ok."}], {maxTokens: 4});
    } catch (e) { progress(""); problem(`The agent could not start: ${e.message}`, true); $("#agent-go").disabled = false; return; }
    progress("");
    const hubKey = local ? "browser" : (base || location.origin);
    this.agent = new PyrtanAgents.Agent({name: this.config.name, engine, hub: (m, p, b) => this.hub(m, p, b), raw: (n) => this.raw(n), storage: PyrtanAgents.browserStorage(`pyrtan.agents:${hubKey}:`), skills: this.skills, onEvent: (e) => this.event(e)});
    $("#agent-go").disabled = false;
    $("#agent-setup").hidden = true; $("#agent-on").hidden = false;
    $("#agent-who").textContent = this.agent.name;
    const m = this.models(engineKind).find(x => x.id === engine.model);
    $("#agent-what").textContent = `${m ? m.label : engine.model} · ${engineKind === "webllm" ? "in this browser" : engineKind === "wllama" ? "in this browser, on its CPU" : "on " + this.config.url} · beside ${this.where()}`;
    $("#agent-log").innerHTML = "";
    for (const msg of this.agent.memory.chat.slice(-10)) this.line(msg.role === "you" ? "you" : "bot", msg.role === "you" ? "You" : this.agent.name, esc(msg.content));
    this.sys(`${this.agent.name} is a model, not a tree: what it says is its own, and what a tree says comes with the tree's name and path. /help lists what it can do.`);
    this.render();
  },
  putAway() {
    if (!this.agent) return;
    this.agent.stop();
    if (this.agent.engine.unload) this.agent.engine.unload().catch(() => {});
    this.agent = null; $("#agent-setup").hidden = false; $("#agent-on").hidden = true;
  },
  line(cls, who, html, extra) {
    const log = $("#agent-log"), div = document.createElement("div");
    div.className = "line " + cls;
    div.innerHTML = (who ? `<span class="who">${esc(who)}:</span>` : "") + html + (extra || "");
    log.appendChild(div); log.scrollTop = log.scrollHeight;
  },
  sys(text) { this.line("sys", "", esc(text)); },
  event(e) {
    if (e.type === "thinking") $("#agent-typing").textContent = `${e.agent} is thinking (${e.job})…`;
    else if (e.type === "probe") { const r = e.result; this.sys(`probe ${e.tree}: “${r.text}” → ${r.read === null ? "no tree reads it" : r.read === "tie" ? "a tie" : r.read === e.tree ? (r.status ? `${r.status}${r.reason ? " (" + r.reason + ")" : ""}` : "reads, not run") : "reads as " + r.read}`); }
    else if (e.type === "refused") this.sys(`refused by the rules: ${e.what}: ${e.why}`);
    else if (e.type === "suggestion") { this.sys(`suggests a ${e.item.suggestion.kind} for ${e.item.suggestion.tree || "“" + e.item.suggestion.text + "”"}`); this.renderSuggestions(); }
    else if (e.type === "patrol" && e.step !== "done") this.sys(e.step === "probe" ? `patrol: probing ${e.tree}` : `patrol: the ${e.step}`);
    else if (e.type === "action") { const a = e.action; if (a.kind === "ask" && a.result) this.line("tree", a.result.tree || "no tree", esc(a.result.say || a.result.status), a.result.path && a.result.path.length ? `<details><summary>path</summary><code>${esc(a.result.path.join(" > "))}</code></details>` : ""); else if (a.kind === "remember") this.sys(`remembered: ${a.input}`); }
  },
  async run(fn) {
    if (this.busy) return; this.busy = true; $("#agent-stop").disabled = false;
    try { await fn(); } catch (e) { this.sys(`error: ${e.message}`); }
    finally { this.busy = false; $("#agent-stop").disabled = true; $("#agent-typing").textContent = ""; this.renderMemory(); }
  },
  say(text) {
    if (!this.agent || !text) return;
    this.line("you", "You", esc(text));
    return this.run(async () => {
      const r = await this.agent.chat(text);
      if (r.outcome) this.line("tree", r.outcome.tree || "no tree", esc(r.outcome.say || r.outcome.status), (r.outcome.trace ? `<details><summary>trace</summary><pre>${esc(renderTrace(r.outcome.trace))}</pre></details>` : ""));
      else if (r.say) this.line("bot", this.agent.name, esc(r.say).replace(/\n/g, "<br>"));
    });
  },
  patrol() { return this.run(async () => { const r = await this.agent.patrol(); this.line("bot", this.agent.name, esc(`Patrolled ${r.trees.length} trees: ${r.tally.sentences} sentences, ${r.tally.read} read, ${r.tally.unread} read to nothing, ${r.tally.misread} to another tree, ${r.tally.gaps} met a gap. ${r.suggestions.length} suggestions, ${r.refused.length} refused by the rules.${r.stopped ? " (stopped)" : ""}`)); }); },
  // Accepting: in a browser that is its own hub, the tree grows here and now; any hub keeps the suggestion for pyrtan grow.
  async accept(id) {
    const item = this.agent.suggestions.find(x => x.id === id); if (!item) return;
    const s = item.suggestion; let applied = "";
    if (local && (s.kind === "branch" || s.kind === "trigger")) {
      const raw = this.raw(s.tree);
      try {
        let grown = null;
        if (raw && s.kind === "branch") { const d = Pyrtan.grow.draftBranch(raw, s); if (d) grown = Pyrtan.grow.growTree(raw, d, s.meaning); }
        if (raw && s.kind === "trigger") { grown = JSON.parse(JSON.stringify(raw)); grown.triggers = (grown.triggers || []).concat([{pattern: s.pattern}]); }
        if (grown) { new Pyrtan.Tree(grown); local.add(grown, local.held[s.tree]); applied = "grown in this page"; }
      } catch (e) { this.sys(`could not grow ${s.tree} here: ${e.message}`); return; }
    }
    let kept = "";
    try { const out = await this.agent.send([item]); kept = (out.refused || []).length ? `refused by the hub: ${out.refused[0].why}` : `kept at ${this.where()}'s /suggestions`; }
    catch (e) { kept = `not kept: ${e.message}${/404/.test(e.message) ? " (a hub older than 0.18 has no /suggestions; download it instead)" : ""}`; }
    this.agent.decide(id, "accepted");
    item.note = [applied, kept].filter(Boolean).join(" · ");
    if (applied) { try { renderCaps(await json("capabilities")); await this.agent.trees(true); } catch (e) {} }
    this.renderSuggestions(); this.renderMemory();
  },
  dismiss(id) {
    const why = prompt("Why not? The agent remembers. (optional)") || "";
    this.agent.decide(id, "dismissed", why.trim()); this.renderSuggestions(); this.renderMemory();
  },
  download() {
    const items = (this.agent ? this.agent.suggestions : []).filter(x => x.status === "accepted").map(x => x.suggestion);
    if (!items.length) { this.sys("Nothing accepted yet."); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify({"pyrtan-suggestions": 1, suggestions: items}, null, 2)], {type: "application/json"}));
    a.download = "pyrtan-suggestions.json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    this.sys("Saved. Then: pyrtan grow --suggestions pyrtan-suggestions.json --trees DIR --apply");
  },
  render() { if (!this.skills) this.init(); this.renderSuggestions(); this.renderMemory(); this.renderSkills(); },
  renderSuggestions() {
    const box = $("#agent-sugs"); if (!box) return;
    const items = this.agent ? this.agent.suggestions.slice().reverse() : [];
    if (!items.length) { box.innerHTML = '<p class="mute">Nothing yet. Press Patrol, or ask it to <code>/probe</code> a tree.</p>'; return; }
    box.innerHTML = items.map(x => {
      const s = x.suggestion, by = s.by || {};
      const what = s.kind === "branch" ? `<code>${esc(s.tree)}</code> · <code>${esc(s.node)}</code>: <code>${esc(s.on || "")}</code> = <code class="gap">${esc(JSON.stringify(s.observed))}</code> means a <strong>${esc(s.meaning.kind)}</strong>${s.meaning.reason ? ` <span class="mute">(${esc(s.meaning.reason)})</span>` : ""}<div class="say">${esc(s.meaning.say)}</div>`
        : s.kind === "trigger" ? `<code>${esc(s.tree)}</code> should answer to <code>${esc(s.pattern)}</code><div class="mute" style="font-size:14px">reads ${(s.covers || []).map(c => "“" + esc(c) + "”").join(", ")}</div>`
        : `a new tree for “${esc(s.text)}”<div class="say">${esc(s.description || "")}</div>${(s.hosts_hint || []).length ? `<div class="mute" style="font-size:14px">hosts it guessed (a note, never a host): ${esc(s.hosts_hint.join(", "))}</div>` : ""}`;
      const state = x.status === "new" ? `<div style="display:flex;gap:8px;margin-top:8px"><button type="button" data-accept="${x.id}">Accept</button><button type="button" class="quiet" data-dismiss="${x.id}">Dismiss</button></div>` : `<div class="mute sug-state" style="font-size:14px;margin-top:6px">${esc(x.status)}${x.note ? " · " + esc(x.note) : ""}</div>`;
      return `<div class="card"><div><span class="status asked">${esc(s.kind)}</span> <span class="mute">by ${esc(by.agent || "")}${by.model ? " · " + esc(by.model) : ""}</span></div><div style="margin-top:4px">${what}</div>${s.why ? `<div class="mute" style="font-size:14px">${esc(s.why)}</div>` : ""}${x.preview && x.preview.text ? `<details><summary>what it would write</summary><pre>${esc(x.preview.text)}</pre></details>` : ""}${state}</div>`;
    }).join("");
  },
  renderMemory() {
    const body = $("#agent-notes tbody"); if (!body) return;
    const notes = this.agent ? this.agent.memory.notes.slice().reverse() : [];
    body.innerHTML = notes.length ? notes.map(n => `<tr><td class="mute">${esc(n.source)}</td><td>${esc(n.text)}</td><td><button type="button" class="quiet" data-forget="${n.id}">Forget</button></td></tr>`).join("") : `<tr><td class="mute">${this.agent ? "Nothing kept yet." : "Start an agent to see what it remembers."}</td></tr>`;
  },
  renderSkills() {
    const body = $("#agent-skills tbody"); if (!body || !this.skills) return;
    body.innerHTML = this.skills.list().map(s => `<tr><td><code>${esc(s.name)}</code><div class="mute">${esc(s.description || "")}</div></td><td>${esc(s.when)}</td><td>${s.builtin ? '<span class="mute">built in</span>' : `<label><input type="checkbox" data-skill-on="${esc(s.name)}"${s.on ? " checked" : ""}> on</label> <button type="button" class="quiet" data-skill-remove="${esc(s.name)}">Remove</button>`}</td></tr>`).join("");
  },
};
$("#agent-engine").addEventListener("change", () => agentsUI.engineChanged());
$("#agent-start").addEventListener("submit", (e) => { e.preventDefault(); agentsUI.start(); });
$("#agent-say").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#agent-text").value.trim(); $("#agent-text").value = ""; agentsUI.say(t); });
$("#agent-patrol").addEventListener("click", () => agentsUI.patrol());
$("#agent-stop").addEventListener("click", () => { if (agentsUI.agent) { agentsUI.agent.stop(); agentsUI.sys("stopping after this step…"); } });
$("#agent-off").addEventListener("click", () => agentsUI.putAway());
$("#agent-download").addEventListener("click", () => agentsUI.download());
$("#agent-sugs").addEventListener("click", (e) => { const a = e.target.closest("[data-accept]"), d = e.target.closest("[data-dismiss]"); if (a) agentsUI.accept(Number(a.dataset.accept)); if (d) agentsUI.dismiss(Number(d.dataset.dismiss)); });
$("#agent-note-form").addEventListener("submit", (e) => { e.preventDefault(); if (agentsUI.agent && $("#agent-note").value.trim()) { agentsUI.agent.memory.remember($("#agent-note").value, "you"); $("#agent-note").value = ""; agentsUI.renderMemory(); } });
$("#agent-notes").addEventListener("click", (e) => { const f = e.target.closest("[data-forget]"); if (f && agentsUI.agent) { agentsUI.agent.memory.forget(f.dataset.forget); agentsUI.renderMemory(); } });
$("#agent-forget-all").addEventListener("click", () => { if (agentsUI.agent && confirm("Forget everything this agent keeps for this hub: notes, probes, verdicts and the conversation?")) { agentsUI.agent.memory.clear(); agentsUI.renderMemory(); } });
$("#agent-skill-form").addEventListener("submit", (e) => {
  e.preventDefault();
  try { agentsUI.skills.add({name: $("#skill-name").value, when: $("#skill-when").value, description: $("#skill-desc").value, instructions: $("#skill-body").value}); for (const id of ["#skill-name", "#skill-desc", "#skill-body"]) $(id).value = ""; problem(""); }
  catch (err) { problem(err.message, true); }
  agentsUI.renderSkills();
});
$("#agent-skills").addEventListener("change", (e) => { const t = e.target.closest("[data-skill-on]"); if (t) agentsUI.skills.toggle(t.dataset.skillOn, t.checked); });
$("#agent-skills").addEventListener("click", (e) => { const r = e.target.closest("[data-skill-remove]"); if (r) { agentsUI.skills.remove(r.dataset.skillRemove); agentsUI.renderSkills(); } });
$("#skill-import").addEventListener("change", async (e) => { const f = e.target.files[0]; if (!f) return; try { const got = agentsUI.skills.import(await f.text()); problem(""); if (agentsUI.agent) agentsUI.sys(`imported ${got.map(s => s.name).join(", ")}`); } catch (err) { problem(`${f.name}: ${err.message}`, true); } e.target.value = ""; agentsUI.renderSkills(); });
$("#skill-export").addEventListener("click", () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([agentsUI.skills.export()], {type: "application/json"})); a.download = "pyrtan-skills.json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); });
agentsUI.init();

// Forgetting the hub makes this browser its own hub again.
$("#forget").addEventListener("click", () => { chat.leave(true); agentsUI.putAway(); store.clear(); base = ""; token = ""; member.stop(); $("#hub-url").value = ""; $("#hub-token").value = ""; $("#caps tbody").innerHTML = ""; $("#tries").innerHTML = ""; problem(""); if (remote) { const q = new URLSearchParams(location.search); if (q.has("hub")) { q.delete("hub"); history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q : "") + location.hash); } load(); } });
ident.init().finally(load);
</script>
</body>
</html>
""".replace("__VERSION__", __version__).replace("__THEME__", THEME_JS).replace("__IDENTITY__", IDENTITY_JS).replace("__SYNC__", SYNC_JS).replace("__AGENTS__", AGENTS_JS)


def page(trees: dict[str, Any] | None = None) -> str:
    """The console, carrying ``trees`` (in the shape of a catalog's
    ``trees.json``; the default trees when None) to hold when no hub is
    joined, with no network needed."""
    carried = bundle() if trees is None else trees
    # Keys keep the file's order (a set node renders its keys in order). Inside
    # <script>, "<" never appears as itself, so no "</script>" in a tree can end the block.
    data = json.dumps(carried, ensure_ascii=False, separators=(",", ":"), default=str).replace("<", "\\u003c")
    return TEMPLATE.replace("__TREES__", data)


@functools.lru_cache(maxsize=1)
def default_page() -> str:
    """The console a hub or service serves: carrying the default trees,
    built the first time it is asked for rather than on every import."""
    return page()


def __getattr__(name: str) -> Any:
    # ``PAGE``, as it was before the console carried trees: the default page.
    if name == "PAGE":
        return default_page()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

MANIFEST = json.dumps({
    "name": "Pyrtan",
    "short_name": "Pyrtan",
    "description": "The console of a Pyrtan swarm, and a member of it: install it and this device keeps the hub's blobs.",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#fbfaf7",
    "theme_color": "#c2410c",
    "icons": [{"src": "data:image/svg+xml," + urllib.parse.quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#c2410c"/><path d="M18 46V18h14a9 9 0 0 1 0 18h-8" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/></svg>'), "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
}, indent=2)

# The service worker keeps the console shell so the page opens offline (a
# phone in a tunnel, a hub that is down): the shell comes from the cache,
# everything the page asks the hub for goes to the network and nowhere else.
SERVICE_WORKER = r"""const SHELL = "pyrtan-shell-__VERSION__";
const FILES = ["./", "manifest.webmanifest", "pyrtan.js"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const shell = e.request.mode === "navigate" || url.pathname.endsWith("/manifest.webmanifest");
  if (!shell || e.request.method !== "GET") return;  // API calls: the network, always
  e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request).then(r => r || caches.match("./"))));
});
""".replace("__VERSION__", __version__)
