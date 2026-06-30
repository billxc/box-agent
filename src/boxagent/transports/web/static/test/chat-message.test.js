"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { ChatMessage, makeRoot } = require("./load");

function attach(role, text, opts) {
  const root = makeRoot();
  const el = ChatMessage.create(role, text, opts || {});
  root.appendChild(el); // connect → connectedCallback builds + renders
  return el;
}

test("assistant text renders as markdown (escaped when marked absent)", () => {
  const el = attach("assistant", "<b>hi</b>");
  assert.ok(el.classList.contains("msg"));
  assert.ok(el.classList.contains("assistant"));
  assert.equal(el.querySelector(".markdown").innerHTML, "&lt;b&gt;hi&lt;/b&gt;");
});

test("user text is escaped and newlines become <br> (no markdown)", () => {
  const el = attach("user", "a<b>\nc");
  assert.ok(el.classList.contains("user"));
  assert.equal(el.querySelector(".markdown").innerHTML, "a&lt;b&gt;<br>c");
});

test("setText updates the bubble in place (streaming)", () => {
  const el = attach("assistant", "");
  el.setText("hello world");
  assert.equal(el.querySelector(".markdown").innerHTML, "hello world");
});

test("create stashes message id as data-id", () => {
  const el = attach("assistant", "x", { id: "m1" });
  assert.equal(el.getAttribute("data-id"), "m1");
});

test("a message renders a timestamp element", () => {
  const el = attach("assistant", "x", { ts: 1719000000 });
  const time = el.querySelector("time");
  assert.ok(time && time.textContent.length > 0);
});
