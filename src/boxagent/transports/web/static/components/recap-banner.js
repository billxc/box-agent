// <recap-banner> — the per-session recap bar at the top of the chat.
//
// Owns its DOM (icon + text + dismiss button) AND its behaviour: collapse on
// click, and remember dismissal in localStorage keyed by the caller-supplied
// `dismissKey` (the key depends on machine|bot|chat, which is app state, so the
// caller computes it; the persistence logic lives here). Browser APIs only.
//
// Usage: `document.getElementById("recap-banner").show(recap, dismissKey)`.
(function () {
  "use strict";

  class RecapBanner extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      const icon = document.createElement("span");
      icon.className = "recap-icon";
      icon.textContent = "📌";
      this._textEl = document.createElement("div");
      this._textEl.className = "recap-text";
      this._closeEl = document.createElement("button");
      this._closeEl.className = "recap-close icon-btn";
      this._closeEl.setAttribute("aria-label", "Dismiss recap");
      this._closeEl.title = "Dismiss";
      this._closeEl.textContent = "✕";
      this.append(icon, this._textEl, this._closeEl);
      this._textEl.onclick = () => this.classList.toggle("collapsed");
    }

    // Show `recap`; empty recap hides. A recap already dismissed (same text)
    // under `dismissKey` stays hidden.
    show(recap, dismissKey) {
      if (!this._built) this.connectedCallback();
      if (!recap) {
        this.classList.add("hidden");
        this._textEl.textContent = "";
        return;
      }
      if (window.localStorage.getItem(dismissKey) === recap) {
        this.classList.add("hidden");
        return;
      }
      this._textEl.textContent = recap;
      this.classList.remove("hidden");
      this.classList.add("collapsed");
      this._closeEl.onclick = () => {
        window.localStorage.setItem(dismissKey, recap);
        this.classList.add("hidden");
      };
    }
  }

  customElements.define("recap-banner", RecapBanner);
  window.RecapBanner = RecapBanner;
})();
