// The console's identity, end to end, in Chromium against a running hub, with no shell:
//   node tests/browser/console_gui.cjs HUB_URL DOWNLOAD_DIR
// Three browser contexts are three devices. A makes an identity and saves its
// backup, claims names, signs B's note and keeps the hub's grants; B is a device
// that asks; C restores A from the backup. Prints one line per step (WORD rest);
// after `ROOT <id>` it waits for a line on stdin, while the test grants that root.
const { chromium } = require("playwright");
const readline = require("readline");
const path = require("path");
const fs = require("fs");
const [hub, downloads] = process.argv.slice(2);
const rl = readline.createInterface({ input: process.stdin });
const lines = []; let waiting = null;
rl.on("line", (l) => { if (waiting) { const w = waiting; waiting = null; w(l); } else lines.push(l); });
const line = () => (lines.length ? Promise.resolve(lines.shift()) : new Promise((res) => { waiting = res; }));
const say = (word, rest) => console.log(`${word} ${rest}`);
const PASS = "a long sentence nobody else would guess";

async function open(browser) {
  const ctx = await browser.newContext({ acceptDownloads: true });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => say("PAGEERROR", e.message));
  // GitHub's public API, as the console asks it for a login's numeric id.
  await page.route("https://api.github.com/users/**", (r) => r.fulfill({ status: 200, contentType: "application/json", headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify({ login: "b-i-t-8", id: 212773752 }) }));
  await page.goto(hub + "/");
  await page.waitForSelector("#identity-tab:not([hidden])");
  await page.click("#identity-tab");
  return page;
}
const text = (page, sel) => page.textContent(sel).then((t) => t.trim());

(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  try {
    // A: make the identity, and save its backup.
    const a = await open(browser);
    await a.click("#ident-new");
    await a.fill("#ident-pass", PASS);
    await a.fill("#ident-pass2", PASS);
    await a.click("#ident-new-form button");
    await a.waitForSelector("#ident-root:not([hidden])", { timeout: 30000 });
    say("UNSAVED", await text(a, "#root-backup-state"));
    const [dl] = await Promise.all([a.waitForEvent("download"), a.click("#root-download")]);
    const backupPath = path.join(downloads, dl.suggestedFilename());
    await dl.saveAs(backupPath);
    await a.waitForFunction(() => document.querySelector("#root-backup-state").textContent.includes("Backed up"));
    say("BACKUP", backupPath);
    await a.fill("#root-gh", "b-i-t-8");
    await a.click("#root-gh-go");
    say("PRIVATEREPO", await a.getAttribute("#root-gh-file", "href"));
    say("ROOT", await text(a, "#root-id"));
    await line();  // the test grants this root

    // A: claim a name at a network, and at pyrtan.com.
    await a.check('input[name="claim-where"][value="network"]');
    await a.fill("#claim-name", "alice@names.example.org");
    await a.fill("#claim-repo", "https://github.com/example/names");
    await a.click("#claim-network button");
    say("CLAIMLINK", await (await a.waitForSelector("#claim-open")).getAttribute("href"));
    await a.check('input[name="claim-where"][value="pyrtan"]');
    await a.fill("#claim-login", "b-i-t-8");
    await a.click("#claim-pyrtan button");
    await a.waitForFunction(() => ((document.querySelector("#claim-open") || {}).href || "").includes("/b-i-t-8/pyrtan/new/"));
    say("PYRTANLINK", await a.getAttribute("#claim-open", "href"));

    // B: a device; A signs its note; B pastes it.
    const b = await open(browser);
    await b.click("#ident-make");
    await b.waitForSelector("#ident-on:not([hidden])");
    const device = await text(b, "#ident-id");
    say("DEVICE", device);
    await a.fill("#dev-code", device);
    await a.fill("#dev-label", "phone");
    await a.click("#dev-form button[type=submit]");
    const note = await text(a, "#dev-out pre");
    say("NOTE", JSON.stringify(JSON.parse(note)));
    await b.fill("#ident-note", note);
    await b.click("#ident-note-form button");
    await b.waitForFunction(() => document.querySelector("#ident-paired").textContent.includes("speaks for"));

    // A keeps the hub's grants in the Access tab.
    await a.click("#access-tab");
    await a.waitForSelector("#grant-table tbody tr");
    await a.fill("#grant-who", device);
    await a.uncheck('#grant-may input[value="ask"]');
    await a.check('#grant-may input[value="chat"]');
    await a.click("#grant-form button");
    await a.waitForFunction((d) => document.querySelector("#grant-table").textContent.includes(d.slice(0, 8)), device);
    say("GRANTS", String(await a.$$eval("#grant-table tbody tr", (r) => r.length)));

    // B asks, with no token: the hub grants its root ask.
    const ask = async (page, words) => {
      await page.click('#tabs a[data-tab="ask"]');
      await page.fill("#text", words);
      const before = await page.$$eval("#out .card", (c) => c.length);
      await page.click("#go");
      await page.waitForFunction((n) => document.querySelectorAll("#out .card").length > n, before);
      return text(page, "#out .card");
    };
    say("ASKED", await ask(b, "echo from the phone"));

    // A revokes B and gives the hub its newer document; B is refused.
    await a.click("#identity-tab");
    a.once("dialog", (d) => d.accept());
    await a.click(`#dev-table button[data-revoke="${device}"]`);
    await a.waitForFunction(() => document.querySelector("#dev-table").textContent.includes("revoked"));
    await a.click("#doc-push");
    await a.waitForFunction(() => document.querySelector("#problem").textContent.includes("newest document"));
    say("REVOKED", await ask(b, "echo once more"));

    // C: a new browser restores A from the backup file.
    const c = await open(browser);
    await c.click("#ident-restore-open");
    await c.setInputFiles("#ident-file", backupPath);
    await c.fill("#ident-restore-pass", PASS);
    await c.click("#ident-restore-form button");
    await c.waitForSelector("#ident-root:not([hidden])", { timeout: 30000 });
    say("RESTORED", `${await text(c, "#root-id")} ${await c.$$eval("#dev-table tbody tr", (r) => r.length)}`);
    say("DONE", "ok");
  } finally {
    await browser.close();
    rl.close();
  }
})().catch((e) => { say("ERROR", e.message.split("\n")[0]); process.exit(1); });
