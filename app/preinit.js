// Pre-paint bootstrap, loaded synchronously in <head> before styles.css.
// Kept as an external file (not inline) so the Content-Security-Policy can
// forbid inline scripts (no script-src 'unsafe-inline').
//
// 1) Apply the persisted theme before styles load to avoid a flash of the
//    wrong theme (FOUC).
(function () {
  try {
    var pref = localStorage.getItem("themePreference");
    var theme;
    if (pref === "light" || pref === "dark") {
      theme = pref;
    } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      theme = "dark";
    } else {
      theme = "light";
    }
    document.documentElement.setAttribute("data-theme", theme);
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "light");
  }
})();

// 2) Apply the persisted filters-rail collapsed state before first paint.
(function () {
  try {
    if (localStorage.getItem("filtersCollapsed") === "true") {
      document.documentElement.classList.add("filters-collapsed-init");
    }
  } catch (e) {}
})();
