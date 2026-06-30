// <machines-panel> — cluster machine/bot tree, as a self-contained custom
// element.
//
// Native Web Component, no framework / no build step. Light DOM with an inner
// `<ul id="machine-list">` so the existing `#machine-list*` / `.machine*` rules
// in style.css apply unchanged. Owns the per-machine collapse state
// (localStorage["ba.collapsedMachines"]).
//
// app.js drives it and handles the side-effects:
//   panel.render(machines, { bot, botMachine })   // data lives in app state
//   "select-bot"     {detail:{bot, machine}}   → app.js selectBot()
//   "restart-machine"{detail:{machine, online}}→ app.js restartMachine()
// (the offline-guard alert is handled here; select-bot only fires when online).
(function () {
  "use strict";

  const COLLAPSE_KEY = "ba.collapsedMachines";

  class MachinesPanel extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "contents"; // host transparent; CSS targets #machine-list
      this._list = document.createElement("ul");
      this._list.id = "machine-list";
      this.appendChild(this._list);
      this._collapsed = new Set(this._loadCollapsed());
      this._machines = [];
      this._ctx = {};
    }

    _loadCollapsed() {
      try {
        const raw = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]");
        return Array.isArray(raw) ? raw : [];
      } catch {
        return [];
      }
    }

    _toggle(machineId) {
      if (this._collapsed.has(machineId)) this._collapsed.delete(machineId);
      else this._collapsed.add(machineId);
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...this._collapsed]));
      this._render();
    }

    render(machines, ctx) {
      this._machines = machines || [];
      this._ctx = ctx || {};
      this._render();
    }

    _render() {
      const list = this._list;
      if (!list) return;
      list.innerHTML = "";
      if (this._machines.length === 0) {
        list.innerHTML = "<li class='muted'>No machines</li>";
        return;
      }
      const ctx = this._ctx;
      for (const m of this._machines) {
        const li = document.createElement("li");
        li.className = "machine" + (m.online ? "" : " offline") + (this._collapsed.has(m.machine_id) ? " collapsed" : "");
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
            this.dispatchEvent(new CustomEvent("restart-machine", { detail: { machine: m.machine_id, online: m.online } }));
            return;
          }
          this._toggle(m.machine_id);
        };
        li.appendChild(head);

        const bots = document.createElement("ul");
        bots.className = "machine-bots";
        for (const b of (m.bots || [])) {
          const botLi = document.createElement("li");
          if (ctx.bot === b.name && ctx.botMachine === m.machine_id) {
            botLi.classList.add("active");
          }
          botLi.innerHTML = `<span>${escapeHtml(b.display_name || b.name)}</span><span class="kind">${b.kind || "bot"}</span>`;
          botLi.onclick = (e) => {
            e.stopPropagation();
            if (!m.online) { alert(`Machine ${m.machine_id} is offline`); return; }
            this.dispatchEvent(new CustomEvent("select-bot", { detail: { bot: b.name, machine: m.machine_id } }));
          };
          bots.appendChild(botLi);
        }
        if (!(m.bots || []).length) {
          bots.innerHTML = "<li class='muted' style='cursor:default;'>(no bots)</li>";
        }
        li.appendChild(bots);
        list.appendChild(li);
      }
    }
  }

  customElements.define("machines-panel", MachinesPanel);
  window.MachinesPanel = MachinesPanel;
})();
