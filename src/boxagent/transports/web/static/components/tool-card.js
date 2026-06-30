// <tool-card> — a tool-call card as a self-contained custom element.
//
// Native Web Component, no framework / no build step. Owns its own DOM and
// lifecycle so app.js no longer juggles a `{el, headerEl, bodyEl, resultEl}`
// registry (`state.toolCards`). Created via `document.createElement("tool-card")`;
// call `.setCall(name, args)` then later `.setResult(ok, summary, error)`.
//
// Built lazily in connectedCallback so a card can be staged into a detached
// history fragment (setCall/setResult buffered) and renders once inserted.
(function () {
  "use strict";

  function argSummary(args) {
    if (!args || typeof args !== "object") return "";
    for (const value of Object.values(args)) {
      if (typeof value === "string" && value.length) {
        const collapsed = value.replace(/\s+/g, " ");
        return collapsed.length > 60 ? collapsed.slice(0, 60) + "…" : collapsed;
      }
    }
    try {
      const json = JSON.stringify(args);
      return json.length > 60 ? json.slice(0, 60) + "…" : json;
    } catch {
      return "";
    }
  }

  class ToolCard extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "contents"; // wrapper transparent; CSS targets inner .tool-card

      const details = document.createElement("details");
      details.className = "tool-card";
      if (this.hasAttribute("subagent")) details.classList.add("subagent");
      this._header = document.createElement("summary");
      this._header.className = "tool-card-header";
      this._body = document.createElement("pre");
      this._body.className = "tool-card-body";
      this._result = document.createElement("div");
      this._result.className = "tool-card-result hidden";
      details.append(this._header, this._body, this._result);
      this.appendChild(details);
      this._render();
    }

    // Set / update the call (idempotent — the same tool id can arrive twice:
    // streaming start + final).
    setCall(name, args) {
      this._name = name;
      this._args = args;
      this._done = false;
      this._render();
    }

    setResult(ok, summary, error) {
      this._ok = ok;
      this._summary = summary;
      this._error = error;
      this._done = true;
      this._render();
    }

    _render() {
      if (!this._built) return; // buffered until connectedCallback
      const name = this._name || "tool";
      const summary = argSummary(this._args);
      if (this._done) {
        const icon = this._ok ? "✓" : "✗";
        this._header.textContent = `${icon} ${name}(${summary})`;
        this._result.classList.remove("hidden");
        this._result.textContent = this._ok
          ? (this._summary || "(ok)")
          : (this._error || this._summary || "(failed)");
        this._result.classList.toggle("ok", this._ok);
        this._result.classList.toggle("failed", !this._ok);
      } else {
        this._header.textContent = `▶ ${name}(${summary})`;
      }
      try {
        this._body.textContent = JSON.stringify(this._args, null, 2);
      } catch {
        this._body.textContent = String(this._args);
      }
    }

    // ── Card routing (owns find / create / dedup so callers stay one-liners) ──
    // `root` is the container the card lives in: the live #messages element, or
    // a detached history DocumentFragment. Querying + appending share that root.

    static _find(root, toolId) {
      return toolId ? root.querySelector(`tool-card[data-tool-id="${CSS.escape(toolId)}"]`) : null;
    }

    static _create(root, toolId, parentToolId) {
      const card = document.createElement("tool-card");
      if (toolId) card.dataset.toolId = toolId;
      if (parentToolId) card.setAttribute("subagent", "");
      root.appendChild(card);
      return card;
    }

    // Create or update a card for a tool call (idempotent on toolId).
    static upsertCall(root, toolId, name, args, parentToolId = "") {
      if (!toolId) toolId = `t${Math.random().toString(36).slice(2, 10)}`;
      const card = ToolCard._find(root, toolId) || ToolCard._create(root, toolId, parentToolId);
      card.setCall(name, args);
      return card;
    }

    // Apply a tool result, synthesising a card if the call was never seen.
    static applyResult(root, toolId, ok, summary, error) {
      let card = ToolCard._find(root, toolId);
      if (!card) {
        card = ToolCard._create(root, toolId, "");
        card.setCall("tool", {});
      }
      card.setResult(ok, summary, error);
      return card;
    }
  }

  customElements.define("tool-card", ToolCard);
  window.ToolCard = ToolCard; // component's public API for app.js (cf. marked / BoxAgentTheme globals)
})();
