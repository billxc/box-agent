// BoxAgent Logs page — paginated tail of boxagent.log via /api/logs
"use strict";

const tokenHead = (() => {
  const t = new URLSearchParams(location.search).get("token");
  return t ? `?token=${encodeURIComponent(t)}` : "";
})();
const tokenAmp = tokenHead ? `&${tokenHead.slice(1)}` : "";

const state = {
  machines: [],          // [{machine_id, self}]
  selected_machine: "",  // "" = local
  offset: 0,
  limit: 200,
  has_more: false,
  log_file: null,
};

const $ = (id) => document.getElementById(id);

async function fetchMachines() {
  try {
    const r = await fetch(`/api/machines${tokenHead}`).then((r) => r.json());
    state.machines = (r.machines || []).map((m) => ({
      machine_id: m.machine_id,
      self: !!m.self,
    }));
  } catch {
    state.machines = [];
  }
  const select = $("machine-select");
  select.innerHTML = "";
  if (!state.machines.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(local)";
    select.appendChild(opt);
    return;
  }
  for (const m of state.machines) {
    const opt = document.createElement("option");
    opt.value = m.self ? "" : m.machine_id;
    opt.textContent = m.machine_id + (m.self ? " (this)" : "");
    if (m.self) opt.selected = true;
    select.appendChild(opt);
  }
}

function selectedLevels() {
  return [...document.querySelectorAll("#level-chips input:checked")].map((cb) => cb.value);
}

function buildQuery() {
  const params = new URLSearchParams();
  if (state.selected_machine) params.set("machine", state.selected_machine);
  params.set("limit", String(state.limit));
  params.set("offset", String(state.offset));
  const levels = selectedLevels();
  if (levels.length) params.set("levels", levels.join(","));
  const grep = $("grep-input").value.trim();
  if (grep) params.set("grep", grep);
  if (tokenHead) params.set("token", new URLSearchParams(location.search).get("token"));
  return params.toString();
}

function escapeHTML(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderRows(lines) {
  const body = $("logs-body");
  body.innerHTML = "";
  if (!lines.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="4" style="padding:24px;text-align:center;color:var(--dim);">No matching log entries</td>`;
    body.appendChild(tr);
    return;
  }
  for (const line of lines) {
    const tr = document.createElement("tr");
    if (line.raw !== undefined) {
      tr.className = "row-raw";
      tr.innerHTML = `<td class="col-time">—</td><td class="col-level">RAW</td><td class="col-logger">—</td><td class="col-msg">${escapeHTML(line.raw)}</td>`;
    } else {
      const lvl = String(line.level || "").toUpperCase();
      tr.innerHTML =
        `<td class="col-time">${escapeHTML(line.time || "")}</td>` +
        `<td class="col-level level-${escapeHTML(lvl)}">${escapeHTML(lvl)}</td>` +
        `<td class="col-logger" title="${escapeHTML(line.logger || "")}">${escapeHTML(line.logger || "")}</td>` +
        `<td class="col-msg">${escapeHTML(line.msg || "")}</td>`;
    }
    const msgCell = tr.querySelector(".col-msg");
    msgCell.addEventListener("click", () => msgCell.classList.toggle("expanded"));
    body.appendChild(tr);
  }
}

async function load() {
  $("status").textContent = "Loading…";
  try {
    const r = await fetch(`/api/logs?${buildQuery()}`).then((r) => r.json());
    if (!r.ok) {
      $("status").textContent = `Error: ${r.error || "unknown"}`;
      renderRows([]);
      return;
    }
    state.has_more = !!r.has_more;
    state.log_file = r.log_file || null;
    renderRows(r.lines || []);
    const start = state.offset + 1;
    const end = state.offset + (r.lines || []).length;
    $("status").textContent = state.log_file
      ? `${state.log_file}  —  showing ${start}–${end} (newest first)`
      : "No log file configured";
    $("pager-info").textContent = `entries ${start}–${end}`;
    $("prev-page").disabled = state.offset === 0;
    $("next-page").disabled = !state.has_more;
  } catch (e) {
    $("status").textContent = `Fetch failed: ${e}`;
  }
}

function bindUI() {
  $("machine-select").addEventListener("change", (e) => {
    state.selected_machine = e.target.value;
    state.offset = 0;
    load();
  });
  $("apply").addEventListener("click", () => {
    const lim = parseInt($("limit-input").value, 10);
    state.limit = isFinite(lim) && lim > 0 ? Math.min(2000, lim) : 200;
    state.offset = 0;
    load();
  });
  $("grep-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      state.offset = 0;
      load();
    }
  });
  for (const cb of document.querySelectorAll("#level-chips input")) {
    cb.addEventListener("change", () => {
      state.offset = 0;
      load();
    });
  }
  $("reload").addEventListener("click", () => load());
  $("prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    load();
  });
  $("next-page").addEventListener("click", () => {
    state.offset += state.limit;
    load();
  });
}

(async function init() {
  bindUI();
  await fetchMachines();
  await load();
})();
