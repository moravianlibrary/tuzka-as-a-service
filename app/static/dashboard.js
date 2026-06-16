// Auth is carried by a short-lived httpOnly cookie that the server sets after
// the master key is verified once via /dashboard/login. The raw key is never
// stored in the browser, and a page refresh stays logged in until the cookie
// expires (or you log out).
let headers = { "Content-Type": "application/json" };

// Verify the session before showing anything: reuse an existing cookie if it
// is still valid, otherwise prompt for the master key and exchange it for one.
async function ensureAuth() {
  if ((await fetch("/dashboard/stats", { headers })).ok) return;
  while (true) {
    const key = prompt("Enter Master Key:") || "";
    const r = await fetch("/dashboard/login", {
      method: "POST",
      headers: { "X-Master-Key": key },
    });
    if (r.ok) return;
    alert("Invalid master key — please try again.");
  }
}

// Reveal a freshly minted key once, in a copyable field (it is never stored
// server-side, so this is the only chance to grab it).
function showKey(title, key) {
  document.getElementById("key-title").textContent = title;
  document.getElementById("key-value").value = key;
  document.getElementById("key-dialog").setAttribute("open", "");
}
function closeKey() {
  document.getElementById("key-dialog").removeAttribute("open");
}
async function copyKey(btn) {
  const input = document.getElementById("key-value");
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.select();
    document.execCommand("copy"); // fallback for non-secure (http) contexts
  }
  const label = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => { btn.textContent = label; }, 1200);
}

async function logout() {
  await fetch("/dashboard/logout", { method: "POST", headers });
  location.reload();
}

// Tab switching — the active tab is mirrored to ?tab= so a refresh (or a shared
// link) reopens the same tab.
const TABS = ["overview", "users", "jobs", "backends", "config"];

function currentTabFromUrl() {
  const t = new URLSearchParams(location.search).get("tab");
  return TABS.includes(t) ? t : "overview";
}

function activateTab(tab, { push = false } = {}) {
  if (!TABS.includes(tab)) tab = "overview";
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-content").forEach(t =>
    t.classList.toggle("active", t.id === tab));
  const url = `?tab=${tab}`;
  if (push) history.pushState({ tab }, "", url);
  else history.replaceState({ tab }, "", url);
  loadTab(tab);
}

document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab, { push: true }));
});

// Back/forward navigation between tabs.
window.addEventListener("popstate", e =>
  activateTab((e.state && e.state.tab) || currentTabFromUrl()));

function loadTab(tab) {
  if (tab === "overview") loadOverview();
  else if (tab === "users") loadUsers();
  else if (tab === "jobs") loadJobs();
  else if (tab === "backends") loadBackends();
  else if (tab === "config") loadConfig();
}

function statusBadge(s) {
  return `<span class="status status-${s}">${s}</span>`;
}

// Czech locale formatting
function fmtDate(d) {
  return d ? new Date(d).toLocaleString("cs-CZ") : "-";
}

// Same, with millisecond precision — used in the job detail modal where the
// sub-second phase boundaries (dispatched/started/finished/stored) matter.
function fmtDateMs(d) {
  if (!d) return "-";
  const dt = new Date(d);
  return `${dt.toLocaleString("cs-CZ")}.${String(dt.getMilliseconds()).padStart(3, "0")}`;
}

