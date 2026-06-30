"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot, SessionPicker } = require("./load");

function attach() {
  const root = makeRoot();
  const el = document.createElement("session-picker");
  root.appendChild(el); // connected → connectedCallback builds the modal
  return el;
}

test("connectedCallback builds the modal skeleton (light DOM)", () => {
  const el = attach();
  assert.ok(el.querySelector(".picker"), "overlay .picker built");
  assert.ok(el.querySelector(".picker-card"), "card built");
  assert.ok(el.querySelector(".picker-list"), "a project/session list built");
  assert.ok(el.querySelector(".picker-preview"), "transcript preview built");
  assert.ok(el.querySelector(".primary-btn"), "resume button built");
  // Modal starts hidden + resume disabled.
  assert.ok(el._overlay.classList.contains("hidden"));
  assert.equal(el._resume.disabled, true);
});

test("open() shows the modal, close() hides + resets", () => {
  const el = attach();
  // Fake the injected collaborators so open()'s project fetch resolves empty.
  el.api = async () => ({ ok: true, json: async () => ({ projects: [], total: 0, has_more: false }) });
  el.getContext = () => ({ machine: "m1", bot: "b1" });

  el.open();
  assert.ok(!el._overlay.classList.contains("hidden"), "open() reveals overlay");

  el.close();
  assert.ok(el._overlay.classList.contains("hidden"), "close() hides overlay");
  assert.equal(el._state.project, null, "close() resets browse state");
});

test("formatTs: falsy → empty, real ts → non-empty (locale-agnostic)", () => {
  assert.equal(SessionPicker.formatTs(0), "");
  assert.equal(SessionPicker.formatTs(undefined), "");
  assert.match(SessionPicker.formatTs(1_700_000_000), /\S/); // some rendered string
});

test("renderSessionItem escapes the title and shows count + short id", () => {
  const el = attach();
  const li = el.renderSessionItem({
    first_user: "<b>hello</b>",
    last_ts: 1_700_000_000,
    session_id: "abcdef1234567890",
    message_count: 7,
  });
  assert.match(li.innerHTML, /&lt;b&gt;hello&lt;\/b&gt;/, "title HTML-escaped");
  assert.match(li.innerHTML, /💬 7/, "message count shown");
  assert.match(li.innerHTML, /abcdef12/, "session id truncated to 8 chars");
  assert.ok(!li.innerHTML.includes("<b>hello</b>"), "no raw HTML injected");
});

test("renderSessionItem falls back to a placeholder for empty user text", () => {
  const el = attach();
  const li = el.renderSessionItem({ first_user: "", last_ts: 0, session_id: "x", message_count: 0 });
  assert.match(li.innerHTML, /\(no user message\)/);
});
