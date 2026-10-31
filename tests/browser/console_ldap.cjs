// The console's sign-in, driven in Chromium against a service started with LDAP users.
//   node tests/browser/console_ldap.cjs URL USER PASSWORD
// Prints `FIELDS <shown>`, then signs in and prints `WHERE <the header's line>`,
// `KEPT <what local storage holds>` and `ASKED <say>`. Needs node's playwright and a Chromium.
const { chromium } = require("playwright");
const [url, user, password] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  try {
    const page = await browser.newPage();
    page.on("pageerror", (e) => console.log("PAGEERROR " + e.message));
    await page.goto(url + "/");
    await page.waitForSelector("#hub-user:not([hidden])");
    console.log("FIELDS " + await page.textContent("#connect-lede"));
    await page.fill("#hub-user", user);
    await page.fill("#hub-password", password);
    await page.click("#connect-go");
    await page.waitForFunction(() => document.querySelector("#where").textContent.includes("signed in as"), null, { timeout: 15000 }).catch(() => {});
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
