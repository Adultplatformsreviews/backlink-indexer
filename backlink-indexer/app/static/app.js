/* Backlink Indexer dashboard */
const state = {
  projects: [],
  projectId: null,
  urls: [],
  total: 0,
  batches: [],
  selected: new Set(),
  jobTimer: null,
  settings: null,
  meta: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Accept": "application/json", ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}) },
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string"
      ? JSON.stringify(opts.body)
      : opts.body,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || d).join("; ");
    } catch {
      try { detail = await res.text(); } catch {}
    }
    throw new Error(detail || "Request failed");
  }
  const ctype = res.headers.get("content-type") || "";
  if (ctype.includes("application/json")) return res.json();
  return res;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  const text = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Karachi",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
  return text + " PKT";
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* Navigation */
$$(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-btn").forEach((b) => b.classList.remove("is-active"));
    btn.classList.add("is-active");
    const view = btn.dataset.view;
    $$(".view").forEach((v) => { v.hidden = v.id !== "view-" + view; });
    if (view === "settings" || view === "hub") loadSettings();
  });
});

/* Projects */
async function loadProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects;
  const sel = $("#projectSelect");
  const previous = state.projectId || Number(localStorage.getItem("bi_project") || 0);
  sel.innerHTML = state.projects.map((p) =>
    `<option value="${p.id}">${escapeHtml(p.name)} (${p.url_count})</option>`
  ).join("");
  const exists = state.projects.some((p) => p.id === previous);
  state.projectId = exists ? previous : (state.projects[0] && state.projects[0].id);
  if (state.projectId) sel.value = String(state.projectId);
  updateTargetLabel();
  await loadBatches();
  await refreshDashboard();
}

function currentProject() {
  return state.projects.find((p) => p.id === state.projectId);
}

function updateTargetLabel() {
  const p = currentProject();
  $("#targetLabel").textContent = p ? p.target_domain : "—";
}

$("#projectSelect").addEventListener("change", async (e) => {
  state.projectId = Number(e.target.value);
  localStorage.setItem("bi_project", String(state.projectId));
  state.selected.clear();
  updateTargetLabel();
  await loadBatches();
  await refreshDashboard();
});

$("#newProjectBtn").addEventListener("click", () => openProjectDialog(null));
$("#editProjectBtn").addEventListener("click", () => openProjectDialog(currentProject()));

function openProjectDialog(project) {
  const dlg = $("#projectDialog");
  $("#projectDialogTitle").textContent = project ? "Edit project" : "New project";
  $("#projName").value = project ? project.name : "";
  $("#projTarget").value = project ? project.target_domain : "feetfinder.com";
  $("#projSubs").checked = project ? !!project.include_subdomains : true;
  dlg.dataset.mode = project ? "edit" : "new";
  dlg.showModal();
}

$("#projectForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const payload = {
    name: $("#projName").value.trim(),
    target_domain: $("#projTarget").value.trim(),
    include_subdomains: $("#projSubs").checked,
  };
  try {
    if ($("#projectDialog").dataset.mode === "edit") {
      await api("/api/projects/" + state.projectId, { method: "PATCH", body: payload });
    } else {
      const res = await api("/api/projects", { method: "POST", body: payload });
      state.projectId = res.project.id;
      localStorage.setItem("bi_project", String(state.projectId));
    }
    $("#projectDialog").close();
    await loadProjects();
    toast("Project saved.", "good");
  } catch (err) {
    toast(err.message, "bad");
  }
});

/* Ingest */
$("#fileInput").addEventListener("change", () => {
  const f = $("#fileInput").files[0];
  $("#fileName").textContent = f ? f.name : "";
});

