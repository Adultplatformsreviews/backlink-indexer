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
