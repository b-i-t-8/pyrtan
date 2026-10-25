"""The console: one HTML file a service or hub serves at ``/``, and the same
file hosted anywhere else (``pyrtan console --out``).

It asks a sentence and shows the answer, the output and the trace; lists
what the swarm can answer; shows the hub's peers and their ``link``; and
lists the gaps. Everything it does goes through the public endpoints
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

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pyrtan</title>
<meta name="description" content="Pyrtan console: ask the swarm, read the trace, see what it can answer, and find the gaps.">
<style>
  :root { --bg:#fbfaf7; --fg:#1d1d1b; --mute:#6b6a64; --line:#e3e1da; --card:#ffffff; --ok:#1f7a4d; --bad:#a3382b; --ask:#8a6400; --accent:#2b5db8; --accent-fg:#fff; --soft:#f1efe9 }
  @media (prefers-color-scheme: dark) { :root { --bg:#141513; --fg:#ecebe6; --mute:#9d9c95; --line:#2b2c29; --card:#1c1d1b; --ok:#5fcf93; --bad:#f08a7a; --ask:#e2b84a; --accent:#7fa6f0; --accent-fg:#101318; --soft:#222420 } }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }
  main { max-width:920px; margin:0 auto; padding:24px 16px 64px }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px; margin-bottom:12px }
  h1 { font-size:22px; margin:0 } h1 small { color:var(--mute); font-weight:normal; font-size:14px; margin-left:8px }
  h2 { font-size:17px; margin:24px 0 8px }
  nav { display:flex; gap:4px; flex-wrap:wrap; margin:12px 0 16px }
  nav a { padding:6px 12px; border-radius:999px; color:var(--fg); text-decoration:none; border:1px solid var(--line); background:var(--card); font-size:14px }
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
  @media (max-width:560px) { table thead { display:none } table td { display:block; border-top:0 } table tr { display:block; border-top:1px solid var(--line); padding:6px 0 } }
</style>
</head>
<body>
<main>
  <header>
    <h1>Pyrtan <small id="who">…</small></h1>
    <span class="mute" id="where"></span>
  </header>
  <p class="lede">A swarm of small services that act on the web through hardcoded, inspectable decision trees. Ask a sentence a tree answers to; the answer comes back with the exact path that produced it. No model is involved.</p>

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
    <a href="#gaps" data-tab="gaps">Gaps</a>
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
  </section>

  <section id="tab-gaps" hidden>
    <h2>Gaps</h2>
    <p class="mute" style="font-size:14px;margin-top:0">A gap is a decision that met a value no branch covers. Each one is the next branch to write, in the tree and node named.</p>
    <div class="card"><table id="gaps"><thead><tr><th>Tree · node</th><th>Saw</th><th>Times</th><th>Where</th></tr></thead><tbody></tbody></table></div>
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
GET  /peers    (hub only)</pre>
      <div class="mute" style="font-size:14px;margin-top:8px">With a token: <code>Authorization: Bearer …</code>. From the command line: <code>pyrtan ask "…" --at <span class="at">URL</span></code>, <code>pyrtan gaps --at <span class="at">URL</span></code>.</div>
    </div>
  </section>

  <footer><a href="https://pyrtan.com">pyrtan.com</a> · <a href="https://github.com/b-i-t-8/pyrtan">source</a> · AGPL-3.0-or-later</footer>
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
  base = saved.url || ""; token = saved.token || "";
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
  $("#where").textContent = (base || location.origin) + (a.auth === "bearer" ? " · token" : "");
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
  if (r.status === "asked") html += `<form class="row resume"><input type="text" placeholder="${esc(r.question||"")}" autofocus><button>Answer</button></form>`;
  if (r.output != null) html += `<details open><summary>output</summary><pre>${esc(JSON.stringify(r.output, null, 2))}</pre></details>`;
  if (r.trace) html += `<details open><summary>trace</summary><pre>${esc(renderTrace(r.trace))}</pre></details>`;
  if (r.reason === "gap" && r.output && r.output.gap) html += `<div class="mute" style="font-size:14px;margin-top:8px">This run met a gap. It is listed under <a href="#gaps">Gaps</a>; the branch to write is in <code>${esc(r.output.gap.tree)}</code>, node <code>${esc(r.output.gap.node)}</code>.</div>`;
  card.innerHTML = html;
  out.prepend(card);
  const rf = card.querySelector("form.resume");
  if (rf) rf.addEventListener("submit", (e) => { e.preventDefault(); ask(r.snapshot, rf.querySelector("input").value); });
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
$("#forget").addEventListener("click", () => { store.clear(); base = ""; token = ""; $("#hub-url").value = ""; $("#hub-token").value = ""; $("#who").textContent = "not connected"; $("#caps tbody").innerHTML = ""; $("#tries").innerHTML = ""; problem(""); });
load();
</script>
</body>
</html>
"""
