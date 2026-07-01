// machines-controller.js — the cluster machine + bot-selection controller,
// factored out of app.js. Same app-context pattern as chat-controller.js:
// app.js builds `app` and calls MachinesController(app); this attaches
// app.loadMachines / app.selectBot / app.restartMachine / app.restartCluster /
// app.startMachinePoll and reads everything else through `app.*`.
//
// Reads from `app`: state, api, machinesPanel, recents, chatTitle, $,
// sessionsPanel, sessionsOf, fetchServerSessions, switchChat (the chat
// controller — selectBot ends by opening a chat), botKey, pickFirstSessionId,
// uuid, closeSidebar. Global from session-data.js: loadSessions.
//
// Injected deps make it unit-testable (mock api + spy switchChat/panels) —
// see machines-controller.test.js.
(function () {
  "use strict";

  function MachinesController(app) {
    const state = app.state;

    function renderMachines() {
      // <machines-panel> renders the tree + owns collapse; we own the data.
      app.machinesPanel.render(state.machines, { bot: state.bot, botMachine: state.botMachine });
    }

    async function loadMachines() {
      const r = await app.api("machines");
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
      app.recents.render();
      // Auto-pick a default bot on first load.
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
        // Mark current bot as stale if its machine went offline.
        const m = state.machines.find((m) => m.machine_id === state.botMachine);
        if (m && !m.online) {
          app.chatTitle.textContent = `${state.bot} @ ${state.botMachine} · offline`;
        }
      }
    }

    async function selectBot(botName, machineId) {
      // Mask the chat panel + show a session-list placeholder while we fetch
      // the new bot's sessions + history (switchChat keeps the mask up).
      app.$("messages-mask").classList.remove("hidden");
      app.sessionsPanel.showLoading();
      state.bot = botName;
      state.botMachine = machineId;
      localStorage.setItem("ba.lastBot", botName);
      localStorage.setItem("ba.lastBotMachine", machineId);
      app.sessionsOf.textContent = `· ${botName} @ ${machineId}`;
      const key = app.botKey(machineId, botName);
      state.sessions[key] = loadSessions(machineId, botName);
      state.serverSessions[key] = await app.fetchServerSessions(machineId, botName);
      renderMachines();
      const lastChat = localStorage.getItem("ba.last." + key);
      await app.switchChat(lastChat || app.pickFirstSessionId() || app.uuid());
      app.closeSidebar();
    }

    async function restartMachine(machineId, online) {
      if (!online) { alert(`${machineId} is offline; nothing to restart`); return; }
      if (!confirm(`Restart ${machineId}? Supervisor (easy-service) will relaunch.`)) return;
      try {
        const r = await app.api("admin/cluster_restart", {
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
        const r = await app.api("admin/cluster_restart", { method: "POST" });
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

    function startMachinePoll() {
      if (state.refreshTimer) clearInterval(state.refreshTimer);
      state.refreshTimer = setInterval(() => {
        loadMachines().catch(() => { /* network blip; UI stays as-is */ });
      }, 15000);
    }

    app.loadMachines = loadMachines;
    app.selectBot = selectBot;
    app.restartMachine = restartMachine;
    app.restartCluster = restartCluster;
    app.startMachinePoll = startMachinePoll;
  }

  window.MachinesController = MachinesController;
})();
