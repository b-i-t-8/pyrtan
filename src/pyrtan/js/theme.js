// The wordmark and its two colours. "pyr" is the primary colour (buttons,
// links, the current tab); "tan" is the secondary (focus rings, hover edges,
// selected text), and its picker also sets the mode: light, dark, or auto to
// follow the system. Click either half of the wordmark, right-click it, or
// press and hold it on a touch screen to open that half's picker. The choice
// is kept in this browser only, under this origin, and applies to every
// Pyrtan page served from it.
//
// A page gives the defaults as --pyr, --pyr-fg, --tan and --tan-fg on :root,
// with its dark values both under prefers-color-scheme (for
// :root:not([data-theme=light])) and under :root[data-theme=dark]; marks the
// halves with data-theme-part="pyr" and "tan"; and loads this script in
// <head> so a saved choice applies before first paint.
(() => {
  const KEY = "pyrtan.theme";
  const PARTS = {
    pyr: {label: "pyr", role: "primary", presets: [["ember", "#c2410c"], ["crimson", "#c92a2a"], ["rose", "#c2255c"], ["violet", "#7048e8"], ["blue", "#2b5db8"], ["teal", "#0c8599"], ["green", "#2f9e44"], ["slate", "#495057"]]},
    tan: {label: "tan", role: "secondary", presets: [["tan", "#c19a6b"], ["sand", "#d2b48c"], ["copper", "#b87333"], ["clay", "#a0522d"], ["gold", "#d4a017"], ["khaki", "#b5a642"], ["sage", "#8a9a5b"], ["steel", "#7d8ea3"]]},
  };
  const MODES = [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]];
  const root = document.documentElement;
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { return {}; } };
  const save = (t) => { try { if (Object.keys(t).length) localStorage.setItem(KEY, JSON.stringify(t)); else localStorage.removeItem(KEY); } catch (e) {} };
  const valid = (c) => typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c);
  // Text on a filled background of colour c: near-black on light colours, white on dark ones.
  function ink(c) {
    const lin = (i) => { const v = parseInt(c.slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * lin(1) + 0.7152 * lin(3) + 0.0722 * lin(5) > 0.22 ? "#101318" : "#ffffff";
  }
  function apply(t) {
    for (const part in PARTS) {
      if (valid(t[part])) { root.style.setProperty("--" + part, t[part]); root.style.setProperty("--" + part + "-fg", ink(t[part])); }
      else { root.style.removeProperty("--" + part); root.style.removeProperty("--" + part + "-fg"); }
    }
    if (t.mode === "light" || t.mode === "dark") root.dataset.theme = t.mode; else delete root.dataset.theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) { const c = getComputedStyle(root).getPropertyValue("--pyr").trim(); if (c) meta.content = c; }
  }
  let theme = load();
  apply(theme);

  const style = document.createElement("style");
  style.textContent = `
  :root[data-theme=light] { color-scheme:light } :root[data-theme=dark] { color-scheme:dark }
  [data-theme-part] { cursor:pointer; border-radius:4px; -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; touch-action:manipulation }
  [data-theme-part]:hover { text-decoration:underline; text-decoration-thickness:2px; text-underline-offset:4px }
  [data-theme-part][aria-expanded=true] { text-decoration:underline; text-decoration-thickness:2px; text-underline-offset:4px }
  .theme-pick { position:absolute; z-index:1000; width:min(264px, calc(100vw - 32px)); padding:12px; background:var(--card, #fff); color:var(--fg, #1d1d1b); border:1px solid var(--line, #e3e1da); border-radius:10px; box-shadow:0 8px 28px rgba(0,0,0,.18); font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif; letter-spacing:normal }
  .theme-pick[hidden] { display:none }
  .theme-pick .theme-head { font-weight:600; margin:0 0 8px } .theme-pick .theme-head span { color:var(--mute, #6b6a64); font-weight:normal }
  .theme-pick .theme-swatches { display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; margin:0 0 10px }
  .theme-pick .theme-swatches button { aspect-ratio:1; width:100%; min-height:36px; padding:0; border:2px solid var(--line, #e3e1da); border-radius:8px; cursor:pointer }
  .theme-pick .theme-swatches button[aria-pressed=true] { border-color:var(--fg, #1d1d1b); box-shadow:inset 0 0 0 2px var(--card, #fff) }
  .theme-pick .theme-row { display:flex; align-items:center; justify-content:space-between; gap:8px }
  .theme-pick label { display:flex; align-items:center; gap:8px; cursor:pointer }
  .theme-pick input[type=color] { width:36px; height:28px; padding:0; border:1px solid var(--line, #e3e1da); border-radius:6px; background:none; cursor:pointer }
  .theme-pick .theme-reset { font:inherit; padding:6px 10px; border:1px solid var(--line, #e3e1da); border-radius:6px; background:var(--soft, #f1efe9); color:var(--fg, #1d1d1b); cursor:pointer }
  .theme-pick .theme-reset:disabled { opacity:.5; cursor:default }
  .theme-pick .theme-mode { display:flex; gap:4px; margin:10px 0 0; padding:3px; background:var(--soft, #f1efe9); border-radius:8px }
  .theme-pick .theme-mode[hidden] { display:none }
  .theme-pick .theme-mode button { flex:1; font:inherit; padding:5px 0; border:0; border-radius:6px; background:transparent; color:var(--fg, #1d1d1b); cursor:pointer }
  .theme-pick .theme-mode button[aria-pressed=true] { background:var(--card, #fff); box-shadow:0 1px 3px rgba(0,0,0,.2); font-weight:600 }
  .theme-pick .theme-hint { color:var(--mute, #6b6a64); font-size:12px; margin:8px 0 0 }`;
  document.head.appendChild(style);

  let pick = null, openPart = null, openEl = null;

  function current(part) { return valid(theme[part]) ? theme[part].toLowerCase() : null; }
  function choose(part, color) {
    if (color) theme[part] = color.toLowerCase(); else delete theme[part];
    save(theme); apply(theme); render();
  }
  function mode(m) {
    if (m === "light" || m === "dark") theme.mode = m; else delete theme.mode;
    save(theme); apply(theme); render();
  }
  function render() {
    if (!pick || !openPart) return;
    const p = PARTS[openPart], cur = current(openPart);
    pick.setAttribute("aria-label", `${p.label} colour`);
    pick.querySelector(".theme-head").innerHTML = `${p.label} colour <span>· ${p.role}</span>`;
    pick.querySelector(".theme-swatches").innerHTML = p.presets.map(([name, c]) => `<button type="button" data-color="${c}" style="background:${c}" title="${name}" aria-label="${name}" aria-pressed="${cur === c}"></button>`).join("");
    pick.querySelector("input[type=color]").value = cur || getComputedStyle(root).getPropertyValue("--" + openPart).trim() || "#000000";
    pick.querySelector(".theme-reset").disabled = !cur;
    const modes = pick.querySelector(".theme-mode"), on = theme.mode || "auto";
    modes.hidden = openPart !== "tan";
    for (const b of modes.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === on));
  }
  function build() {
    pick = document.createElement("div");
    pick.className = "theme-pick"; pick.hidden = true; pick.setAttribute("role", "dialog");
    pick.innerHTML = `<div class="theme-head"></div><div class="theme-swatches"></div><div class="theme-row"><label><input type="color"> Custom</label><button type="button" class="theme-reset">Default</button></div><div class="theme-mode" role="group" aria-label="Mode">${MODES.map(([m, label]) => `<button type="button" data-mode="${m}">${label}</button>`).join("")}</div><div class="theme-hint">Kept in this browser only.</div>`;
    pick.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-color]");
      if (b) choose(openPart, b.dataset.color);
      else if (e.target.closest(".theme-reset")) choose(openPart, null);
      else if (e.target.closest("button[data-mode]")) mode(e.target.closest("button[data-mode]").dataset.mode);
    });
    pick.querySelector("input[type=color]").addEventListener("input", (e) => choose(openPart, e.target.value));
    document.body.appendChild(pick);
  }
  function place() {
    const r = openEl.getBoundingClientRect(), w = pick.offsetWidth;
    const left = Math.max(16, Math.min(r.left, document.documentElement.clientWidth - w - 16));
    pick.style.left = (left + window.scrollX) + "px";
    pick.style.top = (r.bottom + window.scrollY + 8) + "px";
  }
  function open(part, el) {
    if (!pick) build();
    if (openEl && openEl !== el) openEl.setAttribute("aria-expanded", "false");
    openPart = part; openEl = el; el.setAttribute("aria-expanded", "true");
    render(); pick.hidden = false; place();
  }
  function close(refocus) {
    if (!pick || pick.hidden) return;
    pick.hidden = true;
    if (openEl) { openEl.setAttribute("aria-expanded", "false"); if (refocus) openEl.focus(); }
    openPart = null; openEl = null;
  }
  const toggle = (part, el) => (openEl === el && !pick.hidden) ? close(false) : open(part, el);

  function bind(el) {
    const part = el.dataset.themePart;
    if (!PARTS[part] || el.dataset.themeBound) return;
    el.dataset.themeBound = "1";
    el.setAttribute("role", "button"); el.tabIndex = 0;
    el.setAttribute("aria-haspopup", "dialog"); el.setAttribute("aria-expanded", "false");
    el.title = `${PARTS[part].label} colour (${PARTS[part].role}): click, right-click or press and hold`;
    // Press and hold on touch and pen; a mouse has click and right-click.
    let timer = null, held = false;
    const cancel = () => { clearTimeout(timer); timer = null; };
    el.addEventListener("pointerdown", (e) => {
      held = false;
      if (e.pointerType === "mouse") return;
      cancel(); timer = setTimeout(() => { held = true; open(part, el); }, 450);
    });
    for (const ev of ["pointerup", "pointercancel", "pointerleave"]) el.addEventListener(ev, cancel);
    el.addEventListener("click", (e) => { e.preventDefault(); if (held) { held = false; return; } toggle(part, el); });
    el.addEventListener("contextmenu", (e) => { e.preventDefault(); cancel(); held = e.pointerType !== "mouse"; open(part, el); });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(part, el); } });
  }

  function start() {
    document.querySelectorAll("[data-theme-part]").forEach(bind);
    document.addEventListener("pointerdown", (e) => { if (pick && !pick.hidden && !pick.contains(e.target) && !e.target.closest("[data-theme-part]")) close(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(true); });
    window.addEventListener("resize", () => { if (pick && !pick.hidden) place(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  // Auto follows the system; the browser bar's colour follows with it.
  try { matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { apply(theme); render(); }); } catch (e) {}
  // Another tab on the same origin changed the colours.
  window.addEventListener("storage", (e) => { if (e.key === KEY) { theme = load(); apply(theme); render(); } });
})();
