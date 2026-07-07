// multiplex.js — one page-level WebSocket that carries every open chat's events.
//
// Why: a per-chat SSE stream burns one of the browser's ~6 HTTP/1.1 connection
// slots each, so a few open chats stall the whole UI (and aiohttp has no HTTP/2).
// This client holds a single WebSocket to /api/multiplex and tells the server
// which (machine, bot, chat_id) chats it cares about via subscribe/unsubscribe
// frames. Each pushed event is tagged {machine,bot,chat_id,event:{...}}, so we
// demux it to the subscriber registered for that chat.
//
// No build step: app.js calls MultiplexClient(app) and hangs the returned object
// on app.multiplex. chat-controller.js talks to it through that boundary, which
// also makes both unit-testable (the controller gets a spy multiplex; this
// client gets a spy WebSocket via app._makeSocket).
(function () {
  "use strict";

  function tagKey(machine, bot, chatId) {
    return `${machine}|${bot}|${chatId}`;
  }

  function MultiplexClient(app) {
    // key -> handler(event). One live subscription per chat.
    const handlers = new Map();
    let socket = null;
    let reconnectDelay = 1000;
    let reconnectTimer = null;
    let closedByUs = false;

    function socketUrl() {
      const base = new URL("api/multiplex", location.href);
      base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
      if (app.TOKEN) base.searchParams.set("token", app.TOKEN);
      return base.toString();
    }

    // EventSource has no header path; the socket factory is injectable for tests.
    function makeSocket(url) {
      return app._makeSocket ? app._makeSocket(url) : new WebSocket(url);
    }

    function sendFrame(frame) {
      if (socket && socket.readyState === 1 /* OPEN */) {
        socket.send(JSON.stringify(frame));
        return true;
      }
      return false;
    }

    function resubscribeAll() {
      for (const key of handlers.keys()) {
        const [machine, bot, chatId] = key.split("|");
        sendFrame({ type: "subscribe", machine, bot, chat_id: chatId });
      }
    }

    function connect() {
      if (socket) return;
      closedByUs = false;
      const ws = makeSocket(socketUrl());
      socket = ws;
      ws.onopen = () => {
        reconnectDelay = 1000;
        app.setConn && app.setConn("online");
        // Re-declare interest after a (re)connect; server holds no state across sockets.
        resubscribeAll();
      };
      ws.onmessage = (e) => {
        let frame;
        try { frame = JSON.parse(e.data); } catch { return; }
        const key = tagKey(frame.machine, frame.bot, frame.chat_id);
        const handler = handlers.get(key);
        if (handler && frame.event) handler(frame.event);
      };
      ws.onclose = () => {
        socket = null;
        if (closedByUs) return;
        app.setConn && app.setConn("offline");
        scheduleReconnect();
      };
      ws.onerror = () => {
        // onclose follows; let it drive reconnect.
        try { ws.close(); } catch (_) {}
      };
    }

    function scheduleReconnect() {
      if (reconnectTimer) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }

    // Register interest in a chat. Replaces any prior handler for the same chat.
    function subscribe(machine, bot, chatId, handler) {
      if (!machine || !bot || !chatId) return;
      const key = tagKey(machine, bot, chatId);
      const isNew = !handlers.has(key);
      handlers.set(key, handler);
      connect();
      if (isNew) sendFrame({ type: "subscribe", machine, bot, chat_id: chatId });
    }

    function unsubscribe(machine, bot, chatId) {
      if (!machine || !bot || !chatId) return;
      const key = tagKey(machine, bot, chatId);
      if (!handlers.delete(key)) return;
      sendFrame({ type: "unsubscribe", machine, bot, chat_id: chatId });
    }

    function close() {
      closedByUs = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      handlers.clear();
      if (socket) { try { socket.close(); } catch (_) {} socket = null; }
    }

    const client = { subscribe, unsubscribe, close, _handlers: handlers };
    app.multiplex = client;
    return client;
  }

  window.MultiplexClient = MultiplexClient;
})();
