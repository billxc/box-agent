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
  "session-data.js",
  "chat-controller.js",
  "machines-controller.js",
  "components/tool-card.js",
  "components/chat-message.js",
  "components/chat-log.js",
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
  get ChatLog() { return globalThis.ChatLog; },
  get RecapBanner() { return globalThis.RecapBanner; },
  get SessionInfo() { return globalThis.SessionInfo; },
  get SessionPicker() { return globalThis.SessionPicker; },
  get RecentsPanel() { return globalThis.RecentsPanel; },
  get MachinesPanel() { return globalThis.MachinesPanel; },
  get SessionsPanel() { return globalThis.SessionsPanel; },
  get escapeHtml() { return globalThis.escapeHtml; },
  get renderMarkdown() { return globalThis.renderMarkdown; },
  get loadSessions() { return globalThis.loadSessions; },
  get saveSessions() { return globalThis.saveSessions; },
  get buildSessionList() { return globalThis.buildSessionList; },
  get defaultTitle() { return globalThis.defaultTitle; },
  get shortId() { return globalThis.shortId; },
  get ChatController() { return globalThis.ChatController; },
  get MachinesController() { return globalThis.MachinesController; },
};
