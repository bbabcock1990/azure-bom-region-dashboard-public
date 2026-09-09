// Pre-paint bootstrap, loaded synchronously in <head> before styles.css.
// Kept as an external file (not inline) so the Content-Security-Policy can
// forbid inline scripts (no script-src 'unsafe-inline').
//
// 1) The dashboard is dark-only — set the theme before styles load so there's
//    never a flash of a light theme (FOUC).
(function () {
  document.documentElement.setAttribute("data-theme", "dark");
})();

// 2) Apply the persisted filters-rail collapsed state before first paint.
(function () {
  try {
    if (localStorage.getItem("filtersCollapsed") === "true") {
      document.documentElement.classList.add("filters-collapsed-init");
    }
  } catch (e) {}
})();

// 3) Apply the persisted BOM-rail collapsed state before first paint.
(function () {
  try {
    if (localStorage.getItem("bomnavCollapsed") === "true") {
      document.documentElement.classList.add("bomnav-collapsed-init");
    }
  } catch (e) {}
})();
