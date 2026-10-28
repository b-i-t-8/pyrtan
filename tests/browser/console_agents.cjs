// The console's Agents tab, driven in Chromium.
//   node tests/browser/console_agents.cjs PAGE_URL MODEL_URL
// PAGE_URL is a hub's console, or a static console that is its own hub;
// MODEL_URL is an OpenAI-compatible server (the test's, scripted). Prints
// one line per step: STARTED, PROBED <what the agent said>, SUGGESTED
// <kinds>, ACCEPTED <the note>, ASKED <what the tree said>, LEVEL <the
// answer to "level 5" after accepting>, KEPT <a note, after a reload>,
// SKILL <the skills listed>, then DONE. Needs node's playwright and a Chromium.
const { chromium } = require("playwright");
const [page_url, model_url] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  try {
    const page = await browser.newPage();
    page.on("pageerror", (e) => console.log("PAGEERROR " + e.message));
    page.on("dialog", (d) => d.accept("too vague"));
    const start = async () => {
      await page.goto(page_url);
      await page.waitForFunction(() => document.querySelector("#caps tbody").children.length > 0);
      await page.click('#tabs a[data-tab="agents"]');
      await page.selectOption("#agent-engine", "openai");
      await page.fill("#agent-url", model_url);
      await page.fill("#agent-server-model", "scripted");
      await page.fill("#agent-name", "scout");
      await page.click("#agent-go");
      await page.waitForSelector("#agent-on:not([hidden])");
    };
    const say = async (text) => {
      const before = await page.$$eval("#agent-log .line.bot, #agent-log .line.tree", (els) => els.length);
      await page.fill("#agent-text", text);
      await page.click("#agent-say button");
      await page.waitForFunction((n) => document.querySelectorAll("#agent-log .line.bot, #agent-log .line.tree").length > n && document.querySelector("#agent-stop").disabled, before);
      return page.$$eval("#agent-log .line.bot, #agent-log .line.tree", (els) => els[els.length - 1].textContent);
    };
    await start();
    console.log("STARTED " + await page.textContent("#agent-what"));
    console.log("PROBED " + await say("/probe level 4"));
    console.log("SUGGESTED " + (await page.$$eval("#agent-sugs .status", (els) => els.map((e) => e.textContent))).join(","));
    // Accept every suggestion but one, which is dismissed (the dialog says why).
    const ids = await page.$$eval("[data-accept]", (els) => els.map((e) => e.dataset.accept));
    for (const id of ids.slice(0, -1)) {
      await page.click(`[data-accept="${id}"]`);
      await page.waitForFunction((i) => !document.querySelector(`[data-accept="${i}"]`), id);
    }
    await page.click(`[data-dismiss="${ids[ids.length - 1]}"]`);
    console.log("ACCEPTED " + (await page.$$eval("#agent-sugs .sug-state", (els) => els.map((e) => e.textContent))).join(" | "));
    console.log("ASKED " + await say("is the level low at 2?"));
    // In a browser that is its own hub, the accepted branch is in the tree now.
    await page.click('#tabs a[data-tab="ask"]');
    await page.fill("#text", "level 5");
    await page.click("#go");
    console.log("LEVEL " + await (await page.waitForSelector("#out .card .say")).textContent());
    await page.click('#tabs a[data-tab="agents"]');
    await page.click("#agent-memory summary");
    await page.click("#agent-skills-box summary");
    await page.fill("#agent-note", "the level is out of ten");
    await page.click("#agent-note-form button");
    await page.fill("#skill-name", "terse");
    await page.fill("#skill-desc", "answer in five words");
    await page.fill("#skill-body", "Answer in five words or fewer.");
    await page.click("#agent-skill-form button[type=submit]");
    // Memory and skills outlive the page.
    await start();
    await page.click("#agent-memory summary");
    await page.click("#agent-skills-box summary");
    console.log("KEPT " + (await page.$$eval("#agent-notes td", (els) => els.map((e) => e.textContent))).join(" | "));
    console.log("SKILL " + (await page.$$eval("#agent-skills code", (els) => els.map((e) => e.textContent))).join(","));
    console.log("TERSE " + await say("/terse what is the level"));
    // A browser without WebGPU (Firefox on Linux, say) is offered the CPU, and told which browsers have a GPU.
    const nogpu = await browser.newPage();
    await nogpu.addInitScript(() => Object.defineProperty(Navigator.prototype, "gpu", { get: () => undefined }));
    await nogpu.goto(page_url);
    await nogpu.evaluate(() => localStorage.removeItem("pyrtan.agents.config"));
    await nogpu.reload();
    await nogpu.click('#tabs a[data-tab="agents"]');
    const engineNow = await nogpu.inputValue("#agent-engine");
    const models = await nogpu.$$eval("#agent-model option", (els) => els.map((e) => e.value));
    await nogpu.selectOption("#agent-engine", "webllm");
    const notice = await nogpu.isVisible("#agent-gpu") ? await nogpu.textContent("#agent-gpu") : "(hidden)";
    console.log("NOGPU " + engineNow + " " + models.join(","));
    console.log("NOTICE " + notice);
    console.log("DONE");
  } catch (e) {
    console.log("ERROR " + (e && e.stack ? e.stack.split("\n").slice(0, 3).join(" ") : e));
  } finally {
    await browser.close();
  }
})();
