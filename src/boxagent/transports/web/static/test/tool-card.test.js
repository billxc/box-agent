"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { ToolCard, makeRoot } = require("./load");

function toolCards(root) {
  return root.children.filter((c) => c.localName === "tool-card");
}

test("upsertCall creates a connected card with a ▶ header", () => {
  const root = makeRoot();
  const card = ToolCard.upsertCall(root, "t1", "grep", { pattern: "foo" });
  assert.ok(card.isConnected, "card should connect (connectedCallback ran)");
  assert.equal(card.getAttribute("data-tool-id"), "t1");
  assert.match(card.querySelector("summary").textContent, /^▶ grep\(/);
});

test("upsertCall is idempotent on toolId — updates, no duplicate", () => {
  const root = makeRoot();
  ToolCard.upsertCall(root, "t1", "grep", { pattern: "a" });
  ToolCard.upsertCall(root, "t1", "grep", { pattern: "bbb" });
  assert.equal(toolCards(root).length, 1);
  assert.match(root.querySelector(`tool-card[data-tool-id="t1"]`).querySelector("summary").textContent, /bbb/);
});

test("applyResult flips header to ✓ and reveals the result", () => {
  const root = makeRoot();
  ToolCard.upsertCall(root, "t1", "ls", {});
  ToolCard.applyResult(root, "t1", true, "done");
  const card = root.querySelector(`tool-card[data-tool-id="t1"]`);
  assert.match(card.querySelector("summary").textContent, /^✓ ls/);
  const result = card.querySelector(".tool-card-result");
  assert.ok(!result.classList.contains("hidden"));
  assert.equal(result.textContent, "done");
  assert.ok(result.classList.contains("ok"));
});

test("applyResult on a failure shows ✗ + error", () => {
  const root = makeRoot();
  ToolCard.upsertCall(root, "t1", "rm", {});
  ToolCard.applyResult(root, "t1", false, "", "boom");
  const result = root.querySelector(".tool-card-result");
  assert.equal(result.textContent, "boom");
  assert.ok(result.classList.contains("failed"));
});

test("applyResult without a preceding call synthesises a card", () => {
  const root = makeRoot();
  ToolCard.applyResult(root, "t9", true, "late");
  assert.equal(toolCards(root).length, 1);
  assert.equal(root.querySelector(`tool-card[data-tool-id="t9"]`).querySelector(".tool-card-result").textContent, "late");
});

test("subagent attribute tags the inner details", () => {
  const root = makeRoot();
  const card = ToolCard.upsertCall(root, "t1", "task", {}, "parent-1");
  assert.ok(card.querySelector("details").classList.contains("subagent"));
});

test("a card staged into a detached fragment renders on connect", () => {
  const { El } = require("./dom-stub");
  const frag = new El("div"); // not connected
  ToolCard.upsertCall(frag, "h1", "grep", { pattern: "x" });
  const card = frag.querySelector(`tool-card[data-tool-id="h1"]`);
  assert.ok(!card.isConnected, "buffered, not yet built");
  const root = makeRoot();
  root.appendChild(frag);
  assert.ok(card.isConnected);
  assert.match(card.querySelector("summary").textContent, /^▶ grep/);
});
