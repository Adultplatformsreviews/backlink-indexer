const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  const saved = localStorage.getItem("bi-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
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
