// <recents-panel> — cross-bot "Recent chats" list, as a self-contained custom
// element.
//
// Native Web Component, no framework / no build step. Owns its browser-local
// data (localStorage["ba.recents"], capped + newest-first) and the list
// rendering. Light DOM with an inner `<ul id="recents-list">` so the existing
// `#recents-list*` rules in style.css apply unchanged.
//
// app.js injects the current-selection context (for active/offline marking)
// and an onOpen callback:
//   panel.getContext = () => ({ machines, bot, botMachine, chatId })
//   panel.onOpen(entry)  → app.js does the navigation
//        (selectBot / switchChat / closeSidebar — app state it doesn't touch).
//
// Public methods: touch(patch), remove(machine, bot, chatId), clear(), render().
(function () {
  "use strict";

  const STORAGE_KEY = "ba.recents";
  const RECENTS_MAX = 25;

  function recentKey(machine, bot, chatId) {
    return `${machine}|${bot}|${chatId}`;
  }

  class RecentsPanel extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "contents"; // host transparent; CSS targets #recents-list
      this._list = document.createElement("ul");
      this._list.id = "recents-list";
      this.appendChild(this._list);
      this.render();
    }

    // ── Data (browser-local) ──

    _load() {
      try {
        const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
        return Array.isArray(raw) ? raw : [];
      } catch {
        return [];
      }
    }

    _save(arr) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(arr.slice(0, RECENTS_MAX)));
    }

    // Bump a chat to the top. Called on switchChat + every incoming/outgoing
    // message so the most-active chat sits first.
    touch(patch) {
      if (!patch.machine || !patch.bot || !patch.chat_id) return;
      const key = recentKey(patch.machine, patch.bot, patch.chat_id);
      const all = this._load();
      const idx = all.findIndex((r) => recentKey(r.machine, r.bot, r.chat_id) === key);
      const prev = idx >= 0 ? all[idx] : {};
      if (idx >= 0) all.splice(idx, 1);
      all.unshift({
        machine: patch.machine,
        bot: patch.bot,
        chat_id: patch.chat_id,
        title: patch.title || prev.title || patch.chat_id,
        preview: patch.preview != null ? patch.preview : (prev.preview || ""),
        recap: patch.recap != null ? patch.recap : (prev.recap || ""),
        platform: patch.platform || prev.platform || "unknown",
        display_name: patch.display_name || prev.display_name || patch.bot,
        ts: patch.ts || Math.floor(Date.now() / 1000),
      });
      this._save(all);
      this.render();
    }

    remove(machine, bot, chatId) {
      const key = recentKey(machine, bot, chatId);
      this._save(this._load().filter((r) => recentKey(r.machine, r.bot, r.chat_id) !== key));
      this.render();
    }

    clear() {
      if (!confirm("Clear all recent chats from this browser?")) return;
      this._save([]);
      this.render();
    }

    // ── Render ──

    render() {
      const ul = this._list;
      if (!ul) return; // not built yet
      const ctx = (this.getContext && this.getContext()) || {};
      const machines = ctx.machines || [];
      ul.innerHTML = "";
      const all = this._load();
      if (all.length === 0) {
        const li = document.createElement("li");
        li.style.color = "var(--muted)";
        li.style.cursor = "default";
        li.textContent = "No recent chats";
        ul.appendChild(li);
        return;
      }
      const onlineMachines = new Set(machines.filter((m) => m.online).map((m) => m.machine_id));
      for (const r of all) {
        const li = document.createElement("li");
        const isActive = ctx.bot === r.bot && ctx.botMachine === r.machine && ctx.chatId === r.chat_id;
        if (isActive) li.classList.add("active");
        if (machines.length && !onlineMachines.has(r.machine)) li.classList.add("offline");

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
          this.remove(r.machine, r.bot, r.chat_id);
        };

        li.appendChild(plat);
        li.appendChild(body);
        li.appendChild(time);
        li.appendChild(del);
        li.onclick = () => this.onOpen?.(r);
        ul.appendChild(li);
      }
    }
  }

  customElements.define("recents-panel", RecentsPanel);
  window.RecentsPanel = RecentsPanel;
})();
