"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot } = require("./load");

function attach() {
  const root = makeRoot();
  const el = document.createElement("chat-log");
  root.appendChild(el); // connected → connectedCallback
  return el;
}

// Display-method tests only — history replace/prepend use DocumentFragment +
// scroll-height math that the DOM stub doesn't model; those run in the browser.

test("addMessage appends a <chat-message> and returns it (for streaming)", () => {
  const log = attach();
  const el = log.addMessage("assistant", "hi");
  assert.equal(el.localName, "chat-message");
  assert.equal(log.children.length, 1);
  assert.equal(log.children[0], el);
});

test("showTyping adds one indicator; idempotent; removeTyping clears it", () => {
  const log = attach();
  log.showTyping();
  log.showTyping(); // no duplicate
  assert.equal(log.children.filter((c) => c.classList.contains("typing")).length, 1);
  log.removeTyping();
  assert.ok(!log.querySelector(".typing"));
});

test("addMessage removes a pending typing indicator first", () => {
  const log = attach();
  log.showTyping();
  log.addMessage("assistant", "answer");
  assert.ok(!log.querySelector(".typing"), "typing cleared when a message lands");
  assert.equal(log.children.length, 1);
});

test("upsertToolCall / applyToolResult render a tool-card into the log", () => {
  const log = attach();
  log.upsertToolCall("t1", "read_file", { path: "x" });
  const card = log.querySelector("tool-card");
  assert.ok(card, "tool-card created in the log");
  log.applyToolResult("t1", true, "ok");
  assert.equal(log.children.length, 1, "same tool id stays one card (ToolCard dedup)");
});

test("scrolling near the top calls onLoadOlder; elsewhere it doesn't", () => {
  const log = attach();
  let fired = 0;
  log.onLoadOlder = () => { fired++; };

  log.scrollTop = 50; // < 100 → near top
  log.dispatchEvent({ type: "scroll" });
  assert.equal(fired, 1);

  log.scrollTop = 300; // not near top
  log.dispatchEvent({ type: "scroll" });
  assert.equal(fired, 1, "no extra call when scrolled away from the top");
});
