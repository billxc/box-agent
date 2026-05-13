// Events page — vanilla JS, no framework.
// Layout: sidebar filters (level chips, search, category tree) + scrollable list.
// SSE for realtime, /api/events for history + pagination.

const LEVELS_STORAGE_KEY = "boxagent.events.levels";
const MACHINES_STORAGE_KEY = "boxagent.events.machines";

const state = {
  events: [],
  unread_only: false,
  selected_category_prefix: null,
  search: "",
  next_cursor: null,
  sse: null,
  selected_machines: null, // null = no filter; Set of machine_ids when set
};

function loadStoredLevels() {
  try {
    const raw = localStorage.getItem(LEVELS_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed) : null;
  } catch { return null; }
}

function saveLevels() {
  try {
    localStorage.setItem(LEVELS_STORAGE_KEY, JSON.stringify(selectedLevels()));
  } catch {}
}

function applyStoredLevels() {
  const stored = loadStoredLevels();
  if (!stored) return;
  document.querySelectorAll("#level-filter input").forEach(el => {
    el.checked = stored.has(el.value);
  });
}

function loadStoredMachines() {
  try {
    const raw = localStorage.getItem(MACHINES_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed) : null;
  } catch { return null; }
}

function saveMachines() {
  try {
    const values = state.selected_machines ? Array.from(state.selected_machines) : null;
    if (values === null) localStorage.removeItem(MACHINES_STORAGE_KEY);
    else localStorage.setItem(MACHINES_STORAGE_KEY, JSON.stringify(values));
  } catch {}
}

const tokenParam = (() => {
  const t = new URLSearchParams(location.search).get("token");
  return t ? `&token=${encodeURIComponent(t)}` : "";
})();

const tokenHead = tokenParam ? "?" + tokenParam.slice(1) : "";

function selectedLevels() {
  return Array.from(document.querySelectorAll("#level-filter input:checked")).map(i => i.value);
}

function filterParams() {
  const params = new URLSearchParams();
  const levels = selectedLevels();
  if (levels.length > 0 && levels.length < 5) params.set("levels", levels.join(","));
  if (state.unread_only) params.set("unread_only", "1");
  if (state.search) params.set("search", state.search);
  if (state.selected_category_prefix) params.set("category_prefix", state.selected_category_prefix);
  if (state.selected_machines && state.selected_machines.size > 0) {
    params.set("machines", Array.from(state.selected_machines).join(","));
  }
  return params;
}

async function fetchMachines() {
  try {
    const r = await fetch(`/api/machines${tokenHead}`);
    const data = await r.json();
    const machines = (data.machines || []).map(m => m.machine_id).filter(Boolean);
    renderMachineFilter(machines);
  } catch (err) { console.error(err); }
}

function renderMachineFilter(machineIds) {
  const container = document.getElementById("machine-filter");
  container.innerHTML = "";
  if (machineIds.length === 0) {
    container.innerHTML = '<div style="color: var(--muted); font-size: 12px;">(none)</div>';
    return;
  }
  const stored = loadStoredMachines();
  if (stored) state.selected_machines = new Set(machineIds.filter(m => stored.has(m)));
  for (const machine of machineIds) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = machine;
    cb.checked = !state.selected_machines || state.selected_machines.has(machine);
    cb.onchange = () => {
      const checked = Array.from(document.querySelectorAll("#machine-filter input:checked")).map(i => i.value);
      const all = Array.from(document.querySelectorAll("#machine-filter input")).map(i => i.value);
      state.selected_machines = checked.length === all.length ? null : new Set(checked);
      saveMachines();
      fetchEvents();
      startSse();
    };
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + machine));
    container.appendChild(label);
  }
}

async function fetchEvents(append = false) {
  const params = filterParams();
  params.set("limit", "200");
  if (append && state.next_cursor) params.set("before_id", state.next_cursor);
  const url = `/api/events?${params}${tokenParam}`;
  const r = await fetch(url);
  const data = await r.json();
  if (!data.ok) {
    setStatus(`error: ${data.error || "?"}`);
    return;
  }
  if (append) {
    state.events.push(...data.events);
  } else {
    state.events = data.events;
  }
  state.next_cursor = data.next_cursor;
  document.getElementById("load-more").hidden = !state.next_cursor;
  render();
  setStatus(`${state.events.length} events${state.next_cursor ? " (more available)" : ""}`);
}

async function fetchCategories() {
  const r = await fetch(`/api/events/categories${tokenHead}`);
  const data = await r.json();
  if (!data.ok) return;
  renderCategoryTree(data.categories);
}

function buildTree(categories) {
  // categories: [{category: "scheduler.run", count: 3}, ...]
  // Tree: { name, fullPath, count, children: { name: node } }
  const root = { name: "(all)", fullPath: null, count: 0, children: {} };
  for (const { category, count } of categories) {
    root.count += count;
    const parts = category.split(".");
    let node = root;
    let path = "";
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      path = path ? `${path}.${part}` : part;
      if (!node.children[part]) {
        node.children[part] = { name: part, fullPath: path, count: 0, children: {} };
      }
      node = node.children[part];
      if (i === parts.length - 1) node.count = count;
    }
  }
  return root;
}

