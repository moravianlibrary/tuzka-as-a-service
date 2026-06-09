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

async function logout() {
  await fetch("/dashboard/logout", { method: "POST", headers });
  location.reload();
}

// Tab switching
document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
    loadTab(btn.dataset.tab);
  });
});

function loadTab(tab) {
  if (tab === "overview") loadOverview();
  else if (tab === "usage") loadUsage();
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
    <div class="card"><h3>Total Jobs</h3><div class="value">${stats.total_jobs}</div></div>
    <div class="card"><h3>Today</h3><div class="value">${stats.jobs_today}</div></div>
    <div class="card"><h3>Avg Duration</h3><div class="value">${stats.avg_duration_seconds ? stats.avg_duration_seconds.toFixed(1) + "s" : "-"}</div></div>
    ${Object.entries(stats.jobs_by_status).map(([k, v]) => `<div class="card"><h3>${k}</h3><div class="value">${v}</div></div>`).join("")}
  `;
  const tbody = document.getElementById("overview-backends");
  tbody.innerHTML = backends.map(b => `<tr>
    <td>${b.id}</td><td>${b.url}</td><td>${b.label || "-"}</td>
    <td>${b.inflight_now} / ${b.max_inflight}</td>
    <td class="${b.healthy ? "health-ok" : "health-bad"}">${b.healthy ? "OK" : "DOWN"}</td>
  </tr>`).join("");
}

// Usage (last 30 days, per user)
async function loadUsage() {
  const u = await fetch("/dashboard/usage?days=30", { headers }).then(r => r.json());
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

// Up to 5 line slots, each a fixed color; pick which client (or All / none) per slot.
const USAGE_SLOT_COLORS = ["#1a1a2e", "#4e79a7", "#f28e2b", "#59a14f", "#e15759"];
let usageData = null; // { elId, days, totals, series, users:[ranked] }

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
  usageData = { elId, days: u.days, totals, series: u.series, users };

  // default slots: All users, then the top 4 clients
  const defaults = ["__all__", ...users.slice(0, 4)];
  const options = (sel) =>
    `<option value="">— none —</option>`
    + `<option value="__all__"${sel === "__all__" ? " selected" : ""}>All users</option>`
    + users.map(usr => `<option value="${usr}"${sel === usr ? " selected" : ""}>${usr}</option>`).join("");
  const slots = USAGE_SLOT_COLORS.map((c, i) =>
    `<div class="uslot"><i style="background:${c}"></i><select data-slot="${i}" onchange="drawUsage()">${options(defaults[i] || "")}</select></div>`
  ).join("");

  el.innerHTML = `<div class="uslots">${slots}</div><div id="usage-svg" class="usage-svg"></div>`;
  drawUsage();
}

function drawUsage() {
  const { elId, days, totals, series } = usageData;
  const el = document.getElementById(elId);
  const chosen = [];
  el.querySelectorAll("select[data-slot]").forEach(sel => {
    if (!sel.value) return;
    const data = sel.value === "__all__" ? totals : series[sel.value];
    if (!data) return;
    chosen.push({
      name: sel.value === "__all__" ? "All users" : sel.value,
      color: USAGE_SLOT_COLORS[+sel.dataset.slot],
      data,
    });
  });

  const target = document.getElementById("usage-svg");
  if (!chosen.length) {
    target.innerHTML = `<p class="muted">Select a client to plot.</p>`;
    return;
  }

  const W = 920, H = 280, padL = 40, padR = 14, padT = 14, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const n = days.length;
  const xAt = i => padL + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const max = Math.max(1, ...chosen.flatMap(s => s.data));
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
  const lines = chosen.map(s => {
    const pts = s.data.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
    return `<polyline fill="none" stroke="${s.color}" stroke-width="2" points="${pts}"/>`;
  }).join("");

  target.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img">${grid}${xlabels}${lines}</svg>`;
}

// Users
const LIMIT_FIELDS = [
  ["rate_submit_per_minute", "submit/min"],
  ["burst_submit", "submit burst"],
  ["rate_query_per_minute", "query/min"],
  ["burst_query", "query burst"],
  ["rate_ws_per_minute", "ws/min"],
  ["burst_ws", "ws burst"],
];

