// <chat-log> — the scrollable message area, as a custom element. IS the scroll
// container: a drop-in for `<div id="messages">` (the `#messages` CSS applies to
// the host), with message bubbles / tool cards / typing indicator as light-DOM
// children.
//
// Owns DISPLAY only — append a bubble, the typing indicator, scroll-to-bottom,
// history replace/prepend (with scroll-position preservation), and tool-card
// rendering. The stream/fetch/navigation logic stays in app.js, which holds the
// element addMessage returns to stream deltas into it, and keeps the
// "scrolled near the top → load older" listener (the host is a real scroll box).
//
// Methods: addMessage(role, text, opts) → el · showTyping() · removeTyping() ·
//   scrollToBottom() · setHistory(items) · prependHistory(items) ·
//   upsertToolCall(...) · applyToolResult(...).
(function () {
  "use strict";

  class ChatLog extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this._typingEl = null;
      // Scrolled near the top → ask the controller for older history. The
      // component owns "when to load more" (a scroll/display concern); app.js
      // owns "how" (the fetch) via the injected onLoadOlder callback.
      this.addEventListener("scroll", () => {
        if (this.scrollTop < 100 && this.onLoadOlder) this.onLoadOlder();
      }, { passive: true });
    }

    scrollToBottom() {
      requestAnimationFrame(() => { this.scrollTop = this.scrollHeight; });
    }

    addMessage(role, text, opts = {}) {
      this.removeTyping();
      const el = ChatMessage.create(role, text, opts);
      this.appendChild(el);
      this.scrollToBottom();
      return el;
    }

    showTyping() {
      if (this._typingEl) return;
      const el = document.createElement("div");
      el.className = "typing";
      el.innerHTML = "<span></span><span></span><span></span>";
      this.appendChild(el);
      this._typingEl = el;
      this.scrollToBottom();
    }

    removeTyping() {
      if (this._typingEl) { this._typingEl.remove(); this._typingEl = null; }
    }

    // Build a detached fragment of bubbles / tool cards from history items.
    _buildFragment(items) {
      const frag = document.createDocumentFragment();
      for (const h of items) {
        if (h.role === "tool_call") {
          ToolCard.upsertCall(frag, h.tool_id || "", h.name || "tool", h.args || {});
          continue;
        }
        if (h.role === "tool_result") {
          ToolCard.applyResult(frag, h.tool_id || "", !!h.ok, h.summary || "", h.error || "");
          continue;
        }
        const el = ChatMessage.create(h.role, h.text, { ts: h.ts });
        el.style.animation = "none"; // history shouldn't re-animate in
        frag.appendChild(el);
      }
      return frag;
    }

    // Replace the whole log with history, then jump to bottom (no smooth scroll).
    setHistory(items) {
      const frag = this._buildFragment(items);
      const prevBehavior = this.style.scrollBehavior;
      this.style.scrollBehavior = "auto";
      this.replaceChildren(frag);
      this.scrollTop = this.scrollHeight;
      this.style.scrollBehavior = prevBehavior;
    }

    // Prepend older history, preserving the visible scroll position.
    prependHistory(items) {
      const frag = this._buildFragment(items);
      const prevBehavior = this.style.scrollBehavior;
      this.style.scrollBehavior = "auto";
      const beforeHeight = this.scrollHeight;
      const beforeTop = this.scrollTop;
      this.insertBefore(frag, this.firstChild);
      const afterHeight = this.scrollHeight;
      this.scrollTop = beforeTop + (afterHeight - beforeHeight);
      this.style.scrollBehavior = prevBehavior;
    }

    upsertToolCall(toolId, name, args, parentToolId = "") {
      ToolCard.upsertCall(this, toolId, name, args, parentToolId);
    }

    applyToolResult(toolId, ok, summary, error) {
      ToolCard.applyResult(this, toolId, ok, summary, error);
    }
  }

  customElements.define("chat-log", ChatLog);
  window.ChatLog = ChatLog;
})();
