"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { ChatController } = require("./load");

// ChatController is pure over the injected `app` — no real EventSource/fetch in
// handleEvent — so we validate the dispatcher with a fake app that records calls.
function makeApp(overrides = {}) {
  const calls = [];
  const rec = (name) => (...args) => { calls.push([name, ...args]); };
  const app = {
    _calls: calls,
    state: { streamMsgs: {}, serverSessions: {}, bot: "b", botMachine: "m" },
    TOKEN: "",
    curKey: () => "m|b",
    setConn: rec("setConn"),
    addMessage: (...a) => { calls.push(["addMessage", ...a]); return { setText: rec("setText") }; },
    showTyping: rec("showTyping"),
    removeTyping: rec("removeTyping"),
    scrollDown: rec("scrollDown"),
    renderToolCall: rec("renderToolCall"),
    renderToolResult: rec("renderToolResult"),
    touchSession: rec("touchSession"),
    fetchServerSessions: async () => [],
    refreshSessionList: rec("refreshSessionList"),
    refreshSessionInfo: rec("refreshSessionInfo"),
    ...overrides,
  };
  ChatController(app); // attaches app.openStream + app.handleEvent
  return app;
}

const names = (app) => app._calls.map((c) => c[0]);

test("ChatController attaches openStream + handleEvent onto app", () => {
  const app = makeApp();
  assert.equal(typeof app.openStream, "function");
  assert.equal(typeof app.handleEvent, "function");
});

test("message → addMessage + touchSession", () => {
  const app = makeApp();
  app.handleEvent({ type: "message", role: "assistant", text: "hi", message_id: "1" });
  assert.ok(names(app).includes("addMessage"));
  assert.ok(names(app).includes("touchSession"));
});

test("typing → showTyping; stream_start clears typing + registers stream msg", () => {
  const app = makeApp();
  app.handleEvent({ type: "typing" });
  assert.ok(names(app).includes("showTyping"));

  app.handleEvent({ type: "stream_start", message_id: "m1" });
  assert.ok(names(app).includes("removeTyping"));
  assert.ok(app.state.streamMsgs.m1, "stream message registered");
});

test("stream_delta appends text into the registered element", () => {
  const app = makeApp();
  const setTexts = [];
  app.state.streamMsgs.m1 = { el: { setText: (t) => setTexts.push(t) }, text: "" };
  app.handleEvent({ type: "stream_delta", message_id: "m1", delta: "he" });
  app.handleEvent({ type: "stream_delta", message_id: "m1", delta: "llo" });
  assert.deepEqual(setTexts, ["he", "hello"]);
  assert.ok(names(app).includes("scrollDown"));
});

test("stream_delta for an unknown message id is ignored", () => {
  const app = makeApp();
  app.handleEvent({ type: "stream_delta", message_id: "ghost", delta: "x" });
  assert.ok(!names(app).includes("scrollDown"));
});

test("stream_end finalizes + refreshes the session list/info", async () => {
  const app = makeApp();
  app.state.streamMsgs.m1 = { el: { setText() {} }, text: "done" };
  app.handleEvent({ type: "stream_end", message_id: "m1", text: "done" });
  assert.ok(!app.state.streamMsgs.m1, "stream msg removed");
  await new Promise((r) => setTimeout(r, 0)); // let the fetchServerSessions().then run
  assert.ok(names(app).includes("refreshSessionList"));
  assert.ok(names(app).includes("refreshSessionInfo"));
});

test("tool_call / tool_result route to the renderers", () => {
  const app = makeApp();
  app.handleEvent({ type: "tool_call", tool_id: "t1", name: "read" });
  app.handleEvent({ type: "tool_result", tool_id: "t1", ok: true });
  assert.ok(names(app).includes("renderToolCall"));
  assert.ok(names(app).includes("renderToolResult"));
});

test("_close marks the connection offline", () => {
  const app = makeApp();
  app.handleEvent({ type: "_close" });
  assert.deepEqual(app._calls.at(-1), ["setConn", "offline"]);
});
