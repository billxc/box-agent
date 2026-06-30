// Sidebar drag-to-resize (desktop only). Self-contained behavior — persists the
// width to localStorage["ba.sidebarWidth"] and touches no app state. Mobile uses
// the drawer instead, so the drag is guarded by the 720px breakpoint.
(function () {
  "use strict";

  const sidebar = document.getElementById("sidebar");
  const resizer = document.getElementById("sidebar-resizer");
  if (!sidebar || !resizer) return;

  // Restore persisted width on desktop only.
  const saved = parseInt(localStorage.getItem("ba.sidebarWidth") || "0", 10);
  if (saved >= 200 && window.innerWidth > 720) {
    sidebar.style.flex = `0 0 ${saved}px`;
    sidebar.style.width = `${saved}px`;
  }

  let dragging = false;
  let startX = 0, startW = 0;

  function onMove(e) {
    if (!dragging) return;
    const x = e.touches ? e.touches[0].clientX : e.clientX;
    const dx = x - startX;
    let w = startW + dx;
    // clamp [200, 70vw]
    w = Math.max(200, Math.min(window.innerWidth * 0.7, w));
    sidebar.style.flex = `0 0 ${w}px`;
    sidebar.style.width = `${w}px`;
  }

  function onUp() {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove("dragging");
    document.body.classList.remove("sidebar-dragging");
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    document.removeEventListener("touchmove", onMove);
    document.removeEventListener("touchend", onUp);
    const w = parseInt(sidebar.style.width, 10);
    if (w >= 200) localStorage.setItem("ba.sidebarWidth", String(w));
  }

  function onDown(e) {
    if (window.innerWidth <= 720) return;  // mobile uses drawer
    dragging = true;
    startX = e.touches ? e.touches[0].clientX : e.clientX;
    startW = sidebar.getBoundingClientRect().width;
    resizer.classList.add("dragging");
    document.body.classList.add("sidebar-dragging");
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.addEventListener("touchmove", onMove, { passive: false });
    document.addEventListener("touchend", onUp);
    e.preventDefault();
  }

  resizer.addEventListener("mousedown", onDown);
  resizer.addEventListener("touchstart", onDown, { passive: false });
  // Double-click resets to default.
  resizer.addEventListener("dblclick", () => {
    sidebar.style.flex = "";
    sidebar.style.width = "";
    localStorage.removeItem("ba.sidebarWidth");
  });
})();
