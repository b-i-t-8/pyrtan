// Run every vector through pyrtan.js and compare with what Python said.
//   node tests/conformance/run.cjs            (exit 1 on any difference)
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");
const Pyrtan = require(path.join(ROOT, "src", "pyrtan", "js", "pyrtan.js"));
const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, "vectors.json"), "utf8"));
const fixture = JSON.parse(fs.readFileSync(path.join(ROOT, vectors.fixture), "utf8"));
const clock = () => new Date(vectors.clock);

// What must agree: the outcome and the path, not the timings or the summaries' wording.
function shape(d) {
  return {
    status: d.status, tree: d.tree, say: d.say === undefined ? null : d.say, reason: d.reason === undefined ? null : d.reason,
    output: d.output === undefined ? null : d.output, inputs: d.inputs || {}, question: d.question === undefined ? null : d.question,
    steps: (d.trace ? d.trace.steps : []).map((s) => ({ node: s.node, kind: s.kind, chosen: s.chosen === undefined ? null : s.chosen, next: s.next === undefined ? null : s.next, depth: s.depth || 0 })),
    gaps: (d.trace ? d.trace.gaps : []).map((g) => ({ node: g.node, on: g.on, observed: g.observed })),
    // The intent step is the reading, and it is compared word for word.
    intent: d.trace && d.trace.steps.length && d.trace.steps[0].kind === "intent" ? { summary: d.trace.steps[0].summary, observed: d.trace.steps[0].observed } : null,
  };
}
const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x) ? Object.keys(x).sort().reduce((o, kk) => ((o[kk] = x[kk]), o), {}) : x));

let failed = 0, total = 0;
function compare(label, expect, got) {
  total++;
  const a = canon(expect), b = canon(got);
  if (a === b) { console.log(`ok    ${label}`); return; }
  failed++;
  console.log(`DIFF  ${label}`);
  if (expect && got && typeof expect === "object" && !Array.isArray(expect)) {
    for (const k of new Set([...Object.keys(expect), ...Object.keys(got)])) if (canon(expect[k]) !== canon(got[k])) console.log(`      ${k}:\n        python: ${canon(expect[k]).slice(0, 400)}\n        js:     ${canon(got[k]).slice(0, 400)}`);
  } else console.log(`        python: ${a.slice(0, 400)}\n        js:     ${b.slice(0, 400)}`);
}
// A hub's answer, as compared: an outcome by its shape (with the snapshot), anything else whole.
function hubShape(d) {
  if (d && typeof d === "object" && "status" in d && "tree" in d) return Object.assign(shape(d), { snapshot: d.snapshot === undefined ? null : d.snapshot });
  if (d && Array.isArray(d.gaps)) return { gaps: d.gaps.map((g) => Object.assign({}, g, { first_seen: undefined, last_seen: undefined })) };
  if (d && Array.isArray(d.unanswered)) return { unanswered: d.unanswered.map((e) => Object.assign({}, e, { first: undefined, last: undefined })) };
  return d;
}

(async () => {
  for (const run of vectors.runs) {
    const runner = new Pyrtan.Runner({ trees: Object.values(vectors.trees), transport: new Pyrtan.RecordedTransport(fixture), clock });
    const got = Pyrtan.outcomeDict(run.intent ? await runner.runIntent(run.intent) : await runner.runTree(run.tree, run.inputs));
    compare(run.intent ? `run "${run.intent}"` : `run ${run.tree} ${JSON.stringify(run.inputs)}`, shape(run.expect), shape(got));
  }
  const all = new Pyrtan.Runner({ trees: Object.values(vectors.trees).concat(vectors.language_trees), clock });
  for (const r of vectors.readings) {
    const reading = all.read(r.text);
    compare(`read "${r.text}"`, r.expect, Object.assign(reading.toDict(), { step: reading.step() }));
  }
  const fns = { clean: (t) => Pyrtan.nlp.clean(t), singular: (t) => Pyrtan.nlp.singular(t), plural: (t) => Pyrtan.nlp.plural(t), parse_number: (t) => Pyrtan.nlp.parseNumber(t).value, parse_duration: (t) => Pyrtan.nlp.parseDuration(t).value, parse_time: (t) => Pyrtan.nlp.parseTime(t).value, parse_date: (t) => Pyrtan.nlp.parseDate(t, clock()).value, parse_host: (t) => Pyrtan.nlp.parseHost(t).value, parse_url: (t) => Pyrtan.nlp.parseUrl(t).value, parse_email: (t) => Pyrtan.nlp.parseEmail(t).value };
  for (const c of vectors.nlp) {
    let got;
    try { got = { fn: c.fn, text: c.text, value: fns[c.fn](c.text) }; } catch (e) { if (!(e instanceof Pyrtan.nlp.ParseError)) throw e; got = { fn: c.fn, text: c.text, error: e.message }; }
    compare(`${c.fn} ${JSON.stringify(c.text)}`, c, got);
  }
  for (const p of vectors.proposals) compare(`propose ${JSON.stringify(p.text)}`, p.expect, Pyrtan.propose(p.text));
  // The hub in a page: the same requests, the same answers as a service.
  const transport = new Pyrtan.RecordedTransport(fixture);
  const hub = new Pyrtan.LocalHub({ trees: Object.values(vectors.trees).concat(vectors.language_trees), transport, adapters: transport, clock });
  for (const x of vectors.hub) {
    const [status, payload] = await hub.route(x.method, x.path, x.body === null ? undefined : JSON.parse(JSON.stringify(x.body)));
    compare(`hub ${x.method} ${x.path} ${x.body ? JSON.stringify(x.body).slice(0, 60) : ""}`, { status: x.status, body: hubShape(x.expect) }, { status, body: hubShape(JSON.parse(JSON.stringify(payload))) });
  }
  console.log(`${total - failed}/${total} agree`);
  process.exit(failed ? 1 : 0);
})();
