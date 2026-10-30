// The console's Chat tab with an agent in the seat, driven in Chromium,
// playing "person or agent?" against a person who calls the hub directly.
//   node tests/browser/console_chat.cjs HUB_URL MODEL_URL
// HUB_URL is a hub started --chat; MODEL_URL is an OpenAI-compatible
// server (the test's, scripted). Prints one line per step: SEATED,
// OPENED <the agent's first line, as the person read it>, ANSWERED <its
// answer>, REVEAL <what the person was told>, TOLD <what the agent's
// console showed>, AGAIN <the agent waiting for the next stranger>, then
// DONE. Needs node's playwright and a Chromium.
const { chromium } = require("playwright");
const [hub_url, model_url] = process.argv.slice(2);

const call = async (path, body) => (await fetch(hub_url + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();

(async () => {
  const browser = await chromium.launch(process.env.PYRTAN_BROWSER ? { executablePath: process.env.PYRTAN_BROWSER } : {});
  try {
    const page = await browser.newPage();
    page.on("pageerror", (e) => console.log("PAGEERROR " + e.message));
    await page.goto(hub_url);
    await page.waitForFunction(() => document.querySelector("#caps tbody").children.length > 0);
    await page.click('#tabs a[data-tab="agents"]');
    await page.selectOption("#agent-engine", "openai");
    await page.fill("#agent-url", model_url);
    await page.fill("#agent-server-model", "scripted");
    await page.fill("#agent-name", "scout");
    await page.click("#agent-go");
    await page.waitForSelector("#agent-on:not([hidden])");

    // Seat the agent, in the game, with a persona.
    await page.click('#tabs a[data-tab="chat"]');
    await page.selectOption("#chat-as", "agent");
    await page.check("#chat-game");
    await page.fill("#chat-persona", "a night-shift nurse who loves chess");
    await page.click("#chat-start button[type=submit]");
    await page.waitForFunction(() => /Waiting for a stranger/.test(document.querySelector("#chat-log").textContent));
    console.log("SEATED " + await page.getAttribute("#chat-text", "placeholder"));

    // The person: a game seat of their own, straight at the hub.
    const me = await call("/chat/start", { game: true });
    let after = me.after;
    const heard = async (test) => {
      for (let i = 0; i < 60; i++) {
        const r = await call("/chat/poll", { seat: me.seat, after, wait: 1 });
        after = r.after;
        const found = r.events.find(test);
        if (found) return found;
      }
      throw new Error("never heard it");
    };
    console.log("OPENED " + (await heard((e) => e.kind === "stranger")).text);  // nobody spoke: the agent opens
    await call("/chat/say", { seat: me.seat, text: "hi there, you a bot?" });
    console.log("ANSWERED " + (await heard((e) => e.kind === "stranger")).text);
    await call("/chat/guess", { seat: me.seat, guess: "agent" });
    const moved = await call("/chat/next", { seat: me.seat, after });
    console.log("REVEAL " + moved.events.filter((e) => e.kind === "reveal").map((e) => e.text).join(" | "));
    await call("/chat/leave", { seat: me.seat });
    const told = await page.waitForFunction(() => { const r = document.querySelector("#chat-log .reveal"); return r && r.textContent; });
    console.log("TOLD " + await told.jsonValue());
    await page.waitForFunction(() => document.querySelectorAll("#chat-log .sys").length && /Waiting for a stranger/.test([...document.querySelectorAll("#chat-log .sys")].pop().textContent));
    console.log("AGAIN " + (await page.$$eval("#chat-log .line.you", (els) => els.map((e) => e.textContent))).join(" | "));
    console.log("DONE");
  } catch (e) {
    console.log("ERROR " + e.message);
  } finally {
    await browser.close();
  }
})();
