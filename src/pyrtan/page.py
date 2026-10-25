"""The demo page: one HTML file a service or hub serves at ``/``.

It lists what the swarm can answer, takes a sentence, posts it to
``/intent`` on the same origin, and shows the answer, the output and the
trace. When nothing answers, it shows the tree that would, and the command
that scaffolds it; the sentences nobody answered are listed beside what the
swarm can do. No external assets, no build step; it exists so that someone
can point a browser at a running Pyrtan and understand it in a minute.
"""

from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pyrtan</title>
<style>
  :root { --bg:#fbfaf7; --fg:#1d1d1b; --mute:#6b6a64; --line:#e3e1da; --card:#ffffff; --ok:#1f7a4d; --bad:#a3382b; --ask:#8a6400; --accent:#2b5db8; }
  @media (prefers-color-scheme: dark) { :root { --bg:#141513; --fg:#ecebe6; --mute:#9d9c95; --line:#2b2c29; --card:#1c1d1b; --ok:#5fcf93; --bad:#f08a7a; --ask:#e2b84a; --accent:#7fa6f0; } }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }
  main { max-width:880px; margin:0 auto; padding:32px 16px 64px }
  h1 { font-size:22px; margin:0 0 4px } h1 small { color:var(--mute); font-weight:normal; font-size:14px; margin-left:8px }
  p.lede { color:var(--mute); margin:0 0 24px }
  form { display:flex; gap:8px; margin:0 0 16px }
  input[type=text] { flex:1; font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg) }
  button { font:inherit; padding:10px 16px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer }
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
  footer { color:var(--mute); font-size:13px; margin-top:32px }
</style>
</head>
<body>
<main>
  <h1>Pyrtan <small id="who">…</small></h1>
  <p class="lede">A core set of expandable services that act on all forms of I/O through hardcoded, inspectable decision trees. Type a sentence a tree answers to; the answer comes back with the exact path that produced it. If nothing answers, you get the tree that would. No model is involved.</p>

  <form id="ask">
    <input type="text" id="text" placeholder="what's the weather in Oslo" autofocus autocomplete="off">
    <button type="submit" id="go">Ask</button>
  </form>
  <div class="card" style="margin-bottom:24px">
    <div class="mute" style="margin-bottom:8px">Try one</div>
    <ul class="tries" id="tries"></ul>
  </div>

  <div id="out"></div>

  <h2 style="font-size:17px;margin:32px 0 8px">What this swarm can answer</h2>
  <div class="card"><table id="caps"><thead><tr><th>Tree</th><th>Answers to</th><th>From</th></tr></thead><tbody></tbody></table></div>

  <h2 style="font-size:17px;margin:32px 0 8px">Waiting to be written</h2>
  <div class="card"><table id="unanswered"><thead><tr><th>Nobody answered</th><th>Would be</th><th>To write it</th></tr></thead><tbody><tr><td colspan="3" class="mute">Nothing yet: every sentence so far found a tree.</td></tr></tbody></table></div>

  <footer>The same endpoints the page uses: <code>POST /intent {"text": "…"}</code>, <code>POST /run {"tree": "…", "inputs": {…}}</code>, <code>GET /capabilities</code>, <code>GET /health</code>, <code>GET /gaps</code>, <code>GET /unanswered</code>. <a href="https://github.com/b-i-t-8/pyrtan">Source</a>.</footer>
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
function sample(t) {
  if (examples[t.name]) return examples[t.name];
  const pat = (t.triggers || [])[0]; if (!pat) return null;
  return pat.replace(/\[[^\]]*\]/g, "").replace(/\((\w+)\|[^)]*\)/g, "$1").replace(/\{(\w+)(?::\w+)?\}/g, (m, n) => (t.inputs && t.inputs[n] && t.inputs[n].default != null) ? t.inputs[n].default : "…").replace(/\s+/g, " ").trim();
}
async function load() {
  try {
    const h = await (await fetch("health")).json();
    $("#who").textContent = (h.name ? h.name + " · " : "") + (h.peers != null ? `${h.up}/${h.peers} peers up, ${h.trees} trees` : `${(h.trees||[]).length} trees`);
  } catch (e) { $("#who").textContent = "unreachable"; }
  const c = await (await fetch("capabilities")).json();
  const body = $("#caps tbody"); const tries = $("#tries");
  for (const t of c.trees || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><code>${esc(t.name)}</code><div class="mute">${esc(t.description||"")}</div></td><td>${(t.triggers||[]).map(p => `<div><code>${esc(p)}</code></div>`).join("")}</td><td class="mute">${esc((t.hosts||[]).join(", "))}${t.peer ? `<div>${esc(t.peer)} · ${esc(t.link||"")}</div>` : ""}</td>`;
    body.appendChild(tr);
    const s = sample(t); if (s && !s.includes("…")) { const li = document.createElement("li"); li.innerHTML = `<a href="#" data-q="${esc(s)}">${esc(s)}</a>`; tries.appendChild(li); }
  }
  tries.addEventListener("click", (e) => { const a = e.target.closest("a[data-q]"); if (!a) return; e.preventDefault(); $("#text").value = a.dataset.q; ask(); });
  loadUnanswered();
}
async function loadUnanswered() {
  let u; try { u = await (await fetch("unanswered")).json(); } catch (e) { return; }
  const rows = (u.unanswered || []);
  if (!rows.length) return;
  const body = $("#unanswered tbody"); body.innerHTML = "";
  for (const e of rows) {
    const p = e.proposal || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(e.text)} <span class="mute">×${esc(e.count||1)}${e.peer ? " · " + esc(e.peer) : ""}</span></td><td><code>${esc(p.name||"")}</code><div class="mute"><code>${esc(p.trigger||"")}</code></div></td><td><code>${esc(p.scaffold||"")}</code></td>`;
    body.appendChild(tr);
  }
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
    const res = snapshot ? await fetch("resume", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({snapshot, answer})})
                         : await fetch("intent", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text})});
    r = await res.json();
  } catch (e) { r = {status:"error", reason:"unreachable", say:String(e)}; }
  const ms = Math.round(performance.now() - t0);
  const out = $("#out");
  const card = document.createElement("div"); card.className = "card";
  let html = `<div><span class="status ${esc(r.status)}">${esc(r.status)}</span>${r.reason ? ` <span class="mute">(${esc(r.reason)})</span>` : ""} <span class="mute">· ${esc(r.tree||"")} · ${ms} ms${r.peer ? " · " + esc(r.peer) : ""}</span></div>`;
  if (r.say) html += `<div class="say">${esc(r.say)}</div>`;
  if (r.reason === "no-match" && r.output && r.output.proposal) {
    const p = r.output.proposal;
    html += `<div class="gap">No tree answers this yet. The one that would: <code>${esc(p.name)}</code>, answering <code>${esc(p.trigger)}</code>.</div><pre>${esc(p.scaffold)}</pre>`;
  }
  if (r.status === "asked") html += `<form class="resume"><input type="text" placeholder="${esc(r.question||"")}" autofocus><button>Answer</button></form>`;
  if (r.output != null) html += `<details open><summary>output</summary><pre>${esc(JSON.stringify(r.output, null, 2))}</pre></details>`;
  if (r.trace) html += `<details open><summary>trace</summary><pre>${esc(renderTrace(r.trace))}</pre></details>`;
  card.innerHTML = html;
  out.prepend(card);
  const rf = card.querySelector("form.resume");
  if (rf) rf.addEventListener("submit", (e) => { e.preventDefault(); ask(r.snapshot, rf.querySelector("input").value); });
  if (r.reason === "no-match") loadUnanswered();
  $("#go").disabled = false;
}
$("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask(); });
load();
</script>
</body>
</html>
"""
