/* identity.js: a pyrtan identity in JavaScript, for the console.
 *
 * What the console needs of _meta/identity.md, and what Python's
 * src/pyrtan/identity.py does the same way: identifiers (`pyr:` and the
 * base32 of key, SHA3-256 checksum and version), canonical JSON, the
 * message a request signature is over, and signing it; the documents a
 * root signs (identity documents, device notes, name claims, releases,
 * handovers); and the encrypted backup of a root key (PBKDF2-SHA256, then
 * AES-256-GCM, all by crypto.subtle). tests/conformance/identity.json
 * holds the vectors both must agree on.
 *
 * SHA-256 and SHA3-256 are written out here: crypto.subtle has no SHA3,
 * and none of crypto.subtle on a plain http:// LAN address. Ed25519 is
 * never written out: a key is made, held and used only by crypto.subtle,
 * non-extractable, so a page without it (http:// on a LAN, an old
 * browser) holds no key and says so. The device key lives in IndexedDB.
 *
 * Runs in a browser as `PyrtanIdentity` and in node.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PyrtanIdentity = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PREFIX = "pyr:", ID_LENGTH = 56, VERSION_ED25519 = 1, REQUEST_TAG = "pyrtan-req-v1";
  const DOC_TAG = "pyrtan-doc-v1\n", HANDOVER_TAG = "pyrtan-handover-v1\n", BACKUP_TAG = "pyrtan-key-backup-v1\n";
  const ABILITIES = ["ask", "chat", "run", "operate"], YEAR = 365 * 24 * 3600, BACKUP_ITERATIONS = 600000, MIN_ITERATIONS = 100000;
  const LOCAL = /^[a-z0-9][a-z0-9._-]{0,62}$/, LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
  const enc = new TextEncoder();
  const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  const concat = (...parts) => { const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let o = 0; for (const p of parts) { out.set(p, o); o += p.length; } return out; };

  // ------------------------------------------------------------ SHA-256

  const sha256 = (() => {
    const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    const rotr = (x, n) => (x >>> n) | (x << (32 - n));
    return (bytes) => {
      const l = bytes.length, bitLen = l * 8, padded = new Uint8Array(((l + 9 + 63) >> 6) << 6);
      padded.set(bytes); padded[l] = 0x80;
      const dv = new DataView(padded.buffer); dv.setUint32(padded.length - 4, bitLen >>> 0); dv.setUint32(padded.length - 8, Math.floor(bitLen / 4294967296));
      let h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
      const w = new Uint32Array(64);
      for (let o = 0; o < padded.length; o += 64) {
        for (let i = 0; i < 16; i++) w[i] = dv.getUint32(o + i * 4);
        for (let i = 16; i < 64; i++) { const s0 = rotr(w[i-15],7) ^ rotr(w[i-15],18) ^ (w[i-15] >>> 3), s1 = rotr(w[i-2],17) ^ rotr(w[i-2],19) ^ (w[i-2] >>> 10); w[i] = (w[i-16] + s0 + w[i-7] + s1) >>> 0; }
        let [a,b,c,d,e,f,g,hh] = h;
        for (let i = 0; i < 64; i++) { const S1 = rotr(e,6) ^ rotr(e,11) ^ rotr(e,25), ch = (e & f) ^ (~e & g), t1 = (hh + S1 + ch + K[i] + w[i]) >>> 0, S0 = rotr(a,2) ^ rotr(a,13) ^ rotr(a,22), maj = (a & b) ^ (a & c) ^ (b & c), t2 = (S0 + maj) >>> 0; hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0; }
        h = h.map((v, i) => (v + [a,b,c,d,e,f,g,hh][i]) >>> 0);
      }
      return new Uint8Array(h.flatMap((v) => [v >>> 24 & 255, v >>> 16 & 255, v >>> 8 & 255, v & 255]));
    };
  })();

  // ------------------------------------------------------------ SHA3-256 (FIPS 202), for the identifier's checksum

  const sha3_256 = (() => {
    const MASK = (1n << 64n) - 1n;
    const RC = [0x1n, 0x8082n, 0x800000000000808an, 0x8000000080008000n, 0x808bn, 0x80000001n, 0x8000000080008081n, 0x8000000000008009n, 0x8an, 0x88n, 0x80008009n, 0x8000000an, 0x8000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n, 0x8000000000008002n, 0x8000000000000080n, 0x800an, 0x800000008000000an, 0x8000000080008081n, 0x8000000000008080n, 0x80000001n, 0x8000000080008008n];
    const R = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]];  // R[x][y]
    const rot = (v, n) => (n === 0 ? v : ((v << BigInt(n)) | (v >> BigInt(64 - n))) & MASK);
    function permute(A) {
      const C = new Array(5), B = new Array(25);
      for (let round = 0; round < 24; round++) {
        for (let x = 0; x < 5; x++) C[x] = A[x] ^ A[x + 5] ^ A[x + 10] ^ A[x + 15] ^ A[x + 20];
        for (let x = 0; x < 5; x++) { const D = C[(x + 4) % 5] ^ rot(C[(x + 1) % 5], 1); for (let y = 0; y < 5; y++) A[x + 5 * y] ^= D; }
        for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) B[y + 5 * ((2 * x + 3 * y) % 5)] = rot(A[x + 5 * y], R[x][y]);
        for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) A[x + 5 * y] = B[x + 5 * y] ^ (~B[(x + 1) % 5 + 5 * y] & MASK & B[(x + 2) % 5 + 5 * y]);
        A[0] ^= RC[round];
      }
    }
    const RATE = 136;
    return (bytes) => {
      const padded = new Uint8Array(Math.floor(bytes.length / RATE) * RATE + RATE);
      padded.set(bytes); padded[bytes.length] ^= 0x06; padded[padded.length - 1] ^= 0x80;
      const A = new Array(25).fill(0n);
      for (let o = 0; o < padded.length; o += RATE) {
        for (let i = 0; i < RATE / 8; i++) { let lane = 0n; for (let b = 7; b >= 0; b--) lane = (lane << 8n) | BigInt(padded[o + i * 8 + b]); A[i] ^= lane; }
        permute(A);
      }
      const out = new Uint8Array(32);
      for (let i = 0; i < 4; i++) { let lane = A[i]; for (let b = 0; b < 8; b++) { out[i * 8 + b] = Number(lane & 0xffn); lane >>= 8n; } }
      return out;
    };
  })();

  // ------------------------------------------------------------ base32 and base64url

  const B32 = "abcdefghijklmnopqrstuvwxyz234567";
  function b32(bytes) {
    let bits = 0, value = 0, out = "";
    for (const byte of bytes) { value = (value << 8) | byte; bits += 8; while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; } }
    if (bits > 0) out += B32[(value << (5 - bits)) & 31];
    return out;
  }
  function unb32(text) {
    let bits = 0, value = 0; const out = [];
    for (const ch of text) { const v = B32.indexOf(ch); if (v < 0) return null; value = (value << 5) | v; bits += 5; if (bits >= 8) { out.push((value >>> (bits - 8)) & 255); bits -= 8; } }
    return new Uint8Array(out);
  }
  const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64url = (text) => Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - text.length % 4) % 4)), (c) => c.charCodeAt(0));

  // ------------------------------------------------------------ identifiers

  const checksum = (key, version) => sha3_256(concat(enc.encode(".pyrtan id"), key, new Uint8Array([version]))).slice(0, 2);
  function encode(key, version = VERSION_ED25519) {
    if (version !== VERSION_ED25519 || key.length !== 32) throw new Error(`a version ${version} key is 32 bytes, not ${key.length}`);
    return PREFIX + b32(concat(key, checksum(key, version), new Uint8Array([version])));
  }
  // Ed25519 points of small order, sign bit cleared (libsodium's list), as identity.py has them.
  const SMALL_ORDER = new Set([
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
  ]);
  function decode(text) {
    if (typeof text !== "string" || !text.toLowerCase().startsWith(PREFIX)) throw new Error(`${JSON.stringify(text)} is not an identity: it starts ${PREFIX}`);
    const body = text.slice(PREFIX.length).toLowerCase();
    if (body.length !== ID_LENGTH) throw new Error(`not an identity: ${body.length} characters after ${PREFIX}, not ${ID_LENGTH}`);
    const raw = unb32(body);
    if (!raw || raw.length !== 35) throw new Error("not an identity: not base32");
    const key = raw.slice(0, 32), version = raw[34];
    if (version !== VERSION_ED25519) throw new Error(`version ${version}, which this console does not know`);
    const sum = checksum(key, version);
    if (sum[0] !== raw[32] || sum[1] !== raw[33]) throw new Error("not an identity: the checksum does not match (a typo?)");
    if (SMALL_ORDER.has(hex(key.slice(0, 31)) + (key[31] & 0x7f).toString(16).padStart(2, "0"))) throw new Error("not an identity: a point of small order, which no key is");
    return key;
  }
  const valid = (text) => { try { decode(text); return true; } catch (e) { return false; } };
  const short = (id) => `${PREFIX}${id.slice(PREFIX.length, PREFIX.length + 4)}…${id.slice(-4)}`;

  // ------------------------------------------------------------ canonical JSON and the request message

  function canonical(v) {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
  }
  function requestMessage(audience, method, path, date, body) {
    const digest = hex(sha256(body === null || body === undefined ? new Uint8Array() : enc.encode(canonical(body))));
    return enc.encode([REQUEST_TAG, audience.replace(/\/+$/, ""), method.toUpperCase(), path, String(date), digest].join("\n"));
  }

  // ------------------------------------------------------------ keys (crypto.subtle only)

  const subtle = () => (typeof crypto !== "undefined" && crypto.subtle) || null;
  async function available() {
    if (!subtle()) return false;
    try { await subtle().generateKey({ name: "Ed25519" }, false, ["sign", "verify"]); return true; } catch (e) { return false; }
  }
  async function idOf(publicKey) { return encode(new Uint8Array(await subtle().exportKey("raw", publicKey))); }
  async function generate() {
    const pair = await subtle().generateKey({ name: "Ed25519" }, false, ["sign", "verify"]);
    return { id: await idOf(pair.publicKey), privateKey: pair.privateKey, publicKey: pair.publicKey };
  }
  async function fromSeedAndPublic(seed, publicKey) {  // for the conformance vectors; a page never holds a seed
    const jwk = { kty: "OKP", crv: "Ed25519", d: b64url(seed), x: b64url(publicKey) };
    const privateKey = await subtle().importKey("jwk", jwk, { name: "Ed25519" }, false, ["sign"]);
    return { id: encode(publicKey), privateKey };
  }
  async function signRequest(key, audience, method, path, body, note, date) {
    const when = String(date !== undefined ? date : Math.floor(Date.now() / 1000));
    const sig = new Uint8Array(await subtle().sign({ name: "Ed25519" }, key.privateKey, requestMessage(audience, method, path, when, body)));
    const headers = { "X-Pyrtan-Key": key.id, "X-Pyrtan-Date": when, "X-Pyrtan-Audience": audience.replace(/\/+$/, ""), "X-Pyrtan-Key-Signature": b64url(sig) };
    if (note) {
      if (note.device !== key.id) throw new Error(`the note is for ${note.device}, not this key`);
      headers["X-Pyrtan-Device"] = b64url(enc.encode(canonical(note)));
    }
    return headers;
  }
  /** What is wrong with a pasted device note for this key, or null. The hub checks the signature. */
  function noteProblem(note, deviceId, now) {
    if (!note || typeof note !== "object" || note["pyrtan-device"] !== 1) return "not a device note";
    if (note.device !== deviceId) return `the note is for ${note.device}, not this browser's key`;
    if (!valid(note.root)) return "the note names no root";
    if (String(note.root).toLowerCase() === String(note.device).toLowerCase()) return "a note's device is not its root";
    if (typeof note.sig !== "string") return "the note is not signed";
    if (!(note.expires > (now !== undefined ? now : Date.now() / 1000))) return "the note has expired";
    return null;
  }

  // ------------------------------------------------------------ documents a root signs

  const now = () => Math.floor(Date.now() / 1000);
  const without = (doc, ...keys) => Object.fromEntries(Object.entries(doc).filter(([k]) => !keys.includes(k)));
  async function sign(key, bytes) { return new Uint8Array(await subtle().sign({ name: "Ed25519" }, key.privateKey, bytes)); }
  async function signDocument(key, doc) {
    const body = without(doc, "sig");
    return Object.assign({}, body, { sig: b64url(await sign(key, enc.encode(DOC_TAG + canonical(body)))) });
  }
  const abilities = (may) => { const bad = may.filter((a) => !ABILITIES.includes(a)); if (bad.length || new Set(may).size !== may.length) throw new Error(`may is some of ${ABILITIES.join(", ")}, each once`); return ABILITIES.filter((a) => may.includes(a)); };
  function host(text) {
    const h = String(text || "").toLowerCase(), labels = h.split(".");
    if (h.length > 253 || !labels.every((l) => LABEL.test(l))) throw new Error(`${JSON.stringify(text)} is not a host name`);
    if (labels[labels.length - 1] === "onion" && (labels.length < 2 || labels[labels.length - 2].length !== 56)) throw new Error("an onion address is 56 characters before .onion");
    return h;
  }
  function parseName(text) {
    const t = String(text || "").trim().toLowerCase(), at = t.lastIndexOf("@");
    if (at < 1 || t.startsWith(PREFIX)) throw new Error(`${JSON.stringify(text)} is not a name (name@domain)`);
    const name = t.slice(0, at);
    if (!LOCAL.test(name)) throw new Error(`${JSON.stringify(name)} is not lowercase letters, digits, dot, dash, underscore, at most 63`);
    return { name, domain: host(t.slice(at + 1)) };
  }
  async function makeDevice(root, device, may, opts) {
    const o = opts || {}, issued = o.issued !== undefined ? o.issued : now();
    decode(device);
    if (device.toLowerCase() === root.id) throw new Error("a root does not delegate to itself; it signs for itself");
    return signDocument(root, { "pyrtan-device": 1, root: root.id, device: device.toLowerCase(), label: o.label || "", may: abilities(may), issued, expires: o.expires !== undefined ? o.expires : issued + YEAR });
  }
  async function makeIdentity(root, opts) {
    const o = opts || {};
    return signDocument(root, { "pyrtan-identity": 1, id: root.id, kind: o.kind || "person", routes: o.routes || [], devices: o.devices || [], revoked: o.revoked || [], updated: o.updated !== undefined ? o.updated : now() });
  }
  // A new document, strictly newer than the one it replaces, or a holder would keep the old one.
  const reissue = (doc, root, changes) => makeIdentity(root, Object.assign({ kind: doc.kind, routes: doc.routes, devices: doc.devices, revoked: doc.revoked }, changes, { updated: Math.max(now(), doc.updated + 1) }));
  const withDevice = (doc, root, note) => reissue(doc, root, { devices: doc.devices.filter((n) => n.device !== note.device).concat([note]), revoked: doc.revoked.filter((r) => r !== note.device) });
  const withRevoked = (doc, root, device) => reissue(doc, root, { devices: doc.devices.filter((n) => n.device !== device), revoked: doc.revoked.includes(device) ? doc.revoked : doc.revoked.concat([device]) });
  async function makeName(key, name, opts) {
    const o = opts || {}, n = parseName(name);
    const claim = Object.assign({ "pyrtan-name": 1, name: n.name, domain: n.domain, id: key.id, issued: o.issued !== undefined ? o.issued : now() }, o.extra || {});
    if (o.handoverFrom) claim.handover = { id: o.handoverFrom.id, sig: b64url(await sign(o.handoverFrom, enc.encode(HANDOVER_TAG + canonical(claim)))) };
    return signDocument(key, claim);
  }
  async function makeRelease(key, name, opts) {
    const o = opts || {}, n = parseName(name);
    return signDocument(key, { "pyrtan-name-release": 1, name: n.name, domain: n.domain, id: key.id, issued: o.issued !== undefined ? o.issued : now() });
  }

  // ------------------------------------------------------------ roots and their backups
  //
  // A root made in a page is extractable exactly once: its seed is read out to
  // be encrypted into the backup, then the key is imported again,
  // non-extractable, and the seed is wiped. Restoring decrypts the seed and
  // imports it non-extractable the same way.

  async function createRoot() {
    const pair = await subtle().generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    const jwk = await subtle().exportKey("jwk", pair.privateKey);
    const seed = unb64url(jwk.d), publicKey = unb64url(jwk.x);
    return { id: encode(publicKey), seed, publicKey };
  }
  async function backupKey(passphrase, salt, iterations) {
    const base = await subtle().importKey("raw", enc.encode(passphrase), "PBKDF2", false, ["deriveKey"]);
    return subtle().deriveKey({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  }
  async function makeBackup(seed, id, passphrase, opts) {
    const o = opts || {};
    if (String(passphrase).length < 8) throw new Error("a passphrase is at least 8 characters; a long sentence is better");
    const salt = o.salt || crypto.getRandomValues(new Uint8Array(16)), iv = o.iv || crypto.getRandomValues(new Uint8Array(12)), iterations = o.iterations || BACKUP_ITERATIONS;
    const data = new Uint8Array(await subtle().encrypt({ name: "AES-GCM", iv, additionalData: enc.encode(BACKUP_TAG + id) }, await backupKey(passphrase, salt, iterations), seed));
    const backup = { "pyrtan-key-backup": 1, id, made: o.made !== undefined ? o.made : now(), kdf: { name: "PBKDF2-SHA256", iterations, salt: b64url(salt) }, cipher: { name: "AES-256-GCM", iv: b64url(iv) }, data: b64url(data) };
    if (o.document) backup.document = o.document;
    return backup;
  }
  function backupProblem(b) {
    if (!b || typeof b !== "object" || b["pyrtan-key-backup"] !== 1) return "not a key backup (pyrtan-key-backup: 1)";
    if (!valid(b.id)) return "the backup names no identity";
    if (!b.kdf || b.kdf.name !== "PBKDF2-SHA256" || !(b.kdf.iterations >= MIN_ITERATIONS && b.kdf.iterations <= 10000000) || typeof b.kdf.salt !== "string") return `the backup's kdf is not PBKDF2-SHA256 with at least ${MIN_ITERATIONS} iterations`;
    if (!b.cipher || b.cipher.name !== "AES-256-GCM" || typeof b.cipher.iv !== "string" || typeof b.data !== "string") return "the backup's cipher is not AES-256-GCM with an iv and data";
    if (b.document && b.document.id !== b.id.toLowerCase()) return "the document beside the key is another identity's";
    return null;
  }
  /** The seed in a backup; throws for a wrong passphrase or a damaged file. */
  async function openBackup(b, passphrase) {
    const bad = backupProblem(b);
    if (bad) throw new Error(bad);
    let seed;
    try {
      seed = new Uint8Array(await subtle().decrypt({ name: "AES-GCM", iv: unb64url(b.cipher.iv), additionalData: enc.encode(BACKUP_TAG + b.id.toLowerCase()) }, await backupKey(passphrase, unb64url(b.kdf.salt), b.kdf.iterations), unb64url(b.data)));
    } catch (e) { throw new Error("the passphrase does not open this backup (or the file is damaged)"); }
    return seed;
  }
  /** A non-extractable key from a seed, checked against the identity it should be. */
  async function rootFromSeed(seed, id) {
    const key = await fromSeedAndPublic(seed, decode(id));
    const probe = enc.encode("pyrtan-probe"), sig = await sign(key, probe);
    const pub = await subtle().importKey("raw", decode(id), { name: "Ed25519" }, false, ["verify"]);
    if (!(await subtle().verify({ name: "Ed25519" }, pub, sig, probe))) throw new Error("the backup's key is not the identity it names");
    seed.fill(0);
    return key;
  }

  // ------------------------------------------------------------ the device key, in IndexedDB

  const store = {
    db: null,
    async open() {
      if (this.db) return this.db;
      this.db = await new Promise((res, rej) => { const r = indexedDB.open("pyrtan-identity", 1); r.onupgradeneeded = () => r.result.createObjectStore("keys"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      return this.db;
    },
    async get() { const db = await this.open(); return new Promise((res, rej) => { const r = db.transaction("keys").objectStore("keys").get("device"); r.onsuccess = () => res(r.result || null); r.onerror = () => rej(r.error); }); },
    async put(v) { const db = await this.open(); return new Promise((res, rej) => { const t = db.transaction("keys", "readwrite"); if (v) t.objectStore("keys").put(v, "device"); else t.objectStore("keys").delete("device"); t.oncomplete = () => res(); t.onerror = () => rej(t.error); }); },
  };

  return {
    PREFIX, ABILITIES, sha256, sha3_256, hex, b64url, unb64url, encode, decode, valid, short, canonical, parseName, requestMessage,
    available, generate, fromSeedAndPublic, signRequest, noteProblem, store,
    signDocument, makeDevice, makeIdentity, withDevice, withRevoked, makeName, makeRelease,
    createRoot, makeBackup, backupProblem, openBackup, rootFromSeed,
  };
});
