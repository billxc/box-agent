// Shared pure helpers used by app.js and the Web Components. No build step —
// exposed as globals (bare `escapeHtml(...)` calls in app.js resolve here once
// the local copies are removed; cf. the `marked` / `BoxAgentTheme` globals).
(function () {
  "use strict";

  window.escapeHtml = function (value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  };

  window.renderMarkdown = function (text) {
    try {
      return window.marked
        ? window.marked.parse(text, { breaks: true, gfm: true })
        : window.escapeHtml(text);
    } catch {
      return window.escapeHtml(text);
    }
  };

  // Single-glyph icon for a chat platform.
  window.platformIcon = function (platform) {
    return ({ telegram: "✈︎", web: "◉", claude: "✦", other: "•", unknown: "•" })[platform] || "•";
  };

  // Coarse "N{s,m,h,d} ago" from a unix-seconds timestamp.
  window.formatRelative = function (ts) {
    if (!ts) return "";
    const diff = Math.floor(Date.now() / 1000 - ts);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  };
})();
