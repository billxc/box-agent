// chat-controller.js — 从 app.js 拆出的实时会话 controller。
//
// 无构建步骤：app.js 是装配根。它构造共享的 `app` 上下文
//（state + 本 controller 需要的 DOM/组件/辅助函数）并调用
// `ChatController(app)`，后者把自己的公开函数挂回 `app`
//（app.switchChat / app.sendText / app.addMessage / app.openStream /
// app.handleEvent），并接线 app.chatLog.onLoadOlder。
//
// controller 对外的一切都经 `app.*` 读取，因此本文件从不碰 app.js 内部。
// 这种注入也让 controller 可单测：chat-controller.test.js 用假 `app`
//（mock api + spy 组件 + spy app.multiplex）驱动它，无需真的 socket/DOM。
//
// 仍留在 app.js（挂在 `app` bag 上）：state, api, $, TOKEN, HISTORY_PAGE_SIZE,
// curKey, setConn, showRecapBanner, chatTitle, sessionInfoEl, sendBtn, chatLog,
// recents, refreshSessionList, fetchServerSessions, loadMachines。
// 来自 session-data.js 的全局：defaultTitle, saveSessions。
(function () {
  "use strict";

  function ChatController(app) {
    const state = app.state;

    // ── 消息区代理（所有显示都在 <chat-log>）──
    function scrollDown() { app.chatLog.scrollToBottom(); }
    function addMessage(role, text, opts = {}) { return app.chatLog.addMessage(role, text, opts); }
    function showTyping() { app.chatLog.showTyping(); }
    function removeTyping() { app.chatLog.removeTyping(); }
    function renderToolCall(toolId, name, args, parentToolId = "") {
      app.chatLog.upsertToolCall(toolId, name, args, parentToolId);
    }
    function renderToolResult(toolId, ok, summary, error) {
      app.chatLog.applyToolResult(toolId, ok, summary, error);
    }

    // ── 会话记账 ──

    // 每轮对话更新本地 session 元信息 + 跨 bot 最近记录。
    function touchSession(preview) {
      const key = app.curKey();
      const sessions = state.sessions[key] || {};
      const current = sessions[state.chatId] || { title: "Chat " + new Date().toLocaleString() };
      current.preview = preview.slice(0, 60);
      current.ts = Date.now();
      sessions[state.chatId] = current;
      state.sessions[key] = sessions;
      saveSessions(state.botMachine, state.bot, sessions);
      app.refreshSessionList();
      app.chatTitle.textContent = current.title;
      const botInfo = (state.machines || [])
        .find((m) => m.machine_id === state.botMachine)?.bots
        ?.find((b) => b.name === state.bot);
      app.recents.touch({
        machine: state.botMachine,
        bot: state.bot,
        chat_id: state.chatId,
        title: current.title,
        preview: current.preview,
        platform: state.chatId.startsWith("web-") ? "web" : "other",
        display_name: botInfo?.display_name || state.bot,
      });
    }

    // <session-info> 负责渲染 + token 格式化；这里只 fetch 后把 info 对象
    // （或 null）交给它。
    async function refreshSessionInfo() {
      if (!state.botMachine) { app.sessionInfoEl.setInfo(null); return; }
      const serverList = state.serverSessions[app.curKey()] || [];
      const serverMeta = serverList.find((s) => s.chat_id === state.chatId);
      const sessionId = serverMeta && serverMeta.session_id;
      if (!sessionId) { app.sessionInfoEl.setInfo(null); return; }
      const botInfo = (state.machines || [])
        .find((m) => m.machine_id === state.botMachine)?.bots
        ?.find((b) => b.name === state.bot);
      const backendKind = botInfo?.backend || "";
      const model = botInfo?.model || "";
      if (!backendKind) { app.sessionInfoEl.setInfo(null); return; }
      try {
        const r = await app.api(
          `session_info?session_id=${encodeURIComponent(sessionId)}` +
          `&backend_kind=${encodeURIComponent(backendKind)}` +
          `&machine=${encodeURIComponent(state.botMachine)}` +
          `&model=${encodeURIComponent(model)}`
        );
        if (!r.ok) { app.sessionInfoEl.setInfo(null); return; }
        const data = await r.json();
        app.sessionInfoEl.setInfo(data.info || null);
      } catch (_) {
        app.sessionInfoEl.setInfo(null);
      }
    }

    // ── 历史 ──

    async function loadOlderHistory() {
      if (state.historyLoading || state.historyExhausted) return;
      if (!state.chatId || !state.bot) return;
      state.historyLoading = true;
      try {
        const url = `history?bot=${encodeURIComponent(state.bot)}&machine=${encodeURIComponent(state.botMachine)}&chat_id=${encodeURIComponent(state.chatId)}&limit=${app.HISTORY_PAGE_SIZE}&offset=${state.historyOffset}`;
        const r = await app.api(url);
        if (!r.ok) return;
        const j = await r.json();
        const items = j.history || [];
        state.historyTotal = j.total || state.historyTotal;
        if (!items.length) { state.historyExhausted = true; return; }
        app.chatLog.prependHistory(items); // 构建气泡 + 保持滚动位置
        state.historyOffset += items.length;
        if (state.historyOffset >= state.historyTotal) state.historyExhausted = true;
      } catch (e) {
        console.warn("history load-more failed", e);
      } finally {
        state.historyLoading = false;
      }
    }

    // ── 切换 chat：标题、历史替换、stream ──

    async function switchChat(chatId) {
      // 丢掉上一个 chat 的 multiplex 订阅。一个页面级 socket 跨 chat 常驻，
      // 切换只增删订阅，从不 churn 连接（也从不占用超过一个 slot）。
      if (state.subscribed) {
        app.multiplex.unsubscribe(state.subscribed.machine, state.subscribed.bot, state.subscribed.chat_id);
        state.subscribed = null;
      }
      state.chatId = chatId;
      state.streamMsgs = {};
      app.refreshSessionList();
      const meta = (state.sessions[app.curKey()] || {})[chatId] || {};
      const serverList = state.serverSessions[app.curKey()] || [];
      const serverMeta = serverList.find((s) => s.chat_id === chatId);
      const backendTitle = serverMeta && (serverMeta.custom_title || serverMeta.summary);
      const resolvedTitle = backendTitle || meta.title || (serverMeta ? defaultTitle(serverMeta) : chatId);
      app.chatTitle.textContent = resolvedTitle;
      refreshSessionInfo();
      localStorage.setItem("ba.last." + app.curKey(), chatId);

      // 记录本次打开到跨 bot 最近记录。
      const botInfo = (state.machines || [])
        .find((m) => m.machine_id === state.botMachine)?.bots
        ?.find((b) => b.name === state.bot);
      app.recents.touch({
        machine: state.botMachine,
        bot: state.bot,
        chat_id: chatId,
        title: resolvedTitle,
        preview: serverMeta?.preview || meta.preview || "",
        recap: serverMeta?.recap || "",
        platform: serverMeta?.platform || (chatId.startsWith("web-") ? "web" : "other"),
        display_name: botInfo?.display_name || state.bot,
        ts: serverMeta?.last_ts || Math.floor(Date.now() / 1000),
      });

      app.showRecapBanner(serverMeta?.recap || "", chatId);

      // 遮住 chat 面板，让 fetch + swap + 滚到底的过程不可见。
      const mask = app.$("messages-mask");
      mask.classList.remove("hidden");

      app.setConn("connecting");
      state.historyOffset = 0;
      state.historyTotal = 0;
      state.historyExhausted = false;
      state.historyLoading = false;
      let history = [];
      try {
        const r = await app.api(`history?bot=${encodeURIComponent(state.bot)}&machine=${encodeURIComponent(state.botMachine)}&chat_id=${encodeURIComponent(chatId)}&limit=${app.HISTORY_PAGE_SIZE}&offset=0`);
        if (r.ok) {
          const j = await r.json();
          history = j.history || [];
          state.historyTotal = j.total || history.length;
          state.historyOffset = history.length;
          if (state.historyOffset >= state.historyTotal) state.historyExhausted = true;
        }
      } catch (e) { console.warn("history load failed", e); }

      app.chatLog.setHistory(history); // 替换气泡 + 跳到底部（无平滑动画）

      requestAnimationFrame(() => mask.classList.add("hidden"));

      openStream();
    }

    // ── Multiplex 订阅 + 事件路由 ──

    // 把当前 chat 订阅到页面级 multiplex socket。属于该 (machine, bot, chat_id)
    // 的事件被 demux 回 handleEvent。沿用旧的 per-chat SSE 入口名 openStream。
    function openStream() {
      const machine = state.botMachine;
      const bot = state.bot;
      const chatId = state.chatId;
      state.subscribed = { machine, bot, chat_id: chatId };
      app.multiplex.subscribe(machine, bot, chatId, handleEvent);
    }

    function handleEvent(ev) {
      switch (ev.type) {
        case "message": {
          addMessage(ev.role || "assistant", ev.text || "", { id: ev.message_id, ts: ev.ts });
          touchSession(ev.text || "");
          break;
        }
        case "typing": showTyping(); break;
        case "stream_start": {
          removeTyping();
          const el = addMessage("assistant", "", { id: ev.message_id, ts: ev.ts });
          state.streamMsgs[ev.message_id] = { el, text: "" };
          break;
        }
        case "stream_delta": {
          const m = state.streamMsgs[ev.message_id];
          if (!m) return;
          m.text = ev.text != null ? ev.text : m.text + (ev.delta || "");
          m.el.setText(m.text);
          scrollDown();
          break;
        }
        case "stream_end": {
          const m = state.streamMsgs[ev.message_id];
          if (m) {
            if (ev.text) m.el.setText(ev.text);
            touchSession(ev.text || m.text);
            delete state.streamMsgs[ev.message_id];
          }
          // 刷新服务端 session 列表（其他平台可能有新轮次）。
          app.fetchServerSessions(state.botMachine, state.bot).then((list) => {
            state.serverSessions[app.curKey()] = list;
            app.refreshSessionList();
            refreshSessionInfo();
          });
          break;
        }
        case "tool_call": {
          renderToolCall(ev.tool_id, ev.name || "tool", ev.args || {}, ev.parent_tool_id || "");
          scrollDown();
          break;
        }
        case "tool_result": {
          renderToolResult(ev.tool_id, !!ev.ok, ev.summary || "", ev.error || "");
          scrollDown();
          break;
        }
        case "_close": app.setConn("offline"); break;
      }
    }

    // ── 发送 ──

    async function sendText(text) {
      if (!text.trim()) return;
      if (!state.bot || !state.chatId) return;
      const m = state.machines.find((x) => x.machine_id === state.botMachine);
      if (m && !m.online) {
        addMessage("assistant", `_Machine **${state.botMachine}** is offline; can't send._`);
        return;
      }
      app.sendBtn.disabled = true;
      try {
        const r = await app.api("send", {
          method: "POST",
          body: JSON.stringify({ bot: state.bot, machine: state.botMachine, chat_id: state.chatId, text }),
        });
        if (!r.ok) {
          const err = await r.text();
          addMessage("assistant", `_Error (${r.status}): ${err}_`);
          if (r.status === 502 || r.status === 504) {
            // guest 大概率掉线了 — 刷新 machines 让 UI 反映。
            app.loadMachines().catch(() => {});
          }
        }
      } catch (e) {
        addMessage("assistant", "_Network error: " + e.message + "_");
      } finally {
        app.sendBtn.disabled = false;
      }
    }

    // app.js（接线 + 启动）使用的公开接口：
    app.switchChat = switchChat;
    app.sendText = sendText;
    app.addMessage = addMessage;       // 启动时 "failed to connect" 提示
    app.openStream = openStream;       // （也暴露给测试）
    app.handleEvent = handleEvent;     // （暴露给测试）
    app.chatLog.onLoadOlder = loadOlderHistory;
  }

  window.ChatController = ChatController;
})();