// Human duration between two ISO timestamps, or em-dash if either is missing.
function fmtDuration(fromIso, toIso) {
  if (!fromIso || !toIso) return "—";
  const s = (new Date(toIso) - new Date(fromIso)) / 1000;
  if (isNaN(s) || s < 0) return "—";
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

// Total time in the system: submitted -> stored (result available).
function fmtTimeInSystem(j) {
  return fmtDuration(j.submitted_at, j.stored_at);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// "YYYY-MM-DD" -> "DD.MM."
function dayLabel(day) {
  return `${day.slice(8, 10)}.${day.slice(5, 7)}.`;
}

// Overview
async function loadOverview() {
  const [stats, backends] = await Promise.all([
    fetch("/dashboard/stats", { headers }).then(r => r.json()),
    fetch("/dashboard/backends", { headers }).then(r => r.json()),
  ]);
  const cards = document.getElementById("stat-cards");
  cards.innerHTML = `
    <article class="card"><h3>Total Jobs</h3><div class="value">${stats.total_jobs}</div></article>
    <article class="card"><h3>Avg Duration</h3><div class="value">${stats.avg_duration_seconds ? stats.avg_duration_seconds.toFixed(1) + "s" : "-"}</div></article>
    ${Object.entries(stats.jobs_by_status).map(([k, v]) => `<article class="card"><h3>${k}</h3><div class="value">${v}</div></article>`).join("")}
  `;
  const tbody = document.getElementById("overview-backends");
  tbody.innerHTML = backends.map(b => `<tr>
    <td>${b.id}</td><td>${b.url}</td><td>${b.label || "-"}</td>
    <td>${b.inflight_now} / ${b.max_inflight}</td>
    ${healthCell(b.healthy)}
  </tr>`).join("");

  // The 30-day usage section now lives under Overview too.
  await loadUsage();
}

// Usage (last 30 days, per user)
async function loadUsage() {
  populateStatsYearPicker();
  const u = await fetch("/dashboard/usage?days=30", { headers }).then(r => r.json());
  renderStatusChart(u, "status-chart");
  renderUsage(u, "usage-chart");

  // per-user totals over the window
  const totalsByUser = u.users
    .map(usr => ({ usr, total: u.series[usr].reduce((a, b) => a + b, 0) }))
    .sort((a, b) => b.total - a.total);
  const tbody = document.getElementById("usage-users-table");
  tbody.innerHTML = totalsByUser.length
    ? totalsByUser.map(r => `<tr><td>${r.usr}</td><td>${r.total}</td></tr>`).join("")
    : `<tr><td colspan="2" class="muted">No jobs in the last 30 days.</td></tr>`;
}

// Fill the export year picker from the years that actually have data, once.
// Defaults to the current year when present, otherwise the newest available.
async function populateStatsYearPicker() {
  const sel = document.getElementById("stats-year");
  if (!sel || sel.options.length) return;
  const { years } = await fetch("/dashboard/stats/years", { headers }).then(r => r.json());
  const cur = new Date().getFullYear();
  sel.innerHTML = years
    .map(y => `<option value="${y}"${y === cur ? " selected" : ""}>${y}</option>`)
    .join("");
}

// CSV download — the session cookie is sent automatically, so a plain navigation
// works (no need to attach the auth header).
function downloadStatsCsv() {
  const year = document.getElementById("stats-year").value || new Date().getFullYear();
  window.location = `/dashboard/stats.csv?year=${year}`;
}

// Stacked-area usage: the top-N users each get their own band (coloured along a
// sequential gradient), and every remaining user is folded into a "rest" band.
// Use 1, 2 or 3 stops here — series are sampled evenly along them.
const USAGE_GRADIENT = ["#f28e2b", "#4e79a7"]; // orange -> blue
const USAGE_REST_COLOR = "#c9ccd3";
let usageData = null; // { days, totals, series, users:[ranked desc by total] }

function hexToRgb(h) {
  const v = parseInt(h.slice(1), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}
// t in [0,1] mapped across the gradient stops
function rampColor(t) {
  const s = USAGE_GRADIENT;
  if (s.length === 1) return s[0];
  const seg = t * (s.length - 1);
  const i = Math.min(Math.floor(seg), s.length - 2);
  const f = seg - i, a = hexToRgb(s[i]), b = hexToRgb(s[i + 1]);
  return `rgb(${a.map((c, k) => Math.round(c + (b[k] - c) * f)).join(",")})`;
}

function renderUsage(u, elId) {
  const el = document.getElementById(elId);
  if (!u.users.length) {
    el.innerHTML = `<p class="muted">No jobs in the last ${u.days.length} days.</p>`;
    return;
  }
  const totals = u.days.map((_, i) => u.users.reduce((s, usr) => s + u.series[usr][i], 0));
  const users = u.users
    .map(usr => ({ usr, total: u.series[usr].reduce((a, b) => a + b, 0) }))
    .sort((a, b) => b.total - a.total)
    .map(r => r.usr);
  usageData = { days: u.days, totals, series: u.series, users };

  const topN = Math.min(users.length, 4); // default
  el.innerHTML = `<div class="uslots">
      <label class="uslot">Users shown
        <input id="usage-topn" type="number" min="1" max="${users.length}" value="${topN}" style="width:64px" onchange="drawUsage()">
      </label>
      <span id="usage-legend" class="uslots"></span>
    </div>
    <div id="usage-svg" class="usage-svg"></div>`;
  drawUsage();
}

// Render a stacked-area chart (bands stacked bottom->top) into targetEl, with a
// swatch legend in legendEl. stack: [{ name, color, data:number[] }].
function drawStackedArea(targetEl, legendEl, days, stack) {
  const n = days.length;
  const W = 920, H = 280, padL = 40, padR = 14, padT = 14, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xAt = i => padL + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const totals = days.map((_, i) => stack.reduce((s, ser) => s + ser.data[i], 0));
  const max = Math.max(1, ...totals);
  const yAt = v => padT + plotH - (v / max) * plotH;

  const grid = [0, max / 2, max].map(t => {
    const y = yAt(t).toFixed(1);
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#eee"/>`
      + `<text x="${padL - 6}" y="${(+y + 3).toFixed(1)}" text-anchor="end" font-size="10" fill="#999">${Math.round(t)}</text>`;
  }).join("");
  const xlabels = days.map((d, i) =>
    (i % 5 === 0 || i === n - 1)
      ? `<text x="${xAt(i).toFixed(1)}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#999">${dayLabel(d)}</text>`
      : "").join("");

  const cum = new Array(n).fill(0);
  const areas = stack.map(s => {
    const lower = cum.map((c, i) => `${xAt(i).toFixed(1)},${yAt(c).toFixed(1)}`).reverse();
    for (let i = 0; i < n; i++) cum[i] += s.data[i];
    const upper = cum.map((c, i) => `${xAt(i).toFixed(1)},${yAt(c).toFixed(1)}`);
    return `<polygon points="${upper.join(" ")} ${lower.join(" ")}" fill="${s.color}" fill-opacity="0.9" stroke="#fff" stroke-width="0.5"/>`;
  }).join("");

  if (legendEl) legendEl.innerHTML = stack.map(s =>
    `<span class="uslot"><i style="background:${s.color}"></i>${s.name}</span>`).join("");
  targetEl.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img">${grid}${areas}${xlabels}</svg>`;
}

function drawUsage() {
  const { days, series, users } = usageData;
  let topN = parseInt(document.getElementById("usage-topn").value) || 1;
  topN = Math.max(1, Math.min(topN, users.length));

  const shown = users.slice(0, topN);
  const rest = users.slice(topN);
  const stack = shown.map((usr, j) => ({
    name: usr,
    color: rampColor(shown.length > 1 ? j / (shown.length - 1) : 0),
    data: series[usr],
  }));
  if (rest.length) {
    stack.push({
      name: `rest (${rest.length})`,
      color: USAGE_REST_COLOR,
      data: days.map((_, i) => rest.reduce((s, usr) => s + series[usr][i], 0)),
    });
  }
  drawStackedArea(
    document.getElementById("usage-svg"),
    document.getElementById("usage-legend"),
    days, stack,
  );
}

// Status-over-time stacked area: fixed series/colours matching the status
// badges; "failed" sits on top (last) so its thin band stays visible.
const STATUS_COLORS = { done: "#28a745", running: "#f0ad4e", queued: "#5bc0de", failed: "#dc3545" };
const STATUS_ORDER = ["done", "running", "queued", "failed"]; // bottom -> top

function renderStatusChart(u, elId) {
  const el = document.getElementById(elId);
  const ss = u.status_series || {};
  const hasAny = Object.values(ss).some(arr => arr.some(v => v > 0));
  if (!hasAny) {
    el.innerHTML = `<p class="muted">No jobs in the last ${u.days.length} days.</p>`;
    return;
  }
  el.innerHTML = `<span id="status-legend" class="uslots"></span><div id="status-svg" class="usage-svg"></div>`;
  const stack = STATUS_ORDER
    .filter(s => ss[s])
    .map(s => ({ name: s, color: STATUS_COLORS[s], data: ss[s] }));
  drawStackedArea(
    document.getElementById("status-svg"),
    document.getElementById("status-legend"),
    u.days, stack,
  );
}

// Users — one row per rate-limit class, same shape as the Config tab table.
const LIMIT_ROWS = [
  ["submit", "rate_submit_per_minute", "burst_submit"],
  ["query", "rate_query_per_minute", "burst_query"],
  ["ws", "rate_ws_per_minute", "burst_ws"],
];

let usersSort = "username"; // "username" (A→Z) | "created" (newest first)
function setUsersSort(key) { usersSort = key; loadUsers(); }

async function loadUsers() {
  const [users, dUsers] = await Promise.all([
    fetch("/admin/users", { headers }).then(r => r.json()),
    fetch("/dashboard/users", { headers }).then(r => r.json()),
  ]);
  users.sort((a, b) => usersSort === "created"
    ? new Date(b.created_at) - new Date(a.created_at)
    : a.username.localeCompare(b.username));
  document.getElementById("th-username").textContent = "Username" + (usersSort === "username" ? " ▾" : "");
  document.getElementById("th-created").textContent = "Created" + (usersSort === "created" ? " ▾" : "");
  const statsMap = {};
  dUsers.forEach(u => { statsMap[u.username] = u; });
  const tbody = document.getElementById("users-table");
  tbody.innerHTML = users.map(u => {
    const s = statsMap[u.username] || { total_jobs: 0, done: 0, failed: 0 };
    const hasOverrides = LIMIT_ROWS.some(([, pm, b]) => u[pm] != null || u[b] != null);
    const editor = `<table>
      <thead><tr><th>Class</th><th>Per minute</th><th>Burst</th></tr></thead>
      <tbody>${LIMIT_ROWS.map(([cls, pm, b]) => `<tr><td>${cls}</td>
        <td><input type="number" min="0" data-field="${pm}" value="${u[pm] ?? ""}" placeholder="inherit" style="width:90px"></td>
        <td><input type="number" min="0" data-field="${b}" value="${u[b] ?? ""}" placeholder="inherit" style="width:90px"></td></tr>`).join("")}</tbody>
    </table>`;
    return `<tr>
      <td>${u.username}</td>
      <td>${u.active ? `<span class="status status-done">active</span>` : `<span class="status status-failed">disabled</span>`}</td>
      <td>${fmtDate(u.created_at)}</td>
      <td>${s.total_jobs}</td><td>${s.done}</td><td>${s.failed}</td>
      <td class="actions"><button onclick="toggleLimits('${u.username}')">${hasOverrides ? "custom" : "default"}</button></td>
      <td class="actions">
        <button onclick="rotateKey('${u.username}')">Rotate Key</button>
        ${u.active
          ? `<button class="btn-disable" onclick="setUserActive('${u.username}', false)">Disable</button>`
          : `<button class="btn-enable" onclick="setUserActive('${u.username}', true)">Enable</button>`}
        <button class="btn-delete" onclick="deleteUser('${u.username}')" ${s.total_jobs ? "disabled title='User still has jobs'" : ""}>Delete</button>
      </td>
    </tr>
    <tr id="limits-${u.username}" style="display:none"><td colspan="8">
      ${editor}
      <button onclick="saveLimits('${u.username}')">Save limits</button>
      <span class="muted">(empty = inherit default)</span>
    </td></tr>`;
  }).join("");
}

function toggleLimits(username) {
  const row = document.getElementById(`limits-${username}`);
  row.style.display = row.style.display === "none" ? "" : "none";
}

async function saveLimits(username) {
  const body = {};
  document.querySelectorAll(`#limits-${username} input`).forEach(i => {
    body[i.dataset.field] = i.value === "" ? null : parseInt(i.value);
  });
  await fetch(`/admin/users/${username}`, { method: "PATCH", headers, body: JSON.stringify(body) });
  loadUsers();
}

document.getElementById("add-user-form").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const resp = await fetch("/admin/users", { method: "POST", headers, body: JSON.stringify({ username: fd.get("username") }) });
  const data = await resp.json();
  if (data.api_key) showKey(`API key for ${fd.get("username")}`, data.api_key);
  e.target.reset();
  loadUsers();
});

async function rotateKey(username) {
  const resp = await fetch(`/admin/users/${username}/rotate-key`, { method: "POST", headers });
  const data = await resp.json();
  if (data.api_key) showKey(`New API key for ${username}`, data.api_key);
  loadUsers();
}

// Enable/disable via PATCH — same mechanism as backends.
async function setUserActive(username, active) {
  await fetch(`/admin/users/${username}`, {
    method: "PATCH", headers, body: JSON.stringify({ active }),
  });
  loadUsers();
}

async function deleteUser(username) {
  if (!confirm(`Permanently delete user "${username}"? This cannot be undone.`)) return;
  const resp = await fetch(`/admin/users/${username}`, { method: "DELETE", headers });
  if (!resp.ok) {
    const e = await resp.json().catch(() => ({}));
    alert(e.detail || "Delete failed");
  }
  loadUsers();
}

// Jobs
let jobsOffset = 0;
let lastJobs = []; // current page, indexed by openJobDialog

function openJobDialog(i) {
  const j = lastJobs[i];
  if (!j) return;
  document.getElementById("job-dialog-body").innerHTML = `
    <div class="kv"><span>Job ID</span><code>${j.job_id}</code></div>
    <div class="kv"><span>External ID</span><code>${j.external_id || "—"}</code></div>
    <div class="kv"><span>User</span>${j.username}</div>
    <div class="kv"><span>Status</span>${statusBadge(j.status)}</div>
    <div class="kv"><span>Format</span>${j.fmt}</div>
    <div class="kv"><span>Domain</span>${j.domain || "—"}</div>
    <div class="kv"><span>Backend</span>${j.backend || (j.backend_id ? "#" + j.backend_id : "—")}</div>
    <div class="kv"><span>Engine version</span>${j.engine_version || "—"}</div>
    <hr>
    <div class="kv"><span>Submitted</span>${fmtDateMs(j.submitted_at)}</div>
    <div class="kv"><span>Dispatched</span>${fmtDateMs(j.dispatched_at)}</div>
    <div class="kv"><span>Started</span>${fmtDateMs(j.started_at)}</div>
    <div class="kv"><span>Finished</span>${fmtDateMs(j.finished_at)}</div>
    <div class="kv"><span>Stored</span>${fmtDateMs(j.stored_at)}</div>
    <hr>
    <div class="kv"><span>Queued (taas)</span>${fmtDuration(j.submitted_at, j.dispatched_at)}</div>
    <div class="kv"><span>Engine queue</span>${fmtDuration(j.dispatched_at, j.started_at)}</div>
    <div class="kv"><span>OCR running</span>${fmtDuration(j.started_at, j.finished_at)}</div>
    <div class="kv"><span>Store</span>${fmtDuration(j.finished_at, j.stored_at)}</div>
    <div class="kv"><span>Time in system</span>${fmtDuration(j.submitted_at, j.stored_at)}</div>
    ${j.error ? `<div class="job-error"><span>Error</span><pre>${escapeHtml(j.error)}</pre></div>` : ""}`;
  document.getElementById("job-dialog").showModal();
}
function closeJobDialog() {
  document.getElementById("job-dialog").close();
}
function clearJobsFilters() {
  document.getElementById("jobs-filter-user").value = "";
  document.getElementById("jobs-filter-status").value = "";
  jobsOffset = 0;
  loadJobs();
}
async function loadJobs() {
  const user = document.getElementById("jobs-filter-user").value;
  const status = document.getElementById("jobs-filter-status").value;
  const params = new URLSearchParams({ limit: 50, offset: jobsOffset });
  if (user) params.set("username", user);
  if (status) params.set("status", status);
  const data = await fetch(`/dashboard/jobs?${params}`, { headers }).then(r => r.json());
  lastJobs = data.jobs;
  const tbody = document.getElementById("jobs-table");
  // Rows are clickable -> full lifecycle detail (incl. error) in a dialog, to keep
  // the table compact. The table shows only submitted time + total time in system.
  tbody.innerHTML = data.jobs.map((j, i) => `<tr class="clickable" onclick="openJobDialog(${i})">
    <td class="jobid">${j.job_id}</td>
    <td>${j.username}</td><td>${statusBadge(j.status)}</td><td>${j.fmt}</td>
    <td>${fmtDate(j.submitted_at)}</td><td>${fmtTimeInSystem(j)}</td>
  </tr>`).join("");
  const pag = document.getElementById("jobs-pagination");
  const pages = Math.ceil(data.total / 50);
  const current = Math.floor(jobsOffset / 50);
  pag.innerHTML = Array.from({ length: Math.min(pages, 10) }, (_, i) =>
    `<button class="${i === current ? "active" : ""}" onclick="jobsOffset=${i * 50};loadJobs()">${i + 1}</button>`
  ).join("");
}

// Backends
async function loadBackends() {
  const data = await fetch("/dashboard/backends", { headers }).then(r => r.json());
  const tbody = document.getElementById("backends-table");
  tbody.innerHTML = data.map(b => `<tr>
    <td>${b.id}</td><td>${b.url}</td><td>${b.label || "-"}</td>
    <td>${b.enabled}</td>
    <td><input type="number" min="1" value="${b.max_inflight}" id="mi-${b.id}" style="width:70px">
      <button onclick="saveMaxInflight(${b.id})">Save</button></td>
    <td>${b.inflight_now}</td>
    ${healthCell(b.healthy)}
    <td class="actions">${b.enabled
      ? `<button class="btn-disable" onclick="disableBackend(${b.id})">Disable</button>`
      : `<button class="btn-enable" onclick="enableBackend(${b.id})">Enable</button>`}
      <button class="btn-delete" onclick="deleteBackend(${b.id})" ${b.can_delete ? "" : "disabled title='Backend still has jobs'"}>Delete</button></td>
  </tr>`).join("");
}

async function saveMaxInflight(id) {
  const v = parseInt(document.getElementById(`mi-${id}`).value);
  if (!v || v < 1) { alert("Max in-flight must be ≥ 1"); return; }
  await fetch(`/admin/backends/${id}`, {
    method: "PATCH", headers, body: JSON.stringify({ max_inflight: v }),
  });
  loadBackends();
}

async function deleteBackend(id) {
  if (!confirm(`Permanently delete backend ${id}? This cannot be undone.`)) return;
  const resp = await fetch(`/admin/backends/${id}`, { method: "DELETE", headers });
  if (!resp.ok) {
    const e = await resp.json().catch(() => ({}));
    alert(e.detail || "Delete failed");
  }
  loadBackends();
}

// Disabled backends aren't probed, so healthy is null -> show a neutral dash.
function healthCell(healthy) {
  if (healthy === null) return `<td class="health-na">—</td>`;
  return `<td class="${healthy ? "health-ok" : "health-bad"}">${healthy ? "OK" : "DOWN"}</td>`;
}

document.getElementById("add-backend-form").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await fetch("/admin/backends", { method: "POST", headers, body: JSON.stringify({
    url: fd.get("url"), label: fd.get("label") || null,
    api_key: fd.get("api_key") || null, max_inflight: parseInt(fd.get("max_inflight")) || 4,
  })});
  e.target.reset();
  loadBackends();
});