async function loadUsers() {
  const [users, dUsers] = await Promise.all([
    fetch("/admin/users", { headers }).then(r => r.json()),
    fetch("/dashboard/users", { headers }).then(r => r.json()),
  ]);
  const statsMap = {};
  dUsers.forEach(u => { statsMap[u.username] = u; });
  const tbody = document.getElementById("users-table");
  tbody.innerHTML = users.map(u => {
    const s = statsMap[u.username] || { total_jobs: 0, done: 0, failed: 0 };
    const hasOverrides = LIMIT_FIELDS.some(([f]) => u[f] != null);
    const editor = LIMIT_FIELDS.map(([f, label]) =>
      `<label style="margin-right:10px">${label} <input type="number" min="0" data-field="${f}" value="${u[f] ?? ""}" placeholder="inherit" style="width:70px"></label>`
    ).join("");
    return `<tr>
      <td>${u.username}</td>
      <td>${u.active ? `<span class="status status-done">active</span>` : `<span class="status status-failed">disabled</span>`}</td>
      <td>${fmtDate(u.created_at)}</td>
      <td>${s.total_jobs}</td><td>${s.done}</td><td>${s.failed}</td>
      <td><button onclick="toggleLimits('${u.username}')">${hasOverrides ? "custom" : "default"}</button></td>
      <td class="actions">
        <button onclick="rotateKey('${u.username}')">Rotate Key</button>
        ${u.active
          ? `<button class="btn-disable" onclick="disableUser('${u.username}')">Disable</button>`
          : `<button class="btn-enable" onclick="enableUser('${u.username}')">Enable</button>`}
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
  if (data.api_key) alert("API Key (save it!): " + data.api_key);
  e.target.reset();
  loadUsers();
});

async function rotateKey(username) {
  const resp = await fetch(`/admin/users/${username}/rotate-key`, { method: "POST", headers });
  const data = await resp.json();
  if (data.api_key) alert("New API Key: " + data.api_key);
  loadUsers();
}

async function disableUser(username) {
  await fetch(`/admin/users/${username}`, { method: "DELETE", headers });
  loadUsers();
}

async function enableUser(username) {
  await fetch(`/admin/users/${username}/enable`, { method: "POST", headers });
  loadUsers();
}

// Jobs
let jobsOffset = 0;
async function loadJobs() {
  const user = document.getElementById("jobs-filter-user").value;
  const status = document.getElementById("jobs-filter-status").value;
  const params = new URLSearchParams({ limit: 50, offset: jobsOffset });
  if (user) params.set("username", user);
  if (status) params.set("status", status);
  const data = await fetch(`/dashboard/jobs?${params}`, { headers }).then(r => r.json());
  const tbody = document.getElementById("jobs-table");
  tbody.innerHTML = data.jobs.map(j => `<tr>
    <td class="jobid">${j.job_id}</td>
    <td>${j.username}</td><td>${statusBadge(j.status)}</td><td>${j.fmt}</td>
    <td>${fmtDate(j.submitted_at)}</td><td>${fmtDate(j.finished_at)}</td>
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
    <td>${b.enabled}</td><td>${b.max_inflight}</td><td>${b.inflight_now}</td>
    <td class="${b.healthy ? "health-ok" : "health-bad"}">${b.healthy ? "OK" : "DOWN"}</td>
    <td class="actions"><button onclick="deleteBackend(${b.id})">Delete</button></td>
  </tr>`).join("");
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

async function deleteBackend(id) {
  if (confirm("Delete backend?")) {
    await fetch(`/admin/backends/${id}`, { method: "DELETE", headers });
    loadBackends();
  }
}

// Config (rate limit defaults + storage TTLs)
const LIMIT_CLASSES = ["submit", "query", "ws_connect"];

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
    ? storageKeys.map(k => `<div class="storage-row"><label>${k}</label><input type="number" data-key="${k}" value="${cfg[k]}"> minutes</div>`).join("")
    : `<p class="muted">No storage TTLs configured.</p>`;
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
  await fetch("/admin/config", { method: "PUT", headers, body: JSON.stringify(values) });
  alert("Saved");
}

// Initial load — verify the master key first
ensureAuth().then(loadOverview);
