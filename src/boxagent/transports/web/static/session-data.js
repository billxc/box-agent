// Session-list data helpers shared by app.js. Pure — localStorage I/O plus the
// server/local merge; no app state, no DOM. Exposed as globals like util.js
// (bare `buildSessionList(...)` / `loadSessions(...)` calls in app.js resolve
// here). The network fetch (fetchServerSessions) stays in app.js — it needs the
// app's authenticated fetch wrapper.
(function () {
  "use strict";

  function botKey(machine, bot) {
    return `${machine}|${bot}`;
  }

  function loadSessions(machine, bot) {
    try {
      return JSON.parse(localStorage.getItem("ba.sessions." + botKey(machine, bot)) || "{}");
    } catch {
      return {};
    }
  }

  function saveSessions(machine, bot, sessions) {
    localStorage.setItem("ba.sessions." + botKey(machine, bot), JSON.stringify(sessions));
  }

  function shortId(cid) {
    return cid.length > 12 ? cid.slice(0, 6) + "…" + cid.slice(-4) : cid;
  }

  function defaultTitle(s) {
    if (s.platform === "claude") return `✦ Resumed Claude session`;
    const tag = ({ telegram: "Telegram", web: "Web", other: "Chat" })[s.platform] || "Chat";
    return `${tag} · ${shortId(s.chat_id)}`;
  }

  // Merge server sessions (cross-platform) with local ones (web-only, may carry
  // user-renamed titles). Pure: caller passes the local map + server array.
  // Returns entries newest-first.
  function buildSessionList(local, server) {
    local = local || {};
    server = server || [];
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
    // Local-only entries (brand-new web chats with no transcript yet).
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

  window.loadSessions = loadSessions;
  window.saveSessions = saveSessions;
  window.shortId = shortId;
  window.defaultTitle = defaultTitle;
  window.buildSessionList = buildSessionList;
})();
