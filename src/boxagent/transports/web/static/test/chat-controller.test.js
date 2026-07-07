"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { ChatController } = require("./load");

// 整个实时会话 controller 都经 `app` 注入，所以用假 app（mock api + spy 组件）
// 驱动它 — 无真 EventSource/fetch/DOM。断言针对 `app` 服务边界
//（chatLog / recents / api / setConn / sessionInfoEl / refreshSessionList），
// 因为 controller 自身的辅助函数（addMessage、touchSession…）是其内部实现。
function makeApp(over = {}) {
  const calls = [];
  const setTexts = [];
  const rec = (n) => (...a) => { calls.push([n, ...a]); };
  const state = Object.assign(
    { streamMsgs: {}, serverSessions: {}, sessions: {}, machines: [], bot: "b", botMachine: "m", chatId: "c", subscribed: null,
      historyOffset: 0, historyTotal: 0, historyLoading: false, historyExhausted: false },
    over.state || {},
  );
  const app = {
    _calls: calls, _setTexts: setTexts, state,
    TOKEN: "", HISTORY_PAGE_SIZE: 50,
    api: over.api || (async () => ({ ok: true, json: async () => ({}), text: async () => "" })),
    $: () => ({ classList: { add: rec("mask.add"), remove: rec("mask.remove") } }),
    curKey: () => `${state.botMachine}|${state.bot}`,
    setConn: rec("setConn"),
    showRecapBanner: rec("showRecapBanner"),
    chatTitle: { textContent: "" },
    sessionInfoEl: { setInfo: (v) => calls.push(["setInfo", v]) },
    sendBtn: { disabled: false },
    multiplex: {
      subscribe: (machine, bot, chatId, handler) => calls.push(["mux.subscribe", machine, bot, chatId, handler]),
      unsubscribe: (machine, bot, chatId) => calls.push(["mux.unsubscribe", machine, bot, chatId]),
    },
    chatLog: {
      addMessage: (...a) => { calls.push(["addMessage", ...a]); return { setText: (t) => setTexts.push(t) }; },
      showTyping: rec("showTyping"), removeTyping: rec("removeTyping"), scrollToBottom: rec("scrollToBottom"),
      upsertToolCall: rec("upsertToolCall"), applyToolResult: rec("applyToolResult"),
      setHistory: rec("setHistory"), prependHistory: rec("prependHistory"), onLoadOlder: null,
    },
    recents: { touch: (p) => calls.push(["recents.touch", p]) },
    refreshSessionList: rec("refreshSessionList"),
    fetchServerSessions: over.fetchServerSessions || (async () => []),
    loadMachines: async () => { calls.push(["loadMachines"]); },
  };
  ChatController(app);
  return app;
}
const has = (app, n) => app._calls.some((c) => c[0] === n);
const arg = (app, n) => (app._calls.find((c) => c[0] === n) || [])[1];
const arg2 = (app, n) => (app._calls.find((c) => c[0] === n) || []).slice(1);

// ── 接线 ──

test("ChatController attaches the public surface + wires onLoadOlder", () => {
  const app = makeApp();
  for (const k of ["switchChat", "sendText", "addMessage", "openStream", "handleEvent"]) {
    assert.equal(typeof app[k], "function", k);
  }
  assert.equal(typeof app.chatLog.onLoadOlder, "function", "onLoadOlder wired");
});

// ── handleEvent 事件处理 ──

test("message → adds a bubble + bumps the session (touchSession)", () => {
  const app = makeApp();
  app.handleEvent({ type: "message", role: "assistant", text: "hi", message_id: "1" });
  assert.ok(has(app, "addMessage"));
  assert.ok(has(app, "refreshSessionList"), "touchSession refreshed the list");
  assert.ok(has(app, "recents.touch"), "touchSession bumped recents");
});

test("typing → showTyping; stream_start → removeTyping + register", () => {
  const app = makeApp();
  app.handleEvent({ type: "typing" });
  assert.ok(has(app, "showTyping"));
  app.handleEvent({ type: "stream_start", message_id: "m1" });
  assert.ok(has(app, "removeTyping"));
  assert.ok(app.state.streamMsgs.m1, "stream message registered");
});

test("stream_delta accumulates into the element; unknown id ignored", () => {
  const app = makeApp();
  app.handleEvent({ type: "stream_start", message_id: "m1" });
  app.handleEvent({ type: "stream_delta", message_id: "m1", delta: "he" });
  app.handleEvent({ type: "stream_delta", message_id: "m1", delta: "llo" });
  assert.deepEqual(app._setTexts, ["he", "hello"]);
  const before = app._calls.length;
  app.handleEvent({ type: "stream_delta", message_id: "ghost", delta: "x" });
  assert.equal(app._calls.length, before, "unknown id is a no-op");
});

test("stream_end clears the stream msg + refreshes list/info", async () => {
  const app = makeApp();
  app.state.streamMsgs.m1 = { el: { setText() {} }, text: "done" };
  app.handleEvent({ type: "stream_end", message_id: "m1", text: "done" });
  assert.ok(!app.state.streamMsgs.m1, "stream msg removed");
  await new Promise((r) => setTimeout(r, 0)); // 等 fetchServerSessions().then(...)
  assert.ok(has(app, "refreshSessionList"));
  assert.ok(has(app, "setInfo"), "refreshSessionInfo ran");
});

