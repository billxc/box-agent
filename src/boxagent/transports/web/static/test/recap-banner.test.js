"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { makeRoot } = require("./load");

function attach() {
  const root = makeRoot();
  const banner = document.createElement("recap-banner");
  root.appendChild(banner); // connect → builds icon/text/close
  return banner;
}

test("show renders recap text and reveals the banner", () => {
  const banner = attach();
  banner.show("Session summary", "k1");
  assert.equal(banner.querySelector(".recap-text").textContent, "Session summary");
  assert.ok(!banner.classList.contains("hidden"));
  assert.ok(banner.classList.contains("collapsed"));
});

test("empty recap hides the banner", () => {
  const banner = attach();
  banner.show("", "k2");
  assert.ok(banner.classList.contains("hidden"));
});

test("a previously dismissed recap stays hidden", () => {
  const banner = attach();
  localStorage.setItem("k3", "Recap X");
  banner.show("Recap X", "k3");
  assert.ok(banner.classList.contains("hidden"));
});

test("clicking close persists the dismissal and hides", () => {
  const banner = attach();
  banner.show("Recap Y", "k4");
  banner.querySelector(".recap-close").onclick();
  assert.equal(localStorage.getItem("k4"), "Recap Y");
  assert.ok(banner.classList.contains("hidden"));
});

test("clicking the text toggles collapse", () => {
  const banner = attach();
  banner.show("Recap Z", "k5");
  banner.querySelector(".recap-text").onclick();
  assert.ok(!banner.classList.contains("collapsed"));
});
