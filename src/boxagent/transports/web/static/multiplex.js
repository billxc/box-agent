// multiplex.js — 一个页面级 WebSocket，承载所有打开 chat 的事件。
//
// 为什么：per-chat SSE 每条各占浏览器约 6 个 HTTP/1.1 连接 slot 之一，
// 开几个 chat 就会卡死整个 UI（且 aiohttp 无 HTTP/2）。本 client 只对
// /api/multiplex 保持一个 WebSocket，用 subscribe/unsubscribe 帧告诉服务端
// 关心哪些 (machine, bot, chat_id)。每个推送事件带 {machine,bot,chat_id,event:{...}} 标签，
// 据此 demux 给对应 chat 的订阅者。
//
// 无构建步骤：app.js 调 MultiplexClient(app)，把返回对象挂到 app.multiplex。
// chat-controller.js 经此边界与它对话，也让两者可单测（controller 拿到 spy
// multiplex；本 client 经 app._makeSocket 拿到 spy WebSocket）。
(function () {
  "use strict";

  function tagKey(machine, bot, chatId) {
    return `${machine}|${bot}|${chatId}`;
  }

  function MultiplexClient(app) {
    // key -> handler(event)。每个 chat 一个 live 订阅。
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

    // EventSource 无法带 header；socket 工厂可注入以便测试。
    function makeSocket(url) {
      return app._makeSocket ? app._makeSocket(url) : new WebSocket(url);
    }

    function sendFrame(frame) {
      if (socket && socket.readyState === 1 /* 已打开 */) {
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
        // （重）连后重新声明订阅；服务端跨 socket 不保存状态。
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
        // onclose 会跟着触发；让它驱动重连。
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

    // 注册对某 chat 的订阅。会替换该 chat 之前的 handler。
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
