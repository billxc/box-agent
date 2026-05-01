/* BoxAgent Web Chat — vanilla JS, no build step */
(() => {
  const qs = new URLSearchParams(location.search);
  const TOKEN = qs.get("token") || localStorage.getItem("ba.token") || "";
  if (qs.get("token")) localStorage.setItem("ba.token", TOKEN);

  const $ = (id) => document.getElementById(id);
  const messagesEl = $("messages");
  const botSelect = $("bot-select");
  const sessionList = $("session-list");
  const chatTitle = $("chat-title");
  const composer = $("composer");
  const input = $("input");
  const sendBtn = $("send");
  const connDot = $("conn-state");
  const connLabel = $("conn-label");
  const sidebar = $("sidebar");

  const state = {
    bots: [],
    bot: null,
    sessions: {}, // bot -> {chat_id: {title, preview, ts}}  (local, browser-side)
    serverSessions: {}, // bot -> [{chat_id, platform, preview, last_ts, ...}]
    chatId: null,
    es: null,
    streamMsgs: {}, // message_id -> {el, text}
    typingEl: null,
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

  function loadSessions(bot) {
    try {
      return JSON.parse(localStorage.getItem("ba.sessions." + bot) || "{}");
    } catch { return {}; }
  }
  function saveSessions(bot, sessions) {
    localStorage.setItem("ba.sessions." + bot, JSON.stringify(sessions));
  }

  async function fetchServerSessions(bot) {
    try {
      const r = await api(`sessions?bot=${encodeURIComponent(bot)}`);
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

  function addMessage(role, text, opts = {}) {
    removeTyping();
    const el = document.createElement("div");
    el.className = "msg " + role;
    const md = document.createElement("div");
    md.className = "md";
    md.innerHTML = role === "user" ? escapeHtml(text).replace(/\n/g, "<br>") : renderMd(text);
    el.appendChild(md);
    if (opts.id) el.dataset.id = opts.id;
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
    const local = state.sessions[state.bot] || {};
    const server = state.serverSessions[state.bot] || [];
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
    const sessions = state.sessions[state.bot] || {};
    const cur = sessions[state.chatId] || { title: "Chat " + new Date().toLocaleString() };
    cur.preview = preview.slice(0, 60);
    cur.ts = Date.now();
    sessions[state.chatId] = cur;
    state.sessions[state.bot] = sessions;
    saveSessions(state.bot, sessions);
    refreshSessionList();
    chatTitle.textContent = cur.title;
  }

  // ── Chat lifecycle ──
  async function switchChat(chatId) {
    if (state.es) { state.es.close(); state.es = null; }
    state.chatId = chatId;
    state.streamMsgs = {};
    messagesEl.innerHTML = "";
    refreshSessionList();
    const meta = (state.sessions[state.bot] || {})[chatId] || {};
    chatTitle.textContent = meta.title || chatId;
    localStorage.setItem("ba.last." + state.bot, chatId);

    // Load history
    setConn("connecting");
    try {
      const r = await api(`history?bot=${encodeURIComponent(state.bot)}&chat_id=${encodeURIComponent(chatId)}`);
      if (r.ok) {
        const { history } = await r.json();
        for (const h of history) addMessage(h.role, h.text);
      }
    } catch (e) { console.warn("history load failed", e); }

    // Open SSE stream
    openStream();
  }

  function openStream() {
    const url = `api/stream?bot=${encodeURIComponent(state.bot)}&chat_id=${encodeURIComponent(state.chatId)}` + (TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : "");
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
        fetchServerSessions(state.bot).then((list) => {
          state.serverSessions[state.bot] = list;
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
    sendBtn.disabled = true;
    try {
      const r = await api("send", {
        method: "POST",
        body: JSON.stringify({ bot: state.bot, chat_id: state.chatId, text }),
      });
      if (!r.ok) {
        const err = await r.text();
        addMessage("assistant", "_Error: " + err + "_");
      }
    } catch (e) {
      addMessage("assistant", "_Network error: " + e.message + "_");
    } finally {
      sendBtn.disabled = false;
    }
  }

  // ── Bot picker ──
  async function loadBots() {
    setConn("connecting");
    const r = await api("bots");
    if (!r.ok) {
      if (r.status === 401) {
        const t = prompt("Enter access token:");
        if (t) { localStorage.setItem("ba.token", t); location.reload(); }
        return;
      }
      throw new Error("bots fetch " + r.status);
    }
    const { bots } = await r.json();
    state.bots = bots;
    botSelect.innerHTML = "";
    for (const b of bots) {
      const opt = document.createElement("option");
      opt.value = b.name;
      opt.textContent = b.display_name || b.name;
      botSelect.appendChild(opt);
    }
    if (bots.length === 0) {
      addMessage("assistant", "No web-enabled bots configured. Add `channels.web: true` in config.yaml.");
      return;
    }
    const lastBot = localStorage.getItem("ba.lastBot");
    state.bot = (lastBot && bots.find(b => b.name === lastBot)) ? lastBot : bots[0].name;
    botSelect.value = state.bot;
    state.sessions[state.bot] = loadSessions(state.bot);
    state.serverSessions[state.bot] = await fetchServerSessions(state.bot);
    const lastChat = localStorage.getItem("ba.last." + state.bot);
    const chatId = lastChat || pickFirstSessionId() || uuid();
    await switchChat(chatId);
  }

  function pickFirstSessionId() {
    const list = buildSessionList();
    return list.length ? list[0].chat_id : null;
  }

  botSelect.addEventListener("change", async () => {
    state.bot = botSelect.value;
    localStorage.setItem("ba.lastBot", state.bot);
    state.sessions[state.bot] = loadSessions(state.bot);
    state.serverSessions[state.bot] = await fetchServerSessions(state.bot);
    const lastChat = localStorage.getItem("ba.last." + state.bot);
    await switchChat(lastChat || pickFirstSessionId() || uuid());
  });

  // ── UI events ──
  $("new-session").onclick = async () => {
    await switchChat(uuid());
    closeSidebar();
    input.focus();
  };

  $("rename-session").onclick = () => {
    if (!state.chatId) return;
    const sessions = state.sessions[state.bot] || {};
    const cur = sessions[state.chatId] || {};
    const t = prompt("Rename session:", cur.title || "");
    if (t == null) return;
    cur.title = t.trim() || cur.title;
    sessions[state.chatId] = cur;
    state.sessions[state.bot] = sessions;
    saveSessions(state.bot, sessions);
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

  // ── Boot ──
  loadBots().catch((e) => {
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
      const r = await api("claude/projects");
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
      const r = await api(`claude/sessions?project=${encodeURIComponent(project.encoded)}`);
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
      const r = await api(`claude/transcript?project=${encodeURIComponent(picker_state.project.encoded)}&session_id=${encodeURIComponent(session.session_id)}`);
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
          project: picker_state.project.encoded,
          session_id: picker_state.session.session_id,
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      const { chat_id } = await r.json();
      // Tag the local session list so the title reads nicely
      const sessions = loadSessions(state.bot);
      sessions[chat_id] = {
        title: `Claude · ${picker_state.project.label}`,
        preview: picker_state.session.first_user || "",
        ts: Date.now(),
      };
      saveSessions(state.bot, sessions);
      state.sessions[state.bot] = sessions;
      // Refresh server-side list and switch into the resumed chat
      state.serverSessions[state.bot] = await fetchServerSessions(state.bot);
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
