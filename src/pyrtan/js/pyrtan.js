/* pyrtan.js: the tree runtime, in JavaScript.
 *
 * The same tree files the Python runtime reads (as JSON, the TOML's own
 * table), walked the same way: observe, act, decide, set, each, delegate,
 * ask, done, fail; the same templates, conditions and extraction; the same
 * trace. tests/conformance holds the vectors both runtimes must agree on.
 *
 * What differs, and is written down here rather than hidden:
 * - `float` in a condition's `type` means any number here; JSON has no
 *   distinct float, so 4.0 and 4 are the same value.
 * - CSS extraction uses the host's selector engine (DOMParser) plus
 *   `:contains(text)`; in a host without a DOM, a css extract is MISSING.
 * - Regular expressions are the host's; the subset trees use is common.
 * - There is no intent matching here: a member runs the tree its hub
 *   already chose. `delegate` goes to the resolver (the hub).
 *
 * No model. No dependencies. Runs in a browser as `Pyrtan` and in node.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Pyrtan = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_STEPS = 500, MAX_DEPTH = 8, DEFAULT_TIMEOUT = 10.0, DEFAULT_RETRIES = 1;
  const MISSING = Object.freeze({ toString: () => "<missing>", missing: true });
  const isMissing = (v) => v === MISSING;
  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const isBool = (v) => typeof v === "boolean";
  const isNum = (v) => typeof v === "number" && !Number.isNaN(v);

  class TemplateError extends Error {}
  class TreeError extends Error {}
  class TransportError extends Error {}
  class ConditionError extends Error {}

  // ------------------------------------------------------------ python-ish text
  // Summaries and the chosen branch in a trace read like the Python runtime's,
  // so both runtimes' traces compare as text.
  function pyrepr(v) {
    if (v === null || v === undefined || isMissing(v)) return "None";
    if (isBool(v)) return v ? "True" : "False";
    if (isNum(v)) return Number.isInteger(v) ? String(v) : String(v);
    if (typeof v === "string") {
      const q = v.includes("'") && !v.includes('"') ? '"' : "'";
      const body = v.replace(/\\/g, "\\\\").replace(new RegExp(q, "g"), "\\" + q).replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t");
      return q + body + q;
    }
    if (Array.isArray(v)) return "[" + v.map(pyrepr).join(", ") + "]";
    if (isObj(v)) return "{" + Object.keys(v).map((k) => pyrepr(k) + ": " + pyrepr(v[k])).join(", ") + "}";
    return String(v);
  }
  function pyJson(v) {  // json.dumps(v, ensure_ascii=False): ", " and ": " separators
    if (v === null || v === undefined || isMissing(v)) return "null";
    if (isBool(v)) return v ? "true" : "false";
    if (isNum(v)) return Number.isInteger(v) ? String(v) : String(v);
    if (typeof v === "string") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(pyJson).join(", ") + "]";
    if (isObj(v)) return "{" + Object.keys(v).map((k) => JSON.stringify(k) + ": " + pyJson(v[k])).join(", ") + "}";
    return JSON.stringify(v);
  }
  function brief(v, limit = 80) {
    if (isMissing(v)) return "<missing>";
    const text = typeof v === "string" ? v : pyrepr(v);
    return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
  }
  function short(v, limit = 200) {
    if (typeof v === "string" && v.length > limit) return v.slice(0, limit) + "…";
    if (Array.isArray(v) || isObj(v)) { const t = pyJson(v); if (t.length > limit) return t.slice(0, limit) + "…"; }
    return v;
  }
  const condText = (cond) => Object.keys(cond).map((k) => `${k} ${pyrepr(cond[k])}`).join(" and ");
  // A tree's regular expressions are written for Python; the inline flags
  // (?i) (?s) (?m) at the start become JavaScript flags.
  function pyRegex(pattern, flags = "") {
    let src = String(pattern), fl = flags;
    const m = /^\(\?([imsx]+)\)/.exec(src);
    if (m) { src = src.slice(m[0].length); for (const f of m[1]) if ("ims".includes(f) && !fl.includes(f)) fl += f; }
    return new RegExp(src, fl);
  }

  // ------------------------------------------------------------ templates
  const PLACEHOLDER = /\{\{\s*([^{}]*?)\s*\}\}/g;
  const PATH_TOKEN = /([A-Za-z_][A-Za-z0-9_\-]*)|\[(-?\d+)\]|\["([^"]*)"\]|\['([^']*)'\]/y;

  function resolve(path, scope) {
    path = String(path).trim();
    if (!path) return MISSING;
    let pos = 0, cur = scope;
    while (pos < path.length) {
      if (path[pos] === ".") { pos++; continue; }
      PATH_TOKEN.lastIndex = pos;
      const m = PATH_TOKEN.exec(path);
      if (!m) throw new TemplateError(`bad path ${pyrepr(path)} at offset ${pos}`);
      pos = PATH_TOKEN.lastIndex;
      const [, name, index, dq, sq] = m;
      if (name !== undefined) cur = getKey(cur, name);
      else if (index !== undefined) cur = getIndex(cur, parseInt(index, 10));
      else cur = getKey(cur, dq !== undefined ? dq : sq);
      if (isMissing(cur)) return MISSING;
    }
    return cur;
  }
  function getKey(cur, key) {
    if (isObj(cur)) return Object.prototype.hasOwnProperty.call(cur, key) ? cur[key] : MISSING;
    if (Array.isArray(cur) && /^\d+$/.test(key)) return getIndex(cur, parseInt(key, 10));
    return MISSING;
  }
  function getIndex(cur, index) {
    if (Array.isArray(cur) || typeof cur === "string") {
      const i = index < 0 ? cur.length + index : index;
      if (i < 0 || i >= cur.length) return MISSING;
      return typeof cur === "string" ? cur[i] : cur[i];
    }
    return MISSING;
  }
  const pyQuote = (s) => encodeURIComponent(String(s)).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
  const pyStr = (v) => (typeof v === "string" ? v : isBool(v) ? (v ? "True" : "False") : v === null || v === undefined ? "None" : Array.isArray(v) || isObj(v) ? pyrepr(v) : String(v));
  const FILTERS = {
    url: (v) => pyQuote(pyStr(v)),
    json: (v) => pyJson(v),
    int: (v) => { const n = Number(typeof v === "string" ? v.trim() : v); if (Number.isNaN(n)) throw new TemplateError(`int: ${pyrepr(v)}`); return Math.trunc(n); },
    float: (v) => { const n = Number(typeof v === "string" ? v.trim() : v); if (Number.isNaN(n)) throw new TemplateError(`float: ${pyrepr(v)}`); return n; },
    str: (v) => pyStr(v),
    lower: (v) => pyStr(v).toLowerCase(),
    upper: (v) => pyStr(v).toUpperCase(),
    strip: (v) => pyStr(v).trim(),
    first: (v) => ((Array.isArray(v) || typeof v === "string") && v.length ? v[0] : MISSING),
    last: (v) => ((Array.isArray(v) || typeof v === "string") && v.length ? v[v.length - 1] : MISSING),
    length: (v) => (Array.isArray(v) || typeof v === "string" ? v.length : isObj(v) ? Object.keys(v).length : MISSING),
    title: (v) => pyStr(v).toLowerCase().replace(/(^|[^a-z0-9'])([a-z])/gi, (m, pre, c) => pre + c.toUpperCase()).replace(/'([a-z])/gi, (m, c) => "'" + c.toUpperCase()),
  };
  function renderValue(expr, scope) {
    const parts = expr.split("|").map((p) => p.trim());
    let value = resolve(parts[0], scope);
    for (const name of parts.slice(1)) {
      if (!(name in FILTERS)) throw new TemplateError(`unknown filter ${pyrepr(name)} in {{ ${expr} }}`);
      if (isMissing(value)) continue;
      value = FILTERS[name](value);
    }
    return value;
  }
  function render(template, scope, strict = true) {
    if (typeof template === "string") {
      const trimmed = template.trim();
      const whole = /^\{\{\s*([^{}]*?)\s*\}\}$/.exec(trimmed);
      if (whole && trimmed === template) {
        const value = renderValue(whole[1], scope);
        if (isMissing(value)) { if (strict) throw new TemplateError(`${template} resolves to nothing`); return null; }
        return value;
      }
      return template.replace(PLACEHOLDER, (m, expr) => {
        const value = renderValue(expr, scope);
        if (isMissing(value)) { if (strict) throw new TemplateError(`${m} resolves to nothing`); return ""; }
        if (Array.isArray(value) || isObj(value)) return pyJson(value);
        return pyStr(value);
      });
    }
    if (Array.isArray(template)) return template.map((t) => render(t, scope, strict));
    if (isObj(template)) { const out = {}; for (const k of Object.keys(template)) out[k] = render(template[k], scope, strict); return out; }
    return template;
  }

  // ------------------------------------------------------------ conditions
  function num(v) {
    if (isBool(v)) return null;
    if (isNum(v)) return v;
    if (typeof v === "string") { const t = v.trim(); if (t === "" || Number.isNaN(Number(t))) return null; return Number(t); }
    return null;
  }
  function looseEq(a, b) {
    if (isBool(a) || isBool(b)) return isBool(a) && isBool(b) && a === b;
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return pyJson(a) === pyJson(b);
    if (isObj(a) && isObj(b)) return pyJson(sortKeys(a)) === pyJson(sortKeys(b));
    if ((a === null || a === undefined) && (b === null || b === undefined)) return true;
    const na = num(a), nb = num(b);
    if (na !== null && nb !== null && (typeof a === "string" || typeof b === "string")) return na === nb;
    return false;
  }
  function sortKeys(o) { const out = {}; for (const k of Object.keys(o).sort()) out[k] = isObj(o[k]) ? sortKeys(o[k]) : o[k]; return out; }
  const isEmpty = (v) => isMissing(v) || v === null || v === undefined || ((typeof v === "string" || Array.isArray(v)) && v.length === 0) || (isObj(v) && Object.keys(v).length === 0);
  function typeIs(name, v) {
    switch (name) {
      case "str": case "string": return typeof v === "string";
      case "int": return isNum(v) && Number.isInteger(v);
      case "float": case "number": return isNum(v);
      case "bool": return isBool(v);
      case "list": return Array.isArray(v);
      case "dict": case "map": return isObj(v);
      case "null": case "none": return v === null || v === undefined;
      default: throw new ConditionError(`unknown type ${pyrepr(name)}`);
    }
  }
  function test(key, expected, value) {
    if (key === "exists") return (!isMissing(value) && value !== null && value !== undefined) === Boolean(expected);
    if (key === "empty") return isEmpty(value) === Boolean(expected);
    if (isMissing(value)) return false;
    switch (key) {
      case "eq": return looseEq(value, expected);
      case "ne": return !looseEq(value, expected);
      case "lt": case "lte": case "gt": case "gte": {
        const a = num(value), b = num(expected);
        if (a === null || b === null) return false;
        return key === "lt" ? a < b : key === "lte" ? a <= b : key === "gt" ? a > b : a >= b;
      }
      case "between": { const a = num(value), lo = num(expected[0]), hi = num(expected[1]); if (a === null || lo === null || hi === null) return false; return lo <= a && a <= hi; }
      case "matches": return value !== null && value !== undefined && pyRegex(expected).test(pyStr(value));
      case "contains":
        if (Array.isArray(value)) return value.some((item) => looseEq(item, expected));
        if (isObj(value)) return Object.prototype.hasOwnProperty.call(value, expected);
        return pyStr(value).includes(pyStr(expected));
      case "startswith": return pyStr(value).startsWith(pyStr(expected));
      case "endswith": return pyStr(value).endsWith(pyStr(expected));
      case "in": return expected.some((option) => looseEq(value, option));
      case "type": return typeIs(expected, value);
      default: throw new ConditionError(`unknown condition key ${pyrepr(key)}`);
    }
  }
  const holds = (cond, value) => Object.keys(cond).every((k) => test(k, cond[k], value));

  // ------------------------------------------------------------ html (host engine)
  const INLINE = new Set(["a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "del", "ins"]);
  function textOf(el) {
    const parts = [];
    for (const child of el.childNodes) {
      if (child.nodeType === 3) parts.push(child.nodeValue);
      else if (child.nodeType === 1) parts.push(INLINE.has(child.tagName.toLowerCase()) ? textOf(child) : " " + textOf(child) + " ");
    }
    return parts.join("").replace(/\s+/g, " ").trim();
  }
  function selectAll(rootEl, selector) {
    // The host's engine, plus :contains(text) and a group of them.
    const out = [];
    for (const part of splitGroups(selector)) {
      const m = /^(.*?)\s*:contains\((?:"([^"]*)"|'([^']*)'|([^)]*))\)\s*$/.exec(part);
      if (m) {
        const base = m[1].trim() || "*", needle = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4];
        for (const el of rootEl.querySelectorAll(base)) if (textOf(el).includes(needle) && !out.includes(el)) out.push(el);
      } else {
        for (const el of rootEl.querySelectorAll(part)) if (!out.includes(el)) out.push(el);
      }
    }
    return out;
  }
  function splitGroups(selector) {
    const parts = []; let depth = 0, cur = "";
    for (const ch of selector) { if (ch === "(") depth++; if (ch === ")") depth--; if (ch === "," && depth === 0) { parts.push(cur.trim()); cur = ""; } else cur += ch; }
    if (cur.trim()) parts.push(cur.trim());
    return parts;
  }
  const html = {
    parse(text) {
      if (typeof DOMParser === "undefined") return null;
      const doc = new DOMParser().parseFromString(text, "text/html");
      return doc.documentElement;
    },
  };

  // ------------------------------------------------------------ extract
  function extract(entry, source, cache) {
    if ("json" in entry || "path" in entry) return resolve(entry.json !== undefined ? entry.json : entry.path, source);
    if ("regex" in entry) {
      const text = textSource(entry, source);
      if (isMissing(text)) return MISSING;
      const re = pyRegex(entry.regex, (entry.dotall ? "s" : "") + "g");
      const groups = pyRegex(entry.regex + "|").exec("").length - 1;
      const group = entry.group !== undefined ? entry.group : groups ? 1 : 0;
      if (entry.all) return Array.from(text.matchAll(re), (m) => m[group]);
      const m = re.exec(text);
      return m ? (m[group] === undefined ? null : m[group]) : MISSING;
    }
    if ("css" in entry) {
      const text = textSource(entry, source);
      if (isMissing(text)) return MISSING;
      let doc = cache && cache.get(text);
      if (!doc) { doc = html.parse(text); if (!doc) return MISSING; if (cache) cache.set(text, doc); }
      const found = selectAll(doc, entry.css);
      if (entry.all) return found.map((el) => elementValue(entry, el));
      if (!found.length) return MISSING;
      return elementValue(entry, found[0]);
    }
    throw new Error(`extract entry ${pyrepr(entry.into)} has no way`);
  }
  function textSource(entry, source) {
    const frm = entry.from !== undefined ? entry.from : "body";
    const value = typeof frm === "string" ? resolve(frm, source) : MISSING;
    if (isMissing(value) || value === null || value === undefined) return MISSING;
    return typeof value === "string" ? value : pyStr(value);
  }
  function elementValue(entry, el) {
    if (entry.fields) {
      const out = {};
      for (const name of Object.keys(entry.fields)) {
        const spec = entry.fields[name];
        const sub = selectAll(el, typeof spec === "string" ? spec : spec.css)[0] || null;
        if (!sub) out[name] = null;
        else if (typeof spec !== "string" && "attr" in spec) out[name] = sub.getAttribute(spec.attr);
        else out[name] = textOf(sub);
      }
      return out;
    }
    if ("attr" in entry) return el.getAttribute(entry.attr);
    return textOf(el);
  }

  // ------------------------------------------------------------ trees
  const INPUT_TYPES = ["str", "int", "float", "bool", "list", "dict"];
  function inferType(v) { return isBool(v) ? "bool" : isNum(v) ? (Number.isInteger(v) ? "int" : "float") : Array.isArray(v) ? "list" : isObj(v) ? "dict" : "str"; }
  function coerce(spec, value) {
    const bad = () => { throw new TreeError(`input ${pyrepr(spec.name)} must be ${spec.type}, got ${pyrepr(value)}`); };
    if (spec.type === "int") { if (isBool(value)) bad(); const n = Number(typeof value === "string" ? value.trim() : value); if (Number.isNaN(n) || (typeof value === "string" && !/^-?\d+$/.test(value.trim()))) bad(); value = Math.trunc(n); }
    else if (spec.type === "float") { const n = Number(typeof value === "string" ? value.trim() : value); if (Number.isNaN(n) || isBool(value)) bad(); value = n; }
    else if (spec.type === "bool") {
      if (typeof value === "string") { const low = value.trim().toLowerCase(); if (["true", "yes", "1", "on"].includes(low)) value = true; else if (["false", "no", "0", "off"].includes(low)) value = false; else bad(); }
      else value = Boolean(value);
    }
    else if (spec.type === "str") value = pyStr(value);
    else if (spec.type === "list" && !Array.isArray(value)) bad();
    else if (spec.type === "dict" && !isObj(value)) bad();
    if (spec.min !== undefined && spec.min !== null && isNum(value) && value < spec.min) throw new TreeError(`input ${pyrepr(spec.name)} must be at least ${spec.min}, got ${value}`);
    if (spec.max !== undefined && spec.max !== null && isNum(value) && value > spec.max) throw new TreeError(`input ${pyrepr(spec.name)} must be at most ${spec.max}, got ${value}`);
    if (spec.one_of && !spec.one_of.some((o) => looseEq(o, value) && typeof o === typeof value)) throw new TreeError(`input ${pyrepr(spec.name)} must be one of ${pyrepr(spec.one_of)}, got ${pyrepr(value)}`);
    if (spec.pattern && typeof value === "string" && !pyRegex("^(?:" + String(spec.pattern).replace(/^\(\?[imsx]+\)/, (f) => { spec._flags = f; return ""; }) + ")$", (spec._flags || "").replace(/[()?x]/g, "")).test(value)) throw new TreeError(`input ${pyrepr(spec.name)} must match ${pyrepr(spec.pattern)}, got ${pyrepr(value)}`);
    return value;
  }
  class Tree {
    constructor(raw) {
      this.raw = raw;
      this.name = raw.name; this.version = raw.version || 1; this.description = raw.description || "";
      this.hosts = raw.hosts || []; this.start = raw.start; this.tags = raw.tags || []; this.license = raw.license || "";
      this.language = raw.language || {};
      this.triggers = (raw.triggers || []).map((t) => (typeof t === "string" ? { pattern: t } : t));
      this.inputs = {};
      for (const [name, s] of Object.entries(raw.inputs || {})) {
        const spec = isObj(s) ? s : { default: s };
        this.inputs[name] = { name, type: spec.type || inferType(spec.default), default: spec.default === undefined ? null : spec.default, required: spec.required !== undefined ? Boolean(spec.required) : spec.default === undefined, description: spec.description || "", min: spec.min, max: spec.max, one_of: spec.one_of, pattern: spec.pattern };
      }
      this.nodes = {};
      for (const [id, spec] of Object.entries(raw.nodes || {})) this.nodes[id] = { id, kind: spec.kind, spec, get: (k, d) => (spec[k] !== undefined ? spec[k] : d), next: spec.next };
    }
    node(id) { const n = this.nodes[id]; if (!n) throw new TreeError(`${this.name}: no node ${pyrepr(id)}`); return n; }
    hostAllowed(url) {
      let host = "";
      try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return false; }
      if (!host) return false;
      for (let allowed of this.hosts) {
        allowed = allowed.toLowerCase();
        if (allowed === "*") return true;
        if (allowed.startsWith("*.")) { const suffix = allowed.slice(1); if (host.endsWith(suffix) || host === allowed.slice(2)) return true; }
        else if (host === allowed) return true;
      }
      return false;
    }
    bindInputs(given) {
      given = given || {};
      const unknown = Object.keys(given).filter((k) => !(k in this.inputs)).sort();
      if (unknown.length) throw new TreeError(`${this.name}: unknown inputs ${pyrepr(unknown)}`);
      const bound = {};
      for (const [name, spec] of Object.entries(this.inputs)) {
        if (name in given && given[name] !== null && given[name] !== undefined) bound[name] = coerce(spec, given[name]);
        else if (spec.default !== null && spec.default !== undefined) bound[name] = spec.default;
        else if (spec.required) throw new TreeError(`${this.name}: input ${pyrepr(name)} is required`);
        else bound[name] = null;
      }
      return bound;
    }
    capability() {
      const inputs = {};
      for (const [n, s] of Object.entries(this.inputs)) {
        const d = {};
        if (s.type) d.type = s.type;
        if (s.default !== null && s.default !== undefined && s.default !== "" && s.default !== false) d.default = s.default;
        if (s.required && (s.default === null || s.default === undefined)) d.required = true;
        if (s.description) d.description = s.description;
        inputs[n] = d;
      }
      return { name: this.name, version: this.version, description: this.description, tags: this.tags, hosts: this.hosts, license: this.license, language: this.language, triggers: this.triggers.map((t) => t.pattern), inputs };
    }
  }

  // ------------------------------------------------------------ transports
  class Response {
    constructor(status, headers, body, url, elapsed_ms) { this.status = status; this.headers = headers; this.body = body; this.url = url; this.elapsed_ms = elapsed_ms || 0; }
    get json() { try { return JSON.parse(this.body); } catch (e) { return null; } }
    toState() { return { status: this.status, ok: this.status >= 200 && this.status < 300, headers: this.headers, body: this.body, json: this.json, url: this.url, elapsed_ms: this.elapsed_ms }; }
  }
  class FetchTransport {
    constructor(opts) { this.userAgent = (opts && opts.userAgent) || "pyrtan.js"; }
    async request(method, url, headers, body, timeout) {
      const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = ctl ? setTimeout(() => ctl.abort(), (timeout || DEFAULT_TIMEOUT) * 1000) : null;
      const started = Date.now();
      try {
        const r = await fetch(url, { method: method.toUpperCase(), headers: Object.assign({ Accept: "application/json, text/html;q=0.9, */*;q=0.8" }, headers || {}), body: body === null || body === undefined ? undefined : body, signal: ctl ? ctl.signal : undefined, redirect: "follow" });
        const text = await r.text();
        const h = {}; r.headers.forEach((v, k) => { h[k.toLowerCase()] = v; });
        return new Response(r.status, h, text, r.url || url, Date.now() - started);
      } catch (e) {
        throw new TransportError(`${method} ${url}: ${e && e.message ? e.message : e}`);
      } finally { if (timer) clearTimeout(timer); }
    }
  }
  class RecordedTransport {
    constructor(responses) { this.responses = JSON.parse(JSON.stringify(responses || {})); this.calls = []; }
    async request(method, url) {
      const key = `${method.toUpperCase()} ${url}`;
      this.calls.push([method.toUpperCase(), url]);
      let rec = this.responses[key];
      if (rec === undefined) throw new TransportError(`no recorded response for ${key}`);
      if (Array.isArray(rec)) rec = rec.length > 1 ? rec.shift() : rec[0];
      if ("error" in rec) throw new TransportError(String(rec.error));
      let text = rec.body === undefined ? "" : rec.body;
      if (typeof text !== "string") text = pyJson(text);
      const headers = {}; for (const [k, v] of Object.entries(rec.headers || {})) headers[k.toLowerCase()] = v;
      return new Response(rec.status === undefined ? 200 : Number(rec.status), headers, text, url, 0);
    }
  }

  // ------------------------------------------------------------ runtime
  class Halt { constructor(outcome) { this.outcome = outcome; } }
  const statePath = (path) => { path = String(path).trim(); const head = path.split(".")[0].split("[")[0]; return ["inputs", "state", "item", "index", "tree", "now", "env"].includes(head) ? path : "state." + path; };
  function put(state, path, value) {
    if (path.startsWith("state.")) path = path.slice(6);
    const parts = path.split("."); let cur = state;
    for (const part of parts.slice(0, -1)) { let nxt = cur[part]; if (!isObj(nxt)) { nxt = {}; cur[part] = nxt; } cur = nxt; }
    cur[parts[parts.length - 1]] = value;
  }
  const plain = (v) => (isMissing(v) ? null : v);
  function nowScope(date) {
    const d = date || new Date();
    const iso = d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
    return { iso, date: iso.slice(0, 10), epoch: Math.floor(d.getTime() / 1000), year: d.getUTCFullYear() };
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  class Trace {
    constructor(tree) { this.tree = tree; this.steps = []; this.started = Date.now() / 1000; this.gaps = []; }
    add(step) { this.steps.push(step); return step; }
    gap(node, on, observed) { this.gaps.push({ tree: this.tree, node, on, observed: short(observed) }); }
    toDict() { return { tree: this.tree, started: this.started, steps: this.steps.map(stepDict), gaps: this.gaps }; }
    render() {
      const lines = [`trace: ${this.tree}`];
      for (const s of this.steps) lines.push(`${"  ".repeat((s.depth || 0) + 1)}${s.node} (${s.kind})${s.chosen ? " [" + s.chosen + "]" : ""}: ${s.summary}${s.next ? " -> " + s.next : ""}`);
      for (const g of this.gaps) lines.push(`  gap: ${g.node} saw ${g.on} = ${pyJson(g.observed)} and had no branch for it`);
      return lines.join("\n");
    }
  }
  function step(node, kind, summary, extra) { return Object.assign({ node, kind, summary, observed: null, chosen: null, next: null, elapsed_ms: 0, depth: 0 }, extra || {}); }
  function stepDict(s) { const d = { node: s.node, kind: s.kind, summary: s.summary, next: s.next, elapsed_ms: s.elapsed_ms, depth: s.depth }; if (s.observed !== null && s.observed !== undefined) d.observed = s.observed; if (s.chosen !== null && s.chosen !== undefined) d.chosen = s.chosen; return d; }
  function outcome(status, tree, fields) { return Object.assign({ status, tree, say: null, output: null, reason: null, question: null, snapshot: null, trace: null, inputs: {} }, fields || {}); }
  function outcomeDict(o, withTrace = true) {
    const d = { status: o.status, tree: o.tree, inputs: o.inputs || {} };
    for (const k of ["say", "output", "reason", "question", "snapshot"]) if (o[k] !== null && o[k] !== undefined) d[k] = o[k];
    if (withTrace && o.trace) d.trace = o.trace.toDict();
    return d;
  }

  class Runner {
    constructor(opts) {
      opts = opts || {};
      this.trees = {};
      for (const t of opts.trees || []) this.add(t);
      this.transport = opts.transport || new FetchTransport();
      this.resolver = opts.resolver || null;   // {runTree(name, inputs, depth), runIntent(text, depth)} -> outcome dicts
      this.env = opts.env || {};
      this.clock = opts.clock || (() => new Date());
      this.maxSteps = opts.maxSteps || MAX_STEPS; this.maxDepth = opts.maxDepth || MAX_DEPTH;
      this.emitted = [];
    }
    add(treeOrRaw) { const t = treeOrRaw instanceof Tree ? treeOrRaw : new Tree(treeOrRaw); this.trees[t.name] = t; return t; }
    get(name) { return this.trees[name] || null; }

    async runTree(name, inputs, depth = 0) {
      const tree = this.get(name);
      if (!tree) {
        if (this.resolver) return fromDict(await this.resolver.runTree(name, inputs || {}, depth), name);
        return outcome("error", name, { reason: "no-such-tree", say: `No tree named ${pyrepr(name)}.` });
      }
      return this.run(tree, inputs || {}, { depth });
    }
    async runIntent(text, depth = 0) {
      if (this.resolver) return fromDict(await this.resolver.runIntent(text, depth), "");
      return outcome("error", "", { reason: "no-match", say: `Nothing here answers ${pyrepr(text)}.` });
    }
    async run(tree, inputs, opts) {
      opts = opts || {};
      const depth = opts.depth || 0;
      const trace = opts.trace || new Trace(tree.name);
      if (depth > this.maxDepth) return outcome("error", tree.name, { reason: "too-deep", say: `Delegation deeper than ${this.maxDepth} levels.`, trace });
      let bound;
      try { bound = tree.bindInputs(inputs); } catch (e) { if (e instanceof TreeError) return outcome("error", tree.name, { reason: "bad-input", say: e.message, trace }); throw e; }
      const state = opts.state ? opts.state : {};
      const scope = Object.assign({ inputs: bound, state, tree: { name: tree.name, version: tree.version }, now: nowScope(this.clock()), env: this.env }, opts.scopeExtra || {});
      try {
        return await this.walk(tree, opts.start || tree.start, scope, trace, depth);
      } catch (h) {
        if (!(h instanceof Halt)) throw h;
        h.outcome.inputs = bound;
        h.outcome.trace = h.outcome.trace || trace;
        return h.outcome;
      }
    }
    async walk(tree, start, scope, trace, depth) {
      let nodeId = start, steps = 0;
      for (;;) {
        steps++;
        if (steps > this.maxSteps) throw new Halt(outcome("error", tree.name, { reason: "too-many-steps", say: `More than ${this.maxSteps} steps.`, trace }));
        const node = tree.node(nodeId);
        const started = Date.now();
        let result;
        try {
          result = await this["do_" + node.kind](tree, node, scope, trace, depth);
        } catch (e) {
          if (e instanceof TemplateError) { trace.add(step(node.id, node.kind, `template: ${e.message}`, { depth })); throw new Halt(outcome("error", tree.name, { reason: "template", say: `${node.id}: ${e.message}`, trace })); }
          throw e;
        }
        if (result && result.status) { result.trace = trace; throw new Halt(result); }
        const [st, nextId] = result;
        st.elapsed_ms = Date.now() - started; st.depth = depth; st.next = nextId === undefined ? null : nextId;
        trace.add(st);
        if (nextId === undefined || nextId === null) throw new Halt(outcome("error", tree.name, { reason: "dead-end", say: `${node.id} has nowhere to go.`, trace }));
        nodeId = nextId;
      }
    }
    do_observe(tree, node, scope, trace, depth) { return this.observeOrAct(tree, node, scope, trace, true); }
    async do_act(tree, node, scope, trace, depth) {
      const emit = node.get("emit");
      if (emit !== undefined && emit !== null) {
        const event = { event: render(emit.event, scope), data: render(emit.data !== undefined ? emit.data : {}, scope), tree: tree.name };
        this.emitted.push(event);
        put(scope.state, node.get("into", "last"), event);
        return [step(node.id, "act", `emitted ${event.event}`, { observed: event.data }), node.next];
      }
      return this.observeOrAct(tree, node, scope, trace, false);
    }
    async observeOrAct(tree, node, scope, trace, retry) {
      const state = scope.state, into = node.get("into", "last"), http = node.get("http");
      let source, summary;
      if (http !== undefined && http !== null) {
        const req = render(http, scope);
        const method = String(req.method || "GET").toUpperCase(), url = String(req.url);
        if (!tree.hostAllowed(url)) return outcome("error", tree.name, { reason: "host-not-allowed", say: `${node.id}: ${url} is outside this tree's hosts ${pyrepr(tree.hosts)}.` });
        const headers = {}; for (const [k, v] of Object.entries(req.headers || {})) headers[String(k)] = String(v);
        let body = req.body;
        if (body !== undefined && body !== null && typeof body !== "string") { body = JSON.stringify(body); if (!("Content-Type" in headers)) headers["Content-Type"] = "application/json"; }
        const timeout = Number(node.get("timeout", DEFAULT_TIMEOUT));
        const attempts = retry ? 1 + Number(node.get("retries", DEFAULT_RETRIES)) : 1;
        let lastError = null, resp = null;
        for (let attempt = 0; attempt < attempts; attempt++) {
          try { resp = await this.transport.request(method, url, headers, body === undefined ? null : body, timeout); break; }
          catch (e) { if (!(e instanceof TransportError)) throw e; lastError = e.message; if (attempt + 1 < attempts) await sleep(Math.min(200 * (attempt + 1), 1000)); }
        }
        if (!resp) { trace.add(step(node.id, node.kind, `${method} ${url} failed: ${lastError}`)); return outcome("error", tree.name, { reason: "transport", say: `${node.id}: ${lastError}` }); }
        const observed = resp.toState();
        put(state, into, observed);
        source = observed;
        summary = `${method} ${url} -> ${resp.status}`;
      } else {
        const frm = node.get("from");
        const sourceVal = resolve(statePath(frm), scope);
        if (isMissing(sourceVal)) source = {};
        else if (isObj(sourceVal)) source = sourceVal;
        else source = { value: sourceVal, body: typeof sourceVal === "string" ? sourceVal : null };
        summary = `from ${frm}`;
      }
      const extracted = {}; const cache = new Map();
      for (const entry of node.get("extract", []) || []) {
        const value = extract(entry, source, cache);
        if (isMissing(value)) extracted[entry.into] = null;
        else { put(state, entry.into, value); extracted[entry.into] = value; }
      }
      const keys = Object.keys(extracted);
      if (keys.length) summary += "; " + keys.map((k) => `${k}=${brief(extracted[k])}`).join(", ");
      return [step(node.id, node.kind, summary, { observed: keys.length ? extracted : null }), node.next];
    }
    do_decide(tree, node, scope, trace, depth) {
      const on = statePath(node.get("on"));
      const value = resolve(on, scope);
      for (const br of node.get("branches")) {
        if ("else" in br) return [step(node.id, "decide", `${on} = ${brief(value)}`, { observed: plain(value), chosen: "else" }), br.else];
        if (holds(br.when, value)) return [step(node.id, "decide", `${on} = ${brief(value)}`, { observed: plain(value), chosen: condText(br.when) }), br.then];
      }
      trace.gap(node.id, on, plain(value));
      trace.add(step(node.id, "decide", `${on} = ${brief(value)}: no branch`, { observed: plain(value), depth }));
      return outcome("error", tree.name, { reason: "gap", say: `${node.id}: no branch for ${on} = ${brief(value)}.`, output: { gap: trace.gaps[trace.gaps.length - 1] } });
    }
    do_set(tree, node, scope) {
      const values = {};
      for (const [key, tmpl] of Object.entries(node.get("set"))) { const value = render(tmpl, scope); put(scope.state, key, value); values[key] = value; }
      return [step(node.id, "set", Object.keys(values).map((k) => `${k}=${brief(values[k])}`).join(", "), { observed: values }), node.next];
    }
    async do_each(tree, node, scope, trace, depth) {
      const state = scope.state;
      let items = resolve(statePath(node.get("over")), scope);
      if (isMissing(items) || items === null || items === undefined) items = [];
      if (!Array.isArray(items)) return outcome("error", tree.name, { reason: "not-a-list", say: `${node.id}: ${node.get("over")} is not a list.` });
      let limit = node.get("limit");
      if (limit !== undefined && limit !== null) { limit = Math.trunc(Number(render(limit, scope))); items = items.slice(0, limit); }
      const results = []; let failures = 0;
      for (let index = 0; index < items.length; index++) {
        const item = items[index];
        const withInputs = render(node.get("with", {}), Object.assign({}, scope, { item, index }));
        let sub;
        if ("tree" in node.spec) sub = await this.runTree(node.get("tree"), withInputs, depth + 1);
        else sub = await this.run(tree, scope.inputs, { depth: depth + 1, start: node.get("start"), state, scopeExtra: { item, index }, trace });
        if (sub.status === "done") results.push(sub.output !== null && sub.output !== undefined ? sub.output : sub.say);
        else { failures++; results.push(null); if (node.get("stop_on_fail", false)) return outcome("error", tree.name, { reason: "each-failed", say: `${node.id}: item ${index} failed: ${sub.say || sub.reason}` }); }
        if (sub.trace && sub.trace !== trace) { for (const s of sub.trace.steps) { s.depth = depth + 1; trace.steps.push(s); } trace.gaps.push(...sub.trace.gaps); }
      }
      put(state, node.get("into"), results);
      return [step(node.id, "each", `${items.length} item(s)` + (failures ? `, ${failures} failed` : "")), node.next];
    }
    async do_delegate(tree, node, scope, trace, depth) {
      const withInputs = render(node.get("with", {}), scope);
      let sub, summary;
      if ("intent" in node.spec) { const text = render(node.get("intent"), scope); sub = await this.runIntent(pyStr(text), depth + 1); summary = `intent ${pyrepr(pyStr(text))} -> ${sub.tree || "?"}: ${sub.status}`; }
      else { sub = await this.runTree(node.get("tree"), withInputs, depth + 1); summary = `tree ${node.get("tree")}: ${sub.status}`; }
      if (sub.trace && sub.trace !== trace) { for (const s of sub.trace.steps) { s.depth = depth + 1; trace.steps.push(s); } trace.gaps.push(...sub.trace.gaps); }
      put(scope.state, node.get("into"), { status: sub.status, say: sub.say, output: sub.output, reason: sub.reason, tree: sub.tree });
      if (sub.status === "asked") return outcome("error", tree.name, { reason: "ask-in-delegate", say: `${node.id}: ${sub.tree} asked a question; delegated trees cannot pause.` });
      return [step(node.id, "delegate", summary), node.next];
    }
    do_ask(tree, node, scope, trace, depth) {
      if (depth > 0) return outcome("error", tree.name, { reason: "ask-in-subflow", say: `${node.id}: only a top-level run can ask.` });
      const prompt = render(node.get("prompt"), scope);
      trace.add(step(node.id, "ask", `asked: ${prompt}`, { depth }));
      return outcome("asked", tree.name, { question: pyStr(prompt), snapshot: { tree: tree.name, node: node.id, inputs: scope.inputs, state: scope.state, trace: trace.steps.map(stepDict) } });
    }
    do_done(tree, node, scope, trace, depth) {
      let say = node.get("say"), output = node.get("output");
      say = say !== undefined && say !== null ? render(say, scope, false) : null;
      output = output !== undefined && output !== null ? render(output, scope, false) : null;
      trace.add(step(node.id, "done", say ? pyStr(say) : "done", { depth }));
      return outcome("done", tree.name, { say: say !== null ? pyStr(say) : null, output });
    }
    do_fail(tree, node, scope, trace, depth) {
      let say = node.get("say");
      say = say !== undefined && say !== null ? render(say, scope, false) : null;
      const reason = pyStr(node.get("reason"));
      trace.add(step(node.id, "fail", `${reason}: ${say || ""}`.replace(/: $/, ""), { depth }));
      const out = node.get("output");
      return outcome("failed", tree.name, { reason, say: say !== null ? pyStr(say) : null, output: out !== undefined && out !== null ? render(out, scope, false) : null });
    }
  }
  function fromDict(d, fallbackTree) {
    if (!isObj(d)) return outcome("error", fallbackTree, { reason: "remote", say: "no answer" });
    let trace = null;
    if (isObj(d.trace)) { trace = new Trace(d.trace.tree || fallbackTree); trace.steps = (d.trace.steps || []).map((s) => step(s.node, s.kind, s.summary, { observed: s.observed === undefined ? null : s.observed, chosen: s.chosen === undefined ? null : s.chosen, next: s.next === undefined ? null : s.next, elapsed_ms: s.elapsed_ms || 0, depth: s.depth || 0 })); trace.gaps = d.trace.gaps || []; }
    return outcome(d.status || "error", d.tree || fallbackTree, { say: d.say === undefined ? null : d.say, output: d.output === undefined ? null : d.output, reason: d.reason === undefined ? null : d.reason, question: d.question === undefined ? null : d.question, snapshot: d.snapshot === undefined ? null : d.snapshot, trace, inputs: d.inputs || {} });
  }

  return { MISSING, Tree, Runner, Trace, FetchTransport, RecordedTransport, Response, render, resolve, holds, extract, outcomeDict, fromDict, pyrepr, pyJson, html, TemplateError, TreeError, TransportError, ConditionError, version: "0.6.0" };
});
