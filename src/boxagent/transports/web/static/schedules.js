// Schedules page — vanilla JS, no framework.
// Three-pane: schedule list (per machine) → run list → run detail.
// On-demand fetch only (no SSE), per user spec.

const tokenParam = (() => {
  const t = new URLSearchParams(location.search).get("token");
  return t ? `&token=${encodeURIComponent(t)}` : "";
})();
const tokenHead = tokenParam ? "?" + tokenParam.slice(1) : "";

const state = {
  machines: [],          // [{machine_id, self}]
  selected_machines: null, // null = all; Set otherwise
  schedules: [],         // [{id, machine_id, ...}]  flattened across machines
  selected_key: null,    // "machine_id|task_id"
  runs: [],
  selected_run_index: null,
  runs_total: 0,
  runs_loading: false,
  runs_machine: null,
  runs_task: null,
};

const RUNS_PAGE_SIZE = 30;

const MACHINES_KEY = "boxagent.schedules.machines";

function loadStoredMachines() {
  try {
    const raw = localStorage.getItem(MACHINES_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed) : null;
  } catch { return null; }
}
function saveMachines() {
  try {
    const v = state.selected_machines ? Array.from(state.selected_machines) : null;
    if (v === null) localStorage.removeItem(MACHINES_KEY);
    else localStorage.setItem(MACHINES_KEY, JSON.stringify(v));
  } catch {}
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function fetchMachines() {
  const r = await fetch(`/api/machines${tokenHead}`).then(r => r.json()).catch(() => ({}));
  state.machines = (r.machines || []).map(m => ({ machine_id: m.machine_id, self: !!m.self }));
  const stored = loadStoredMachines();
  if (stored) {
    state.selected_machines = new Set(state.machines.map(m => m.machine_id).filter(id => stored.has(id)));
  }
  renderMachineChips();
}

function renderMachineChips() {
  const container = document.getElementById("machine-chips");
  container.innerHTML = "";
  for (const m of state.machines) {
    const label = document.createElement("label");
    label.style.marginRight = "8px";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = m.machine_id;
    cb.checked = !state.selected_machines || state.selected_machines.has(m.machine_id);
    cb.onchange = () => {
      const checked = Array.from(container.querySelectorAll("input:checked")).map(i => i.value);
      const all = state.machines.map(m => m.machine_id);
      state.selected_machines = checked.length === all.length ? null : new Set(checked);
      saveMachines();
      reload();
    };
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + m.machine_id + (m.self ? " (self)" : "")));
    container.appendChild(label);
  }
}

function activeMachines() {
  if (state.selected_machines) return Array.from(state.selected_machines);
  return state.machines.map(m => m.machine_id);
}

async function fetchSchedules() {
  state.schedules = [];
  const machines = activeMachines();
  const results = await Promise.all(machines.map(async m => {
    try {
      const url = `/api/schedules?machine=${encodeURIComponent(m)}${tokenParam}`;
      const r = await fetch(url).then(r => r.json());
      if (!r.ok) return [];
      return r.schedules.map(s => ({ ...s, machine_id: m }));
    } catch { return []; }
  }));
  for (const arr of results) state.schedules.push(...arr);
  renderSchedules();
}

function scheduleKey(machine, taskId) { return `${machine}|${taskId}`; }

function renderSchedules() {
  const container = document.getElementById("schedules-list");
  container.innerHTML = "";
  if (state.schedules.length === 0) {
    container.innerHTML = '<div class="empty">No schedules</div>';
    return;
  }
  state.schedules.sort((a, b) => a.id.localeCompare(b.id));
  for (const s of state.schedules) {
    const key = scheduleKey(s.machine_id, s.id);
    const div = document.createElement("div");
    div.className = "sched-item" + (state.selected_key === key ? " selected" : "");
    div.innerHTML = `
      <div class="sched-id">${escapeHtml(s.id)}<span class="machine-chip">${escapeHtml(s.machine_id)}</span></div>
      <div class="sched-meta">${escapeHtml(s.cron)} · ${escapeHtml(s.mode)}${s.enabled ? "" : " · off"}</div>
    `;
    div.onclick = () => selectSchedule(s.machine_id, s.id);
    container.appendChild(div);
  }
}

