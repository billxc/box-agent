"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot } = require("./load");

function attach() {
  localStorage.clear(); // recents persist in the shared stub store — isolate per test
  const root = makeRoot();
  const el = document.createElement("recents-panel");
  root.appendChild(el); // connected → connectedCallback builds the inner <ul>
  return el;
}

function rec(chat_id, over = {}) {
  return { machine: "m1", bot: "b1", chat_id, title: "T-" + chat_id, platform: "web", ...over };
}

test("connectedCallback builds an inner ul#recents-list; empty shows placeholder", () => {
  const el = attach();
  const ul = el.querySelector("ul");
  assert.ok(ul, "inner ul built");
  assert.equal(ul.id, "recents-list", "id preserved for CSS");
  assert.equal(el._list.children[0].textContent, "No recent chats");
});

test("touch adds an entry; dedup bumps to top without duplicating", () => {
  const el = attach();
  el.touch(rec("a"));
  el.touch(rec("b"));
  el.touch(rec("a")); // re-touch a → moves to front, no dup
  const all = el._load();
  assert.equal(all.length, 2);
  assert.equal(all[0].chat_id, "a", "re-touched entry is newest");
  assert.equal(all[1].chat_id, "b");
});

test("touch ignores entries missing machine/bot/chat_id", () => {
  const el = attach();
  el.touch({ machine: "m1", bot: "b1" }); // no chat_id
  assert.equal(el._load().length, 0);
});

test("touch caps the list at 25 (newest kept)", () => {
  const el = attach();
  for (let i = 0; i < 30; i++) el.touch(rec("c" + i));
  const all = el._load();
  assert.equal(all.length, 25);
  assert.equal(all[0].chat_id, "c29", "newest first");
});

test("render marks the active entry from getContext", () => {
  const el = attach();
  el.getContext = () => ({ machines: [{ machine_id: "m1", online: true }], bot: "b1", botMachine: "m1", chatId: "a" });
  el.touch(rec("a"));
  el.touch(rec("z"));
  el.render();
  assert.ok(el.querySelector(".active"), "active li present");
});

test("render marks entries on offline machines", () => {
  const el = attach();
  el.getContext = () => ({ machines: [{ machine_id: "m1", online: true }], bot: "b1", botMachine: "m1", chatId: "x" });
  el.touch(rec("a", { machine: "m2" })); // m2 not in online set
  el.render();
  assert.ok(el.querySelector(".offline"), "offline li present");
});

test("clear empties the list", () => {
  const el = attach();
  globalThis.confirm = () => true;
  el.touch(rec("a"));
  assert.equal(el._load().length, 1);
  el.clear();
  assert.equal(el._load().length, 0);
  assert.equal(el._list.children[0].textContent, "No recent chats");
});

test("remove drops the matching entry", () => {
  const el = attach();
  el.touch(rec("a"));
  el.touch(rec("b"));
  el.remove("m1", "b1", "a");
  const all = el._load();
  assert.equal(all.length, 1);
  assert.equal(all[0].chat_id, "b");
});