$("#addUrlsBtn").addEventListener("click", async () => {
  if (!state.projectId) return toast("Create a project first.", "bad");
  const file = $("#fileInput").files[0];
  const text = $("#urlInput").value.trim();
  const batchName = $("#batchName").value.trim();
  try {
    let result;
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("batch_name", batchName);
      result = await api(`/api/projects/${state.projectId}/urls/upload`, { method: "POST", body: fd });
    }
    if (text) {
      const pasted = await api(`/api/projects/${state.projectId}/urls`, {
        method: "POST",
        body: { urls: text, batch_name: batchName || (file ? `${file.name} (pasted)` : "") },
      });
      result = result
        ? {
            added: (result.added || 0) + pasted.added,
            duplicates: (result.duplicates || 0) + pasted.duplicates,
            skipped: (result.skipped || 0) + pasted.skipped,
          }
        : pasted;
    }
    if (!file && !text) return toast("Paste URLs or choose a file.");
    $("#urlInput").value = "";
    $("#fileInput").value = "";
    $("#fileName").textContent = "";
    toast(`Added ${result.added} URL(s). ${result.duplicates || 0} duplicate(s), ${result.skipped || 0} skipped.`, "good");
    await loadProjects();
  } catch (err) {
    toast(err.message, "bad");
  }
});

