"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot } = require("./load");

function attach() {
  const root = makeRoot();
  const el = document.createElement("sessions-panel");
  root.appendChild(el);
  return el;
}

const S = (chat_id, over = {}) => ({ chat_id, platform: "web", title: "T-" + chat_id, preview: "p", ...over });

test("connectedCallback builds inner ul#session-list", () => {
  const el = attach();
  assert.ok(el.querySelector("ul"));
  assert.equal(el._list.id, "session-list");
});

test("empty render shows the placeholder", () => {
  const el = attach();
  el.render([], {});
  assert.equal(el._list.children[0].textContent, "No sessions yet — start chatting");
});

test("render builds a li per entry; active from chatId", () => {
  const el = attach();
  el.render([S("a"), S("b")], { chatId: "b" });
  assert.equal(el._list.children.length, 2);
  assert.ok(el.querySelector(".active"));
});

test("recap row rendered only when present", () => {
  const el = attach();
  el.render([S("a", { recap: "did the thing" })], {});
  assert.ok(el.querySelector(".session-recap"));
  el.render([S("a")], {});
  assert.ok(!el.querySelector(".session-recap"));
});

test("showLoading shows the loading placeholder", () => {
  const el = attach();
  el.showLoading();
  assert.match(el._list.innerHTML, /Loading sessions/);
});
