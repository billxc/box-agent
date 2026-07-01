/* BoxAgent Web Chat — vanilla JS, no build step */
(() => {
  const params = new URLSearchParams(location.search);
  const TOKEN = params.get("token") || localStorage.getItem("ba.token") || "";
  if (params.get("token")) localStorage.setItem("ba.token", TOKEN);

  const $ = (id) => document.getElementById(id);
  const chatLog = $("messages");
  const machinesPanel = $("machines");
  const sessionsPanel = $("sessions");
  const sessionsOf = $("sessions-of");
  const chatTitle = $("chat-title");
  const sessionInfoEl = $("session-info");
  const composer = $("composer");
  const input = $("input");
  const sendBtn = $("send");
  const connDot = $("conn-state");
  const connLabel = $("conn-label");
  const sidebar = $("sidebar");
  const recapBanner = $("recap-banner");

  // Per-session dismissed-recap memory (browser-local). When user closes the
  // banner we don't show it again for that chat unless the recap text changes.
  // <recap-banner> (components/recap-banner.js) owns the dismiss / collapse /
  // persist behaviour; we just supply the recap text + a key scoped to the
  // current machine|bot|chat (app state).
  function showRecapBanner(recap, chatId) {
    recapBanner.show(recap, "ba.recap-dismissed." + curKey() + "|" + chatId);
  }

  const state = {
    machines: [],         // [{machine_id, online, role, self, bots, last_seen}]
    bot: null,            // selected bot name
    botMachine: null,     // selected bot's machine_id (for display)
    sessions: {},         // "machine|bot" -> {chat_id: {title, preview, ts}}  (local browser-side)
    serverSessions: {},   // "machine|bot" -> [{chat_id, platform, preview, last_ts, ...}]
    chatId: null,
    es: null,
    streamMsgs: {},
    refreshTimer: null,
    historyOffset: 0,     // how many items already loaded from end of history
    historyTotal: 0,      // total items reported by server
    historyLoading: false,
    historyExhausted: false,
  };
  const HISTORY_PAGE_SIZE = 50;

  // ── Helpers ──
  function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
    if (opts.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    return fetch("api/" + path, { ...opts, headers });
  }

  function uuid() {
    return "web-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  }

  function botKey(machine, bot) { return `${machine}|${bot}`; }
  function curKey() { return botKey(state.botMachine, state.bot); }

  // ── Recents (cross-bot, browser-local) — component: components/recents-panel.js
  // The <recents-panel> element owns the localStorage data + list render. We
  // inject the current selection (for active/offline marking) and handle its
  // "open" via the navigation below.
  const recents = $("recents");
  recents.getContext = () => ({
    machines: state.machines,
    bot: state.bot,
    botMachine: state.botMachine,
    chatId: state.chatId,
  });
  recents.onOpen = openRecent;

  // Open a recent: switch bot+machine if needed, then open the chat.
  // Falls back to a friendly alert if the target machine is offline.
  async function openRecent(r) {
    const m = state.machines.find(x => x.machine_id === r.machine);
    if (m && !m.online) {
      alert(`${r.machine} is offline`);
      return;
    }
    if (state.bot !== r.bot || state.botMachine !== r.machine) {
      // selectBot will load sessions and open last-opened chat; we override
      // by setting ba.last so it lands on the recent chat instead.
      localStorage.setItem("ba.last." + botKey(r.machine, r.bot), r.chat_id);
      await selectBot(r.bot, r.machine);
    } else if (state.chatId !== r.chat_id) {
      await switchChat(r.chat_id);
      closeSidebar();
    } else {
      closeSidebar();
    }
  }

  // loadSessions / saveSessions / buildSessionList / defaultTitle / shortId
  // live in session-data.js (pure helpers, exposed as globals).

  async function fetchServerSessions(machine, bot) {
    try {
      const r = await api(`sessions?bot=${encodeURIComponent(bot)}&machine=${encodeURIComponent(machine)}`);
      if (!r.ok) return [];
      const { sessions } = await r.json();
      return sessions || [];
    } catch { return []; }
  }

  function setConn(state_) {
    connDot.className = "dot " + state_;
    connLabel.textContent = state_;
  }

  // Message area is the <chat-log> custom element (components/chat-log.js).
  // These stay as thin delegators so the controller's call sites don't move;
  // addMessage returns the bubble element so the stream can write deltas into it.
  function scrollDown() { chatLog.scrollToBottom(); }
  function addMessage(role, text, opts = {}) { return chatLog.addMessage(role, text, opts); }
  function showTyping() { chatLog.showTyping(); }
  function removeTyping() { chatLog.removeTyping(); }

  // ── Sessions ──
  // buildSessionList(local, server) / defaultTitle / shortId live in
  // session-data.js. Resolve the current bot's local+server maps into the
  // merged, sorted entry list.
  function currentSessionEntries() {
    return buildSessionList(state.sessions[curKey()] || {}, state.serverSessions[curKey()] || []);
  }

  function refreshSessionList() {
    // <sessions-panel> renders; app.js owns the server+local merge.
    sessionsPanel.render(currentSessionEntries(), { chatId: state.chatId });
  }

  async function restartMachine(machineId, online) {
    if (!online) { alert(`${machineId} is offline; nothing to restart`); return; }
    if (!confirm(`Restart ${machineId}? Supervisor (easy-service) will relaunch.`)) return;
    try {
      const r = await api("admin/cluster_restart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machines: [machineId], include_self: true }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      const info = (data.results || {})[machineId];
      alert(`${machineId}: ${JSON.stringify(info || data)}`);
    } catch (e) {
      alert(`Restart failed: ${e.message || e}`);
    }
  }

  async function restartCluster() {
    if (!confirm("Restart all guest nodes? Host stays up.\n(Add include_self=1 manually to also restart host.)")) return;
    try {
      const r = await api("admin/cluster_restart", { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      const lines = ["Cluster restart scheduled:"];
      for (const [machine_id, info] of Object.entries(data.results || {})) {
        lines.push(`  ${machine_id}: ${JSON.stringify(info)}`);
      }
      alert(lines.join("\n"));
    } catch (e) {
      alert(`Cluster restart failed: ${e.message || e}`);
    }
  }

  function touchSession(preview) {
    const key = curKey();
    const sessions = state.sessions[key] || {};
    const current = sessions[state.chatId] || { title: "Chat " + new Date().toLocaleString() };
    current.preview = preview.slice(0, 60);
    current.ts = Date.now();
    sessions[state.chatId] = current;
    state.sessions[key] = sessions;
    saveSessions(state.botMachine, state.bot, sessions);
    refreshSessionList();
    chatTitle.textContent = current.title;
    // Bump global recents too — keeps the cross-bot Recent panel fresh.
    const botInfo = (state.machines || [])
      .find(m => m.machine_id === state.botMachine)?.bots
      ?.find(b => b.name === state.bot);
    recents.touch({
      machine: state.botMachine,
      bot: state.bot,
      chat_id: state.chatId,
      title: current.title,
      preview: current.preview,
      platform: state.chatId.startsWith("web-") ? "web" : "other",
      display_name: botInfo?.display_name || state.bot,
    });
  }

  // ── Chat lifecycle ──

  // <session-info> (components/session-info.js) owns rendering + token
  // formatting; refreshSessionInfo just fetches and hands it the info object.
  async function refreshSessionInfo() {
    if (!state.botMachine) { sessionInfoEl.setInfo(null); return; }
    const serverList = state.serverSessions[curKey()] || [];
    const serverMeta = serverList.find(s => s.chat_id === state.chatId);
    const sessionId = serverMeta && serverMeta.session_id;
    if (!sessionId) { sessionInfoEl.setInfo(null); return; }
    const botInfo = (state.machines || [])
      .find(m => m.machine_id === state.botMachine)?.bots
      ?.find(b => b.name === state.bot);
    const backendKind = botInfo?.backend || "";
    const model = botInfo?.model || "";
    if (!backendKind) { sessionInfoEl.setInfo(null); return; }
    try {
      const r = await api(
        `session_info?session_id=${encodeURIComponent(sessionId)}` +
        `&backend_kind=${encodeURIComponent(backendKind)}` +
        `&machine=${encodeURIComponent(state.botMachine)}` +
        `&model=${encodeURIComponent(model)}`
      );
      if (!r.ok) { sessionInfoEl.setInfo(null); return; }
      const data = await r.json();
      sessionInfoEl.setInfo(data.info || null);
    } catch (_) {
      sessionInfoEl.setInfo(null);
    }
  }

  async function loadOlderHistory() {
    if (state.historyLoading || state.historyExhausted) return;
    if (!state.chatId || !state.bot) return;
    state.historyLoading = true;
    try {
      const url = `history?bot=${encodeURIComponent(state.bot)}&machine=${encodeURIComponent(state.botMachine)}&chat_id=${encodeURIComponent(state.chatId)}&limit=${HISTORY_PAGE_SIZE}&offset=${state.historyOffset}`;
      const r = await api(url);
      if (!r.ok) return;
      const j = await r.json();
      const items = j.history || [];
      state.historyTotal = j.total || state.historyTotal;
      if (!items.length) { state.historyExhausted = true; return; }
      chatLog.prependHistory(items); // builds bubbles + preserves scroll position
      state.historyOffset += items.length;
      if (state.historyOffset >= state.historyTotal) state.historyExhausted = true;
    } catch (e) {
      console.warn("history load-more failed", e);
    } finally {
      state.historyLoading = false;
    }
  }

  // <chat-log> calls this when the user scrolls near the top; we fetch more.
  chatLog.onLoadOlder = loadOlderHistory;

  async function switchChat(chatId) {
    if (state.es) { state.es.close(); state.es = null; }
    state.chatId = chatId;
    state.streamMsgs = {};
    refreshSessionList();
    const meta = (state.sessions[curKey()] || {})[chatId] || {};
    const serverList = state.serverSessions[curKey()] || [];
    const serverMeta = serverList.find(s => s.chat_id === chatId);
    const backendTitle = serverMeta && (serverMeta.custom_title || serverMeta.summary);
    const resolvedTitle = backendTitle || meta.title || (serverMeta ? defaultTitle(serverMeta) : chatId);
    chatTitle.textContent = resolvedTitle;
    refreshSessionInfo();
    localStorage.setItem("ba.last." + curKey(), chatId);

    // Record this open in the cross-bot recents so the next visit can find
    // it from the top-level Recent panel.
    const botInfo = (state.machines || [])
      .find(m => m.machine_id === state.botMachine)?.bots
      ?.find(b => b.name === state.bot);
    recents.touch({
      machine: state.botMachine,
      bot: state.bot,
      chat_id: chatId,
      title: resolvedTitle,
      preview: serverMeta?.preview || meta.preview || "",
      recap: serverMeta?.recap || "",
      platform: serverMeta?.platform
        || (chatId.startsWith("web-") ? "web" : "other"),
      display_name: botInfo?.display_name || state.bot,
      ts: serverMeta?.last_ts || Math.floor(Date.now() / 1000),
    });

    showRecapBanner(serverMeta?.recap || "", chatId);

    // Cover the chat panel with a mask so the fetch + swap + scroll-to-bottom
    // all happen invisibly. Old content stays in the DOM behind the mask.
    const mask = $("messages-mask");
    mask.classList.remove("hidden");

    setConn("connecting");
    state.historyOffset = 0;
    state.historyTotal = 0;
    state.historyExhausted = false;
    state.historyLoading = false;
    let history = [];
    try {
      const r = await api(`history?bot=${encodeURIComponent(state.bot)}&machine=${encodeURIComponent(state.botMachine)}&chat_id=${encodeURIComponent(chatId)}&limit=${HISTORY_PAGE_SIZE}&offset=0`);
      if (r.ok) {
        const j = await r.json();
        history = j.history || [];
        state.historyTotal = j.total || history.length;
        state.historyOffset = history.length;
        if (state.historyOffset >= state.historyTotal) state.historyExhausted = true;
      }
    } catch (e) { console.warn("history load failed", e); }

    chatLog.setHistory(history); // replace bubbles + jump to bottom (no smooth)

    requestAnimationFrame(() => mask.classList.add("hidden"));

    app.openStream();
  }

  // openStream + handleEvent live in chat-controller.js (ChatController(app),
  // wired near the bottom of this file). switchChat calls app.openStream().

  // ── Tool call rendering ──
  // <chat-log> wraps <tool-card>'s find / create / dedup / result over the live
  // message container.
  function renderToolCall(toolId, name, args, parentToolId = "") {
    chatLog.upsertToolCall(toolId, name, args, parentToolId);
  }

  function renderToolResult(toolId, ok, summary, error) {
    chatLog.applyToolResult(toolId, ok, summary, error);
  }

  // ── Send ──
  async function sendText(text) {
    if (!text.trim()) return;
    if (!state.bot || !state.chatId) return;
    const m = state.machines.find(m => m.machine_id === state.botMachine);
    if (m && !m.online) {
      addMessage("assistant", `_Machine **${state.botMachine}** is offline; can't send._`);
      return;
    }
    sendBtn.disabled = true;
    try {
      const r = await api("send", {
        method: "POST",
        body: JSON.stringify({ bot: state.bot, machine: state.botMachine, chat_id: state.chatId, text }),
      });
      if (!r.ok) {
        const err = await r.text();
        addMessage("assistant", `_Error (${r.status}): ${err}_`);
        if (r.status === 502 || r.status === 504) {
          // Likely the guest dropped — refresh machines so UI reflects it.
          loadMachines().catch(() => {});
        }
      }
    } catch (e) {
      addMessage("assistant", "_Network error: " + e.message + "_");
    } finally {
      sendBtn.disabled = false;
    }
  }

  // ── Machines + bot grouping ──
  async function loadMachines() {
    const r = await api("machines");
    if (!r.ok) {
      if (r.status === 401) {
        const t = prompt("Enter access token:");
        if (t) { localStorage.setItem("ba.token", t); location.reload(); }
        return;
      }
      throw new Error("machines fetch " + r.status);
    }
    const { machines } = await r.json();
    state.machines = machines;
    renderMachines();
    recents.render();
    // Auto-pick a default bot on first load
    if (!state.bot) {
      const lastBot = localStorage.getItem("ba.lastBot");
      const lastMachine = localStorage.getItem("ba.lastBotMachine");
      let pick = null;
      for (const m of machines) {
        for (const b of (m.bots || [])) {
          if (b.name === lastBot && m.machine_id === lastMachine && m.online) {
            pick = { bot: b.name, machine: m.machine_id }; break;
          }
        }
        if (pick) break;
      }
      if (!pick) {
        for (const m of machines) {
          if (!m.online) continue;
          if ((m.bots || []).length) { pick = { bot: m.bots[0].name, machine: m.machine_id }; break; }
        }
      }
      if (pick) {
        await selectBot(pick.bot, pick.machine);
      }
    } else {
      // Mark current bot as stale if its machine went offline
      const m = state.machines.find(m => m.machine_id === state.botMachine);
      if (m && !m.online) {
        chatTitle.textContent = `${state.bot} @ ${state.botMachine} · offline`;
      }
    }
  }

  function renderMachines() {
    // <machines-panel> renders the tree + owns collapse; app.js owns the data.
    machinesPanel.render(state.machines, { bot: state.bot, botMachine: state.botMachine });
  }

  async function selectBot(botName, machineId) {
    // Mask the chat panel for the whole switch so the user doesn't see
    // empty/stale content while we fetch the new bot's session list and
    // history. switchChat() will keep the mask up through its own swap.
    $("messages-mask").classList.remove("hidden");
    // Also show a loading placeholder in the sidebar's session list so the
    // old bot's sessions don't sit there stale during the fetch.
    sessionsPanel.showLoading();
    state.bot = botName;
    state.botMachine = machineId;
    localStorage.setItem("ba.lastBot", botName);
    localStorage.setItem("ba.lastBotMachine", machineId);
    sessionsOf.textContent = `· ${botName} @ ${machineId}`;
    const key = botKey(machineId, botName);
    state.sessions[key] = loadSessions(machineId, botName);
    state.serverSessions[key] = await fetchServerSessions(machineId, botName);
    renderMachines();
    const lastChat = localStorage.getItem("ba.last." + key);
    await switchChat(lastChat || pickFirstSessionId() || uuid());
    closeSidebar();
  }

  // ── App context ──
  // Shared bag the split-out controllers read/attach to (no build step). First
  // slice: chat-controller.js owns the SSE stream + event router; it reads these
  // and attaches app.openStream / app.handleEvent.
  const app = {
    state, TOKEN,
    curKey, setConn,
    addMessage, showTyping, removeTyping, scrollDown,
    renderToolCall, renderToolResult,
    touchSession, fetchServerSessions, refreshSessionList, refreshSessionInfo,
  };
  ChatController(app);

  // ── UI events ──
  $("refresh-machines").onclick = () => loadMachines().catch(() => {});
  $("restart-all").onclick = () => restartCluster();
  // Panels signal intent via injected callbacks; app.js owns navigation + actions.
  machinesPanel.onSelectBot = selectBot;
  machinesPanel.onRestartMachine = restartMachine;
  sessionsPanel.onSelectSession = (chatId) => { switchChat(chatId); closeSidebar(); };
  $("recents-clear").onclick = () => recents.clear();
  // Section collapse (caret + persisted state). Same shape for both sidebar
  // sections, so share one setup.
  function setupCollapse(sectionId, toggleId, storageKey) {
    const section = $(sectionId);
    const toggle = $(toggleId);
    if (localStorage.getItem(storageKey) === "1") {
      section.classList.add("collapsed");
      toggle.textContent = "▸";
    }
    toggle.onclick = () => {
      const collapsed = section.classList.toggle("collapsed");
      toggle.textContent = collapsed ? "▸" : "▾";
      localStorage.setItem(storageKey, collapsed ? "1" : "0");
    };
  }
  setupCollapse("recents-section", "recents-toggle", "ba.recentsCollapsed");
  setupCollapse("machines-section", "machines-toggle", "ba.machinesCollapsed");

  // ── Sidebar resize ── component: sidebar-resize.js (self-contained behavior)

  $("new-session").onclick = async () => {
    await switchChat(uuid());
    closeSidebar();
    input.focus();
  };

  $("rename-session").onclick = async () => {
    if (!state.chatId) return;
    const key = curKey();
    const server = (state.serverSessions[key] || []).find(s => s.chat_id === state.chatId);
    const sessions = state.sessions[key] || {};
    const current = sessions[state.chatId] || {};
    const currentTitle = (server && (server.custom_title || server.summary)) || current.title || "";
    const t = prompt("Rename session:", currentTitle);
    if (t == null) return;
    const newTitle = t.trim();
    if (!newTitle) return;

    const sid = server && server.session_id;
    if (sid) {
      try {
        const r = await fetch("/api/sessions/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            bot: state.bot,
            machine: state.botMachine,
            session_id: sid,
            title: newTitle,
          }),
        });
        const j = await r.json();
        if (!j.ok) {
          alert("Rename failed: " + (j.error || "unknown"));
          return;
        }
        if (server) server.custom_title = newTitle;
      } catch (e) {
        alert("Rename failed: " + e);
        return;
      }
    } else {
      // No backend session yet (brand-new web chat) — fall back to local rename.
      current.title = newTitle;
      sessions[state.chatId] = current;
      state.sessions[key] = sessions;
      saveSessions(state.botMachine, state.bot, sessions);
    }
    chatTitle.textContent = newTitle;
    refreshSessionList();
  };

  $("menu-open").onclick = () => { sidebar.classList.add("open"); document.body.classList.add("menu-open"); };
  $("menu-close").onclick = closeSidebar;
  function closeSidebar() { sidebar.classList.remove("open"); document.body.classList.remove("menu-open"); }
  document.addEventListener("click", (e) => {
    if (document.body.classList.contains("menu-open") && !sidebar.contains(e.target) && e.target.id !== "menu-open") {
      closeSidebar();
    }
  });

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    autoResize();
    sendText(text);
  });

  function autoResize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
  }
  input.addEventListener("input", autoResize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      composer.dispatchEvent(new Event("submit"));
    }
  });

  function pickFirstSessionId() {
    const list = currentSessionEntries();
    return list.length ? list[0].chat_id : null;
  }

  // ── Periodic refresh ──
  function startMachinePoll() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(() => {
      loadMachines().catch(() => { /* network blip; UI stays as-is */ });
    }, 15000);
  }

  // ── Boot ──
  recents.render();  // populate from localStorage before machines come back
  loadMachines().then(startMachinePoll).catch((e) => {
    console.error(e);
    setConn("offline");
    addMessage("assistant", "_Failed to connect: " + e.message + "_");
  });

  // ── Claude session picker (component: components/session-picker.js) ──
  // The <session-picker> element owns the modal + all Claude-resume HTTP +
  // pagination. We inject the HTTP wrapper + current-bot context; on a
  // successful resume it emits "resumed", and we update the local session
  // caches + navigate (app state the component deliberately doesn't touch).
  const picker = $("claude-picker");
  picker.api = api;
  picker.getContext = () => ({ machine: state.botMachine, bot: state.bot });
  picker.onResumed = async (info) => {
    const { chat_id, machine, bot, raw, project, session } = info;
    const sessions = loadSessions(machine, bot);
    sessions[chat_id] = {
      title: `${raw ? "Raw" : "Claude"} · ${project.label}`,
      preview: session.first_user || "",
      ts: Date.now(),
    };
    saveSessions(machine, bot, sessions);
    if (raw && state.bot !== "raw") { state.bot = "raw"; state.botMachine = machine; }
    const key = curKey();
    state.sessions[key] = sessions;
    state.serverSessions[key] = await fetchServerSessions(machine, bot);
    await switchChat(chat_id);
  };
  $("open-claude-picker").onclick = () => picker.open();
})();