async function loadBatches() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${state.projectId}/batches`);
  state.batches = data.batches;
  const cur = $("#batchFilter").value;
  $("#batchFilter").innerHTML = `<option value="">All batches</option>` +
    state.batches.map((b) => `<option value="${b.id}">${escapeHtml(b.name)} (${b.url_count})</option>`).join("");
  if ([...$("#batchFilter").options].some((o) => o.value === cur)) $("#batchFilter").value = cur;
}

async function refreshDashboard() {
  if (!state.projectId) return;
  const q = $("#search").value.trim();
  const batch = $("#batchFilter").value;
  const filter = $("#statusFilter").value;
  const params = new URLSearchParams({ q, filter, limit: "500" });
  if (batch) params.set("batch_id", batch);
  const [list, stats] = await Promise.all([
    api(`/api/projects/${state.projectId}/urls?${params}`),
    api(`/api/projects/${state.projectId}/stats`),
  ]);
  state.urls = list.urls;
  state.total = list.total;
  renderStats(stats.stats);
  renderTable();
  updateSelectionHint();
}

function renderStats(s) {
  const cells = [
    ["Total", s.total],
    ["Live", s.live],
    ["Has link", s.has_link],
    ["No link", s.no_link],
    ["Blocked", s.blocked],
    ["Indexed", s.indexed],
    ["Unchecked", s.unchecked],
  ];
  $("#stats").innerHTML = cells.map(([k, v]) =>
    `<div class="stat"><b>${v ?? 0}</b><span>${k}</span></div>`
  ).join("");
}

function renderTable() {
  const body = $("#urlBody");
  if (!state.urls.length) {
    body.innerHTML = `<tr><td colspan="10" class="empty">${state.total === 0 ? "Add some backlink URLs to get started." : "No URLs match this filter."}</td></tr>`;
    return;
  }
  body.innerHTML = state.urls.map((u) => rowHtml(u)).join("");
  $$("#urlBody input[type=checkbox][data-id]").forEach((cb) => {
    cb.checked = state.selected.has(Number(cb.dataset.id));
    cb.addEventListener("change", () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) state.selected.add(id); else state.selected.delete(id);
      updateSelectionHint();
    });
  });
  $$("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => onRowAction(btn.dataset.action, Number(btn.dataset.id), btn.dataset.flag));
  });
}

function rowHtml(u) {
  const checked = state.selected.has(u.id) ? "checked" : "";
  const live = u.last_checked_at == null
    ? `<span class="pill mute">—</span>`
    : u.is_live
      ? `<span class="pill good">Live</span>`
      : `<span class="pill bad">Down</span>`;
  const http = u.http_status ? `<span class="code">${u.http_status}</span>` : "—";
  const barriers = barrierPills(u);
  const link = linkCell(u);
  const sub = submissionCell(u);
  const idx = indexCell(u);
  const final = u.final_url && u.final_url !== u.url
    ? `<span class="final">→ ${escapeHtml(u.final_url)}</span>`
    : "";
  const err = u.fetch_error ? `<span class="final">${escapeHtml(u.fetch_error)}</span>` : "";
  return `<tr>
    <td class="chk"><input type="checkbox" data-id="${u.id}" ${checked}></td>
    <td class="url"><a href="${escapeHtml(u.url)}" target="_blank" rel="noopener">${escapeHtml(u.url)}</a>${final}${err}</td>
    <td>${http}</td>
    <td>${live}</td>
    <td>${barriers}</td>
    <td>${link}</td>
    <td>${sub}</td>
    <td>${idx}</td>
    <td>${fmtTime(u.last_checked_at)}</td>
    <td>
      <button class="btn sm ghost" data-action="mark-yes" data-id="${u.id}">Mark indexed</button>
      <button class="btn sm ghost" data-action="mark-no" data-id="${u.id}">Mark not indexed</button>
    </td>
  </tr>`;
}

function barrierPills(u) {
  if (u.last_checked_at == null) return `<span class="pill mute">Not checked</span>`;
  const parts = [];
  if (u.noindex) parts.push(`<span class="pill bad" title="${escapeHtml(u.meta_robots || u.x_robots_tag || "noindex")}">noindex</span>`);
  if (u.robots_disallow === 1) parts.push(`<span class="pill bad">robots disallow</span>`);
  if (u.robots_disallow == null && u.last_checked_at) parts.push(`<span class="pill mute" title="robots.txt could not be read">robots unknown</span>`);
  if (u.canonicalized_away) parts.push(`<span class="pill warn" title="${escapeHtml(u.canonical || "")}">canonical elsewhere</span>`);
  if (!parts.length) return `<span class="pill good">Clear</span>`;
  return parts.join(" ");
}

function linkCell(u) {
  if (u.last_checked_at == null) return `<span class="pill mute">—</span>`;
  if (!u.has_link) return `<span class="pill bad">No link</span>`;
  const rel = u.link_rel || "dofollow";
  const kind = rel === "dofollow" ? "good" : rel === "mixed" ? "warn" : "info";
  const details = Array.isArray(u.link_details) ? u.link_details : [];
  const tip = details.slice(0, 3).map((d) => `${d.rel}: ${d.href}`).join("\n");
  return `<span class="pill ${kind}" title="${escapeHtml(tip)}">Yes · ${escapeHtml(rel)}</span>
    ${u.link_count > 1 ? `<span class="muted">×${u.link_count}</span>` : ""}`;
}

function submissionCell(u) {
  const map = {
    pending: ["mute", "Pending"],
    submitted_hub: ["info", "Hub"],
    submitted_indexnow: ["good", "IndexNow"],
    submitted_google: ["good", "Google API"],
    failed: ["bad", "Failed"],
  };
  const [k, label] = map[u.submission_status] || ["mute", u.submission_status || "Pending"];
  let extra = "";
  if (u.indexnow_status) extra += `<span class="link-tip">IndexNow: ${escapeHtml(u.indexnow_status)}</span>`;
  if (u.google_index_status) extra += `<span class="link-tip">Google: ${escapeHtml(u.google_index_status)}</span>`;
  return `<span class="pill ${k}">${label}</span>${extra}`;
}

function indexCell(u) {
  const map = {
    unknown: ["mute", "Unknown"],
    indexed: ["good", "Indexed"],
    not_indexed: ["warn", "Not found"],
    manual_indexed: ["good", "Indexed (manual)"],
    manual_not_indexed: ["warn", "Not indexed (manual)"],
  };
  const [k, label] = map[u.index_status] || ["mute", u.index_status];
  const note = u.index_note ? `<span class="link-tip">${escapeHtml(u.index_note)}</span>` : "";
  const when = u.last_index_check_at ? `<span class="link-tip">${fmtTime(u.last_index_check_at)}</span>` : "";
  return `<span class="pill ${k}">${label}</span>${note}${when}`;
}

function updateSelectionHint() {
  const n = state.selected.size;
  $("#selectionHint").textContent = n
    ? `${n} URL(s) selected. Actions will run on the selection.`
    : "No rows selected — actions run on every URL in the current filter.";
}

$("#checkAll").addEventListener("change", (e) => {
  state.selected = new Set();
  if (e.target.checked) state.urls.forEach((u) => state.selected.add(u.id));
  renderTable();
  updateSelectionHint();
});

$("#search").addEventListener("input", debounce(() => refreshDashboard(), 300));
$("#batchFilter").addEventListener("change", refreshDashboard);
$("#statusFilter").addEventListener("change", refreshDashboard);

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function actionIds() {
  if (state.selected.size) return { ids: [...state.selected] };
  const payload = { project_id: state.projectId, filter: $("#statusFilter").value, q: $("#search").value.trim() };
  if ($("#batchFilter").value) payload.batch_id = Number($("#batchFilter").value);
  return payload;
}

async function startJob(path, extra = {}) {
  try {
    const res = await api(path, { method: "POST", body: { ...actionIds(), ...extra } });
    watchJob(res.job_id);
  } catch (err) {
    toast(err.message, "bad");
  }
}

$("#precheckBtn").addEventListener("click", () => startJob("/api/urls/precheck"));
$("#indexnowBtn").addEventListener("click", () => {
  if (!confirm("IndexNow only accepts URLs on hosts you control (your hub domain or owned hosts). Third-party backlinks will be skipped. Continue?")) return;
  startJob("/api/urls/indexnow");
});
$("#googleBtn").addEventListener("click", () => {
  if (!confirm("The Google Indexing API is for your own Search Console–verified site only. Third-party backlink pages will be skipped. Continue?")) return;
  startJob("/api/urls/google-index");
});
$("#serpBtn").addEventListener("click", () => startJob("/api/urls/check-indexed"));

$("#exportBtn").addEventListener("click", () => {
  if (!state.projectId) return;
  const params = new URLSearchParams({
    filter: $("#statusFilter").value,
    q: $("#search").value.trim(),
  });
  if ($("#batchFilter").value) params.set("batch_id", $("#batchFilter").value);
  window.location = `/api/projects/${state.projectId}/export.csv?${params}`;
});

$("#deleteBtn").addEventListener("click", async () => {
  const ids = state.selected.size ? [...state.selected] : state.urls.map((u) => u.id);
  if (!ids.length) return toast("Nothing to delete.");
  if (!confirm(`Delete ${ids.length} URL(s)?`)) return;
  try {
    await api("/api/urls", { method: "DELETE", body: { ids } });
    state.selected.clear();
    toast("Deleted.", "good");
    await loadProjects();
  } catch (err) {
    toast(err.message, "bad");
  }
});

async function onRowAction(action, id) {
  try {
    if (action === "mark-yes") {
      await api("/api/urls/mark-indexed", { method: "POST", body: { id, indexed: true } });
    } else if (action === "mark-no") {
      await api("/api/urls/mark-indexed", { method: "POST", body: { id, indexed: false } });
    }
    await refreshDashboard();
  } catch (err) {
    toast(err.message, "bad");
  }
}

function watchJob(jobId) {
  const bar = $("#jobbar");
  bar.hidden = false;
  if (state.jobTimer) clearInterval(state.jobTimer);
  const tick = async () => {
    try {
      const { job } = await api("/api/jobs/" + jobId);
      const pct = job.total ? Math.round((job.done_count / job.total) * 100) : 0;
      $("#jobFill").style.width = pct + "%";
      $("#jobCount").textContent = `${job.done_count}/${job.total}`;
      $("#jobLabel").textContent = job.message || (job.status === "running" ? "Working…" : job.status);
      if (job.status === "done" || job.status === "error") {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        if (job.status === "error") toast(job.message || "Job failed", "bad");
        else toast(job.message || "Done", "good");
        await refreshDashboard();
        await loadBatches();
        setTimeout(() => { bar.hidden = true; }, 2500);
      }
    } catch (err) {
      clearInterval(state.jobTimer);
      toast(err.message, "bad");
    }
  };
  tick();
  state.jobTimer = setInterval(tick, 800);
}

/* Hub + settings */
async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  const s = data.settings;
  const hub = data.hub;
  if ($("#publicBase")) {
    $("#publicBase").value = s.public_base_url || "";
    $("#defaultTarget").value = s.default_target_domain || "feetfinder.com";
    $("#websubHub").value = s.websub_hub || "https://pubsubhubbub.appspot.com/";
    $("#indexnowKey").value = s.indexnow_key || "";
    $("#indexnowKeyUrl").textContent = s.indexnow_key_url
      ? "Key file will be served at " + s.indexnow_key_url
      : "Generate a key and set the public base URL to get a key-file URL.";
    $("#serpProvider").value = s.serp_provider || "serpapi";
    $("#serpKey").value = s.serp_api_key || "";
    $("#serpEndpoint").value = s.serp_endpoint || "";
    $("#saStatus").textContent = s.google_sa_present
      ? "Uploaded: " + (s.google_sa_email || "service account on disk")
      : "No service account uploaded.";
    $$("input[name=hubInclude]").forEach((r) => { r.checked = r.value === (s.hub_include || "all"); });
  }
  const list = $("#hostList");
  if (list) {
    list.innerHTML = (data.owned_hosts || []).map((h) =>
      `<li><span>${escapeHtml(h.host)}</span> <button class="btn sm ghost danger" data-host-id="${h.id}">Remove</button></li>`
    ).join("") || `<li class="muted">No owned hosts yet.</li>`;
    $$("[data-host-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api("/api/settings/owned-hosts/" + btn.dataset.hostId, { method: "DELETE" });
        loadSettings();
      });
    });
  }
  const links = $("#hubLinks");
  if (links && hub) {
    const rows = [
      ["HTML hub", hub.hub],
      ["Sitemap", hub.sitemap],
      ["RSS feed", hub.rss],
      ["Atom feed", hub.atom],
      ["robots.txt", hub.robots],
    ];
    links.innerHTML = rows.map(([k, v]) =>
      `<dt>${k}</dt><dd>${v ? `<a href="${escapeHtml(v)}" target="_blank" rel="noopener">${escapeHtml(v)}</a>` : "Set a public base URL"}</dd>`
    ).join("");
    $("#openHubBtn").href = hub.hub || "/hub";
  }
}

$("#saveGeneral")?.addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "POST",
      body: {
        public_base_url: $("#publicBase").value.trim(),
        default_target_domain: $("#defaultTarget").value.trim(),
        websub_hub: $("#websubHub").value.trim(),
      },
    });
    toast("Settings saved.", "good");
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
});

$("#saveKeyBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: { indexnow_key: $("#indexnowKey").value.trim() } });
    toast("IndexNow key saved.", "good");
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
});

$("#genKeyBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/settings/indexnow/generate", { method: "POST", body: {} });
    toast("New IndexNow key generated.", "good");
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
});

$("#addHostBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/settings/owned-hosts", { method: "POST", body: { host: $("#ownedHost").value.trim() } });
    $("#ownedHost").value = "";
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
});

$("#saveHubInclude")?.addEventListener("click", async () => {
  const value = ($$("input[name=hubInclude]").find((r) => r.checked) || {}).value || "all";
  try {
    await api("/api/settings", { method: "POST", body: { hub_include: value } });
    toast("Hub filter saved.", "good");
  } catch (err) { toast(err.message, "bad"); }
});

$("#saveSerp")?.addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "POST",
      body: {
        serp_provider: $("#serpProvider").value,
        serp_api_key: $("#serpKey").value,
        serp_endpoint: $("#serpEndpoint").value.trim(),
      },
    });
    toast("SERP settings saved.", "good");
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
});

$("#saFile")?.addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await api("/api/settings/google-sa", { method: "POST", body: fd });
    toast("Service account uploaded: " + (res.client_email || ""), "good");
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
  e.target.value = "";
});

$("#deleteSaBtn")?.addEventListener("click", async () => {
  if (!confirm("Remove the uploaded service-account file?")) return;
  try {
    await api("/api/settings/google-sa", { method: "DELETE" });
    toast("Removed.");
    loadSettings();
  } catch (err) { toast(err.message, "bad"); }
});

$("#pingHubBtn")?.addEventListener("click", async () => {
  $("#pingResult").textContent = "Pinging…";
  try {
    const res = await api("/api/hub/ping", { method: "POST", body: {} });
    const bits = [];
    bits.push("WebSub: " + (res.websub.ok ? "ok" : "failed") + " — " + (res.websub.detail || ""));
    bits.push("Bing sitemap ping: " + (res.bing_sitemap_ping.ok ? "ok" : "failed") + " — " + (res.bing_sitemap_ping.detail || ""));
    bits.push(res.google_sitemap_ping.detail);
    $("#pingResult").textContent = bits.join(" | ");
    toast("Ping finished.", res.websub.ok || res.bing_sitemap_ping.ok ? "good" : "");
  } catch (err) {
    $("#pingResult").textContent = err.message;
    toast(err.message, "bad");
  }
});

loadProjects().catch((err) => toast(err.message, "bad"));
