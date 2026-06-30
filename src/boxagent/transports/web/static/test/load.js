// Loads util.js + the components into the DOM stub's globals so tests can
// exercise the real component source (IIFEs that register custom elements on
// `window`). Eval'd once; each test uses a fresh `makeRoot()` container.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { install, makeRoot } = require("./dom-stub");

install(); // sets globalThis.window / document / customElements / CSS / HTMLElement

const staticDir = path.join(__dirname, "..");
for (const rel of [
  "util.js",
  "components/tool-card.js",
  "components/chat-message.js",
  "components/recap-banner.js",
  "components/session-info.js",
  "components/session-picker.js",
  "components/recents-panel.js",
  "components/machines-panel.js",
  "components/sessions-panel.js",
]) {
  const code = fs.readFileSync(path.join(staticDir, rel), "utf8");
  vm.runInThisContext(code, { filename: rel });
}

module.exports = {
  makeRoot,
  get ToolCard() { return globalThis.ToolCard; },
  get ChatMessage() { return globalThis.ChatMessage; },
  get RecapBanner() { return globalThis.RecapBanner; },
  get SessionInfo() { return globalThis.SessionInfo; },
  get SessionPicker() { return globalThis.SessionPicker; },
  get RecentsPanel() { return globalThis.RecentsPanel; },
  get MachinesPanel() { return globalThis.MachinesPanel; },
  get SessionsPanel() { return globalThis.SessionsPanel; },
  get escapeHtml() { return globalThis.escapeHtml; },
  get renderMarkdown() { return globalThis.renderMarkdown; },
};