async function selectSchedule(machine, taskId) {
  state.selected_key = scheduleKey(machine, taskId);
  state.selected_run_index = null;
  state.runs = [];
  state.runs_total = 0;
  state.runs_machine = machine;
  state.runs_task = taskId;
  renderSchedules();
  document.getElementById("runs-list").innerHTML = '<div class="empty">Loading…</div>';
  document.getElementById("detail-content").innerHTML = '<div class="empty">Pick a run</div>';
  await loadMoreRuns();
}

async function loadMoreRuns() {
  if (state.runs_loading) return;
  if (state.runs.length > 0 && state.runs.length >= state.runs_total) return;
  state.runs_loading = true;
  const machine = state.runs_machine;
  const taskId = state.runs_task;
  try {
    const url = `/api/schedules/runs?task=${encodeURIComponent(taskId)}&machine=${encodeURIComponent(machine)}` +
                `&offset=${state.runs.length}&limit=${RUNS_PAGE_SIZE}${tokenParam}`;
    const r = await fetch(url).then(r => r.json());
    if (r.ok) {
      state.runs.push(...(r.runs || []));
      state.runs_total = r.total || state.runs.length;
    }
  } catch {} finally {
    state.runs_loading = false;
  }
  // Skip render if user moved to a different schedule mid-flight.
  if (state.runs_machine === machine && state.runs_task === taskId) {
    renderRuns(machine, taskId);
  }
}

function renderRuns(machine, taskId) {
  const container = document.getElementById("runs-list");
  container.innerHTML = "";
  if (state.runs.length === 0) {
    container.innerHTML = '<div class="empty">No runs yet</div>';
    return;
  }
  state.runs.forEach((run, i) => {
    const div = document.createElement("div");
    div.className = "run-item" + (state.selected_run_index === i ? " selected" : "");
    const status = run.error ? "ERROR" : "OK";
    div.innerHTML = `
      <div><span class="run-status ${status}">${status}</span> ${escapeHtml(run.time)}</div>
      <div class="run-meta">${escapeHtml(run.node_id || "")} · ${escapeHtml(run.ai_backend || "")}/${escapeHtml(run.model || "")}</div>
    `;
    div.onclick = () => selectRun(machine, taskId, i + 1);
    container.appendChild(div);
  });
  const remaining = state.runs_total - state.runs.length;
  if (remaining > 0) {
    const hint = document.createElement("div");
    hint.className = "empty";
    hint.id = "runs-load-more";
    hint.textContent = state.runs_loading ? "Loading…" : `Scroll to load ${remaining} more`;
    container.appendChild(hint);
  }
}

async function selectRun(machine, taskId, runIndex) {
  state.selected_run_index = runIndex - 1;
  renderRuns(machine, taskId);
  document.getElementById("detail-content").innerHTML = '<div class="empty">Loading…</div>';
  try {
    const url = `/api/schedules/runs/${encodeURIComponent(taskId)}/${runIndex}?machine=${encodeURIComponent(machine)}${tokenParam}`;
    const r = await fetch(url).then(r => r.json());
    if (!r.ok) {
      document.getElementById("detail-content").innerHTML = `<div class="empty">${escapeHtml(r.error || "error")}</div>`;
      return;
    }
    renderDetail(r.run, machine);
  } catch (e) {
    document.getElementById("detail-content").innerHTML = `<div class="empty">${escapeHtml(String(e))}</div>`;
  }
}