test("tool_call / tool_result route to chatLog", () => {
  const app = makeApp();
  app.handleEvent({ type: "tool_call", tool_id: "t1", name: "read" });
  app.handleEvent({ type: "tool_result", tool_id: "t1", ok: true });
  assert.ok(has(app, "upsertToolCall"));
  assert.ok(has(app, "applyToolResult"));
});

test("_close → connection offline", () => {
  const app = makeApp();
  app.handleEvent({ type: "_close" });
  assert.deepEqual(app._calls.at(-1), ["setConn", "offline"]);
});

// ── switchChat 切换会话 ──

test("switchChat resolves title, swaps history, opens the stream", async () => {
  const app = makeApp({
    state: { serverSessions: { "m|b": [{ chat_id: "c1", custom_title: "Renamed", session_id: "s" }] } },
    api: async (path) =>
      path.startsWith("history")
        ? { ok: true, json: async () => ({ history: [{ role: "user", text: "h" }], total: 5 }) }
        : { ok: true, json: async () => ({ info: null }) },
  });
  app.state.subscribed = { machine: "m", bot: "b", chat_id: "c0" };
  await app.switchChat("c1");
  assert.deepEqual(arg2(app, "mux.unsubscribe"), ["m", "b", "c0"], "old chat unsubscribed");
  assert.equal(app.state.chatId, "c1");
  assert.equal(app.chatTitle.textContent, "Renamed", "custom_title wins");
  assert.ok(has(app, "setHistory"));
  assert.equal(app.state.historyTotal, 5);
  assert.equal(app.state.historyOffset, 1);
  assert.equal(app.state.historyExhausted, false);
  assert.deepEqual(app._calls.find((c) => c[0] === "setConn"), ["setConn", "connecting"]);
  const sub = app._calls.find((c) => c[0] === "mux.subscribe");
  assert.ok(sub, "openStream subscribed on the multiplex socket");
  assert.deepEqual(sub.slice(1, 4), ["m", "b", "c1"], "subscribed the new chat tag");
  assert.deepEqual(app.state.subscribed, { machine: "m", bot: "b", chat_id: "c1" });
});

test("switchChat with a failed history fetch renders an empty log", async () => {
  const app = makeApp({ api: async () => ({ ok: false, json: async () => ({}) }) });
  await app.switchChat("cX");
  assert.deepEqual(arg(app, "setHistory"), []);
});

// ── sendText 发送 ──

test("sendText: offline machine → notice, no POST", async () => {
  let posted = false;
  const app = makeApp({
    state: { machines: [{ machine_id: "m", online: false }] },
    api: async () => { posted = true; return { ok: true }; },
  });
  await app.sendText("hi");
  assert.ok(!posted, "no api call when offline");
  assert.ok(app._calls.some((c) => c[0] === "addMessage" && /offline/.test(c[2])));
});

test("sendText: 502 → error notice + refresh machines", async () => {
  const app = makeApp({
    state: { machines: [{ machine_id: "m", online: true }] },
    api: async () => ({ ok: false, status: 502, text: async () => "gone" }),
  });
  await app.sendText("hi");
  assert.ok(app._calls.some((c) => c[0] === "addMessage" && /Error \(502\)/.test(c[2])));
  assert.ok(has(app, "loadMachines"));
});

test("sendText: happy path posts, no error bubble", async () => {
  let posted = false;
  const app = makeApp({
    state: { machines: [{ machine_id: "m", online: true }] },
    api: async () => { posted = true; return { ok: true }; },
  });
  await app.sendText("hi");
  assert.ok(posted);
  assert.ok(!app._calls.some((c) => c[0] === "addMessage"), "no error message on success");
});

// ── loadOlderHistory 加载更早历史 ──

test("loadOlderHistory prepends + advances offset; empty → exhausted", async () => {
  const app = makeApp({ api: async () => ({ ok: true, json: async () => ({ history: [{ role: "user", text: "x" }], total: 9 }) }) });
  await app.chatLog.onLoadOlder();
  assert.ok(has(app, "prependHistory"));
  assert.equal(app.state.historyOffset, 1);

  const app2 = makeApp({ api: async () => ({ ok: true, json: async () => ({ history: [], total: 9 }) }) });
  await app2.chatLog.onLoadOlder();
  assert.equal(app2.state.historyExhausted, true);
  assert.ok(!has(app2, "prependHistory"));
});

test("loadOlderHistory bails when already exhausted", async () => {
  let called = false;
  const app = makeApp({ state: { historyExhausted: true }, api: async () => { called = true; return { ok: true, json: async () => ({}) }; } });
  await app.chatLog.onLoadOlder();
  assert.ok(!called);
});

// ── refreshSessionInfo（已在 stream_end 路径覆盖过；此处直接测）──

test("touchSession infers platform + persists via recents", () => {
  const app = makeApp({ state: { chatId: "web-42" } });
  app.handleEvent({ type: "message", role: "assistant", text: "hey" });
  const payload = arg(app, "recents.touch");
  assert.equal(payload.platform, "web");
  assert.equal(payload.chat_id, "web-42");
});
