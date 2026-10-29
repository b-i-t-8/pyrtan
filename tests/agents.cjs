// agents.js against the hub in a page, with a scripted model: what an agent
// does is checked by rules, whatever the model says.
//   node tests/agents.cjs        (exit 0 and "agents ok" when every check holds)
const assert = require("assert");
const path = require("path");
const root = path.resolve(__dirname, "..");
const Pyrtan = require(path.join(root, "src/pyrtan/js/pyrtan.js"));
const A = require(path.join(root, "src/pyrtan/js/agents.js"));
const vectors = require(path.join(root, "tests/conformance/vectors.json"));
const fixture = require(path.join(root, "tests/fixtures/examples.json"));
const clock = () => new Date(vectors.clock);

function swarm() {
  const transport = new Pyrtan.RecordedTransport(fixture);
  const hub = new Pyrtan.LocalHub({ trees: Object.values(vectors.trees), transport, adapters: transport, clock });
  return { hub, call: async (m, p, b) => (await hub.route(m, "/" + p, b))[1] };
}
// The model: a reply per job, found by what the task asks.
const model = (replies) => new A.ScriptedEngine((messages) => {
  const task = messages[messages.length - 1].content;
  for (const [re, reply] of replies) if (re.test(task)) return typeof reply === "function" ? reply(task, messages) : reply;
  return "I have nothing to say.";
});