async function disableBackend(id) {
  if (confirm("Disable backend? It will be taken out of rotation.")) {
    await fetch(`/admin/backends/${id}`, {
      method: "PATCH", headers, body: JSON.stringify({ enabled: false }),
    });
    loadBackends();
  }
}

async function enableBackend(id) {
  await fetch(`/admin/backends/${id}`, {
    method: "PATCH", headers, body: JSON.stringify({ enabled: true }),
  });
  loadBackends();
}

// Config (rate limit defaults + storage TTLs)
const LIMIT_CLASSES = ["submit", "query", "ws_connect"];
const STORAGE_LABELS = {
  "storage.incoming_ttl_minutes": "Incoming files",
  "storage.results_ttl_minutes": "Results",
};
const POLICY_LABELS = {
  "jobs.queued_timeout_seconds": ["Queued timeout", "seconds"],
  "jobs.running_timeout_seconds": ["Running timeout", "seconds"],
  "presigned.ttl_minutes": ["Presigned URL TTL", "minutes"],
};
// Job-record retention is hardcoded to 30 days in the cleanup worker, so it is no
// longer an operator-tunable policy.
const POLICY_MIN = {};
function storageLabel(k) {
  return STORAGE_LABELS[k] || k.replace(/^storage\./, "").replace(/_ttl_minutes$/, "").replace(/_/g, " ");
}

