const MASTER_KEY = prompt("Enter Master Key:") || "";
const headers = { "X-Master-Key": MASTER_KEY, "Content-Type": "application/json" };

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
  else if (tab === "users") loadUsers();
  else if (tab === "jobs") loadJobs();
  else if (tab === "backends") loadBackends();
  else if (tab === "storage") loadStorage();
}

function statusBadge(s) {
  return `<span class="status status-${s}">${s}</span>`;
}

function fmtDate(d) {
  return d ? new Date(d).toLocaleString() : "-";
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

// Users
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
    return `<tr>
      <td>${u.username}</td><td>${u.active}</td><td>${fmtDate(u.created_at)}</td>
      <td>${s.total_jobs}</td><td>${s.done}</td><td>${s.failed}</td>
      <td class="actions">
        <button onclick="rotateKey('${u.username}')">Rotate Key</button>
        ${u.active ? `<button onclick="disableUser('${u.username}')">Disable</button>` : ""}
      </td>
    </tr>`;
  }).join("");
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
    <td title="${j.job_id}">${j.job_id.substring(0, 8)}...</td>
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

// Storage
async function loadStorage() {
  const data = await fetch("/admin/storage-config", { headers }).then(r => r.json());
  const form = document.getElementById("storage-form");
  form.innerHTML = data.map(c => `<div class="storage-row">
    <label>${c.bucket}</label>
    <input type="number" value="${c.ttl_minutes}" data-bucket="${c.bucket}"> minutes
  </div>`).join("") + `<button onclick="saveStorage()" style="margin-top:12px;padding:8px 20px;background:#1a1a2e;color:white;border:none;border-radius:4px;cursor:pointer">Save</button>`;
}

async function saveStorage() {
  const inputs = document.querySelectorAll("#storage-form input");
  const configs = Array.from(inputs).map(i => ({ bucket: i.dataset.bucket, ttl_minutes: parseInt(i.value) }));
  await fetch("/admin/storage-config", { method: "PUT", headers, body: JSON.stringify(configs) });
  alert("Saved");
}

// Initial load
loadOverview();
