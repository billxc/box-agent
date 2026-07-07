// chat-controller.js — the live-conversation controller, factored out of app.js.
//
// No build step: app.js is the composition root. It builds a shared `app`
// context (state + the DOM/components/helpers this controller needs) and calls
// `ChatController(app)`, which attaches its public functions back onto `app`
// (app.switchChat / app.sendText / app.addMessage / app.openStream /
// app.handleEvent) and wires app.chatLog.onLoadOlder.
//
// Everything the controller needs from the outside is read through `app.*`, so
// this file never touches app.js internals. That injection is also what makes
// the controller unit-testable: chat-controller.test.js drives it with a fake
// `app` (mock api + spy components + spy app.multiplex) — no real socket/DOM needed.
//
// Stays in app.js (on the `app` bag): state, api, $, TOKEN, HISTORY_PAGE_SIZE,
// curKey, setConn, showRecapBanner, chatTitle, sessionInfoEl, sendBtn, chatLog,
// recents, refreshSessionList, fetchServerSessions, loadMachines.
// Globals from session-data.js: defaultTitle, saveSessions.
(function () {
  "use strict";

  function ChatController(app) {
    const state = app.state;

    // ── Message-area delegators (all display lives in <chat-log>) ──
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

    // ── Session bookkeeping ──

    // Bump the local session meta + cross-bot recents on each turn.
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

    // <session-info> owns rendering + token formatting; we just fetch + hand it
    // the info object (or null).
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

    // ── History ──

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
        app.chatLog.prependHistory(items); // builds bubbles + preserves scroll position
        state.historyOffset += items.length;
        if (state.historyOffset >= state.historyTotal) state.historyExhausted = true;
      } catch (e) {
        console.warn("history load-more failed", e);
      } finally {
        state.historyLoading = false;
      }
    }

    // ── Switch to a chat: title, history swap, stream ──

    async function switchChat(chatId) {
      // Drop the previous chat's multiplex subscription. One page-level socket
      // stays open across chats; switching only adds/removes interest, so we
      // never churn a connection (and never occupy more than one slot).
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

      // Record this open in the cross-bot recents.
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

      // Mask the chat panel so the fetch + swap + scroll-to-bottom are invisible.
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

      app.chatLog.setHistory(history); // replace bubbles + jump to bottom (no smooth)

      requestAnimationFrame(() => mask.classList.add("hidden"));

      openStream();
    }

    // ── Multiplex subscription + event router ──

    // Subscribe the active chat to the page-level multiplex socket. Events for
    // this (machine, bot, chat_id) are demuxed back into handleEvent. Named
    // openStream for continuity with the old per-chat SSE entrypoint.
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
          // Refresh server-side session list (other platforms may have new turns).
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

    // ── Send ──

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
            // Likely the guest dropped — refresh machines so UI reflects it.
            app.loadMachines().catch(() => {});
          }
        }
      } catch (e) {
        addMessage("assistant", "_Network error: " + e.message + "_");
      } finally {
        app.sendBtn.disabled = false;
      }
    }

    // Public surface used by app.js (wiring + boot):
    app.switchChat = switchChat;
    app.sendText = sendText;
    app.addMessage = addMessage;       // boot's "failed to connect" notice
    app.openStream = openStream;       // (also exposed for tests)
    app.handleEvent = handleEvent;     // (exposed for tests)
    app.chatLog.onLoadOlder = loadOlderHistory;
  }

  window.ChatController = ChatController;
})();