function renderTreeNode(node, container, depth = 0) {
  const childKeys = Object.keys(node.children).sort();
  const div = document.createElement("div");
  div.className = "tree-node";
  if (node.fullPath !== null) {
    const toggle = document.createElement("span");
    toggle.className = "tree-toggle";
    toggle.textContent = childKeys.length > 0 ? "▾" : " ";
    div.appendChild(toggle);

    const label = document.createElement("span");
    label.className = "tree-label";
    label.textContent = node.name;
    label.dataset.prefix = node.fullPath;
    if (state.selected_category_prefix === node.fullPath) label.classList.add("selected");
    label.onclick = () => {
      if (state.selected_category_prefix === node.fullPath) {
        state.selected_category_prefix = null;
      } else {
        state.selected_category_prefix = node.fullPath;
      }
      renderTreeSelection();
      fetchEvents();
    };
    div.appendChild(label);

    if (node.count > 0) {
      const c = document.createElement("span");
      c.className = "tree-count";
      c.textContent = node.count;
      div.appendChild(c);
    }
    container.appendChild(div);

    if (childKeys.length > 0) {
      const childrenDiv = document.createElement("div");
      childrenDiv.className = "tree-children";
      toggle.onclick = () => {
        childrenDiv.classList.toggle("collapsed");
        toggle.textContent = childrenDiv.classList.contains("collapsed") ? "▸" : "▾";
      };
      for (const key of childKeys) renderTreeNode(node.children[key], childrenDiv, depth + 1);
      container.appendChild(childrenDiv);
    }
  } else {
    // Root: render an "All" pseudo-entry then children.
    const allLabel = document.createElement("div");
    allLabel.className = "tree-node";
    const inner = document.createElement("span");
    inner.className = "tree-label";
    inner.textContent = "(all)";
    if (!state.selected_category_prefix) inner.classList.add("selected");
    inner.onclick = () => {
      state.selected_category_prefix = null;
      renderTreeSelection();
      fetchEvents();
    };
    allLabel.appendChild(inner);
    container.appendChild(allLabel);

    for (const key of childKeys) renderTreeNode(node.children[key], container, depth);
  }
}

function renderCategoryTree(categories) {
  const root = buildTree(categories);
  const container = document.getElementById("category-tree");
  container.innerHTML = "";
  renderTreeNode(root, container);
}

function renderTreeSelection() {
  document.querySelectorAll(".tree-label").forEach(el => {
    el.classList.toggle(
      "selected",
      el.dataset.prefix === state.selected_category_prefix ||
        (!state.selected_category_prefix && el.textContent === "(all)")
    );
  });
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function renderEvent(event) {
  const li = document.createElement("li");
  li.className = "event-item" + (event.read_at ? "" : " unread");
  li.dataset.id = event.id;
  const meta = event.meta && Object.keys(event.meta).length > 0
    ? `<div class="event-meta">${escapeHtml(JSON.stringify(event.meta))}</div>` : "";
  const machineSuffix = event.origin_machine
    ? `<span class="event-machine"> @${escapeHtml(event.origin_machine)}</span>` : "";
  li.innerHTML = `
    <div class="event-time">${fmtTime(event.ts)}</div>
    <div class="event-level ${event.level}">${event.level}</div>
    <div class="event-category">${escapeHtml(event.category)}${machineSuffix}</div>
    <div>
      <div class="event-message">${escapeHtml(event.message)}</div>
      ${meta}
    </div>
  `;
  li.onclick = () => markRead(event.id);
  return li;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function render() {
  const list = document.getElementById("events-list");
  list.innerHTML = "";
  for (const event of state.events) list.appendChild(renderEvent(event));
}

async function markRead(id) {
  const r = await fetch(`/api/events/${id}/read${tokenHead}`, { method: "POST" });
  if (r.ok) {
    const event = state.events.find(e => e.id === id);
    if (event && !event.read_at) {
      event.read_at = Date.now() / 1000;
      const li = document.querySelector(`li[data-id="${id}"]`);
      if (li) li.classList.remove("unread");
    }
  }
}

async function markAllRead() {
  const params = filterParams();
  const body = {};
  for (const [k, v] of params) {
    if (k === "levels") body.levels = v.split(",");
    else if (k === "category_prefix") body.category_prefix = v;
    else if (k === "search") {} // server doesn't filter on search for read_all (could)
  }
  const r = await fetch(`/api/events/read_all${tokenHead}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  setStatus(`Marked ${data.updated || 0} as read`);
  fetchEvents();
}

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

function startSse() {
  if (state.sse) state.sse.close();
  const params = filterParams();
  const url = `/api/events/stream?${params}${tokenParam}`;
  const sse = new EventSource(url);
  sse.addEventListener("event", e => {
    try {
      const event = JSON.parse(e.data);
      state.events.unshift(event);
      const list = document.getElementById("events-list");
      const li = renderEvent(event);
      list.insertBefore(li, list.firstChild);
    } catch (err) { console.error(err); }
  });
  state.sse = sse;
}

document.getElementById("unread-only").onchange = e => {
  state.unread_only = e.target.checked;
  fetchEvents();
};
document.getElementById("search").oninput = e => {
  state.search = e.target.value.trim();
  clearTimeout(window.__searchTimer);
  window.__searchTimer = setTimeout(() => fetchEvents(), 300);
};
document.querySelectorAll("#level-filter input").forEach(el => {
  el.onchange = () => { saveLevels(); fetchEvents(); startSse(); };
});
document.getElementById("mark-all-read").onclick = markAllRead;
document.getElementById("reload").onclick = () => { fetchCategories(); fetchMachines(); fetchEvents(); };
document.getElementById("load-more").onclick = () => fetchEvents(true);

applyStoredLevels();
fetchCategories();
fetchMachines();
fetchEvents();
startSse();
