const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  const saved = localStorage.getItem("bi-theme");
  if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || "light";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("bi-theme", next);
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