function renderDetail(run, machine) {
  const status = run.error ? "ERROR" : "OK";
  const metaRows = [
    ["time", run.time], ["task", run.task], ["node", run.node_id],
    ["mode", run.mode], ["bot", run.bot], ["backend", run.ai_backend],
    ["model", run.model], ["workspace", run.workspace],
    ["timeout", run.timeout_seconds],
  ].filter(([_k, v]) => v !== "" && v != null);

  const sessionBlock = run.session_id ? `
    <div class="detail-section">
      <h3>Session</h3>
      <div class="session-id">${escapeHtml(run.session_id)}</div>
      <div class="sched-meta" style="margin-top:4px;">
        Open in chat with <code>/resume ${escapeHtml(run.session_id)}</code>
        ${run.bot ? `(bot <strong>${escapeHtml(run.bot)}</strong> on machine <strong>${escapeHtml(machine)}</strong>)` : ""}
      </div>
      <button class="transcript-toggle" id="transcript-btn">📜 View transcript</button>
      <div id="transcript-body" style="margin-top:10px;"></div>
    </div>` : "";

  document.getElementById("detail-content").innerHTML = `
    <div class="detail-section">
      <h3>Run · <span class="run-status ${status}">${status}</span></h3>
      <dl class="detail-meta">
        ${metaRows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}
      </dl>
    </div>
    ${sessionBlock}
    ${run.prompt ? `<div class="detail-section"><h3>Prompt</h3><pre>${escapeHtml(run.prompt)}</pre></div>` : ""}
    ${run.error ? `<div class="detail-section"><h3>Error</h3><pre>${escapeHtml(run.error)}</pre></div>` : ""}
    ${run.result != null ? `<div class="detail-section"><h3>Result</h3><pre>${escapeHtml(typeof run.result === "string" ? run.result : JSON.stringify(run.result, null, 2))}</pre></div>` : ""}
    ${run.output ? `<div class="detail-section"><h3>Output</h3><pre>${escapeHtml(run.output)}</pre></div>` : ""}
  `;
  const btn = document.getElementById("transcript-btn");
  if (btn) btn.onclick = () => loadTranscript(run, machine);
}

async function loadTranscript(run, machine) {
  const body = document.getElementById("transcript-body");
  body.innerHTML = '<div class="empty">Loading…</div>';
  const params = new URLSearchParams({
    backend: run.ai_backend || "claude-cli",
    project: run.workspace || "",
    session_id: run.session_id,
    machine: machine,
  });
  try {
    const r = await fetch(`/api/claude/transcript?${params}${tokenParam}`).then(r => r.json());
    if (!r.ok) {
      body.innerHTML = `<div class="empty">${escapeHtml(r.error || "error")}</div>`;
      return;
    }
    if (!r.messages || r.messages.length === 0) {
      body.innerHTML = '<div class="empty">(no messages)</div>';
      return;
    }
    body.innerHTML = r.messages.map(renderMessage).join("");
  } catch (e) {
    body.innerHTML = `<div class="empty">${escapeHtml(String(e))}</div>`;
  }
}

function renderMessage(m) {
  const role = m.role || "?";
  let content = "";
  if (role === "user" || role === "assistant" || role === "skill_output") {
    content = escapeHtml(m.text || "");
  } else if (role === "tool_call") {
    const args = m.args ? JSON.stringify(m.args, null, 2) : "";
    content = `<strong>${escapeHtml(m.name || "")}</strong>${args ? "\n" + escapeHtml(args) : ""}`;
  } else if (role === "tool_result") {
    const ok = m.ok === false ? "❌ " : "✅ ";
    content = ok + escapeHtml(m.summary || m.error || "");
  } else {
    content = escapeHtml(JSON.stringify(m));
  }
  return `<div class="transcript-msg ${role}"><div class="transcript-role">${escapeHtml(role)}</div><div class="transcript-body">${content}</div></div>`;
}

function reload() {
  state.selected_key = null;
  state.runs = [];
  state.selected_run_index = null;
  document.getElementById("runs-list").innerHTML = '<div class="empty">Pick a schedule</div>';
  document.getElementById("detail-content").innerHTML = '<div class="empty">Pick a run</div>';
  fetchSchedules();
}

document.getElementById("reload").onclick = reload;

const runsPane = document.getElementById("runs-pane");
runsPane.addEventListener("scroll", () => {
  if (!state.runs_task) return;
  if (runsPane.scrollTop + runsPane.clientHeight >= runsPane.scrollHeight - 80) {
    loadMoreRuns();
  }
});

(async () => {
  await fetchMachines();
  await fetchSchedules();
})();
