// Run identity.json through identity.js and compare with what Python said.
//   node tests/conformance/identity.cjs        (exit 1 on any difference)
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");
const Id = require(path.join(ROOT, "src", "pyrtan", "js", "identity.js"));
const v = JSON.parse(fs.readFileSync(path.join(__dirname, "identity.json"), "utf8"));
const bytes = (h) => Uint8Array.from(h.match(/../g) || [], (x) => parseInt(x, 16));

(async () => {
  let failed = 0, total = 0;
  const check = (label, python, js) => {
    total++;
    const a = Id.canonical(python === undefined ? null : python), b = Id.canonical(js === undefined ? null : js);
    if (a === b) { console.log(`ok    ${label}`); return; }
    failed++;
    console.log(`DIFF  ${label}\n        python: ${a.slice(0, 400)}\n        js:     ${b.slice(0, 400)}`);
  };
  for (const s of v.sha3_256) check(`sha3-256 of ${s.in.length / 2} bytes`, s.out, Id.hex(Id.sha3_256(bytes(s.in))));
  for (const i of v.ids) {
    check(`id of ${i.key.slice(0, 8)}…`, i.id, Id.encode(bytes(i.key)));
    check(`key of ${Id.short(i.id)}`, i.key, Id.hex(Id.decode(i.id)));
  }
  for (const t of v.refuse) check(`refuses ${t.slice(0, 24)}…`, false, Id.valid(t));
  for (const t of v.accept) check(`accepts ${t.slice(0, 24)}…`, true, Id.valid(t));
  for (const c of v.canonical) check(`canonical ${c.canonical.slice(0, 40)}`, c.canonical, Id.canonical(c.value));
  check("a pasted note: good", null, Id.noteProblem(v.note, v.note.device, v.note.issued));
  check("a pasted note: another key's", true, /not this browser's key/.test(Id.noteProblem(v.note, v.ids[2].id, v.note.issued)));
  check("a pasted note: expired", "the note has expired", Id.noteProblem(v.note, v.note.device, v.note.expires + 1));
  for (const r of v.requests) {
    const label = `${r.method} ${r.path}${r.note ? " (device)" : ""}`;
    check(`message ${label}`, r.message, new TextDecoder().decode(Id.requestMessage(r.audience, r.method, r.path, r.date, r.body)));
    const key = await Id.fromSeedAndPublic(bytes(r.seed), bytes(r.public));
    check(`headers ${label}`, r.headers, await Id.signRequest(key, r.audience, r.method, r.path, r.body, r.note, r.date));
  }
  console.log(`${total - failed}/${total} agree`);
  process.exit(failed ? 1 : 0);
})();
