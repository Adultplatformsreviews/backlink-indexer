const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  try { localStorage.removeItem("bi-theme"); } catch (e) {}
  const saved = localStorage.getItem("bi-theme-2");
  if (saved === "dark") document.documentElement.dataset.theme = "dark";
  else document.documentElement.removeAttribute("data-theme");
  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    if (next === "dark") document.documentElement.dataset.theme = "dark";
    else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("bi-theme-2", next);
  });
}
const toggle = document.getElementById("menuToggle");
const sidebar = document.getElementById("sidebar");
if (toggle && sidebar) {
  toggle.addEventListener("click", () => sidebar.classList.toggle("open"));
}
const all = document.getElementById("checkAll");
if (all) {
  all.addEventListener("change", () => {
    document.querySelectorAll('input[name="ids"]').forEach((box) => { box.checked = all.checked; });
  });
}

const drawer = document.getElementById("historyDrawer");
const historyList = document.getElementById("historyList");
const historyUrl = document.getElementById("historyUrl");
const historyClose = document.getElementById("historyClose");
if (drawer && historyList) {
  document.querySelectorAll(".history-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      historyList.replaceChildren();
      historyUrl.textContent = "Loading…";
      drawer.showModal();
      try {
        const response = await fetch(button.dataset.history, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("Could not load history");
        const payload = await response.json();
        historyUrl.textContent = payload.url || "";
        const attempts = payload.attempts || [];
        if (!attempts.length) {
          const item = document.createElement("li");
          item.textContent = "No submission attempts yet.";
          historyList.append(item);
          return;
        }
        attempts.forEach((attempt) => {
          const item = document.createElement("li");
          const title = document.createElement("strong");
          title.textContent = `${attempt.method || "engine"} · ${attempt.status || ""}`;
          const when = document.createElement("span");
          when.textContent = attempt.created_at || "";
          const detail = document.createElement("p");
          detail.textContent = attempt.detail || "";
          item.append(title, when, detail);
          historyList.append(item);
        });
      } catch (error) {
        historyUrl.textContent = error.message || "Could not load history";
      }
    });
  });
  if (historyClose) historyClose.addEventListener("click", () => drawer.close());
}

document.querySelectorAll("[data-engine-group]").forEach((group) => {
  const master = group.querySelector("[data-engine-all]");
  const boxes = Array.from(group.querySelectorAll("[data-engine]"));
  if (!master || !boxes.length) return;
  const sync = () => {
    const on = boxes.filter((box) => box.checked).length;
    master.checked = on === boxes.length;
    master.indeterminate = on > 0 && on < boxes.length;
  };
  master.addEventListener("change", () => {
    boxes.forEach((box) => { box.checked = master.checked; });
    master.indeterminate = false;
  });
  boxes.forEach((box) => box.addEventListener("change", sync));
  sync();
});

const processForm = document.getElementById("processForm");
const processProgress = document.getElementById("processProgress");
async function processUntilDone() {
  if (!processForm || !processProgress) return;
  processProgress.hidden = false;
  let guard = 0;
  while (guard < 80) {
    guard += 1;
    processProgress.textContent = "Processing a batch…";
    const response = await fetch(processForm.action, {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      processProgress.textContent = "Processing stopped (" + response.status + "). Run Process due now to continue.";
      return;
    }
    const data = await response.json();
    processProgress.textContent = data.message || "Batch finished.";
    if (!data.continue) {
      window.location.reload();
      return;
    }
  }
  window.location.reload();
}
if (processForm) {
  processForm.addEventListener("submit", (event) => {
    event.preventDefault();
    processUntilDone();
  });
  if (new URLSearchParams(window.location.search).get("continue") === "1") processUntilDone();
}
