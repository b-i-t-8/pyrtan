/* agents.js: agents beside the swarm, in the browser.
 *
 * An agent is an open-weights model a person starts in the console. It
 * runs in the page (WebLLM, on the browser's GPU) or on a server of the
 * person's own that speaks the OpenAI chat API (Ollama, llama.cpp, vLLM).
 * It is a client of the swarm like any other: it reads the trees'
 * triggers, asks sentences through /intent, reads /gaps and /unanswered,
 * and sends what it thinks to /suggestions. It never sits inside a run:
 * every answer a tree gives is still the tree's, with its trace.
 *
 * What it does:
 * - probes a tree with sentences of its own, read here by the same rules
 *   the swarm reads them (pyrtan.js), and runs the ones that reach the
 *   tree, so a value no branch covers becomes a real gap;
 * - says what a gap's value means (a branch), what pattern would let a
 *   tree read sentences it missed (a trigger), and what tree a sentence
 *   nobody answered needs (a tree);
 * - talks with the person, and asks the swarm when a tree can answer;
 * - remembers: notes (its own and the person's), what it probed, what was
 *   accepted, dismissed or refused, and the conversation;
 * - follows skills: instructions, built in or written by the person, for
 *   each of its jobs.
 *
 * What it may not do, by construction rather than by asking it nicely:
 * every suggestion is checked here by rules (Pyrtan.grow.problem, a
 * reading with the new pattern, a tree that still loads) before it is
 * shown, and again by `pyrtan grow --suggestions` before it is written.
 * No suggestion adds I/O, a host, or a read of env. A person accepts.
 *
 * Runs in a browser as `PyrtanAgents` (after pyrtan.js) and in node.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./pyrtan.js"));
  else root.PyrtanAgents = factory(root.Pyrtan);
})(typeof globalThis !== "undefined" ? globalThis : this, function (Pyrtan) {
  "use strict";

  // ------------------------------------------------------------ models
  // Open weights under open licences, as WebLLM builds them. Sizes are the
  // download, once; the browser keeps it.
  const WEBLLM_URL = "https://cdn.jsdelivr.net/npm/@mlc-ai/web-llm@0.2.85/+esm";
  const MODELS = [
    { id: "Qwen2.5-1.5B-Instruct-q4f16_1-MLC", label: "Qwen2.5 1.5B Instruct", licence: "Apache-2.0", size: "1.0 GB" },
    { id: "Qwen3-4B-q4f16_1-MLC", label: "Qwen3 4B", licence: "Apache-2.0", size: "2.3 GB" },
    { id: "Phi-3.5-mini-instruct-q4f16_1-MLC", label: "Phi-3.5 mini", licence: "MIT", size: "2.2 GB" },
    { id: "SmolLM2-1.7B-Instruct-q4f16_1-MLC", label: "SmolLM2 1.7B Instruct", licence: "Apache-2.0", size: "1.0 GB" },
    { id: "Qwen2.5-0.5B-Instruct-q4f16_1-MLC", label: "Qwen2.5 0.5B Instruct (small, rough)", licence: "Apache-2.0", size: "0.4 GB" },
  ];
  const DEFAULT_MODEL = MODELS[0].id;

  // ------------------------------------------------------------ engines
  // An engine is {name, model, complete(messages, {json, temperature, maxTokens}) -> text}.
  class WebLLMEngine {
    constructor(opts) {
      opts = opts || {};
      this.name = "webllm"; this.model = opts.model || DEFAULT_MODEL; this.url = opts.url || WEBLLM_URL;
      this.onProgress = opts.onProgress || null; this.engine = null; this.loading = null;
    }
    static available() { return typeof navigator !== "undefined" && !!navigator.gpu; }
    load() {
      if (this.engine) return Promise.resolve();
      if (!this.loading) {
        this.loading = (async () => {
          if (!WebLLMEngine.available()) throw new Error("this browser has no WebGPU; use a server of your own (Ollama, llama.cpp) instead");
          const lib = await import(this.url);
          this.engine = await lib.CreateMLCEngine(this.model, { initProgressCallback: (r) => this.onProgress && this.onProgress(r.text, r.progress) });
        })().catch((e) => { this.loading = null; throw e; });
      }
      return this.loading;
    }
    async complete(messages, opts) {
      opts = opts || {};
      await this.load();
      const req = { messages, temperature: opts.temperature === undefined ? 0.4 : opts.temperature, max_tokens: opts.maxTokens || 512 };
      if (opts.json) req.response_format = { type: "json_object" };
      if (/^Qwen3/.test(this.model)) req.extra_body = { enable_thinking: false };
      const r = await this.engine.chat.completions.create(req);
      return (r.choices[0] && r.choices[0].message && r.choices[0].message.content) || "";
    }
    async unload() { if (this.engine) await this.engine.unload(); this.engine = null; this.loading = null; }
  }

  // A server of the person's own: Ollama (http://localhost:11434/v1), llama.cpp, vLLM, LM Studio.
  class OpenAIEngine {
    constructor(opts) {
      opts = opts || {};
      this.name = "openai"; this.url = String(opts.url || "http://localhost:11434/v1").replace(/\/+$/, ""); this.model = opts.model || "qwen2.5:3b"; this.key = opts.key || "";
    }
    async load() {}
    async complete(messages, opts) {
      opts = opts || {};
      const body = { model: this.model, messages, temperature: opts.temperature === undefined ? 0.4 : opts.temperature, max_tokens: opts.maxTokens || 512, stream: false };
      if (opts.json) body.response_format = { type: "json_object" };
      const headers = { "Content-Type": "application/json" };
      if (this.key) headers.Authorization = "Bearer " + this.key;
      const r = await fetch(this.url + "/chat/completions", { method: "POST", headers, body: JSON.stringify(body) });
      if (!r.ok) throw new Error(`${this.url} answered ${r.status}${r.status === 403 || r.status === 0 ? " (does it allow this page's origin? OLLAMA_ORIGINS for Ollama)" : ""}`);
      const d = await r.json();
      return (d.choices && d.choices[0] && d.choices[0].message && d.choices[0].message.content) || "";
    }
    async unload() {}
  }

  // For tests and demos: fn(messages, opts) -> text, or a list of replies in turn.
  class ScriptedEngine {
    constructor(fn, opts) {
      this.name = "scripted"; this.model = (opts && opts.model) || "scripted"; this.calls = [];
      this.fn = typeof fn === "function" ? fn : ((list) => () => (list.length > 1 ? list.shift() : list[0]))(Array.isArray(fn) ? fn.slice() : [String(fn)]);
    }
    async load() {}
    async complete(messages, opts) { this.calls.push({ messages, opts }); const out = await this.fn(messages, opts || {}); return typeof out === "string" ? out : JSON.stringify(out); }
    async unload() {}
  }

  // A model's JSON, found in what it said: fences, a preamble and a thinking block are its habits.
  function parseJSON(text) {
    let t = String(text || "").replace(/<think>[\s\S]*?<\/think>/g, "").replace(/```(?:json)?/g, "");
    const start = t.search(/[{[]/);
    if (start < 0) return null;
    t = t.slice(start);
    const open = t[0], close = open === "{" ? "}" : "]";
    let depth = 0, inStr = false, esc = false;
    for (let i = 0; i < t.length; i++) {
      const c = t[i];
      if (inStr) { if (esc) esc = false; else if (c === "\\") esc = true; else if (c === '"') inStr = false; continue; }
      if (c === '"') inStr = true;
      else if (c === open) depth++;
      else if (c === close && --depth === 0) { try { return JSON.parse(t.slice(0, i + 1)); } catch (e) { return null; } }
    }
    return null;
  }
  const stripThinking = (text) => String(text || "").replace(/<think>[\s\S]*?<\/think>/g, "").trim();

  // ------------------------------------------------------------ storage
  // {get(key), set(key, value), remove(key)}, JSON values. The browser's is
  // localStorage, per origin; nothing in it leaves the device.
  function browserStorage(prefix) {
    prefix = prefix || "pyrtan.agents:";
    return {
      get(k) { try { return JSON.parse(localStorage.getItem(prefix + k) || "null"); } catch (e) { return null; } },
      set(k, v) { try { localStorage.setItem(prefix + k, JSON.stringify(v)); } catch (e) {} },
      remove(k) { try { localStorage.removeItem(prefix + k); } catch (e) {} },
    };
  }
  function memoryStorage() {
    const m = new Map();
    return { get: (k) => (m.has(k) ? JSON.parse(m.get(k)) : null), set: (k, v) => { m.set(k, JSON.stringify(v)); }, remove: (k) => { m.delete(k); }, map: m };
  }

  // ------------------------------------------------------------ memory
  const NOTES_MAX = 100, PROBES_MAX = 60, CHAT_MAX = 40, NOTE_LEN = 300;
  const words = (text) => new Set(String(text || "").toLowerCase().split(/[^\p{L}\p{N}]+/u).filter((w) => w.length > 2));
  class Memory {
    constructor(storage, key, clock) {
      this.storage = storage; this.key = key; this.clock = clock || (() => new Date());
      const d = storage.get(key) || {};
      this.notes = Array.isArray(d.notes) ? d.notes : []; this.probes = d.probes && typeof d.probes === "object" ? d.probes : {};
      this.verdicts = d.verdicts && typeof d.verdicts === "object" ? d.verdicts : {}; this.chat = Array.isArray(d.chat) ? d.chat : []; this.seq = d.seq || 0;
    }
    save() { this.storage.set(this.key, { notes: this.notes, probes: this.probes, verdicts: this.verdicts, chat: this.chat, seq: this.seq }); }
    now() { return Math.floor(this.clock().getTime() / 1000); }
    // source: "you" (the person), "agent" (the model asked to), "rule" (what happened: a refusal, a verdict).
    remember(text, source) {
      text = String(text || "").replace(/\s+/g, " ").trim().slice(0, NOTE_LEN);
      if (!text) return null;
      const same = this.notes.find((n) => n.text.toLowerCase() === text.toLowerCase());
      if (same) { same.at = this.now(); this.save(); return same; }
      const note = { id: ++this.seq, text, source: source || "agent", at: this.now() };
      this.notes.push(note);
      while (this.notes.length > NOTES_MAX) {
        const drop = this.notes.findIndex((n) => n.source !== "you");  // the person's own notes go last
        this.notes.splice(drop < 0 ? 0 : drop, 1);
      }
      this.save();
      return note;
    }
    forget(id) {
      if (id === "all") { const n = this.notes.length; this.notes = []; this.save(); return n; }
      const before = this.notes.length; this.notes = this.notes.filter((n) => String(n.id) !== String(id)); this.save(); return before - this.notes.length;
    }
    clear() { this.notes = []; this.probes = {}; this.verdicts = {}; this.chat = []; this.save(); }
    // The notes that share most words with the query; the person's and the newest first among equals.
    recall(query, n) {
      n = n || 6;
      const q = words(query);
      const scored = this.notes.map((note) => { let s = 0; for (const w of words(note.text)) if (q.has(w)) s++; return { note, s }; });
      const hits = scored.filter((x) => x.s > 0).sort((a, b) => b.s - a.s || (b.note.source === "you") - (a.note.source === "you") || b.note.at - a.note.at).map((x) => x.note);
      const recent = this.notes.slice().sort((a, b) => b.at - a.at).filter((x) => !hits.includes(x));
      return hits.concat(recent).slice(0, n);
    }
    probed(tree, entry) {
      const list = this.probes[tree] || (this.probes[tree] = []);
      const i = list.findIndex((p) => p.text.toLowerCase() === entry.text.toLowerCase());
      if (i >= 0) list.splice(i, 1);
      list.push(Object.assign({ at: this.now() }, entry));
      while (list.length > PROBES_MAX) list.shift();
      this.save();
    }
    tried(tree) { return (this.probes[tree] || []).map((p) => p.text); }
    known() { const out = []; for (const list of Object.values(this.probes)) for (const p of list) if (p.read && p.read !== "tie") out.push([p.text, p.read]); return out; }
    verdict(key, v, why) { this.verdicts[key] = { v, why: why || "", at: this.now() }; this.save(); }
    decided(key) { return this.verdicts[key] ? this.verdicts[key].v : null; }
    say(role, content) { this.chat.push({ role, content: String(content).slice(0, 2000), at: this.now() }); while (this.chat.length > CHAT_MAX) this.chat.shift(); this.save(); }
  }

  // ------------------------------------------------------------ skills
  // A skill is instructions for a job, the way a Claude skill is: a name, a
  // description, when it applies, and the text. The built-in ones are the
  // jobs themselves; a person's own are added to the job they name (or to
  // every job, "always"), and a chat skill can be called as /name.
  const JOBS = ["chat", "probe", "meaning", "trigger", "tree"];
  const BUILTIN = [
    { name: "probe", when: "probe", description: "Write sentences that test whether a tree reads what people say", instructions: "Write sentences a real person might type to get what this tree gives. Mix them: plain ones; other words for the same thing; other word orders; other values for each input, including unusual but real ones; a chatty or polite one; one with a small typo. Each sentence is one line, at most 15 words. Never repeat a sentence already tried." },
    { name: "meaning", when: "meaning", description: "Say what a value a decision met means", instructions: "A decision in a deterministic tree met a value none of its branches covers. Say what that value means to the person who asked, in one plain sentence they would want to read. If the tree can still give them an answer, it is a done; if it cannot, it is a fail with a short reason slug (lowercase words joined by dashes). Never invent facts the value does not carry: when you do not know what it means, say what was seen and that the source did not say more." },
    { name: "trigger", when: "trigger", description: "Write a trigger pattern for sentences a tree missed", instructions: "Write the smallest pattern that reads the sentences. A pattern is literal words (case and plurals ignored), [optional words], (one|or|other) for a required choice, and {slot} or {slot:kind} for an input; kinds: text (default), word, int, float, number, date, time, duration, host, url, email. A pattern needs at least one literal word, and every slot must be one of the tree's inputs. Prefer one general pattern over many narrow ones, but never one so loose it would take sentences meant for other trees." },
    { name: "tree", when: "tree", description: "Find the tree a sentence nobody answered needs", instructions: "A person typed a sentence no tree's trigger read. If one of the trees listed gives what they want, name it. If none does, describe in one line the tree that would, and name hosts that might answer it (an API is better than a page); a person checks them before any is listed." },
    { name: "chat", when: "chat", description: "Talk with the person about the swarm", instructions: "Be brief and plain. When the person asks something a tree can answer, ask the swarm instead of answering from what you know, and quote the tree's answer as it is. Say so when you are guessing." },
  ];
  const RESERVED = new Set(["help", "ask", "read", "probe", "gaps", "unanswered", "patrol", "remember", "forget", "memory", "skills", "suggestions", "clear"].concat(BUILTIN.map((s) => s.name)));
  function skillProblem(s) {
    if (!s || typeof s !== "object") return "a skill is a table";
    if (typeof s.name !== "string" || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(s.name)) return "a skill's name is lowercase letters, digits and dashes, at most 32";
    if (RESERVED.has(s.name)) return `${s.name} is taken by a command or a built-in skill`;
    if (!JOBS.concat(["always"]).includes(s.when || "chat")) return `when is one of ${JOBS.join(", ")}, always`;
    if (typeof s.instructions !== "string" || !s.instructions.trim() || s.instructions.length > 4000) return "a skill has instructions, at most 4000 characters";
    if (s.description !== undefined && (typeof s.description !== "string" || s.description.length > 200)) return "description is one line";
    return null;
  }
  // A skill written as Markdown with front matter (name, description, when), as Claude skills are; or JSON.
  function parseSkills(text) {
    text = String(text || "").trim();
    if (text.startsWith("{") || text.startsWith("[")) {
      const d = JSON.parse(text);
      return Array.isArray(d) ? d : Array.isArray(d.skills) ? d.skills : [d];
    }
    const m = /^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/.exec(text);
    if (!m) throw new Error("a skill file starts with --- front matter (name, description, when) or is JSON");
    const s = { instructions: m[2].trim() };
    for (const line of m[1].split("\n")) { const kv = /^(\w+)\s*:\s*(.*)$/.exec(line.trim()); if (kv) s[kv[1]] = kv[2].replace(/^["']|["']$/g, "").trim(); }
    return [s];
  }
  class Skills {
    constructor(storage, key) { this.storage = storage; this.key = key || "skills"; const d = storage.get(this.key); this.mine = Array.isArray(d) ? d.filter((s) => !skillProblem(s)) : []; }
    save() { this.storage.set(this.key, this.mine); }
    list() { return BUILTIN.map((s) => Object.assign({ builtin: true, on: true }, s)).concat(this.mine.map((s) => Object.assign({ builtin: false, on: s.on !== false, when: s.when || "chat" }, s))); }
    get(name) { return this.list().find((s) => s.name === name) || null; }
    add(s) {
      const skill = { name: String(s.name || "").trim().toLowerCase(), description: String(s.description || "").trim(), when: s.when || "chat", instructions: String(s.instructions || "").trim(), on: s.on !== false };
      const bad = skillProblem(skill);
      if (bad) throw new Error(bad);
      this.mine = this.mine.filter((x) => x.name !== skill.name).concat([skill]);
      this.save();
      return skill;
    }
    remove(name) { const before = this.mine.length; this.mine = this.mine.filter((s) => s.name !== name); this.save(); return before !== this.mine.length; }
    toggle(name, on) { const s = this.mine.find((x) => x.name === name); if (s) { s.on = on; this.save(); } return !!s; }
    import(text) { return parseSkills(text).map((s) => this.add(s)); }
    export() { return JSON.stringify({ "pyrtan-skills": 1, skills: this.mine }, null, 2); }
    // The instructions for a job: the built-in one, then the person's that are on.
    for(job) {
      const own = BUILTIN.find((s) => s.when === job);
      const extra = this.mine.filter((s) => s.on !== false && (s.when === job || s.when === "always"));
      return [own ? own.instructions : ""].concat(extra.map((s) => `Skill ${s.name}${s.description ? " (" + s.description + ")" : ""}: ${s.instructions}`)).filter(Boolean).join("\n\n");
    }
  }

  // ------------------------------------------------------------ reading, as the swarm reads
  function candidatesOf(caps, extra) {
    const out = [];
    for (const t of caps.slice().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
      let lang;
      try { lang = Pyrtan.intent.language(t.language || null); } catch (e) { lang = undefined; }
      const fixed = t.trigger_inputs || {};
      const patterns = (t.triggers || []).concat(extra && extra.tree === t.name ? [extra.pattern] : []);
      for (const p of patterns) { try { out.push([t.name, new Pyrtan.intent.Trigger(p, fixed[p] || {}, lang)]); } catch (e) { /* a pattern the page cannot read is the swarm's problem */ } }
    }
    return out;
  }
  function brief(t) {
    const inputs = Object.entries(t.inputs || {}).map(([n, s]) => `${n} (${s.type || "str"}${s.required ? ", required" : ""}${s.description ? ": " + s.description : ""})`);
    return [`Tree ${t.name}: ${t.description || "no description"}`, `Answers to: ${(t.triggers || []).map((p) => JSON.stringify(p)).join(", ") || "nothing yet"}`, `Inputs: ${inputs.join("; ") || "none"}`].join("\n");
  }
  const pathOf = (out) => (out && out.trace && out.trace.steps ? out.trace.steps.filter((s) => !s.depth).map((s) => s.node) : []);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const slug = (text, n) => String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, n || 40).replace(/-+$/, "");

  // ------------------------------------------------------------ the agent
  class Agent {
    // opts.hub(method, path, body) -> payload: the swarm, through the console's own calls.
    // opts.raw(name) -> a tree's table, when the page holds it (a browser that is its own hub).
    constructor(opts) {
      opts = opts || {};
      if (!opts.engine) throw new Error("an agent needs an engine");
      if (!opts.hub) throw new Error("an agent needs a hub to ask");
      this.name = slug(opts.name || "scout", 32) || "scout";
      this.engine = opts.engine; this.hub = opts.hub; this.raw = opts.raw || (() => null);
      this.storage = opts.storage || memoryStorage();
      this.clock = opts.clock || (() => new Date());
      this.memory = new Memory(this.storage, `agent:${this.name}`, this.clock);
      this.skills = opts.skills || new Skills(this.storage);
      this.limits = Object.assign({ probes: 6, runs: 4, gaps: 6, unanswered: 4, actions: 3, pause: 300 }, opts.limits || {});
      this.onEvent = opts.onEvent || (() => {});
      this.caps = null; this.suggestions = []; this.stopping = false; this.seq = 0;
    }
    by() { return { agent: this.name, model: String(this.engine.model || ""), engine: String(this.engine.name || "") }; }
    emit(type, data) { try { this.onEvent(Object.assign({ type, agent: this.name }, data || {})); } catch (e) {} }
    stop() { this.stopping = true; }

    async trees(fresh) {
      if (!this.caps || fresh) { const c = await this.hub("GET", "capabilities"); this.caps = (c && c.trees) || []; }
      return this.caps;
    }
    tree(name) { return (this.caps || []).find((t) => t.name === name) || null; }
    // How the swarm reads a sentence, here: {tree (null for none, "tie" for a tie), inputs}.
    read(text, extra) {
      const r = Pyrtan.read(text, candidatesOf(this.caps || [], extra), this.clock());
      const w = r.winner;
      return { tree: w ? w.tree : r.matches.length ? "tie" : null, inputs: w ? w.inputs : {}, cleaned: r.cleaned, ties: w ? [] : r.matches.map((m) => m.tree) };
    }

    // One call to the model for a job, with the job's skills and what it remembers.
    async think(job, task, opts) {
      opts = opts || {};
      const notes = this.memory.recall(task, 6);
      const system = [
        `You are ${this.name}, an agent beside a Pyrtan swarm: a set of deterministic decision trees that answer sentences. You are a model (${this.engine.model || "open weights"}); the trees are not, and you never answer for them. You suggest; a person decides.`,
        this.skills.for(job),
        notes.length ? "What you remember:\n" + notes.map((n) => `- ${n.text}`).join("\n") : "",
        opts.json ? "Reply with JSON only, no other text." : "",
      ].filter(Boolean).join("\n\n");
      const messages = [{ role: "system", content: system }].concat(opts.history || [], [{ role: "user", content: task }]);
      this.emit("thinking", { job });
      const text = await this.engine.complete(messages, { json: !!opts.json, temperature: opts.temperature, maxTokens: opts.maxTokens });
      return opts.json ? parseJSON(text) : stripThinking(text);
    }

    // A suggestion that passed the rules, kept for the person to accept or dismiss.
    offer(s, preview) {
      const key = Pyrtan.grow.key(s);
      if (this.memory.decided(key)) return null;
      const found = this.suggestions.find((x) => x.key === key && x.status === "new");
      if (found) return found;
      const item = { id: ++this.seq, key, status: "new", suggestion: s, preview: preview || null };
      this.suggestions.push(item);
      this.emit("suggestion", { item });
      return item;
    }
    refuse(what, why) { this.memory.remember(`refused: ${what}: ${why}`, "rule"); this.emit("refused", { what, why }); return { refused: what, why }; }
    decide(id, verdict, why) {
      const item = this.suggestions.find((x) => x.id === id);
      if (!item) return null;
      item.status = verdict;
      this.memory.verdict(item.key, verdict, why);
      const s = item.suggestion;
      const what = s.kind === "branch" ? `the meaning "${s.meaning.say}" for ${s.tree}.${s.node} = ${JSON.stringify(s.observed)}` : s.kind === "trigger" ? `the pattern "${s.pattern}" for ${s.tree}` : `a new tree for "${s.text}"`;
      this.memory.remember(`the person ${verdict} ${what}${why ? ": " + why : ""}`, "rule");
      return item;
    }
    async send(items) {
      items = (items || this.suggestions.filter((x) => x.status === "accepted")).filter(Boolean);
      if (!items.length) return { kept: 0, refused: [] };
      const out = await this.hub("POST", "suggestions", { suggestions: items.map((x) => x.suggestion) });
      for (const x of items) if (!(out.refused || []).some((r) => r.index === items.indexOf(x))) x.sent = true;
      return out;
    }

    // ---- probing a tree
    async probe(name, count) {
      await this.trees();
      const t = this.tree(name);
      if (!t) throw new Error(`no tree named ${name} here`);
      count = Math.max(1, Math.min(Number(count) || this.limits.probes, 12));
      const tried = this.memory.tried(name);
      const task = `${brief(t)}\n\n${tried.length ? "Already tried, do not repeat:\n" + tried.slice(-20).map((s) => "- " + s).join("\n") + "\n\n" : ""}Write ${count} new sentences for this tree. Reply as {"sentences": ["...", "..."]}.`;
      const got = await this.think("probe", task, { json: true, temperature: 0.9 });
      const seen = new Set(tried.map((s) => s.toLowerCase()));
      const sentences = (got && Array.isArray(got.sentences) ? got.sentences : []).filter((s) => typeof s === "string").map((s) => s.trim()).filter((s) => s && s.length <= 200 && !seen.has(s.toLowerCase()) && seen.add(s.toLowerCase())).slice(0, count);
      if (!sentences.length) { this.emit("note", { text: `the model wrote no usable sentences for ${name}` }); return { tree: name, results: [], suggestions: [] }; }
      const results = [], gaps = [];
      let runs = 0;
      for (const text of sentences) {
        if (this.stopping) break;
        const r = Object.assign({ text }, this.read(text));
        r.read = r.tree; delete r.tree;
        if (r.read === name && runs < this.limits.runs) {
          runs++;
          let out;
          try { out = await this.hub("POST", "intent", { text }); } catch (e) { out = { status: "error", reason: "unreachable", say: String(e.message || e) }; }
          Object.assign(r, { status: out.status, reason: out.reason || null, say: out.say || "", path: pathOf(out) });
          for (const g of (out.trace && out.trace.gaps) || []) if (g.tree === name) gaps.push(g);
          if (this.limits.pause) await sleep(this.limits.pause);
        }
        this.memory.probed(name, { text, read: r.read, status: r.status || null, reason: r.reason || null });
        results.push(r);
        this.emit("probe", { tree: name, result: r });
      }
      const offered = [];
      const unread = results.filter((r) => r.read === null).map((r) => r.text);
      for (const r of results) if (r.read && r.read !== name) this.memory.remember(`"${r.text}" was written for ${name} but reads as ${r.read === "tie" ? "a tie between " + r.ties.join(" and ") : r.read}`, "rule");
      if (unread.length && !this.stopping) offered.push(...await this.triggerFor(name, unread));
      for (const g of gaps) { if (this.stopping) break; const m = await this.meaning(g); if (m && m.id) offered.push(m); }
      return { tree: name, results, suggestions: offered };
    }

    // ---- a pattern for sentences a tree missed, kept only if the reading says it works
    async triggerFor(name, sentences) {
      await this.trees();
      const t = this.tree(name);
      if (!t) return [];
      const inputs = Object.keys(t.inputs || {});
      const task = `${brief(t)}\n\nThese sentences were meant for this tree, but no trigger reads them:\n${sentences.map((s) => "- " + s).join("\n")}\n\nWrite at most 2 patterns that would. Slots may only be: ${inputs.map((n) => "{" + n + "}").join(", ") || "none (write literal words only)"}. Reply as {"patterns": [{"pattern": "...", "covers": ["...the sentences it reads..."]}], "why": "..."}.`;
      const got = await this.think("trigger", task, { json: true, temperature: 0.3 });
      const out = [];
      for (const p of (got && Array.isArray(got.patterns) ? got.patterns : []).slice(0, 2)) {
        const pattern = p && typeof p.pattern === "string" ? p.pattern.trim() : "";
        const why = got && typeof got.why === "string" ? got.why.slice(0, 500) : "";
        const checked = this.checkTrigger(name, pattern, sentences);
        if (checked.why) { out.push(this.refuse(`the pattern "${pattern}" for ${name}`, checked.why)); continue; }
        const s = { kind: "trigger", tree: name, pattern, covers: checked.covers, why, by: this.by(), evidence: checked.covers.map((text) => ({ text, read: null, inputs: checked.inputs[text] })) };
        const bad = Pyrtan.grow.problem(s);
        if (bad) { out.push(this.refuse(`the pattern "${pattern}" for ${name}`, bad)); continue; }
        const item = this.offer(s, { text: `[[triggers]]\npattern = ${JSON.stringify(pattern)}`, reads: checked.inputs });
        if (item) out.push(item);
      }
      return out;
    }
    // The rules a pattern must pass: it compiles, names only the tree's inputs, reads at least one sentence to the tree, and takes no sentence from another tree.
    checkTrigger(name, pattern, sentences) {
      const t = this.tree(name);
      if (!pattern) return { why: "no pattern" };
      let trig;
      try { trig = new Pyrtan.intent.Trigger(pattern, {}, Pyrtan.intent.language(t.language || null)); } catch (e) { return { why: e.message }; }
      const unknown = Object.keys(trig.slots).filter((s) => !(s in (t.inputs || {})));
      if (unknown.length) return { why: `it names ${unknown.join(", ")}, which ${name} does not take as inputs` };
      if ((t.triggers || []).includes(pattern)) return { why: `${name} answers to it already` };
      const extra = { tree: name, pattern };
      const covers = [], inputs = {};
      for (const s of sentences) { const r = this.read(s, extra); if (r.tree === name) { covers.push(s); inputs[s] = r.inputs; } }
      if (!covers.length) return { why: "with it, none of the sentences reads to " + name };
      for (const [text, tree] of this.memory.known()) {
        const r = this.read(text, extra);
        if (r.tree !== tree && this.read(text).tree === tree) return { why: `it would take "${text}" from ${tree}` };
      }
      return { covers, inputs };
    }

    // ---- what a gap's value means
    async meaning(gap) {
      await this.trees();
      const t = this.tree(gap.tree);
      if (!t) return null;
      const base = { kind: "branch", tree: gap.tree, node: gap.node, on: gap.on || null, observed: gap.observed === undefined ? null : gap.observed };
      if (this.memory.decided(Pyrtan.grow.key(base)) || this.suggestions.some((x) => x.key === Pyrtan.grow.key(base))) return null;
      const raw = this.raw(gap.tree);
      const draft = raw ? Pyrtan.grow.draftBranch(raw, gap) : null;
      if (raw && !draft) return null;  // a branch covers it now
      if (draft && draft.list === "table") return null;  // a lookup's gap asks for a row, which has no node for a meaning to go in
      let context = "";
      if (raw && raw.nodes && raw.nodes[gap.node]) {
        const lines = (raw.nodes[gap.node].branches || []).map((b) => {
          const target = raw.nodes[b.then || b.else] || {};
          return `- ${b.else ? "anything else" : JSON.stringify(b.when)} -> ${b.then || b.else} (${target.kind || "?"}${target.say ? ': "' + target.say + '"' : ""})`;
        });
        context = `\nIts branches now:\n${lines.join("\n")}`;
      }
      const task = `${brief(t)}\n\nThe decision "${gap.node}" reads ${gap.on} and met ${JSON.stringify(gap.observed)}, which no branch covers.${context}\n\nSay what ${JSON.stringify(gap.observed)} means here. The say may use {{ inputs.NAME }} for these inputs: ${Object.keys(t.inputs || {}).join(", ") || "none"}${raw ? ", and {{ state.NAME }} for what the tree observed" : ""}; nothing else. Reply as {"kind": "done" or "fail", "say": "...", "reason": "a-slug", "why": "..."}.`;
      const got = await this.think("meaning", task, { json: true, temperature: 0.3 });
      if (!got || typeof got !== "object") return this.refuse(`a meaning for ${gap.tree}.${gap.node} = ${JSON.stringify(gap.observed)}`, "the model's reply was not JSON");
      const meaning = { kind: got.kind === "done" ? "done" : "fail", say: typeof got.say === "string" ? got.say.trim().slice(0, 300) : "" };
      if (meaning.kind === "fail") meaning.reason = slug(got.reason || got.say, 40) || "unhandled";
      const s = Object.assign({}, base, { meaning, why: typeof got.why === "string" ? got.why.slice(0, 500) : "", by: this.by(), evidence: [{ gap: { node: gap.node, on: gap.on, observed: gap.observed } }] });
      const bad = Pyrtan.grow.problem(s);
      if (bad) return this.refuse(`the meaning "${meaning.say}" for ${gap.tree}.${gap.node}`, bad);
      let preview = null;
      if (draft) {
        try { new Pyrtan.Tree(Pyrtan.grow.growTree(raw, draft, meaning)); } catch (e) { return this.refuse(`the meaning for ${gap.tree}.${gap.node}`, `the tree would not load: ${e.message}`); }
        preview = { draft, text: `# in [nodes.${draft.node}], before the else:\n  ${draft.branch}\n\n[nodes.${draft.then}]\nkind = "${meaning.kind}"${meaning.reason ? `\nreason = "${meaning.reason}"` : ""}\nsay = ${JSON.stringify(meaning.say)}` };
      }
      return this.offer(s, preview);
    }
    async meanings(limit) {
      const g = await this.hub("GET", "gaps");
      const out = [];
      for (const gap of ((g && g.gaps) || []).slice(0, limit || this.limits.gaps)) { if (this.stopping) break; const m = await this.meaning(gap); if (m) out.push(m); }
      return out;
    }

    // ---- sentences nobody answered
    async unanswered(limit) {
      await this.trees();
      const u = await this.hub("GET", "unanswered");
      const out = [];
      for (const e of ((u && u.unanswered) || []).slice(0, limit || this.limits.unanswered)) {
        if (this.stopping) break;
        const asTree = { kind: "tree", text: e.text };
        if (this.memory.decided(Pyrtan.grow.key(asTree))) continue;
        const list = (this.caps || []).map((t) => `- ${t.name}: ${t.description || ""}`).join("\n");
        const got = await this.think("tree", `The trees:\n${list || "(none)"}\n\nA person typed: ${JSON.stringify(e.text)} (${e.count || 1} times). No trigger read it. Reply as {"tree": "a name above, or null", "description": "if null: one line for the tree that would", "hosts": ["..."], "why": "..."}.`, { json: true, temperature: 0.3 });
        if (!got) { out.push(this.refuse(`a tree for "${e.text}"`, "the model's reply was not JSON")); continue; }
        if (got.tree && this.tree(String(got.tree))) { out.push(...await this.triggerFor(String(got.tree), [e.text])); continue; }
        const s = { kind: "tree", text: e.text, description: typeof got.description === "string" ? got.description.trim().slice(0, 200) : "", hosts_hint: (Array.isArray(got.hosts) ? got.hosts : []).filter((h) => typeof h === "string").map((h) => h.trim().slice(0, 100)).slice(0, 5), why: typeof got.why === "string" ? got.why.slice(0, 500) : "", by: this.by(), evidence: [{ text: e.text, count: e.count || 1 }] };
        const bad = Pyrtan.grow.problem(s);
        if (bad) { out.push(this.refuse(`a tree for "${e.text}"`, bad)); continue; }
        const p = Pyrtan.propose(e.text);
        const item = this.offer(s, { text: `${p.scaffold}\n# name ${p.name}, answering "${p.trigger}"${s.description ? "\n# " + s.description : ""}` });
        if (item) out.push(item);
      }
      return out;
    }

    // ---- everything, once: each tree probed, then the gaps, then the unanswered
    async patrol(opts) {
      opts = opts || {};
      this.stopping = false;
      await this.trees(true);
      const names = (opts.trees || this.caps.map((t) => t.name)).slice(0, opts.maxTrees || 12);
      const report = { agent: this.name, model: this.by().model, started: Math.floor(this.clock().getTime() / 1000), trees: [], suggestions: [], refused: [] };
      const take = (list) => { for (const x of list || []) { if (x && x.refused) report.refused.push(x); else if (x) report.suggestions.push(x); } };
      for (const name of names) {
        if (this.stopping) break;
        this.emit("patrol", { step: "probe", tree: name });
        try { const p = await this.probe(name, opts.probes); report.trees.push({ tree: name, results: p.results }); take(p.suggestions); }
        catch (e) { report.trees.push({ tree: name, error: String(e.message || e) }); }
      }
      if (!this.stopping) { this.emit("patrol", { step: "gaps" }); try { take(await this.meanings()); } catch (e) { report.error = String(e.message || e); } }
      if (!this.stopping) { this.emit("patrol", { step: "unanswered" }); try { take(await this.unanswered()); } catch (e) { report.error = String(e.message || e); } }
      report.stopped = this.stopping; this.stopping = false;
      const tally = report.trees.reduce((a, t) => { for (const r of t.results || []) { a.sentences++; if (r.read === t.tree) a.read++; if (r.read === null) a.unread++; if (r.read && r.read !== t.tree) a.misread++; if (r.reason === "gap") a.gaps++; } return a; }, { sentences: 0, read: 0, unread: 0, misread: 0, gaps: 0 });
      report.tally = tally;
      this.memory.remember(`patrol: ${tally.sentences} sentences over ${report.trees.length} trees; ${tally.read} read, ${tally.unread} unread, ${tally.misread} misread, ${tally.gaps} gaps; ${report.suggestions.length} suggestions`, "rule");
      this.emit("patrol", { step: "done", report });
      return report;
    }

    // ---- talking with the person
    // "/command args" is done by rules; anything else goes to the model,
    // which may end its reply with lines the agent acts on: ASK: a sentence
    // for the swarm, REMEMBER: a note, PROBE: a tree.
    async chat(text) {
      text = String(text || "").trim();
      if (!text) return { say: "" };
      if (text.startsWith("/")) return this.command(text);
      return this.converse(text, null);
    }
    async converse(text, skill) {
      await this.trees();
      const job = "chat";
      const list = (this.caps || []).slice(0, 30).map((t) => `- ${t.name}: ${t.description || ""} (e.g. ${JSON.stringify((t.triggers || [])[0] || "")})`).join("\n");
      const task = [
        `The trees this swarm holds:\n${list || "(none yet)"}`,
        skill ? `Use the skill ${skill.name} now: ${skill.instructions}` : "",
        `To act, end your reply with lines of their own (at most ${this.limits.actions}):\nASK: <a sentence for the swarm, when a tree can answer it>\nREMEMBER: <a short note worth keeping across conversations>\nPROBE: <a tree name, to test it with sentences of your own>`,
        `The person says: ${text}`,
      ].filter(Boolean).join("\n\n");
      const history = this.memory.chat.slice(-12).map((m) => ({ role: m.role === "you" ? "user" : "assistant", content: m.content }));
      this.memory.say("you", text);
      const reply = await this.think(job, task, { history, temperature: 0.5, maxTokens: 400 });
      const actions = [];
      const say = reply.split("\n").filter((line) => {
        const m = /^\s*(ASK|REMEMBER|PROBE)\s*:\s*(.+?)\s*$/i.exec(line);
        if (!m) return true;
        if (actions.length < this.limits.actions) actions.push({ kind: m[1].toLowerCase(), input: m[2].replace(/^["']|["']$/g, "") });
        return false;
      }).join("\n").trim();
      const answers = [];
      for (const a of actions) {
        if (a.kind === "remember") { a.result = this.memory.remember(a.input, "agent"); }
        else if (a.kind === "ask") {
          try { const out = await this.hub("POST", "intent", { text: a.input }); a.result = { status: out.status, tree: out.tree, reason: out.reason || null, say: out.say || "", path: pathOf(out) }; } catch (e) { a.result = { status: "error", say: String(e.message || e) }; }
          answers.push(`${JSON.stringify(a.input)} -> ${a.result.tree || "no tree"} (${a.result.status}${a.result.reason ? ", " + a.result.reason : ""}): ${a.result.say}`);
        } else if (a.kind === "probe") {
          if (this.tree(a.input)) { const p = await this.probe(a.input); a.result = { probed: p.results.length, suggestions: p.suggestions.filter((x) => x && x.id).length }; }
          else a.result = { error: `no tree named ${a.input}` };
        }
        this.emit("action", { action: a });
      }
      let final = say;
      if (answers.length) {
        final = await this.think(job, `You asked the swarm, and its trees answered (these are their words, not yours):\n${answers.join("\n")}\n\nThe person had said: ${text}\nYou had said: ${say || "(nothing)"}\nNow answer the person in one to three sentences. Quote a tree's answer as it is; if no tree answered, say so.`, { history, temperature: 0.3, maxTokens: 300 });
        final = final.split("\n").filter((l) => !/^\s*(ASK|REMEMBER|PROBE)\s*:/i.test(l)).join("\n").trim();
      }
      this.memory.say("agent", final || "(nothing)");
      return { say: final, actions };
    }
    async command(text) {
      const [, cmd, rest] = /^\/(\S+)\s*([\s\S]*)$/.exec(text) || [null, "", ""];
      const arg = rest.trim();
      const done = (say, extra) => Object.assign({ say, command: cmd }, extra || {});
      switch (cmd) {
        case "help": return done(["/ask SENTENCE: ask the swarm (a tree answers, not me)", "/read SENTENCE: how the swarm reads it, without running anything", "/probe TREE [N]: test a tree with sentences of my own", "/gaps: say what each gap's value means", "/unanswered: find the tree each unanswered sentence needs", "/patrol: all of it, every tree", "/suggestions: what I have suggested", "/remember NOTE, /memory, /forget ID|all: what I keep", "/skills: my skills; /NAME TEXT uses a chat skill of yours", "/clear: forget this conversation (notes stay)"].join("\n"));
        case "ask": {
          if (!arg) return done("Ask what? /ask what's the weather in Oslo");
          const out = await this.hub("POST", "intent", { text: arg });
          return done(`${out.tree || "no tree"} (${out.status}${out.reason ? ", " + out.reason : ""}): ${out.say || ""}`, { outcome: out });
        }
        case "read": {
          await this.trees();
          const r = this.read(arg);
          return done(r.tree === null ? `No trigger reads ${JSON.stringify(arg)} (read as ${JSON.stringify(r.cleaned)}).` : r.tree === "tie" ? `A tie between ${r.ties.join(" and ")}.` : `${r.tree}, with ${JSON.stringify(r.inputs)} (read as ${JSON.stringify(r.cleaned)}).`, { reading: r });
        }
        case "probe": {
          await this.trees();
          const [name, n] = arg.split(/\s+/);
          if (!name || !this.tree(name)) return done(`Probe which tree? ${(this.caps || []).map((t) => t.name).join(", ")}`);
          const p = await this.probe(name, n);
          const read = p.results.filter((r) => r.read === name).length;
          return done(`${p.results.length} sentences for ${name}: ${read} read to it, ${p.results.filter((r) => r.read === null).length} read to nothing, ${p.results.filter((r) => r.read && r.read !== name).length} to something else. ${p.suggestions.filter((x) => x && x.id).length} suggestions.`, { probe: p });
        }
        case "gaps": { const m = await this.meanings(); return done(`${m.filter((x) => x && x.id).length} meanings suggested, ${m.filter((x) => x && x.refused).length} refused by the rules.`, { suggestions: m }); }
        case "unanswered": { const m = await this.unanswered(); return done(`${m.filter((x) => x && x.id).length} suggestions, ${m.filter((x) => x && x.refused).length} refused by the rules.`, { suggestions: m }); }
        case "patrol": { const r = await this.patrol(); return done(`Patrolled ${r.trees.length} trees: ${r.tally.sentences} sentences, ${r.tally.read} read, ${r.tally.unread} unread, ${r.tally.misread} misread, ${r.tally.gaps} gaps. ${r.suggestions.length} suggestions, ${r.refused.length} refused.`, { report: r }); }
        case "suggestions": { const open = this.suggestions.filter((x) => x.status === "new"); return done(open.length ? open.map((x) => `#${x.id} ${x.suggestion.kind} ${x.suggestion.tree || x.suggestion.text}: ${x.suggestion.pattern || (x.suggestion.meaning && x.suggestion.meaning.say) || x.suggestion.description || ""}`).join("\n") : "Nothing waiting."); }
        case "remember": { const n = this.memory.remember(arg, "you"); return done(n ? `Kept #${n.id}.` : "Remember what?"); }
        case "memory": { const notes = this.memory.notes.slice(-30); return done(notes.length ? notes.map((n) => `#${n.id} (${n.source}) ${n.text}`).join("\n") : "Nothing kept yet."); }
        case "forget": { const n = this.memory.forget(arg || "all"); return done(`Forgot ${n} note${n === 1 ? "" : "s"}.`); }
        case "clear": { this.memory.chat = []; this.memory.save(); return done("This conversation is forgotten; the notes are kept."); }
        case "skills": return done(this.skills.list().map((s) => `${s.builtin ? "built-in" : s.on ? "on" : "off"}  ${s.name} (${s.when}): ${s.description || ""}`).join("\n"));
        default: {
          const skill = this.skills.get(cmd);
          if (skill && !skill.builtin && skill.on && (skill.when === "chat" || skill.when === "always")) return this.converse(arg || skill.description || skill.name, skill);
          return done(`No command or chat skill /${cmd}. /help lists them.`);
        }
      }
    }
  }

  return { Agent, Memory, Skills, WebLLMEngine, OpenAIEngine, ScriptedEngine, MODELS, DEFAULT_MODEL, WEBLLM_URL, BUILTIN, JOBS, parseJSON, parseSkills, skillProblem, browserStorage, memoryStorage, candidatesOf };
});
