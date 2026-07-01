// chat-controller.js — the live-conversation controller, factored out of app.js.
//
// No build step: app.js is the composition root. It builds a shared `app`
// context (state + the DOM/helpers this controller needs) and calls
// `ChatController(app)`, which attaches its public functions back onto `app`
// (app.openStream / app.handleEvent). Everything the controller needs from the
// outside is read through `app.*`, so this file never touches app.js internals
// directly.
//
// First slice of the app-context split: just the SSE stream (openStream) + its
// event router (handleEvent). openStream can't be unit-tested (real
// EventSource), but handleEvent is a pure dispatcher over injected `app`
// methods and is covered in chat-controller.test.js with a fake app.
(function () {
  "use strict";

  function ChatController(app) {
    const state = app.state;

    function openStream() {
      const url =
        `api/stream?bot=${encodeURIComponent(state.bot)}` +
        `&machine=${encodeURIComponent(state.botMachine)}` +
        `&chat_id=${encodeURIComponent(state.chatId)}` +
        (app.TOKEN ? `&token=${encodeURIComponent(app.TOKEN)}` : "");
      const es = new EventSource(url);
      state.es = es;
      es.onopen = () => app.setConn("online");
      es.onerror = () => {
        app.setConn("offline");
        // EventSource auto-reconnects; don't fight it.
      };
      es.onmessage = (e) => {
        let ev;
        try { ev = JSON.parse(e.data); } catch { return; }
        handleEvent(ev);
      };
    }

    function handleEvent(ev) {
      switch (ev.type) {
        case "message": {
          app.addMessage(ev.role || "assistant", ev.text || "", { id: ev.message_id, ts: ev.ts });
          app.touchSession(ev.text || "");
          break;
        }
        case "typing": app.showTyping(); break;
        case "stream_start": {
          app.removeTyping();
          const el = app.addMessage("assistant", "", { id: ev.message_id, ts: ev.ts });
          state.streamMsgs[ev.message_id] = { el, text: "" };
          break;
        }
        case "stream_delta": {
          const m = state.streamMsgs[ev.message_id];
          if (!m) return;
          m.text = ev.text != null ? ev.text : m.text + (ev.delta || "");
          m.el.setText(m.text);
          app.scrollDown();
          break;
        }
        case "stream_end": {
          const m = state.streamMsgs[ev.message_id];
          if (m) {
            if (ev.text) m.el.setText(ev.text);
            app.touchSession(ev.text || m.text);
            delete state.streamMsgs[ev.message_id];
          }
          // Refresh server-side session list (other platforms may have new turns).
          app.fetchServerSessions(state.botMachine, state.bot).then((list) => {
            state.serverSessions[app.curKey()] = list;
            app.refreshSessionList();
            app.refreshSessionInfo();
          });
          break;
        }
        case "tool_call": {
          app.renderToolCall(ev.tool_id, ev.name || "tool", ev.args || {}, ev.parent_tool_id || "");
          app.scrollDown();
          break;
        }
        case "tool_result": {
          app.renderToolResult(ev.tool_id, !!ev.ok, ev.summary || "", ev.error || "");
          app.scrollDown();
          break;
        }
        case "_close": app.setConn("offline"); break;
      }
    }

    app.openStream = openStream;
    app.handleEvent = handleEvent; // exposed for switchChat is internal; kept public for tests
  }

  window.ChatController = ChatController;
})();
