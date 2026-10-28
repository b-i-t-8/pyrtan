"""A person's devices share their gaps with each other, with no hub (sync.js).

The rules run in node: who a bundle may come from, what a link's hello
binds, that a bundle passed between devices is never counted twice, and
that what one device met reaches a third through a second. Then two
browsers, two devices of one identity, link in Chromium with a code
carried by hand, and a stranger's browser cannot.
"""

import json
import shutil
import subprocess

import pytest

from pyrtan.cli import main

from conftest import ROOT
from test_console import GAPPY, _node_playwright

JS = ROOT / "src" / "pyrtan" / "js"

RULES = r"""
const [js] = process.argv.slice(2);
const I = require(js + "/identity.js"), S = require(js + "/sync.js")(I), Pyrtan = require(js + "/pyrtan.js");
async function root() { const r = await I.createRoot(); const k = await I.rootFromSeed(r.seed, r.id); return { role: "root", id: k.id, privateKey: k.privateKey, document: await I.makeIdentity(k) }; }
async function device(of, opts) { const k = await I.generate(); return { role: "device", id: k.id, privateKey: k.privateKey, note: await I.makeDevice(of, k.id, ["ask"], opts) }; }
const own = { gaps: [{ tree: "t", node: "n", on: "x", observed: 2, count: 3, first_seen: 1, last_seen: 2 }], unanswered: [{ text: "hello there", count: 1, first: 1, last: 1 }] };
// Two sessions, each one's send the other's receive, until both are quiet.
async function link(a, b, id) {
  const queue = [];
  const sa = new S.Session(Object.assign({ send: (m) => queue.push([sb, m]) }, a)), sb = new S.Session(Object.assign({ send: (m) => queue.push([sa, m]) }, b));
  await sa.start(id || "fp-a,fp-b"); await sb.start(id || "fp-a,fp-b");
  while (queue.length) { const [to, m] = queue.shift(); await to.receive(JSON.parse(JSON.stringify(m))); }
  return [sa, sb];
}
const side = (key, hub, events) => ({ key, revoked: () => [], own: () => hub.own(), held: () => [...hub.others.values()], take: (b) => hub.takeOther(b), note: (e, d) => events.push([e, typeof d === "object" ? d.took : d]) });
(async () => {
  const out = {};
  const me = await root(), phone = await device(me, { label: "phone" }), tablet = await device(me, { label: "tablet" });
  const stranger = await root();
  const b = await S.makeBundle(phone, own);
  out.ok = await S.checkBundle(b, me.id, []);
  out.root_bundle = [await S.checkBundle(await S.makeBundle(me, own), me.id, []), "document" in (await S.makeBundle(me, own))];
  out.other_root = await S.checkBundle(b, stranger.id, []);
  out.revoked = await S.checkBundle(b, me.id, [phone.id]);
  const tampered = JSON.parse(JSON.stringify(b)); tampered.gaps[0].count = 99;
  out.tampered = await S.checkBundle(tampered, me.id, []);
  // A stranger's device claiming my root: its note is not my root's signature.
  const theirs = await device(stranger); theirs.note = Object.assign({}, theirs.note, { root: me.id });
  out.forged_note = await S.checkBundle(await S.makeBundle(theirs, own), me.id, []);
  out.expired = await S.checkBundle(await S.makeBundle(await device(me, { issued: 1, expires: 2 }), own), me.id, []);
  out.stranger_document = await S.checkBundle(Object.assign({}, b, { document: stranger.document }), me.id, []);
  // A hello is for one link.
  const h = await S.makeHello(phone, "fp-a,fp-b");
  out.hello = [await S.checkHello(h, me.id, "fp-a,fp-b", []), await S.checkHello(h, me.id, "fp-a,fp-c", [])];
  out.link_of = S.linkOf("v=0\r\na=fingerprint:sha-256 AA:BB\r\n", "a=fingerprint:sha-256 01:02\r\n");

  // Root and phone link: each takes the other's, once.
  const hubs = {}, events = {};
  for (const [name, key] of [["me", me], ["phone", phone], ["tablet", tablet], ["stranger", stranger]]) { hubs[name] = new Pyrtan.LocalHub({ device: key.id }); events[name] = []; }
  hubs.me.noteGaps([{ tree: "t", node: "n", on: "x", observed: 1 }]);
  hubs.phone.noteGaps([{ tree: "t", node: "n", on: "x", observed: 2 }, { tree: "t", node: "n", on: "x", observed: 2 }]);
  const [sMe, sPhone] = await link(side(me, hubs.me, events.me), side(phone, hubs.phone, events.phone));
  const rows = async (hub) => (await hub.route("GET", "/gaps"))[1].gaps.map((g) => [g.observed, g.count, g.device === hub.device ? "self" : g.device === me.id ? "me" : g.device === phone.id ? "phone" : "tablet"]);
  out.me_after = await rows(hubs.me); out.phone_after = await rows(hubs.phone);
  out.events = JSON.parse(JSON.stringify([events.me, events.phone]));
  // Pushed again and again: still one row per device, never added up.
  await sPhone.push(true); await sPhone.push(true);
  out.again = await rows(hubs.phone);
  // The tablet links with the phone only, and gets the root's gaps through it.
  await link(side(tablet, hubs.tablet, events.tablet), side(phone, hubs.phone, []), "fp-c,fp-d");
  out.relayed = (await rows(hubs.tablet)).sort();
  // A stranger links with me: refused both ways, and nothing crosses.
  await link(side(stranger, hubs.stranger, events.stranger), side(me, hubs.me, events.me), "fp-e,fp-f");
  out.stranger = [events.stranger, (await rows(hubs.stranger)).length, hubs.me.others.size];

  // Delivered as fast as they come, not one awaited after another: bundles right behind a hello still count.
  const eager = { a: new Pyrtan.LocalHub({ device: me.id }), b: new Pyrtan.LocalHub({ device: tablet.id }) };
  eager.b.noteGaps([{ tree: "t", node: "n", on: "x", observed: 5 }]);
  const pendingE = [];
  const ea = new S.Session(Object.assign({ send: (m) => pendingE.push(eb.receive(JSON.parse(JSON.stringify(m)))) }, side(me, eager.a, [])));
  const eb = new S.Session(Object.assign({ send: (m) => pendingE.push(ea.receive(JSON.parse(JSON.stringify(m)))) }, side(tablet, eager.b, [])));
  ea.start("fp-g,fp-h"); eb.start("fp-g,fp-h");
  for (let i = 0; i < 100 && (pendingE.length || i < 3); i++) { const batch = pendingE.splice(0); await Promise.all(batch); await new Promise((r) => setTimeout(r, 5)); }
  out.eager = [eager.a.others.size, eager.b.others.size];

  // A file carries the same bundles, checked the same way.
  const file = await S.makeFile(phone, hubs.phone.own(), [...hubs.phone.others.values()]);
  const fresh = new Pyrtan.LocalHub({ device: tablet.id });
  out.file = await S.readFile(JSON.parse(JSON.stringify(file)), tablet, () => [], (x) => fresh.takeOther(x));
  out.file.from = out.file.from.map((d) => (d === me.id ? "me" : "phone")).sort();
  try { await S.readFile(file, stranger, () => [], () => true); out.file_stranger = null; } catch (e) { out.file_stranger = e.message; }

  // Codes and parts.
  const sdp = "v=0\r\n" + "a=candidate:1 1 udp 2122260223 192.168.1.20 54321 typ host\r\n".repeat(8);
  const code = await S.encodeCode("o", sdp);
  out.code = [code.startsWith("pyrgaps1.z."), (await S.decodeCode(code)).sdp === sdp, (await S.decodeCode("https://pyrtan.com/console/#sync=" + code)).kind];
  try { await S.decodeCode("hello"); } catch (e) { out.bad_code = e.message; }
  const parts = new S.Parts(), big = { t: "bundles", items: ["x".repeat(40000)] };
  const pieces = parts.split(big); let back;
  for (const p of pieces.reverse()) back = parts.join(p) || back;
  out.parts = [pieces.length, JSON.stringify(back) === JSON.stringify(big)];
  // A QR code this encoder made, read back by OpenCV when it was written; the same text must make the same modules.
  out.qr = I.hex(I.sha256(new TextEncoder().encode(S.qr("https://pyrtan.com/console/#sync=pyrgaps1.z.Q").map((r) => r.map((d) => (d ? 1 : 0)).join("")).join("\n"))));
  out.qr_size = S.qr("x".repeat(600)).length;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_only_devices_of_one_identity_share_their_gaps(tmp_path):
    script = tmp_path / "rules.cjs"
    script.write_text(RULES, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(JS)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["ok"] is None
    assert got["root_bundle"] == [None, True]
    assert got["other_root"] == "it is another identity's"
    assert got["revoked"].endswith("is revoked")
    assert got["tampered"] == "its signature does not hold"
    assert got["forged_note"] == "its device note is not signed by your identity"
    assert got["expired"] == "the note has expired"
    assert got["stranger_document"] == "only the root carries its identity document"
    assert got["hello"] == [None, "it is for another link"]
    assert got["link_of"] == "sha-256 01:02,sha-256 aa:bb"
    assert got["me_after"] == [[2, 2, "phone"], [1, 1, "self"]]
    assert got["phone_after"] == [[2, 2, "self"], [1, 1, "me"]]
    assert got["events"] == [[["linked", got["events"][0][0][1]], ["took", 1]], [["linked", got["events"][1][0][1]], ["took", 1]]]
    assert got["again"] == got["phone_after"]
    assert got["relayed"] == [[1, 1, "me"], [2, 2, "phone"]]
    events, held, mine = got["stranger"]
    assert events and all(e == "refused" and "another identity's" in why for e, why in events)
    assert held == 0 and mine == 1  # the stranger took nothing, and I still hold only the phone's
    assert got["file"]["took"] == 2 and got["file"]["from"] == ["me", "phone"] and got["file"]["refused"] == []
    assert got["file_stranger"] == "that file is another identity's"
    assert got["code"] == [True, True, "o"]
    assert got["bad_code"] == "that is not a link code from a Pyrtan console"
    assert got["parts"] == [3, True]
    assert got["qr"] == QR_SHA256
    assert got["qr_size"] == 4 * 19 + 17  # 600 bytes: version 19 at level M
    assert got["eager"] == [1, 1]


# The modules of S.qr("https://pyrtan.com/console/#sync=pyrgaps1.z.Q"), version 4-M; read back by OpenCV (QRCodeDetectorAruco) when recorded, as were 30 more from 5 to 2,300 bytes.
QR_SHA256 = "17accba5b3c5230bcb4cd4ed240233f8e05e07604622c726ae46a9c18f2604a2"


PAIRING = r"""
const [js] = process.argv.slice(2);
const I = require(js + "/identity.js"), S = require(js + "/sync.js")(I);
async function root() { const r = await I.createRoot(); const k = await I.rootFromSeed(r.seed, r.id); return { role: "root", id: k.id, privateKey: k.privateKey, document: await I.makeIdentity(k) }; }
async function fresh() { const k = await I.generate(); return { role: "device", id: k.id, privateKey: k.privateKey, note: null }; }
// A root and a new device on one link; decide(request) says what the root's person does.
async function pairUp(me, phone, decide, opts) {
  const o = opts || {}, events = { root: [], device: [] }, queue = [];
  const sign = async (device, may, label) => { const note = await I.makeDevice(me, device, may, { label }); me.document = await I.withDevice(me.document, me, note); return { note, document: o.document || me.document }; };
  const r = new S.Pairing({ key: me, role: "root", sign, send: (m) => queue.push([d, o.tamper ? o.tamper(m) : m]), note: (e, x) => { events.root.push([e, x]); if (e === "request") queue.push(["decide", x]); } });
  const d = new S.Pairing({ key: phone, role: "device", label: "phone", send: (m) => queue.push([r, m]), note: (e, x) => events.device.push([e, x]) });
  await r.start(o.rootLink || "fp-a,fp-b"); await d.start("fp-a,fp-b");
  while (queue.length) {
    const [to, m] = queue.shift();
    if (to === "decide") { const yes = decide(m); if (yes) await r.approve(yes, m.label); else r.refuse("the digits did not match"); continue; }
    await to.receive(JSON.parse(JSON.stringify(m)));
  }
  return events;
}
(async () => {
  const out = {};
  const me = await root(), phone = await fresh();
  const ev = await pairUp(me, phone, () => ["ask", "chat"]);
  const request = ev.root.find((e) => e[0] === "request")[1], waiting = ev.device.find((e) => e[0] === "waiting")[1];
  const paired = ev.device.find((e) => e[0] === "paired");
  out.words = [request.words === waiting, /^\d{3} \d{3}$/.test(waiting), request.device === phone.id, request.label];
  out.paired = paired ? [paired[1].note.root === me.id, paired[1].note.device === phone.id, paired[1].note.may, paired[1].note.label, paired[1].document.devices.length] : null;
  // The paired device now speaks for the root: its bundles pass.
  phone.note = paired[1].note;
  out.speaks = await S.checkBundle(await S.makeBundle(phone, { gaps: [], unanswered: [] }), me.id, []);
  // Refused by the person: nothing signed.
  const before = me.document.devices.length, other = await fresh();
  const no = await pairUp(me, other, () => null);
  out.refused = [no.device.map((e) => e[0]), me.document.devices.length === before];
  // A root on another link (someone in the middle): the request is refused, unsigned.
  const mid = await pairUp(me, await fresh(), () => ["ask"], { rootLink: "fp-x,fp-y" });
  out.middle = [mid.root.map((e) => e[0]), mid.device.map((e) => e[0])];
  // What the device takes: a note its root really signed, for it, with a document that lists it.
  const stranger = await root(), third = await fresh();
  const forged = await pairUp(me, third, () => ["ask"], { tamper: (m) => (m.t === "pair-note" ? Object.assign({}, m, { note: Object.assign({}, m.note, { label: "evil" }) }) : m) });
  out.forged = forged.device.filter((e) => e[0] !== "waiting");
  const elsewhere = await pairUp(me, await fresh(), () => ["ask"], { document: stranger.document });
  out.other_document = elsewhere.device.filter((e) => e[0] !== "waiting");
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_root_pairs_a_device_over_a_link_only_when_the_person_says_so(tmp_path):
    script = tmp_path / "pairing.cjs"
    script.write_text(PAIRING, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(JS)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["words"] == [True, True, True, "phone"]
    assert got["paired"] == [True, True, ["ask", "chat"], "phone", 1]
    assert got["speaks"] is None
    assert got["refused"] == [["waiting", "refused"], True]
    assert got["middle"] == [["refused"], ["waiting", "refused"]]
    assert got["forged"] == [["refused", "the note is not signed by the identity it names"]]
    assert got["other_document"] == [["refused", "the identity document is not that identity's"]]


LINK = r"""
const { chromium } = require("playwright");
const [file] = process.argv.slice(2);
const PASS = "a long sentence nobody else would guess";
(async () => {
  const browser = await chromium.launch();
  const out = { errors: [] };
  const open = async () => {
    const page = await (await browser.newContext({ acceptDownloads: true })).newPage();
    page.on("pageerror", (e) => out.errors.push(String(e)));
    await page.route(/^https?:\/\//, (r) => r.abort());
    await page.goto("file://" + file);
    await page.waitForFunction(() => document.querySelectorAll("#caps tbody tr").length > 0);
    return page;
  };
  const ask = async (page, q) => {
    await page.click('#tabs a[data-tab="ask"]');
    const n = (await page.$$("#out .card")).length;
    await page.fill("#text", q); await page.click("#go");
    await page.waitForFunction((n) => document.querySelectorAll("#out .card").length > n, n);
  };
  const makeRoot = async (page) => {
    await page.click("#identity-tab"); await page.click("#ident-new");
    await page.fill("#ident-pass", PASS); await page.fill("#ident-pass2", PASS); await page.click("#ident-new-form button");
    await page.waitForSelector("#ident-root:not([hidden])", { timeout: 60000 });
  };
  const gaps = (page) => page.$$eval("#gaps tbody tr", (t) => t.map((x) => x.innerText.split("\t")));
  const state = (page) => page.textContent("#sync-state");
  // Link a (offers) and b (answers) by codes carried by hand; resolves when a says what it took or why not.
  const link = async (a, b) => {
    for (const p of [a, b]) await p.click('#tabs a[data-tab="gaps"]');
    await a.click("#sync-offer"); await a.waitForSelector("#sync-code");
    const offer = await a.inputValue("#sync-code");
    if (out.qr === undefined) out.qr = await a.$$eval("#sync-step .qr svg", (s) => s.length);
    await b.click("#sync-have"); await b.fill("#sync-offer-code", offer); await b.click("#sync-answer-form button");
    await b.waitForSelector("#sync-code");
    await a.fill("#sync-answer", await b.inputValue("#sync-code")); await a.click("#sync-accept button");
    await a.waitForFunction(() => /Took|Not linked|Nothing newer/.test(document.querySelector("#sync-state").textContent), null, { timeout: 30000 });
  };

  const laptop = await open(), phone = await open(), stranger = await open();
  await laptop.click('#tabs a[data-tab="gaps"]');
  out.before = await laptop.textContent("#sync-lede");
  await makeRoot(laptop);
  await phone.click("#identity-tab"); await phone.click("#ident-make"); await phone.waitForSelector("#ident-on:not([hidden])");
  const device = (await phone.textContent("#ident-id")).trim();
  await laptop.fill("#dev-code", device); await laptop.fill("#dev-label", "phone"); await laptop.click("#dev-form button[type=submit]");
  await phone.fill("#ident-note", (await laptop.textContent("#dev-out pre")).trim()); await phone.click("#ident-note-form button");
  await phone.waitForFunction(() => document.querySelector("#ident-paired").textContent.includes("speaks for"));
  await ask(laptop, "pick 2"); await ask(phone, "pick 3"); await ask(phone, "pick 3");

  await link(laptop, phone);
  out.linked = [await state(laptop), await state(phone)];
  out.laptop = await gaps(laptop);
  // While both pages are open, a new gap on one shows on the other.
  await ask(phone, "pick 9");
  await laptop.click('#tabs a[data-tab="gaps"]');
  await laptop.waitForFunction(() => document.querySelector("#gaps").innerText.includes("= 9"), null, { timeout: 15000 });
  out.live = await gaps(laptop);
  // Kept across a reload, as its own gaps are.
  await laptop.reload(); await laptop.waitForFunction(() => document.querySelectorAll("#caps tbody tr").length > 0);
  await laptop.click('#tabs a[data-tab="gaps"]'); await laptop.waitForFunction(() => document.querySelectorAll("#gaps tbody tr").length > 2);
  out.reloaded = await gaps(laptop);

  // A stranger with an identity of its own: the link opens, and nothing crosses it.
  await makeRoot(stranger);
  await link(stranger, laptop);
  out.stranger = [await state(stranger), await gaps(stranger)];

  // A file: the phone's, loaded on a laptop that forgot what it held.
  await phone.click('#tabs a[data-tab="gaps"]');
  const [dl] = await Promise.all([phone.waitForEvent("download"), phone.click("#sync-export")]);
  const path = require("path").join(require("os").tmpdir(), `pyrtan-gaps-${process.pid}.json`); await dl.saveAs(path);
  laptop.once("dialog", (d) => d.accept()); await laptop.click("#gaps-forget");
  await laptop.setInputFiles("#sync-file", path);
  await laptop.waitForFunction(() => document.querySelector("#sync-state").textContent.includes("Took"));
  out.file = await gaps(laptop);
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
def test_two_browsers_of_one_identity_link_and_share_their_gaps(tmp_path):
    trees = tmp_path / "trees"
    trees.mkdir()
    (trees / "gappy.toml").write_text(GAPPY, encoding="utf-8")
    out = tmp_path / "console" / "index.html"
    assert main(["console", "--out", str(out), "--trees", str(trees)]) == 0
    script = tmp_path / "link.cjs"
    script.write_text(LINK, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(out)], capture_output=True, text=True, timeout=240)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert "make your identity" in got["before"]
    assert got["qr"] == 1
    assert all(s.startswith("Took the newest gaps of pyr:") for s in got["linked"])
    seen = lambda rows: sorted((r[1], r[2], "this device" if r[3] == "this device" else "other") for r in rows)  # noqa: E731
    assert seen(got["laptop"]) == [("inputs.n = 2", "1", "this device"), ("inputs.n = 3", "2", "other")]
    assert ("inputs.n = 9", "1", "other") in seen(got["live"])
    assert seen(got["reloaded"]) == seen(got["live"])
    assert got["stranger"][0].startswith("Not linked:") and "another identity" in got["stranger"][0] and got["stranger"][1] == [["No gaps: every decision so far had a branch."]]
    assert seen(got["file"]) == [("inputs.n = 3", "2", "other"), ("inputs.n = 9", "1", "other")]
    assert got["errors"] == []


PAIR_LINK = r"""
const { chromium } = require("playwright");
const [file] = process.argv.slice(2);
const PASS = "a long sentence nobody else would guess";
(async () => {
  const browser = await chromium.launch();
  const out = { errors: [] };
  const open = async () => {
    const page = await (await browser.newContext()).newPage();
    page.on("pageerror", (e) => out.errors.push(String(e)));
    await page.route(/^https?:\/\//, (r) => r.abort());
    await page.goto("file://" + file);
    await page.waitForFunction(() => document.querySelectorAll("#caps tbody tr").length > 0);
    return page;
  };
  const ask = async (page, q) => {
    await page.click('#tabs a[data-tab="ask"]');
    const n = (await page.$$("#out .card")).length;
    await page.fill("#text", q); await page.click("#go");
    await page.waitForFunction((n) => document.querySelectorAll("#out .card").length > n, n);
  };
  const laptop = await open(), phone = await open();
  await laptop.click("#identity-tab"); await laptop.click("#ident-new");
  await laptop.fill("#ident-pass", PASS); await laptop.fill("#ident-pass2", PASS); await laptop.click("#ident-new-form button");
  await laptop.waitForSelector("#ident-root:not([hidden])", { timeout: 60000 });
  await ask(laptop, "pick 2"); await ask(phone, "pick 7");

  // The laptop offers; the phone, with no key yet, answers; the answer goes back.
  await laptop.click("#identity-tab"); await laptop.click("#pair-offer"); await laptop.waitForSelector("#pair-step textarea[data-code]");
  out.qr = await laptop.$$eval("#pair-step .qr svg", (s) => s.length);
  const offer = await laptop.inputValue("#pair-step textarea[data-code]");
  await phone.click("#identity-tab"); await phone.click("#pair-have");
  await phone.fill("#pair-offer-code", offer); await phone.click("#pair-answer-form button");
  await phone.waitForSelector("#pair-step textarea[data-code]");
  out.phone_key = (await phone.textContent("#ident-id")).trim();
  await laptop.fill("#pair-answer", await phone.inputValue("#pair-step textarea[data-code]")); await laptop.click("#pair-accept button[type=submit]");

  // Both screens show the same digits; the person says so on the laptop.
  await laptop.waitForSelector("#pair-confirm", { timeout: 30000 });
  await phone.waitForSelector("#pair-step .pair-words");
  out.words = [await laptop.textContent("#pair-confirm .pair-words"), await phone.textContent("#pair-step .pair-words")];
  out.asked = await laptop.textContent("#pair-confirm p");
  await laptop.fill("#pair-label", "my phone");
  await laptop.click("#pair-confirm button[type=submit]");
  await phone.waitForFunction(() => document.querySelector("#pair-state").textContent.includes("Paired"), null, { timeout: 15000 });
  out.phone_state = await phone.textContent("#pair-state");
  out.phone_paired = await phone.textContent("#ident-paired");
  out.devices = await laptop.$$eval("#dev-table tbody tr", (t) => t.map((x) => x.innerText));

  // The same link goes on to share gaps.
  await laptop.click('#tabs a[data-tab="gaps"]');
  await laptop.waitForFunction(() => document.querySelector("#gaps").innerText.includes("= 7"), null, { timeout: 15000 });
  out.gaps = await laptop.$$eval("#gaps tbody tr", (t) => t.map((x) => x.innerText.split("\t")));
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
def test_a_new_browser_pairs_by_code_and_goes_on_to_share_gaps(tmp_path):
    trees = tmp_path / "trees"
    trees.mkdir()
    (trees / "gappy.toml").write_text(GAPPY, encoding="utf-8")
    out = tmp_path / "console" / "index.html"
    assert main(["console", "--out", str(out), "--trees", str(trees)]) == 0
    script = tmp_path / "pair.cjs"
    script.write_text(PAIR_LINK, encoding="utf-8")
    run = subprocess.run(["node", str(script), str(out)], capture_output=True, text=True, timeout=240)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["qr"] == 1
    words = got["words"]
    assert words[0] == words[1] and len(words[0]) == 7
    key = got["phone_key"]
    assert f"{key[:8]}…{key[-4:]}" in got["asked"]  # the device asking is the phone's new key
    assert got["phone_state"].startswith("Paired: this browser now speaks for pyr:") and "my phone" in got["phone_state"]
    assert "speaks for" in got["phone_paired"]
    assert len(got["devices"]) == 1 and got["devices"][0].startswith("my phone") and "ask, chat" in got["devices"][0]
    assert sorted((r[1], r[3]) for r in got["gaps"]) == [("inputs.n = 2", "this device"), ("inputs.n = 7", f"{key[:8]}…{key[-4:]}")]
    assert got["errors"] == []
