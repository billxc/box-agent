/* BoxAgent Web Chat — vanilla JS, no build step */
(() => {
  const params = new URLSearchParams(location.search);
  const TOKEN = params.get("token") || localStorage.getItem("ba.token") || "";
  if (params.get("token")) localStorage.setItem("ba.token", TOKEN);

  const $ = (id) => document.getElementById(id);
  const messagesEl = $("messages");
  const machineList = $("machine-list");
  const sessionList = $("session-list");
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
    collapsed: new Set(JSON.parse(localStorage.getItem("ba.collapsedMachines") || "[]")),
    sessions: {},         // "machine|bot" -> {chat_id: {title, preview, ts}}  (local browser-side)
    serverSessions: {},   // "machine|bot" -> [{chat_id, platform, preview, last_ts, ...}]
    chatId: null,
    es: null,
    streamMsgs: {},
    typingEl: null,
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
  function recentKey(machine, bot, chatId) { return `${machine}|${bot}|${chatId}`; }

  // ── Recents (cross-bot, browser-local) ──
  // Stored as an array in localStorage["ba.recents"], capped at RECENTS_MAX,
  // newest first. Each entry: {machine, bot, chat_id, title, preview, ts,
  // platform, display_name}. Click → switch bot+machine then open chat.
  const RECENTS_MAX = 25;

  function loadRecents() {
    try {
      const raw = JSON.parse(localStorage.getItem("ba.recents") || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch { return []; }
  }
  function saveRecents(arr) {
    localStorage.setItem("ba.recents", JSON.stringify(arr.slice(0, RECENTS_MAX)));
  }
  // Bump a chat to the top of recents. Called on switchChat + on every
  // incoming/outgoing message so the most-active chat sits at the top.
  function touchRecent(patch) {
    if (!patch.machine || !patch.bot || !patch.chat_id) return;
    const key = recentKey(patch.machine, patch.bot, patch.chat_id);
    const all = loadRecents();
    const idx = all.findIndex(r => recentKey(r.machine, r.bot, r.chat_id) === key);
    const prev = idx >= 0 ? all[idx] : {};
    if (idx >= 0) all.splice(idx, 1);
    const merged = {
      machine: patch.machine,
      bot: patch.bot,
      chat_id: patch.chat_id,
      title: patch.title || prev.title || patch.chat_id,
      preview: patch.preview != null ? patch.preview : (prev.preview || ""),
      recap: patch.recap != null ? patch.recap : (prev.recap || ""),
      platform: patch.platform || prev.platform || "unknown",
      display_name: patch.display_name || prev.display_name || patch.bot,
      ts: patch.ts || Math.floor(Date.now() / 1000),
    };
    all.unshift(merged);
    saveRecents(all);
    renderRecents();
  }
  function removeRecent(machine, bot, chatId) {
    const key = recentKey(machine, bot, chatId);
    const all = loadRecents().filter(r => recentKey(r.machine, r.bot, r.chat_id) !== key);
    saveRecents(all);
    renderRecents();
  }
  function clearRecents() {
    if (!confirm("Clear all recent chats from this browser?")) return;
    saveRecents([]);
    renderRecents();
  }

  function renderRecents() {
    const ul = $("recents-list");
    if (!ul) return;
    ul.innerHTML = "";
    const all = loadRecents();
    if (all.length === 0) {
      const li = document.createElement("li");
      li.style.color = "var(--muted)";
      li.style.cursor = "default";
      li.textContent = "No recent chats";
      ul.appendChild(li);
      return;
    }
    const onlineMachines = new Set(
      (state.machines || []).filter(m => m.online).map(m => m.machine_id),
    );
    for (const r of all) {
      const li = document.createElement("li");
      const isActive = state.bot === r.bot
        && state.botMachine === r.machine
        && state.chatId === r.chat_id;
      if (isActive) li.classList.add("active");
      if (state.machines.length && !onlineMachines.has(r.machine)) li.classList.add("offline");

      const plat = document.createElement("span");
      plat.className = "plat";
      plat.title = r.platform;
      plat.textContent = platformIcon(r.platform);

      const body = document.createElement("div");
      body.className = "recent-body";
      const title = document.createElement("div");
      title.className = "recent-title";
      title.textContent = r.title || r.chat_id;
      const meta = document.createElement("div");
      meta.className = "recent-meta";
      meta.textContent = `${r.display_name || r.bot} @ ${r.machine}${r.preview ? " · " + r.preview : ""}`;
      body.appendChild(title);
      body.appendChild(meta);

      const time = document.createElement("span");
      time.className = "recent-time";
      time.textContent = r.ts ? formatRelative(r.ts) : "";

      const del = document.createElement("button");
      del.className = "recent-del";
      del.textContent = "×";
      del.title = "Remove from recents";
      del.onclick = (e) => {
        e.stopPropagation();
        removeRecent(r.machine, r.bot, r.chat_id);
      };

      li.appendChild(plat);
      li.appendChild(body);
      li.appendChild(time);
      li.appendChild(del);
      li.onclick = () => openRecent(r);
      ul.appendChild(li);
    }
  }

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

  function loadSessions(machine, bot) {
    try {
      return JSON.parse(localStorage.getItem("ba.sessions." + botKey(machine, bot)) || "{}");
    } catch { return {}; }
  }
  function saveSessions(machine, bot, sessions) {
    localStorage.setItem("ba.sessions." + botKey(machine, bot), JSON.stringify(sessions));
  }

  async function fetchServerSessions(machine, bot) {
    try {
      const r = await api(`sessions?bot=${encodeURIComponent(bot)}&machine=${encodeURIComponent(machine)}`);
      if (!r.ok) return [];
      const { sessions } = await r.json();
      return sessions || [];
    } catch { return []; }
  }

  function platformIcon(p) {
    return ({ telegram: "✈︎", web: "◉", claude: "✦", other: "•", unknown: "•" })[p] || "•";
  }

  function setConn(state_) {
    connDot.className = "dot " + state_;
    connLabel.textContent = state_;
  }

  function scrollDown() {
    requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
  }

  // Message bubbles render via the <chat-message> custom element
  // (components/chat-message.js); shared markdown/escape live in util.js.
  function addMessage(role, text, opts = {}) {
    removeTyping();
    const el = ChatMessage.create(role, text, opts);
    messagesEl.appendChild(el);
    scrollDown();
    return el;
  }

  function showTyping() {
    if (state.typingEl) return;
    const el = document.createElement("div");
    el.className = "typing";
    el.innerHTML = "<span></span><span></span><span></span>";
    messagesEl.appendChild(el);
    state.typingEl = el;
    scrollDown();
  }
  function removeTyping() {
    if (state.typingEl) { state.typingEl.remove(); state.typingEl = null; }
  }

  // ── Sessions ──
  function buildSessionList() {
    // Merge server sessions (cross-platform) with local ones (web-only, may have user-renamed titles)
    const local = state.sessions[curKey()] || {};
    const server = state.serverSessions[curKey()] || [];
    const merged = new Map(); // chat_id -> entry
    for (const s of server) {
      const backendTitle = s.custom_title || s.summary || "";
      merged.set(s.chat_id, {
        chat_id: s.chat_id,
        platform: s.platform || "unknown",
        title: backendTitle || (local[s.chat_id] && local[s.chat_id].title) || defaultTitle(s),
        custom_title: s.custom_title || "",
        summary: s.summary || "",
        recap: s.recap || "",
        session_id: s.session_id || "",
        preview: s.preview || "",
        ts: (s.last_ts ? s.last_ts * 1000 : 0) || (local[s.chat_id] && local[s.chat_id].ts) || 0,
        backend: s.backend || "",
        model: s.model || "",
      });
    }
    // Local-only entries (brand-new web chats with no transcript yet)
    for (const [cid, meta] of Object.entries(local)) {
      if (merged.has(cid)) continue;
      merged.set(cid, {
        chat_id: cid,
        platform: cid.startsWith("web-") ? "web" : "unknown",
        title: meta.title || cid,
        preview: meta.preview || "",
        ts: meta.ts || 0,
      });
    }
    return [...merged.values()].sort((a, b) => (b.ts || 0) - (a.ts || 0));
  }

  function defaultTitle(s) {
    if (s.platform === "claude") return `✦ Resumed Claude session`;
    const tag = ({ telegram: "Telegram", web: "Web", other: "Chat" })[s.platform] || "Chat";
    return `${tag} · ${shortId(s.chat_id)}`;
  }
  function shortId(cid) { return cid.length > 12 ? cid.slice(0, 6) + "…" + cid.slice(-4) : cid; }

  function refreshSessionList() {
    sessionList.innerHTML = "";
    const entries = buildSessionList();
    if (entries.length === 0) {
      const li = document.createElement("li");
      li.style.color = "var(--muted)";
      li.style.cursor = "default";
      li.textContent = "No sessions yet — start chatting";
      sessionList.appendChild(li);
      return;
    }
    for (const meta of entries) {
      const li = document.createElement("li");
      if (meta.chat_id === state.chatId) li.classList.add("active");
      const title = document.createElement("div");
      title.className = "session-title";
      title.innerHTML = `<span class="plat" title="${meta.platform}">${platformIcon(meta.platform)}</span> ${escapeHtml(meta.title)}`;
      const preview = document.createElement("div");
      preview.className = "session-preview";
      preview.textContent = meta.preview || "(no messages yet)";
      li.appendChild(title); li.appendChild(preview);
      if (meta.recap) {
        const recap = document.createElement("div");
        recap.className = "session-recap";
        recap.textContent = meta.recap;
        recap.title = meta.recap;
        li.appendChild(recap);
      }
      li.onclick = () => { switchChat(meta.chat_id); closeSidebar(); };
      sessionList.appendChild(li);
    }
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
    touchRecent({
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

  function renderHistoryFragment(items) {
    const frag = document.createDocumentFragment();
    for (const h of items) {
      if (h.role === "tool_call") {
        ToolCard.upsertCall(frag, h.tool_id || "", h.name || "tool", h.args || {});
        continue;
      }
      if (h.role === "tool_result") {
        ToolCard.applyResult(frag, h.tool_id || "", !!h.ok, h.summary || "", h.error || "");
        continue;
      }
      const el = ChatMessage.create(h.role, h.text, { ts: h.ts });
      el.style.animation = "none";
      frag.appendChild(el);
    }
    return frag;
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
      const frag = renderHistoryFragment(items);
      // Preserve scroll position: remember height before prepend, restore after.
      const prevBehavior = messagesEl.style.scrollBehavior;
      messagesEl.style.scrollBehavior = "auto";
      const beforeHeight = messagesEl.scrollHeight;
      const beforeTop = messagesEl.scrollTop;
      messagesEl.insertBefore(frag, messagesEl.firstChild);
      const afterHeight = messagesEl.scrollHeight;
      messagesEl.scrollTop = beforeTop + (afterHeight - beforeHeight);
      messagesEl.style.scrollBehavior = prevBehavior;
      state.historyOffset += items.length;
      if (state.historyOffset >= state.historyTotal) state.historyExhausted = true;
    } catch (e) {
      console.warn("history load-more failed", e);
    } finally {
      state.historyLoading = false;
    }
  }

  messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop < 100) loadOlderHistory();
  }, { passive: true });

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
    touchRecent({
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

    const frag = renderHistoryFragment(history);
    // Disable smooth scroll just for the bottom-jump; the live-stream
    // appends below still use the smooth behavior set in CSS.
    const prevBehavior = messagesEl.style.scrollBehavior;
    messagesEl.style.scrollBehavior = "auto";
    messagesEl.replaceChildren(frag);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    messagesEl.style.scrollBehavior = prevBehavior;

    requestAnimationFrame(() => mask.classList.add("hidden"));

    openStream();
  }

  function openStream() {
    const url = `api/stream?bot=${encodeURIComponent(state.bot)}&machine=${encodeURIComponent(state.botMachine)}&chat_id=${encodeURIComponent(state.chatId)}` + (TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : "");
    const es = new EventSource(url);
    state.es = es;
    es.onopen = () => setConn("online");
    es.onerror = () => {
      setConn("offline");
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
        addMessage(ev.role || "assistant", ev.text || "", { id: ev.message_id, ts: ev.ts });
        if (ev.role === "assistant") touchSession(ev.text || "");
        else touchSession(ev.text || "");
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
        const s = state.streamMsgs[ev.message_id];
        if (!s) return;
        s.text = ev.text != null ? ev.text : s.text + (ev.delta || "");
        s.el.setText(s.text);
        scrollDown();
        break;
      }
      case "stream_end": {
        const s = state.streamMsgs[ev.message_id];
        if (s) {
          if (ev.text) s.el.setText(ev.text);
          touchSession(ev.text || s.text);
          delete state.streamMsgs[ev.message_id];
        }
        // Refresh server-side session list (other platforms may have new turns).
        fetchServerSessions(state.botMachine, state.bot).then((list) => {
          state.serverSessions[curKey()] = list;
          refreshSessionList();
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
      case "_close": setConn("offline"); break;
    }
  }

  // ── Tool call rendering ──
  // <tool-card> (components/tool-card.js) owns find / create / dedup / result;
  // here we just hand it the live #messages container.
  function renderToolCall(toolId, name, args, parentToolId = "") {
    ToolCard.upsertCall(messagesEl, toolId, name, args, parentToolId);
  }

  function renderToolResult(toolId, ok, summary, error) {
    ToolCard.applyResult(messagesEl, toolId, ok, summary, error);
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
    renderRecents();
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
    machineList.innerHTML = "";
    if (state.machines.length === 0) {
      machineList.innerHTML = "<li class='muted'>No machines</li>";
      return;
    }
    for (const m of state.machines) {
      const li = document.createElement("li");
      li.className = "machine" + (m.online ? "" : " offline") + (state.collapsed.has(m.machine_id) ? " collapsed" : "");
      const head = document.createElement("div");
      head.className = "machine-head";
      const dotCls = m.online ? "online" : "offline";
      const lastSeen = m.online ? "" : ` · ${formatRelative(m.last_seen)}`;
      head.innerHTML = `
        <span class="caret"></span>
        <span class="dot ${dotCls}"></span>
        <span class="name">${escapeHtml(m.machine_id)}</span>
        ${m.role && m.role !== "guest" ? `<span class="role">${m.role}${typeof m.host_index === "number" && m.host_index >= 0 ? `·#${m.host_index + 1}` : ""}</span>` : ""}
        ${m.self ? `<span class="role self">this</span>` : ""}
        <span class="last">${lastSeen}</span>
        <button class="machine-restart icon-btn" title="Restart this node">⟲</button>
      `;
      head.onclick = (e) => {
        if (e.target.classList.contains("machine-restart")) {
          e.stopPropagation();
          restartMachine(m.machine_id, m.online);
          return;
        }
        toggleMachine(m.machine_id);
      };
      li.appendChild(head);

      const bots = document.createElement("ul");
      bots.className = "machine-bots";
      for (const b of (m.bots || [])) {
        const bot_li = document.createElement("li");
        if (state.bot === b.name && state.botMachine === m.machine_id) {
          bot_li.classList.add("active");
        }
        bot_li.innerHTML = `<span>${escapeHtml(b.display_name || b.name)}</span><span class="kind">${b.kind || "bot"}</span>`;
        bot_li.onclick = (e) => {
          e.stopPropagation();
          if (!m.online) { alert(`Machine ${m.machine_id} is offline`); return; }
          selectBot(b.name, m.machine_id);
        };
        bots.appendChild(bot_li);
      }
      if (!(m.bots || []).length) {
        bots.innerHTML = "<li class='muted' style='cursor:default;'>(no bots)</li>";
      }
      li.appendChild(bots);
      machineList.appendChild(li);
    }
  }

  function toggleMachine(machine_id) {
    if (state.collapsed.has(machine_id)) state.collapsed.delete(machine_id);
    else state.collapsed.add(machine_id);
    localStorage.setItem("ba.collapsedMachines", JSON.stringify([...state.collapsed]));
    renderMachines();
  }

  async function selectBot(botName, machineId) {
    // Mask the chat panel for the whole switch so the user doesn't see
    // empty/stale content while we fetch the new bot's session list and
    // history. switchChat() will keep the mask up through its own swap.
    $("messages-mask").classList.remove("hidden");
    // Also show a loading placeholder in the sidebar's session list so the
    // old bot's sessions don't sit there stale during the fetch.
    sessionList.innerHTML = "<li class='muted' style='cursor:default;'>Loading sessions…</li>";
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

  function formatRelative(ts) {
    if (!ts) return "";
    const diff = Math.floor(Date.now() / 1000 - ts);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  // ── UI events ──
  $("refresh-machines").onclick = () => loadMachines().catch(() => {});
  $("restart-all").onclick = () => restartCluster();
  $("recents-clear").onclick = () => clearRecents();
  const recentsSection = $("recents-section");
  const recentsToggle = $("recents-toggle");
  if (localStorage.getItem("ba.recentsCollapsed") === "1") {
    recentsSection.classList.add("collapsed");
    recentsToggle.textContent = "▸";
  }
  recentsToggle.onclick = () => {
    const collapsed = recentsSection.classList.toggle("collapsed");
    recentsToggle.textContent = collapsed ? "▸" : "▾";
    localStorage.setItem("ba.recentsCollapsed", collapsed ? "1" : "0");
  };
  const machinesSection = $("machines-section");
  const machinesToggle = $("machines-toggle");
  if (localStorage.getItem("ba.machinesCollapsed") === "1") {
    machinesSection.classList.add("collapsed");
    machinesToggle.textContent = "▸";
  }
  machinesToggle.onclick = () => {
    const collapsed = machinesSection.classList.toggle("collapsed");
    machinesToggle.textContent = collapsed ? "▸" : "▾";
    localStorage.setItem("ba.machinesCollapsed", collapsed ? "1" : "0");
  };

  // ── Sidebar resize ──
  (function setupResize() {
    const resizer = $("sidebar-resizer");
    if (!resizer) return;
    // Restore persisted width on desktop only
    const saved = parseInt(localStorage.getItem("ba.sidebarWidth") || "0", 10);
    if (saved >= 200 && window.innerWidth > 720) {
      sidebar.style.flex = `0 0 ${saved}px`;
      sidebar.style.width = `${saved}px`;
    }
    let dragging = false;
    let startX = 0, startW = 0;
    function onMove(e) {
      if (!dragging) return;
      const x = e.touches ? e.touches[0].clientX : e.clientX;
      const dx = x - startX;
      let w = startW + dx;
      // clamp [200, 70vw]
      w = Math.max(200, Math.min(window.innerWidth * 0.7, w));
      sidebar.style.flex = `0 0 ${w}px`;
      sidebar.style.width = `${w}px`;
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("dragging");
      document.body.classList.remove("sidebar-dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("touchmove", onMove);
      document.removeEventListener("touchend", onUp);
      const w = parseInt(sidebar.style.width, 10);
      if (w >= 200) localStorage.setItem("ba.sidebarWidth", String(w));
    }
    function onDown(e) {
      if (window.innerWidth <= 720) return;  // mobile uses drawer
      dragging = true;
      startX = e.touches ? e.touches[0].clientX : e.clientX;
      startW = sidebar.getBoundingClientRect().width;
      resizer.classList.add("dragging");
      document.body.classList.add("sidebar-dragging");
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onUp);
      e.preventDefault();
    }
    resizer.addEventListener("mousedown", onDown);
    resizer.addEventListener("touchstart", onDown, { passive: false });
    // Double-click resets to default
    resizer.addEventListener("dblclick", () => {
      sidebar.style.flex = "";
      sidebar.style.width = "";
      localStorage.removeItem("ba.sidebarWidth");
    });
  })();

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
    const list = buildSessionList();
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
  renderRecents();  // populate from localStorage before machines come back
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
  picker.addEventListener("resumed", async (e) => {
    const { chat_id, machine, bot, raw, project, session } = e.detail;
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
  });
  $("open-claude-picker").onclick = () => picker.open();
})();
