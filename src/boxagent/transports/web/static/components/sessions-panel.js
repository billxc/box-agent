// <sessions-panel> — chat session list for the current bot, as a self-contained
// custom element.
//
// Native Web Component, no framework / no build step. Light DOM with an inner
// `<ul id="session-list">` so the existing `#session-list*` / `.session-*`
// rules in style.css apply unchanged. Pure presentation: app.js owns the
// session data (the server+local merge in buildSessionList) and navigation.
//
//   panel.render(entries, { chatId })   // entries = merged session list
//   panel.showLoading()                 // placeholder during a bot switch
//   panel.onSelectSession(chat_id)      → app.js switchChat() + closeSidebar()
(function () {
  "use strict";

  class SessionsPanel extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "contents"; // host transparent; CSS targets #session-list
      this._list = document.createElement("ul");
      this._list.id = "session-list";
      this.appendChild(this._list);
    }

    showLoading() {
      if (this._list) {
        this._list.innerHTML = "<li class='muted' style='cursor:default;'>Loading sessions…</li>";
      }
    }

    render(entries, ctx) {
      const list = this._list;
      if (!list) return;
      ctx = ctx || {};
      list.innerHTML = "";
      entries = entries || [];
      if (entries.length === 0) {
        const li = document.createElement("li");
        li.style.color = "var(--muted)";
        li.style.cursor = "default";
        li.textContent = "No sessions yet — start chatting";
        list.appendChild(li);
        return;
      }
      for (const meta of entries) {
        const li = document.createElement("li");
        if (meta.chat_id === ctx.chatId) li.classList.add("active");
        const title = document.createElement("div");
        title.className = "session-title";
        title.innerHTML = `<span class="plat" title="${meta.platform}">${platformIcon(meta.platform)}</span> ${escapeHtml(meta.title)}`;
        const preview = document.createElement("div");
        preview.className = "session-preview";
        preview.textContent = meta.preview || "(no messages yet)";
        li.appendChild(title);
        li.appendChild(preview);
        if (meta.recap) {
          const recap = document.createElement("div");
          recap.className = "session-recap";
          recap.textContent = meta.recap;
          recap.title = meta.recap;
          li.appendChild(recap);
        }
        li.onclick = () => this.onSelectSession?.(meta.chat_id);
        list.appendChild(li);
      }
    }
  }

  customElements.define("sessions-panel", SessionsPanel);
  window.SessionsPanel = SessionsPanel;
})();
