// <session-picker> — Claude native session resume browser, as a self-contained
// custom element.
//
// Native Web Component, no framework / no build step. Owns its own modal DOM
// (light DOM, so the existing `.picker*` rules in style.css apply unchanged),
// all Claude-resume HTTP (projects / sessions / transcript / resume POST), and
// the projects→sessions pagination.
//
// app.js injects three collaborators after grabbing the element:
//   picker.api        = (path, opts) => fetch(...)   // the app's HTTP wrapper
//   picker.getContext = () => ({ machine, bot })     // current bot selection
//   picker.onResumed  = (info) => {...}              // resume result callback
//     onResumed({chat_id, machine, bot, raw, project, session}) fires after a
//     successful resume POST. The picker has already closed itself; app.js
//     updates its local session caches and navigates (switchChat) — those touch
//     app state the component deliberately doesn't.
//
// Public methods: open(), close(). Built lazily in connectedCallback.
(function () {
  "use strict";

  const PROJECTS_PAGE_SIZE = 30;
  const SESSION_PAGE_SIZE = 50;

  // Relative-ish timestamp: time-of-day for today, date otherwise. Pure.
  function formatTs(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString();
  }

  function make(tag, className, attrs) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  class SessionPicker extends HTMLElement {
    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "contents"; // wrapper transparent; CSS targets .picker

      // ── Modal skeleton (mirrors the old #claude-picker markup) ──
      const overlay = make("div", "picker hidden", { role: "dialog", "aria-modal": "true" });
      const card = make("div", "picker-card");

      const head = make("header", "picker-head");
      const title = make("strong");
      title.textContent = "Resume Claude Session";
      this._crumb = make("span", "muted");
      this._back = make("button", "text-btn hidden");
      this._back.textContent = "← Back";
      this._close = make("button", "icon-btn", { "aria-label": "Close" });
      this._close.textContent = "×";
      head.append(title, this._crumb, make("span", "spacer"), this._back, this._close);

      const body = make("div", "picker-body");
      this._projects = make("ul", "picker-list");
      this._sessions = make("ul", "picker-list hidden");
      this._preview = make("div", "picker-preview hidden");
      body.append(this._projects, this._sessions, this._preview);

      const foot = make("footer", "picker-foot");
      this._count = make("span", "muted");
      const rawLabel = make("label", "muted", {
        style: "display:flex;align-items:center;gap:4px;font-size:12px;",
      });
      this._raw = make("input", null, { type: "checkbox" });
      this._raw.checked = true;
      const rawText = make("span");
      rawText.textContent = "Raw passthrough (no BoxAgent injection)";
      rawLabel.append(this._raw, rawText);
      this._cancel = make("button", "text-btn");
      this._cancel.textContent = "Cancel";
      this._resume = make("button", "primary-btn", { disabled: "" });
      this._resume.disabled = true;
      this._resume.textContent = "Resume";
      foot.append(this._count, make("span", "spacer"), rawLabel, this._cancel, this._resume);

      card.append(head, body, foot);
      overlay.appendChild(card);
      this.appendChild(overlay);
      this._overlay = overlay;

      // Internal interactions
      this._close.onclick = () => this.close();
      this._cancel.onclick = () => this.close();
      this._back.onclick = () => this._goBack();
      this._resume.onclick = () => this._doResume();

      // Per-open browse state
      this._state = { project: null, session: null };
      this._paging = { offset: 0, total: 0, loading: false, done: false, observer: null };
    }

    // ── Public API ──

    open() {
      this._overlay.classList.remove("hidden");
      this.showProjects();
    }

    close() {
      this._overlay.classList.add("hidden");
      this._preview.classList.add("hidden");
      this._preview.innerHTML = "";
      this._state = { project: null, session: null };
    }

    _goBack() {
      if (this._state.session) {
        this._state.session = null;
        this._resume.disabled = true;
        this._preview.classList.add("hidden");
        this._preview.innerHTML = "";
        this._sessions.classList.remove("hidden");
        this._crumb.textContent = this._state.project ? this._state.project.label : "";
      } else if (this._state.project) {
        this.showProjects();
      }
    }

    // ── Projects ──

    async showProjects() {
      this._state = { project: null, session: null };
      this._crumb.textContent = "";
      this._back.classList.add("hidden");
      this._sessions.classList.add("hidden");
      this._preview.classList.add("hidden");
      this._preview.innerHTML = "";
      this._resume.disabled = true;
      this._projects.classList.remove("hidden");
      this._projects.innerHTML = "<li class='muted'>Loading…</li>";
      this._count.textContent = "";
      try {
        await this.loadProjectsPage(0, /*replace=*/ true);
      } catch (e) {
        this._projects.innerHTML = `<li class='muted'>Error: ${escapeHtml(e.message)}</li>`;
      }
    }

    async loadProjectsPage(offset, replace) {
      const ctx = this.getContext();
      const r = await this.api(
        `claude/projects?machine=${encodeURIComponent(ctx.machine)}` +
        `&offset=${offset}&limit=${PROJECTS_PAGE_SIZE}`,
      );
      const { projects, total, has_more } = await r.json();
      const existingMore = this._projects.querySelector(".load-more");
      if (existingMore) existingMore.remove();
      if (replace) this._projects.innerHTML = "";
      if (replace && projects.length === 0) {
        this._projects.innerHTML = "<li class='muted'>No Claude sessions found at ~/.claude/projects/</li>";
        this._count.textContent = "0 projects";
        return;
      }
      for (const p of projects) {
        const li = document.createElement("li");
        li.innerHTML = `<div class="grow"><div class="row1">📁 ${escapeHtml(p.label)}</div><div class="row2">${escapeHtml(p.cwd || p.encoded)}</div></div><span class="meta">${p.session_count} · ${formatTs(p.last_ts)}</span>`;
        li.onclick = () => this.showSessions(p);
        this._projects.appendChild(li);
      }
      const shown = this._projects.querySelectorAll("li:not(.load-more):not(.muted)").length;
      this._count.textContent = `${shown} / ${total} projects`;
      if (has_more) {
        const li = document.createElement("li");
        li.className = "load-more muted";
        li.style.cursor = "pointer";
        li.style.textAlign = "center";
        li.textContent = "Load more…";
        li.onclick = async () => {
          li.textContent = "Loading…";
          li.onclick = null;
          try {
            await this.loadProjectsPage(offset + PROJECTS_PAGE_SIZE, /*replace=*/ false);
          } catch (e) {
            li.textContent = `Error: ${e.message}`;
          }
        };
        this._projects.appendChild(li);
      }
    }

    // ── Sessions ──

    async showSessions(project) {
      this._state = { project, session: null };
      this._projects.classList.add("hidden");
      this._sessions.classList.remove("hidden");
      this._preview.classList.add("hidden");
      this._preview.innerHTML = "";
      this._resume.disabled = true;
      this._back.classList.remove("hidden");
      this._crumb.textContent = project.label;
      this._sessions.innerHTML = "<li class='muted'>Loading…</li>";
      if (this._paging.observer) this._paging.observer.disconnect();
      this._paging = { offset: 0, total: 0, loading: false, done: false, observer: null };
      try {
        await this.loadSessionPage();
      } catch (e) {
        this._sessions.innerHTML = `<li class='muted'>Error: ${escapeHtml(e.message)}</li>`;
      }
    }

    renderSessionItem(s) {
      const li = document.createElement("li");
      const title = (s.first_user || "(no user message)").trim();
      li.innerHTML = `<div class="grow"><div class="row1">${escapeHtml(title)}</div><div class="row2">${formatTs(s.last_ts)} · ${s.session_id.slice(0, 8)}</div></div><span class="meta">💬 ${s.message_count}</span>`;
      li.onclick = () => this.selectSession(li, s);
      return li;
    }

    async loadSessionPage() {
      if (this._paging.loading || this._paging.done) return;
      this._paging.loading = true;
      const ctx = this.getContext();
      const project = this._state.project;
      const url =
        `claude/sessions?machine=${encodeURIComponent(ctx.machine)}` +
        `&project=${encodeURIComponent(project.encoded)}` +
        `&offset=${this._paging.offset}&limit=${SESSION_PAGE_SIZE}`;
      const firstPage = this._paging.offset === 0;
      const sentinel = this._sessions.querySelector("li.sentinel");
      if (sentinel) sentinel.remove();
      if (firstPage) this._sessions.innerHTML = "";
      try {
        const r = await this.api(url);
        const data = await r.json();
        const sessions = data.sessions || [];
        this._paging.total = data.total ?? sessions.length;
        this._paging.offset += sessions.length;
        this._paging.done = !data.has_more || sessions.length === 0;
        for (const s of sessions) {
          this._sessions.appendChild(this.renderSessionItem(s));
        }
        this._count.textContent = `${this._paging.offset} / ${this._paging.total} sessions`;
        if (firstPage && sessions.length === 0) {
          this._sessions.innerHTML = "<li class='muted'>(empty)</li>";
          return;
        }
        if (!this._paging.done) {
          const newSentinel = document.createElement("li");
          newSentinel.className = "sentinel muted";
          newSentinel.textContent = "Loading more…";
          this._sessions.appendChild(newSentinel);
          if (!this._paging.observer) {
            this._paging.observer = new IntersectionObserver((entries) => {
              for (const entry of entries) {
                if (entry.isIntersecting) {
                  this.loadSessionPage().catch((e) => console.error("page load failed", e));
                }
              }
            }, { root: this._sessions, rootMargin: "200px" });
          }
          this._paging.observer.observe(newSentinel);
        }
      } finally {
        this._paging.loading = false;
      }
    }

    async selectSession(li, session) {
      this._state.session = session;
      for (const x of this._sessions.querySelectorAll("li")) x.classList.remove("selected");
      li.classList.add("selected");
      this._resume.disabled = false;
      this._preview.classList.remove("hidden");
      this._preview.innerHTML = "<div class='muted'>Loading transcript…</div>";
      try {
        const ctx = this.getContext();
        const r = await this.api(`claude/transcript?machine=${encodeURIComponent(ctx.machine)}&project=${encodeURIComponent(this._state.project.encoded)}&session_id=${encodeURIComponent(session.session_id)}`);
        const { messages } = await r.json();
        this._preview.innerHTML = "";
        const tail = messages.slice(-12);
        for (const m of tail) {
          const div = document.createElement("div");
          div.className = "pmsg";
          div.innerHTML = `<span class="role">${m.role}</span>${escapeHtml((m.text || "").slice(0, 240))}${m.text.length > 240 ? "…" : ""}`;
          this._preview.appendChild(div);
        }
      } catch (e) {
        this._preview.innerHTML = `<div class='muted'>Preview failed: ${escapeHtml(e.message)}</div>`;
      }
    }

    // ── Resume ──
    // The picker owns the resume POST; the resulting chat_id + selection go out
    // via the onResumed callback so app.js can update its local caches + navigate.

    async _doResume() {
      const ctx = this.getContext();
      if (!this._state.session || !ctx.bot) return;
      this._resume.disabled = true;
      this._resume.textContent = "Resuming…";
      const raw = !!(this._raw && this._raw.checked);
      const resumeBot = raw ? "raw" : ctx.bot;
      const resumeMachine = ctx.machine;
      try {
        const r = await this.api("claude/resume", {
          method: "POST",
          body: JSON.stringify({
            bot: resumeBot,
            machine: resumeMachine,
            project: this._state.project.encoded,
            session_id: this._state.session.session_id,
            backend: raw ? "claude-cli" : undefined,
          }),
        });
        if (!r.ok) throw new Error(await r.text());
        const { chat_id } = await r.json();
        this.onResumed?.({
          chat_id,
          machine: resumeMachine,
          bot: resumeBot,
          raw,
          project: this._state.project,
          session: this._state.session,
        });
        this.close();
      } catch (e) {
        alert("Resume failed: " + e.message);
      } finally {
        this._resume.disabled = false;
        this._resume.textContent = "Resume";
      }
    }
  }

  SessionPicker.formatTs = formatTs; // exposed for unit tests
  customElements.define("session-picker", SessionPicker);
  window.SessionPicker = SessionPicker;
})();
