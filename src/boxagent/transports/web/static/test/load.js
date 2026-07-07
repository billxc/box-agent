// 把 util.js + 各组件加载进 DOM stub 的全局，让测试能跑真实组件源码
//（在 `window` 上注册自定义元素的 IIFE）。只 eval 一次；
// 每个测试用一个新的 `makeRoot()` 容器。

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { install, makeRoot } = require("./dom-stub");

install(); // 设置 globalThis.window / document / customElements / CSS / HTMLElement

const staticDir = path.join(__dirname, "..");
for (const rel of [
  "util.js",
  "session-data.js",
  "multiplex.js",
  "chat-controller.js",
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
  get MultiplexClient() { return globalThis.MultiplexClient; },
};
