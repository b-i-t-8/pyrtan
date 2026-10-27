"""The console: one HTML file a service or hub serves at ``/``, and the same
file hosted anywhere else (``pyrtan console --out``).

It asks a sentence and shows the answer, the output and the trace; lists
what the swarm can answer; shows the hub's peers and their ``link``; lists
the gaps; and, on a hub started with ``--chat``, pairs this browser with a
random stranger or a bot (``/chat``, see :mod:`pyrtan.chat`). Everything it does goes through the public endpoints
(``/api``, ``/health``, ``/capabilities``, ``/peers``, ``/gaps``,
``/intent``, ``/resume``), so it works the same against a hub and a
service, and the same whether it was served by that hub or by a static
host somewhere else.

Served by a hub or service, it talks to the origin it came from. Served
from elsewhere (it finds out by asking ``api`` on its own origin), it shows
a connect box for a hub URL and a token, keeps them in the browser's local
storage, and sends the token as a bearer. The hub must then allow the
console's origin with ``--cors``. No external assets, no build step.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from . import __version__

# The tree runtime in JavaScript, served beside the console so a member can run trees.
PYRTAN_JS = (Path(__file__).parent / "js" / "pyrtan.js").read_text(encoding="utf-8")
# The wordmark's colour picker, inlined into the console and copied beside pyrtan.com's pages.
THEME_JS = (Path(__file__).parent / "js" / "theme.js").read_text(encoding="utf-8")

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pyrtan</title>
<meta name="description" content="Pyrtan console: ask the swarm, read the trace, see what it can answer, find the gaps, and join a hub as a member that keeps its blobs.">
<link rel="manifest" href="manifest.webmanifest">
<script src="pyrtan.js"></script>
<meta name="theme-color" content="#c2410c">
<script>__THEME__</script>
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
  form.row input[type=text], form.row input[type=url], form.row input[type=password] { flex:1 1 180px; min-width:0; font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  button { font:inherit; padding:10px 16px; border:0; border-radius:8px; background:var(--accent); color:var(--accent-fg); cursor:pointer }
  button.quiet { background:var(--soft); color:var(--fg) }
  button:disabled { opacity:.6; cursor:wait }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:0 0 14px }
  .status { font-weight:600 } .done{color:var(--ok)} .failed,.error{color:var(--bad)} .asked{color:var(--ask)}
  .say { font-size:18px; margin:6px 0 }
  pre { margin:8px 0 0; padding:10px 12px; background:var(--bg); border:1px solid var(--line); border-radius:8px; overflow:auto; font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap }
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
  footer { color:var(--mute); font-size:13px; margin-top:32px }
  footer a, .card a { color:var(--accent) }
  .pyr { color:var(--pyr) } .tan { color:var(--tan) }
  :focus-visible { outline:2px solid var(--tan); outline-offset:2px }
  ::selection { background:var(--tan); color:var(--tan-fg) }
  .chat-log { height:min(60vh,480px); overflow-y:auto; padding:2px 2px 8px; overflow-wrap:anywhere }
  .chat-log .line { margin:6px 0 }
  .chat-log .sys { color:var(--mute); font-size:14px }
  .chat-log .who { font-weight:600; margin-right:6px }
  .chat-log .you .who { color:var(--accent) } .chat-log .stranger .who { color:var(--bad) } .chat-log .bot .who { color:var(--ok) }
  .chat-log details { font-size:13px; margin-top:2px }
  #chat-say { flex-wrap:nowrap } #chat-say input { flex-basis:0 }
  select { font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  @media (max-width:560px) { table thead { display:none } table td { display:block; border-top:0 } table tr { display:block; border-top:1px solid var(--line); padding:6px 0 } }
</style>
</head>
<body>
<main>
  <header>
    <h1><span class="pyr" data-theme-part="pyr">pyr</span><span class="tan" data-theme-part="tan">tan</span> <small id="who">…</small></h1>
    <span class="mute" id="where"></span>
  </header>
  <p class="lede">A core set of expandable services that act on all forms of I/O through inspectable decision trees. Ask a sentence a tree answers to; the answer comes back with the exact path that produced it. If nothing answers, you get the tree that would. No model is involved.</p>

  <section id="connect" hidden>
    <div class="card">
      <div style="margin-bottom:8px" id="connect-lede">This console is not being served by a Pyrtan. Point it at one:</div>
      <form class="row" id="connect-form">
        <input type="url" id="hub-url" placeholder="https://hub.example.net or http://192.168.1.20:8700" autocomplete="off">
        <input type="password" id="hub-token" placeholder="token, if the hub needs one" autocomplete="off">
        <button type="submit">Connect</button>
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
    <a href="#watches" data-tab="watches">Watches</a>
    <a href="#gaps" data-tab="gaps">Gaps</a>
    <a href="#unanswered" data-tab="unanswered">Unanswered</a>
    <a href="#directory" data-tab="directory" id="directory-tab" hidden>Directory</a>
    <a href="#member" data-tab="member" id="member-tab" hidden>Member</a>
    <a href="#chat" data-tab="chat" id="chat-tab" hidden>Chat</a>
    <a href="#api" data-tab="api">API</a>
  </nav>

  <section id="tab-ask">
    <form class="row" id="ask">
      <input type="text" id="text" placeholder="what's the weather in Oslo" autofocus autocomplete="off">
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
    <p class="mute" style="font-size:14px">Need a tree that is not here? Browse the <a href="https://pyrtan.com/catalog/" id="catalog-link">catalog</a>, then <code>pyrtan pull NAME</code> into a folder a service serves.</p>
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
    <div class="card"><table id="gaps"><thead><tr><th>Tree · node</th><th>Saw</th><th>Times</th><th>Where</th></tr></thead><tbody></tbody></table></div>
  </section>

  <section id="tab-unanswered" hidden>
    <h2>Waiting to be written</h2>
    <p class="mute" style="font-size:14px;margin-top:0">Sentences nobody answered, each with the tree that would. <code>pyrtan unanswered --at <span class="at">URL</span> --scaffold DIR</code> writes the files.</p>
    <div class="card"><table id="unanswered"><thead><tr><th>Nobody answered</th><th>Would be</th><th>To write it</th></tr></thead><tbody></tbody></table></div>
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

  <section id="tab-chat" hidden>
    <h2>Talk to a stranger</h2>
    <p class="mute" style="font-size:14px;margin-top:0">You are paired at random: with someone else on this hub or on a hub it follows, or with a bot. A bot is a set of trees on the network, not a person and not a model; each of its lines carries the path that produced it, and what it cannot answer it says so, with the tree that would. Nobody has a name and nothing is kept: the conversation is gone when either of you leaves. <span id="chat-count"></span></p>
    <div class="card" id="chat-off">
      <form class="row" id="chat-start" style="margin:0">
        <input type="text" id="chat-interests" placeholder="interests, optional: weather, news, music" autocomplete="off">
        <select id="chat-want" aria-label="who to meet"><option value="anyone">a stranger, or a bot</option><option value="people">people only</option><option value="bot">a bot</option></select>
        <button type="submit">Start</button>
      </form>
    </div>
    <div class="card" id="chat-on" hidden>
      <div class="chat-log" id="chat-log" aria-live="polite"></div>
      <form class="row" id="chat-say" style="margin:8px 0 0">
        <button type="button" class="quiet" id="chat-next" title="Esc">New</button>
        <input type="text" id="chat-text" placeholder="say something" autocomplete="off" maxlength="1000">
        <button type="submit">Send</button>
      </form>
      <div class="mute" style="font-size:13px">Stop (or Esc), twice: end this and meet someone new · <a href="#" id="chat-stop">leave the lobby</a></div>
    </div>
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
GET  /peers    (hub only)
POST /chat/start {"want": "anyone"|"people"|"bot"}   (hub with --chat)</pre>
      <div class="mute" style="font-size:14px;margin-top:8px">With a token: <code>Authorization: Bearer …</code>. From the command line: <code>pyrtan ask "…" --at <span class="at">URL</span></code>, <code>pyrtan gaps --at <span class="at">URL</span></code>.</div>
    </div>
  </section>

  <footer><a href="https://pyrtan.com">pyrtan.com</a> · <a href="https://pyrtan.com/source/">source</a> · AGPL-3.0-or-later · console v__VERSION__<span id="hub-version"></span></footer>
</main>
<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const examples = {
  "weather": "what's the weather in Oslo",
  "hacker-news": "top 3 stories on hacker news",
  "site-status": "is example.com up",
  "github-release": "latest release of astral-sh/uv",
  "hacker-news-item": "hacker news item 1",
};
// Served by a Pyrtan, only its token is kept, under its own origin; served elsewhere, the hub URL and token.
const KEY = "pyrtan.console:" + location.origin;
const store = {
  get() { try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } },
  set(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} },
  clear() { try { localStorage.removeItem(KEY); } catch (e) {} },
};
// base: "" when this page is served by a Pyrtan (relative URLs); otherwise the hub URL the person gave.
let base = "", token = "", remote = false, isHub = false;

function api(path, init) {
  const headers = Object.assign({"Accept": "application/json"}, (init && init.headers) || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const url = base ? base.replace(/\/+$/, "") + "/" + path : path;
  return fetch(url, Object.assign({}, init, {headers}));
}
async function json(path, init) {
  const r = await api(path, init);
  if (r.status === 401) throw new Error("401: this Pyrtan needs a token");
  return r.json();
}
function problem(msg, bad) { $("#problem").innerHTML = msg ? `<div class="notice${bad ? " bad" : ""}">${esc(msg)}</div>` : ""; }

function showTab(name) {
  for (const a of document.querySelectorAll("#tabs a")) { if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  for (const s of document.querySelectorAll("main > section[id^=tab-]")) s.hidden = s.id !== "tab-" + name;
  if (name === "peers") loadPeers();
  if (name === "gaps") loadGaps();
  if (name === "watches") loadWatches();
  if (name === "unanswered") loadUnanswered();
  if (name === "directory") loadDirectory();
  if (name === "member") member.render();
  if (name === "chat") chat.count();
}

async function loadDirectory() {
  let d; try { d = await json("directory"); } catch (e) { problem(e.message, true); return; }
  const body = $("#directory tbody"); body.innerHTML = "";
  for (const h of d.hubs || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="link ${esc(h.link)}"></span>${esc(h.link)}</td><td><code>${esc(h.name||"")}</code>${h.via === "self" ? ' <span class="mute">(this hub)</span>' : ""}${h.kind === "member" ? ' <span class="mute">(member)</span>' : ""}<div class="mute">${esc(h.id)}</div></td><td>${(h.trees||[]).map(n => `<code>${esc(n)}</code>`).join(" ")}</td><td class="mute">${h.seen ? new Date(h.seen * 1000).toLocaleString() : ""}</td>`;
    body.appendChild(tr);
  }
  if (!(d.hubs || []).length) body.innerHTML = `<tr><td colspan="4" class="mute">Nobody heard of yet. Follow a seed, or start this hub with <code>--listed</code>.</td></tr>`;
}
window.addEventListener("hashchange", () => showTab((location.hash || "#ask").slice(1)));

function sample(t) {
  if (examples[t.name]) return examples[t.name];
  const pat = (t.triggers || [])[0]; if (!pat) return null;
  return pat.replace(/\[[^\]]*\]/g, "").replace(/\((\w+)\|[^)]*\)/g, "$1").replace(/\{(\w+)(?::\w+)?\}/g, (m, n) => (t.inputs && t.inputs[n] && t.inputs[n].default != null) ? t.inputs[n].default : "…").replace(/\s+/g, " ").trim();
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
        if (!token) problem("Enter the token to see what this Pyrtan can answer.");
      }
      return a;
    }
  } catch (e) {}
  remote = true;
  const saved = store.get();
  const asked = new URLSearchParams(location.search).get("hub");
  base = asked || saved.url || ""; token = saved.token || "";
  $("#connect").hidden = false;
  $("#origin").textContent = location.origin;
  $("#hub-url").value = base; $("#hub-token").value = token;
  if (!base) return null;
  try {
    const a = await json("api");
    if (a && a.pyrtan) { isHub = a.pyrtan === "hub"; return a; }
    problem(`${base} answered, but not as a Pyrtan.`, true);
  } catch (e) { problem(`${base}: ${e.message}. Is the hub up, and started with --cors ${location.origin}?`, true); }
  return null;
}

async function load() {
  const a = await detect();
  for (const el of document.querySelectorAll(".at")) el.textContent = base || location.origin;
  if (!a) { $("#who").textContent = remote ? "not connected" : "unreachable"; return; }
  $("#peers-tab").hidden = !isHub;
  $("#directory-tab").hidden = !(isHub && a.federation);
  $("#member-tab").hidden = !(isHub && a.federation);
  $("#chat-tab").hidden = !(isHub && a.chat);
  member.hub = a.federation || null;
  if (member.hub) member.start();
  $("#where").textContent = (base || location.origin) + (a.auth === "bearer" ? " · token" : "");
  $("#hub-version").textContent = a.version ? ` · ${a.pyrtan} v${a.version}` : "";
  try {
    const h = await json("health");
    $("#who").textContent = (h.name ? h.name + " · " : "") + (h.peers != null ? `${h.up}/${h.peers} peers up` + (h.trees != null ? `, ${h.trees} trees` : "") : (h.trees ? `${h.trees.length} trees` : ""));
  } catch (e) { $("#who").textContent = "unreachable"; }
  if (a.auth === "bearer" && !token) return;
  let c;
  try { c = await json("capabilities"); } catch (e) { problem(e.message + (a.auth === "bearer" ? " (is it the right one?)" : ""), true); return; }
  problem("");
  const body = $("#caps tbody"); const tries = $("#tries");
  body.innerHTML = ""; tries.innerHTML = "";
  for (const t of c.trees || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><code>${esc(t.name)}</code> <span class="mute">v${esc(t.version||1)}</span><div class="mute">${esc(t.description||"")}</div></td><td>${(t.triggers||[]).map(p => `<div><code>${esc(p)}</code></div>`).join("")}</td><td class="mute">${esc((t.hosts||[]).join(", "))}${t.peer ? `<div><span class="link ${esc(t.link||"")}"></span>${esc(t.peer)}</div>` : ""}</td>`;
    body.appendChild(tr);
    const s = sample(t); if (s && !s.includes("…")) { const li = document.createElement("li"); li.innerHTML = `<a href="#" data-q="${esc(s)}">${esc(s)}</a>`; tries.appendChild(li); }
  }
  showTab((location.hash || "#ask").slice(1));
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
        tr.innerHTML = `<td><span class="link ${esc(r.link)}"></span>${esc(r.link)}</td><td><code>${esc(r.name||"")}</code><div class="mute">${esc(r.url)}</div></td><td>${rel}${r.state && r.state !== "accepted" ? ` <span class="mute">(${esc(r.state)})</span>` : ""}</td><td>${(r.trees||[]).map(n => `<code>${esc(n)}</code>`).join(" ")}${r.last_error ? `<div class="failed">${esc(r.last_error)}</div>` : ""}</td>`;
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
    tr.innerHTML = `<td><code>${esc(gap.tree)}</code> · <code>${esc(gap.node)}</code></td><td><code>${esc(gap.on)}</code> = <code class="gap">${esc(JSON.stringify(gap.observed))}</code></td><td>${esc(gap.count||1)}</td><td class="mute">${esc(gap.peer||"")}</td>`;
    body.appendChild(tr);
  }
  if (!(g.gaps || []).length) body.innerHTML = `<tr><td colspan="4" class="mute">No gaps: every decision so far had a branch.</td></tr>`;
}

async function loadUnanswered() {
  let u; try { u = await json("unanswered"); } catch (e) { problem(e.message, true); return; }
  const body = $("#unanswered tbody"); body.innerHTML = "";
  for (const e of u.unanswered || []) {
    const p = e.proposal || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(e.text)} <span class="mute">×${esc(e.count||1)}${e.peer ? " · " + esc(e.peer) : ""}</span></td><td><code>${esc(p.name||"")}</code><div class="mute"><code>${esc(p.trigger||"")}</code></div></td><td><code>${esc(p.scaffold||"")}</code></td>`;
    body.appendChild(tr);
  }
  if (!(u.unanswered || []).length) body.innerHTML = `<tr><td colspan="3" class="mute">Nothing yet: every sentence so far found a tree.</td></tr>`;
}

function renderTrace(tr) {
  if (!tr || !tr.steps) return "";
  const lines = tr.steps.map(s => `${"  ".repeat((s.depth||0)+1)}${s.node} (${s.kind})${s.chosen ? " [" + s.chosen + "]" : ""}: ${s.summary}${s.next ? " -> " + s.next : ""}`);
  for (const g of tr.gaps || []) lines.push(`  gap: ${g.node} saw ${g.on} = ${JSON.stringify(g.observed)} and had no branch for it`);
  return `trace: ${tr.tree}\n` + lines.join("\n");
}

async function ask(snapshot, answer) {
  const text = $("#text").value.trim(); if (!text && !snapshot) return;
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
  let html = `<div><span class="status ${esc(r.status)}">${esc(r.status)}</span>${r.reason ? ` <span class="mute">(${esc(r.reason)})</span>` : ""} <span class="mute">· ${esc(r.tree||"")} · ${ms} ms${r.peer ? " · " + esc(r.peer) : ""}</span></div>`;
  if (r.say) html += `<div class="say">${esc(r.say)}</div>`;
  if (r.output && r.output.reading && r.output.reading.steps && r.output.reading.steps.length) html += `<div class="mute" style="font-size:14px">Read as <code>${esc(r.output.reading.cleaned)}</code>: ${r.output.reading.steps.map(esc).join("; ")}</div>`;
  if (r.reason === "no-match" && r.output && r.output.proposal) {
    const p = r.output.proposal;
    html += `<div class="gap">No tree answers this yet. The one that would: <code>${esc(p.name)}</code>, answering <code>${esc(p.trigger)}</code>. It is listed under <a href="#unanswered">Unanswered</a>.</div><pre>${esc(p.scaffold)}</pre>`;
  }
  if (r.status === "asked") html += `<form class="row resume"><input type="text" placeholder="${esc(r.question||"")}" autofocus><button>Answer</button></form>`;
  if (r.output != null) html += `<details open><summary>output</summary><pre>${esc(JSON.stringify(r.output, null, 2))}</pre></details>`;
  if (r.trace) html += `<details open><summary>trace</summary><pre>${esc(renderTrace(r.trace))}</pre></details>`;
  if (r.reason === "gap" && r.output && r.output.gap) html += `<div class="mute" style="font-size:14px;margin-top:8px">This run met a gap. It is listed under <a href="#gaps">Gaps</a>; the branch to write is in <code>${esc(r.output.gap.tree)}</code>, node <code>${esc(r.output.gap.node)}</code>.</div>`;
  card.innerHTML = html;
  out.prepend(card);
  const rf = card.querySelector("form.resume");
  if (rf) rf.addEventListener("submit", (e) => { e.preventDefault(); ask(r.snapshot, rf.querySelector("input").value); });
  if (r.reason === "no-match") loadUnanswered();
  $("#go").disabled = false;
}

$("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask(); });
$("#tries").addEventListener("click", (e) => { const a = e.target.closest("a[data-q]"); if (!a) return; e.preventDefault(); $("#text").value = a.dataset.q; location.hash = "#ask"; ask(); });
$("#connect-form").addEventListener("submit", (e) => {
  e.preventDefault();
  token = $("#hub-token").value;
  store.set(remote ? {url: $("#hub-url").value.trim(), token} : {token});
  load();
});
// ---------------------------------------------------------------- member
// A browser as a federation actor without an inbox. Everything it sends is
// signed the way hubs sign: HMAC-SHA256 over method, path, date and the
// SHA-256 of the canonical JSON body. SHA-256 and HMAC are written out here
// because crypto.subtle is not available on a plain http:// LAN address.
const sha256 = (() => {
  const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  return (bytes) => {
    const l = bytes.length, bitLen = l * 8, padded = new Uint8Array(((l + 9 + 63) >> 6) << 6);
    padded.set(bytes); padded[l] = 0x80;
    const dv = new DataView(padded.buffer); dv.setUint32(padded.length - 4, bitLen >>> 0); dv.setUint32(padded.length - 8, Math.floor(bitLen / 4294967296));
    let h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    const w = new Uint32Array(64);
    for (let o = 0; o < padded.length; o += 64) {
      for (let i = 0; i < 16; i++) w[i] = dv.getUint32(o + i * 4);
      for (let i = 16; i < 64; i++) { const s0 = rotr(w[i-15],7) ^ rotr(w[i-15],18) ^ (w[i-15] >>> 3), s1 = rotr(w[i-2],17) ^ rotr(w[i-2],19) ^ (w[i-2] >>> 10); w[i] = (w[i-16] + s0 + w[i-7] + s1) >>> 0; }
      let [a,b,c,d,e,f,g,hh] = h;
      for (let i = 0; i < 64; i++) { const S1 = rotr(e,6) ^ rotr(e,11) ^ rotr(e,25), ch = (e & f) ^ (~e & g), t1 = (hh + S1 + ch + K[i] + w[i]) >>> 0, S0 = rotr(a,2) ^ rotr(a,13) ^ rotr(a,22), maj = (a & b) ^ (a & c) ^ (b & c), t2 = (S0 + maj) >>> 0; hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0; }
      h = h.map((v, i) => (v + [a,b,c,d,e,f,g,hh][i]) >>> 0);
    }
    return new Uint8Array(h.flatMap(v => [v >>> 24 & 255, v >>> 16 & 255, v >>> 8 & 255, v & 255]));
  };
})();
const enc = new TextEncoder();
const hex = (b) => Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
function hmac(keyText, msgBytes) {
  let key = enc.encode(keyText); if (key.length > 64) key = sha256(key);
  const k = new Uint8Array(64); k.set(key);
  const ipad = k.map(x => x ^ 0x36), opad = k.map(x => x ^ 0x5c);
  const inner = sha256(new Uint8Array([...ipad, ...msgBytes]));
  return hex(sha256(new Uint8Array([...opad, ...inner])));
}
function canonical(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  return "{" + Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
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
    this.me = {id: "urn:pyrtan:member:" + randomHex(16), secret: randomHex(32), name: name || "browser", state: "pending", listed: true, joined: Date.now(), trusted: false};
    this.save();
    const act = this.activity("Follow", this.hub, {secret: this.me.secret, inbox: "mailbox", name: this.me.name});
    const headers = Object.assign({"Accept": "application/json", "Content-Type": "application/json"}, signHeaders(fedSecret || this.me.secret, this.me.id, "POST", "/inbox", act));
    if (token) headers["Authorization"] = "Bearer " + token;  // the operator's token makes this device trusted: it runs the hub's trees
    const r = await fetch((base ? base.replace(/\/+$/, "") : "") + "/inbox", {method: "POST", headers, body: canonical(act)});
    const data = await r.json().catch(() => ({}));
    if (r.status !== 202) { this.me = null; this.save(); this.note(`follow refused: ${data.say || r.status}`); problem(`The hub did not take the follow: ${data.say || r.status}`, true); this.render(); return; }
    this.me.trusted = !!data.trusted; this.save();
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
    $("#member-trust").textContent = this.me.trusted ? "trusted: runs the hub's trees" : "keeper only (join with the hub's token to run trees)";
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
  async post(path, body, extra) {
    const r = await api(path, Object.assign({method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}, extra || {}));
    if (r.status === 401) throw new Error("401: this Pyrtan needs a token");
    return r.json();
  },
  async count() {
    try { const s = await json("chat"); $("#chat-count").textContent = `Now: ${s.waiting} waiting, ${s.talking} talking, ${s.bots} bot${s.bots === 1 ? "" : "s"}` + (s.hubs ? `, and ${s.hubs} other hub${s.hubs === 1 ? "" : "s"} to meet strangers on.` : "."); } catch (e) {}
  },
  async start() {
    const interests = $("#chat-interests").value.split(/[,\s]+/).filter(Boolean);
    let r; try { r = await this.post("chat/start", {want: $("#chat-want").value, interests}); } catch (e) { problem(e.message, true); return; }
    if (!r.seat) { problem(r.say || "The lobby did not take a seat.", true); return; }
    problem(""); this.seat = r.seat; this.after = 0; $("#chat-log").innerHTML = "";
    $("#chat-off").hidden = true; $("#chat-on").hidden = false;
    this.take(r); this.loop(++this.gen); $("#chat-text").focus();
  },
  take(r) {
    for (const e of r.events || []) if (e.n > this.after) { this.render(e); this.after = e.n; }
    this.state = r.state; this.partner = r.partner;
    $("#chat-text").disabled = r.state !== "talking";
    $("#chat-text").placeholder = r.state === "talking" ? (r.partner && r.partner.kind === "bot" ? "ask the bot something" : "say something") : r.state === "waiting" ? "waiting for someone…" : "press New for someone else";
    this.resetNext();
  },
  async loop(gen) {
    while (this.seat && gen === this.gen) {
      let r; try { r = await this.post("chat/poll", {seat: this.seat, after: this.after, wait: 20}); } catch (e) { await new Promise(res => setTimeout(res, 2000)); continue; }
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
    $("#chat-text").value = "";
    try { const r = await this.post("chat/say", {seat: this.seat, text}); if (r.status !== "ok") this.line("sys", r.say || r.reason); } catch (e) { this.line("sys", e.message); }
  },
  resetNext() { this.sure = 0; $("#chat-next").textContent = this.state === "talking" ? "Stop" : "New"; },
  async next() {
    // Like Omegle: while talking, the first press asks, the second ends it and finds someone new.
    if (this.state === "talking" && !this.sure) { this.sure = 1; $("#chat-next").textContent = "Really?"; setTimeout(() => this.resetNext(), 3000); return; }
    if (!this.seat) return this.start();
    try { this.take(await this.post("chat/next", {seat: this.seat, after: this.after})); } catch (e) { this.line("sys", e.message); }
  },
  leave(keepalive) {
    if (!this.seat) return;
    const seat = this.seat; this.seat = null; this.gen++;
    this.post("chat/leave", {seat}, keepalive ? {keepalive: true} : {}).catch(() => {});
  },
};
$("#chat-start").addEventListener("submit", (e) => { e.preventDefault(); chat.start(); });
$("#chat-say").addEventListener("submit", (e) => { e.preventDefault(); chat.say(); });
$("#chat-next").addEventListener("click", () => chat.next());
$("#chat-stop").addEventListener("click", (e) => { e.preventDefault(); chat.leave(); $("#chat-on").hidden = true; $("#chat-off").hidden = false; chat.count(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#tab-chat").hidden && chat.seat) { e.preventDefault(); chat.next(); } });
window.addEventListener("pagehide", () => chat.leave(true));

$("#forget").addEventListener("click", () => { store.clear(); base = ""; token = ""; $("#hub-url").value = ""; $("#hub-token").value = ""; $("#who").textContent = "not connected"; $("#caps tbody").innerHTML = ""; $("#tries").innerHTML = ""; problem(""); });
load();
</script>
</body>
</html>
""".replace("__VERSION__", __version__).replace("__THEME__", THEME_JS)

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
