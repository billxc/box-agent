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
    chatId: null,
    es: null,
    streamMsgs: {},
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
    return ({ telegram: "✈︎", discord: "◈", web: "◉", heartbeat: "♥", claude: "✦", other: "•", unknown: "•" })[p] || "•";
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
    if (opts.id) el.dataset.id = opts.id;
    return el;
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
    if (s.platform === "heartbeat") {
      const wg = s.chat_id.startsWith("heartbeat:") ? s.chat_id.slice("heartbeat:".length) : "admin";
      return `♥ Heartbeat · ${wg}`;
    }
    if (s.platform === "claude") return `✦ Resumed Claude session`;
    const tag = ({ telegram: "Telegram", discord: "Discord", web: "Web", other: "Chat" })[s.platform] || "Chat";
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
      title.innerHTML = `<span class="plat" title="${meta.platform}">${platformIcon(meta.platform)}</span> ${escapeHtml(meta.title)}`;
      const preview = document.createElement("div");
      preview.className = "sess-preview";
      preview.textContent = meta.preview || "(no messages yet)";
      li.appendChild(title); li.appendChild(preview);
      li.onclick = () => { switchChat(meta.chat_id); closeSidebar(); };
      sessionList.appendChild(li);
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
  }

  // ── Chat lifecycle ──
  async function switchChat(chatId) {
    if (state.es) { state.es.close(); state.es = null; }
    state.chatId = chatId;
    state.streamMsgs = {};
    refreshSessionList();
    const meta = (state.sessions[curKey()] || {})[chatId] || {};
    chatTitle.textContent = meta.title || chatId;
    localStorage.setItem("ba.last." + curKey(), chatId);

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
      const el = buildMessage(h.role, h.text);
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
        addMessage(ev.role || "assistant", ev.text || "", { id: ev.message_id });
        if (ev.role === "assistant") touchSession(ev.text || "");
        else touchSession(ev.text || "");
        break;
      }
      case "typing": showTyping(); break;
      case "stream_start": {
        removeTyping();
        const el = addMessage("assistant", "", { id: ev.message_id });
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
      case "_close": setConn("offline"); break;
    }
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
          // Likely the satellite dropped — refresh machines so UI reflects it.
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
        ${m.role && m.role !== "satellite" ? `<span class="role">${m.role}</span>` : ""}
        <span class="last">${lastSeen}</span>
      `;
      head.onclick = () => toggleMachine(m.machine_id);
      li.appendChild(head);

      const bots = document.createElement("ul");
      bots.className = "machine-bots";
      for (const b of (m.bots || [])) {
        const bli = document.createElement("li");
        if (state.bot === b.name && state.botMachine === m.machine_id) bli.classList.add("active");
        bli.innerHTML = `<span>${escapeHtml(b.display_name || b.name)}</span><span class="kind">${b.kind || "bot"}</span>`;
        bli.onclick = (e) => {
          e.stopPropagation();
          if (!m.online) { alert(`Machine ${m.machine_id} is offline`); return; }
          selectBot(b.name, m.machine_id);
        };
        bots.appendChild(bli);
      }
      if (!(m.bots || []).length) {
        bots.innerHTML = "<li class='muted' style='cursor:default;'>(no bots)</li>";
      }
      li.appendChild(bots);
      machineList.appendChild(li);
    }
  }

  function toggleMachine(mid) {
    if (state.collapsed.has(mid)) state.collapsed.delete(mid);
    else state.collapsed.add(mid);
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
    try {
      const r = await api("claude/resume", {
        method: "POST",
        body: JSON.stringify({
          bot: state.bot,
          machine: state.botMachine,
          project: picker_state.project.encoded,
          session_id: picker_state.session.session_id,
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      const { chat_id } = await r.json();
      const key = curKey();
      const sessions = loadSessions(state.botMachine, state.bot);
      sessions[chat_id] = {
        title: `Claude · ${picker_state.project.label}`,
        preview: picker_state.session.first_user || "",
        ts: Date.now(),
      };
      saveSessions(state.botMachine, state.bot, sessions);
      state.sessions[key] = sessions;
      state.serverSessions[key] = await fetchServerSessions(state.botMachine, state.bot);
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
