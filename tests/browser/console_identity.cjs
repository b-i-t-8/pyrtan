// The console's Identity tab, driven in Chromium against a running hub.
//   node tests/browser/console_identity.cjs HUB_URL
// Prints `DEVICE <id>` once the page has made its key, then reads one line
// from stdin (the device note the test's root signs for that id), pastes it,
// asks a sentence and joins as a member, printing `ASKED <say>` and
// `JOINED <what the member tab says>`. Needs node's playwright and a Chromium.
const { chromium } = require("playwright");
const readline = require("readline");
const hub = process.argv[2];
const line = () => new Promise((res) => { const rl = readline.createInterface({ input: process.stdin }); rl.once("line", (l) => { rl.close(); res(l); }); });

(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  try {
    const page = await browser.newPage();
    page.on("pageerror", (e) => console.log("PAGEERROR " + e.message));
    await page.goto(hub + "/");
    await page.waitForSelector("#identity-tab:not([hidden])");
    await page.click("#identity-tab");
    await page.click("#ident-make");
    await page.waitForFunction(() => document.querySelector("#ident-id").textContent.startsWith("pyr:"));
    console.log("DEVICE " + await page.textContent("#ident-id"));
    const note = await line();
    await page.fill("#ident-note", note);
    await page.click("#ident-note-form button");
    await page.waitForFunction(() => document.querySelector("#ident-paired").textContent.includes("speaks for"));
    console.log("PAIRED " + await page.textContent("#ident-paired"));
    // Survives a reload: the key is in IndexedDB, non-extractable.
    await page.reload();
    await page.waitForSelector("#identity-tab:not([hidden])");
    await page.click("#identity-tab");
    await page.waitForFunction(() => document.querySelector("#ident-paired").textContent.includes("speaks for"));
    console.log("KEPT " + await page.textContent("#ident-id"));
    await page.click('#tabs a[data-tab="ask"]');
    await page.fill("#text", "echo from the browser");
    await page.click("#go");
    console.log("ASKED " + await (await page.waitForSelector("#out .card .say, #out .card .status.error")).textContent());
    await page.click("#member-tab");
    await page.fill("#member-name", "browser");
    await page.click("#join-form button");
    await page.waitForFunction(() => !document.querySelector("#member-on").hidden);
    console.log("JOINED " + await page.textContent("#member-id") + " " + await page.textContent("#member-trust"));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.log("ERROR " + e.message); process.exit(1); });
