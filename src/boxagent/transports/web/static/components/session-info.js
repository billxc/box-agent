// <session-info> — the one-line backend/context summary under the chat title.
//
// Owns its rendering + the token formatting. The caller fetches the info object
// (app state / API) and hands it over; all formatting lives here. Browser APIs
// only.
//
// Usage: `document.getElementById("session-info").setInfo(info | null)`.
(function () {
  "use strict";

  function fmtTokens(n) {
    if (!n) return "0";
    if (n >= 1000) return Math.floor(n / 1000) + "k";
    return String(n);
  }

  class SessionInfo extends HTMLElement {
    setInfo(info) {
      if (!info) {
        this.classList.add("hidden");
        this.textContent = "";
        return;
      }
      const parts = [];
      if (info.backend_kind) parts.push(info.backend_kind);
      if (info.context_window && info.context_used) {
        const pct = Math.round((info.context_used / info.context_window) * 100);
        parts.push(`ctx ${fmtTokens(info.context_used)}/${fmtTokens(info.context_window)} (${pct}%)`);
      }
      this.textContent = parts.join(" · ");
      this.classList.toggle("hidden", parts.length === 0);
    }
  }

  customElements.define("session-info", SessionInfo);
  window.SessionInfo = SessionInfo;
})();
