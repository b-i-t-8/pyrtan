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
    output: d.output === undefined ? null : d.output, inputs: d.inputs || {},
    steps: (d.trace ? d.trace.steps : []).map((s) => ({ node: s.node, kind: s.kind, chosen: s.chosen === undefined ? null : s.chosen, next: s.next === undefined ? null : s.next, depth: s.depth || 0 })),
    gaps: (d.trace ? d.trace.gaps : []).map((g) => ({ node: g.node, on: g.on, observed: g.observed })),
  };
}
const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x) ? Object.keys(x).sort().reduce((o, kk) => ((o[kk] = x[kk]), o), {}) : x));

(async () => {
  let failed = 0;
  for (const run of vectors.runs) {
    const runner = new Pyrtan.Runner({ trees: Object.values(vectors.trees), transport: new Pyrtan.RecordedTransport(fixture), clock });
    const got = Pyrtan.outcomeDict(await runner.runTree(run.tree, run.inputs));
    const a = canon(shape(run.expect)), b = canon(shape(got));
    const label = run.intent ? `"${run.intent}"` : `${run.tree} ${JSON.stringify(run.inputs)}`;
    if (a === b) { console.log(`ok    ${label}`); continue; }
    failed++;
    console.log(`DIFF  ${label}`);
    const ea = shape(run.expect), eb = shape(got);
    for (const k of Object.keys(ea)) if (canon(ea[k]) !== canon(eb[k])) console.log(`      ${k}:\n        python: ${canon(ea[k]).slice(0, 400)}\n        js:     ${canon(eb[k]).slice(0, 400)}`);
  }
  console.log(`${vectors.runs.length - failed}/${vectors.runs.length} agree`);
  process.exit(failed ? 1 : 0);
})();
