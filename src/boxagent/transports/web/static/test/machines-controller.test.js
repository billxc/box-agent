"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { MachinesController } = require("./load");

// Driven through a fake `app` (mock api + spy panels/switchChat) — no real
// fetch/DOM. confirm/alert/prompt are globals the restart/token paths use.
globalThis.confirm = () => true;
globalThis.alert = () => {};

function makeApp(over = {}) {
  const calls = [];
  const rec = (n) => (...a) => { calls.push([n, ...a]); };
  const state = Object.assign(
    { machines: [], bot: null, botMachine: null, sessions: {}, serverSessions: {}, refreshTimer: null },
    over.state || {},
  );
  const app = {
    _calls: calls, state,
    api: over.api || (async () => ({ ok: true, json: async () => ({ machines: [] }) })),
    machinesPanel: { render: rec("machinesPanel.render") },
    recents: { render: rec("recents.render") },
    chatTitle: { textContent: "" },
    $: () => ({ classList: { remove: rec("mask.remove"), add: rec("mask.add") } }),
    sessionsPanel: { showLoading: rec("showLoading") },
    sessionsOf: { textContent: "" },
    fetchServerSessions: over.fetchServerSessions || (async () => []),
    switchChat: async (id) => { calls.push(["switchChat", id]); },
    botKey: (m, b) => `${m}|${b}`,
    pickFirstSessionId: () => null,
    uuid: () => "web-new",
    closeSidebar: rec("closeSidebar"),
  };
  MachinesController(app);
  return app;
}
const has = (app, n) => app._calls.some((c) => c[0] === n);
const arg = (app, n) => (app._calls.find((c) => c[0] === n) || [])[1];

test("MachinesController attaches the public surface", () => {
  const app = makeApp();
  for (const k of ["loadMachines", "selectBot", "restartMachine", "restartCluster", "startMachinePoll"]) {
    assert.equal(typeof app[k], "function", k);
  }
});

test("loadMachines stores machines + renders panel + recents (bot already set → no auto-pick)", async () => {
  const app = makeApp({
    state: { bot: "b", botMachine: "m" },
    api: async () => ({ ok: true, json: async () => ({ machines: [{ machine_id: "m", online: true, bots: [] }] }) }),
  });
  await app.loadMachines();
  assert.equal(app.state.machines.length, 1);
  assert.ok(has(app, "machinesPanel.render"));
  assert.ok(has(app, "recents.render"));
  assert.ok(!has(app, "switchChat"), "no auto-pick when a bot is already selected");
});

test("loadMachines auto-picks the first online bot on first load", async () => {
  const app = makeApp({
    api: async () => ({ ok: true, json: async () => ({ machines: [
      { machine_id: "off", online: false, bots: [{ name: "x" }] },
      { machine_id: "m2", online: true, bots: [{ name: "b2" }] },
    ] }) }),
  });
  await app.loadMachines();
  assert.equal(app.state.bot, "b2");
  assert.equal(app.state.botMachine, "m2");
  assert.ok(has(app, "switchChat"), "selectBot ran → opened a chat");
});

test("loadMachines marks the current bot offline when its machine drops", async () => {
  const app = makeApp({
    state: { bot: "b", botMachine: "m" },
    api: async () => ({ ok: true, json: async () => ({ machines: [{ machine_id: "m", online: false, bots: [] }] }) }),
  });
  await app.loadMachines();
  assert.match(app.chatTitle.textContent, /offline/);
});

test("selectBot sets selection, loads sessions, opens last chat", async () => {
  const app = makeApp({ fetchServerSessions: async () => [{ chat_id: "c9" }] });
  await app.selectBot("botA", "machA");
  assert.equal(app.state.bot, "botA");
  assert.equal(app.state.botMachine, "machA");
  assert.equal(app.sessionsOf.textContent, "· botA @ machA");
  assert.ok(has(app, "showLoading"));
  assert.ok(has(app, "machinesPanel.render"));
  assert.ok(has(app, "switchChat"));
  assert.ok(has(app, "closeSidebar"));
});

test("selectBot falls back to uuid when there is no last/first chat", async () => {
  const app = makeApp();
  await app.selectBot("b", "m");
  assert.equal(arg(app, "switchChat"), "web-new");
});

test("restartMachine: offline → no api call", async () => {
  let posted = false;
  const app = makeApp({ api: async () => { posted = true; return { ok: true, json: async () => ({}) }; } });
  await app.restartMachine("m", false);
  assert.ok(!posted);
});

test("restartMachine: online → POST cluster_restart", async () => {
  let path = null;
  const app = makeApp({ api: async (p) => { path = p; return { ok: true, json: async () => ({ results: {} }) }; } });
  await app.restartMachine("m", true);
  assert.equal(path, "admin/cluster_restart");
});

test("startMachinePoll schedules a poll timer", () => {
  const app = makeApp();
  app.startMachinePoll();
  assert.ok(app.state.refreshTimer, "timer set");
  clearInterval(app.state.refreshTimer); // don't leak the interval
});
