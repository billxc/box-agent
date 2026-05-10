/* BoxAgent Web Chat — vanilla JS, no build step */
(() => {
  const qs = new URLSearchParams(location.search);
  const TOKEN = qs.get("token") || localStorage.getItem("ba.token") || "";
  if (qs.get("token")) localStorage.setItem("ba.token", TOKEN);

  const $ = (id) => document.getElementById(id);
  const messagesEl = $("messages");
  const machineList = $("machine-list");
  const sessionList = $("session-list");
  const sessionsOf = $("sessions-of");
  const chatTitle = $("chat-title");
  const composer = $("composer");
  const input = $("input");
  const sendBtn = $("send");
  const connDot = $("conn-state");
  const connLabel = $("conn-label");
  const sidebar = $("sidebar");

  const state = {
    machines: [],         // [{machine_id, online, role, self, bots, last_seen}]
    bot: null,            // selected bot name
    botMachine: null,     // selected bot's machine_id (for display)
    collapsed: new Set(JSON.parse(localStorage.getItem("ba.collapsedMachines") || "[]")),
    sessions: {},         // "machine|bot" -> {chat_id: {title, preview, ts}}  (local browser-side)
    serverSessions: {},   // "machine|bot" -> [{chat_id, platform, preview, last_ts, ...}]
    specialists: {},      // "machine|wg" -> [{name, display_name}] (workgroup specialists)
    chatId: null,
    es: null,
    streamMsgs: {},
    toolCards: {},        // tool_id -> {el, headerEl, resultEl, name, args}
    typingEl: null,
    refreshTimer: null,
  };

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
      body.className = "rec-body";
      const title = document.createElement("div");
      title.className = "rec-title";
      title.textContent = r.title || r.chat_id;
      const meta = document.createElement("div");
      meta.className = "rec-meta";
      meta.textContent = `${r.display_name || r.bot} @ ${r.machine}${r.preview ? " · " + r.preview : ""}`;
      body.appendChild(title);
      body.appendChild(meta);

      const time = document.createElement("span");
      time.className = "rec-time";
      time.textContent = r.ts ? formatRelative(r.ts) : "";

      const del = document.createElement("button");
      del.className = "rec-del";
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

  // Fetch the specialist list for a workgroup once per session and cache it.
  // Local-only for v1: remote-machine workgroups would need API forwarding
  // (out of scope; tracked under #9).
  async function loadSpecialists(machine, wgName) {
    const key = botKey(machine, wgName);
    if (state.specialists[key]) return state.specialists[key];
    let list = [];
    try {
      const r = await api(`workgroup/specialists?workgroup=${encodeURIComponent(wgName)}`);
      if (r.ok) {
        const data = await r.json();
        if (data && data.ok && Array.isArray(data.specialists)) list = data.specialists;
      }
    } catch { /* swallow — cache empty list to avoid retry loop */ }
    state.specialists[key] = list;
    return list;
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

  function renderMd(text) {
    try {
      return window.marked
        ? window.marked.parse(text, { breaks: true, gfm: true })
        : escapeHtml(text);
    } catch { return escapeHtml(text); }
  }
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  }

  function buildMessage(role, text, opts = {}) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    const md = document.createElement("div");
    md.className = "md";
    md.innerHTML = role === "user" ? escapeHtml(text).replace(/\n/g, "<br>") : renderMd(text);
    el.appendChild(md);
    // Per-message timestamp. ts is epoch seconds (server-assigned via WebChannel
    // _publish, or stored in transcript). Falls back to "now" for safety.
    const tsSec = (opts.ts && opts.ts > 0) ? opts.ts : (Date.now() / 1000);
    const time = document.createElement("time");
    time.className = "msg-time";
    const d = new Date(tsSec * 1000);
    time.textContent = _fmtMessageTime(d);
    time.title = d.toLocaleString();
    time.dateTime = d.toISOString();
    el.appendChild(time);
    if (opts.id) el.dataset.id = opts.id;
    return el;
  }

  // HH:MM if today, otherwise MM-DD HH:MM. Locale-aware via Intl, 24h to keep
  // chats compact and unambiguous.
  function _fmtMessageTime(d) {
    const now = new Date();
    const sameDay = d.getFullYear() === now.getFullYear()
      && d.getMonth() === now.getMonth()
      && d.getDate() === now.getDate();
    const pad = (n) => String(n).padStart(2, "0");
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (sameDay) return hm;
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
  }

  function addMessage(role, text, opts = {}) {
    removeTyping();
    const el = buildMessage(role, text, opts);
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
      merged.set(s.chat_id, {
        chat_id: s.chat_id,
        platform: s.platform || "unknown",
        is_main: !!s.is_main,
        title: (local[s.chat_id] && local[s.chat_id].title) || defaultTitle(s),
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
    if (s.is_main) return `★ Main session`;
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
      title.className = "sess-title";
      const mainBadge = meta.is_main ? `<span class="main-badge" title="Main session">★</span> ` : "";
      title.innerHTML = `${mainBadge}<span class="plat" title="${meta.platform}">${platformIcon(meta.platform)}</span> ${escapeHtml(meta.title)}`;
      const preview = document.createElement("div");
      preview.className = "sess-preview";
      preview.textContent = meta.preview || "(no messages yet)";
      const actions = document.createElement("div");
      actions.className = "sess-actions";
      if (!meta.is_main) {
        const setMain = document.createElement("a");
        setMain.href = "#";
        setMain.className = "sess-action";
        setMain.textContent = "set as main";
        setMain.title = "Heartbeat ticks and incoming peer messages will route into this session";
        setMain.onclick = (e) => { e.stopPropagation(); e.preventDefault(); setMainSession(meta.chat_id); };
        actions.appendChild(setMain);
      }
      li.appendChild(title); li.appendChild(preview);
      if (actions.children.length) li.appendChild(actions);
      li.onclick = () => { switchChat(meta.chat_id); closeSidebar(); };
      sessionList.appendChild(li);
    }
  }

  async function setMainSession(chatId) {
    try {
      const r = await api("sessions/set_main", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bot: state.bot, machine: state.botMachine, chat_id: chatId }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.serverSessions[curKey()] = await fetchServerSessions(state.botMachine, state.bot);
      refreshSessionList();
    } catch (e) {
      alert(`Failed to set main session: ${e.message || e}`);
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
    const cur = sessions[state.chatId] || { title: "Chat " + new Date().toLocaleString() };
    cur.preview = preview.slice(0, 60);
    cur.ts = Date.now();
    sessions[state.chatId] = cur;
    state.sessions[key] = sessions;
    saveSessions(state.botMachine, state.bot, sessions);
    refreshSessionList();
    chatTitle.textContent = cur.title;
    // Bump global recents too — keeps the cross-bot Recent panel fresh.
    const botInfo = (state.machines || [])
      .find(m => m.machine_id === state.botMachine)?.bots
      ?.find(b => b.name === state.bot);
    touchRecent({
      machine: state.botMachine,
      bot: state.bot,
      chat_id: state.chatId,
      title: cur.title,
      preview: cur.preview,
      platform: state.chatId.startsWith("web-") ? "web"
        : state.chatId.startsWith("workgroup:") ? "workgroup"
        : "other",
      display_name: botInfo?.display_name || state.bot,
    });
  }

  // ── Chat lifecycle ──
  async function switchChat(chatId) {
    if (state.es) { state.es.close(); state.es = null; }
    state.chatId = chatId;
    state.streamMsgs = {};
    state.toolCards = {};
    refreshSessionList();
    const meta = (state.sessions[curKey()] || {})[chatId] || {};
    chatTitle.textContent = meta.title || chatId;
    localStorage.setItem("ba.last." + curKey(), chatId);

    // Record this open in the cross-bot recents so the next visit can find
    // it from the top-level Recent panel.
    const botInfo = (state.machines || [])
      .find(m => m.machine_id === state.botMachine)?.bots
      ?.find(b => b.name === state.bot);
    const serverList = state.serverSessions[curKey()] || [];
    const serverMeta = serverList.find(s => s.chat_id === chatId);
    touchRecent({
      machine: state.botMachine,
      bot: state.bot,
      chat_id: chatId,
      title: meta.title || (serverMeta ? defaultTitle(serverMeta) : chatId),
      preview: serverMeta?.preview || meta.preview || "",
      platform: serverMeta?.platform
        || (chatId.startsWith("web-") ? "web"
            : chatId.startsWith("workgroup:") ? "workgroup" : "other"),
      display_name: botInfo?.display_name || state.bot,
      ts: serverMeta?.last_ts || Math.floor(Date.now() / 1000),
    });

    // Cover the chat panel with a mask so the fetch + swap + scroll-to-bottom
    // all happen invisibly. Old content stays in the DOM behind the mask.
    const mask = $("messages-mask");
    mask.classList.remove("hidden");

    setConn("connecting");
    let history = [];
    try {
      const r = await api(`history?bot=${encodeURIComponent(state.bot)}&machine=${encodeURIComponent(state.botMachine)}&chat_id=${encodeURIComponent(chatId)}`);
      if (r.ok) {
        const j = await r.json();
        history = j.history || [];
      }
    } catch (e) { console.warn("history load failed", e); }

    const frag = document.createDocumentFragment();
    for (const h of history) {
      if (h.role === "tool_call") {
        const card = _buildToolCard(h.tool_id || "", h.name || "tool", h.args || {});
        frag.appendChild(card.el);
        // Stash so a following tool_result with the same id can update it.
        state.toolCards[h.tool_id || `__hist${frag.children.length}`] = card;
        continue;
      }
      if (h.role === "tool_result") {
        _applyToolResult(h.tool_id || "", !!h.ok, h.summary || "", h.error || "");
        continue;
      }
      const el = buildMessage(h.role, h.text, { ts: h.ts });
      el.style.animation = "none";
      frag.appendChild(el);
    }
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
        s.el.querySelector(".md").innerHTML = renderMd(s.text);
        scrollDown();
        break;
      }
      case "stream_end": {
        const s = state.streamMsgs[ev.message_id];
        if (s) {
          if (ev.text) s.el.querySelector(".md").innerHTML = renderMd(ev.text);
          touchSession(ev.text || s.text);
          delete state.streamMsgs[ev.message_id];
        }
        // Refresh server-side session list (other platforms may have new turns).
        fetchServerSessions(state.botMachine, state.bot).then((list) => {
          state.serverSessions[curKey()] = list;
          refreshSessionList();
        });
        break;
      }
      case "tool_call": {
        renderToolCall(ev.tool_id, ev.name || "tool", ev.args || {});
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
  function _argSummary(args) {
    if (!args || typeof args !== "object") return "";
    for (const v of Object.values(args)) {
      if (typeof v === "string" && v.length) {
        const s = v.replace(/\s+/g, " ");
        return s.length > 60 ? s.slice(0, 60) + "…" : s;
      }
    }
    try {
      const j = JSON.stringify(args);
      return j.length > 60 ? j.slice(0, 60) + "…" : j;
    } catch { return ""; }
  }

  function _buildToolCard(toolId, name, args) {
    const det = document.createElement("details");
    det.className = "tool-card";
    const summary = document.createElement("summary");
    summary.className = "tool-card-header";
    summary.textContent = `▶ ${name}(${_argSummary(args)})`;
    const body = document.createElement("pre");
    body.className = "tool-card-body";
    try { body.textContent = JSON.stringify(args, null, 2); } catch { body.textContent = String(args); }
    const result = document.createElement("div");
    result.className = "tool-card-result hidden";
    det.appendChild(summary);
    det.appendChild(body);
    det.appendChild(result);
    return { el: det, headerEl: summary, bodyEl: body, resultEl: result, name, args, toolId };
  }

  function _applyToolResult(toolId, ok, summary, error) {
    const card = state.toolCards[toolId];
    if (!card) {
      // Result without preceding call (rare; e.g. history machine_id-truncated).
      const synth = _buildToolCard(toolId || "?", "tool", {});
      document.getElementById("messages").appendChild(synth.el);
      state.toolCards[toolId || "?"] = synth;
      return _applyToolResult(toolId || "?", ok, summary, error);
    }
    const icon = ok ? "✓" : "✗";
    card.headerEl.textContent = `${icon} ${card.name}(${_argSummary(card.args)})`;
    card.resultEl.classList.remove("hidden");
    card.resultEl.textContent = ok ? (summary || "(ok)") : (error || summary || "(failed)");
    card.resultEl.classList.toggle("ok", ok);
    card.resultEl.classList.toggle("failed", !ok);
  }

  function renderToolCall(toolId, name, args) {
    if (!toolId) toolId = `t${Math.random().toString(36).slice(2, 10)}`;
    let card = state.toolCards[toolId];
    if (card) {
      // Idempotent: same id arriving twice (Claude streaming start + final).
      card.args = args;
      card.headerEl.textContent = `▶ ${name}(${_argSummary(args)})`;
      try { card.bodyEl.textContent = JSON.stringify(args, null, 2); } catch {}
      return;
    }
    card = _buildToolCard(toolId, name, args);
    document.getElementById("messages").appendChild(card.el);
    state.toolCards[toolId] = card;
  }

  function renderToolResult(toolId, ok, summary, error) {
    _applyToolResult(toolId, ok, summary, error);
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
        const bli = document.createElement("li");
        if (state.bot === b.name && state.botMachine === m.machine_id && !isSpecialistChat(state.chatId)) {
          bli.classList.add("active");
        }
        bli.innerHTML = `<span>${escapeHtml(b.display_name || b.name)}</span><span class="kind">${b.kind || "bot"}</span>`;
        bli.onclick = (e) => {
          e.stopPropagation();
          if (!m.online) { alert(`Machine ${m.machine_id} is offline`); return; }
          selectBot(b.name, m.machine_id);
        };
        bots.appendChild(bli);

        // For local workgroup admins, render specialists as collapsible
        // children so users can open each specialist's stream directly.
        // Remote workgroups are skipped (cluster RPC needed; #9).
        if (b.kind === "workgroup" && m.self) {
          const subUl = document.createElement("ul");
          subUl.className = "machine-bots specialist-sublist";
          bli.appendChild(subUl);
          renderSpecialistsInto(subUl, m.machine_id, b.name);
        }
      }
      if (!(m.bots || []).length) {
        bots.innerHTML = "<li class='muted' style='cursor:default;'>(no bots)</li>";
      }
      li.appendChild(bots);
      machineList.appendChild(li);
    }
  }

  // Marker: chat ids of the form "workgroup:<sp_name>" are specialist sub-chats.
  // ("wg:" is the legacy prefix; both honored during migration window.)
  function isSpecialistChat(chatId) {
    return typeof chatId === "string"
      && (chatId.startsWith("workgroup:") || chatId.startsWith("wg:"));
  }

  // Populate `<ul>` with one <li> per specialist of the given workgroup.
  // Uses cached state.specialists; first call triggers a fetch and re-render.
  function renderSpecialistsInto(ul, machineId, wgName) {
    const key = botKey(machineId, wgName);
    const cached = state.specialists[key];
    if (!cached) {
      ul.innerHTML = "<li class='muted specialist' style='cursor:default;'>…</li>";
      loadSpecialists(machineId, wgName).then(() => renderMachines());
      return;
    }
    ul.innerHTML = "";
    if (cached.length === 0) {
      // Don't show an empty placeholder — just collapse.
      return;
    }
    for (const sp of cached) {
      const li = document.createElement("li");
      li.className = "specialist";
      const chatId = `workgroup:${sp.name}`;
      if (state.bot === wgName && state.botMachine === machineId && state.chatId === chatId) {
        li.classList.add("active");
      }
      li.innerHTML = `<span>↳ ${escapeHtml(sp.display_name || sp.name)}</span>`;
      li.onclick = (e) => {
        e.stopPropagation();
        selectSpecialist(machineId, wgName, sp);
      };
      ul.appendChild(li);
    }
  }

  // Open a specialist's chat. Same bot (the workgroup admin) but chat_id
  // points at the specialist's virtual `workgroup:<name>` stream so the SSE
  // subscription receives that specialist's events.
  async function selectSpecialist(machineId, wgName, sp) {
    const chatId = `workgroup:${sp.name}`;
    // Make sure the target bot is loaded as the active bot first.
    if (state.bot !== wgName || state.botMachine !== machineId) {
      $("messages-mask").classList.remove("hidden");
      sessionList.innerHTML = "<li class='muted' style='cursor:default;'>Loading sessions…</li>";
      state.bot = wgName;
      state.botMachine = machineId;
      localStorage.setItem("ba.lastBot", wgName);
      localStorage.setItem("ba.lastBotMachine", machineId);
      sessionsOf.textContent = `· ${wgName} @ ${machineId}`;
      const key = botKey(machineId, wgName);
      state.sessions[key] = loadSessions(machineId, wgName);
      state.serverSessions[key] = await fetchServerSessions(machineId, wgName);
    }
    // Persist a session entry for this specialist chat so the session
    // sidebar lists it like any other conversation.
    const key = botKey(machineId, wgName);
    const sessions = state.sessions[key] || {};
    if (!sessions[chatId]) {
      sessions[chatId] = {
        title: `Specialist · ${sp.display_name || sp.name}`,
        preview: "",
        ts: Date.now(),
      };
      saveSessions(machineId, wgName, sessions);
      state.sessions[key] = sessions;
    }
    renderMachines();
    await switchChat(chatId);
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

  $("rename-session").onclick = () => {
    if (!state.chatId) return;
    const key = curKey();
    const sessions = state.sessions[key] || {};
    const cur = sessions[state.chatId] || {};
    const t = prompt("Rename session:", cur.title || "");
    if (t == null) return;
    cur.title = t.trim() || cur.title;
    sessions[state.chatId] = cur;
    state.sessions[key] = sessions;
    saveSessions(state.botMachine, state.bot, sessions);
    chatTitle.textContent = cur.title;
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

  // ── Claude session picker ──
  const picker = $("claude-picker");
  const pickerProjects = $("picker-projects");
  const pickerSessions = $("picker-sessions");
  const pickerPreview = $("picker-preview");
  const pickerCrumb = $("picker-crumb");
  const pickerBack = $("picker-back");
  const pickerCount = $("picker-count");
  const pickerResume = $("picker-resume");
  let picker_state = { project: null, session: null };

  function openPicker() {
    picker.classList.remove("hidden");
    showProjects();
  }
  function closePicker() {
    picker.classList.add("hidden");
    pickerPreview.classList.add("hidden");
    pickerPreview.innerHTML = "";
    picker_state = { project: null, session: null };
  }
  $("open-claude-picker").onclick = openPicker;
  $("picker-close").onclick = closePicker;
  $("picker-cancel").onclick = closePicker;
  pickerBack.onclick = () => {
    if (picker_state.session) { picker_state.session = null; pickerResume.disabled = true; pickerPreview.classList.add("hidden"); pickerPreview.innerHTML = ""; pickerSessions.classList.remove("hidden"); pickerCrumb.textContent = picker_state.project ? picker_state.project.label : ""; }
    else if (picker_state.project) { showProjects(); }
  };

  async function showProjects() {
    picker_state = { project: null, session: null };
    pickerCrumb.textContent = "";
    pickerBack.classList.add("hidden");
    pickerSessions.classList.add("hidden");
    pickerPreview.classList.add("hidden");
    pickerPreview.innerHTML = "";
    pickerResume.disabled = true;
    pickerProjects.classList.remove("hidden");
    pickerProjects.innerHTML = "<li class='muted'>Loading…</li>";
    pickerCount.textContent = "";
    try {
      const r = await api(`claude/projects?machine=${encodeURIComponent(state.botMachine)}`);
      const { projects } = await r.json();
      pickerProjects.innerHTML = "";
      pickerCount.textContent = `${projects.length} projects`;
      for (const p of projects) {
        const li = document.createElement("li");
        li.innerHTML = `<div class="grow"><div class="row1">📁 ${escapeHtml(p.label)}</div><div class="row2">${escapeHtml(p.cwd || p.encoded)}</div></div><span class="meta">${p.session_count} · ${formatTs(p.last_ts)}</span>`;
        li.onclick = () => showSessions(p);
        pickerProjects.appendChild(li);
      }
      if (projects.length === 0) {
        pickerProjects.innerHTML = "<li class='muted'>No Claude sessions found at ~/.claude/projects/</li>";
      }
    } catch (e) {
      pickerProjects.innerHTML = `<li class='muted'>Error: ${escapeHtml(e.message)}</li>`;
    }
  }

  async function showSessions(project) {
    picker_state = { project, session: null };
    pickerProjects.classList.add("hidden");
    pickerSessions.classList.remove("hidden");
    pickerPreview.classList.add("hidden");
    pickerPreview.innerHTML = "";
    pickerResume.disabled = true;
    pickerBack.classList.remove("hidden");
    pickerCrumb.textContent = project.label;
    pickerSessions.innerHTML = "<li class='muted'>Loading…</li>";
    try {
      const r = await api(`claude/sessions?machine=${encodeURIComponent(state.botMachine)}&project=${encodeURIComponent(project.encoded)}`);
      const { sessions } = await r.json();
      pickerSessions.innerHTML = "";
      pickerCount.textContent = `${sessions.length} sessions`;
      for (const s of sessions) {
        const li = document.createElement("li");
        const title = (s.first_user || "(no user message)").trim();
        li.innerHTML = `<div class="grow"><div class="row1">${escapeHtml(title)}</div><div class="row2">${formatTs(s.last_ts)} · ${s.session_id.slice(0, 8)}</div></div><span class="meta">💬 ${s.message_count}</span>`;
        li.onclick = () => selectSession(li, s);
        pickerSessions.appendChild(li);
      }
      if (sessions.length === 0) {
        pickerSessions.innerHTML = "<li class='muted'>(empty)</li>";
      }
    } catch (e) {
      pickerSessions.innerHTML = `<li class='muted'>Error: ${escapeHtml(e.message)}</li>`;
    }
  }

  async function selectSession(li, session) {
    picker_state.session = session;
    for (const x of pickerSessions.querySelectorAll("li")) x.classList.remove("selected");
    li.classList.add("selected");
    pickerResume.disabled = false;
    pickerPreview.classList.remove("hidden");
    pickerPreview.innerHTML = "<div class='muted'>Loading transcript…</div>";
    try {
      const r = await api(`claude/transcript?machine=${encodeURIComponent(state.botMachine)}&project=${encodeURIComponent(picker_state.project.encoded)}&session_id=${encodeURIComponent(session.session_id)}`);
      const { messages } = await r.json();
      pickerPreview.innerHTML = "";
      const tail = messages.slice(-12);
      for (const m of tail) {
        const div = document.createElement("div");
        div.className = "pmsg";
        div.innerHTML = `<span class="role">${m.role}</span>${escapeHtml((m.text || "").slice(0, 240))}${m.text.length > 240 ? "…" : ""}`;
        pickerPreview.appendChild(div);
      }
    } catch (e) {
      pickerPreview.innerHTML = `<div class='muted'>Preview failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  pickerResume.onclick = async () => {
    if (!picker_state.session || !state.bot) return;
    pickerResume.disabled = true;
    pickerResume.textContent = "Resuming…";
    const raw = $("picker-raw") && $("picker-raw").checked;
    const resumeBot = raw ? "raw" : state.bot;
    const resumeMachine = raw ? state.botMachine : state.botMachine;
    try {
      const r = await api("claude/resume", {
        method: "POST",
        body: JSON.stringify({
          bot: resumeBot,
          machine: resumeMachine,
          project: picker_state.project.encoded,
          session_id: picker_state.session.session_id,
          backend: raw ? "claude-cli" : undefined,
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      const { chat_id } = await r.json();
      const sessions = loadSessions(resumeMachine, resumeBot);
      sessions[chat_id] = {
        title: `${raw ? "Raw" : "Claude"} · ${picker_state.project.label}`,
        preview: picker_state.session.first_user || "",
        ts: Date.now(),
      };
      saveSessions(resumeMachine, resumeBot, sessions);
      // Switch active bot if we routed to raw
      if (raw && state.bot !== "raw") {
        state.bot = "raw";
        state.botMachine = resumeMachine;
      }
      const key = curKey();
      state.sessions[key] = sessions;
      state.serverSessions[key] = await fetchServerSessions(resumeMachine, resumeBot);
      closePicker();
      await switchChat(chat_id);
    } catch (e) {
      alert("Resume failed: " + e.message);
    } finally {
      pickerResume.disabled = false;
      pickerResume.textContent = "Resume";
    }
  };

  function formatTs(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString();
  }
})();
