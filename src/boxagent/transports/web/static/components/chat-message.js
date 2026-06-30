// <chat-message> — a chat bubble (user / assistant) as a custom element.
//
// Native Web Component, no framework / no build step. Owns the bubble DOM,
// markdown rendering, and timestamp. Streaming updates call `.setText(full)`
// instead of reaching into `.querySelector(".markdown")`. Depends only on the
// shared `window.renderMarkdown` / `window.escapeHtml` (util.js) + browser APIs.
//
// Usage: `ChatMessage.create(role, text, { id, ts })` → element you append.
(function () {
  "use strict";

  // HH:MM if today, else MM-DD HH:MM. 24h, compact.
  function fmtTime(date) {
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
    const pad = (n) => String(n).padStart(2, "0");
    const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return sameDay ? hm : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hm}`;
  }

  class ChatMessage extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.classList.add("msg", this._role || "assistant");
      this._markdown = document.createElement("div");
      this._markdown.className = "markdown";
      this.appendChild(this._markdown);
      this._timeEl = document.createElement("time");
      this._timeEl.className = "msg-time";
      this.appendChild(this._timeEl);
      this._renderText();
      this._renderTime();
    }

    setText(text) {
      this._text = text;
      this._renderText();
    }

    _renderText() {
      if (!this._built) return;
      const text = this._text || "";
      this._markdown.innerHTML = this._role === "user"
        ? window.escapeHtml(text).replace(/\n/g, "<br>")
        : window.renderMarkdown(text);
    }

    _renderTime() {
      if (!this._built) return;
      const sec = (this._ts && this._ts > 0) ? this._ts : (Date.now() / 1000);
      const date = new Date(sec * 1000);
      this._timeEl.textContent = fmtTime(date);
      this._timeEl.title = date.toLocaleString();
      this._timeEl.dateTime = date.toISOString();
    }

    // Build (not yet attached) a message element. role/text/ts are stashed on
    // the instance and rendered by connectedCallback on append.
    static create(role, text, opts = {}) {
      const el = document.createElement("chat-message");
      el._role = role;
      el._text = text;
      el._ts = opts.ts;
      if (opts.id) el.dataset.id = opts.id;
      return el;
    }
  }

  customElements.define("chat-message", ChatMessage);
  window.ChatMessage = ChatMessage;
})();
