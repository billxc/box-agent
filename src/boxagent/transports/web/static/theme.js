// BoxAgent — theme switcher
// Persists the user's choice in localStorage and applies it to <html data-theme>.
// Themes: phosphor (dark brutalist green), ink (light brutalist red), soft (Apple-like, follows system).

(function () {
  const KEY = "boxagent.theme";
  const DEFAULT = "phosphor";
  const THEMES = [
    { id: "phosphor", label: "Phosphor" },
    { id: "ink",      label: "Ink"      },
    { id: "soft",     label: "Soft"     },
  ];

  function read() {
    try {
      const value = localStorage.getItem(KEY);
      return THEMES.some(t => t.id === value) ? value : DEFAULT;
    } catch { return DEFAULT; }
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  // Apply ASAP to avoid FOUC. Called inline from <head>.
  function bootstrap() { apply(read()); }

  // Mount a <select> inside the given container element.
  function mount(container, opts) {
    if (!container) return;
    opts = opts || {};
    const wrap = document.createElement("span");
    wrap.className = "theme-switch";
    if (opts.label !== false) {
      const lbl = document.createElement("span");
      lbl.textContent = opts.label || "theme";
      wrap.appendChild(lbl);
    }
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", "Theme");
    const current = read();
    for (const t of THEMES) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.label;
      if (t.id === current) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => {
      try { localStorage.setItem(KEY, sel.value); } catch {}
      apply(sel.value);
    });
    wrap.appendChild(sel);
    container.appendChild(wrap);
  }

  window.BoxAgentTheme = { bootstrap, mount, apply, read };
  // Always bootstrap on script load.
  bootstrap();
})();
