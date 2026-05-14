// BoxAgent — theme switcher
// A theme is a (shape, palette) tuple. The bootstrap sets both attributes
// on <html> so CSS can react to either axis independently.
//
// Shapes:   brutalist (default) / soft / paper / neon
// Palettes: phosphor (default)  / synthwave / nord / paper / apple

(function () {
  const KEY_THEME = "boxagent.theme";
  const DEFAULT = "phosphor";

  const THEMES = [
    { id: "phosphor",  label: "Phosphor",  shape: "brutalist", palette: "phosphor"  },
    { id: "synthwave", label: "Synthwave", shape: "neon",      palette: "synthwave" },
    { id: "nord",      label: "Nord",      shape: "scandi",    palette: "nord"      },
    { id: "paper",     label: "Paper",     shape: "paper",     palette: "paper"     },
    { id: "soft",      label: "Soft",      shape: "soft",      palette: "apple"     },
  ];

  function find(id) { return THEMES.find(t => t.id === id); }

  function read() {
    try {
      const stored = localStorage.getItem(KEY_THEME);
      return find(stored) ? stored : DEFAULT;
    } catch { return DEFAULT; }
  }

  function apply(themeId) {
    const t = find(themeId) || find(DEFAULT);
    const root = document.documentElement;
    root.setAttribute("data-theme", t.id);
    root.setAttribute("data-shape", t.shape);
    root.setAttribute("data-palette", t.palette);
  }

  function bootstrap() { apply(read()); }

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
      try { localStorage.setItem(KEY_THEME, sel.value); } catch {}
      apply(sel.value);
    });
    wrap.appendChild(sel);
    container.appendChild(wrap);
  }

  window.BoxAgentTheme = { bootstrap, mount, apply, read, THEMES };
  bootstrap();
})();
