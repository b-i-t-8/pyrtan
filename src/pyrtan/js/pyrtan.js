/* pyrtan.js: the tree runtime, in JavaScript.
 *
 * The same tree files the Python runtime reads (as JSON, the TOML's own
 * table), walked the same way: observe, act, decide, set, lookup, each, delegate,
 * ask, done, fail; the same templates, conditions and extraction; the same
 * trace. tests/conformance holds the vectors both runtimes must agree on.
 *
 * What differs, and is written down here rather than hidden:
 * - `float` in a condition's `type` means any number here; JSON has no
 *   distinct float, so 4.0 and 4 are the same value.
 * - HTML is read by a port of the Python runtime's own parser and selector
 *   subset, not the host's DOM, so a page reads the same everywhere; named
 *   character references beyond the common ones are left as written.
 * - Regular expressions are the host's; the subset trees use is common.
 * - Sentences are read here as there (nlp.py, intent.py and the proposal
 *   from grow.py): a runner reads among the trees it holds and asks its
 *   resolver (a member's hub) only when none answers. Python's \d takes
 *   any script's digits; here a digit is 0-9 (fold makes full-width ones
 *   plain first).
 * - LocalHub makes a page a hub of its own: the paths and answers of a
 *   service, in memory, for a browser that has joined no hub.
 * - `grow` drafts the branch a gap asks for by grow.py's rules, and checks
 *   an agent's suggestion as suggestions.py does; it never asks a model.
 *
 * No model. No dependencies. Runs in a browser as `Pyrtan` and in node.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Pyrtan = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const VERSION = "0.10.0";
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
  const asText = (v) => (Array.isArray(v) || isObj(v) ? pyJson(v) : pyStr(v));  // a value as it reads inside a sentence
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
    join: (v) => (Array.isArray(v) ? v.map(asText).join(", ") : asText(v)),
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
        return asText(value);
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

  // ------------------------------------------------------------ html
  // The Python runtime's own document model and selector subset (html.py),
  // here too, so a page reads the same on a service and on a member, and
  // the same in node: tag, #id, .class, [attr], [attr=v], [attr^=v],
  // [attr$=v], [attr*=v], :first-child, :last-child, :nth-child(n),
  // :contains(text), descendant and > combinators, and , groups.
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
  const INLINE = new Set(["a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "del", "ins"]);
  const RAW_TEXT = new Set(["script", "style"]);
  const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", copy: "©", reg: "®", trade: "™", hellip: "…", mdash: "—", ndash: "–", laquo: "«", raquo: "»", lsquo: "‘", rsquo: "’", ldquo: "“", rdquo: "”", bull: "•", middot: "·", deg: "°", euro: "€", pound: "£", yen: "¥", cent: "¢", times: "×", divide: "÷", plusmn: "±", frac12: "½", eacute: "é", egrave: "è", agrave: "à", ccedil: "ç", ouml: "ö", auml: "ä", uuml: "ü", szlig: "ß", ntilde: "ñ", aring: "å", oslash: "ø", aelig: "æ" };
  function unescapeHtml(text) {
    if (!text.includes("&")) return text;
    return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);?/g, (m, body) => {
      if (body[0] === "#") { const code = body[1] === "x" || body[1] === "X" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10); return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : m; }
      return body in ENTITIES ? ENTITIES[body] : m;
    });
  }
  class Element {
    constructor(tag, attrs, parent) { this.tag = tag; this.attrs = attrs || {}; this.children = []; this.parent = parent || null; }
    get text() {
      const parts = [];
      for (const child of this.children) {
        if (typeof child === "string") parts.push(child);
        else parts.push(INLINE.has(child.tag) ? child.text : " " + child.text + " ");
      }
      return parts.join("").replace(/\s+/g, " ").trim();
    }
    get classes() { return (this.attrs.class || "").split(/\s+/).filter(Boolean); }
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
    elements() { return this.children.filter((c) => typeof c !== "string"); }
    *descendants() { for (const child of this.elements()) { yield child; yield* child.descendants(); } }
    select(selector) { return select(this, selector); }
  }
  const ATTR = /\s*([^\s"'>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/y;
  function parseHtml(text) {
    const root = new Element("document");
    const stack = [root];
    let pos = 0;
    const n = text.length;
    const addText = (t) => { if (t) stack[stack.length - 1].children.push(unescapeHtml(t)); };
    while (pos < n) {
      const lt = text.indexOf("<", pos);
      if (lt < 0) { addText(text.slice(pos)); break; }
      addText(text.slice(pos, lt));
      if (text.startsWith("<!--", lt)) { const e = text.indexOf("-->", lt + 4); pos = e < 0 ? n : e + 3; continue; }
      if (text[lt + 1] === "!" || text[lt + 1] === "?") { const e = text.indexOf(">", lt); pos = e < 0 ? n : e + 1; continue; }
      if (text[lt + 1] === "/") {
        const e = text.indexOf(">", lt);
        const tag = text.slice(lt + 2, e < 0 ? n : e).trim().toLowerCase();
        pos = e < 0 ? n : e + 1;
        for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; break; }
        continue;
      }
      const m = /^<([a-zA-Z][^\s\/>]*)/.exec(text.slice(lt, lt + 200));
      if (!m) { addText("<"); pos = lt + 1; continue; }
      const tag = m[1].toLowerCase();
      let p = lt + m[0].length;
      const attrs = {};
      while (p < n) {
        const ws = /\s*/y; ws.lastIndex = p; ws.exec(text); p = ws.lastIndex;
        if (text[p] === ">" || text.startsWith("/>", p) || p >= n) break;
        ATTR.lastIndex = p;
        const am = ATTR.exec(text);
        if (!am) { p++; continue; }
        p = ATTR.lastIndex;
        const name = am[1].toLowerCase();
        if (name !== "/") attrs[name] = unescapeHtml(am[2] !== undefined ? am[2] : am[3] !== undefined ? am[3] : am[4] !== undefined ? am[4] : "");
      }
      const selfClosing = text.startsWith("/>", p);
      pos = p >= n ? n : p + (selfClosing ? 2 : 1);
      const el = new Element(tag, attrs, stack[stack.length - 1]);
      stack[stack.length - 1].children.push(el);
      if (selfClosing || VOID.has(tag)) continue;
      if (RAW_TEXT.has(tag)) {
        const close = text.toLowerCase().indexOf("</" + tag, pos);
        const raw = text.slice(pos, close < 0 ? n : close);
        if (raw) el.children.push(raw);
        if (close < 0) { pos = n; continue; }
        const e = text.indexOf(">", close); pos = e < 0 ? n : e + 1;
        continue;
      }
      stack.push(el);
    }
    return root;
  }
  class SelectorError extends Error {}
  const SEL_TOKEN = /([a-zA-Z][a-zA-Z0-9\-]*|\*)|#([\w\-]+)|\.([\w\-]+)|\[([\w\-:]+)(?:([\^$*]?=)(["']?)([^\]"']*)\6)?\]|:(first-child|last-child|nth-child\(\d+\)|contains\((["']?)([^)"']*)\9\))/y;
  function parseCompound(text) {
    const simple = { tag: null, id: null, classes: [], attrs: [], pseudos: [] };
    text = text.trim();
    if (!text) throw new SelectorError("empty selector part");
    let pos = 0;
    while (pos < text.length) {
      SEL_TOKEN.lastIndex = pos;
      const m = SEL_TOKEN.exec(text);
      if (!m) throw new SelectorError(`cannot parse selector near ${pyrepr(text.slice(pos))}`);
      pos = SEL_TOKEN.lastIndex;
      if (m[1]) { if (simple.tag) throw new SelectorError(`two tags in one compound selector: ${pyrepr(text)}`); simple.tag = m[1].toLowerCase(); }
      else if (m[2]) simple.id = m[2];
      else if (m[3]) simple.classes.push(m[3]);
      else if (m[4]) simple.attrs.push([m[4].toLowerCase(), m[5] || "", m[7] === undefined ? null : m[7]]);
      else if (m[8]) simple.pseudos.push(m[8].startsWith("contains(") ? "contains:" + m[10] : m[8]);
    }
    return simple;
  }
  function compileSelector(selector) {
    const groups = [];
    for (let alt of String(selector).split(",")) {
      alt = alt.trim();
      if (!alt) throw new SelectorError(`empty alternative in ${pyrepr(selector)}`);
      const chain = [];
      let comb = " ";
      for (const part of alt.split(/\s*(>)\s*|\s+/)) {
        if (part === undefined || part === "") continue;
        if (part === ">") { comb = ">"; continue; }
        chain.push([comb, parseCompound(part)]);
        comb = " ";
      }
      if (!chain.length) throw new SelectorError(`nothing to match in ${pyrepr(selector)}`);
      groups.push(chain);
    }
    return groups;
  }
  function simpleMatches(simple, el) {
    if (simple.tag && simple.tag !== "*" && el.tag !== simple.tag) return false;
    if (simple.id && el.attrs.id !== simple.id) return false;
    if (simple.classes.length) { const have = new Set(el.classes); if (!simple.classes.every((c) => have.has(c))) return false; }
    for (const [name, op, val] of simple.attrs) {
      if (!(name in el.attrs)) return false;
      const have = el.attrs[name];
      if (op === "=" && have !== val) return false;
      if (op === "^=" && !have.startsWith(val || "")) return false;
      if (op === "$=" && !have.endsWith(val || "")) return false;
      if (op === "*=" && !have.includes(val || "")) return false;
    }
    for (const p of simple.pseudos) {
      if (p.startsWith("contains:")) { if (!el.text.includes(p.slice(9))) return false; continue; }
      const siblings = el.parent ? el.parent.elements() : [el];
      if (p === "first-child") { if (siblings[0] !== el) return false; }
      else if (p === "last-child") { if (siblings[siblings.length - 1] !== el) return false; }
      else if (p.startsWith("nth-child(")) { const k = parseInt(p.slice(10, -1), 10); if (!(k > 0 && k <= siblings.length && siblings[k - 1] === el)) return false; }
      else throw new SelectorError(`unknown pseudo-class :${p}`);
    }
    return true;
  }
  function chainMatches(el, chain) {
    const [comb, simple] = chain[chain.length - 1];
    if (!simpleMatches(simple, el)) return false;
    if (chain.length === 1) return true;
    const rest = chain.slice(0, -1);
    if (comb === ">") return el.parent !== null && el.parent.tag !== "document" && chainMatches(el.parent, rest);
    for (let anc = el.parent; anc !== null && anc.tag !== "document"; anc = anc.parent) if (chainMatches(anc, rest)) return true;
    return false;
  }
  function select(rootEl, selector) {
    const compiled = compileSelector(selector);
    const out = [];
    const seen = new Set();
    for (const el of rootEl.descendants()) for (const chain of compiled) if (chainMatches(el, chain) && !seen.has(el)) { seen.add(el); out.push(el); break; }
    return out;
  }
  const selectAll = select;
  const textOf = (el) => el.text;
  const html = { parse: parseHtml, select, compileSelector, Element, SelectorError };

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
      if (!doc) { doc = html.parse(text); if (cache) cache.set(text, doc); }
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

  // ------------------------------------------------------------ sources and their allow-lists
  // The other forms of I/O (file, shell, serial, queue, browser) live on a
  // service; a member refuses them unless a recorded fixture answers for
  // them. The allow-list checks and the fixture keys are the Python ones.
  const SOURCES = ["http", "browser", "file", "shell", "serial", "queue"];
  const LIST_NAMES = ["paths", "commands", "devices", "topics"];
  const MQTT_PORTS = { mqtt: 1883, mqtts: 8883 };
  function absolutePath(path) {
    path = String(path);
    const parts = path.split("/"), out = [];
    for (const part of parts) { if (part === "" || part === ".") continue; if (part === "..") out.pop(); else out.push(part); }
    return (path.startsWith("/") ? "/" : "") + out.join("/");
  }
  function globMatch(pattern, text) {
    let re = "";
    for (const ch of pattern) re += ch === "*" ? ".*" : ch === "?" ? "." : ch.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    return new RegExp("^" + re + "$").test(text);
  }
  function pathAllowed(patterns, path) {
    const target = absolutePath(path);
    for (let pat of patterns) {
      if (pat === "*") return true;
      pat = absolutePath(pat);
      if (/[*?[]/.test(pat)) { if (globMatch(pat, target)) return true; }
      else if (target === pat || target.startsWith(pat.replace(/\/+$/, "") + "/")) return true;
    }
    return false;
  }
  function commandAllowed(commands, command) {
    command = String(command);
    for (const entry of commands) {
      if (entry === "*") return true;
      if (entry.includes("/")) { if (absolutePath(command) === absolutePath(entry)) return true; }
      else if (command === entry || command.split("/").pop() === entry) return true;
    }
    return false;
  }
  const deviceAllowed = (devices, device) => devices.some((d) => d === "*" || globMatch(d, String(device)));
  function topicAllowed(topics, topic) {
    const parts = String(topic).split("/");
    for (const entry of topics) {
      if (entry === "*" || entry === "#") return true;
      const pat = entry.split("/");
      let ok = true, i = 0;
      for (; i < pat.length; i++) {
        const seg = pat[i];
        if (seg === "#") { ok = ok && i <= parts.length; break; }
        if (i >= parts.length || (seg !== "+" && seg !== "*" && seg !== parts[i])) { ok = false; break; }
      }
      if (i === pat.length) ok = ok && pat.length === parts.length;
      if (ok) return true;
    }
    return false;
  }
  function broker(url) {
    const m = /^(mqtts?):\/\/([^/:?#]+)(?::(\d+))?/.exec(String(url));
    if (!m) throw new Error(`${pyrepr(String(url))} is not mqtt://host[:port] or mqtts://host[:port]`);
    return [m[1], m[2], m[3] ? Number(m[3]) : MQTT_PORTS[m[1]]];
  }
  const arg = (a) => (typeof a === "string" ? a : pyJson(a));
  function adapterKey(source, spec) {
    if (source === "file") return `file ${"write" in spec ? "write " : "append" in spec ? "append " : ""}${spec.path}`;
    if (source === "shell") return "shell " + [String(spec.command)].concat((spec.args || []).map(arg)).join(" ");
    if (source === "serial") { const w = spec.write; return `serial ${spec.device}` + (w === undefined || w === null || w === "" ? "" : ` ${JSON.stringify(String(w))}`); }
    if (source === "queue") return `queue ${"publish" in spec ? "publish " : ""}${spec.url} ${spec.topic}`;
    if (source === "browser") return `browser ${spec.url}`;
    throw new Error(source);
  }
  function what(source, spec) {
    if (source === "file") return String(spec.path);
    if (source === "shell") return [String(spec.command)].concat((spec.args || []).map(arg)).join(" ");
    if (source === "serial") return String(spec.device);
    if (source === "queue") return `${spec.topic} at ${spec.url}`;
    return String(spec.url);
  }
  function briefResult(source, observed) {
    if (source === "http" || source === "browser") return pyStr(observed.status === undefined ? null : observed.status);
    if (source === "file") return "written" in observed ? `wrote ${observed.written} B` : observed.exists ? `${observed.size} B` : "missing";
    if (source === "shell") return `exit ${observed.status}`;
    if (source === "serial") return `${observed.bytes} B`;
    if (source === "queue") return "published" in observed ? `published ${observed.published} B` : observed.received ? "1 message" : "no message";
    return "ok";
  }

  // ------------------------------------------------------------ language (nlp.py, intent.py)
  // How a sentence finds its tree, the same rules as the Python runtime: the
  // same tables, the same rewrites in the same order, the same regular
  // expression built from each trigger, the same verdicts and notes, so a
  // browser reads a sentence the way a hub does.
  const CONTRACTIONS = {
    "what's": "what is", "whats": "what is", "where's": "where is", "who's": "who is", "how's": "how is",
    "when's": "when is", "why's": "why is", "it's": "it is", "that's": "that is", "there's": "there is",
    "here's": "here is", "let's": "let us", "isn't": "is not", "aren't": "are not", "wasn't": "was not",
    "weren't": "were not", "don't": "do not", "doesn't": "does not", "didn't": "did not", "can't": "can not",
    "cannot": "can not", "couldn't": "could not", "won't": "will not", "wouldn't": "would not",
    "shouldn't": "should not", "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "i'm": "i am", "i've": "i have", "i'd": "i would", "i'll": "i will", "you're": "you are",
    "you've": "you have", "you'd": "you would", "you'll": "you will", "we're": "we are", "we've": "we have",
    "they're": "they are", "they've": "they have", "gimme": "give me", "lemme": "let me", "wanna": "want to",
    "gonna": "going to", "u": "you", "pls": "please", "plz": "please", "thx": "thanks",
  };
  const LEADING_FILLERS = [
    "hey pyrtan", "hi pyrtan", "hello pyrtan", "ok pyrtan", "okay pyrtan", "pyrtan",
    "hey", "hi", "hello", "yo", "ok", "okay", "so", "um", "uh", "well", "please", "kindly",
    "can you", "could you", "would you", "will you", "can you please", "could you please", "would you please",
    "i want you to", "i need you to", "i would like you to", "i'd like you to",
    "quick question", "question",
  ];
  const TRAILING_FILLERS = ["please", "thanks", "thank you", "for me", "if you can", "if you could", "if possible", "pyrtan"];
  // Python's sorted(..., key=len, reverse=True) is stable: equal lengths keep their order.
  const byLengthDesc = (list) => list.map((f, i) => [f, i]).sort((a, b) => b[0].length - a[0].length || a[1] - b[1]).map((p) => p[0]);
  const LEADING_SORTED = byLengthDesc(LEADING_FILLERS), TRAILING_SORTED = byLengthDesc(TRAILING_FILLERS);
  const UNITS = { zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19 };
  const TENS = { twenty: 20, thirty: 30, forty: 40, fourty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90 };
  const SCALES = { hundred: 100, thousand: 1000, million: 1000000, billion: 1000000000 };
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const NOT_PLURALS = new Set(["news", "is", "as", "us", "his", "this", "thus", "yes", "plus", "bus", "gas", "was", "has", "its",
    "status", "bonus", "always", "perhaps", "physics", "series", "species", "means", "analysis", "basis",
    "lens", "chess", "address", "process", "access", "class", "pass", "less", "unless", "across", "ssl",
    "https", "dns", "tls", "os", "ios", "chaos", "canvas", "atlas", "virus", "campus", "census", "focus",
    "iris", "oasis", "crisis", "thesis", "diagnosis", "axis", "texas", "paris", "mars", "venus"]);
  const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  const MONTH_ABBR = {}; MONTHS.forEach((m, i) => { MONTH_ABBR[m.slice(0, 3)] = i + 1; }); MONTH_ABBR.sept = 9;
  const DURATION_UNITS = {
    s: 1, sec: 1, secs: 1, second: 1, seconds: 1, m: 60, min: 60, mins: 60, minute: 60, minutes: 60,
    h: 3600, hr: 3600, hrs: 3600, hour: 3600, hours: 3600, d: 86400, day: 86400, days: 86400,
    w: 604800, wk: 604800, wks: 604800, week: 604800, weeks: 604800,
  };
  const HOST_RE = /^(?:https?:\/\/)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}|localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?:\/\S*)?$/i;
  const URL_RE = /^https?:\/\/[^\s<>"']+$/i;
  const EMAIL_RE = /^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}$/;
  const isAlpha = (s) => s.length > 0 && /^\p{L}+$/u.test(s);
  const isUpperChar = (c) => c !== c.toLowerCase() && c === c.toUpperCase();
  const isLowerWord = (w) => w === w.toLowerCase() && w !== w.toUpperCase();
  const reEscape = (s) => String(s).replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
  // Python's str() of a float: 2.0, not 2.
  const pyFloat = (n) => (Number.isInteger(n) && Math.abs(n) < 1e16 ? n.toFixed(1) : String(n));
  class ParseError extends Error {}
  class TriggerError extends Error {}

  function fold(text) {
    text = String(text).normalize("NFKC");
    text = text.replace(/’/g, "'").replace(/‘/g, "'").replace(/“/g, '"').replace(/”/g, '"');
    text = text.replace(/[‐-―−]/g, "-");
    return text.replace(/\s+/gu, " ").trim();
  }
  function trim(text) {
    text = text.replace(/^[^\p{L}\p{N}]+|[\s?!.,;:]+$/gu, "");
    // A comma between digits (1,000) is part of a number and stays.
    text = text.replace(/\s*[,;!?]+\s*/g, (m, at, s) => (m === "," && /[0-9]/.test(s[at - 1] || "") && /[0-9]/.test(s[at + 1] || "") ? m : " "));
    text = text.replace(/(?<=[A-Za-z])\.(?=\s|$)/g, "");
    return text.replace(/\s+/gu, " ").trim();
  }
  function stripFillers(text, keep) {
    keep = keep || new Set();
    const dropped = [];
    let changed = true;
    while (changed && text) {
      changed = false;
      let low = text.toLowerCase();
      for (const f of LEADING_SORTED) {
        if ((low === f || low.startsWith(f + " ") || low.startsWith(f + ",")) && !keep.has(f.split(" ")[0])) {
          dropped.push(text.slice(0, f.length));
          text = text.slice(f.length).replace(/^[ ,]+/, "");
          changed = true;
          break;
        }
      }
      low = text.toLowerCase();
      for (const f of TRAILING_SORTED) {
        const words = f.split(" ");
        if ((low === f || low.endsWith(" " + f) || low.endsWith("," + f)) && !keep.has(words[words.length - 1])) {
          dropped.push(text.slice(text.length - f.length));
          text = text.slice(0, text.length - f.length).replace(/[ ,]+$/, "");
          changed = true;
          break;
        }
      }
    }
    return [text, dropped];
  }
  function expandContractions(text) {
    const expanded = [];
    const out = text.replace(/[A-Za-z]+(?:'[A-Za-z]+)?/g, (word) => {
      const low = word.toLowerCase().replace(/’/g, "'");
      if (!has(CONTRACTIONS, low)) return word;
      const full = CONTRACTIONS[low];
      expanded.push(`${word}->${full}`);
      return isLowerWord(word) || !isUpperChar(word[0]) ? full : full[0].toUpperCase() + full.slice(1);
    });
    return [out, expanded];
  }
  const isNumberWord = (word) => word.toLowerCase().split("-").every((p) => has(UNITS, p) || has(TENS, p) || has(SCALES, p) || p === "dozen");
  function numberRun(words, start) {
    let i = start;
    const first = words[i].toLowerCase();
    if (first === "a" && i + 1 < words.length && ["dozen", "hundred", "thousand", "million", "billion"].includes(words[i + 1].toLowerCase())) i += 1;
    else if (!isNumberWord(first)) return start;
    let end = i;
    while (i < words.length) {
      const w = words[i].toLowerCase();
      if (isNumberWord(w)) { i += 1; end = i; }
      else if ((w === "and" || w === "a") && i + 1 < words.length && isNumberWord(words[i + 1].toLowerCase())) i += 1;
      else break;
    }
    return end;
  }
  function runValue(run) {
    const words = [];
    for (const tok of run) words.push(...tok.toLowerCase().split("-"));
    let total = 0, current = 0, seen = false;
    for (const w of words) {
      if (w === "and" || w === "a") { if (w === "a") current = 1; continue; }
      if (has(UNITS, w)) { current += UNITS[w]; seen = true; }
      else if (has(TENS, w)) { current += TENS[w]; seen = true; }
      else if (w === "dozen") { current = (current || 1) * 12; seen = true; }
      else if (w === "hundred") { current = (current || 1) * 100; seen = true; }
      else if (has(SCALES, w)) { total += (current || 1) * SCALES[w]; current = 0; seen = true; }
      else return null;
    }
    return seen ? total + current : null;
  }
  function wordsToDigits(text) {
    const words = text.split(" "), out = [], changed = [];
    let i = 0;
    while (i < words.length) {
      const end = numberRun(words, i);
      if (end > i) {
        const run = words.slice(i, end), value = runValue(run);
        if (value !== null) { out.push(String(value)); changed.push(`${run.join(" ")}->${value}`); i = end; continue; }
      }
      out.push(words[i]); i += 1;
    }
    return [out.join(" "), changed];
  }
  function clean(text, opts) {
    opts = opts || {};
    const steps = [];
    const folded = fold(text);
    if (folded !== String(text).trim()) steps.push("fold");
    text = trim(folded);
    if (opts.fillers !== false) {
      const [t, dropped] = stripFillers(text, opts.keep);
      text = t;
      if (dropped.length) { steps.push("fillers: " + dropped.map(pyrepr).join(", ")); text = trim(text); }
    }
    const [t2, expanded] = expandContractions(text);
    text = t2;
    if (expanded.length) steps.push("contractions: " + expanded.join(", "));
    if (opts.numbers !== false) {
      const [t3, changed] = wordsToDigits(text);
      text = t3;
      if (changed.length) steps.push("numbers: " + changed.join(", "));
    }
    return [text.replace(/\s+/gu, " ").trim(), steps];
  }
  function singular(word) {
    const w = word.toLowerCase();
    if (w.length < 4 || NOT_PLURALS.has(w) || w.endsWith("ss") || !isAlpha(w)) return w;
    if (w.endsWith("ies") && w.length > 4) return w.slice(0, -3) + "y";
    if (["ches", "shes", "sses", "xes", "zes"].some((e) => w.endsWith(e))) return w.slice(0, -2);
    if (w.endsWith("s")) return w.slice(0, -1);
    return w;
  }
  function plural(word) {
    const w = word.toLowerCase();
    if (!isAlpha(w) || NOT_PLURALS.has(w)) return w;
    if (w.endsWith("y") && w.length > 2 && !"aeiou".includes(w[w.length - 2])) return w.slice(0, -1) + "ies";
    if (["ch", "sh", "ss", "x", "z", "s"].some((e) => w.endsWith(e))) return w + "es";
    return w + "s";
  }
  function forms(word) {
    const w = word.toLowerCase(), out = new Set([w]);
    if (isAlpha(w) && Array.from(w).length >= 3) { const base = singular(w); out.add(base); out.add(plural(base)); }
    return out;
  }
  function distance(a, b, limit = 2) {
    a = Array.from(a.toLowerCase()); b = Array.from(b.toLowerCase());
    if (Math.abs(a.length - b.length) > limit) return limit + 1;
    let prevPrev = null, prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i].concat(new Array(b.length).fill(0));
      for (let j = 1; j <= b.length; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        if (prevPrev !== null && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) cur[j] = Math.min(cur[j], prevPrev[j - 2] + 1);
      }
      if (Math.min(...cur) > limit) return limit + 1;
      prevPrev = prev; prev = cur;
    }
    return prev[prev.length - 1];
  }

  // ---- values: each typed slot's fixed reader
  function parseNumber(text) {
    let [t] = wordsToDigits(text.trim());
    t = t.replace(/,/g, "");
    if (/^-?\d+$/.test(t)) return { value: parseInt(t, 10), py: String(parseInt(t, 10)) };
    if (/^-?\d+\.\d+$/.test(t)) { const v = parseFloat(t); return { value: v, py: pyFloat(v) }; }
    throw new ParseError(`${pyrepr(text)} is not a number`);
  }
  function parseDuration(text) {
    let [t] = wordsToDigits(text.trim().toLowerCase());
    t = t.replace(/\bhalf\s+an?\s+([a-z]+)/g, "0.5 $1");
    t = t.replace(/\b(an?)\s+(?=(?:hour|minute|second|day|week)s?\b)/g, "1 ");
    t = t.replace(/(\d+(?:\.\d+)?)\s*([a-z]+)\s+and\s+a\s+half\b/g, (m, n, u) => `${pyFloat(parseFloat(n) + 0.5)} ${u}`);
    t = t.replace(/\s+and\s+/g, " ");
    let total = 0, found = false, pos = 0;
    for (const m of t.matchAll(/(\d+(?:\.\d+)?)\s*([a-z]+)/g)) {
      if (t.slice(pos, m.index).trim()) throw new ParseError(`${pyrepr(text)} is not a duration`);
      if (!has(DURATION_UNITS, m[2])) throw new ParseError(`${pyrepr(text)}: unknown unit ${pyrepr(m[2])}`);
      total += parseFloat(m[1]) * DURATION_UNITS[m[2]];
      found = true; pos = m.index + m[0].length;
    }
    if (!found || t.slice(pos).trim()) throw new ParseError(`${pyrepr(text)} is not a duration`);
    const v = Math.trunc(total);
    return { value: v, py: String(v) };
  }
  const pad2 = (n) => String(n).padStart(2, "0");
  function parseTime(text) {
    let [t] = wordsToDigits(text.trim().toLowerCase());
    t = t.replace(/\./g, ":").replace(/ /g, "");
    const done = (s) => ({ value: s, py: s });
    if (t === "noon" || t === "midday") return done("12:00");
    if (t === "midnight") return done("00:00");
    const m = /^(\d{1,2})(?::(\d{2}))?(?:o'?clock)?(am|pm)?$/.exec(t);
    if (!m) throw new ParseError(`${pyrepr(text)} is not a time`);
    let hour = parseInt(m[1], 10);
    const minute = parseInt(m[2] || "0", 10), half = m[3];
    if (half) {
      if (!(hour >= 1 && hour <= 12)) throw new ParseError(`${pyrepr(text)}: ${hour} with ${half}`);
      hour = (hour % 12) + (half === "pm" ? 12 : 0);
    } else if (m[2] === undefined && !m[0].endsWith("clock")) throw new ParseError(`${pyrepr(text)}: a bare number is not a time; say 15:00 or 3pm`);
    if (!(hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59)) throw new ParseError(`${pyrepr(text)} is not a time`);
    return done(`${pad2(hour)}:${pad2(minute)}`);
  }
  // Dates are plain calendar days in UTC, as the hub's clock gives them.
  const isLeap = (y) => y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const monthDays = (y, m) => [31, isLeap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1];
  const dayOf = (y, m, d) => { const out = new Date(0); out.setUTCFullYear(y, m - 1, d); return out; };
  const isoDay = (d) => `${String(d.getUTCFullYear()).padStart(4, "0")}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
  const addDays = (d, n) => new Date(d.getTime() + n * 86400000);
  const weekdayOf = (d) => (d.getUTCDay() + 6) % 7;
  function mkDate(year, month, day, text) {
    const why = !(year >= 1 && year <= 9999) ? `year ${year} is out of range` : !(month >= 1 && month <= 12) ? "month must be in 1..12" : !(day >= 1 && day <= monthDays(year, month)) ? "day is out of range for month" : null;
    if (why) throw new ParseError(text === null ? why : `${pyrepr(text)}: ${why}`);
    return dayOf(year, month, day);
  }
  function shift(day, n, unit) {
    unit = unit.replace(/s+$/, "");
    if (unit === "day") return addDays(day, n);
    if (unit === "week") return addDays(day, 7 * n);
    const months = day.getUTCMonth() + (unit === "month" ? n : 12 * n);
    const year = day.getUTCFullYear() + Math.floor(months / 12), month = (((months % 12) + 12) % 12) + 1;
    return mkDate(year, month, Math.min(day.getUTCDate(), monthDays(year, month)), null);
  }
  function monthOf(word) {
    if (!word || /^\d+$/.test(word)) return null;
    const i = MONTHS.indexOf(word);
    if (i >= 0) return i + 1;
    const three = word.slice(0, 3);
    if (word.length >= 3 && has(MONTH_ABBR, three) && MONTHS[MONTH_ABBR[three] - 1].startsWith(word)) { const k = word.startsWith("sept") ? word.slice(0, 4) : three; return has(MONTH_ABBR, k) ? MONTH_ABBR[k] : null; }
    return null;
  }
  function weekdayIndex(word) {
    const i = WEEKDAYS.indexOf(word);
    if (i >= 0) return i;
    if (word.length >= 3) { const j = WEEKDAYS.findIndex((d) => d.slice(0, 3) === word.slice(0, 3)); if (j >= 0 && WEEKDAYS[j].startsWith(word)) return j; }
    return null;
  }
  function parseDate(text, now) {
    const n = now instanceof Date ? now : new Date();
    const today = dayOf(n.getUTCFullYear(), n.getUTCMonth() + 1, n.getUTCDate());
    const done = (d) => { const s = isoDay(d); return { value: s, py: s }; };
    let [t] = wordsToDigits(text.trim().toLowerCase());
    t = t.replace(/^(on|at|for|the|this coming)\s+/, "").trim();
    t = t.replace(/(\d)(st|nd|rd|th)\b/g, "$1");
    t = t.split(",").join(" ").split("  ").join(" ").trim();
    let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t);
    if (m) return done(mkDate(+m[1], +m[2], +m[3], text));
    m = /^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?$/.exec(t);
    if (m) {
      const d = +m[1], mo = +m[2];
      if (d <= 12 && mo <= 12 && d !== mo) throw new ParseError(`${pyrepr(text)} could be day/month or month/day; write it as ${today.getUTCFullYear()}-${pad2(mo)}-${pad2(d)} or '${d} ${MONTHS[mo - 1]}'`);
      let year = m[3] ? +m[3] : today.getUTCFullYear();
      if (year < 100) year += 2000;
      return done(mkDate(year, mo, d, text));
    }
    m = /^(\d{1,2})(?:\s+of)?\s+([a-z]+)(?:\s+(\d{4}))?$/.exec(t) || /^([a-z]+)\s+(\d{1,2})(?:\s+(\d{4}))?$/.exec(t);
    if (m && (monthOf(m[1]) || monthOf(m[2]))) {
      let day, month;
      if (/^\d+$/.test(m[1])) { day = +m[1]; month = monthOf(m[2]); } else { month = monthOf(m[1]); day = +m[2]; }
      if (!month) throw new ParseError(`${pyrepr(text)} is not a date`);
      return done(mkDate(m[3] ? +m[3] : today.getUTCFullYear(), month, day, text));
    }
    if (t === "today" || t === "now") return done(today);
    if (["tomorrow", "tmrw", "tmr"].includes(t)) return done(addDays(today, 1));
    if (t === "yesterday") return done(addDays(today, -1));
    if (t === "day after tomorrow" || t === "the day after tomorrow") return done(addDays(today, 2));
    if (t === "day before yesterday" || t === "the day before yesterday") return done(addDays(today, -2));
    m = /^(?:(this|next|last)\s+)?([a-z]+)$/.exec(t);
    if (m && weekdayIndex(m[2]) !== null) {
      let ahead = (((weekdayIndex(m[2]) - weekdayOf(today)) % 7) + 7) % 7;
      if (m[1] === "next") ahead = ahead || 7;
      else if (m[1] === "last") ahead = ahead ? ahead - 7 : -7;
      return done(addDays(today, ahead));
    }
    m = /^in\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)$/.exec(t);
    if (m) return done(shift(today, +m[1], m[2]));
    m = /^(\d+)\s+(day|days|week|weeks|month|months|year|years)\s+ago$/.exec(t);
    if (m) return done(shift(today, -m[1], m[2]));
    m = /^(next|last)\s+(week|month|year)$/.exec(t);
    if (m) return done(shift(today, m[1] === "next" ? 1 : -1, m[2]));
    m = /^end of (?:the |this )?(month|year)$/.exec(t);
    if (m) {
      if (m[1] === "year") return done(dayOf(today.getUTCFullYear(), 12, 31));
      return done(addDays(shift(dayOf(today.getUTCFullYear(), today.getUTCMonth() + 1, 1), 1, "month"), -1));
    }
    if (["start of month", "start of the month", "beginning of month", "beginning of the month"].includes(t)) return done(dayOf(today.getUTCFullYear(), today.getUTCMonth() + 1, 1));
    throw new ParseError(`${pyrepr(text)} is not a date this reads`);
  }
  function parseHost(text) {
    const m = HOST_RE.exec(text.trim());
    if (!m) throw new ParseError(`${pyrepr(text)} is not a host name`);
    const v = m[1].toLowerCase();
    return { value: v, py: v };
  }
  function parseUrl(text) {
    const t = text.trim();
    if (!URL_RE.test(t)) throw new ParseError(`${pyrepr(text)} is not a URL`);
    return { value: t, py: t };
  }
  function parseEmail(text) {
    const t = text.trim();
    if (!EMAIL_RE.test(t)) throw new ParseError(`${pyrepr(text)} is not an email address`);
    return { value: t.toLowerCase(), py: t.toLowerCase() };
  }
  const PARSERS = { number: parseNumber, duration: parseDuration, time: parseTime, date: parseDate, host: parseHost, url: parseUrl, email: parseEmail };
  const nlp = { fold, trim, stripFillers, expandContractions, wordsToDigits, clean, singular, plural, forms, distance, parseNumber, parseDuration, parseTime, parseDate, parseHost, parseUrl, parseEmail, ParseError };

  // ---- triggers
  const KINDS = ["int", "float", "word", "text", "number", "date", "time", "duration", "host", "url", "email"];
  const SLOT_RE = { int: "-?\\d+", float: "-?\\d+(?:\\.\\d+)?", number: "-?\\d+(?:[.,]\\d+)*", word: "\\S+", text: ".+?", date: ".+?", time: "\\S+(?:\\s*[ap]m)?", duration: ".+?", host: "\\S+", url: "\\S+", email: "\\S+", "": ".+?" };
  const TYPO_MIN_LENGTH = 5;
  function normalise(text) {
    text = String(text).trim();
    text = text.replace(/^[^\p{L}\p{N}]+|[\s?!.,;:]+$/gu, "");
    return text.replace(/\s+/gu, " ");
  }
  function language(raw, where = "language") {
    if (raw === null || raw === undefined) return { synonyms: {}, typos: false };
    if (!isObj(raw)) throw new TriggerError(`${where} must be a table`);
    const syn = raw.synonyms === undefined ? {} : raw.synonyms;
    if (!isObj(syn)) throw new TriggerError(`${where}.synonyms must be a table of word = [words]`);
    const synonyms = {};
    for (const [word, a] of Object.entries(syn)) {
      const alts = typeof a === "string" ? [a] : a;
      if (!Array.isArray(alts) || !alts.every((x) => typeof x === "string" && x.trim())) throw new TriggerError(`${where}.synonyms.${word} must be a list of words`);
      if (!/^[A-Za-z][A-Za-z'-]*$/.test(word)) throw new TriggerError(`${where}.synonyms: ${pyrepr(word)} is not a word`);
      synonyms[word.toLowerCase()] = alts.map((x) => x.trim().toLowerCase());
    }
    const typos = raw.typos === undefined ? false : raw.typos;
    if (!isBool(typos)) throw new TriggerError(`${where}.typos must be true or false`);
    for (const key of Object.keys(raw)) if (key !== "synonyms" && key !== "typos") throw new TriggerError(`${where}: unknown key ${pyrepr(key)}; one of synonyms, typos`);
    return { synonyms, typos };
  }
  const languageDict = (lang) => { const out = {}; if (Object.keys(lang.synonyms).length) out.synonyms = lang.synonyms; if (lang.typos) out.typos = true; return out; };
  function wordRegex(word, lang) {
    const spellings = forms(word), phrases = [];
    for (const syn of lang.synonyms[word.toLowerCase()] || []) {
      if (syn.includes(" ")) phrases.push(syn.split(/\s+/).map(reEscape).join("\\s+"));
      else for (const f of forms(syn)) spellings.add(f);
    }
    const sorted = [...spellings].sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0));
    return "(?:" + sorted.map(reEscape).concat(phrases).join("|") + ")";
  }
  function compilePattern(pattern, lang) {
    lang = lang || { synonyms: {}, typos: false };
    if (!pattern || !pattern.trim()) throw new TriggerError("a trigger pattern cannot be empty");
    const slots = {}, out = [], vocabulary = new Set(), ordered = [];
    let literal = 0, pos = 0, depth = 0, word = [];
    const text = Array.from(pattern.trim().toLowerCase());
    const flush = () => {
      if (!word.length) return;
      const w = word.join("");
      out.push(wordRegex(w, lang)); vocabulary.add(w); ordered.push(w);
      for (const syn of lang.synonyms[w] || []) for (const s of syn.split(/\s+/)) if (s) vocabulary.add(s);
      word = [];
    };
    while (pos < text.length) {
      const ch = text[pos];
      if (isAlpha(ch) || (word.length && "'-".includes(ch) && pos + 1 < text.length && isAlpha(text[pos + 1]))) { word.push(ch); literal++; pos++; continue; }
      flush();
      if (ch === "{") {
        const m = /^\{([A-Za-z_][A-Za-z0-9_]*)(?::([a-z]+))?\}/.exec(text.slice(pos).join(""));
        if (!m) throw new TriggerError(`bad slot in trigger ${pyrepr(pattern)} at ${pos}`);
        const name = m[1], kind = m[2] === undefined ? null : m[2];
        if (kind !== null && !KINDS.includes(kind)) throw new TriggerError(`slot {${name}:${kind}} in ${pyrepr(pattern)}: kind must be one of ${KINDS.join(", ")}`);
        if (has(slots, name)) throw new TriggerError(`slot ${pyrepr(name)} appears twice in ${pyrepr(pattern)}`);
        slots[name] = kind;
        out.push(`(?<${name}>${SLOT_RE[kind === null ? "" : kind]})`);
        pos += m[0].length;
      } else if (ch === "[") { depth++; out.push("(?:\\s*"); pos++; }
      else if (ch === "]") { if (depth === 0) throw new TriggerError(`unbalanced ] in trigger ${pyrepr(pattern)}`); depth--; out.push(")?"); pos++; }
      else if (ch === "(") { depth++; out.push("(?:"); pos++; }
      else if (ch === ")") { if (depth === 0) throw new TriggerError(`unbalanced ) in trigger ${pyrepr(pattern)}`); depth--; out.push(")"); pos++; }
      else if (ch === "|" && depth > 0) { out.push("|"); pos++; }
      else if (/^\s$/u.test(ch)) {
        while (pos < text.length && /^\s$/u.test(text[pos])) pos++;
        // Space is optional around optional groups and slots, so "[the] weather" matches "weather".
        if (out.length && ["(?:\\s*", ")?", "(?:", ")", "|"].includes(out[out.length - 1])) out.push("\\s*");
        else if (pos < text.length && "[(|".includes(text[pos])) out.push("\\s*");
        else out.push("\\s+");
      } else { out.push(reEscape(ch)); literal++; pos++; }
    }
    flush();
    if (depth !== 0) throw new TriggerError(`unbalanced [ in trigger ${pyrepr(pattern)}`);
    if (literal === 0) throw new TriggerError(`trigger ${pyrepr(pattern)} has no literal words; it would match anything`);
    let regex;
    try { regex = new RegExp("^(?:" + out.join("") + ")$", "iu"); } catch (e) { throw new TriggerError(`trigger ${pyrepr(pattern)} does not compile: ${e.message}`); }
    return { regex, slots, specificity: literal, vocabulary, edges: ordered.length ? [ordered[0], ordered[ordered.length - 1]] : ["", ""] };
  }
  function correctTypos(sentence, vocabulary) {
    if (!vocabulary.size) return [sentence, []];
    const fixed = [];
    const out = sentence.replace(/[A-Za-z]+/g, (word) => {
      const low = word.toLowerCase();
      if (low.length < TYPO_MIN_LENGTH || vocabulary.has(low) || [...vocabulary].some((v) => forms(v).has(low))) return word;
      const close = [...vocabulary].filter((v) => v.length >= TYPO_MIN_LENGTH && distance(low, v, 1) <= 1);
      if (close.length !== 1) return word;
      fixed.push([word, close[0]]);
      return close[0];
    });
    return [out, fixed];
  }
  function readSlot(kind, raw, now) {
    if (kind === "int") return { value: parseInt(raw, 10), py: String(parseInt(raw, 10)) };
    if (kind === "float") { const v = parseFloat(raw); return { value: v, py: pyFloat(v) }; }
    if (kind === null || kind === "word" || kind === "text") return { value: raw, py: raw };
    return kind === "date" ? PARSERS.date(raw, now) : PARSERS[kind](raw);
  }
  class Trigger {
    constructor(pattern, fixedInputs, lang) {
      this.pattern = pattern;
      this.fixedInputs = fixedInputs || {};
      this.language = lang && lang.synonyms ? lang : language(lang);
      // The pattern gets the rewrites a sentence gets, minus fillers and number words.
      this.cleaned = fold(pattern).split(/(\{[^}]*\})/).map((part) => (part.startsWith("{") ? part : expandContractions(part)[0])).join("");
      Object.assign(this, compilePattern(this.cleaned, this.language));
    }
    match(text, now) { const m = this.matchDetail(text, now); return m ? m.inputs : null; }
    matchDetail(text, now, cleaned = false) {
      let sentence = cleaned ? text : clean(text)[0];
      const notes = [];
      if (this.language.typos) { const [s, fixed] = correctTypos(sentence, this.vocabulary); sentence = s; for (const [a, b] of fixed) notes.push(`typo: ${a}->${b}`); }
      const m = this.regex.exec(sentence);
      if (!m) return null;
      const inputs = Object.assign({}, this.fixedInputs);
      for (const [name, kind] of Object.entries(this.slots)) {
        let raw = m.groups[name];
        if (raw === undefined) continue;
        raw = raw.trim();
        let got;
        try { got = readSlot(kind, raw, now); }
        catch (e) {
          if (!(e instanceof ParseError)) throw e;
          notes.push(`slot ${name}: ${e.message}`);
          return { tree: "", trigger: this, inputs: {}, specificity: this.specificity, notes, rejected: true };
        }
        inputs[name] = got.value;
        if (has(PARSERS, kind) && got.py !== raw) notes.push(`${name}: ${pyrepr(raw)} read as ${typeof got.value === "string" ? pyrepr(got.value) : got.py}`);
      }
      return { tree: "", trigger: this, inputs, specificity: this.specificity, notes, rejected: false };
    }
  }
  const matchDict = (m) => ({ tree: m.tree, trigger: m.trigger.pattern, specificity: m.specificity, inputs: m.inputs, notes: m.notes, rejected: m.rejected });
  function isoNow(d) {
    if (!(d instanceof Date)) return null;
    const ms = d.getUTCMilliseconds();
    return d.toISOString().replace(/\.\d{3}Z$/, (ms ? "." + String(ms).padStart(3, "0") + "000" : "") + "+00:00");
  }
  class Reading {
    constructor(text, cleaned, steps, matches, considered, now) { Object.assign(this, { text, cleaned, steps, matches, considered, now }); }
    get winner() { return this.matches.length === 1 ? this.matches[0] : null; }
    toDict() { return { text: this.text, cleaned: this.cleaned, steps: this.steps, now: this.now, matches: this.matches.map(matchDict), considered: this.considered.map(matchDict) }; }
    step() {
      const rewrite = pyrepr(this.text) + (this.cleaned !== this.text ? ` -> ${pyrepr(this.cleaned)}` : "");
      let summary;
      const w = this.winner;
      if (w) { summary = `${rewrite}; ${w.tree} via ${pyrepr(w.trigger.pattern)} (${w.specificity} literal chars)`; if (w.notes.length) summary += "; " + w.notes.join("; "); }
      else if (this.matches.length) summary = `${rewrite}; tie between ${pyrepr([...new Set(this.matches.map((m) => m.tree))].sort())}`;
      else summary = `${rewrite}; no trigger matched`;
      const observed = { cleaned: this.cleaned, steps: this.steps };
      if (w) { observed.trigger = w.trigger.pattern; observed.inputs = w.inputs; if (w.notes.length) observed.notes = w.notes; }
      return { node: "intent", kind: "intent", summary, observed };
    }
  }
  // candidates: [[treeName, Trigger], ...] in the order a registry lists them (trees by name).
  function read(text, candidates, now) {
    const keep = new Set();
    for (const [, trig] of candidates) for (const w of trig.edges) if (w) keep.add(w);
    const [cleaned, steps] = clean(text, { keep });
    const considered = [];
    for (const [tree, trig] of candidates) { const got = trig.matchDetail(cleaned, now, true); if (got) { got.tree = tree; considered.push(got); } }
    const live = considered.filter((m) => !m.rejected);
    const top = live.reduce((t, m) => Math.max(t, m.specificity), 0);
    return new Reading(text, cleaned, steps, live.filter((m) => m.specificity === top), considered, isoNow(now));
  }

  // ---- the tree that would answer a sentence nobody answered (grow.py)
  const PROPOSE_FILLER = new Set(["a", "an", "the", "what", "what's", "whats", "how", "please", "me", "my", "tell", "show", "give", "is", "are", "of", "to", "for", "in", "on", "at", "do", "does", "it", "and", "with"]);
  const NAME_MAX = 40;
  function proposalSlot(tok, inputs) {
    const bare = tok.replace(/^["'(),;:!?]+|["'(),;:!?]+$/g, "");
    const unique = (base) => { if (!has(inputs, base)) return base; let n = 2; while (has(inputs, base + n)) n++; return base + n; };
    if (/^-?\d+$/.test(bare)) { const s = unique("count"); inputs[s] = { type: "int", description: `as in: ${bare}` }; return `{${s}:int}`; }
    if (/^-?\d+\.\d+$/.test(bare)) { const s = unique("value"); inputs[s] = { type: "float", description: `as in: ${bare}` }; return `{${s}:float}`; }
    if (/^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?$/i.test(bare) && !bare.endsWith("...") && !bare.endsWith(".")) { const s = unique("host"); inputs[s] = { type: "str", description: `a host name, as in: ${bare}`, role: "host" }; return `{${s}:host}`; }
    if (/^[\p{L}\p{N}_.\-]+\/[\p{L}\p{N}_.\-/]+$/u.test(bare)) { const s = unique("target"); inputs[s] = { type: "str", description: `as in: ${bare}` }; return `{${s}:word}`; }
    return null;
  }
  const proposalLiteral = (tok) => tok.replace(/[{}[\]()|]/g, "").trim();
  function shellQuote(s) {
    if (/^[\p{L}\p{N}_./:@%+=,-]+$/u.test(s)) return s;
    if (!/["$`\\]/.test(s)) return `"${s}"`;
    return "'" + s.replace(/'/g, "'\"'\"'") + "'";
  }
  function proposalName(literalWords, tokens, slots) {
    const slug = (w) => w.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    let words = literalWords.filter((w) => !PROPOSE_FILLER.has(w.toLowerCase())).map(slug).filter(Boolean);
    if (words.length < 2 && slots.length) words = slots.map((s) => s.replace(/\d+$/, "")).concat(words);
    if (!words.length) words = tokens.map(slug).filter(Boolean);
    let name = words.join("-").slice(0, NAME_MAX).replace(/^-+|-+$/g, "");
    name = name.replace(/-{2,}/g, "-");
    if (!name || !/^[a-z0-9]/.test(name)) name = "new-tree";
    return name;
  }
  function propose(text) {
    const cleanText = normalise(text) || String(text).trim() || "new tree";
    const tokens = cleanText.split(" ");
    const pieces = [], literalWords = [];
    let inputs = {};
    for (const tok of tokens) {
      const slot = proposalSlot(tok, inputs);
      if (slot !== null) pieces.push(slot); else { pieces.push(proposalLiteral(tok)); literalWords.push(tok); }
    }
    let trigger = pieces.filter(Boolean).join(" "), words = literalWords;
    try { new Trigger(trigger); }
    catch (e) {
      if (!(e instanceof TriggerError)) throw e;
      trigger = tokens.map(proposalLiteral).filter(Boolean).join(" ") || "new tree";
      inputs = {}; words = tokens;
    }
    const name = proposalName(words, tokens, Object.keys(inputs));
    return { text: cleanText, name, trigger, inputs, scaffold: ["pyrtan", "new", name, "--from", shellQuote(cleanText)].join(" ") };
  }
  function noMatchOutcome(text, where) {
    const p = propose(text);
    return { say: `${where} answers ${pyrepr(text)}. To write the tree that would: ${p.scaffold}`, output: { unanswered: text, proposal: p } };
  }
  const intent = { Trigger, TriggerError, read, normalise, language, compilePattern, correctTypos, propose, noMatchOutcome, KINDS };

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
      this.hosts = raw.hosts || []; this.start = raw.start; this.tags = raw.tags || []; this.license = raw.license || ""; this.namespace = raw.namespace || "";
      for (const k of LIST_NAMES) this[k] = raw[k] || [];
      try { this.language = language(raw.language); } catch (e) { if (e instanceof TriggerError) throw new TreeError(`${this.name}: ${e.message}`); throw e; }
      this.triggers = (raw.triggers || []).map((t, i) => {
        if (typeof t === "string") t = { pattern: t };
        if (!isObj(t) || !("pattern" in t)) throw new TreeError(`${this.name}: triggers[${i}] needs a 'pattern'`);
        try { return new Trigger(t.pattern, Object.assign({}, t.inputs || {}), this.language); }
        catch (e) { if (e instanceof TriggerError) throw new TreeError(`${this.name}: triggers[${i}]: ${e.message}`); throw e; }
      });
      this.inputs = {};
      for (const [name, s] of Object.entries(raw.inputs || {})) {
        const spec = isObj(s) ? s : { default: s };
        this.inputs[name] = { name, type: spec.type || inferType(spec.default), default: spec.default === undefined ? null : spec.default, required: spec.required !== undefined ? Boolean(spec.required) : spec.default === undefined, description: spec.description || "", min: spec.min, max: spec.max, one_of: spec.one_of, pattern: spec.pattern };
      }
      this.nodes = {};
      for (const [id, spec] of Object.entries(raw.nodes || {})) this.nodes[id] = { id, kind: spec.kind, spec, get: (k, d) => (spec[k] !== undefined ? spec[k] : d), next: spec.next };
    }
    node(id) { const n = this.nodes[id]; if (!n) throw new TreeError(`${this.name}: no node ${pyrepr(id)}`); return n; }
    get id() { return this.namespace ? `${this.namespace}/${this.name}` : this.name; }
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
    allowed(source, spec) {
      if (source === "http" || source === "browser") { const url = String(spec.url === undefined ? "" : spec.url); return this.hostAllowed(url) ? null : ["host-not-allowed", `${url} is outside this tree's hosts ${pyrepr(this.hosts)}.`]; }
      if (source === "file") { const path = absolutePath(spec.path === undefined ? "" : spec.path); return pathAllowed(this.paths, path) ? null : ["path-not-allowed", `${path} is outside this tree's paths ${pyrepr(this.paths)}.`]; }
      if (source === "shell") { const cmd = String(spec.command === undefined ? "" : spec.command); return commandAllowed(this.commands, cmd) ? null : ["command-not-allowed", `${pyrepr(cmd)} is not in this tree's commands ${pyrepr(this.commands)}.`]; }
      if (source === "serial") { const dev = String(spec.device === undefined ? "" : spec.device); return deviceAllowed(this.devices, dev) ? null : ["device-not-allowed", `${dev} is outside this tree's devices ${pyrepr(this.devices)}.`]; }
      if (source === "queue") {
        let host, port;
        try { [, host, port] = broker(spec.url === undefined ? "" : spec.url); } catch (e) { return ["host-not-allowed", e.message]; }
        if (!this.hostAllowed(`mqtt://${host}:${port}/`)) return ["host-not-allowed", `${host} is outside this tree's hosts ${pyrepr(this.hosts)}.`];
        const topic = String(spec.topic === undefined ? "" : spec.topic);
        return topicAllowed(this.topics, topic) ? null : ["topic-not-allowed", `${pyrepr(topic)} is outside this tree's topics ${pyrepr(this.topics)}.`];
      }
      return null;
    }
    sources() {
      const out = new Set();
      for (const n of Object.values(this.nodes)) if (n.kind === "observe" || n.kind === "act") for (const s of SOURCES) if (n.spec[s] !== undefined && n.spec[s] !== null) out.add(s);
      return [...out].sort();
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
        if (s.default !== null && s.default !== undefined && s.default !== "" && s.default !== false && s.default !== 0) d.default = s.default;  // Python drops what == False
        if (s.required && (s.default === null || s.default === undefined)) d.required = true;
        if (s.description) d.description = s.description;
        inputs[n] = d;
      }
      const cap = { name: this.name, version: this.version };
      if (this.namespace) cap.namespace = this.namespace;
      Object.assign(cap, { description: this.description, tags: this.tags, hosts: this.hosts });
      for (const k of LIST_NAMES) if (this[k].length) cap[k] = this[k];
      cap.sources = this.sources();
      const watches = Array.isArray(this.raw.watches) ? this.raw.watches : [];
      if (watches.length) cap.watches = watches.map((w) => { const kind = ["every", "file", "queue", "serial"].find((k) => k in w) || "?"; return { kind, what: kind === "every" ? String(w.every) : kind === "file" ? String(w.file) : kind === "queue" ? `${w.queue && w.queue.topic} at ${w.queue && w.queue.url}` : String(w.serial && w.serial.device) }; });
      const fixed = this.triggers.filter((t) => Object.keys(t.fixedInputs).length);
      return Object.assign(cap, { license: this.license, language: languageDict(this.language), triggers: this.triggers.map((t) => t.pattern) }, fixed.length ? { trigger_inputs: Object.fromEntries(fixed.map((t) => [t.pattern, t.fixedInputs])) } : {}, { inputs });
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
        // A browser hides why: most often the site does not let pages on other origins read it (CORS).
        const why = typeof window !== "undefined" && e instanceof TypeError ? " (the site may not let a page in a browser read it; a hub can)" : "";
        throw new TransportError(`${method} ${url}: ${e && e.message ? e.message : e}${why}`);
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
    async perform(source, spec) {
      const key = adapterKey(source, spec);
      this.calls.push([source, key]);
      let rec = this.responses[key];
      if (rec === undefined) throw new TransportError(`no recorded result for ${key}`);
      if (Array.isArray(rec)) rec = rec.length > 1 ? rec.shift() : rec[0];
      if ("error" in rec) throw new TransportError(String(rec.error));
      return JSON.parse(JSON.stringify(rec));
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
      // The other sources: a fixture answers for them; a live member has none and says so.
      this.adapters = opts.adapters || (typeof this.transport.perform === "function" ? this.transport : null);
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
    candidates() {
      const out = [];
      for (const name of Object.keys(this.trees).sort()) for (const trig of this.trees[name].triggers) out.push([name, trig]);
      return out;
    }
    read(text) { return read(text, this.candidates(), this.clock()); }
    // Read a sentence and run the tree it names; the trace begins with the intent step.
    async runIntent(text, depth = 0) {
      const reading = this.read(text);
      if (!reading.matches.length) {
        if (this.resolver) return fromDict(await this.resolver.runIntent(text, depth), "");
        const nm = noMatchOutcome(text, "Nothing here");
        nm.output.reading = { cleaned: reading.cleaned, steps: reading.steps, considered: reading.considered.map((m) => m.trigger.pattern) };
        return outcome("error", "", Object.assign({ reason: "no-match" }, nm));
      }
      const trees = [...new Set(reading.matches.map((m) => m.tree))].sort();
      if (trees.length > 1) return outcome("error", "", { reason: "ambiguous", say: `${pyrepr(text)} matches more than one tree equally well: ${trees.join(", ")}`, output: { candidates: trees, reading: { cleaned: reading.cleaned, steps: reading.steps } } });
      const match = reading.matches[0], tree = this.get(match.tree);
      const trace = new Trace(tree.name), st = reading.step();
      trace.add(step(st.node, st.kind, st.summary, { observed: st.observed, next: tree.start, depth }));
      return this.run(tree, match.inputs, { depth, trace });
    }
    // Continue a run paused by ask: the answer goes where the ask node said.
    async resume(snapshot, answer) {
      const tree = this.get(snapshot.tree);
      if (!tree) return outcome("error", snapshot.tree || "", { reason: "no-such-tree" });
      const node = tree.node(snapshot.node), into = String(node.get("into"));
      const inputs = Object.assign({}, snapshot.inputs), state = Object.assign({}, snapshot.state);
      if (into.startsWith("inputs.")) {
        const key = into.slice(7);
        try { inputs[key] = coerce(tree.inputs[key], answer); } catch (e) { if (e instanceof TreeError) return outcome("error", tree.name, { reason: "bad-input", say: e.message }); throw e; }
      } else put(state, into.startsWith("state.") ? into.slice(6) : into, answer);
      const trace = new Trace(tree.name);
      trace.steps = (snapshot.trace || []).map((x) => step(x.node, x.kind, x.summary, { observed: x.observed === undefined ? null : x.observed, chosen: x.chosen === undefined ? null : x.chosen, next: x.next === undefined ? null : x.next, elapsed_ms: x.elapsed_ms || 0, depth: x.depth || 0 }));
      trace.add(step(node.id, "ask", `answered: ${brief(answer)}`, { next: node.next }));
      return this.run(tree, inputs, { start: node.next, state, trace });
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
      const state = scope.state, into = node.get("into", "last");
      const sourceName = SOURCES.find((s) => node.spec[s] !== undefined && node.spec[s] !== null);
      let source, summary;
      if (sourceName !== undefined) {
        const spec = render(node.get(sourceName), scope);
        const problem = tree.allowed(sourceName, spec);
        if (problem) return outcome("error", tree.name, { reason: problem[0], say: `${node.id}: ${problem[1]}` });
        const timeout = Number(node.get("timeout", DEFAULT_TIMEOUT));
        let label, call;
        if (sourceName === "http") {
          const method = String(spec.method || "GET").toUpperCase(), url = String(spec.url);
          const headers = {}; for (const [k, v] of Object.entries(spec.headers || {})) headers[String(k)] = String(v);
          let body = spec.body;
          if (body !== undefined && body !== null && typeof body !== "string") { body = JSON.stringify(body); if (!("Content-Type" in headers)) headers["Content-Type"] = "application/json"; }
          label = `${method} ${url}`;
          call = async () => (await this.transport.request(method, url, headers, body === undefined ? null : body, timeout)).toState();
        } else {
          label = `${sourceName} ${what(sourceName, spec)}`;
          if (!this.adapters) return outcome("error", tree.name, { reason: "source-unsupported", say: `${node.id}: a ${sourceName} node runs on a service, not here; this device has no ${sourceName === "browser" ? "browser of its own to drive" : sourceName === "queue" ? "socket to a broker" : "access to the box's " + (sourceName === "file" ? "files" : sourceName === "shell" ? "programs" : "devices")}.` });
          call = () => this.adapters.perform(sourceName, spec, timeout, (u) => tree.hostAllowed(u));
        }
        const attempts = retry ? 1 + Number(node.get("retries", DEFAULT_RETRIES)) : 1;
        let lastError = null, observed = null;
        for (let attempt = 0; attempt < attempts; attempt++) {
          try { observed = await call(); break; }
          catch (e) { if (!(e instanceof TransportError)) throw e; lastError = e.message; if (attempt + 1 < attempts) await sleep(Math.min(200 * (attempt + 1), 1000)); }
        }
        if (observed === null) { trace.add(step(node.id, node.kind, `${label} failed: ${lastError}`)); return outcome("error", tree.name, { reason: "transport", say: `${node.id}: ${lastError}` }); }
        put(state, into, observed);
        source = observed;
        summary = `${label} -> ${briefResult(sourceName, observed)}`;
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
    do_lookup(tree, node, scope, trace, depth) {
      const on = statePath(node.get("on"));
      const value = resolve(on, scope);
      const into = node.get("into");
      let found = MISSING, chosen = null;
      for (const row of node.get("table")) {
        const cond = Object.fromEntries(Object.entries(row).filter(([k]) => k !== "value" && k !== "note"));
        if (holds(cond, value)) { found = row.value; chosen = condText(cond); break; }
      }
      if (isMissing(found)) {
        if (!("default" in node.spec)) {
          trace.gap(node.id, on, plain(value));
          trace.add(step(node.id, "lookup", `${on} = ${brief(value)}: no row`, { observed: plain(value), depth }));
          return outcome("error", tree.name, { reason: "gap", say: `${node.id}: no row for ${on} = ${brief(value)}.`, output: { gap: trace.gaps[trace.gaps.length - 1] } });
        }
        found = node.spec.default; chosen = "default";
      }
      const result = render(found, scope);
      put(scope.state, into, result);
      return [step(node.id, "lookup", `${on} = ${brief(value)}; ${into}=${brief(result)}`, { observed: plain(value), chosen }), node.next];
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

  // ------------------------------------------------------------ the hub in a page
  // ------------------------------------------------------------ growing (grow.py, suggestions.py)
  // The branch a gap asks for, by the same rules as grow.draft_branch, on a
  // tree's raw table; and the checks suggestions.problem makes of what an
  // agent sends. Rules only: an agent's words are data these check.
  const tomlStr = (s) => String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n");
  function tomlValue(v) {
    if (isBool(v)) return v ? "true" : "false";
    if (isNum(v)) return Number.isInteger(v) ? String(v) : String(v);
    if (Array.isArray(v)) return "[" + v.map(tomlValue).join(", ") + "]";
    return '"' + tomlStr(v === null || v === undefined ? "None" : String(v)) + '"';
  }
  const tomlInline = (cond) => "{ " + Object.keys(cond).map((k) => `${k} = ${tomlValue(cond[k])}`).join(", ") + " }";
  function growSlug(value, limit = 24) {
    let text = value === null || value === undefined ? "missing" : (typeof value === "string" ? value : pyrepr(value)).toLowerCase();
    text = text.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return text.slice(0, limit).replace(/-+$/, "") || "value";
  }
  const safeHolds = (cond, value) => { try { return holds(cond, value); } catch (e) { return false; } };
  function conditionFor(observed, branches) {
    if (observed === null || observed === undefined) return [{ exists: false }, "the value was missing"];
    if (typeof observed === "string" && /^<(str|dict|list|int|float|bool)>$/.test(observed)) return [{ type: observed.slice(1, -1) }, "a report carries a long or structured value as its type"];
    if (isNum(observed)) {
      const covered = [];
      for (const b of branches) {
        const w = isObj(b) ? b.when : null;
        if (!isObj(w)) continue;
        if (Array.isArray(w.between) && w.between.length === 2) { const lo = Number(w.between[0]), hi = Number(w.between[1]); if (!Number.isNaN(lo) && !Number.isNaN(hi)) covered.push([lo, hi]); }
        else if (isNum(w.eq)) covered.push([w.eq, w.eq]);
      }
      if (covered.length && Number.isInteger(observed)) {
        const below = covered.filter(([, hi]) => hi < observed).map(([, hi]) => hi), above = covered.filter(([lo]) => lo > observed).map(([lo]) => lo);
        if (below.length && above.length && below.concat(above).every(Number.isInteger)) {
          const b = Math.max(...below), a = Math.min(...above), lo = b + 1, hi = a - 1;
          if (lo <= hi) return [{ between: [lo, hi] }, `the value falls in the hole between ${b} and ${a}, which the ranges around it leave open`];
        }
      }
    }
    return [{ eq: observed }, "the value itself"];
  }
  // raw: a tree's table (as trees.json carries it); gap: {node, on, observed}. null when the gap names no decide or lookup node or a branch covers the value.
  // For a lookup the draft is a row (list "table") whose value is the value seen, and there is no new node.
  function draftBranch(raw, gap) {
    const nodes = (raw && raw.nodes) || {};
    const id = String(gap.node === undefined ? "" : gap.node), node = nodes[id];
    if (!isObj(node) || (node.kind !== "decide" && node.kind !== "lookup")) return null;
    const on = String(gap.on || node.on || ""), observed = gap.observed === undefined ? null : gap.observed;
    const branches = node.kind === "lookup"
      ? (node.table || []).filter(isObj).map((r) => ({ when: Object.fromEntries(Object.entries(r).filter(([k]) => k !== "value" && k !== "note")) }))
      : (node.branches || []).filter(isObj);
    if (branches.some((b) => "when" in b && safeHolds(b.when, observed))) return null;
    const [when, reason] = conditionFor(observed, branches);
    if (node.kind === "lookup") {
      const row = "{ " + Object.keys(when).map((k) => `${k} = ${tomlValue(when[k])}`).join(", ") + `, value = ${tomlValue(observed === null ? "" : observed)} },`;
      return { tree: raw.name, node: id, on, observed, when, then: "", branch: row, reason, list: "table" };
    }
    const base = `${id}-${growSlug(observed)}`;
    let then = base, n = 2;
    while (has(nodes, then)) then = `${base}-${n++}`;
    return { tree: raw.name, node: id, on, observed, when, then, branch: `{ when = ${tomlInline(when)}, then = "${then}" },`, reason, list: "branches" };
  }
  // The tree a draft and a meaning make, as a new raw table: the branch before the else, the node after the rest. The caller loads it (new Tree) to see it holds.
  function growTree(raw, draft, meaning) {
    const out = JSON.parse(JSON.stringify(raw));
    const node = out.nodes[draft.node];
    const branches = node.branches || (node.branches = []);
    const at = branches.findIndex((b) => isObj(b) && "else" in b);
    branches.splice(at < 0 ? branches.length : at, 0, { when: draft.when, then: draft.then });
    out.nodes[draft.then] = Object.assign({ kind: meaning.kind }, meaning.kind === "fail" ? { reason: meaning.reason || growSlug(draft.then, 40) } : {}, { say: meaning.say });
    return out;
  }

  const SUGGESTION_KINDS = ["branch", "trigger", "tree"], SAY_ROOTS = ["inputs", "state", "tree"];
  function suggestionProblem(s) {
    if (!isObj(s)) return "a suggestion is a table";
    const kind = s.kind;
    if (!SUGGESTION_KINDS.includes(kind)) return `kind is one of ${SUGGESTION_KINDS.join(", ")}`;
    const why = s.why === undefined ? "" : s.why;
    if (typeof why !== "string" || why.length > 500) return "why is text of at most 500 characters";
    const by = s.by === undefined ? {} : s.by;
    if (!isObj(by) || !Object.values(by).every((v) => typeof v === "string" && v.length <= 120)) return "by is a table of short strings: agent, model, engine";
    if ((kind === "branch" || kind === "trigger") && !(typeof s.tree === "string" && s.tree)) return "a branch or a trigger names its tree";
    if (kind === "branch") {
      if (typeof s.node !== "string" || !s.node) return "a branch names the decide node it belongs to";
      if (!("observed" in s)) return "a branch says what value it is for (observed)";
      const m = s.meaning;
      if (!isObj(m) || !["done", "fail"].includes(m.kind)) return "a branch's meaning is a done or a fail";
      if (typeof m.say !== "string" || !m.say.trim() || m.say.length > 300) return "a meaning says something, in at most 300 characters";
      for (const mm of m.say.matchAll(PLACEHOLDER)) {
        const head = mm[1].split("|")[0].trim().split(/[.[|\s]/)[0];
        if (!SAY_ROOTS.includes(head)) return `say may read ${SAY_ROOTS.join(", ")}, not ${head}`;
      }
      if ("reason" in m && !(typeof m.reason === "string" && /^[a-z0-9][a-z0-9-]{0,39}$/.test(m.reason))) return "reason is a short lowercase slug, like no-such-place";
    }
    if (kind === "trigger") {
      const pattern = s.pattern;
      if (typeof pattern !== "string" || !pattern.trim() || pattern.length > 200) return "a trigger is a pattern of at most 200 characters";
      try { new Trigger(pattern); } catch (e) { if (e instanceof TriggerError) return `pattern: ${e.message}`; throw e; }
      const covers = s.covers === undefined ? [] : s.covers;
      if (!Array.isArray(covers) || !covers.length || covers.length > 10 || !covers.every((c) => typeof c === "string" && c.length > 0 && c.length <= 200)) return "a trigger names the sentences it covers (1 to 10)";
    }
    if (kind === "tree") {
      if (typeof s.text !== "string" || !s.text.trim() || s.text.length > 200) return "a tree suggestion carries the sentence it would answer";
      const d = s.description === undefined ? "" : s.description;
      if (typeof d !== "string" || d.length > 200) return "description is one line";
      const hints = s.hosts_hint === undefined ? [] : s.hosts_hint;
      if (!Array.isArray(hints) || !hints.every((h) => typeof h === "string" && h.length <= 100)) return "hosts_hint is a list of host names (a note, never a host)";
    }
    return null;
  }
  function suggestionKey(s) {
    const parts = s.kind === "branch" ? [s.kind, s.tree, s.node, s.on === undefined ? null : s.on, s.observed === undefined ? null : s.observed]
      : s.kind === "trigger" ? [s.kind, s.tree, s.pattern] : [s.kind, String(s.text === undefined ? "" : s.text).trim().toLowerCase()];
    return JSON.stringify(parts);
  }
  const SUGGESTION_FIELDS = ["kind", "tree", "node", "on", "observed", "meaning", "pattern", "covers", "text", "description", "hosts_hint", "why", "by", "evidence"];
  function suggestionClean(s) {
    const out = {};
    for (const k of SUGGESTION_FIELDS) if (has(s, k)) out[k] = s[k];
    if (isObj(out.meaning)) { const m = {}; for (const k of ["kind", "say", "reason"]) if (has(out.meaning, k)) m[k] = out.meaning[k]; out.meaning = m; }
    if (out.evidence !== undefined) out.evidence = Array.isArray(out.evidence) ? out.evidence.filter(isObj).slice(0, 10) : [];
    return JSON.parse(JSON.stringify(out));
  }
  const grow = { draftBranch, growTree, conditionFor, problem: suggestionProblem, key: suggestionKey, clean: suggestionClean, SAY_ROOTS };

  // A browser that has joined no hub is a hub of its own, for as long as the
  // page is open: it holds trees (the catalog's, as a page loads them), reads
  // sentences, runs the trees and keeps the gaps and the unanswered sentences
  // in memory. It answers the same paths with the same shapes as a service
  // (service.py), so the console talks to it exactly as it talks to a hub.
  // What it cannot do is reach what only a box can: files, programs, serial
  // lines, brokers, a browser to drive, and sites that do not let a page read
  // them. Those trees answer with the reason, and joining a hub is the cure.
  const UNANSWERED_LIMIT = 200;
  class LocalHub {
    constructor(opts) {
      opts = opts || {};
      this.name = opts.name || "this browser";
      this.version = opts.version || VERSION;
      this.clock = opts.clock || (() => new Date());
      this.wall = opts.wall || (() => Date.now() / 1000);
      this.runner = new Runner({ transport: opts.transport || new FetchTransport(), adapters: opts.adapters || null, clock: this.clock, env: opts.env });
      this.errors = {}; this.held = {};
      this.gaps = new Map(); this.unanswered = new Map(); this.suggestions = new Map();
      this.runs = 0; this.started = this.wall();
      for (const raw of opts.trees || []) this.add(raw);
    }
    // where: what to call the tree in errors (a catalog id). The first tree of a name is the one held.
    add(raw, where) {
      const name = raw && raw.name;
      if (name && this.runner.get(name) && this.held[name] !== (where || name)) { this.errors[where || name] = `a tree named ${pyrepr(name)} is already held, from ${this.held[name]}`; return null; }
      try { const t = this.runner.add(raw); this.held[t.name] = where || t.name; delete this.errors[where || t.name]; return t; }
      catch (e) { if (!(e instanceof TreeError)) throw e; this.errors[where || (raw && raw.name) || "?"] = e.message; return null; }
    }
    trees() { return Object.keys(this.runner.trees).sort().map((n) => this.runner.trees[n]); }
    health() {
      return { status: "ok", name: this.name, trees: this.trees().map((t) => t.name), errors: this.errors, runs: this.runs, gaps: this.gaps.size, unanswered: this.unanswered.size, suggestions: this.suggestions.size, uptime_s: Math.floor(this.wall() - this.started), watches: 0 };
    }
    capabilities() { return { name: this.name, trees: this.trees().map((t) => t.capability()) }; }
    noteGaps(gaps) {
      const now = this.wall();
      for (const g of gaps) {
        const key = JSON.stringify([g.tree, g.node, g.on, g.observed === undefined ? null : g.observed]);
        const cur = this.gaps.get(key);
        if (cur) { cur.count += 1; cur.last_seen = Math.max(cur.last_seen, now); }
        else this.gaps.set(key, { tree: g.tree, node: g.node, on: g.on, observed: g.observed === undefined ? null : g.observed, count: 1, first_seen: now, last_seen: now });
      }
    }
    noteUnanswered(text) {
      const now = this.wall(), key = normalise(text).toLowerCase() || text;
      let e = this.unanswered.get(key);
      if (!e) { e = { text: normalise(text) || text, count: 0, first: now, last: now, proposal: propose(text) }; this.unanswered.set(key, e); }
      e.count += 1; e.last = Math.max(e.last, now);
      while (this.unanswered.size > UNANSWERED_LIMIT) {
        let oldest = null;
        for (const [k, v] of this.unanswered) if (oldest === null || v.last < this.unanswered.get(oldest).last) oldest = k;
        this.unanswered.delete(oldest);
      }
    }
    // What agents sent, as suggestions.py keeps it: checked, counted, bounded, never applied.
    noteSuggestions(items) {
      const now = this.wall(), refused = [];
      let kept = 0;
      items.forEach((s, index) => {
        const why = suggestionProblem(s);
        if (why) { refused.push({ index, why }); return; }
        const c = suggestionClean(s), k = suggestionKey(c);
        let e = this.suggestions.get(k);
        if (!e) { e = Object.assign({}, c, { count: 0, first: now, last: now }); this.suggestions.set(k, e); }
        else Object.assign(e, c);
        e.count += 1; e.last = Math.max(e.last, now); kept += 1;
        while (this.suggestions.size > UNANSWERED_LIMIT) {
          let oldest = null;
          for (const [kk, v] of this.suggestions) if (oldest === null || v.last < this.suggestions.get(oldest).last) oldest = kk;
          this.suggestions.delete(oldest);
        }
      });
      return { status: "ok", kept, refused };
    }
    finish(o, body) {
      this.runs += 1;
      if (o.trace && o.trace.gaps && o.trace.gaps.length) this.noteGaps(o.trace.gaps);
      return outcomeDict(o, body.trace === undefined ? true : Boolean(body.trace));
    }
    async intent(body) {
      const text = body.text;
      if (typeof text !== "string" || !text.trim()) return { status: "error", reason: "bad-request", say: 'Send {"text": "..."}.' };
      const o = await this.runner.runIntent(text, Math.trunc(Number(body.depth || 0)));
      if (o.reason === "no-match") this.noteUnanswered(text);
      return this.finish(o, body);
    }
    async run(body) {
      if (typeof body.tree !== "string") return { status: "error", reason: "bad-request", say: 'Send {"tree": "...", "inputs": {...}}.' };
      return this.finish(await this.runner.runTree(body.tree, body.inputs || {}, Math.trunc(Number(body.depth || 0))), body);
    }
    async resume(body) {
      const snap = body.snapshot;
      if (!isObj(snap) || !("tree" in snap)) return { status: "error", reason: "bad-request", say: 'Send {"snapshot": {...}, "answer": ...}.' };
      return this.finish(await this.runner.resume(snap, body.answer === undefined ? null : body.answer), body);
    }
    // (method, path, body) -> [status, payload], as service.py's route.
    async route(method, path, body) {
      method = String(method || "GET").toUpperCase();
      path = "/" + String(path || "").split("?")[0].replace(/^\/+/, "");
      body = isObj(body) ? body : {};
      if (method === "GET" && path === "/api") return [200, { pyrtan: "browser", name: this.name, version: this.version, auth: "none", endpoints: ["/health", "/capabilities", "/intent", "/run", "/resume", "/gaps", "/unanswered", "/suggestions", "/watches"] }];
      if (method === "GET" && path === "/health") return [200, this.health()];
      if (method === "GET" && path === "/capabilities") return [200, this.capabilities()];
      if (method === "GET" && path === "/gaps") return [200, { gaps: [...this.gaps.values()].map((g) => Object.assign({}, g)).sort((a, b) => b.count - a.count || a.last_seen - b.last_seen) }];
      if (method === "GET" && path === "/unanswered") return [200, { unanswered: [...this.unanswered.values()].map((e) => Object.assign({}, e)).sort((a, b) => b.count - a.count || b.last - a.last) }];
      if (method === "GET" && path === "/watches") return [200, { count: 0, watches: [], watching: false }];
      if (method === "GET" && path === "/suggestions") return [200, { suggestions: [...this.suggestions.values()].map((e) => Object.assign({}, e)).sort((a, b) => b.count - a.count || b.last - a.last) }];
      if (method === "POST" && path === "/suggestions") {
        const items = Array.isArray(body.suggestions) ? body.suggestions : body.kind ? [body] : null;
        if (items === null) return [400, { status: "error", reason: "bad-request", say: 'Send {"suggestions": [{"kind": "branch"|"trigger"|"tree", ...}]}.' }];
        if (items.length > 50) return [400, { status: "error", reason: "too-many", say: "At most 50 suggestions at a time." }];
        return [200, this.noteSuggestions(items)];
      }
      if (method === "POST" && path === "/intent") return [200, await this.intent(body)];
      if (method === "POST" && path === "/run") return [200, await this.run(body)];
      if (method === "POST" && path === "/resume") return [200, await this.resume(body)];
      return [404, { status: "error", reason: "not-found", say: `No ${method} ${path} here.` }];
    }
  }

  return { MISSING, Tree, Runner, LocalHub, Reading, nlp, intent, read, propose, grow, Trace, FetchTransport, RecordedTransport, Response, render, resolve, holds, extract, outcomeDict, fromDict, pyrepr, pyJson, html, TemplateError, TreeError, TransportError, ConditionError, SOURCES, adapterKey, version: VERSION };
});
