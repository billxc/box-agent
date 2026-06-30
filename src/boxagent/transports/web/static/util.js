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
})();
