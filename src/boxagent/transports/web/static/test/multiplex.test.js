"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { MultiplexClient } = require("./load");

// A fake WebSocket: records sent frames, lets the test drive open/message/close.
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    FakeSocket.instances.push(this);
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { this.closed = true; this.readyState = 3; if (this.onclose) this.onclose(); }
  // test helpers
  open() { this.readyState = 1; if (this.onopen) this.onopen(); }
  deliver(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
  drop() { this.readyState = 3; if (this.onclose) this.onclose(); }
}
FakeSocket.instances = [];

function makeApp(over = {}) {
  const calls = [];
  FakeSocket.instances = [];
  const app = {
    TOKEN: over.TOKEN || "",
    setConn: (s) => calls.push(["setConn", s]),
    _makeSocket: (url) => new FakeSocket(url),
    _calls: calls,
  };
  MultiplexClient(app);
  return app;
}
const sock = () => FakeSocket.instances.at(-1);

test("subscribe opens one socket and sends a subscribe frame on open", () => {
  const app = makeApp();
  const seen = [];
  app.multiplex.subscribe("m1", "b1", "c1", (ev) => seen.push(ev));
  assert.equal(FakeSocket.instances.length, 1, "exactly one socket opened");
  sock().open();
  assert.deepEqual(sock().sent, [{ type: "subscribe", machine: "m1", bot: "b1", chat_id: "c1" }]);
  assert.deepEqual(app._calls.at(-1), ["setConn", "online"]);
});

test("many chats ride ONE socket (connection count stays 1)", () => {
  const app = makeApp();
  app.multiplex.subscribe("m", "b", "c1", () => {});
  app.multiplex.subscribe("m", "b", "c2", () => {});
  app.multiplex.subscribe("m", "b", "c3", () => {});
  assert.equal(FakeSocket.instances.length, 1, "still a single socket for 3 chats");
  sock().open();
  const kinds = sock().sent.map((f) => `${f.type}:${f.chat_id}`);
  assert.deepEqual(kinds, ["subscribe:c1", "subscribe:c2", "subscribe:c3"]);
});

test("incoming events demux to the matching chat's handler only", () => {
  const app = makeApp();
  const a = [], b = [];
  app.multiplex.subscribe("m", "b", "c1", (ev) => a.push(ev));
  app.multiplex.subscribe("m", "b", "c2", (ev) => b.push(ev));
  sock().open();
  sock().deliver({ machine: "m", bot: "b", chat_id: "c1", event: { type: "message", text: "hi" } });
  sock().deliver({ machine: "m", bot: "b", chat_id: "c2", event: { type: "typing" } });
  sock().deliver({ machine: "m", bot: "b", chat_id: "ghost", event: { type: "message" } });
  assert.deepEqual(a, [{ type: "message", text: "hi" }]);
  assert.deepEqual(b, [{ type: "typing" }]);
});

test("unsubscribe sends a frame and stops delivering to that handler", () => {
  const app = makeApp();
  const a = [];
  app.multiplex.subscribe("m", "b", "c1", (ev) => a.push(ev));
  sock().open();
  app.multiplex.unsubscribe("m", "b", "c1");
  assert.ok(
    sock().sent.some((f) => f.type === "unsubscribe" && f.chat_id === "c1"),
    "unsubscribe frame sent",
  );
  sock().deliver({ machine: "m", bot: "b", chat_id: "c1", event: { type: "message" } });
  assert.deepEqual(a, [], "no delivery after unsubscribe");
});

test("re-subscribe to same chat is idempotent (no duplicate subscribe frame)", () => {
  const app = makeApp();
  app.multiplex.subscribe("m", "b", "c1", () => {});
  sock().open();
  const before = sock().sent.length;
  app.multiplex.subscribe("m", "b", "c1", () => {}); // same tag again
  assert.equal(sock().sent.length, before, "no extra subscribe frame for same chat");
});

test("subscribe frames are buffered until the socket opens", () => {
  const app = makeApp();
  app.multiplex.subscribe("m", "b", "c1", () => {});
  assert.deepEqual(sock().sent, [], "nothing sent while CONNECTING");
  sock().open();
  assert.deepEqual(sock().sent, [{ type: "subscribe", machine: "m", bot: "b", chat_id: "c1" }]);
});

test("on drop it reconnects and re-declares interest", async () => {
  const app = makeApp();
  app.multiplex.subscribe("m", "b", "c1", () => {});
  sock().open();
  const first = sock();
  first.drop(); // server/network drop (not closedByUs)
  assert.deepEqual(app._calls.at(-1), ["setConn", "offline"]);
  // reconnect is scheduled with a 1s backoff; wait for it.
  await new Promise((r) => setTimeout(r, 1100));
  assert.equal(FakeSocket.instances.length, 2, "a new socket was opened");
  sock().open();
  assert.deepEqual(
    sock().sent,
    [{ type: "subscribe", machine: "m", bot: "b", chat_id: "c1" }],
    "still-open chat re-subscribed after reconnect",
  );
});

test("close() tears down and does not reconnect", async () => {
  const app = makeApp();
  app.multiplex.subscribe("m", "b", "c1", () => {});
  sock().open();
  app.multiplex.close();
  assert.ok(sock().closed, "socket closed");
  await new Promise((r) => setTimeout(r, 1100));
  assert.equal(FakeSocket.instances.length, 1, "no reconnect after explicit close");
});

test("token is placed on the ws URL query", () => {
  const app = makeApp({ TOKEN: "sekret" });
  app.multiplex.subscribe("m", "b", "c1", () => {});
  assert.ok(sock().url.includes("token=sekret"), "token in ws URL");
  assert.ok(sock().url.startsWith("wss:"), "https upgrades to wss");
});
