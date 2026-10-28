/* sync.js: a person's devices share their gaps with each other, with no hub.
 *
 * Every device of an identity (its root, and each key the root signed a
 * device note for) keeps what its page met: the gaps and the unanswered
 * sentences (pyrtan.js's LocalHub). Here it hands them to the others,
 * browser to browser:
 *
 * - A **bundle** is one device's own gaps and sentences, signed by that
 *   device's key, with its note (signed by the root) beside it; the root's
 *   own bundle carries its identity document, so every device learns which
 *   devices are revoked. A device takes a bundle only for its own root,
 *   from a device that is not revoked, and keeps the newest per device.
 *   Bundles pass on unchanged, so what one device met reaches a third
 *   through a second, and a device's gaps are never added into another's.
 * - A **link** is a WebRTC data channel between two pages, with no server:
 *   one page makes an offer, the person carries it to the other (a code to
 *   paste, a QR to scan, or the console's address with #sync= in it), and
 *   carries the answer back. No STUN or TURN server is asked: the console
 *   reaches nothing but the hosts its trees read, so two devices link when
 *   they can reach each other directly, on the same network.
 * - Over a link, each side first says **hello**: a document signed like a
 *   bundle, naming the link (both ends' DTLS fingerprints, from the offer
 *   and the answer). Nothing else is sent until the other's hello is for
 *   this link, from this root, by a device it may speak for; a code that
 *   reached a stranger opens a link that carries nothing.
 * - A **file** is the bundles a device holds, for devices that are never
 *   on at the same time or on the same network. It is checked as a link's
 *   bundles are.
 *
 * Runs in a browser as `PyrtanSync` (with PyrtanIdentity beside it) and in
 * node, given the identity module, for the parts that need no WebRTC.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory;
  else root.PyrtanSync = factory(root.PyrtanIdentity);
})(typeof globalThis !== "undefined" ? globalThis : this, function (I) {
  "use strict";

  const BUNDLE = "pyrtan-gaps", HELLO = "pyrtan-sync-hello", FILE = "pyrtan-gaps-file", CODE = "pyrgaps1";
  const GAPS = 500, SENTENCES = 200, PART = 15000;
  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

  // ------------------------------------------------------------ who may speak for the root

  /** What is wrong with a document signed by a device of `root`, or null. revoked: the devices root's document revokes. */
  async function speaksFor(doc, rootId, revoked, now) {
    if (!isObj(doc) || doc.root !== rootId) return "it is another identity's";
    if (typeof doc.device !== "string" || !I.valid(doc.device)) return "it names no device";
    if ((revoked || []).includes(doc.device)) return `${I.short(doc.device)} is revoked`;
    if (doc.device !== rootId) {
      const note = doc.note, bad = I.noteProblem(note, doc.device, now);
      if (bad) return bad;
      if (note.root !== rootId) return "its device note is for another identity";
      if (!(await I.verifyDocument(note, rootId))) return "its device note is not signed by your identity";
    } else if (doc.note) return "a root needs no note";
    if (!(await I.verifyDocument(doc, doc.device))) return "its signature does not hold";
    return null;
  }

  // A key's root: itself, or the root its note names.
  const rootOf = (key) => !key ? null : key.role === "root" ? key.id : key.note && key.note.root || null;
  function signed(key, body) {
    const doc = Object.assign({}, body, { root: rootOf(key), device: key.id });
    if (key.role !== "root") doc.note = key.note;
    return I.signDocument(key, doc);
  }

  // ------------------------------------------------------------ bundles

  // JSON-plain, so it signs and checks the same on both sides.
  const plain = (v) => JSON.parse(JSON.stringify(v === undefined ? null : v));
  /** This device's bundle: own = LocalHub.own(). A root's carries its identity document. */
  function makeBundle(key, own, issued) {
    const body = { [BUNDLE]: 1, issued: issued !== undefined ? issued : Date.now(), gaps: plain((own.gaps || []).slice(0, GAPS)), unanswered: plain((own.unanswered || []).slice(0, SENTENCES)) };
    if (key.role === "root" && key.document) body.document = key.document;
    return signed(key, body);
  }
  async function checkBundle(b, rootId, revoked, now) {
    if (!isObj(b) || b[BUNDLE] !== 1) return "not a bundle of gaps";
    if (!Number.isSafeInteger(b.issued)) return "it says no time";
    if (!Array.isArray(b.gaps) || !Array.isArray(b.unanswered) || b.gaps.length > GAPS || b.unanswered.length > SENTENCES) return "its gaps are not a list, or too long";
    if (b.document !== undefined) {
      if (b.device !== rootId) return "only the root carries its identity document";
      if (!isObj(b.document) || b.document.id !== rootId || !(await I.verifyDocument(b.document, rootId))) return "its identity document is not your identity's";
    }
    return speaksFor(b, rootId, revoked, now);
  }

  // ------------------------------------------------------------ hello, bound to one link

  /** The link two descriptions make: both DTLS fingerprints, in order. Null when either has none. */
  function linkOf(sdpA, sdpB) {
    const fp = (sdp) => { const m = /^a=fingerprint:(\S+ \S+)\s*$/im.exec(String(sdp || "")); return m ? m[1].toLowerCase() : null; };
    const a = fp(sdpA), b = fp(sdpB);
    return a && b ? [a, b].sort().join(",") : null;
  }
  const makeHello = (key, link) => signed(key, { [HELLO]: 1, link, issued: Date.now() });
  async function checkHello(h, rootId, link, revoked, now) {
    if (!isObj(h) || h[HELLO] !== 1) return "not a hello";
    if (!link || h.link !== link) return "it is for another link";
    return speaksFor(h, rootId, revoked, now);
  }

  // ------------------------------------------------------------ a session: what goes over a link

  /**
   * opts: key (this device's), revoked() (the root's revoked devices),
   * own() (LocalHub.own()), held() (the other bundles held), take(bundle)
   * (keep a checked bundle; true when it was newer), send(message), and
   * note(event, detail) to hear what happened: "linked" (the other device),
   * "took" ({took, from, refused}), "refused" (why the other is not let in).
   */
  class Session {
    constructor(opts) { Object.assign(this, opts); this.root = rootOf(this.key); this.peer = null; this.link = null; }
    async start(link) { this.link = link; this.send(await makeHello(this.key, link)); }
    async push(all) {
      if (!this.peer) return;
      const mine = await makeBundle(this.key, this.own());
      this.send({ t: "bundles", items: all ? [mine].concat(this.held().filter((b) => b.device !== this.peer)) : [mine] });
    }
    async receive(m) {
      if (isObj(m) && m[HELLO] === 1) {
        const bad = await checkHello(m, this.root, this.link, this.revoked());
        if (bad) { this.note("refused", bad); this.send({ t: "bye", why: bad }); return; }
        if (this.peer) return;
        this.peer = m.device; this.note("linked", m.device);
        await this.push(true);
        return;
      }
      if (isObj(m) && m.t === "bye") { this.note("refused", `the other device would not link: ${m.why}`); return; }
      if (!this.peer || !isObj(m) || m.t !== "bundles" || !Array.isArray(m.items)) return;
      this.note("took", await takeAll(m.items.slice(0, 50), this.root, this.revoked, this.take, this.key.id));
    }
  }
  async function takeAll(items, rootId, revoked, take, self) {
    const out = { took: 0, from: [], refused: [] };
    for (const b of items) {
      if (isObj(b) && b.device === self) continue;
      const bad = await checkBundle(b, rootId, revoked());
      if (bad) { out.refused.push(bad); continue; }
      if (take(b)) { out.took += 1; out.from.push(b.device); }
    }
    return out;
  }

  // ------------------------------------------------------------ files

  async function makeFile(key, own, held) {
    return { [FILE]: 1, root: rootOf(key), made: Date.now(), bundles: [await makeBundle(key, own)].concat(held) };
  }
  async function readFile(f, key, revoked, take) {
    if (!isObj(f) || f[FILE] !== 1 || !Array.isArray(f.bundles)) throw new Error("that file is not gaps from a device");
    if (f.root !== rootOf(key)) throw new Error("that file is another identity's");
    return takeAll(f.bundles.slice(0, 50), rootOf(key), revoked, take, key.id);
  }

  // ------------------------------------------------------------ codes: an offer or an answer, to carry by hand

  const b64url = (bytes) => { let s = ""; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); };
  const unb64url = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - t.length % 4) % 4)), (c) => c.charCodeAt(0));
  async function pipe(bytes, stream) { return new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer()); }
  const zips = () => typeof CompressionStream === "function" && typeof DecompressionStream === "function";
  /** kind: "o" (an offer) or "a" (an answer); sdp: the description. */
  async function encodeCode(kind, sdp) {
    const raw = new TextEncoder().encode(JSON.stringify({ k: kind, s: sdp }));
    return zips() ? `${CODE}.z.${b64url(await pipe(raw, new CompressionStream("deflate-raw")))}` : `${CODE}.j.${b64url(raw)}`;
  }
  /** {kind, sdp} from a code, or from an address with #sync=CODE in it; throws when it is neither. */
  async function decodeCode(text) {
    let t = String(text || "").trim();
    const at = t.indexOf("#sync=");
    if (at >= 0) t = decodeURIComponent(t.slice(at + 6));
    const m = new RegExp(`^${CODE}\\.([zj])\\.([A-Za-z0-9_-]+)$`).exec(t.replace(/\s+/g, ""));
    if (!m) throw new Error("that is not a link code from a Pyrtan console");
    let raw = unb64url(m[2]);
    if (m[1] === "z") { if (!zips()) throw new Error("this browser cannot read a compressed code"); raw = await pipe(raw, new DecompressionStream("deflate-raw")); }
    const d = JSON.parse(new TextDecoder().decode(raw));
    if (!isObj(d) || !["o", "a"].includes(d.k) || typeof d.s !== "string") throw new Error("that link code is damaged");
    return { kind: d.k, sdp: d.s };
  }

  // ------------------------------------------------------------ a WebRTC link, with no server

  /** Messages in parts small enough for any data channel. */
  class Parts {
    constructor() { this.open = new Map(); this.n = 0; }
    split(obj) {
      const text = JSON.stringify(obj), id = ++this.n, count = Math.max(1, Math.ceil(text.length / PART)), out = [];
      for (let i = 0; i < count; i++) out.push(JSON.stringify({ p: id, i, n: count, s: text.slice(i * PART, (i + 1) * PART) }));
      return out;
    }
    join(text) {
      let m; try { m = JSON.parse(text); } catch (e) { return undefined; }
      if (!isObj(m) || !Number.isInteger(m.i) || !Number.isInteger(m.n) || m.n < 1 || m.n > 400 || typeof m.s !== "string") return undefined;
      const got = this.open.get(m.p) || [];
      got[m.i] = m.s; this.open.set(m.p, got);
      if (got.filter((x) => x !== undefined).length < m.n) return undefined;
      this.open.delete(m.p);
      try { return JSON.parse(got.join("")); } catch (e) { return undefined; }
    }
  }
  const gathered = (pc, ms) => new Promise((res) => {
    if (pc.iceGatheringState === "complete") return res();
    const done = () => { if (pc.iceGatheringState === "complete") { pc.removeEventListener("icegatheringstatechange", done); res(); } };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(res, ms || 4000);
  });
  /** One link: offer() then accept(answerCode) on one side, answer(offerCode) on the other. onopen(link), onmessage(obj), onclose(). */
  class Link {
    constructor(on) { this.on = on || {}; this.pc = null; this.channel = null; this.parts = new Parts(); }
    make() {
      this.close();
      this.pc = new RTCPeerConnection({ iceServers: [] });
      this.pc.addEventListener("connectionstatechange", () => { if (["failed", "closed"].includes(this.pc && this.pc.connectionState)) this.ended(); });
      return this.pc;
    }
    wire(ch) {
      this.channel = ch;
      ch.onopen = () => this.on.onopen && this.on.onopen(linkOf(this.pc.localDescription.sdp, this.pc.remoteDescription.sdp));
      ch.onmessage = (e) => { const m = this.parts.join(e.data); if (m !== undefined && this.on.onmessage) this.on.onmessage(m); };
      ch.onclose = () => this.ended();
    }
    async offer() {
      const pc = this.make();
      this.wire(pc.createDataChannel("pyrtan-gaps"));
      await pc.setLocalDescription(await pc.createOffer());
      await gathered(pc);
      return encodeCode("o", pc.localDescription.sdp);
    }
    async answer(code) {
      const d = await decodeCode(code);
      if (d.kind !== "o") throw new Error("that is an answer; this needs the code the first device showed");
      const pc = this.make();
      pc.ondatachannel = (e) => this.wire(e.channel);
      await pc.setRemoteDescription({ type: "offer", sdp: d.sdp });
      await pc.setLocalDescription(await pc.createAnswer());
      await gathered(pc);
      return encodeCode("a", pc.localDescription.sdp);
    }
    async accept(code) {
      const d = await decodeCode(code);
      if (d.kind !== "a") throw new Error("that is an offer; this needs the code the other device gave back");
      if (!this.pc || this.pc.signalingState !== "have-local-offer") throw new Error("make a link first");
      await this.pc.setRemoteDescription({ type: "answer", sdp: d.sdp });
    }
    send(obj) { if (this.channel && this.channel.readyState === "open") for (const p of this.parts.split(obj)) this.channel.send(p); }
    ended() { const had = !!this.pc; this.pc = null; this.channel = null; if (had && this.on.onclose) this.on.onclose(); }
    close() { const pc = this.pc; this.pc = null; this.channel = null; if (pc) pc.close(); }
  }

  // ------------------------------------------------------------ QR codes (ISO/IEC 18004), byte mode
  //
  // After Project Nayuki's QR Code generator (MIT): the same tables, Reed-
  // Solomon over GF(256) and mask penalty, cut to byte mode and levels M then L.

  const ECC = { M: [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
                L: [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30] };
  const BLOCKS = { M: [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
                   L: [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25] };
  const FORMAT = { L: 1, M: 0 };
  const bit = (x, i) => ((x >>> i) & 1) !== 0;
  function rawModules(ver) {
    let n = (16 * ver + 128) * ver + 64;
    if (ver >= 2) { const a = Math.floor(ver / 7) + 2; n -= (25 * a - 10) * a - 55; if (ver >= 7) n -= 36; }
    return n;
  }
  const dataCodewords = (ver, ecl) => Math.floor(rawModules(ver) / 8) - ECC[ecl][ver] * BLOCKS[ecl][ver];
  function gfMul(x, y) { let z = 0; for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; } return z; }
  function rsDivisor(degree) {
    const r = new Array(degree).fill(0); r[degree - 1] = 1; let root = 1;
    for (let i = 0; i < degree; i++) { for (let j = 0; j < r.length; j++) { r[j] = gfMul(r[j], root); if (j + 1 < r.length) r[j] ^= r[j + 1]; } root = gfMul(root, 0x02); }
    return r;
  }
  function rsRemainder(data, div) {
    const r = div.map(() => 0);
    for (const b of data) { const f = b ^ r.shift(); r.push(0); div.forEach((c, i) => { r[i] ^= gfMul(c, f); }); }
    return r;
  }

  /** The modules of a QR code for text (UTF-8, byte mode): rows of booleans, true dark. Throws when it is too long for version 40. */
  function qr(text) {
    const bytes = Array.from(new TextEncoder().encode(String(text)));
    let ver = 0, ecl = null;
    for (const level of ["M", "L"]) {
      for (let v = 1; v <= 40; v++) if (4 + (v <= 9 ? 8 : 16) + bytes.length * 8 <= dataCodewords(v, level) * 8) { ver = v; break; }
      if (ver) { ecl = level; break; }
    }
    if (!ver) throw new Error("too long for a QR code");
    // The data codewords.
    const bits = [];
    const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4); put(bytes.length, ver <= 9 ? 8 : 16); for (const b of bytes) put(b, 8);
    const capacity = dataCodewords(ver, ecl) * 8;
    put(0, Math.min(4, capacity - bits.length));
    put(0, (8 - bits.length % 8) % 8);
    for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) put(pad, 8);
    const data = []; for (let i = 0; i < bits.length; i += 8) data.push(parseInt(bits.slice(i, i + 8).join(""), 2));
    // Blocks, error correction, interleaved.
    const nBlocks = BLOCKS[ecl][ver], eccLen = ECC[ecl][ver], raw = Math.floor(rawModules(ver) / 8);
    const nShort = nBlocks - raw % nBlocks, shortLen = Math.floor(raw / nBlocks), div = rsDivisor(eccLen), blocks = [];
    for (let i = 0, k = 0; i < nBlocks; i++) {
      const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1)); k += dat.length;
      const ecc = rsRemainder(dat, div);
      if (i < nShort) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    const codewords = [];
    for (let i = 0; i < blocks[0].length; i++) blocks.forEach((b, j) => { if (i !== shortLen - eccLen || j >= nShort) codewords.push(b[i]); });
    // The grid: function patterns first.
    const size = ver * 4 + 17;
    const dark = Array.from({ length: size }, () => new Array(size).fill(false)), fixed = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (x, y, d) => { dark[y][x] = d; fixed[y][x] = true; };
    for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    const finder = (x, y) => { for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) { const d = Math.max(Math.abs(dx), Math.abs(dy)), xx = x + dx, yy = y + dy; if (xx >= 0 && xx < size && yy >= 0 && yy < size) set(xx, yy, d !== 2 && d !== 4); } };
    finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
    const align = [];
    if (ver > 1) {
      const n = Math.floor(ver / 7) + 2, step = Math.floor((ver * 8 + n * 3 + 5) / (n * 4 - 4)) * 2;
      for (let p = size - 7; align.length < n - 1; p -= step) align.unshift(p);
      align.unshift(6);
    }
    align.forEach((ax, i) => align.forEach((ay, j) => {
      if ((i === 0 && j === 0) || (i === 0 && j === align.length - 1) || (i === align.length - 1 && j === 0)) return;
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }));
    const format = (mask) => {
      const d = (FORMAT[ecl] << 3) | mask; let rem = d;
      for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const b = ((d << 10) | rem) ^ 0x5412;
      for (let i = 0; i <= 5; i++) set(8, i, bit(b, i));
      set(8, 7, bit(b, 6)); set(8, 8, bit(b, 7)); set(7, 8, bit(b, 8));
      for (let i = 9; i < 15; i++) set(14 - i, 8, bit(b, i));
      for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(b, i));
      for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(b, i));
      set(8, size - 8, true);
    };
    format(0);
    if (ver >= 7) {
      let rem = ver;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      const b = (ver << 12) | rem;
      for (let i = 0; i < 18; i++) { const a = size - 11 + i % 3, c = Math.floor(i / 3); set(a, c, bit(b, i)); set(c, a, bit(b, i)); }
    }
    // The codewords, in the zigzag.
    let i = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let v = 0; v < size; v++) for (let j = 0; j < 2; j++) {
        const x = right - j, y = ((right + 1) & 2) === 0 ? size - 1 - v : v;
        if (!fixed[y][x] && i < codewords.length * 8) { dark[y][x] = bit(codewords[i >>> 3], 7 - (i & 7)); i++; }
      }
    }
    // The mask with the least penalty.
    const MASKS = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0, (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => (x * y) % 2 + (x * y) % 3 === 0, (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0, (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0];
    const apply = (m) => { for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fixed[y][x] && MASKS[m](x, y)) dark[y][x] = !dark[y][x]; };
    const penalty = () => {
      let p = 0, black = 0;
      const lines = [];
      for (let y = 0; y < size; y++) { lines.push(dark[y].map((d) => (d ? 1 : 0)).join("")); lines.push(dark.map((r) => (r[y] ? 1 : 0)).join("")); }
      for (const l of lines) {
        for (const run of l.match(/0+|1+/g)) if (run.length >= 5) p += run.length - 2;
        for (const pat of ["10111010000", "00001011101"]) for (let at = l.indexOf(pat); at >= 0; at = l.indexOf(pat, at + 1)) p += 40;
      }
      for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) { const c = dark[y][x]; if (c === dark[y][x + 1] && c === dark[y + 1][x] && c === dark[y + 1][x + 1]) p += 3; }
      for (const r of dark) for (const d of r) if (d) black++;
      return p + (Math.ceil(Math.abs(black * 20 - size * size * 10) / (size * size)) - 1) * 10;
    };
    let best = 0, least = Infinity;
    for (let m = 0; m < 8; m++) { apply(m); format(m); const p = penalty(); if (p < least) { least = p; best = m; } apply(m); }
    apply(best); format(best);
    return dark;
  }
  /** The QR code as an SVG, dark on white whatever the page's colours, with its quiet zone. */
  function qrSvg(text) {
    const m = qr(text), n = m.length + 8;
    let d = "";
    m.forEach((row, y) => row.forEach((on, x) => { if (on) d += `M${x + 4} ${y + 4}h1v1h-1z`; }));
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n} ${n}" shape-rendering="crispEdges" role="img" aria-label="QR code"><rect width="${n}" height="${n}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
  }

  return { rootOf, speaksFor, makeBundle, checkBundle, makeHello, checkHello, linkOf, Session, makeFile, readFile, encodeCode, decodeCode, Parts, Link, qr, qrSvg };
});