(async () => {
  // parseJSON copes with a model's habits.
  assert.deepStrictEqual(A.parseJSON('Sure! ```json\n{"a": [1, "}"]}\n``` hope that helps'), { a: [1, "}"] });
  assert.deepStrictEqual(A.parseJSON("<think>{\"no\": 1}</think> {\"yes\": 2}"), { yes: 2 });
  assert.strictEqual(A.parseJSON("no json here"), null);

  // The CPU engine: wllama (here a stand-in module), the model's GGUF from Hugging Face, no GPU layers.
  {
    assert.strictEqual(A.WllamaEngine.available(), true);
    assert.ok(A.CPU_MODELS.some((m) => m.id === A.DEFAULT_CPU_MODEL));
    const fake = `export class Wllama {
      constructor(paths, config) { globalThis.wllamaSeen = { paths, config, calls: [] }; }
      async loadModelFromHF(hf, params) { globalThis.wllamaSeen.hf = hf; globalThis.wllamaSeen.params = params; params.progressCallback({ loaded: 1, total: 2 }); }
      async createChatCompletion(req) { globalThis.wllamaSeen.calls.push(req); return { choices: [{ message: { content: "ok" } }] }; }
      async exit() { globalThis.wllamaSeen.exited = true; }
    }`;
    const said = [];
    const engine = new A.WllamaEngine({ url: "data:text/javascript," + encodeURIComponent(fake), onProgress: (text, p) => said.push([text, p]) });
    assert.strictEqual(await engine.complete([{ role: "user", content: "hi" }], { json: true, maxTokens: 9 }), "ok");
    const seen = globalThis.wllamaSeen;
    assert.deepStrictEqual(seen.paths, { default: A.WLLAMA_WASM });
    assert.deepStrictEqual(seen.hf, { repo: A.CPU_MODELS[0].repo, file: A.CPU_MODELS[0].file });
    assert.strictEqual(seen.params.n_gpu_layers, 0);
    assert.deepStrictEqual(seen.calls[0].response_format, { type: "json_object" });
    assert.strictEqual(seen.calls[0].max_tokens, 9);
    assert.strictEqual(said[0][1], 0.5);
    await engine.unload();
    assert.ok(seen.exited);
    await assert.rejects(new A.WllamaEngine({ model: "no-such" }).load(), /no such model/);
  }

  // Probing: sentences read here, the ones that reach the tree run, misses become a checked pattern.
  {
    const { hub, call } = swarm();
    const storage = A.memoryStorage();
    const engine = model([
      [/new sentences/, { sentences: ["weather in Oslo", "is it raining in Oslo", "top 3 stories on hacker news", "weather in Oslo", 7] }],
      [/no trigger reads them/, { patterns: [{ pattern: "is it raining in {place}" }, { pattern: "is it raining in {city}" }, { pattern: "never looked at: at most two" }], why: "rain is weather" }],
    ]);
    const agent = new A.Agent({ name: "Scout One", engine, hub: call, clock, storage, limits: { pause: 0 } });
    assert.strictEqual(agent.name, "scout-one");
    const p = await agent.probe("weather", 5);
    assert.deepStrictEqual(p.results.map((r) => [r.text, r.read]), [["weather in Oslo", "weather"], ["is it raining in Oslo", null], ["top 3 stories on hacker news", "hacker-news"]]);
    assert.strictEqual(p.results[0].status, "done");
    assert.ok(p.results[0].path.includes("sky"));
    assert.strictEqual(p.results[2].status, undefined, "a sentence that reads to another tree is not run");
    assert.strictEqual(hub.runs, 1);
    const offered = p.suggestions.filter((x) => x.id);
    assert.deepStrictEqual(offered.map((x) => x.suggestion.pattern), ["is it raining in {place}"]);
    assert.deepStrictEqual(offered[0].suggestion.covers, ["is it raining in Oslo"]);
    assert.deepStrictEqual(offered[0].preview.reads["is it raining in Oslo"], { place: "Oslo" });
    assert.ok(p.suggestions.some((x) => x.refused && /city, which weather does not take/.test(x.why)));
    // What it learned is in its memory, and survives a new agent on the same storage.
    const again = new A.Agent({ name: "scout-one", engine, hub: call, clock, storage });
    assert.ok(again.memory.notes.some((n) => /reads as hacker-news/.test(n.text)));
    assert.ok(again.memory.notes.some((n) => n.source === "rule" && /refused/.test(n.text)));
    assert.deepStrictEqual(again.memory.tried("weather"), ["weather in Oslo", "is it raining in Oslo", "top 3 stories on hacker news"]);
    // Accepted, it goes to the hub's /suggestions; a dismissal is remembered with its reason.
    agent.decide(offered[0].id, "accepted");
    assert.deepStrictEqual(await agent.send(), { status: "ok", kept: 1, refused: [] });
    const kept = (await call("GET", "suggestions")).suggestions;
    assert.strictEqual(kept.length, 1);
    assert.deepStrictEqual(kept[0].by, { agent: "scout-one", model: "scripted", engine: "scripted" });
    assert.ok(/no literal words/.test(agent.checkTrigger("weather", "{place}", ["Oslo"]).why));
    // A pattern that would take another tree's sentence is refused.
    const thief = agent.checkTrigger("weather", "top {place} on hacker news", ["top 3 stories on hacker news"]);
    assert.ok(/none of the sentences|would take/.test(thief.why), thief.why);
  }

  // A gap: the rules draft the branch, the model says what it means, the tree still loads.
  {
    const { call } = swarm();
    const engine = model([[/no branch covers/, { kind: "fail", say: "{{ inputs.host }} answered {{ state.resp.status }}, which is not a final answer.", reason: "Odd Status!", why: "1xx is informational" }]]);
    const agent = new A.Agent({ engine, hub: call, clock, raw: (n) => vectors.trees[n], limits: { pause: 0 } });
    await agent.trees();
    const item = await agent.meaning({ tree: "site-status", node: "status", on: "resp.status", observed: 100 });
    assert.strictEqual(item.suggestion.meaning.reason, "odd-status");
    assert.deepStrictEqual(item.preview.draft.when, { eq: 100 });
    assert.strictEqual(item.preview.draft.then, "status-100");
    const grown = Pyrtan.grow.growTree(vectors.trees["site-status"], item.preview.draft, item.suggestion.meaning);
    const branches = grown.nodes.status.branches;
    assert.deepStrictEqual(branches[branches.length - 2], { when: { eq: 100 }, then: "status-100" });
    assert.ok("else" in branches[branches.length - 1]);
    assert.strictEqual(await agent.meaning({ tree: "site-status", node: "status", on: "resp.status", observed: 200 }), null, "a covered value asks nothing");
    // A lookup's gap asks for a row, which has no node for a meaning to go in.
    assert.strictEqual(await agent.meaning({ tree: "weather", node: "sky", on: "code", observed: 100 }), null, "a lookup asks nothing");
    // A model that reaches for env is refused by the rules, whatever it says.
    const sly = new A.Agent({ engine: model([[/no branch covers/, { kind: "done", say: "The key is {{ env.API_KEY }}" }]]), hub: call, clock, raw: (n) => vectors.trees[n] });
    const r = await sly.meaning({ tree: "site-status", node: "status", on: "resp.status", observed: 150 });
    assert.ok(r.refused && /not env/.test(r.why), JSON.stringify(r));
    assert.strictEqual(sly.suggestions.length, 0);
  }

  // Unanswered: a tree the model names gets a trigger; none named, a new tree, with hosts only as a hint.
  {
    const { call } = swarm();
    await call("POST", "intent", { text: "make me a sandwich" });
    const agent = new A.Agent({ engine: model([[/No trigger read it/, { tree: null, description: "Makes sandwiches.", hosts: ["sandwich.example"], why: "nothing does" }]]), hub: call, clock });
    const out = await agent.unanswered();
    assert.strictEqual(out.length, 1);
    assert.deepStrictEqual([out[0].suggestion.kind, out[0].suggestion.text, out[0].suggestion.hosts_hint], ["tree", "make me a sandwich", ["sandwich.example"]]);
    assert.ok(/pyrtan new/.test(out[0].preview.text));
    agent.decide(out[0].id, "dismissed", "no sandwiches here");
    assert.deepStrictEqual(await agent.unanswered(), [], "a dismissed suggestion is not made again");
    assert.ok(agent.memory.notes.some((n) => /dismissed a new tree for "make me a sandwich": no sandwiches here/.test(n.text)));
  }

  // Chat: commands by rules; free text to the model, which acts through ASK/REMEMBER lines.
  {
    const { call } = swarm();
    const engine = model([
      [/You asked the swarm/, (task) => "The weather tree says: " + /-> weather \(done\): (.*)/.exec(task)[1]],
      [/The person says/, "Let me ask the trees.\nASK: weather in Oslo\nREMEMBER: the person lives in Oslo\nASK: one\nASK: two\nASK: three"],
    ]);
    const agent = new A.Agent({ engine, hub: call, clock, limits: { actions: 2 } });
    const r = await agent.chat("what's it like outside?");
    assert.deepStrictEqual(r.actions.map((a) => a.kind), ["ask", "remember"]);
    assert.strictEqual(r.actions[0].result.tree, "weather");
    assert.ok(r.say.startsWith("The weather tree says: Oslo, Norway"));
    assert.ok(agent.memory.notes.some((n) => n.text === "the person lives in Oslo" && n.source === "agent"));
    assert.deepStrictEqual(agent.memory.chat.map((m) => m.role), ["you", "agent"]);
    // The next turn carries the conversation and what it remembers.
    await agent.chat("and tomorrow?");
    const last = engine.calls[engine.calls.length - 1].messages;
    assert.ok(last[0].content.includes("the person lives in Oslo"));
    assert.ok(last.some((m) => m.role === "user" && m.content === "what's it like outside?"));
    assert.ok((await agent.chat("/read is it raining in Bergen")).say.startsWith("No trigger reads"));
    assert.ok((await agent.chat("/read weather in Bergen")).say.startsWith('weather, with {"place":"Bergen"}'));
    assert.ok((await agent.chat("/ask weather in Oslo")).say.startsWith("weather (done): Oslo"));
    assert.strictEqual((await agent.chat("/remember the units are celsius")).say, "Kept #2.");
    assert.ok((await agent.chat("/memory")).say.includes("(you) the units are celsius"));
    assert.ok((await agent.chat("/nope")).say.startsWith("No command or chat skill"));
  }

  // Skills: the person's own, added to the job they name, called as /name in chat.
  {
    const storage = A.memoryStorage();
    const skills = new A.Skills(storage);
    assert.throws(() => skills.add({ name: "probe", instructions: "x" }), /taken/);
    assert.throws(() => skills.add({ name: "Bad Name", instructions: "x" }), /lowercase/);
    assert.throws(() => skills.add({ name: "empty", instructions: " " }), /instructions/);
    skills.add({ name: "tourist", when: "probe", description: "probe as a tourist", instructions: "Write like a tourist." });
    const [md] = skills.import("---\nname: terse\ndescription: five words\nwhen: chat\n---\nAnswer in five words.");
    assert.strictEqual(md.name, "terse");
    assert.ok(skills.for("probe").includes("Skill tourist (probe as a tourist): Write like a tourist."));
    assert.ok(!skills.for("chat").includes("tourist"));
    skills.toggle("tourist", false);
    assert.ok(!skills.for("probe").includes("tourist"));
    assert.deepStrictEqual(new A.Skills(storage).list().filter((s) => !s.builtin).map((s) => [s.name, s.on]), [["tourist", false], ["terse", true]]);
    const back = new A.Skills(A.memoryStorage());
    back.import(skills.export());
    assert.strictEqual(back.list().length, A.BUILTIN.length + 2);
    const { call } = swarm();
    const engine = model([[/Use the skill terse now/, "Sunny, mild, light wind."]]);
    const agent = new A.Agent({ engine, hub: call, clock, skills });
    assert.strictEqual((await agent.chat("/terse how is Oslo")).say, "Sunny, mild, light wind.");
    assert.ok((await agent.chat("/skills")).say.includes("on  terse (chat)"));
  }

  // A patrol: every tree, then the gaps, then the unanswered, in one report.
  {
    const { call } = swarm();
    await call("POST", "intent", { text: "make me a sandwich" });
    const engine = model([[/new sentences/, { sentences: [] }], [/No trigger read it/, { tree: null, description: "" }]]);
    const agent = new A.Agent({ engine, hub: call, clock, limits: { pause: 0 } });
    const steps = [];
    agent.onEvent = (e) => { if (e.type === "patrol") steps.push(e.step); };
    const report = await agent.patrol({ trees: ["weather", "github-release"] });
    assert.deepStrictEqual(steps, ["probe", "probe", "gaps", "unanswered", "done"]);
    assert.deepStrictEqual(report.trees.map((t) => t.tree), ["weather", "github-release"]);
    assert.strictEqual(report.suggestions.length, 1);
    assert.ok(agent.memory.notes.some((n) => n.text.startsWith("patrol: 0 sentences over 2 trees")));
  }
  // In a chat lobby: its own words, a prompt that says what it is (or, in the game, that it is playing), nothing asked of the swarm.
  {
    let asked = 0;
    const hub = async () => { asked++; return {}; };
    const skills = new A.Skills(A.memoryStorage());
    skills.add({ name: "pirate", when: "lobby", instructions: "Talk like a pirate." });
    const engine = new A.ScriptedEngine((messages) => (messages[0].content.includes("person or agent?") ? "lol not much hbu" : 'scout: "Ahoy! I am scout, an agent."'));
    const agent = new A.Agent({ name: "scout", engine, hub, clock, skills });
    const said = await agent.stranger([{ from: "stranger", text: "hi" }, { from: "stranger", text: "who are you?" }, { from: "me", text: "hello" }, { from: "stranger", text: "you there?" }]);
    assert.strictEqual(said, "Ahoy! I am scout, an agent.");
    const sent = engine.calls[0].messages;
    assert.ok(sent[0].content.startsWith("You are scout, an agent: a model (scripted)") && sent[0].content.includes("never say or suggest that you are a person") && sent[0].content.includes("Skill pirate"));
    assert.deepStrictEqual(sent.slice(1).map((m) => [m.role, m.content]), [["user", "hi\nwho are you?"], ["assistant", "hello"], ["user", "you there?"]]);
    // The game: the game's skill and the persona, not the lobby's; an opening line when nobody has spoken.
    assert.strictEqual(await agent.stranger([], { game: true, persona: "a night-shift nurse in Leeds" }), "lol not much hbu");
    const game = engine.calls[1].messages;
    assert.ok(game[0].content.includes("trying to seem like a person") && game[0].content.includes("a night-shift nurse in Leeds") && !game[0].content.includes("Skill pirate"));
    assert.ok(game[game.length - 1].role === "user" && game[game.length - 1].content.includes("open with one short line"));
    // The person's last word is the stranger's when the agent spoke last.
    await agent.stranger([{ from: "stranger", text: "hi" }, { from: "me", text: "hey" }]);
    assert.ok(engine.calls[2].messages.at(-1).content.includes("said nothing since"));
    assert.strictEqual(asked, 0);
    assert.deepStrictEqual(agent.memory.chat, []);  // a stranger's conversation is not the person's
    assert.strictEqual(A.skillProblem({ name: "nurse", when: "game", instructions: "x" }), null);
  }
  console.log("agents ok");
})().catch((e) => { console.error(e); process.exit(1); });
