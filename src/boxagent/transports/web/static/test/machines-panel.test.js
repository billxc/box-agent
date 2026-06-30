"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot } = require("./load");

function attach() {
  localStorage.clear(); // collapse state persists in the shared stub store
  const root = makeRoot();
  const el = document.createElement("machines-panel");
  root.appendChild(el);
  return el;
}

const M = (id, over = {}) => ({ machine_id: id, online: true, bots: [], ...over });

test("connectedCallback builds inner ul#machine-list", () => {
  const el = attach();
  assert.ok(el.querySelector("ul"));
  assert.equal(el._list.id, "machine-list");
});

test("empty render shows the placeholder", () => {
  const el = attach();
  el.render([], {});
  assert.match(el._list.innerHTML, /No machines/);
});

test("render builds one li per machine; offline marked", () => {
  const el = attach();
  el.render([M("m1"), M("m2", { online: false, last_seen: 0 })], {});
  assert.equal(el._list.children.length, 2);
  assert.ok(el.querySelector(".offline"), "offline machine marked");
});

test("active bot marked from context", () => {
  const el = attach();
  el.render([M("m1", { bots: [{ name: "b1" }, { name: "b2" }] })], { bot: "b2", botMachine: "m1" });
  assert.ok(el.querySelector(".active"));
});

test("collapse read from localStorage; toggle persists + re-renders", () => {
  localStorage.clear();
  localStorage.setItem("ba.collapsedMachines", JSON.stringify(["m1"]));
  const root = makeRoot();
  const el = document.createElement("machines-panel");
  root.appendChild(el);
  el.render([M("m1")], {});
  assert.ok(el.querySelector(".collapsed"), "m1 starts collapsed from storage");
  el._toggle("m1"); // expand
  assert.deepEqual(JSON.parse(localStorage.getItem("ba.collapsedMachines")), []);
  assert.ok(!el.querySelector(".collapsed"), "no longer collapsed after toggle");
});
