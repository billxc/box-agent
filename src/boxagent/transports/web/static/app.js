/* BoxAgent Web Chat — 原生 JS，无构建步骤 */
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

  // 按会话记住已关闭的 recap（浏览器本地）。用户关掉后，除非 recap 文本变化，
  // 否则该 chat 不再显示。<recap-banner> 负责关闭/折叠/持久化；
  // 这里只提供 recap 文本 + 一个 machine|bot|chat 作用域的 key。
  function showRecapBanner(recap, chatId) {
    recapBanner.show(recap, "ba.recap-dismissed." + curKey() + "|" + chatId);
  }

  const state = {
    machines: [],         // [{machine_id, online, role, self, bots, last_seen}]
    bot: null,            // 当前选中的 bot 名
    botMachine: null,     // 当前 bot 所在的 machine_id（用于显示）
    sessions: {},         // "machine|bot" -> {chat_id: {title, preview, ts}}（浏览器本地）
    serverSessions: {},   // "machine|bot" -> [{chat_id, platform, preview, last_ts, ...}]
    chatId: null,
    subscribed: null,     // 当前订阅到 multiplex socket 的 {machine, bot, chat_id}
    streamMsgs: {},
    refreshTimer: null,
    historyOffset: 0,     // 已从历史末尾加载的条目数
    historyTotal: 0,      // 服务端报告的总条目数
    historyLoading: false,
    historyExhausted: false,
  };
  const HISTORY_PAGE_SIZE = 50;

  // ── 辅助函数 ──
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

  // ── 最近记录（跨 bot，浏览器本地）— 组件：components/recents-panel.js
  // <recents-panel> 负责 localStorage 数据 + 列表渲染。这里注入当前选中项
  // （用于标记 active/offline），并通过下面的导航处理它的 "open"。
  const recents = $("recents");
  recents.getContext = () => ({
    machines: state.machines,
    bot: state.bot,
    botMachine: state.botMachine,
    chatId: state.chatId,
  });
  recents.onOpen = openRecent;

  // 打开一条最近记录：必要时切换 bot+machine，再打开该 chat。
  // 目标机器离线时弹提示。
  async function openRecent(r) {
    const m = state.machines.find(x => x.machine_id === r.machine);
    if (m && !m.online) {
      alert(`${r.machine} is offline`);
      return;
    }
    if (state.bot !== r.bot || state.botMachine !== r.machine) {
      // selectBot 会加载会话并打开上次的 chat；这里改写 ba.last，
      // 让它落到这条最近记录的 chat 上。
      localStorage.setItem("ba.last." + botKey(r.machine, r.bot), r.chat_id);
      await selectBot(r.bot, r.machine);
    } else if (state.chatId !== r.chat_id) {
      await app.switchChat(r.chat_id);
      closeSidebar();
    } else {
      closeSidebar();
    }
  }

  // loadSessions / saveSessions / buildSessionList / defaultTitle / shortId
  // 在 session-data.js（纯函数，挂为全局）。

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

  // ── 会话 ──
  // buildSessionList(local, server) / defaultTitle / shortId 在 session-data.js。
  // 把当前 bot 的 local+server map 合并、排序成条目列表。
  function currentSessionEntries() {
    return buildSessionList(state.sessions[curKey()] || {}, state.serverSessions[curKey()] || []);
  }

  function refreshSessionList() {
    // <sessions-panel> 负责渲染；app.js 负责 server+local 合并。
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

  // touchSession / refreshSessionInfo / loadOlderHistory / switchChat /
  // renderToolCall·renderToolResult / sendText 及消息代理都在
  // chat-controller.js（下面构造的 ChatController(app)）。app.js 调用
  // app.switchChat / app.sendText / app.addMessage。

  // ── 机器 + bot 分组 ──
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
    // 首次加载自动选一个默认 bot
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
      // 当前 bot 所在机器离线则标记为 stale
      const m = state.machines.find(m => m.machine_id === state.botMachine);
      if (m && !m.online) {
        chatTitle.textContent = `${state.bot} @ ${state.botMachine} · offline`;
      }
    }
  }

  function renderMachines() {
    // <machines-panel> 渲染树 + 负责折叠；app.js 负责数据。
    machinesPanel.render(state.machines, { bot: state.bot, botMachine: state.botMachine });
  }

  async function selectBot(botName, machineId) {
    // 整个切换过程遮住 chat 面板，避免用户看到旧 bot 的会话列表和历史被
    // 拉取时的空/过期内容。switchChat() 会在自身 swap 期间继续保持遮罩。
    $("messages-mask").classList.remove("hidden");
    // 侧栏会话列表也显示 loading 占位，拉取期间不留旧 bot 的会话。
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
    await app.switchChat(lastChat || pickFirstSessionId() || uuid());
    closeSidebar();
  }

  // ── App context ──
  // 拆出去的 controller 读取并挂接到这个共享 bag（无构建步骤）。
  // chat-controller.js（ChatController）负责整个实时会话 controller；
  // 它读取这些，并挂上 app.switchChat / app.sendText / app.addMessage /
  // app.openStream / app.handleEvent + 接线 chatLog.onLoadOlder。
  const app = {
    state, api, $, TOKEN, HISTORY_PAGE_SIZE,
    curKey, setConn, showRecapBanner,
    chatTitle, sessionInfoEl, sendBtn, chatLog, recents,
    refreshSessionList, fetchServerSessions, loadMachines,
  };
  // 一个页面级 multiplex WebSocket；controller 通过 app.multiplex
  // 增删每个 chat 的订阅，而不是为每个 chat 各开一个 socket。
  MultiplexClient(app);
  ChatController(app);

  // ── UI 事件 ──
  $("refresh-machines").onclick = () => loadMachines().catch(() => {});
  $("restart-all").onclick = () => restartCluster();
  // 面板通过注入的回调传达意图；app.js 负责导航 + 动作。
  machinesPanel.onSelectBot = selectBot;
  machinesPanel.onRestartMachine = restartMachine;
  sessionsPanel.onSelectSession = (chatId) => { app.switchChat(chatId); closeSidebar(); };
  $("recents-clear").onclick = () => recents.clear();
  // 分区折叠（caret + 持久化状态）。两个侧栏分区形状一致，共用一套逻辑。
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

  // ── 侧栏拖拽调宽 ── 组件：sidebar-resize.js（自包含行为）

  $("new-session").onclick = async () => {
    await app.switchChat(uuid());
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
      // 没有后端 session（全新 web chat）— 退化为本地重命名。
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
    app.sendText(text);
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

  // ── 定期刷新 ──
  function startMachinePoll() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(() => {
      loadMachines().catch(() => { /* 网络抖动；UI 保持原样 */ });
    }, 15000);
  }

  // ── 启动 ──
  recents.render();  // machines 返回前先从 localStorage 填充
  loadMachines().then(startMachinePoll).catch((e) => {
    console.error(e);
    setConn("offline");
    app.addMessage("assistant", "_Failed to connect: " + e.message + "_");
  });

  // ── Claude session 选择器（组件：components/session-picker.js）──
  // <session-picker> 负责弹窗 + 所有 Claude-resume HTTP + 分页。
  // 这里注入 HTTP 封装 + 当前 bot 上下文；resume 成功时它发出 "resumed"，
  // 我们更新本地 session 缓存并导航（这些 app state 组件不碰）。
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
    await app.switchChat(chat_id);
  };
  $("open-claude-picker").onclick = () => picker.open();
})();
