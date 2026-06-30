"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { escapeHtml, renderMarkdown } = require("./load");

test("escapeHtml escapes HTML special chars", () => {
  assert.equal(escapeHtml(`<a href="x">&'`), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;");
});

test("escapeHtml coerces non-strings", () => {
  assert.equal(escapeHtml(42), "42");
});

test("renderMarkdown falls back to escaping when marked is absent", () => {
  // The stub has no window.marked → renderMarkdown must escape, not inject HTML.
  assert.equal(renderMarkdown("<img src=x onerror=alert(1)>"), "&lt;img src=x onerror=alert(1)&gt;");
});
