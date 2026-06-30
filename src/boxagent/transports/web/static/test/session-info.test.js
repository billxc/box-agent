"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot } = require("./load");

function attach() {
  const root = makeRoot();
  const el = document.createElement("session-info");
  root.appendChild(el);
  return el;
}

test("setInfo renders backend + context with token formatting", () => {
  const el = attach();
  el.setInfo({ backend_kind: "agent-sdk-claude", context_window: 200000, context_used: 50000 });
  assert.equal(el.textContent, "agent-sdk-claude · ctx 50k/200k (25%)");
  assert.ok(!el.classList.contains("hidden"));
});

test("setInfo(null) hides and clears", () => {
  const el = attach();
  el.setInfo(null);
  assert.ok(el.classList.contains("hidden"));
  assert.equal(el.textContent, "");
});

test("setInfo with nothing renderable hides", () => {
  const el = attach();
  el.setInfo({});
  assert.ok(el.classList.contains("hidden"));
});

test("token counts under 1000 are shown verbatim", () => {
  const el = attach();
  el.setInfo({ backend_kind: "x", context_window: 800, context_used: 200 });
  assert.match(el.textContent, /ctx 200\/800 \(25%\)/);
});