async function loadConfig() {
  const cfg = await fetch("/admin/config", { headers }).then(r => r.json());
  const tbody = document.getElementById("config-limits");
  tbody.innerHTML = LIMIT_CLASSES.map(cls => {
    const v = cfg[`rate_limit.${cls}`] || {};
    return `<tr><td>${cls}</td>
      <td><input type="number" min="1" data-cfg="rate_limit.${cls}" data-field="per_minute" value="${v.per_minute ?? ""}" style="width:90px"></td>
      <td><input type="number" min="0" data-cfg="rate_limit.${cls}" data-field="burst" value="${v.burst ?? ""}" style="width:90px"></td></tr>`;
  }).join("");
  const storage = document.getElementById("config-storage");
  const storageKeys = Object.keys(cfg).filter(k => k.startsWith("storage."));
  storage.innerHTML = storageKeys.length
    ? storageKeys.map(k => `<div class="storage-row"><label>${storageLabel(k)}</label><input type="number" data-key="${k}" value="${cfg[k]}"> minutes</div>`).join("")
    : `<p class="muted">No storage TTLs configured.</p>`;
  const policy = document.getElementById("config-policy");
  if (policy) {
    policy.innerHTML = Object.entries(POLICY_LABELS).map(([k, [label, unit]]) =>
      `<div class="storage-row"><label>${label}</label>` +
      `<input type="number" min="${POLICY_MIN[k] ?? 1}" data-key="${k}" value="${cfg[k] ?? ""}"> ${unit}</div>`
    ).join("");
  }
}

async function saveConfig() {
  const values = {};
  document.querySelectorAll("#config-limits input").forEach(i => {
    values[i.dataset.cfg] = values[i.dataset.cfg] || {};
    values[i.dataset.cfg][i.dataset.field] = parseInt(i.value);
  });
  document.querySelectorAll("#config-storage input").forEach(i => {
    values[i.dataset.key] = parseInt(i.value);
  });
  document.querySelectorAll("#config-policy input").forEach(i => {
    values[i.dataset.key] = parseInt(i.value);
  });
  await fetch("/admin/config", { method: "PUT", headers, body: JSON.stringify(values) });
  alert("Saved");
}

// Initial load — verify the master key first
ensureAuth().then(() => activateTab(currentTabFromUrl()));
