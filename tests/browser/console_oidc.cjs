// The console's OIDC sign-in, driven in Chromium against a service started with --oidc,
// whose provider is the small one in tests/test_oidc.py.
//   node tests/browser/console_oidc.cjs URL
// Prints `FIELDS <the connect lede>`, `BUTTON <the button's text>`, then clicks it, follows the
// provider there and back, and prints `URL <where it landed>`, `WHERE <the header's line>`,
// `KEPT <what local storage holds>`, `PROBLEM <any notice>` and `ASKED <say>`. Needs node's
// playwright and a Chromium.
const { chromium } = require("playwright");
const [url] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  try {
    const page = await browser.newPage();
    page.on("pageerror", (e) => console.log("PAGEERROR " + e.message));
    await page.goto(url + "/");
    await page.waitForSelector("#hub-signin:not([hidden])");
    console.log("FIELDS " + await page.textContent("#connect-lede"));
    console.log("BUTTON " + await page.textContent("#hub-signin"));
    await page.click("#hub-signin");
    await page.waitForFunction(() => document.querySelector("#where") && document.querySelector("#where").textContent.includes("signed in as"), null, { timeout: 15000 }).catch(() => {});
    console.log("URL " + page.url());
    console.log("WHERE " + await page.textContent("#where"));
    console.log("KEPT " + await page.evaluate(() => JSON.stringify(Object.assign({}, localStorage))));
    console.log("PROBLEM " + await page.textContent("#problem"));
    await page.click('#tabs a[data-tab="ask"]');
    await page.fill("#text", "weather in Oslo");
    await page.click("#go");
    console.log("ASKED " + await (await page.waitForSelector("#out .card .say, #out .card .status.error", { timeout: 15000 })).textContent());
  } finally {
    await browser.close();
  }
})().catch((e) => { console.log("ERROR " + e.message); process.exit(1); });
