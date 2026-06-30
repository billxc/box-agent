"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { loadSessions, saveSessions, buildSessionList, defaultTitle, shortId } = require("./load");

// ── shortId ──

test("shortId leaves short ids alone, truncates long ones", () => {
  assert.equal(shortId("abc123"), "abc123");
  assert.equal(shortId("0123456789abcdef"), "012345…cdef");
});

// ── defaultTitle ──

test("defaultTitle: claude / platform tag / fallback", () => {
  assert.equal(defaultTitle({ platform: "claude", chat_id: "x" }), "✦ Resumed Claude session");
  assert.match(defaultTitle({ platform: "telegram", chat_id: "tg-1" }), /^Telegram · /);
  assert.match(defaultTitle({ platform: "weird", chat_id: "z" }), /^Chat · /);
});

// ── loadSessions / saveSessions (localStorage round-trip) ──

test("save then load round-trips per machine|bot key", () => {
  localStorage.clear();
  saveSessions("m1", "b1", { c1: { title: "T" } });
  assert.deepEqual(loadSessions("m1", "b1"), { c1: { title: "T" } });
  assert.deepEqual(loadSessions("m1", "other"), {}, "different bot is isolated");
});

test("loadSessions returns {} on missing / corrupt data", () => {
  localStorage.clear();
  assert.deepEqual(loadSessions("m", "b"), {});
  localStorage.setItem("ba.sessions.m|b", "{not json");
  assert.deepEqual(loadSessions("m", "b"), {});
});

// ── buildSessionList (the merge) ──

test("server entry: backend title wins, falls back to local title then defaultTitle", () => {
  const [withCustom] = buildSessionList({}, [{ chat_id: "a", custom_title: "Custom", platform: "web" }]);
  assert.equal(withCustom.title, "Custom");

  const [withLocal] = buildSessionList({ b: { title: "LocalName" } }, [{ chat_id: "b", platform: "web" }]);
  assert.equal(withLocal.title, "LocalName");

  const [fallback] = buildSessionList({}, [{ chat_id: "telegram-9", platform: "telegram" }]);
  assert.match(fallback.title, /^Telegram · /);
});

test("local-only entries are included; web- prefix infers platform", () => {
  const list = buildSessionList({ "web-123": { title: "New", ts: 5 } }, []);
  assert.equal(list.length, 1);
  assert.equal(list[0].platform, "web");
  assert.equal(list[0].title, "New");
});

test("server + local for the same chat_id merge into one entry (server wins shape)", () => {
  const list = buildSessionList(
    { c: { title: "Local", ts: 1 } },
    [{ chat_id: "c", platform: "web", custom_title: "Server" }],
  );
  assert.equal(list.length, 1, "no duplicate for the same chat_id");
  assert.equal(list[0].title, "Server");
});

test("entries are sorted newest-first by ts", () => {
  const list = buildSessionList({}, [
    { chat_id: "old", platform: "web", last_ts: 100 },
    { chat_id: "new", platform: "web", last_ts: 200 },
  ]);
  assert.deepEqual(list.map((e) => e.chat_id), ["new", "old"]);
});

test("server last_ts (seconds) is normalised to ms", () => {
  const [e] = buildSessionList({}, [{ chat_id: "a", platform: "web", last_ts: 100 }]);
  assert.equal(e.ts, 100 * 1000);
});
