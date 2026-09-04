/* Azure BOM Region Support Dashboard logic */
"use strict";

const STATE = {
  snapshot: null,             // current loaded snapshot
  snapshots: [],              // available snapshots
  filtered: [],               // currently visible regions
  sortKey: "name",
  sortDir: 1,
  view: "overview",
  regionsSub: "table",        // active sub-view within the Regions tab
  settingsTab: "owner",       // active tab within the Settings view
  selectedSlots: ["", "", ""],
  map: null,
  mapLayer: null,
  latencyChart: null,
  overviewCharts: {},         // {chartId: Chart instance}
  activeBomId: "",            // currently selected BOM id (a BOM owns a subscription)
  snapshotDiff: null,
  quotaRequests: {},          // keyed by "region::family::bom"
  activeDrilldownRegion: "",
  activeSubscription: null,
  pendingQuotaPanelCollapsed: false,
  quotaHierarchyCollapsed: false,
};

const _quotaPollers = new Map(); // key: `${region}:${family}:${bom}`

// The subscription targeted by the active BOM (used for activity-log scoping
// and as the GUID sent to /api/runs). Derived from the active BOM's metadata.
function activeSubscriptionId() {
  const meta = STATE.activeBomId ? getBomMeta(STATE.activeBomId) : null;
  return primarySubscriptionId(meta);
}

function subscriptionList(meta) {
  if (!meta) return [];
  const raw = Array.isArray(meta.subscription_ids) && meta.subscription_ids.length
    ? meta.subscription_ids
    : [meta.subscription_id];
  const out = [];
  for (const item of raw) {
    const value = String(item || "").trim();
    if (value && !out.includes(value)) out.push(value);
  }
  return out;
}

function primarySubscriptionId(meta) {
  return subscriptionList(meta)[0] || "";
}

function summarizeSubscriptions(meta) {
  const ids = subscriptionList(meta);
  if (!ids.length) return "—";
  if (ids.length === 1) return ids[0];
  return `${ids[0]} (+${ids.length - 1} more)`;
}

function summarizeSubscriptionCount(meta) {
  const ids = subscriptionList(meta);
  if (!ids.length) return "0";
  return ids.length === 1 ? "1 subscription" : `${ids.length} subscriptions`;
}

function activeBomMeta() {
  return STATE.activeBomId ? getBomMeta(STATE.activeBomId) : null;
}

function availableQuotaSubscriptionIds() {
  const metaIds = subscriptionList(activeBomMeta());
  if (metaIds.length) return metaIds;
  const perSubIds = Object.keys((STATE.snapshot && STATE.snapshot.per_sub_results) || {});
  if (perSubIds.length) return perSubIds;
  return Array.from(new Set((Array.isArray(window._loadedSubscriptions) ? window._loadedSubscriptions : [])
    .map((sub) => String(sub && sub.id || "").trim())
    .filter(Boolean)));
}

function defaultQuotaSubscriptionId(ids = availableQuotaSubscriptionIds()) {
  const preferred = String(activeSubscriptionId() || "").trim();
  if (preferred && ids.includes(preferred)) return preferred;
  return ids[0] || null;
}

function syncActiveSubscription(preferred) {
  const ids = availableQuotaSubscriptionIds();
  const requested = preferred === undefined ? STATE.activeSubscription : preferred;
  let next = requested == null ? null : String(requested || "").trim();
  if (!ids.length) next = null;
  else if (!next || !ids.includes(next)) next = defaultQuotaSubscriptionId(ids);
  STATE.activeSubscription = next;
  return next;
}

function focusedSubscriptionId() {
  return syncActiveSubscription();
}

function focusedSubscriptionName() {
  const subId = focusedSubscriptionId();
  return subId ? _subNameById(subId) : "";
}

// The validation RG is stored per-subscription (an RG only lives inside one
// subscription). Resolve the one saved for a given subscription, falling back
// to the legacy global value for back-compat.
function _valRgForSub(subId) {
  const s = SUPPORT.settings || {};
  const map = s.validation_resource_groups || {};
  const sub = String(subId || "").trim();
  if (sub && map[sub]) return String(map[sub]).trim();
  return String(s.validation_resource_group || "").trim();
}

// Jump to Settings → Ticket owner and focus the validation-RG field. Used by the
// contextual "enable deployment validation" affordance on the deep check.
function openValidationRgSettings() {
  try { switchView("settings"); } catch (_e) {}
  try { switchSettingsTab("owner"); } catch (_e) {}
  setTimeout(() => {
    const el = document.getElementById("owner-valrg");
    if (el) { try { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.focus(); } catch (_e) {} }
  }, 60);
}

// ---------------------------------------------------------------- Theme

const THEME_KEY = "themePreference"; // "light" | "dark" | (absent → follow system)
const THEME_ICONS = { light: "☾", dark: "☀" }; // shown icon = action you can take

function getStoredTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch (e) { return null; }
}

function currentTheme() {
  // Source of truth is whatever the pre-paint inline script applied to <html>.
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const icon = btn.querySelector(".theme-toggle-icon");
    if (icon) icon.textContent = THEME_ICONS[next];
    btn.title = next === "dark" ? "Switch to light theme" : "Switch to dark theme";
    btn.setAttribute("aria-label", btn.title);
  }
  // Re-render anything that picks colors from CSS vars at construction time.
  refreshChartTheme();
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  applyTheme(next);
}

function themeColors() {
  // Read live values from CSS variables so charts pick the active theme.
  const s = getComputedStyle(document.documentElement);
  const get = (name, fallback) => {
    const v = s.getPropertyValue(name).trim();
    return v || fallback;
  };
  return {
    text:    get("--text-primary",   "#201F1E"),
    muted:   get("--text-secondary", "#605E5C"),
    grid:    get("--border",         "#EDEBE9"),
    surface: get("--surface",        "#FFFFFF"),
  };
}

function refreshChartTheme() {
  // Doughnut / pie charts use surface color for the slice separator.
  // Latency bar chart uses tick + grid colors from CSS vars.
  // We re-render rather than mutate live chart options so axes pick up fresh
  // tick colors cleanly (Chart.js doesn't deep-merge on update for nested
  // option paths reliably).
  const colors = themeColors();
  Object.values(STATE.overviewCharts || {}).forEach((c) => {
    if (!c) return;
    const ds = (c.data && c.data.datasets && c.data.datasets[0]) || null;
    if (ds) ds.borderColor = colors.surface;
    c.update("none");
  });
  if (STATE.latencyChart) {
    // Latency chart is rebuilt every time data changes; for an in-place theme
    // change, push tick/grid colors and update.
    const opts = STATE.latencyChart.options;
    if (opts && opts.scales && opts.scales.x) {
      if (!opts.scales.x.ticks) opts.scales.x.ticks = {};
      if (!opts.scales.x.grid)  opts.scales.x.grid  = {};
      if (!opts.scales.x.title) opts.scales.x.title = {};
      opts.scales.x.ticks.color = colors.muted;
      opts.scales.x.grid.color  = colors.grid;
      opts.scales.x.title.color = colors.text;
    }
    if (opts && opts.scales && opts.scales.y) {
      if (!opts.scales.y.ticks) opts.scales.y.ticks = {};
      if (!opts.scales.y.grid)  opts.scales.y.grid  = {};
      opts.scales.y.ticks.color = colors.text;
      opts.scales.y.grid.color  = colors.grid;
    }
    STATE.latencyChart.update("none");
  }
}

function initThemeController() {
  // Pre-paint inline script in <head> set the initial `data-theme` already.
  // Here we just sync the button icon and wire up listeners.
  applyTheme(currentTheme());

  const btn = document.getElementById("theme-toggle");
  if (btn) btn.addEventListener("click", toggleTheme);

  // Follow system changes ONLY while user has no explicit preference.
  try {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e) => {
      if (getStoredTheme()) return; // user picked one — leave it alone
      applyTheme(e.matches ? "dark" : "light");
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  } catch (e) {}
}

// ---------------------------------------------------------------- API helpers

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  // Delegated (multi-customer) mode: forward the customer's browser-minted ARM
  // token so the stateless server can read Azure as that customer. The token
  // lives only in the browser; it is never persisted server-side.
  try {
    if (
      window.__ARM_TOKEN &&
      typeof path === "string" &&
      path.indexOf("/api/") === 0 &&
      !("X-Bom-Access-Token" in headers)
    ) {
      headers["X-Bom-Access-Token"] = window.__ARM_TOKEN;
    }
  } catch (e) {}
  const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers }));
  // Stateless hosted mode: after any successful mutation, mirror the server's
  // per-user RAM store back to the customer's browser (debounced). Skips the
  // state-sync endpoints themselves to avoid a feedback loop.
  try {
    const m = (opts.method || "GET").toUpperCase();
    if (
      res.ok &&
      typeof path === "string" &&
      path.indexOf("/api/") === 0 &&
      path.indexOf("/api/state/") !== 0 &&
      (m === "POST" || m === "PUT" || m === "DELETE" || m === "PATCH")
    ) {
      scheduleStateSave();
    }
  } catch (e) {}
  return res;
}

// Acquire (and cache in-memory) the customer's ARM token via MSAL for delegated
// mode. Returns the token string or null. ``interactive`` allows a sign-in popup.
async function ensureDelegatedToken({ force = false } = {}) {
  if (!APP_CONFIG || !APP_CONFIG.delegated_mode || !window.DelegatedAuth) return null;
  try {
    const tok = await window.DelegatedAuth.getArmToken({ interactive: force });
    window.__ARM_TOKEN = tok || null;
    return tok;
  } catch (e) {
    window.__ARM_TOKEN = null;
    throw e;
  }
}

// Force an MFA step-up: re-acquire the customer's ARM token interactively
// (passing any claims challenge Azure returned) so the new token carries the
// MFA claim required to *write* Azure resources like support tickets. Returns
// the fresh token or null. Throws if the popup is blocked/cancelled.
async function stepUpDelegatedToken(claims) {
  if (!APP_CONFIG || !APP_CONFIG.delegated_mode || !window.DelegatedAuth) return null;
  const tok = await window.DelegatedAuth.getArmToken({ stepUp: true, claims: claims || null });
  window.__ARM_TOKEN = tok || null;
  return tok;
}

// ---------------------------------------------------------------- State sync
// Stateless hosted mode: the server keeps NOTHING on disk. The customer's
// browser is the durable store. On load we replay the saved document into the
// server's per-user RAM; after any change we mirror the server's state back to
// localStorage, namespaced by the signed-in user so concurrent customers on the
// same machine never collide.
function _stateKey() {
  let uid = "";
  try {
    const a = window.DelegatedAuth && window.DelegatedAuth.account;
    if (a) uid = a.homeAccountId || a.localAccountId || a.username || "";
  } catch (e) {}
  if (!uid) uid = (APP_CONFIG && (APP_CONFIG.user_id || APP_CONFIG.user_name)) || "anon";
  return "bomState:" + uid;
}

function _stateSyncEnabled() {
  return !!(APP_CONFIG && APP_CONFIG.delegated_mode);
}

async function hydrateStateFromLocal() {
  if (!_stateSyncEnabled()) return;
  let raw = null;
  try { raw = localStorage.getItem(_stateKey()); } catch (e) {}
  if (!raw) return;
  try {
    await apiFetch("/api/state/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: raw,
    });
  } catch (e) { console.warn("state hydrate failed", e); }
}

let _stateSaveTimer = null;
let _stateSaveInFlight = false;
function scheduleStateSave() {
  if (!_stateSyncEnabled()) return;
  if (_stateSaveTimer) clearTimeout(_stateSaveTimer);
  _stateSaveTimer = setTimeout(saveStateToLocal, 1000);
}

let _backupQuotaToastShown = false;
// Try to persist `txt` to localStorage; on QuotaExceeded strip the snapshot
// blobs and retry so BOM definitions + run history always survive a reload even
// when the last-analysis payload is too big. Returns true if anything was saved.
function _writeStateBackup(txt) {
  const key = _stateKey();
  try {
    localStorage.setItem(key, txt);
    return true;
  } catch (e) {
    // Fall back to a tables-only doc (drop the heavy blobs client-side, no
    // extra round-trip) so the BOMs still restore; snapshots stay .zip-only.
    try {
      const doc = JSON.parse(txt);
      doc.blobs = {};
      localStorage.setItem(key, JSON.stringify(doc));
      return true;
    } catch (e2) {
      console.warn("state save skipped (localStorage full)", e2);
      return false;
    }
  }
}

async function saveStateToLocal() {
  if (!_stateSyncEnabled() || _stateSaveInFlight) return;
  _stateSaveInFlight = true;
  try {
    // Back up the light BOM/run tables plus only the latest analysis per BOM
    // (blobs=latest) so the last result restores on reload while staying under
    // the ~5MB localStorage quota. Older snapshots keep their .zip-only path.
    const res = await apiFetch("/api/state/export?blobs=latest");
    if (!res.ok) return;
    const txt = await res.text();
    const saved = _writeStateBackup(txt);
    if (saved) {
      _backupQuotaToastShown = false;
    } else if (!_backupQuotaToastShown) {
      // Even the tables-only doc exceeded localStorage — warn once per session.
      _backupQuotaToastShown = true;
      try {
        showToast(
          "Browser backup is full — your BOMs may not restore after a reload. " +
          "Use Settings → Data & storage to download a .zip backup.",
          "warn"
        );
      } catch (_) {}
    }
    try { updateStorageGauge(); } catch (_) {}
  } catch (e) {
    console.warn("state export failed", e);
  } finally {
    _stateSaveInFlight = false;
  }
}

async function apiJson(path, opts = {}) {
  const res = await apiFetch(path, opts);
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch (e) {}
    const msg = (body && (body.message || body.error)) || res.statusText;
    const err = new Error(`${path}: ${msg}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

function snapshotOptionLabel(snapshot) {
  if (!snapshot) return "";
  const date = (snapshot.ended_at || snapshot.started_at || "")
    .replace("T", " ")
    .replace(/\.\d+Z?$/, " UTC");
  const armBadge = snapshot.arm_overlay_applied ? " · ARM" : "";
  return `${date}${armBadge}`;
}

function currentSnapshotRunId() {
  const picker = document.getElementById("snapshot-picker");
  return picker ? (picker.value || "") : "";
}

// ---------------------------------------------------------------- Loading

async function fetchJson(path) {
  // When running as a single-file bundle, data is embedded on window.__EMBEDDED_DATA__
  if (typeof window !== "undefined" && window.__EMBEDDED_DATA__ && window.__EMBEDDED_DATA__[path]) {
    return window.__EMBEDDED_DATA__[path];
  }
  return apiJson(path);
}

async function loadSnapshotsList() {
  try {
    const params = STATE.activeBomId ? `?bom=${encodeURIComponent(STATE.activeBomId)}` : "";
    const idx = await apiJson(`/api/snapshots${params}`);
    STATE.snapshots = idx.snapshots || [];
    const picker = document.getElementById("snapshot-picker");
    const previous = picker.value || "";
    picker.innerHTML = "";
    if (!STATE.snapshots.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(no analysis results yet — click Refresh analysis)";
      picker.appendChild(opt);
      renderSnapshotCompareControls();
      return;
    }
    for (const s of STATE.snapshots) {
      const opt = document.createElement("option");
      opt.value = s.run_id;
      opt.textContent = snapshotOptionLabel(s);
      picker.appendChild(opt);
    }
    const stillExists = STATE.snapshots.some(s => s.run_id === previous);
    picker.value = stillExists ? previous : STATE.snapshots[0].run_id;
    picker.onchange = () => loadSnapshot(picker.value);
    renderSnapshotCompareControls();
  } catch (e) {
    console.warn("snapshots index failed:", e);
  }
}

async function loadSnapshot(runId) {
  try {
    const path = runId
      ? `/api/snapshots/${encodeURIComponent(runId)}`
      : (STATE.activeBomId ? `/api/snapshots/latest?bom=${encodeURIComponent(STATE.activeBomId)}` : `/api/snapshots/latest`);
    STATE.snapshot = await apiJson(path);
  } catch (e) {
    if (e.status === 404) {
      console.info("no snapshot yet for", STATE.activeBomId);
      STATE.snapshot = { regions: [], latency_matrix: {}, stats: {}, bom: { skus: [] } };
    } else {
      throw e;
    }
  }
  renderBrandMark();
  initContinentFilter();
  initSourceRegionDropdown();
  renderSubscriptionFilter();
  renderSnapshotCompareControls();
  initCompareDropdowns();
  updateSkuSelectionLabels();
  renderPendingQuotaPanel();
  applyFilters();
  refreshMap();
  if (STATE.view === "quota") renderQuotaTab();
  fetchPricingEstimate();
  ensureZrsRefData();
  _hydrateVerifyAll().catch(() => {});
}

// Preload the reference data the ZRS (zone-redundancy) readiness check needs:
//   * a region → has-AZ map (from the region catalog), and
//   * the service catalog (so we know which selected tiers are ZRS-capable).
// Both loaders are cached, so this is cheap on repeat snapshot loads. Failures
// are non-fatal — the readiness section simply won't render.
async function ensureZrsRefData() {
  try {
    const [regions] = await Promise.all([
      ensureBomRegionsCatalog().catch(() => null),
      ensureBomCatalog().catch(() => null),
    ]);
    STATE.regionAzMap = {};
    for (const rg of (regions || BOM_EDIT.regionsCatalog || [])) {
      if (rg && rg.name) STATE.regionAzMap[String(rg.name).toLowerCase()] = !!rg.has_az;
    }
  } catch (e) {
    /* non-fatal */
  }
}

// The header is always app-branded now. Per-BOM / per-snapshot context is
// shown in the BOM panel at the top of the main area (renderBomPanel).
function renderBrandMark() {
  const el = document.getElementById("brand-mark");
  if (el) { el.textContent = "Azure BOM Region Support Dashboard"; el.title = "Azure BOM Region Support Dashboard"; }
  renderBomPanel();
}

// Render the BOM details + controls panel at the top of the main area from the
// active BOM (subscription_metadata) and the currently loaded snapshot.
function renderBomPanel() {
  const panel = document.getElementById("bom-panel");
  if (!panel) return;
  const emptyEl = document.getElementById("bom-panel-empty");
  const bodyEl = document.getElementById("bom-panel-body");
  const bomId = STATE.activeBomId || "";
  const meta = bomId ? getBomMeta(bomId) : null;

  if (!bomId || !meta) {
    syncActiveSubscription(null);
    renderSubscriptionSwitcher();
    renderSubscriptionFilter();
    if (bodyEl) bodyEl.classList.add("hidden");
    if (emptyEl) {
      emptyEl.classList.remove("hidden");
      renderOnboardingStepper(emptyEl);
    }
    return;
  }
  if (emptyEl) emptyEl.classList.add("hidden");
  if (bodyEl) bodyEl.classList.remove("hidden");
  syncActiveSubscription();
  renderSubscriptionSwitcher();
  renderSubscriptionFilter();

  document.getElementById("bom-panel-tag").textContent = bomDisplayName(meta);
  // Show customer as secondary only when it isn't already the primary name.
  const secondary = (meta.tag && meta.customer_name) ? meta.customer_name : "";
  document.getElementById("bom-panel-customer").textContent = secondary ? "· " + secondary : "";
  document.getElementById("bom-panel-meta").textContent =
    `Subscriptions: ${summarizeSubscriptions(meta)}`;

  const nSvc = (meta.services || []).length;
  const nReg = (meta.regions || []).length;
  const nSku = (meta.required_skus || []).length;
  document.getElementById("bom-panel-counts").innerHTML =
    `<span><strong>${nSvc}</strong> services</span>` +
    `<span><strong>${nReg || "all"}</strong> regions</span>` +
    `<span><strong>${nSku}</strong> SKU families</span>`;

  const warnEl = document.getElementById("bom-panel-warning");
  const snapMeta = (STATE.snapshot && STATE.snapshot.meta) || {};
  if (warnEl) {
    if (snapMeta.mode === "global_unscoped") {
      const note = snapMeta.mode_note || "Per-subscription restrictions not evaluated.";
      warnEl.textContent = `⚠ Unscoped snapshot: ${note}`;
      warnEl.classList.remove("hidden");
    } else {
      warnEl.textContent = "";
      warnEl.classList.add("hidden");
    }
  }

  // Stale-analysis badge: the snapshot shown in the tabs is an immutable run.
  // If the BOM was edited after the viewed snapshot (or none exists yet), the
  // displayed analysis no longer matches the BOM — flag it so the user knows
  // to Refresh analysis.
  const staleEl = document.getElementById("bom-panel-stale");
  if (staleEl) {
    const editTime = meta.bom_updated_at ? Date.parse(meta.bom_updated_at) : null;
    const snapTime = viewedSnapshotTime();
    const hasSnap = !!(STATE.snapshot && (STATE.snapshot.regions || []).length);
    let stale = false;
    if (!hasSnap) {
      stale = true;
      staleEl.textContent = "⚠ No analysis yet";
      staleEl.title = "This BOM hasn't been analyzed yet. Click ▶ Refresh analysis to populate the dashboard.";
    } else if (editTime && (snapTime === null || editTime > snapTime)) {
      stale = true;
      staleEl.textContent = "⚠ Out of date";
      staleEl.title = "The BOM was edited after the analysis shown below. Click ▶ Refresh analysis to update.";
    }
    staleEl.classList.toggle("hidden", !stale);
  }
}

// ---------------------------------------------------------------- Onboarding
// Active empty-state stepper shown when no BOM is selected/exists. Replaces the
// old passive "select a BOM" text with a guided path: sign in → create a BOM,
// plus a one-click "explore with sample data" escape hatch. Re-rendered on
// sign-in state changes via updateSigninChip().
function _isSignedIn() {
  return !!(TOKEN.info && (TOKEN.info.expires_in_seconds || 0) > 0);
}

function renderOnboardingStepper(host) {
  if (!host) return;
  const hasBoms = Object.keys((BOM_META && BOM_META.index) || {}).length > 0;
  const signedIn = _isSignedIn();
  const who = signedIn ? (TOKEN.info.az_user || "signed in") : "";
  const demo = !!(APP_CONFIG && APP_CONFIG.demo_mode);

  // If BOMs already exist the user just hasn't picked one — keep it lightweight.
  if (hasBoms) {
    host.innerHTML =
      '<div class="onboard-pick">Select a Bill of Materials from the left, ' +
      'or <button type="button" class="link-btn" data-onboard="new">create a new one</button>.</div>';
    const btn = host.querySelector('[data-onboard="new"]');
    if (btn) btn.addEventListener("click", () => openBomModal(null, { create: true }));
    return;
  }

  const step1Done = signedIn;
  const step2Done = _onboardSettingsDone();
  const s1State = step1Done ? "done" : "active";
  const s2State = !step1Done ? "todo" : (step2Done ? "done" : "active");
  const s3State = !step1Done ? "todo" : (step2Done ? "active" : "todo");

  host.innerHTML = `
    <div class="onboard">
      <div class="onboard-head">
        <h2>Welcome — let's plan a deployment</h2>
        <p class="muted">Three quick steps to see where a customer's Bill of Materials can deploy,
        where quota or zonal access is a blocker, and which regions are best.</p>
      </div>
      <ol class="onboard-steps">
        <li class="onboard-step is-${s1State}" data-step="1">
          <span class="onboard-num">${step1Done ? "✓" : "1"}</span>
          <div class="onboard-body">
            <h3>Sign in to Azure</h3>
            <p class="muted">${step1Done
              ? `Signed in as <strong>${escapeHtml(who)}</strong>.`
              : "A one-time browser sign-in mints a read-only ARM token to query SKU, region and quota data."}</p>
            <div class="onboard-actions">
              ${step1Done
                ? '<button type="button" class="btn btn--sm" data-onboard="signin">Switch account</button>'
                : '<button type="button" class="btn btn--accent" data-onboard="signin">Sign in</button>'}
            </div>
          </div>
        </li>
        <li class="onboard-step is-${s2State}" data-step="2">
          <span class="onboard-num">${step2Done ? "✓" : "2"}</span>
          <div class="onboard-body">
            <h3>Configure &amp; refresh your settings</h3>
            <p class="muted">Open <strong>Settings</strong> to set your support ticket owner and
            refresh the model datasets (regions, latency, SKUs) so your analysis uses the latest Azure data.</p>
            <div class="onboard-actions">
              <button type="button" class="btn btn--accent" data-onboard="settings" ${step1Done ? "" : "disabled"}>⚙ Open Settings</button>
              ${step1Done ? "" : '<span class="muted small">Sign in first</span>'}
            </div>
          </div>
        </li>
        <li class="onboard-step is-${s3State}" data-step="3">
          <span class="onboard-num">3</span>
          <div class="onboard-body">
            <h3>Create your first BOM</h3>
            <p class="muted">Name it, pick the customer subscription(s), then choose the services,
            regions and SKUs to analyze.</p>
            <div class="onboard-actions">
              <button type="button" class="btn btn--accent" data-onboard="new" ${step1Done ? "" : "disabled"}>+ New BOM</button>
              ${step1Done ? "" : '<span class="muted small">Sign in first</span>'}
            </div>
          </div>
        </li>
      </ol>
      ${demo ? "" : `
      <div class="onboard-or"><span>or</span></div>
      <div class="onboard-demo">
        <button type="button" class="btn" data-onboard="demo">▶ Explore with sample data</button>
        <span class="muted small">Loads a bundled example BOM &amp; analysis — no Azure sign-in needed.</span>
      </div>`}
    </div>`;

  const signinBtn = host.querySelector('[data-onboard="signin"]');
  if (signinBtn) signinBtn.addEventListener("click", () => {
    // Open the sign-in modal so the browser flow + any errors are visible,
    // then kick off the interactive sign-in.
    openSigninModal();
    refreshAuthToken({ force: true });
  });
  const settingsBtn = host.querySelector('[data-onboard="settings"]');
  if (settingsBtn) settingsBtn.addEventListener("click", () => {
    _setOnboardSettingsDone();
    dismissSettingsCoach();
    switchView("settings");
  });
  const newBtn = host.querySelector('[data-onboard="new"]');
  if (newBtn) newBtn.addEventListener("click", () => openBomModal(null, { create: true }));
  const demoBtn = host.querySelector('[data-onboard="demo"]');
  if (demoBtn) demoBtn.addEventListener("click", () => loadSampleData(demoBtn));
}

// Remembers that the user has visited Settings from the onboarding stepper, so
// step 2 shows as complete on subsequent renders/reloads.
const ONBOARD_SETTINGS_KEY = "onboard_settings_done";
function _onboardSettingsDone() {
  try { return localStorage.getItem(ONBOARD_SETTINGS_KEY) === "1"; }
  catch (_e) { return false; }
}
function _setOnboardSettingsDone() {
  try { localStorage.setItem(ONBOARD_SETTINGS_KEY, "1"); } catch (_e) {}
}

// Seed the bundled sample BOM on demand, then reload the list and open it so
// the user sees a fully populated dashboard immediately.
async function loadSampleData(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Loading sample…"; }
  try {
    const r = await apiJson("/api/demo/seed", { method: "POST" });
    await loadSubscriptions();
    await loadSnapshotsList();
    const target = r.bom_id && BOM_META.index[r.bom_id] ? r.bom_id : (Object.keys(BOM_META.index)[0] || "");
    if (target) await selectBom(target);
    showToast(r.seeded ? "Sample data loaded." : "Sample data already present.", "success");
  } catch (e) {
    showToast(e.message || "Could not load sample data.", "error");
    if (btn) { btn.disabled = false; btn.textContent = "▶ Explore with sample data"; }
  }
}

function renderSubscriptionSwitcher() {
  const hosts = [
    document.getElementById("quota-subscription-control"),
  ].filter(Boolean);
  if (!hosts.length) return;
  const ids = availableQuotaSubscriptionIds();
  if (!ids.length) {
    hosts.forEach((host) => { host.innerHTML = ""; });
    return;
  }

  const activeId = syncActiveSubscription();
  const options = ids.map((subId, index) => {
    const name = _subNameById(subId) || `Subscription ${index + 1}`;
    return `<option value="${escapeHtml(subId)}">${escapeHtml(name)}</option>`;
  }).join("");
  const single = ids.length <= 1;
  hosts.forEach((host) => {
    const isQuotaTab = host.id === "quota-subscription-control";
    const labelText = isQuotaTab ? "Subscription:" : "Viewing quota for";
    if (single) {
      const name = _subNameById(activeId) || _subNameById(ids[0]) || `Subscription 1`;
      host.innerHTML = `<label class="quota-control quota-control--subscription${isQuotaTab ? " quota-control--inline" : ""}" data-subscription-switcher-wrap="1">
        <span>${labelText}</span>
        <span class="quota-control__static" title="This BOM is scoped to a single subscription — pick a different subscription only when a BOM covers more than one.">${escapeHtml(name)}</span>
      </label>`;
      return;
    }
    host.innerHTML = `<label class="quota-control quota-control--subscription${isQuotaTab ? " quota-control--inline" : ""}" data-subscription-switcher-wrap="1">
      <span>${labelText}</span>
      <select data-subscription-switcher="1" title="Switch which subscription's quota you're viewing for this BOM" aria-label="Select the subscription context for quota views">${options}</select>
    </label>`;
    const sel = host.querySelector("[data-subscription-switcher]");
    if (sel) sel.value = activeId || "";
  });
}

function renderSubscriptionFilter() {
  const select = document.getElementById("filter-subscription");
  if (!select) return;
  const ids = availableQuotaSubscriptionIds();
  const activeId = syncActiveSubscription();
  const options = ids.map((subId, index) => {
    const name = _subNameById(subId) || `Subscription ${index + 1}`;
    return `<option value="${escapeHtml(subId)}">${escapeHtml(name)}</option>`;
  });
  select.innerHTML = options.join("");
  select.disabled = ids.length === 0;
  select.value = activeId || "";
}

// Epoch (ms) of the snapshot currently shown in the tabs, or null. Uses the
// run list (ended_at) keyed by the picker's run_id, falling back to the UTC
// timestamp encoded in the run_id (YYYY-MM-DDTHH-MM-SSZ-...).
function viewedSnapshotTime() {
  const picker = document.getElementById("snapshot-picker");
  const runId = picker ? picker.value : "";
  if (!runId) return null;
  const snap = (STATE.snapshots || []).find(s => s.run_id === runId);
  const iso = snap && (snap.ended_at || snap.started_at);
  if (iso) { const t = Date.parse(iso); if (!isNaN(t)) return t; }
  const m = runId.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})Z/);
  if (m) { const t = Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`); if (!isNaN(t)) return t; }
  return null;
}

function renderSnapshotCompareControls() {
  const toggle = document.getElementById("snapshot-compare-toggle");
  const row = document.getElementById("snapshot-compare-row");
  const picker = document.getElementById("snapshot-compare-picker");
  const current = currentSnapshotRunId();
  if (!toggle || !row || !picker) return;

  const choices = (STATE.snapshots || []).filter(s => s.run_id !== current);
  toggle.disabled = choices.length === 0 || !current;
  if (toggle.disabled) {
    row.classList.add("hidden");
    toggle.textContent = "Compare";
  }

  picker.innerHTML = "";
  if (!choices.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(pick another snapshot once more runs exist)";
    picker.appendChild(opt);
    return;
  }

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "- choose a comparison snapshot -";
  picker.appendChild(placeholder);
  for (const snapshot of choices) {
    const opt = document.createElement("option");
    opt.value = snapshot.run_id;
    opt.textContent = snapshotOptionLabel(snapshot);
    picker.appendChild(opt);
  }
  if (!choices.some(s => s.run_id === picker.value)) picker.value = "";
}

function toggleSnapshotComparePicker() {
  const row = document.getElementById("snapshot-compare-row");
  const toggle = document.getElementById("snapshot-compare-toggle");
  if (!row || !toggle || toggle.disabled) return;
  const opening = row.classList.contains("hidden");
  row.classList.toggle("hidden", !opening);
  toggle.textContent = opening ? "Cancel" : "Compare";
  if (opening) {
    renderSnapshotCompareControls();
    const picker = document.getElementById("snapshot-compare-picker");
    if (picker) picker.focus();
  }
}

async function openSnapshotDiff(compareRunId) {
  const current = currentSnapshotRunId();
  if (!current || !compareRunId || current === compareRunId) return;
  const title = document.getElementById("snapshot-diff-title");
  const summary = document.getElementById("snapshot-diff-summary");
  const tbody = document.querySelector("#snapshot-diff-table tbody");
  const empty = document.getElementById("snapshot-diff-empty");
  if (title) title.textContent = "Loading…";
  if (summary) summary.innerHTML = "";
  if (tbody) tbody.innerHTML = "";
  if (empty) empty.classList.add("hidden");
  document.getElementById("snapshot-diff-overlay").classList.remove("hidden");
  document.getElementById("snapshot-diff-modal").classList.remove("hidden");

  try {
    const data = await apiJson(`/api/snapshots/diff?a=${encodeURIComponent(compareRunId)}&b=${encodeURIComponent(current)}`);
    STATE.snapshotDiff = data;
    renderSnapshotDiffModal(data);
  } catch (e) {
    if (title) title.textContent = "Snapshot diff";
    if (empty) {
      empty.textContent = `Unable to compare snapshots: ${e.message}`;
      empty.classList.remove("hidden");
    }
  }
}

function closeSnapshotDiffModal() {
  document.getElementById("snapshot-diff-overlay").classList.add("hidden");
  document.getElementById("snapshot-diff-modal").classList.add("hidden");
}

function renderSnapshotDiffModal(data) {
  const title = document.getElementById("snapshot-diff-title");
  const summary = document.getElementById("snapshot-diff-summary");
  const tbody = document.querySelector("#snapshot-diff-table tbody");
  const empty = document.getElementById("snapshot-diff-empty");
  if (!title || !summary || !tbody || !empty) return;

  title.textContent = `${data.a_timestamp || data.a_id} → ${data.b_timestamp || data.b_id}`;
  const cards = [
    ["Improved", data.summary?.regions_improved ?? 0],
    ["Degraded", data.summary?.regions_degraded ?? 0],
    ["Unchanged", data.summary?.regions_unchanged ?? 0],
    ["New blockers", data.summary?.new_blockers ?? 0],
    ["Resolved blockers", data.summary?.resolved_blockers ?? 0],
  ];
  summary.innerHTML = cards.map(([label, value]) => `
    <div class="diff-summary-card">
      <div class="diff-summary-label">${escapeHtml(label)}</div>
      <div class="diff-summary-value">${escapeHtml(String(value))}</div>
    </div>
  `).join("");

  const rows = Array.isArray(data.changes) ? data.changes : [];
  if (!rows.length) {
    tbody.innerHTML = "";
    empty.textContent = "No region-level changes were detected between these snapshots.";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  tbody.innerHTML = rows.map((row) => {
    const arrow = row.direction === "improved" ? "▲" : (row.direction === "degraded" ? "▼" : "→");
    return `<tr>
      <td><strong>${escapeHtml(row.display || row.region || "—")}</strong><div class="muted">${escapeHtml(row.region || "")}</div></td>
      <td><span class="diff-direction ${escapeHtml(row.direction || "unchanged")}">${arrow} ${escapeHtml(row.direction || "unchanged")}</span></td>
      <td class="num">${escapeHtml(String(row.score_before ?? "—"))} → ${escapeHtml(String(row.score_after ?? "—"))}</td>
      <td>${escapeHtml((row.verdict_before || "unknown").replaceAll("_", " "))} → ${escapeHtml((row.verdict_after || "unknown").replaceAll("_", " "))}</td>
      <td>${row.details && row.details.length
        ? `<ul class="diff-details">${row.details.map(detail => `<li>${escapeHtml(detail)}</li>`).join("")}</ul>`
        : '<span class="muted">No detail</span>'}</td>
    </tr>`;
  }).join("");
}

// ---------------------------------------------------------------- Filters

function readFilters() {
  const search = (document.getElementById("filter-search").value || "").toLowerCase();
  const verdictChecked = Array.from(document.querySelectorAll('[data-filter="verdict"]:checked')).map(e => e.value);
  const continentChecked = Array.from(document.querySelectorAll('[data-filter="continent"]:checked')).map(e => e.value);
  const quotaChecked = Array.from(document.querySelectorAll('[data-filter="quota"]:checked')).map(e => e.value);
  const azChecked = Array.from(document.querySelectorAll('[data-filter="az"]:checked')).map(e => e.value);
  return {
    search,
    verdict: new Set(verdictChecked),
    continent: new Set(continentChecked),
    quota: new Set(quotaChecked),
    az: new Set(azChecked),
    missingOnly: document.getElementById("filter-missing-services").checked,
    v5Fallback: document.getElementById("filter-v5-fallback").checked,
    restrictedOnly: document.getElementById("filter-restricted-only").checked,
  };
}

// Collapse the detailed quota verdict into the three buckets the filter offers.
function _quotaFilterBucket(r) {
  const v = getRegionQuotaVerdict(STATE.snapshot, r.short || "").verdict;
  if (v === "pass" || v === "none") return "sufficient";
  if (v === "fail" || v === "partial" || v === "no_group") return "insufficient";
  return "unknown"; // unknown, not_accessible, no_sub
}

function applyFilters() {
  if (!STATE.snapshot) return;
  const f = readFilters();
  const all = STATE.snapshot.regions || [];

  STATE.filtered = all.filter(r => {
    const deployment = getDeploymentVerdictInfo(r);
    if (f.search && !r.name.toLowerCase().includes(f.search)) return false;
    if (f.verdict.size && !f.verdict.has(deployment.verdict)) return false;
    if (f.continent.size && !f.continent.has(r.geo)) return false;
    if (f.quota.size && !f.quota.has(_quotaFilterBucket(r))) return false;
    if (f.az.size) {
      const azBucket = (_regionSupportsAz(r) === false) ? "regional" : "has_az";
      if (!f.az.has(azBucket)) return false;
    }
    if (f.missingOnly && !((r.missing_services || []).length > 0)) return false;
    // fell_back is the generic engine field. Fall back to legacy
    // sku_fallbacks shape so old snapshots keep filtering correctly.
    const fellBack = (r.fell_back != null) ? r.fell_back : ((r.sku_fallbacks || []).length > 0);
    if (f.v5Fallback) {
      if (r.deployment_health !== "Yes" || !fellBack) return false;
    }
    if (f.restrictedOnly && !r.has_zone_restriction) return false;
    return true;
  });

  STATE.filtered.sort((a, b) => {
    const k = STATE.sortKey;
    let av = a[k], bv = b[k];
    if (av == null) av = "";
    if (bv == null) bv = "";
    if (typeof av === "string") return STATE.sortDir * av.localeCompare(bv);
    return STATE.sortDir * (av - bv);
  });

  renderTable();
  refreshMap();
  renderOverviewCharts();
  renderBestRegionPanel();
  renderOverviewCockpit();
  renderOverviewReco();
  updateStats();
}

function updateStats() {
  if (!STATE.snapshot) return;
  const all = STATE.snapshot.regions || [];
  const ready = all.filter(r => getDeploymentVerdictInfo(r).verdict === "ready").length;
  document.getElementById("stat-shown").textContent = STATE.filtered.length;
  document.getElementById("stat-total").textContent = all.length;
  document.getElementById("stat-ready").textContent = ready;
  document.getElementById("stat-other-verdicts").textContent = all.length - ready;
  updateCostStat();
}

// ---------------------------------------------------------------- Cost estimate (pricing)

const PRICING = { settings: null, estimate: null, loading: false, reqKey: "", altValidation: {}, zonalCap: {} };
const PRICING_DISCLAIMER = "Estimate only — list price anchored to a representative VM size, not a quote. Excludes storage, egress, licensing & negotiated terms.";

function _fmtMoney(n, currency, opts) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  const cur = (currency || "USD").toUpperCase();
  const frac = (opts && opts.cents) ? 2 : 0;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: frac }).format(Number(n));
  } catch (e) {
    return `${cur} ${Number(n).toLocaleString(undefined, { maximumFractionDigits: frac })}`;
  }
}

// Non-compute service names present in the current BOM snapshot.
function _bomServiceNames(snap) {
  const svcs = _currentBomServices(snap);
  return svcs.map(s => String((s && (s.name || s)) || "")).filter(Boolean);
}

// The BOM's selected services (with any per-service tier). Prefer the loaded
// snapshot's meta (self-contained, matches what was analyzed); fall back to the
// active BOM record so tiers/ZRS surface even before the BOM is re-run.
function _currentBomServices(snap) {
  const s = snap || STATE.snapshot;
  const fromSnap = ((s && s.meta) || {}).services;
  if (Array.isArray(fromSnap) && fromSnap.length) return fromSnap;
  const meta = STATE.activeBomId ? getBomMeta(STATE.activeBomId) : null;
  return (meta && Array.isArray(meta.services)) ? meta.services : [];
}

// Map of service name → selected tier id from the snapshot's BOM (or {}).
function _bomServiceTiers(snap) {
  const svcs = _currentBomServices(snap);
  const out = {};
  for (const s of svcs) {
    if (s && s.name && s.tier) out[String(s.name)] = String(s.tier);
  }
  return out;
}

// Friendly label for a service tier id: prefer the catalog label, else prettify.
function _tierLabel(serviceName, tierId) {
  if (!tierId) return "";
  const tiers = _catalogTiersFor(serviceName);
  const hit = tiers.find(t => t.id === tierId);
  if (hit) return hit.label.replace(/\s*\(.*\)$/, "");
  return tierId.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

// Build the estimate request from the current snapshot (all regions + BOM cores + services).
function _pricingRequestBody() {
  const snap = STATE.snapshot;
  if (!snap) return null;
  const regions = (snap.regions || []).map(r => r.short).filter(Boolean);
  if (!regions.length) return null;
  const families = _getCoresRequirements(snap).map(r => ({
    family: r.primary_family,
    label: r.primary_label,
    required_cores: r.required_cores,
    // Availability fallback the backend prices when the primary generation
    // isn't sold in a region (e.g. Dsv7 → Dsv6 in Austria East).
    alt_family: r.alt_family || null,
    alt_label: r.alt_label || null,
  }));
  return { regions, families, services: _bomServiceNames(snap) };
}

// Fetch (once per unique request+settings signature) the BOM cost estimate.
async function fetchPricingEstimate(force) {
  const body = _pricingRequestBody();
  if (!body || !body.families.length) { PRICING.estimate = null; PRICING.reqKey = ""; _applyPricingToUI(); return; }
  const key = JSON.stringify(body) + "|" + JSON.stringify(PRICING.settings || {});
  if (!force && key === PRICING.reqKey && PRICING.estimate) return;
  PRICING.reqKey = key;
  PRICING.loading = true;
  _applyPricingToUI();
  try {
    PRICING.estimate = await apiJson("/api/pricing/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.warn("pricing estimate failed:", e && e.message);
    PRICING.estimate = null;
  } finally {
    PRICING.loading = false;
    _applyPricingToUI();
  }
}

function _pricingRegionInfo(short) {
  const est = PRICING.estimate;
  if (!est || !est.regions) return null;
  return est.regions[String(short || "").toLowerCase()] || null;
}

// Stash per-region totals (for sort) and refresh table cells, stat, drilldown.
function _applyPricingToUI() {
  const est = PRICING.estimate;
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  for (const r of regions) {
    const info = est && est.regions ? est.regions[String(r.short || "").toLowerCase()] : null;
    r.est_monthly = (info && info.priced_any) ? Number(info.monthly_net) : 0;
  }
  if (document.querySelector("#regions-table tbody")) renderTable();
  updateCostStat();
  const dd = document.getElementById("drilldown");
  if (dd && !dd.classList.contains("hidden") && STATE.activeDrilldownRegion) {
    _refreshDrilldownCost(STATE.activeDrilldownRegion);
  }
}

// Overview KPI: cheapest Ready region net monthly (fallback: cheapest any).
function updateCostStat() {
  const wrap = document.getElementById("stat-est-wrap");
  const el = document.getElementById("stat-est-cost");
  if (!wrap || !el) return;
  const hasReq = snapshotHasCoresRequirements(STATE.snapshot);
  wrap.classList.toggle("hidden", !hasReq);
  if (!hasReq) return;
  if (PRICING.loading && !PRICING.estimate) { el.textContent = "…"; return; }
  const est = PRICING.estimate;
  if (!est) { el.textContent = "—"; return; }
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  const pick = (readyOnly) => {
    let best = null;
    for (const r of regions) {
      if (readyOnly && getDeploymentVerdictInfo(r).verdict !== "ready") continue;
      const info = est.regions[String(r.short || "").toLowerCase()];
      if (!info || !info.priced_any) continue;
      if (!best || info.monthly_net < best.net) best = { net: info.monthly_net, region: r.name };
    }
    return best;
  };
  const best = pick(true) || pick(false);
  if (!best) { el.textContent = "—"; return; }
  el.textContent = _fmtMoney(best.net, est.currency);
  wrap.title = `Estimated monthly BOM cost (compute + non-compute) for ${best.region}, the cheapest Ready region. ${PRICING_DISCLAIMER}`;
}

// Friendly label of the BOM fallback family a region was priced on, if any.
function _pricedAltLabel(info) {
  const fams = (info && info.compute && info.compute.families) || [];
  const alt = fams.find(f => f && f.priced_via_alt);
  return alt ? (alt.priced_label || "") : "";
}

// The cost cell shown in the regions table for a region.
function _costCellHtml(r) {
  if (!snapshotHasCoresRequirements(STATE.snapshot)) return `<td class="cost-col hidden num"></td>`;
  const est = PRICING.estimate;
  const info = _pricingRegionInfo(r.short);
  let text = "—", title = PRICING_DISCLAIMER, badge = "";
  if (PRICING.loading && !info) text = "…";
  else if (info && info.priced_any) {
    text = _fmtMoney(info.monthly_net, est.currency);
    if (!info.complete) { text += "*"; title = "Some families could not be priced. " + PRICING_DISCLAIMER; }
    if (info.priced_via_alt) {
      const altLbl = _pricedAltLabel(info);
      badge += ` <span class="cost-alt-badge" title="Your primary SKU generation isn't sold in this region — cost is estimated on your BOM fallback${altLbl ? " (" + altLbl + ")" : ""}. Open the region for details.">↩ fallback</span>`;
    }
    if (info.has_cheaper_alt && info.compute && info.compute.alt_savings_pct > 0) {
      badge += ` <span class="cost-save-badge" title="A cheaper size-equivalent SKU is available in this region — save ~${_fmtMoney(info.compute.alt_savings_monthly_net, est.currency)}/mo. Open the region for details.">▼${info.compute.alt_savings_pct}%</span>`;
    }
  } else if (est) text = "n/a";
  return `<td class="cost-col num" title="${escapeHtml(title)}">${escapeHtml(text)}${badge}</td>`;
}

// Cheaper size-equivalent SKU suggestions block for the drilldown cost section.
function _altSavingsBlock(info, cur, regionShort) {
  const c = info.compute || {};
  const swaps = c.swaps || [];
  if (!swaps.length) return "";
  const rows = swaps.map(s => {
    const vend = s.vendor ? `<span class="alt-vendor alt-vendor--${s.vendor.toLowerCase()}" title="${escapeHtml(s.note || "")}">${escapeHtml(s.vendor)}</span>` : "";
    const ret = (s.retirement && s.retirement.note)
      ? ` <span class="alt-prevgen" title="${escapeHtml((s.retirement.replacement ? "Newer generation available: " + s.retirement.replacement + ". " : "") + "Still fully supported.")}">🕈 ${escapeHtml(s.retirement.note)}</span>`
      : "";
    return `<div class="alt-swap">
      <div class="alt-swap-top">
        <div class="alt-swap-main">
          <span class="alt-from">${escapeHtml(s.from_label)}</span>
          <span class="alt-arrow">→</span>
          <span class="alt-to">${escapeHtml(s.to_label)}</span> ${vend}${ret}
        </div>
        <div class="alt-swap-save">−${_fmtMoney(s.savings_monthly_net, cur)}/mo <span class="muted">(${s.savings_pct}%)</span></div>
      </div>
      <div class="alt-swap-valid" data-alt-fam="${escapeHtml(s.to_family)}"><span class="alt-valid checking">checking availability &amp; quota…</span></div>
    </div>`;
  }).join("");
  const total = `Save up to ${_fmtMoney(c.alt_savings_monthly_net, cur)}/mo (${c.alt_savings_pct}%) → optimized ${_fmtMoney(c.optimized_monthly_net, cur)}/mo`;
  return `
    <div class="dd-readiness-subtitle alt-title">💡 Cheaper size-equivalent SKUs</div>
    <div class="alt-headline">${total}</div>
    <div class="alt-swaps" data-alt-region="${escapeHtml(regionShort || "")}">${rows}</div>
    <div class="note muted alt-note">Same vCPU &amp; memory, different CPU vendor/generation. <strong>Availability, subscription restrictions and regional vCPU quota are validated live against Azure</strong> for this region &amp; subscription (badges above). ARM SKUs additionally require an ARM64-compatible OS image.</div>`;
}

// Live validation badge for one suggested alternative family.
function _altValidBadge(v, regionShort) {
  if (!v) return `<span class="alt-valid muted">not validated — verify manually</span>`;
  const title = escapeHtml(v.message || "");
  const region = escapeHtml(regionShort || "");
  const armFam = escapeHtml(v.arm_family || "");
  const label = escapeHtml(v.family || "");
  const cores = Number(v.required_cores || 0);
  const limit = (v.quota && v.quota.limit != null) ? Number(v.quota.limit) : "";
  const ticketLink = (kind, text) =>
    `<a href="#" class="alt-ticket-link" data-alt-ticket="${kind}" data-alt-region="${region}" data-alt-family="${armFam}" data-alt-label="${label}" data-alt-cores="${cores}" data-alt-limit="${limit}">${text}</a>`;
  // Surface zone status: partial restrictions (some AZs blocked) or an
  // explicit all-clear when every zone is usable for this subscription.
  let zoneNote = "";
  if (Array.isArray(v.zones) && v.zones.length) {
    const blocked = v.zones.map((ok, i) => ok ? null : i + 1).filter(Boolean);
    const avail = v.zones.map((ok, i) => ok ? i + 1 : null).filter(Boolean);
    if (v.zone_limited && blocked.length) {
      zoneNote = ` <span class="alt-zone-note" title="Restricted in AZ ${blocked.join(", ")} for this subscription; usable in AZ ${avail.join(", ") || "none"}.">⚠️ AZ-limited (not in ${blocked.map(z => "AZ " + z).join(", ")})</span> ${ticketLink("technical", "Request AZ access →")}`;
    } else if (v.offered && !v.region_restricted && !blocked.length) {
      zoneNote = ` <span class="alt-zone-note ok" title="No zonal (AZ) restrictions for this subscription; usable in AZ ${avail.join(", ") || "all"}.">✓ No AZ restrictions</span>`;
    }
  }
  switch (v.verdict) {
    case "ok":
      return `<span class="alt-valid ok" title="${title}">✅ Available · quota OK</span>${zoneNote}`;
    case "quota": {
      const need = v.quota && v.quota.shortfall != null ? Math.round(v.quota.shortfall) : null;
      return `<span class="alt-valid warn" title="${title}">⚠️ Quota short${need != null ? ` ${need} vCPU` : ""}</span>${zoneNote} ${ticketLink("quota", "Request quota →")}`;
    }
    case "incompatible": {
      const miss = (v.parity && Array.isArray(v.parity.missing)) ? v.parity.missing.map(m => m.cap) : [];
      const detail = miss.length ? ` (missing ${escapeHtml(miss.join(", "))})` : "";
      return `<span class="alt-valid danger" title="${title}">⛔ Not capability-equivalent${detail}</span>`;
    }
    case "restricted":
      return `<span class="alt-valid danger" title="${title}">⛔ Restricted</span> ${ticketLink("technical", "Request access →")}`;
    case "unavailable":
      return `<span class="alt-valid danger" title="${title}">⛔ Not offered in region</span>`;
    default:
      return `<span class="alt-valid muted" title="${title}">ℹ️ ${escapeHtml(v.message || "Verify availability & quota manually")}</span>${zoneNote}`;
  }
}

// Patch the validation slots in the currently-rendered cost section.
function _patchAltValidation(regionShort, entry) {
  const slots = document.querySelectorAll(".alt-swap-valid[data-alt-fam]");
  slots.forEach((slot) => {
    const fam = slot.getAttribute("data-alt-fam");
    let badge;
    if (!entry || entry.status === "loading") {
      badge = `<span class="alt-valid checking">checking availability &amp; quota…</span>`;
    } else if (entry.status === "error") {
      badge = `<span class="alt-valid muted">validation unavailable — verify manually</span>`;
    } else {
      badge = _altValidBadge((entry.results || {})[fam], regionShort);
    }
    slot.innerHTML = badge;
  });
}

// Kick off (or reuse a cached) live availability/quota validation for the
// cheaper-SKU swaps in this region, then paint the result into the drilldown.
async function _validateAltsForRegion(r) {
  if (!r) return;
  const info = _pricingRegionInfo(r.short);
  const swaps = (info && info.compute && info.compute.swaps) || [];
  if (!swaps.length) return;
  const sub = focusedSubscriptionId() || "";
  const key = `${String(r.short).toLowerCase()}|${sub}`;

  const cached = PRICING.altValidation[key];
  if (cached && (cached.status === "done" || cached.status === "error")) {
    _patchAltValidation(r.short, cached);
    return;
  }
  if (cached && cached.status === "loading") {
    _patchAltValidation(r.short, cached);
    return;
  }

  const entry = { status: "loading", results: {} };
  PRICING.altValidation[key] = entry;
  _patchAltValidation(r.short, entry);

  try {
    const resp = await apiJson("/api/pricing/validate-alternatives", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription_id: sub,
        region: r.short,
        alternatives: swaps.map((s) => ({ family: s.to_family, from_family: s.from_family, required_cores: s.required_cores })),
      }),
    });
    entry.status = "done";
    entry.results = resp.results || {};
    entry.quota_status = resp.quota_status;
  } catch (e) {
    entry.status = "error";
    entry.error = e;
  }
  // Only repaint if the user is still looking at this region.
  if (String(STATE.activeDrilldownRegion || "").toLowerCase() === String(r.short).toLowerCase()) {
    _patchAltValidation(r.short, entry);
  }
}

// Drilldown cost section body (compute families + non-compute + total).
// Returns the inner content only — the collapsible section header is added by
// renderCostEstimateSection so _refreshDrilldownCost can swap the body without
// disturbing the section's collapse state.
function _costBodyInner(r) {
  const est = PRICING.estimate;
  const info = _pricingRegionInfo(r.short);
  let inner;
  if (PRICING.loading && !info) {
    inner = `<div class="note">Estimating cost…</div>`;
  } else if (!info || !info.priced_any) {
    inner = `<div class="note">No cost estimate available for this region.</div>`;
  } else {
    const cur = est.currency, c = info.compute, nc = info.noncompute;
    const famRows = (c.families || []).map(f => f.priced
      ? `<div class="key">${escapeHtml(f.label)}${f.priced_via_alt ? ` <span class="cost-alt-badge" title="${escapeHtml(f.label)} isn't sold in this region — priced on your BOM fallback ${escapeHtml(f.priced_label || "")}">↩ priced on ${escapeHtml(f.priced_label || "fallback")}</span>` : ""} <span class="muted">(${f.required_cores} vCPU × ${_fmtMoney(f.per_core_hour, cur, { cents: true })}/hr)</span></div><div>${_fmtMoney(f.monthly_net, cur)}/mo</div>`
      : `<div class="key">${escapeHtml(f.label)} <span class="muted">(${f.required_cores} vCPU)</span></div><div class="muted">not priced</div>`
    ).join("");
    const svcRows = (nc.items || []).map(s =>
      `<div class="key">${escapeHtml(s.service)}</div><div>${_fmtMoney(s.monthly_net, cur)}/mo</div>`
    ).join("");
    const acdLine = est.acd_discount_pct ? ` · ACD ${est.acd_discount_pct}% off list ${_fmtMoney(info.monthly_list, cur)}/mo` : "";
    inner = `
      <div class="cost-total-row">
        <div class="cost-total">${_fmtMoney(info.monthly_net, cur)}<small>/mo</small></div>
        <div class="cost-sub muted">${info.complete ? "" : "Partial — some families unpriced · "}${_fmtMoney(info.monthly_net * 12, cur)}/yr${acdLine}</div>
      </div>
      <div class="dd-readiness-subtitle">Compute — ${_fmtMoney(c.monthly_net, cur)}/mo</div>
      <div class="kv">${famRows || '<div class="muted">none</div>'}</div>
      ${_altSavingsBlock(info, cur, r.short)}
      <div class="dd-readiness-subtitle">Non-compute — ${_fmtMoney(nc.monthly_net, cur)}/mo</div>
      <div class="kv">
        ${svcRows}
        <div class="key">Uplift <span class="muted">(${nc.uplift_pct}% of compute)</span></div><div>${_fmtMoney(nc.uplift_net, cur)}/mo</div>
      </div>`;
  }
  const meta = est ? `OS: ${escapeHtml(est.os)} · ${est.hours_per_month} h/mo` : "OS: linux · 730 h/mo";
  return `<div class="cost-estimate">${inner}</div>
    <div class="note muted cost-disclaimer">${escapeHtml(PRICING_DISCLAIMER)}<br>${meta}.</div>`;
}

// Collapsible drilldown section. Each region-drilldown block is wrapped in one
// of these; collapsed by default to keep the panel compact. `title` and `badge`
// may contain trusted HTML (callers escape their own dynamic text).
function _ddSection(title, bodyHtml, opts = {}) {
  if (bodyHtml == null || String(bodyHtml).trim() === "") return "";
  const collapsed = opts.collapsed !== false; // default: collapsed
  const badge = opts.badge || "";
  const cls = opts.cls ? ` ${opts.cls}` : "";
  return `<section class="dd-section${collapsed ? " collapsed" : ""}${cls}" data-dd-section>
    <div class="dd-section-head" role="button" tabindex="0" aria-expanded="${collapsed ? "false" : "true"}">
      <span class="dd-caret" aria-hidden="true">▸</span>
      <span class="dd-section-title">${title}</span>
      <span class="dd-section-badge">${badge}</span>
    </div>
    <div class="dd-section-body">${bodyHtml}</div>
  </section>`;
}

function _toggleDdSection(head) {
  const section = head.closest(".dd-section");
  if (!section) return;
  const collapsed = section.classList.toggle("collapsed");
  head.setAttribute("aria-expanded", collapsed ? "false" : "true");
}

function renderCostEstimateSection(r) {
  if (!snapshotHasCoresRequirements(STATE.snapshot)) return "";
  const badge = `<span class="badge-est" title="${escapeHtml(PRICING_DISCLAIMER)}">Estimate</span>`;
  return _ddSection("Cost estimate", `<div id="dd-cost-wrap">${_costBodyInner(r)}</div>`, { badge });
}

function _refreshDrilldownCost(short) {
  const wrap = document.getElementById("dd-cost-wrap");
  if (!wrap) return;
  const r = _findRegionByShort(short);
  if (r) { wrap.innerHTML = _costBodyInner(r); _validateAltsForRegion(r); }
}

// ── Zone-redundancy (ZRS/HA) readiness ──────────────────────────────────────
// Zone-redundant storage / zone-redundant HA (e.g. Azure SQL Business Critical
// or General Purpose with ZR, Premium Redis, Flexible-Server GP/MO HA) require
// the target region to expose Availability Zones. This surfaces, per region,
// whether each ZRS-capable tier the user selected in their BOM can actually be
// deployed zone-redundant there — a region with no AZs can't host it.

// Return the BOM services whose selected tier is zone-redundant-capable:
//   [{ name, tierId, tierLabel }]
function _zrsCapableSelections() {
  const svcs = _currentBomServices(STATE.snapshot);
  const out = [];
  for (const s of svcs) {
    if (!s || !s.name || !s.tier) continue;
    const tiers = _catalogTiersFor(s.name);
    const hit = tiers.find(t => t.id === s.tier);
    if (hit && hit.zone_redundant) {
      out.push({ name: s.name, tierId: s.tier, tierLabel: _tierLabel(s.name, s.tier) });
    }
  }
  return out;
}

// Region-level AZ support: true / false from the region catalog, or null when
// unknown (catalog not loaded / region absent) so we can show an honest
// "unverified" state rather than a false negative.
function _regionSupportsAz(r) {
  const map = STATE.regionAzMap || null;
  if (!map) return null;
  const key = String(r.short || "").toLowerCase();
  if (!(key in map)) return null;
  return !!map[key];
}

// Overall ZRS pill for the region drilldown header, or null when the BOM has no
// ZRS-capable tier selections (nothing to check).
// Services for which we have an authoritative, per-subscription live check
// (must stay in sync with api/_shared/zonal_capability.py). Selections for
// these get a real ARM verdict; everything else falls back to region-AZ.
const _ZRS_LIVE_CHECKABLE = new Set([
  "Azure Blob Storage",
  "Azure Data Lake Storage Gen2",
  "Azure Files",
  "Managed Disks (Premium SSD)",
  "Azure SQL Database",
  "Azure SQL Managed Instance",
  "Azure Database for PostgreSQL",
  "Azure Database for MySQL",
  "Azure Elastic SAN",
]);

// Services with no read-only capability API, but which can be verified by an
// opt-in, non-destructive ARM pre-flight *validation* (creates nothing). These
// are surfaced with a "Run deep check" button rather than checked automatically.
// Must stay in sync with _VALIDATE_SERVICES + _ADVISORY_SERVICES in
// api/_shared/deploy_validation.py.
const _ZRS_DEEP_CHECKABLE = new Set([
  "Azure App Service",
  "App Service Environment",
  "Azure Logic Apps",
  "Azure Cache for Redis",
  "Azure Service Bus",
  "Azure Event Hubs",
  "Azure Container Registry",
  "Azure SignalR Service",
  "Azure Spring Apps",
  "Public IP Addresses",
  "Azure Load Balancer (Standard)",
  "Application Gateway (WAF v2)",
  "Azure VPN Gateway",
  "Azure ExpressRoute",
  "Azure AI Search",
  "Azure API Management",
  "Azure Cosmos DB",
]);

function _zrsKey(name, tier) { return `${name}||${tier}`; }

// Baseline (documented) mark from region-level AZ support — used for services
// with no authoritative API, and as the fallback before the live check returns.
function _zrsBaselineMark(az) {
  if (az === false) return `<span class="zrs-mark danger">⛔ Not supported (no AZs)</span>`;
  if (az === null) return `<span class="zrs-mark warn">⚠️ Unverified (region AZ unknown)</span>`;
  return `<span class="zrs-mark ok" title="Region exposes Availability Zones (documented, not live-verified for this service)">✓ Zone-redundant capable <span class="zrs-src">· region AZ</span></span>`;
}

// Live per-service verdict → mark HTML.
function _zrsLiveMark(v, az) {
  if (!v) return _zrsBaselineMark(az);
  const src = v.source ? ` <span class="zrs-src" title="Verified live via ${escapeHtml(v.source)}">· ${escapeHtml(v.source)}</span>` : "";
  switch (v.verdict) {
    case "available":
      return `<span class="zrs-mark ok" title="${escapeHtml(v.message || "")}">✓ Verified deployable${src}</span>`;
    case "blocked":
      return `<span class="zrs-mark danger" title="${escapeHtml(v.message || "")}">⛔ Restricted for this subscription${src}</span>`;
    case "unavailable":
      return `<span class="zrs-mark danger" title="${escapeHtml(v.message || "")}">⛔ Not available in region${src}</span>`;
    case "no_subscription":
      return `<span class="zrs-mark warn" title="${escapeHtml(v.message || "")}">⚠️ Select a subscription to verify</span>`;
    case "unverifiable":
      return `<span class="zrs-mark warn" title="${escapeHtml(v.message || "")} This provider's capabilities API returned no readable answer for your subscription — commonly a 403 on restricted (sponsored/MCAPS) subscriptions or throttling — so zone redundancy can't be confirmed either way. Use the deep check to validate by deployment pre-flight.">⚠️ Capability not readable for this subscription</span>`;
    default: // not_verifiable → documented region-AZ fallback
      return _zrsBaselineMark(az);
  }
}

// Deep (validate-based) per-service verdict → mark HTML. Blocked results carry a
// block_type + ticket hint so we can offer a one-click support-ticket path.
function _zrsDeepMark(v, az) {
  if (!v) return _zrsBaselineMark(az);
  const msg = escapeHtml(v.message || "");
  switch (v.verdict) {
    case "available":
      return `<span class="zrs-mark ok" title="${msg}">✓ Verified deployable <span class="zrs-src">· pre-flight</span></span>`;
    case "blocked": {
      const bt = v.block_type === "quota" ? "quota" : (v.block_type === "sku_restriction" ? "SKU/zone restriction" : "region restriction");
      const btn = `<button type="button" class="btn btn--xs zrs-ticket-btn" data-zrs-ticket="${escapeHtml(v.block_type || "")}" data-zrs-svc="${escapeHtml(v.name || "")}" title="Open a pre-filled Azure support ticket for this blocker">🎫 Create ticket</button>`;
      const help = v.help_url ? ` <a href="${escapeHtml(v.help_url)}" target="_blank" rel="noopener" class="zrs-src">learn more</a>` : "";
      return `<span class="zrs-mark danger" title="${msg}">⛔ Blocked (${escapeHtml(bt)}) <span class="zrs-src">· pre-flight</span></span> ${btn}${help}`;
    }
    case "advisory": {
      const help = v.help_url ? ` <a href="${escapeHtml(v.help_url)}" target="_blank" rel="noopener" class="zrs-src">region access</a>` : "";
      return `<span class="zrs-mark warn" title="${msg}">ℹ️ Not provable pre-deploy${help}</span>`;
    }
    case "no_resource_group": {
      const notFound = /not found/i.test(v.message || "");
      const label = notFound
        ? "✓ Read-only check · validation RG not found in this subscription"
        : "✓ Read-only check · deployment validation off";
      const enable = `<button type="button" class="btn btn--xs" onclick="openValidationRgSettings()" title="Optionally enable ARM deployment-level pre-flight for this subscription">⚙️ Enable</button>`;
      return `<span class="zrs-mark ok" title="${msg}">${label}</span> ${enable}`;
    }
    case "no_subscription":
      return `<span class="zrs-mark warn" title="${msg}">⚠️ Select a subscription to verify</span>`;
    case "unverifiable":
      return `<span class="zrs-mark warn" title="${msg}">⚠️ Pre-flight inconclusive</span>`;
    default:
      return _zrsBaselineMark(az);
  }
}

// Overall pill. `entry` (if present) carries live results so the header pill can
// reflect a real blocked/verified state rather than only region-AZ.
function _zrsReadinessPill(r, entry) {
  const sels = _zrsCapableSelections();
  if (!sels.length) return null;
  const az = _regionSupportsAz(r);
  if (entry && entry.status === "loading") {
    return { cls: "pill-warn", text: "ZRS: checking…", title: "Verifying zone-redundant deployability live against your subscription." };
  }
  const results = (entry && entry.status === "done") ? (entry.map || {}) : null;
  const needsZr = _bomNeedsZoneRedundancy();
  if (results) {
    const vals = Object.values(results);
    if (vals.some(v => v.verdict === "blocked" || v.verdict === "unavailable")) {
      return needsZr
        ? { cls: "pill-fail", text: "ZRS: blocked", title: "One or more zone-redundant tiers can't be deployed here for this subscription — see details." }
        : { cls: "pill-warn", text: "ZRS: advisory", title: "A zone-redundant tier is restricted here, but this workload is regional (single-zone tolerant), so it doesn't block deployment." };
    }
    const anyVerified = vals.some(v => v.verdict === "available");
    // If nothing was blocked and at least one was live-verified (and region AZ isn't a hard no), we're good.
    if (az !== false && anyVerified) {
      return { cls: "pill-ok", text: "ZRS: verified", title: "Zone-redundant tiers verified deployable against your subscription." };
    }
  }
  if (az === false) {
    return needsZr
      ? { cls: "pill-fail", text: "ZRS: no AZs", title: "This region has no Availability Zones — zone-redundant tiers can't be deployed here." }
      : { cls: "pill-warn", text: "ZRS: n/a", title: "This region has no Availability Zones, but this workload is regional (single-zone tolerant), so that's not a blocker." };
  }
  if (az === null) return { cls: "pill-warn", text: "ZRS: unverified", title: "Availability-Zone support for this region could not be confirmed." };
  return { cls: "pill-ok", text: "ZRS: ready", title: "This region supports Availability Zones — zone-redundant tiers can be deployed." };
}

function renderZrsReadinessSection(r) {
  const sels = _zrsCapableSelections();
  if (!sels.length) return "";
  const az = _regionSupportsAz(r);
  const sub = focusedSubscriptionId() || "";
  const needsZr = _bomNeedsZoneRedundancy();
  let intro;
  if (!needsZr) {
    // Regional (single-zone tolerant) workload: the whole section is advisory.
    intro = `<div class="note">This BOM's availability target is <strong>Regional (single-zone)</strong>, so zone-redundancy findings below are <strong>advisory only</strong> — they won't block a region. Switch the BOM to <em>Zone-redundant</em> if this workload needs multi-AZ HA.</div>`;
  } else if (az === false) {
    intro = `<div class="note danger">${escapeHtml(r.name)} has <strong>no Availability Zones</strong>. Zone-redundant (ZRS/HA) deployments aren't supported here — choose an AZ-enabled region for these tiers, or set this BOM's availability target to <em>Regional</em> if single-zone resilience is acceptable.</div>`;
  } else if (az === null) {
    intro = `<div class="note">Availability-Zone support for this region couldn't be confirmed from the catalog. Verify AZ availability before committing to zone-redundant tiers.</div>`;
  } else {
    intro = `<div class="note ok">${escapeHtml(r.name)} supports <strong>Availability Zones</strong>. Services below are verified live against your subscription where an authoritative API exists.</div>`;
  }
  const rows = sels.map(s => {
    const live = _ZRS_LIVE_CHECKABLE.has(s.name);
    const deep = _ZRS_DEEP_CHECKABLE.has(s.name);
    // Checkable services start in a "checking" state (filled by the live call);
    // documented ones show the region-AZ baseline immediately.
    const initial = live
      ? (sub ? `<span class="zrs-mark checking">⏳ Verifying live…</span>` : _zrsLiveMark({ verdict: "no_subscription" }, az))
      : (deep ? `<span class="zrs-mark">${_zrsBaselineMark(az)}<span class="zrs-src"> · deep check available</span></span>` : _zrsBaselineMark(az));
    return `<div class="key">${escapeHtml(s.name)} <span class="svc-tier-chip">${escapeHtml(s.tierLabel)}</span></div>` +
      `<div class="zrs-svc-slot" data-zrs-key="${escapeHtml(_zrsKey(s.name, s.tierId))}" data-zrs-svc-name="${escapeHtml(s.name)}" data-zrs-tier="${escapeHtml(s.tierId)}" data-zrs-live="${live ? "1" : "0"}" data-zrs-deep="${deep ? "1" : "0"}">${initial}</div>`;
  }).join("");
  const legend = `<div class="note muted zrs-legend">✓ Verified deployable = confirmed live via an authoritative ARM capability/SKU API for your subscription. ✓ Verified deployable · pre-flight = confirmed by the deep check (a non-destructive ARM deployment validation — creates nothing, no cost); ⛔ Blocked · pre-flight flags a quota, SKU/zone or region restriction. “region AZ” = documented Availability-Zone support only — run the deep check below to verify.</div>`;
  const hasDeep = sels.some(s => _ZRS_DEEP_CHECKABLE.has(s.name));
  let deepBar = "";
  if (hasDeep && az !== false) {
    deepBar = `<div class="zrs-deepbar note">
      <div><strong>Deep deployability check</strong> — for services with no read-only capability API (App Service, Logic Apps, Container Registry, SignalR, API Management, AI Search, Public IP, Load Balancer, Application Gateway, VPN Gateway, ExpressRoute, Redis, Service Bus, Event Hubs, Cosmos DB, and more) run a <strong>non-destructive</strong> ARM pre-flight validation (creates nothing, no cost) to confirm the zone-redundant tier actually deploys for your subscription here — catching quota, SKU/zone and region restrictions before you commit.</div>
      <button type="button" class="btn btn--accent btn--sm" id="zrs-deepcheck-btn" data-region-short="${escapeHtml(r.short)}">🔬 Run deep check</button>
    </div>`;
  }
  return `${intro}<div class="kv">${rows}</div>${deepBar}${legend}`;
}

// Patch the per-service slots + header pill once the live check resolves.
function _patchZonalCapability(regionShort, entry) {
  const az = STATE._zrsAzForActive;
  document.querySelectorAll(".zrs-svc-slot[data-zrs-live='1']").forEach((slot) => {
    const key = slot.getAttribute("data-zrs-key");
    if (!entry || entry.status === "loading") {
      slot.innerHTML = `<span class="zrs-mark checking">⏳ Verifying live…</span>`;
    } else if (entry.status === "error") {
      slot.innerHTML = _zrsLiveMark({ verdict: "unverifiable", message: "Live check unavailable — verify manually." }, az);
    } else {
      slot.innerHTML = _zrsLiveMark((entry.map || {})[key], az);
    }
  });
  const pillEl = document.getElementById("zrs-pill");
  if (pillEl) {
    const region = _findRegionByShort(regionShort);
    const pill = region ? _zrsReadinessPill(region, entry) : null;
    if (pill) {
      pillEl.className = `pill ${pill.cls}`;
      pillEl.title = pill.title;
      pillEl.textContent = pill.text;
    }
  }
  // Once live ZRS/HA results resolve, a restricted required tier must be
  // reflected in the headline Deployment Readiness verdict (not just its own
  // pill). Re-render the deployment section + pill for the active region.
  if (String(STATE.activeDrilldownRegion || "").toLowerCase() === String(regionShort || "").toLowerCase()) {
    const region = _findRegionByShort(regionShort);
    if (region) {
      const dep = getDeploymentVerdictInfo(region);
      const secEl = document.getElementById("dd-deploy-section");
      if (secEl) secEl.innerHTML = renderDeploymentReadinessSection(region, dep);
      const depPill = document.getElementById("dd-deploy-pill");
      if (depPill) {
        depPill.className = `pill ${dep.cls}`;
        depPill.title = dep.title;
        depPill.textContent = dep.text;
      }
    }
  }
}

// Kick off (or reuse cached) live zone-redundancy verification for the current
// BOM's zone-redundant selections in this region, then paint the result.
async function _verifyZonalForRegion(r) {
  if (!r) return;
  const sels = _zrsCapableSelections();
  const sub = focusedSubscriptionId() || "";
  STATE._zrsAzForActive = _regionSupportsAz(r);
  // Only the authoritatively-checkable selections need a round-trip.
  const checkable = sels.filter(s => _ZRS_LIVE_CHECKABLE.has(s.name));
  if (!checkable.length || !sub) return;
  const key = `${String(r.short).toLowerCase()}|${sub}`;

  const cached = PRICING.zonalCap[key];
  if (cached && (cached.status === "done" || cached.status === "error" || cached.status === "loading")) {
    _patchZonalCapability(r.short, cached);
    if (cached.status !== "loading") return;
    return;
  }

  const entry = { status: "loading", map: {} };
  PRICING.zonalCap[key] = entry;
  _patchZonalCapability(r.short, entry);

  try {
    const resp = await apiJson("/api/bom/zonal-capability", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription_id: sub,
        region: r.short,
        services: checkable.map(s => ({ name: s.name, tier: s.tierId })),
      }),
    });
    entry.status = "done";
    entry.map = {};
    (resp.results || []).forEach(v => { entry.map[_zrsKey(v.name, v.tier)] = v; });
    entry.ts = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";
  } catch (e) {
    entry.status = "error";
    entry.error = e;
  }
  if (String(STATE.activeDrilldownRegion || "").toLowerCase() === String(r.short).toLowerCase()) {
    _patchZonalCapability(r.short, entry);
  }
}

// ------------------------------------------------------ Verify-all scan
// A read-only, throttle-aware batch that runs the live zone-redundancy probe
// across every region to raise verdict confidence. Creates nothing (calls the
// same /api/bom/zonal-capability endpoint used by a single drilldown).
const _verifyAll = { running: false, cancel: false };

function _sleep(ms) { return new Promise(res => setTimeout(res, ms)); }

// Resolve one region's live entry in place, with bounded 429/5xx backoff.
async function _fetchZonalEntryInto(entry, r, sub, checkable) {
  const maxAttempts = 4;
  const body = JSON.stringify({
    subscription_id: sub,
    region: r.short,
    services: checkable.map(s => ({ name: s.name, tier: s.tierId })),
  });
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const resp = await apiJson("/api/bom/zonal-capability", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      entry.status = "done";
      entry.map = {};
      (resp.results || []).forEach(v => { entry.map[_zrsKey(v.name, v.tier)] = v; });
      entry.ts = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";
      return entry;
    } catch (e) {
      const code = e && e.status;
      if ((code === 429 || code === 502 || code === 503 || code === 504) && attempt < maxAttempts) {
        await _sleep(600 * Math.pow(2, attempt - 1) + Math.random() * 300);
        continue;
      }
      entry.status = "error";
      entry.error = e;
      return entry;
    }
  }
  return entry;
}

function _setVerifyAllUI(running) {
  const btn = document.getElementById("btn-verify-all");
  const cancel = document.getElementById("btn-verify-cancel");
  const prog = document.getElementById("verify-progress");
  if (btn) btn.classList.toggle("hidden", running);
  if (cancel) cancel.classList.toggle("hidden", !running);
  if (prog) prog.classList.toggle("hidden", !running);
}

function _updateVerifyProgress(done, total) {
  const fill = document.getElementById("verify-progress-fill");
  const label = document.getElementById("verify-progress-label");
  const pct = total ? Math.round((done / total) * 100) : 0;
  if (fill) fill.style.width = `${pct}%`;
  if (label) label.textContent = `${done}/${total} regions`;
}

async function verifyAllRegions({ force = false } = {}) {
  if (_verifyAll.running) return;
  const noteEl = document.getElementById("verify-all-note");
  const sub = focusedSubscriptionId() || "";
  const sels = _zrsCapableSelections();
  const checkable = sels.filter(s => _ZRS_LIVE_CHECKABLE.has(s.name));
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  if (!sub) { if (noteEl) noteEl.textContent = "Select a subscription first to run live verification."; return; }
  if (!checkable.length) { if (noteEl) noteEl.textContent = "This BOM has no live-verifiable zone-redundant services."; return; }
  if (!regions.length) return;

  const _rkey = r => `${String(r.short).toLowerCase()}|${sub}`;
  // Resumable: skip regions already verified for this subscription.
  let todo = regions.filter(r => {
    const c = PRICING.zonalCap[_rkey(r)];
    return !(c && c.status === "done");
  });
  // If a force re-run was requested, OR everything is already verified (so a
  // plain click would be a silent no-op), clear the cache for this sub and
  // re-probe every region. This guarantees the button always does something
  // visible and refreshes the live results.
  if (force || todo.length === 0) {
    regions.forEach(r => { delete PRICING.zonalCap[_rkey(r)]; });
    todo = regions.slice();
  }

  _verifyAll.running = true;
  _verifyAll.cancel = false;
  _setVerifyAllUI(true);
  if (noteEl) noteEl.textContent = "";
  const total = regions.length;
  let done = total - todo.length;
  _updateVerifyProgress(done, total);

  const CONC = 4;
  let idx = 0;
  const worker = async () => {
    while (idx < todo.length && !_verifyAll.cancel) {
      const r = todo[idx++];
      const key = `${String(r.short).toLowerCase()}|${sub}`;
      const entry = { status: "loading", map: {} };
      PRICING.zonalCap[key] = entry;
      await _fetchZonalEntryInto(entry, r, sub, checkable);
      done++;
      _updateVerifyProgress(done, total);
      if (String(STATE.activeDrilldownRegion || "").toLowerCase() === String(r.short).toLowerCase()) {
        _patchZonalCapability(r.short, entry);
      }
    }
  };
  const pool = [];
  for (let i = 0; i < Math.min(CONC, todo.length); i++) pool.push(worker());
  await Promise.all(pool);

  const cancelled = _verifyAll.cancel;
  _verifyAll.running = false;
  _setVerifyAllUI(false);
  applyFilters();
  _persistVerifyAll(sub).catch(() => {});
  if (noteEl) {
    // Report conclusiveness, not just completion: a "done" probe on a
    // restricted subscription can come back with no definitive answer, so
    // "Verified 38/38" would be misleading.
    let checked = 0, conclusive = 0, inconclusive = 0, errored = 0;
    regions.forEach(r => {
      const c = PRICING.zonalCap[`${String(r.short).toLowerCase()}|${sub}`];
      if (!c) return;
      if (c.status === "error") { errored++; return; }
      if (c.status !== "done") return;
      checked++;
      if (_zonalEntryConclusive(c)) conclusive++; else inconclusive++;
    });
    const ts = new Date().toLocaleTimeString();
    const parts = [`${conclusive} conclusive`];
    if (inconclusive) parts.push(`${inconclusive} inconclusive`);
    if (errored) parts.push(`${errored} errored`);
    let msg = cancelled
      ? `Stopped — live-checked ${checked}/${total} regions · ${parts.join(", ")}.`
      : `Live-checked ${checked}/${total} regions · ${parts.join(", ")} · ${ts}`;
    if (!cancelled && conclusive === 0 && (inconclusive + errored) > 0) {
      msg += " — no region could be conclusively verified for this subscription "
        + "(restricted access, throttling, or no authoritative API). Confidence stays at ARM metadata.";
    } else if (!cancelled && conclusive === total && total > 0) {
      msg += " — every region is at the highest confidence (Verified live). Re-running won't raise it further.";
    }
    noteEl.textContent = msg;
  }
}

function cancelVerifyAll() {
  if (_verifyAll.running) _verifyAll.cancel = true;
}

// Persist verified live results so a page reload / snapshot re-open keeps the
// raised confidence. Keyed by run_id + subscription; best-effort (ignored if
// the backend store is unavailable).
async function _persistVerifyAll(sub) {
  const runId = (STATE.snapshot && STATE.snapshot.meta && STATE.snapshot.meta.run_id)
    || (STATE.snapshot && STATE.snapshot.run_id) || "";
  if (!runId || !sub) return;
  const results = {};
  Object.keys(PRICING.zonalCap).forEach(k => {
    const [regionKey, keySub] = k.split("|");
    const entry = PRICING.zonalCap[k];
    if (keySub === sub && entry && entry.status === "done") {
      results[regionKey] = { map: entry.map, ts: entry.ts };
    }
  });
  if (!Object.keys(results).length) return;
  try {
    await apiJson("/api/bom/zonal-verifications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, subscription_id: sub, results }),
    });
  } catch (e) { /* best-effort */ }
}

// Rehydrate previously-persisted live results into PRICING.zonalCap when a
// snapshot loads, so confidence survives reloads.
async function _hydrateVerifyAll() {
  const runId = (STATE.snapshot && STATE.snapshot.meta && STATE.snapshot.meta.run_id)
    || (STATE.snapshot && STATE.snapshot.run_id) || "";
  const sub = focusedSubscriptionId() || "";
  if (!runId || !sub) return;
  try {
    const data = await apiJson(`/api/bom/zonal-verifications?run_id=${encodeURIComponent(runId)}&subscription_id=${encodeURIComponent(sub)}`);
    const results = (data && data.results) || {};
    Object.keys(results).forEach(regionKey => {
      const rec = results[regionKey] || {};
      PRICING.zonalCap[`${regionKey}|${sub}`] = { status: "done", map: rec.map || {}, ts: rec.ts, hydrated: true };
    });
    if (Object.keys(results).length) applyFilters();
  } catch (e) { /* no persisted results yet */ }
}

// Opt-in, non-destructive deep deployability check (ARM validate — creates
// nothing). Runs only on explicit user action + confirmation, for the
// zone-redundant selections that have no read-only capability API (see
// _ZRS_DEEP_CHECKABLE) plus advisory-only Cosmos DB.
async function _runZrsDeepCheck(regionShort) {
  const r = _findRegionByShort(regionShort);
  if (!r) return;
  const sub = focusedSubscriptionId() || "";
  if (!sub) { showToast("Select a subscription first to run the deep check.", "warn"); return; }
  const deepSels = _zrsCapableSelections().filter(s => _ZRS_DEEP_CHECKABLE.has(s.name));
  if (!deepSels.length) return;

  try { await ensureSupportSettings(); } catch (_e) {}
  const valRg = _valRgForSub(sub);
  const rgNote = valRg
    ? `Deployment-level validation ON — using resource group "${valRg}" in ${focusedSubscriptionName() || "the selected subscription"}.`
    : `Read-only checks only (quota, SKU & zonal availability). Deployment-level validation is optional — enable it later per subscription in Settings → Ticket owner.`;
  const ok = window.confirm(
    `Run a NON-DESTRUCTIVE deep deployability check in ${r.name}?\n\n` +
    `This calls Azure Resource Manager pre-flight validation for your ${deepSels.length} zone-redundant selection(s). ` +
    `It creates NO resources and incurs NO cost.\n\n${rgNote}`
  );
  if (!ok) return;

  const btn = document.getElementById("zrs-deepcheck-btn");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Validating…"; }
  document.querySelectorAll(".zrs-svc-slot[data-zrs-deep='1']").forEach((slot) => {
    slot.innerHTML = `<span class="zrs-mark checking">⏳ Pre-flight validating…</span>`;
  });

  try {
    const resp = await apiJson("/api/bom/deep-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription_id: sub,
        region: r.short,
        resource_group: valRg,
        services: deepSels.map(s => ({ name: s.name, tier: s.tierId })),
      }),
    });
    const az = _regionSupportsAz(r);
    const map = {};
    (resp.results || []).forEach(v => { map[_zrsKey(v.name, v.tier)] = v; });
    // Merge deep-check verdicts into the live cache so verdict confidence and
    // the verify-all scan consider them (keys never collide: a service is
    // live-checkable XOR deep-checkable).
    const cacheKey = `${String(r.short).toLowerCase()}|${sub}`;
    const prev = PRICING.zonalCap[cacheKey];
    const mergedMap = Object.assign({}, (prev && prev.map) || {}, map);
    PRICING.zonalCap[cacheKey] = {
      status: "done",
      map: mergedMap,
      ts: new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC",
    };
    document.querySelectorAll(".zrs-svc-slot[data-zrs-deep='1']").forEach((slot) => {
      const key = slot.getAttribute("data-zrs-key");
      const v = map[key];
      slot.innerHTML = v ? _zrsDeepMark(v, az) : _zrsBaselineMark(az);
    });
    const blocked = (resp.results || []).filter(v => v.verdict === "blocked").length;
    if (blocked) showToast(`Deep check complete — ${blocked} tier(s) blocked. Use “Create ticket” to request access.`, "warn");
    else showToast("Deep check complete — no blockers found.", "success");
    // Reflect merged verdicts in the headline verdict + region table.
    _patchZonalCapability(r.short, PRICING.zonalCap[cacheKey]);
    applyFilters();
  } catch (e) {
    document.querySelectorAll(".zrs-svc-slot[data-zrs-deep='1']").forEach((slot) => {
      slot.innerHTML = `<span class="zrs-mark warn">⚠️ Deep check failed</span>`;
    });
    showToast(e.message || "Deep check failed.", "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔬 Run deep check"; }
  }
}

// -------- Cost & pricing settings (gear → Settings → Cost & pricing) --------

async function loadPricingSettings() {
  if (!PRICING.settings) {
    try { const s = await apiJson("/api/pricing/settings"); PRICING.settings = s.settings || {}; }
    catch (e) { PRICING.settings = {}; }
  }
  const s = PRICING.settings || {};
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set("pricing-acd", s.acd_discount_pct != null ? s.acd_discount_pct : 0);
  set("pricing-os", s.pricing_os || "linux");
  set("pricing-hours", s.hours_per_month != null ? s.hours_per_month : 730);
  set("pricing-currency", (s.currency || "USD").toUpperCase());
  set("pricing-uplift", s.noncompute_uplift_pct != null ? s.noncompute_uplift_pct : 35);
  set("pricing-alt-min", s.alt_min_savings_pct != null ? s.alt_min_savings_pct : 5);
  const altToggle = document.getElementById("pricing-suggest-alts");
  if (altToggle) altToggle.checked = s.suggest_alternatives !== false;
  const genToggle = document.getElementById("pricing-allow-older-gen");
  if (genToggle) genToggle.checked = s.allow_older_generation === true;
  _renderServiceEstimateInputs(s.service_estimates || {});
  const msg = document.getElementById("pricing-save-msg");
  if (msg) msg.textContent = "";
}

function _renderServiceEstimateInputs(estimates) {
  const wrap = document.getElementById("pricing-services");
  if (!wrap) return;
  const names = _bomServiceNames(STATE.snapshot);
  const tiers = _bomServiceTiers(STATE.snapshot);
  if (!names.length) {
    wrap.innerHTML = `<p class="muted">No non-compute services in the current BOM.</p>`;
    return;
  }
  wrap.innerHTML = names.map(n => {
    const v = (estimates && estimates[n] != null) ? estimates[n] : "";
    const tierId = tiers[n];
    const tierChip = tierId
      ? ` <span class="svc-tier-chip">${escapeHtml(_tierLabel(n, tierId))}</span>`
      : "";
    return `<label class="pricing-svc-row"><span>${escapeHtml(n)}${tierChip}</span>
      <input type="number" min="0" step="1" data-service="${escapeHtml(n)}" value="${escapeHtml(String(v))}" placeholder="0" /><small class="muted">$/mo</small></label>`;
  }).join("");
}

async function savePricingSettings() {
  const num = (id, d) => { const el = document.getElementById(id); const n = el ? parseFloat(el.value) : NaN; return Number.isFinite(n) ? n : d; };
  const svc = {};
  document.querySelectorAll("#pricing-services [data-service]").forEach(inp => {
    const name = inp.getAttribute("data-service");
    const n = parseFloat(inp.value);
    if (Number.isFinite(n) && n > 0) svc[name] = n;
  });
  const patch = {
    acd_discount_pct: num("pricing-acd", 0),
    pricing_os: (document.getElementById("pricing-os") || {}).value || "linux",
    hours_per_month: num("pricing-hours", 730),
    currency: (((document.getElementById("pricing-currency") || {}).value) || "USD").toUpperCase(),
    noncompute_uplift_pct: num("pricing-uplift", 35),
    suggest_alternatives: !!(document.getElementById("pricing-suggest-alts") || {}).checked,
    alt_min_savings_pct: num("pricing-alt-min", 5),
    allow_older_generation: !!(document.getElementById("pricing-allow-older-gen") || {}).checked,
    service_estimates: svc,
  };
  const msg = document.getElementById("pricing-save-msg");
  const btn = document.getElementById("pricing-save");
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = "Saving…";
  try {
    const res = await apiJson("/api/pricing/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    PRICING.settings = res.settings || patch;
    if (msg) msg.textContent = "Saved — recalculating estimate…";
    await fetchPricingEstimate(true);
    if (msg) msg.textContent = "Saved.";
    showToast("Pricing settings saved.", "success");
  } catch (e) {
    if (msg) msg.textContent = "";
    showToast(e.message || "Could not save pricing settings.", "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initContinentFilter() {
  const container = document.getElementById("filter-continents");
  container.innerHTML = "";
  const continents = Array.from(new Set((STATE.snapshot.regions || []).map(r => r.geo))).sort();
  for (const c of continents) {
    const lbl = document.createElement("label");
    lbl.className = "checkrow";
    lbl.innerHTML = `<input type="checkbox" data-filter="continent" value="${c}" checked /> ${c}`;
    container.appendChild(lbl);
  }
  container.querySelectorAll("input").forEach(el => el.addEventListener("change", applyFilters));
}

// Collect the primary/alt SKU labels from a snapshot, preferring bom.skus
// (current engine), then meta.skus_resolved (older snapshots). Returns
// { primaries: [...], alts: [...], hasFallbackMeta: bool } so the UI can
// build dynamic labels without hard-coding "v5" or "v6".
function collectSkuSelectionLabels() {
  const snap = STATE.snapshot;
  if (!snap) return { primaries: [], alts: [], hasFallbackMeta: false };

  const dedup = (arr) => Array.from(new Set(arr.filter(s => typeof s === "string" && s.trim())));

  let primaries = [];
  let alts = [];
  const bomSkus = (snap.bom && Array.isArray(snap.bom.skus)) ? snap.bom.skus : [];
  if (bomSkus.length) {
    primaries = dedup(bomSkus.map(s => s.primary));
    alts = dedup(bomSkus.map(s => s.alt));
  }

  if (!primaries.length) {
    const resolved = (snap.meta && Array.isArray(snap.meta.skus_resolved)) ? snap.meta.skus_resolved : [];
    primaries = dedup(resolved.map(s => s.primary_label));
    alts = dedup(resolved.map(s => s.alt_label));
  }

  return { primaries, alts, hasFallbackMeta: alts.length > 0 };
}

function buildSkuSelectionSubtitle(labels) {
  if (!labels.primaries.length) return "Primary / Fallback SKU selection";
  const pStr = `Primary: ${labels.primaries.join(", ")}`;
  const fStr = labels.alts.length ? ` · Fallback: ${labels.alts.join(", ")}` : "";
  return pStr + fStr;
}

// Refresh the sidebar SKU heading + overview card subtitle, and hide the
// fallback filter row when nothing in the snapshot uses a fallback SKU.
function updateSkuSelectionLabels() {
  const labels = collectSkuSelectionLabels();
  const subtitle = buildSkuSelectionSubtitle(labels);

  const sideSub = document.getElementById("filter-sku-subtitle");
  if (sideSub) sideSub.textContent = subtitle;
  const overSub = document.getElementById("overview-sku-subtitle");
  if (overSub) overSub.textContent = subtitle;

  // Only hide the fallback row when there is genuinely no fallback in play
  // — i.e. no alt configured AND no region actually fell back. This keeps
  // legacy snapshots that lack bom.skus metadata but have populated
  // sku_fallbacks still filterable.
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  const anyRegionFellBack = regions.some(r => {
    const fb = (r.fell_back != null) ? r.fell_back : ((r.sku_fallbacks || []).length > 0);
    return fb;
  });
  const fallbackRow = document.getElementById("filter-fallback-row");
  if (fallbackRow) {
    const showFallback = labels.hasFallbackMeta || anyRegionFellBack;
    fallbackRow.classList.toggle("hidden", !showFallback);
    if (!showFallback) {
      const cb = document.getElementById("filter-v5-fallback");
      if (cb && cb.checked) cb.checked = false;
    }
  }
}

// ---------------------------------------------------------------- Table view

function renderTable() {
  const tbody = document.querySelector("#regions-table tbody");
  tbody.innerHTML = "";
  const showQuotaCol = snapshotHasCoresRequirements(STATE.snapshot);
  const headerCell = document.querySelector("#regions-table thead .quota-col");
  if (headerCell) {
    headerCell.classList.toggle("hidden", !showQuotaCol);
  }
  const costHeader = document.querySelector("#regions-table thead .cost-col");
  if (costHeader) {
    costHeader.classList.toggle("hidden", !showQuotaCol);
  }
  for (const r of STATE.filtered) {
    const tr = document.createElement("tr");
    tr.dataset.region = r.name;
    const deployment = getDeploymentVerdictInfo(r);

    const zoneHtml = _regionSupportsAz(r) === false
      ? `<span class="zone-noaz" title="This region has no Availability Zones — resources are regional (single-zone) only.">Regional only</span>`
      : r.zone_health.map((z, i) =>
          `<span class="zone-cell ${z}" title="AZ${i + 1}: ${z === "green" ? "OK" : "Blocked"}">${i + 1}</span>`
        ).join("");

    let quotaCellHtml = "";
    if (showQuotaCol) {
      const v = getRegionQuotaVerdictForSubscription(STATE.snapshot, r.short, focusedSubscriptionId());
      const p = _regionQuotaVerdictLabel(v);
      quotaCellHtml = `<td class="quota-col"><span class="pill ${p.cls}" title="${escapeHtml(p.title)}">${escapeHtml(p.text)}</span></td>`;
    } else {
      quotaCellHtml = `<td class="quota-col hidden"></td>`;
    }

    tr.innerHTML = `
      <td class="region-cell">${escapeHtml(r.name)}
        <div class="country">${escapeHtml(r.country || "")}</div>
      </td>
      <td><span class="pill ${deployment.cls}" title="${escapeHtml(deployment.title)}">${escapeHtml(deployment.text)}</span><span class="conf-dot ${_confidenceBadge(deployment).cls}" title="${escapeHtml(_confidenceBadge(deployment).text + " — " + _confidenceBadge(deployment).title)}"></span></td>
      <td>${escapeHtml(r.geo || "")}</td>
      <td><span class="zone-cells">${zoneHtml}</span></td>
      ${quotaCellHtml}
      <td class="rec-cell">${escapeHtml(r.recommendation || "—")}</td>
      ${_costCellHtml(r)}
      <td class="alt-cell">${escapeHtml((r.alt_regions || []).map(a =>
        a.latency_ms != null ? `${a.region} (${a.latency_ms}ms)`
          : (a.source === "least_bad" && a.caveat ? `${a.region} (${a.caveat})` : a.region)
      ).join(", "))}</td>
    `;
    tr.addEventListener("click", () => openDrilldown(r));
    tbody.appendChild(tr);
  }
}

function statusClass(s) {
  if (s === "OK") return "pill-ok";
  if (s === "BOM Issue") return "pill-bom";
  if (s === "Compute Issue") return "pill-compute";
  return "pill-both";
}

// inline status pill colors are defined in styles.css under
// .status-pill.pill-ok / .pill-bom / .pill-compute / .pill-both

// ---------------------------------------------------------------- Drilldown

function openDrilldown(r) {
  STATE.activeDrilldownRegion = r.short || "";
  document.getElementById("dd-name").textContent = r.name;
  document.getElementById("dd-geo").textContent = `${r.geo} • ${r.country || ""}`;

  const body = document.getElementById("dd-body");
  let html = "";
  const deployment = getDeploymentVerdictInfo(r);
  const deploymentPill = `<span id="dd-deploy-pill" class="pill ${deployment.cls}" title="${escapeHtml(deployment.title)}">${escapeHtml(deployment.text)}</span>`;
  // All drilldown sections start collapsed so the panel opens compact.
  html += _ddSection("Deployment Readiness",
    `<div id="dd-deploy-section">${renderDeploymentReadinessSection(r, deployment)}</div>`,
    { badge: deploymentPill });

  const ddVerdict = getRegionQuotaVerdictForSubscription(STATE.snapshot, r.short, focusedSubscriptionId());
  const ddVerdictPill = _regionQuotaVerdictLabel(ddVerdict);
  const ddVerdictHtml = `<span class="pill ${ddVerdictPill.cls}" title="${escapeHtml(ddVerdictPill.title)}">${escapeHtml(ddVerdictPill.text)}</span>`;
  const statusPill = `<span class="status-pill ${statusClass(r.status)}">${escapeHtml(r.status)}</span>`;
  html += _ddSection("Summary", `<div class="kv">
      <div class="key">Status</div><div>${statusPill}</div>
      <div class="key">Region (short)</div><div>${escapeHtml(r.short)}</div>
      <div class="key">Quota</div><div>${ddVerdictHtml}</div>
    </div>`, { badge: statusPill });

  html += renderCostEstimateSection(r);

  const zrsSection = renderZrsReadinessSection(r);
  if (zrsSection) {
    const zrsPill = _zrsReadinessPill(r);
    const zrsBadge = zrsPill
      ? `<span id="zrs-pill" class="pill ${zrsPill.cls}" title="${escapeHtml(zrsPill.title)}">${escapeHtml(zrsPill.text)}</span>`
      : "";
    html += _ddSection("Zone-redundancy (ZRS/HA) readiness", zrsSection, { badge: zrsBadge });
  }

  const _noAz = _regionSupportsAz(r) === false;
  if (_noAz) {
    // Region has no Availability Zones — per-AZ red/green grids are
    // meaningless here, so replace them with an explicit regional-only note.
    const zhHtml = `<div class="note danger"><strong>Regional only — no Availability Zones.</strong> ${escapeHtml(r.name)} does not offer Availability Zones, so zone-redundant (ZRS/HA, multi-AZ) deployments aren't possible here. Resources are single-zone (locally redundant) only. Choose an AZ-enabled region if your BOM requires zone redundancy.</div>`;
    html += _ddSection("Zone Availability", zhHtml);
  } else if (r.sku_zone_detail && Object.keys(r.sku_zone_detail).length) {
    // Determine which SKU families are BOM primary vs fallback
    const reqs = _getCoresRequirements(STATE.snapshot || {});
    const primaryLabels = new Set(reqs.map(rq => (rq.primary_label || "").toLowerCase()));
    let zoneHtml = `<div class="kv">`;
    for (const [sku, zones] of Object.entries(r.sku_zone_detail)) {
      const cells = zones.map((z, i) =>
        `<span class="zone-cell ${z ? "green" : "red"}" style="margin-right:2px">${i + 1}</span>`
      ).join("");
      const isPrimary = primaryLabels.has(sku.toLowerCase());
      let tag = "";
      if (isPrimary) {
        tag = ` <span class="sku-tag sku-tag--primary">Primary</span>`;
      } else {
        tag = ` <span class="sku-tag sku-tag--fallback">Fallback</span>`;
      }
      const label = isPrimary ? `<strong>${escapeHtml(sku)}</strong>${tag}` : `${escapeHtml(sku)}${tag}`;
      zoneHtml += `<div class="key">${label}</div><div>${cells}</div>`;
    }
    zoneHtml += `</div>`;
    // Show SKU blockers summary (zone grid already shows per-AZ red/green)
    const skuBlockers = r.sku_blockers || [];
    if (skuBlockers.length) {
      zoneHtml += `<div class="drilldown-zone-restrictions">`;
      for (const issue of skuBlockers) {
        zoneHtml += `<div class="note danger">${escapeHtml(issue)}</div>`;
      }
      zoneHtml += `</div>`;
    }
    html += _ddSection("Zone &amp; SKU Availability", zoneHtml);
  } else {
    // Fallback: just show zone health if no SKU detail available
    let zhHtml = `<div class="kv">`;
    for (let i = 0; i < 3; i++) {
      const z = r.zone_health[i];
      const restriction = r.zone_restrictions[i] || "(none reported)";
      zhHtml += `<div class="key">AZ ${i + 1}</div>
        <div><span class="zone-cell ${z}" style="margin-right:6px">${i + 1}</span> ${escapeHtml(restriction)}</div>`;
    }
    zhHtml += `</div>`;
    html += _ddSection("Zone Health", zhHtml);
  }

  if (r.chosen_skus && r.chosen_skus.length && !(r.sku_zone_detail && Object.keys(r.sku_zone_detail).length)) {
    html += _ddSection("Recommended SKUs",
      r.chosen_skus.map(sku => `<div class="note">${escapeHtml(sku)}</div>`).join(""));
  }

  if (r.sku_fallbacks && r.sku_fallbacks.length) {
    html += _ddSection("v5 Fallbacks",
      r.sku_fallbacks.map(f => `<div class="note warn">${escapeHtml(f)}</div>`).join(""));
  }

  if (r.missing_services && r.missing_services.length) {
    html += _ddSection("Missing BOM Services",
      r.missing_services.map(ms => `<div class="note danger"><strong>${escapeHtml(ms.service)}</strong>: ${escapeHtml(ms.detail)}</div>`).join(""));
  }

  if (r.registration_required && r.registration_required.length) {
    html += _ddSection("Registration Required", _registrationRequiredHtml(r.registration_required));
  }

  if (r.alt_regions && r.alt_regions.length) {
    const isLeastBad = r.alt_regions.some(a => a.source === "least_bad");
    const altHtml = r.alt_regions.map(a => {
      const prox = a.latency_ms != null
        ? `${a.latency_ms} ms`
        : (a.distance_km != null ? `~${a.distance_km} km` : "geo proximity");
      const caveat = a.source === "least_bad" && a.caveat
        ? `<span class="alt-caveat" title="Residual gaps in this region">${escapeHtml(a.caveat)}</span>`
        : "";
      return `<div class="alt-row"><span>${escapeHtml(a.region)}${caveat}</span><span class="ms">${prox}</span></div>`;
    }).join("");
    const title = isLeastBad
      ? "Closest-to-deployable regions (no region is fully healthy)"
      : "Alternative regions based on health and latency";
    const intro = isLeastBad
      ? `<div class="note warn">No region in this snapshot is fully deployment-ready for your BOM. These are the <strong>least-bad</strong> options — ranked by fewest remaining gaps — but each still has caveats to resolve.</div>`
      : "";
    html += _ddSection(title, intro + altHtml);
  }

  const quotaResult = buildQuotaGroupRowsForRegion(STATE.snapshot, r.short);
  const quotaPill = _quotaGroupStatusLabel(_deriveQuotaRegionStatus(quotaResult.rows, r.quota_status || "unknown"));
  if (quotaResult.rows.length) {
    const subName = focusedSubscriptionName();
    const quotaBadge = `<span class="pill ${quotaPill.cls}">${escapeHtml(quotaPill.text)}</span>${subName ? `<span class="dd-sub-context">Focused sub: ${escapeHtml(subName)}</span>` : ""}`;
    html += _ddSection("Quota", renderDrilldownQuotaSection(r, quotaResult, quotaPill), { badge: quotaBadge });
  }

  body.innerHTML = html;
  renderSubscriptionSwitcher();
  _scanRegistrationCards(body);
  _validateAltsForRegion(r);
  _verifyZonalForRegion(r);
  if (!body._ddSectionBound) {
    body.addEventListener("click", (ev) => {
      const head = ev.target.closest(".dd-section-head");
      if (!head || !body.contains(head)) return;
      if (ev.target.closest("a, button, [data-rec-ticket], [data-alt-ticket]")) return;
      _toggleDdSection(head);
    });
    body.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const head = ev.target.closest(".dd-section-head");
      if (!head || !body.contains(head)) return;
      ev.preventDefault();
      _toggleDdSection(head);
    });
    body._ddSectionBound = true;
  }
  if (!body._quotaRequestBound) {
    body.addEventListener("click", _handleQuotaRequestInteraction);
    body._quotaRequestBound = true;
  }
  if (!body._recTicketBound) {
    body.addEventListener("click", (ev) => {
      const link = ev.target.closest("[data-rec-ticket]");
      if (!link) return;
      ev.preventDefault();
      const kind = link.getAttribute("data-rec-ticket");
      const regionShort = STATE.activeDrilldownRegion || "";
      closeDrilldown();
      _supportPrefill(kind, regionShort);
    });
    body._recTicketBound = true;
  }
  if (!body._altTicketBound) {
    body.addEventListener("click", (ev) => {
      const link = ev.target.closest("[data-alt-ticket]");
      if (!link) return;
      ev.preventDefault();
      const kind = link.getAttribute("data-alt-ticket");
      const regionShort = link.getAttribute("data-alt-region") || STATE.activeDrilldownRegion || "";
      const family = link.getAttribute("data-alt-family") || "";
      const label = link.getAttribute("data-alt-label") || "";
      const cores = Number(link.getAttribute("data-alt-cores") || 0);
      const limitRaw = link.getAttribute("data-alt-limit");
      const currentLimit = limitRaw !== "" && limitRaw != null ? Number(limitRaw) : null;
      closeDrilldown();
      _supportPrefill(kind, regionShort, {
        family,
        label,
        cores: cores > 0 ? cores : undefined,
        currentLimit: Number.isFinite(currentLimit) ? currentLimit : undefined,
      });
    });
    body._altTicketBound = true;
  }
  if (!body._zrsDeepBound) {
    body.addEventListener("click", (ev) => {
      const runBtn = ev.target.closest("#zrs-deepcheck-btn");
      if (runBtn) {
        ev.preventDefault();
        _runZrsDeepCheck(runBtn.getAttribute("data-region-short") || STATE.activeDrilldownRegion || "");
        return;
      }
      const tkBtn = ev.target.closest(".zrs-ticket-btn");
      if (tkBtn) {
        ev.preventDefault();
        const blockType = tkBtn.getAttribute("data-zrs-ticket") || "";
        const regionShort = STATE.activeDrilldownRegion || "";
        const kind = blockType === "sku_restriction" ? "technical" : "quota";
        closeDrilldown();
        _supportPrefill(kind, regionShort);
      }
    });
    body._zrsDeepBound = true;
  }
  document.getElementById("drilldown").classList.remove("hidden");
  document.getElementById("drilldown-overlay").classList.add("open");
}

function closeDrilldown() {
  STATE.activeDrilldownRegion = "";
  document.getElementById("drilldown").classList.add("hidden");
  document.getElementById("drilldown-overlay").classList.remove("open");
}
window.closeDrilldown = closeDrilldown;

function getDeploymentVerdictInfo(region) {
  const raw = (region && region.deployment_verdict) || {};
  const inferredVerdict = inferDeploymentVerdict(region);
  const verdict = raw.verdict || inferredVerdict;
  const reasons = Array.isArray(raw.reasons) ? raw.reasons : [];
  const constraints = Array.isArray(raw.constraints) ? raw.constraints : [];
  const blockers = Array.isArray(raw.blockers) ? raw.blockers : [];
  let info;
  switch (verdict) {
    case "ready":
      info = {
        verdict,
        text: "Ready",
        cls: "pill-ok",
        title: "All required services, SKUs, and quota checks passed.",
        reasons,
        constraints,
        blockers,
      };
      break;
    case "ready_with_constraints":
      info = {
        verdict,
        text: "Ready with constraints",
        cls: "pill-warn",
        title: "Core requirements pass, but there are caveats to validate before deployment.",
        reasons,
        constraints,
        blockers,
      };
      break;
    case "not_recommended":
      info = {
        verdict,
        text: "Not recommended",
        cls: "pill-fail",
        title: "Critical blockers make this region a poor deployment target.",
        reasons,
        constraints,
        blockers,
      };
      break;
    case "needs_validation":
    default:
      info = {
        verdict: "needs_validation",
        text: "Needs validation",
        cls: "pill-muted",
        title: "Automated checks could not fully validate deployment readiness.",
        reasons,
        constraints,
        blockers,
      };
      break;
  }
  return _augmentVerdictWithZrs(region, info);
}

// Confidence tier + provenance for a region's verdict. The compile-time verdict
// is "capability" (ARM metadata-backed). A live ZRS/HA check promotes it to
// "validated" when we got a definitive answer, or notes "unverifiable" when a
// live probe was attempted but inconclusive (403 / throttle / no API).
function _regionConfidence(region) {
  const raw = (region && region.deployment_verdict) || {};
  let tier = raw.confidence || ((region && region.deployment_verdict) ? "capability" : "metadata");
  const provenance = Array.isArray(raw.provenance) ? raw.provenance.slice() : [];
  let liveNote = null;
  const sub = focusedSubscriptionId() || "";
  const key = sub ? `${String((region && region.short) || "").toLowerCase()}|${sub}` : "";
  const entry = key ? (PRICING.zonalCap || {})[key] : null;
  if (entry) {
    if (entry.status === "error") {
      liveNote = "unverifiable";
    } else if (entry.status === "done" && entry.map) {
      const vals = Object.values(entry.map);
      const definitive = vals.some(v => v && ["available", "blocked", "unavailable"].includes(v.verdict));
      const inconclusive = vals.some(v => v && ["unverifiable", "not_verifiable", "no_resource_group", "no_subscription", "advisory"].includes(v.verdict));
      if (definitive) {
        tier = "validated";
        provenance.push({
          signal: "Live ZRS/HA deployability",
          source: `verified against subscription${entry.ts ? ` · ${entry.ts}` : ""}`,
        });
        if (inconclusive) liveNote = "partial";
      } else {
        // A live probe ran but returned no definitive per-subscription answer
        // (restricted sub, no authoritative API, or an empty result). Flag it
        // honestly as unverifiable rather than silently leaving it at the
        // metadata tier, which reads as "nothing happened".
        liveNote = "unverifiable";
      }
    }
  }
  return { tier, provenance, liveNote };
}

// Classify a completed zonal-capability entry: true only when it carries at
// least one *definitive* per-subscription verdict (available/blocked/
// unavailable). A "done" probe that returned only inconclusive results (403 /
// restricted / no API / empty) is NOT a conclusive live verification.
function _zonalEntryConclusive(entry) {
  if (!entry || entry.status !== "done" || !entry.map) return false;
  return Object.values(entry.map).some(
    v => v && ["available", "blocked", "unavailable"].includes(v.verdict),
  );
}

// Presentation for a confidence tier → pill.
function _confidenceBadge(info) {
  const tier = (info && info.confidence) || "capability";
  const map = {
    validated: { cls: "conf-validated", text: "Verified live", title: "A live per-subscription check confirmed the constrained tiers — highest confidence." },
    capability: { cls: "conf-capability", text: "ARM metadata", title: "Backed by ARM SKU / provider / quota metadata from the last snapshot. Run Verify all for a live per-subscription confirmation." },
    metadata: { cls: "conf-metadata", text: "Baseline", title: "Region/BOM baseline only — no ARM capability data. Re-run analysis for full signals." },
  };
  const base = map[tier] || map.capability;
  if (info && info.liveNote === "unverifiable") {
    return { cls: "conf-unverifiable", text: "Unverifiable", title: "A live check was attempted but couldn't determine deployability (restricted subscription, throttling, or no authoritative API). Treat with caution." };
  }
  return base;
}

// Read cached live zone-redundancy (ZRS/HA) results for the focused
// subscription and return the selections that came back blocked/unavailable.
// Returns [] when no live check has run yet (e.g. at table render time), so the
// headline verdict is only downgraded once we actually have authoritative data.
function _zrsBlockedForRegion(region) {
  const sub = focusedSubscriptionId() || "";
  if (!region || !sub) return [];
  const key = `${String(region.short || "").toLowerCase()}|${sub}`;
  const entry = (PRICING.zonalCap || {})[key];
  if (!entry || entry.status !== "done" || !entry.map) return [];
  const out = [];
  for (const v of Object.values(entry.map)) {
    if (v && (v.verdict === "blocked" || v.verdict === "unavailable")) out.push(v);
  }
  return out;
}

// A required zone-redundant / HA tier that is restricted for this subscription
// is a genuine caveat: the region can still host non-HA tiers, but it is not
// unconditionally "Ready". Downgrade Ready → Ready with constraints and surface
// each restricted tier as a zone-gap blocker so it shows in the readiness list.
function _augmentVerdictWithZrs(region, info) {
  const conf = _regionConfidence(region);
  info.confidence = conf.tier;
  info.provenance = conf.provenance;
  info.liveNote = conf.liveNote;
  const blocks = _zrsBlockedForRegion(region);
  if (!blocks.length) return info;
  const needsZr = _bomNeedsZoneRedundancy();
  const blockers = Array.isArray(info.blockers) ? info.blockers.slice() : [];
  for (const v of blocks) {
    const tierTxt = v.tier ? ` (${v.tier})` : "";
    const detail = v.message || "zone-redundant / HA tier is restricted for this subscription in this region.";
    blockers.push({
      type: "zone_gap",
      severity: needsZr ? "critical" : "info",
      message: `${v.name}${tierTxt}: ${detail}` +
        (needsZr ? "" : " — advisory only (this workload is regional / single-zone tolerant)."),
    });
  }
  const next = { ...info, blockers };
  // Regional workloads don't need Availability Zones, so a zone-redundancy
  // restriction is informational and must not downgrade the region verdict.
  if (!needsZr) return next;
  // Zone-redundant workloads DO need AZs: a tier that is restricted/unavailable
  // for this subscription is a hard blocker. Escalate the headline to red so it
  // matches the "ZRS: blocked" panel instead of a soft "Ready with constraints".
  if (next.verdict !== "not_recommended") {
    next.verdict = "not_recommended";
    next.text = "Not recommended";
    next.cls = "pill-fail";
    next.title = "One or more zone-redundant (ZRS/HA) tiers your BOM requires are restricted or unavailable for this subscription in this region.";
  }
  return next;
}

function inferDeploymentVerdict(region) {
  if (!region) return "needs_validation";
  const quota = getRegionQuotaVerdict(STATE.snapshot, region.short || "");
  const hasMissingServices = Array.isArray(region.missing_services) && region.missing_services.length > 0;
  const hasSkuBlockers = Array.isArray(region.sku_blockers) && region.sku_blockers.length > 0;
  const fellBack = (region.fell_back != null) ? region.fell_back : ((region.sku_fallbacks || []).length > 0);
  if (hasMissingServices || hasSkuBlockers) return "not_recommended";
  if (quota.verdict === "unknown" || quota.verdict === "not_accessible" || quota.verdict === "no_sub") return "needs_validation";
  if (fellBack || quota.verdict === "partial" || quota.verdict === "no_group" || quota.verdict === "fail") return "ready_with_constraints";
  return region.deployment_health === "Yes" ? "ready" : "needs_validation";
}

function renderDeploymentReadinessSection(region, deployment) {
  const blockers = Array.isArray(deployment.blockers) ? deployment.blockers : [];
  const reasons = Array.isArray(deployment.reasons) ? deployment.reasons : [];
  const constraints = Array.isArray(deployment.constraints) ? deployment.constraints : [];
  const blockerGroups = new Map();
  for (const blocker of blockers) {
    const type = blocker && blocker.type ? blocker.type : "other";
    if (!blockerGroups.has(type)) blockerGroups.set(type, []);
    blockerGroups.get(type).push(blocker);
  }
  const groupMeta = {
    missing_service: { icon: "🚫", label: "Missing services" },
    sku_unavailable: { icon: "⛔", label: "Unavailable SKUs" },
    zone_gap: { icon: "⚠️", label: "Zone gaps" },
    quota_insufficient: { icon: "📊", label: "Quota issues" },
    no_access: { icon: "🔒", label: "Access issues" },
    other: { icon: "•", label: "Other checks" },
  };
  const renderList = (items) =>
    `<ul class="dd-readiness-list">${items.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;

  // Verdict description based on status
  const verdictDesc = {
    ready: "This region meets all deployment requirements.",
    ready_with_constraints: "This region can work but has caveats to address.",
    not_recommended: "This region has critical blockers preventing deployment.",
    needs_validation: "Automated checks could not fully validate this region.",
  };

  const conf = _confidenceBadge(deployment);
  const provenance = Array.isArray(deployment.provenance) ? deployment.provenance : [];
  const provHtml = provenance.length
    ? `<details class="dd-provenance"><summary>How do we know? (${provenance.length})</summary>
        <ul class="dd-prov-list">${provenance.map(p =>
          `<li><strong>${escapeHtml(p.signal || "")}</strong> — <span class="muted">${escapeHtml(p.source || "")}</span></li>`
        ).join("")}</ul></details>`
    : "";

  let html = `<div class="deployment-readiness">
      <div class="deployment-readiness-header">
        <span class="pill pill-lg ${deployment.cls}" title="${escapeHtml(deployment.title)}">${escapeHtml(deployment.text)}</span>
        <span class="conf-badge ${conf.cls}" title="${escapeHtml(conf.title)}">${escapeHtml(conf.text)}</span>
        <span class="dd-verdict-desc">${escapeHtml(verdictDesc[deployment.verdict] || "")}</span>
      </div>
      ${provHtml}`;

  // Show blockers FIRST for not_recommended / needs_validation
  if (blockerGroups.size > 0) {
    for (const [type, items] of blockerGroups.entries()) {
      const meta = groupMeta[type] || groupMeta.other;
      html += `<div class="dd-blocker-group ${items.some(item => item.severity === "critical") ? "is-critical" : "is-warning"}">
        <div class="dd-readiness-subtitle">${meta.icon} ${escapeHtml(meta.label)}</div>
        <ul class="dd-readiness-list">${items.map(item => `<li>${escapeHtml(item.message || "")}</li>`).join("")}</ul>
      </div>`;
    }
  }

  // Show constraints for ready_with_constraints
  if (constraints.length) {
    html += `<div class="dd-readiness-subtitle">⚙️ Constraints</div>${renderList(constraints)}`;
  }

  // Recommendations — actionable mitigations (ODCR, tickets, fallback, etc.)
  const recommendations = Array.isArray(region && region.deployment_verdict && region.deployment_verdict.recommendations)
    ? region.deployment_verdict.recommendations
    : [];
  if (recommendations.length) {
    const prioRank = { high: 0, medium: 1, low: 2 };
    const prioMeta = {
      high: { cls: "is-high", label: "High" },
      medium: { cls: "is-medium", label: "Medium" },
      low: { cls: "is-low", label: "Low" },
    };
    const sorted = recommendations.slice().sort(
      (a, b) => (prioRank[a && a.priority] ?? 1) - (prioRank[b && b.priority] ?? 1)
    );
    html += `<div class="dd-readiness-subtitle">💡 Recommendations</div>`;
    html += `<ul class="dd-recs-list">`;
    for (const rec of sorted) {
      if (!rec) continue;
      const prio = prioMeta[rec.priority] || prioMeta.medium;
      const links = [];
      if (rec.ticket_kind === "quota" || rec.ticket_kind === "technical") {
        links.push(
          `<a href="#" class="dd-rec-link" data-rec-ticket="${escapeHtml(rec.ticket_kind)}">Open a ticket →</a>`
        );
      }
      if (rec.doc_url) {
        links.push(
          `<a href="${escapeHtml(rec.doc_url)}" target="_blank" rel="noopener noreferrer" class="dd-rec-link">Learn more →</a>`
        );
      }
      html += `<li class="dd-rec ${prio.cls}">
        <div class="dd-rec-head">
          <span class="dd-rec-prio">${escapeHtml(prio.label)}</span>
          <span class="dd-rec-title">${escapeHtml(rec.title || "")}</span>
        </div>
        <div class="dd-rec-detail">${escapeHtml(rec.detail || "")}</div>
        ${links.length ? `<div class="dd-rec-links">${links.join("")}</div>` : ""}
      </li>`;
    }
    html += `</ul>`;
  }

  // Only show positive reasons for "ready" verdict
  if (deployment.verdict === "ready" && reasons.length) {
    html += `<div class="dd-readiness-subtitle">✓ Checks passed</div>${renderList(reasons.filter(r => !r.includes("baseline")))}`;
  }

  if (!blockerGroups.size && !constraints.length && !reasons.length) {
    html += `<div class="note">No deployment-readiness details were recorded for this snapshot.</div>`;
  }
  html += `</div>`;
  return html;
}

// ---------------------------------------------------------------- Tabs / views

// Region sub-views are grouped under the single "Regions" primary tab.
const REGION_SUBVIEWS = ["table", "map", "latency", "compare"];

function switchView(view) {
  // Legacy/direct calls to a sub-view name are routed into the Regions group.
  if (REGION_SUBVIEWS.includes(view)) {
    STATE.regionsSub = view;
    view = "regions";
  }
  STATE.view = view;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === view));

  // The Filters & Search rail only applies to region views (table/map/compare)
  // and the overview; hide it where it's meaningless (quota/tickets/settings).
  const filtersRail = document.getElementById("filters-rail");
  if (filtersRail) {
    const showFilters = (view === "regions" || view === "overview");
    filtersRail.classList.toggle("hidden", !showFilters);
  }

  const subbar = document.getElementById("region-subtabs");
  if (view === "regions") {
    const sub = REGION_SUBVIEWS.includes(STATE.regionsSub) ? STATE.regionsSub : "table";
    STATE.regionsSub = sub;
    if (subbar) subbar.classList.remove("hidden");
    document.querySelectorAll(".region-subtab").forEach(t =>
      t.classList.toggle("active", t.dataset.sub === sub));
    // Hide the non-region primary views; show only the active sub-view.
    for (const v of ["overview", "quota", "support", "settings"]) {
      const el = document.getElementById("view-" + v);
      if (el) el.classList.add("hidden");
    }
    for (const v of REGION_SUBVIEWS) {
      const el = document.getElementById("view-" + v);
      if (el) el.classList.toggle("hidden", v !== sub);
    }
    if (sub === "map") setTimeout(refreshMap, 100);
    if (sub === "latency") refreshLatencyChart();
    return;
  }

  if (subbar) subbar.classList.add("hidden");
  for (const v of ["overview", "table", "map", "latency", "compare", "quota", "support", "settings"]) {
    const el = document.getElementById("view-" + v);
    if (el) el.classList.toggle("hidden", v !== view);
  }
  if (view === "overview") setTimeout(() => {
    Object.values(STATE.overviewCharts).forEach(c => c && c.resize());
  }, 50);
  if (view === "quota") renderQuotaTab();
  if (view === "support") renderSupportTab();
  if (view === "settings") {
    switchSettingsTab(STATE.settingsTab || "owner");
  }
}

// Switch the active sub-view within the Regions tab.
function switchRegionsSub(sub) {
  if (!REGION_SUBVIEWS.includes(sub)) return;
  STATE.regionsSub = sub;
  switchView("regions");
}

// True when the Regions tab is active AND showing the given sub-view. After the
// Phase 3 consolidation STATE.view is always "regions" for map/latency/table/
// compare; the specific pane lives in STATE.regionsSub.
function _isRegionsSub(sub) {
  return STATE.view === "regions" && STATE.regionsSub === sub;
}

// ---------------------------------------------------------------- Shared quota helpers

function _formatQuotaNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

// ---------------------------------------------------------------- Quota per Region

// Severity ranking for sorting quota rows: short (BOM requirement not met)
// first, then zero/exhausted availability state, then unknown/missing, then ok.
// Within a tier we sort by family name so the user can scan alphabetically
// once the problem rows are surfaced at the top.
const _QUOTA_SEVERITY_ORDER = { short: -1, zero: 0, exhausted: 1, unknown: 2, ok: 3 };

function _quotaStatusLabel(status) {
  switch (status) {
    case "ok":        return { text: "OK",         cls: "pill-ok"    };
    case "exhausted": return { text: "At quota",   cls: "pill-warn"  };
    case "zero":      return { text: "Zero quota", cls: "pill-fail"  };
    default:          return { text: "Unknown",    cls: "pill-muted" };
  }
}

function _quotaSourceLabel(authority) {
  if (authority === "selfservice") return { text: "Subscription quota", cls: "pill-ok"    };
  if (authority === "global")      return { text: "Global SKU rules",   cls: "pill-muted" };
  return { text: "Unknown", cls: "pill-muted" };
}

function _sufficientLabel(suff) {
  switch (suff) {
    case "enough":  return { text: "Enough",  cls: "pill-ok",
                             title: "Subscription has enough headroom for the cores this BOM family needs. Family-level vCPU headroom only; per-size & zone availability not verified." };
    case "short":   return { text: "Short",   cls: "pill-fail",
                             title: "Subscription does not have enough headroom for this BOM family." };
    case "unknown": return { text: "Unknown", cls: "pill-muted",
                             title: "Subscription quota data is missing for this family in this region." };
    default:        return { text: "—",       cls: "pill-muted",
                             title: "No 'Required Cores' set in the BOM for this family." };
  }
}

// Returns the list of resolved BOM requirements with required_cores set.
// Each entry has primary_family / alt_family / required_cores.
function _getCoresRequirements(snap) {
  const meta = (snap && snap.meta) || {};
  const resolved = Array.isArray(meta.skus_resolved) ? meta.skus_resolved : [];
  const out = [];
  for (const r of resolved) {
    const cores = (r && r.required_cores != null) ? Number(r.required_cores) : null;
    if (!Number.isFinite(cores) || cores <= 0) continue;
    out.push({
      primary_family: (r.primary_family || "").toLowerCase(),
      primary_label:  r.primary_label || r.primary_family || "",
      alt_family:     (r.alt_family || "").toLowerCase() || null,
      alt_label:      r.alt_label || null,
      required_cores: cores,
    });
  }
  return out;
}

// Are there any cores requirements set at all in this snapshot?
function snapshotHasCoresRequirements(snap) {
  return _getCoresRequirements(snap).length > 0;
}

// Build a lookup of (region, familyIdLower) → headroom + per-row metadata.
// headroom is null when CurrentQuota is unknown.
function _buildRegionQuotaIndex(snap, regionShort) {
  const all = (snap && Array.isArray(snap.sku_records)) ? snap.sku_records : [];
  const key = (regionShort || "").toLowerCase();
  const idx = new Map();
  for (const r of all) {
    if ((r.region || "").toLowerCase() !== key) continue;
    const fam = (r.family || "").toLowerCase();
    if (!fam) continue;
    const prov = r.selfservice_provenance || null;
    const cq = prov ? prov.current_quota : null;
    const cu = prov ? prov.current_usage : null;
    const headroom = (cq != null) ? (cq - (cu != null ? cu : 0)) : null;
    idx.set(fam, {
      record: r,
      provenance: prov,
      current_quota: cq,
      current_usage: cu,
      headroom: headroom,
      authority: (prov && prov.restriction_authority) || r.restriction_authority || null,
      sub_restricted: !!r.sub_restricted,
    });
  }
  return idx;
}

// Compute the per-region quota verdict considering alt-family fallback.
// Returns { verdict, viaAlt, problems } where verdict ∈
// "pass" | "fail" | "unknown" | "none" | "no_sub" | "not_accessible".
//   - "none": no requirement in the BOM has required_cores set.
//   - "no_sub": per-sub overlay was not applied for this snapshot.
//   - "not_accessible": the availability feed says region isn't accessible to this sub.
//   - "pass": every requirement passes (primary OR alt has enough headroom).
//   - "fail": at least one requirement fails (neither primary nor alt fits).
//   - "unknown": at least one requirement is undecidable and no failures.
// viaAlt=true when verdict is "pass" but at least one requirement relied on
// the alt family because the primary didn't have enough (or was missing).
function getRegionQuotaVerdict(snap, regionShort) {
  const region = (snap && Array.isArray(snap.regions))
    ? snap.regions.find(r => (r.short || "").toLowerCase() === (regionShort || "").toLowerCase())
    : null;
  if (region && region.quota_status) {
    switch (region.quota_status) {
      case "sufficient":   return { verdict: "pass", viaAlt: false, problems: [] };
      case "partial":      return { verdict: "partial", viaAlt: false, problems: [] };
      case "insufficient": return { verdict: "fail", viaAlt: false, problems: [] };
      case "no_quota_group": return { verdict: "no_group", viaAlt: false, problems: [] };
      default: return { verdict: "unknown", viaAlt: false, problems: [] };
    }
  }
  const meta = (snap && snap.meta) || {};
  const ss = meta.selfservice_overlay || meta.selfservice || null;
  const reqs = _getCoresRequirements(snap);
  if (!reqs.length) return { verdict: "none", viaAlt: false, problems: [] };
  if (!ss || ss.applied !== true) {
    return { verdict: "no_sub", viaAlt: false, problems: [] };
  }
  const idx = _buildRegionQuotaIndex(snap, regionShort);

  // Region-wide "not_accessible" check: if the availability feed returned not_accessible
  // for any record in this region, none of the quota numbers are usable.
  for (const v of idx.values()) {
    if (v.provenance && v.provenance.status === "not_accessible") {
      return { verdict: "not_accessible", viaAlt: false, problems: [] };
    }
  }

  let anyFail = false;
  let anyUnknown = false;
  let viaAlt = false;
  const problems = [];
  for (const req of reqs) {
    const need = req.required_cores;
    const primary = idx.get(req.primary_family) || null;
    const alt = req.alt_family ? (idx.get(req.alt_family) || null) : null;
    const ph = primary ? primary.headroom : null;
    const ah = alt ? alt.headroom : null;
    const primaryFits = (ph != null) && (ph >= need);
    const altFits = (ah != null) && (ah >= need);
    const primaryKnown = (ph != null);
    const altKnown = (ah != null);
    if (primaryFits) continue; // requirement satisfied by primary
    if (altFits) { viaAlt = true; continue; } // requirement satisfied by alt
    // Neither fits with known data — is this a hard fail or unknown?
    const altUsable = !!req.alt_family;
    const undecidedPrimary = !primaryKnown;
    const undecidedAlt = altUsable && !altKnown;
    if (undecidedPrimary && undecidedAlt) {
      anyUnknown = true;
      problems.push({ req, reason: "unknown_both" });
    } else if (!altUsable && undecidedPrimary) {
      anyUnknown = true;
      problems.push({ req, reason: "unknown_primary_no_alt" });
    } else if (!altUsable && primaryKnown && !primaryFits) {
      anyFail = true;
      problems.push({ req, reason: "short_primary_no_alt",
                      primary_headroom: ph });
    } else if (altUsable && primaryKnown && altKnown && !primaryFits && !altFits) {
      anyFail = true;
      problems.push({ req, reason: "short_both",
                      primary_headroom: ph, alt_headroom: ah });
    } else {
      // One side known-short and the other unknown → conservative UNKNOWN.
      anyUnknown = true;
      problems.push({ req, reason: "mixed" });
    }
  }
  if (anyFail) return { verdict: "fail", viaAlt: viaAlt, problems: problems };
  if (anyUnknown) return { verdict: "unknown", viaAlt: viaAlt, problems: problems };
  return { verdict: "pass", viaAlt: viaAlt, problems: problems };
}

function getRegionQuotaVerdictForSubscription(snap, regionShort, subscriptionId) {
  if (!subscriptionId) return getRegionQuotaVerdict(snap, regionShort);
  const region = (snap && Array.isArray(snap.regions))
    ? snap.regions.find(r => (r.short || "").toLowerCase() === (regionShort || "").toLowerCase())
    : null;
  const summary = region && Array.isArray(region.quota_subscriptions)
    ? region.quota_subscriptions.find(item => item && item.subscription_id === subscriptionId)
    : null;
  if (!summary) return getRegionQuotaVerdict(snap, regionShort);
  switch (summary.status) {
    case "sufficient_sub":
    case "sufficient_group":
      return { verdict: "pass", viaAlt: false, problems: [] };
    case "partial":
      return { verdict: "partial", viaAlt: false, problems: [] };
    case "insufficient":
      return { verdict: "fail", viaAlt: false, problems: [] };
    case "no_quota_group":
      return { verdict: "no_group", viaAlt: false, problems: [] };
    case "no_access":
      return { verdict: "not_accessible", viaAlt: false, problems: [] };
    case "not_available":
    case "unknown":
    default:
      return { verdict: "unknown", viaAlt: false, problems: [] };
  }
}

function _regionQuotaVerdictLabel(v) {
  switch (v.verdict) {
    case "pass":           return { text: v.viaAlt ? "Pass ↳ via alt" : "Pass",
                                    cls: "pill-ok",
                                    title: v.viaAlt
                                      ? "All BOM families have enough subscription headroom — at least one relied on the alt fallback."
                                      : "All BOM families have enough subscription headroom (primary)." };
    case "partial":        return { text: "Partial", cls: "pill-warn",
                                    title: "Some BOM families have enough quota, but at least one family is still short." };
    case "fail":           return { text: "Insufficient", cls: "pill-fail",
                                    title: "At least one BOM family does not have enough subscription headroom (primary nor alt)." };
    case "unknown":        return { text: "Unknown",      cls: "pill-muted",
                                    title: "Subscription quota data is missing for one or more BOM families in this region." };
    case "not_accessible": return { text: "Not accessible", cls: "pill-fail",
                                    title: "Region is not accessible to this subscription." };
    case "no_sub":         return { text: "— (no sub)",   cls: "pill-muted",
                                    title: "No subscription was passed, so per-subscription quota was not retrieved." };
    case "no_group":       return { text: "No group",     cls: "pill-warn",
                                    title: "No Azure Quota Group was detected for this region's subscriptions." };
    case "none":
    default:               return { text: "—",            cls: "pill-muted",
                                    title: "No 'Required Cores' set in the BOM." };
  }
}

// Normalize all sku_records for a single region into rows suitable for the
// quota table (drilldown OR top-level tab). Returns:
//   { rows: [...], region_status: "ok"|"not_accessible"|"missing"|... }
// Each row has: family, vmfamily_raw, current_quota, current_usage,
// quota_status, authority, reason, sub_restricted, required_cores,
// headroom, sufficient, severity_rank.
function getQuotaRowsForRegion(snap, regionShort) {
  const all = (snap && Array.isArray(snap.sku_records)) ? snap.sku_records : [];
  const key = (regionShort || "").toLowerCase();
  const matches = all.filter(r => (r.region || "").toLowerCase() === key);

  let regionStatus = "missing";
  for (const r of matches) {
    const s = (r.selfservice_provenance && r.selfservice_provenance.status) || null;
    if (s === "not_accessible") { regionStatus = "not_accessible"; break; }
    if (s) regionStatus = s;
  }

  // Map family-id (lowercase) → required_cores for fast lookup. Iterating
  // the requirements list per row would be O(N×M); a map is plenty fast
  // for the small N we expect.
  const reqs = _getCoresRequirements(snap);
  const coresByFamily = new Map();
  for (const req of reqs) {
    coresByFamily.set(req.primary_family, req.required_cores);
    if (req.alt_family) coresByFamily.set(req.alt_family, req.required_cores);
  }

  const rows = [];
  const seenFamilies = new Set();
  for (const r of matches) {
    const fam = (r.family || "").toLowerCase();
    if (fam) seenFamilies.add(fam);
    const prov = r.selfservice_provenance || null;
    const cq   = prov ? prov.current_quota : null;
    const cu   = prov ? prov.current_usage : null;
    const qs   = prov ? prov.quota_status : null;
    const auth = (prov && prov.restriction_authority) || r.restriction_authority || null;
    const requiredCores = fam ? (coresByFamily.get(fam) || null) : null;
    const headroom = (cq != null) ? (cq - (cu != null ? cu : 0)) : null;
    let sufficient = "none";
    if (requiredCores != null) {
      if (headroom == null) sufficient = "unknown";
      else if (headroom >= requiredCores) sufficient = "enough";
      else sufficient = "short";
    }
    // Hide rows that have NO useful signal: no per-sub quota data AND not
    // a self-service authoritative row AND no BOM requirement.
    const hasQuotaData = cq != null || cu != null || (prov && prov.is_accessible != null);
    if (!hasQuotaData && auth !== "selfservice" && requiredCores == null) continue;
    let severity = _QUOTA_SEVERITY_ORDER[qs] != null
      ? _QUOTA_SEVERITY_ORDER[qs]
      : _QUOTA_SEVERITY_ORDER.unknown;
    if (sufficient === "short") severity = _QUOTA_SEVERITY_ORDER.short;
    rows.push({
      family: r.family || "(unknown)",
      vmfamily_raw: prov ? prov.vmfamily_raw : null,
      current_quota: cq,
      current_usage: cu,
      quota_status: qs,
      authority: auth,
      reason: prov ? prov.reason : null,
      sub_restricted: !!r.sub_restricted,
      required_cores: requiredCores,
      headroom: headroom,
      sufficient: sufficient,
      synthetic: false,
      severity_rank: severity,
    });
  }

  // Emit synthetic rows for any BOM requirement whose family isn't present
  // in this region's availability data. Without this, "I need 100 Dav6 cores
  // but the availability feed doesn't know about Dav6 here" would silently disappear
  // while the region pill says Pass-via-alt — confusing the user.
  // Only emit when the overlay actually returned SOMETHING for this region;
  // otherwise (overlay not applied, region call failed) we'd be inventing
  // rows that don't represent a real check.
  const meta = (snap && snap.meta) || {};
  const ss = meta.selfservice_overlay || meta.selfservice || null;
  const overlayApplied = !!(ss && ss.applied === true);
  const haveRegionData = rows.length > 0;
  if (overlayApplied && haveRegionData) {
    for (const req of reqs) {
      for (const which of ["primary", "alt"]) {
        const famId = which === "primary" ? req.primary_family : req.alt_family;
        if (!famId || seenFamilies.has(famId)) continue;
        seenFamilies.add(famId);
        rows.push({
          family: which === "primary"
            ? (req.primary_label || req.primary_family)
            : (req.alt_label || req.alt_family),
          vmfamily_raw: null,
          current_quota: null,
          current_usage: null,
          quota_status: null,
          authority: null,
          reason: which === "primary"
            ? "No SKU availability data returned for this family in this region."
            : "No SKU availability data returned for this alt family in this region.",
          sub_restricted: false,
          required_cores: req.required_cores,
          headroom: null,
          sufficient: "unknown",
          synthetic: true,
          severity_rank: _QUOTA_SEVERITY_ORDER.unknown,
        });
      }
    }
  }

  rows.sort((a, b) => {
    if (a.severity_rank !== b.severity_rank) return a.severity_rank - b.severity_rank;
    return (a.family || "").localeCompare(b.family || "");
  });

  return { rows: rows, region_status: regionStatus };
}

// Renders a <tbody> of quota rows. Returns the inner HTML string so callers
// can decide whether to wrap in a full table or inject into a partial.
function _renderQuotaRows(rows, opts) {
  if (!rows.length) return "";
  const compact = !!(opts && opts.compact);
  return rows.map(r => {
    const qs = _quotaStatusLabel(r.quota_status);
    const src = _quotaSourceLabel(r.authority);
    const suf = _sufficientLabel(r.sufficient);
    const req = (r.required_cores != null) ? String(r.required_cores) : "—";
    const cq = (r.current_quota != null) ? String(r.current_quota) : "—";
    const cu = (r.current_usage != null) ? String(r.current_usage) : "—";
    const hr = (r.headroom != null) ? String(r.headroom) : "—";
    let sufTitle = suf.title;
    if (r.sufficient === "short" && r.required_cores != null && r.headroom != null) {
      sufTitle = `${suf.title} Need ${r.required_cores}, have ${r.headroom} (short by ${r.required_cores - r.headroom}).`;
    }
    const raw = r.vmfamily_raw
      ? `<code title="VM family code">${escapeHtml(r.vmfamily_raw)}</code>`
      : `<span class="muted">—</span>`;
    const srcCellTitle = r.reason ? ` title="${escapeHtml(r.reason)}"` : "";
    const familyCell = r.synthetic
      ? `<strong>${escapeHtml(r.family)}</strong> <span class="muted" title="No SKU availability data for this family in this region — required by BOM but unmapped.">(no data)</span>`
      : `<strong>${escapeHtml(r.family)}</strong>`;
    if (compact) {
      // Drilldown layout: Family | Required | Quota | Usage | Headroom | Sufficient | Source
      return `<tr>
        <td>${familyCell}</td>
        <td class="num">${escapeHtml(req)}</td>
        <td class="num">${escapeHtml(cq)}</td>
        <td class="num">${escapeHtml(cu)}</td>
        <td class="num">${escapeHtml(hr)}</td>
        <td title="${escapeHtml(sufTitle)}"><span class="pill ${suf.cls}">${escapeHtml(suf.text)}</span></td>
        <td${srcCellTitle}><span class="pill ${src.cls}">${escapeHtml(src.text)}</span></td>
      </tr>`;
    }
    return `<tr>
      <td>${familyCell}</td>
      <td>${raw}</td>
      <td class="num">${escapeHtml(req)}</td>
      <td class="num">${escapeHtml(cq)}</td>
      <td class="num">${escapeHtml(cu)}</td>
      <td class="num">${escapeHtml(hr)}</td>
      <td><span class="pill ${qs.cls}">${escapeHtml(qs.text)}</span></td>
      <td title="${escapeHtml(sufTitle)}"><span class="pill ${suf.cls}">${escapeHtml(suf.text)}</span></td>
      <td${srcCellTitle}><span class="pill ${src.cls}">${escapeHtml(src.text)}</span></td>
    </tr>`;
  }).join("");
}

function _quotaNoDataCopy(snap, regionStatus) {
  const meta = (snap && snap.meta) || {};
  const ss = meta.selfservice_overlay || meta.selfservice || null;
  if (ss && ss.applied === false) {
    const reason = ss.reason ? ` (reason: ${ss.reason})` : "";
    return `Subscription quota data was not collected for this snapshot${reason}. ` +
           `New ARM-only runs capture availability and restriction data only.`;
  }
  if (regionStatus === "not_accessible") {
    return "This region is not accessible to the subscription. " +
           "No CurrentQuota data is reported when a region is inaccessible.";
  }
  if (regionStatus === "not_found" || regionStatus === "region_unmappable") {
    return "No per-subscription quota data was returned for this region " +
           `(status: ${regionStatus}). SKU availability restrictions still apply where shown.`;
  }
  if (regionStatus === "compute_failed" || regionStatus === "region_info_failed" ||
      regionStatus === "bad_request" || regionStatus === "bad_schema") {
    return "Per-subscription quota data could not be read for this region " +
           `(status: ${regionStatus}). Showing SKU availability restrictions where available.`;
  }
  if (regionStatus === "missing") {
    return "This analysis result does not include subscription quota data " +
           "for this region.";
  }
  return "No subscription quota records for this region.";
}

function _quotaGroupStatusLabel(status) {
  switch (status) {
    case "sufficient": return { text: "Sufficient", cls: "pill-ok" };
    case "sufficient_sub": return { text: "Sub quota OK", cls: "pill-ok" };
    case "sufficient_group": return { text: "Quota group OK", cls: "pill-ok" };
    case "insufficient": return { text: "Insufficient", cls: "pill-fail" };
    case "partial": return { text: "Partial", cls: "pill-warn" };
    case "no_quota_group": return { text: "No group", cls: "pill-warn" };
    case "not_available": return { text: "N/A", cls: "pill-muted" };
    case "no_access": return { text: "No access", cls: "pill-muted" };
    default: return { text: "Unknown", cls: "pill-muted" };
  }
}

function _quotaGroupUsageLabel(limit, usage, required, status) {
  const headroom = (limit != null && usage != null) ? (limit - usage) : null;
  if (status === "insufficient" || (headroom != null && headroom <= 0)) {
    return { text: "Insufficient", cls: "pill-fail" };
  }
  if (limit != null && usage != null && limit > 0 && (usage / limit) >= 0.8) {
    return { text: "Tight", cls: "pill-warn" };
  }
  if (required != null && headroom != null && headroom >= required) {
    return { text: "Sufficient", cls: "pill-ok" };
  }
  return _quotaGroupStatusLabel(status);
}

function _quotaRegionInfoForSubscription(subscriptionQuota, regionShort) {
  const regions = (subscriptionQuota && subscriptionQuota.regions) || {};
  const wanted = String(regionShort || "").toLowerCase();
  for (const [key, value] of Object.entries(regions)) {
    if (String(key || "").toLowerCase() === wanted) return value || {};
  }
  return {};
}

function _quotaFamilyRecord(source, familyId) {
  const families = source || {};
  const wanted = String(familyId || "").toLowerCase();
  for (const [key, value] of Object.entries(families)) {
    if (String(key || "").toLowerCase() === wanted) {
      return { family: key, value: value || {} };
    }
  }
  return null;
}

function _bestSubscriptionQuotaMatchClient(subscriptionQuota, regionShort, familyIds, requiredCores, subscriptionId) {
  const regionInfo = _quotaRegionInfoForSubscription(subscriptionQuota, regionShort);
  const families = regionInfo.families || {};
  const candidates = [];
  familyIds.forEach((familyId, order) => {
    const match = _quotaFamilyRecord(families, familyId);
    if (!match) return;
    const limit = Number(match.value.limit);
    const usage = Number(match.value.usage);
    const normalizedLimit = Number.isFinite(limit) ? limit : null;
    const normalizedUsage = Number.isFinite(usage) ? usage : null;
    const headroom = normalizedLimit != null ? (normalizedLimit - (normalizedUsage != null ? normalizedUsage : 0)) : null;
    candidates.push({
      subscription_id: subscriptionId,
      family: match.family || familyId,
      limit: normalizedLimit,
      usage: normalizedUsage,
      headroom,
      sufficient: headroom != null && headroom >= requiredCores,
      order,
    });
  });
  candidates.sort((a, b) => {
    if (a.sufficient !== b.sufficient) return a.sufficient ? -1 : 1;
    const ah = a.headroom != null ? a.headroom : -1e15;
    const bh = b.headroom != null ? b.headroom : -1e15;
    if (ah !== bh) return bh - ah;
    return a.order - b.order;
  });
  const best = candidates[0] || null;
  return {
    subscription_id: subscriptionId,
    status: best ? "ok" : ((regionInfo && regionInfo.status) || (subscriptionQuota && subscriptionQuota.status) || "unknown"),
    error: (regionInfo && regionInfo.error) || (subscriptionQuota && subscriptionQuota.error) || null,
    family: best ? best.family : (familyIds[0] || null),
    limit: best ? best.limit : null,
    usage: best ? best.usage : null,
    headroom: best ? best.headroom : null,
    sufficient: !!(best && best.sufficient),
    total_regional: (regionInfo && regionInfo.total_regional) || null,
  };
}

function _bestQuotaGroupMatchClient(quotaResult, regionShort, familyIds, shortfall, subscriptionId) {
  const groups = ((quotaResult && quotaResult.groups) || []).filter(group =>
    String(group && group.region || "").toLowerCase() === String(regionShort || "").toLowerCase()
  );
  const candidates = [];
  groups.forEach((group) => {
    familyIds.forEach((familyId, order) => {
      ((group && group.families) || []).forEach((family) => {
        if (String(family && family.family || "").toLowerCase() !== String(familyId || "").toLowerCase()) return;
        const limit = Number(family.limit);
        const usage = Number(family.usage);
        const normalizedLimit = Number.isFinite(limit) ? limit : null;
        const normalizedUsage = Number.isFinite(usage) ? usage : null;
        const headroom = (normalizedLimit != null && normalizedUsage != null) ? (normalizedLimit - normalizedUsage) : null;
        candidates.push({
          subscription_id: subscriptionId,
          group: group.name || null,
          family: family.family || familyId,
          limit: normalizedLimit,
          usage: normalizedUsage,
          headroom,
          sufficient: headroom != null && headroom >= shortfall,
          order,
        });
      });
    });
  });
  candidates.sort((a, b) => {
    if (a.sufficient !== b.sufficient) return a.sufficient ? -1 : 1;
    const ah = a.headroom != null ? a.headroom : -1e15;
    const bh = b.headroom != null ? b.headroom : -1e15;
    if (ah !== bh) return bh - ah;
    return a.order - b.order;
  });
  const best = candidates[0] || null;
  return {
    subscription_id: subscriptionId,
    status: (quotaResult && quotaResult.status) || "unknown",
    error: (quotaResult && quotaResult.error) || null,
    available: !!best,
    group: best ? best.group : null,
    family: best ? best.family : (familyIds[0] || null),
    limit: best ? best.limit : null,
    usage: best ? best.usage : null,
    headroom: best ? best.headroom : null,
    shortfall,
    sufficient: !!(best && best.sufficient),
  };
}

function _evaluateSubscriptionRequirementClient(regionShort, subscriptionId, subResult, required) {
  const requiredCores = Number(required && required.required_cores) || 0;
  const familyIds = [required && required.primary_family, required && required.alt_family].filter(Boolean);
  const tier1 = _bestSubscriptionQuotaMatchClient(
    subResult && subResult.subscription_quota,
    regionShort,
    familyIds,
    requiredCores,
    subscriptionId,
  );
  let shortfall = requiredCores;
  if (tier1.headroom != null) shortfall = Math.max(0, requiredCores - tier1.headroom);
  const tier2 = _bestQuotaGroupMatchClient(
    subResult && subResult.quota_groups,
    regionShort,
    familyIds,
    shortfall,
    subscriptionId,
  );
  let overallStatus = "unknown";
  if (tier1.sufficient) overallStatus = "sufficient_sub";
  else if (tier2.sufficient) overallStatus = "sufficient_group";
  else {
    const subRegion = _quotaRegionInfoForSubscription(subResult && subResult.subscription_quota, regionShort);
    const subKnown = tier1.headroom != null;
    const groupKnown = tier2.headroom != null;
    if ((subRegion.status && subRegion.status !== "ok") && !groupKnown) overallStatus = "unknown";
    else if (subKnown || groupKnown || tier2.status === "no_quota_group" || tier2.status === "not_available") overallStatus = "insufficient";
  }
  return {
    overall_status: overallStatus,
    tier1_sub_quota: tier1,
    tier2_quota_group: tier2,
  };
}

function _buildQuotaGroupRowsFromSnapshot(region, regionShort, reqs) {
  const tiered = region.quota_tiers || {};
  const families = tiered.families || {};
  return reqs.map((req) => {
    const key = (req.primary_family || "").toLowerCase();
    const entry = families[key] || {};
    const satisfiedBy = entry.satisfied_by || null;
    const statusKey = entry.overall_status === "sufficient"
      ? (satisfiedBy === "subscription" ? "sufficient_sub"
        : (satisfiedBy === "quota_group" ? "sufficient_group" : "sufficient"))
      : (entry.overall_status || "unknown");
    return {
      family: entry.family || req.primary_family || "—",
      family_label: entry.label || req.primary_label || req.primary_family || "—",
      alt_family: entry.alt_family || req.alt_family || null,
      alt_label: entry.alt_label || req.alt_label || null,
      required: entry.required_cores != null ? entry.required_cores : req.required_cores,
      subscription: entry.tier1_sub_quota || null,
      quota_group: entry.tier2_quota_group || null,
      cross_sub: Array.isArray(entry.tier3_cross_sub) ? entry.tier3_cross_sub : [],
      subscription_id: entry.tier1_sub_quota?.subscription_id || region.quota_tiers?.subscription_id || null,
      region_short: region.short || regionShort || "",
      deficit: _quotaRowDeficit(entry),
      status: statusKey,
      overall_status: entry.overall_status || "unknown",
      satisfied_by: satisfiedBy,
    };
  });
}

function buildQuotaGroupRowsForRegion(snap, regionShort, subscriptionId) {
  const region = (snap && Array.isArray(snap.regions))
    ? snap.regions.find(r => (r.short || "").toLowerCase() === (regionShort || "").toLowerCase())
    : null;
  if (!region) return { rows: [], summaries: [], region: null };
  const reqs = _getCoresRequirements(snap);
  const perSubResults = (snap && snap.per_sub_results) || {};
  const activeSubId = subscriptionId !== undefined ? subscriptionId : focusedSubscriptionId();
  const targetSubscriptionId = activeSubId || region.quota_tiers?.subscription_id || Object.keys(perSubResults)[0] || "";
  let rows = [];
  if (activeSubId == null || activeSubId === "") {
    rows = _buildQuotaGroupRowsFromSnapshot(region, regionShort, reqs);
  } else if (targetSubscriptionId && perSubResults[targetSubscriptionId]) {
    const targetResult = perSubResults[targetSubscriptionId] || {};
    rows = reqs.map((req) => {
      const targetEval = _evaluateSubscriptionRequirementClient(regionShort, targetSubscriptionId, targetResult, req);
      // Get alt family quota separately for display
      let altSubscription = null;
      if (req.alt_family) {
        altSubscription = _bestSubscriptionQuotaMatchClient(
          targetResult && targetResult.subscription_quota,
          regionShort,
          [req.alt_family],
          Number(req.required_cores) || 0,
          targetSubscriptionId,
        );
      }
      const shortfall = Number(targetEval.tier2_quota_group?.shortfall);
      const effectiveShortfall = Number.isFinite(shortfall) ? shortfall : (Number(req.required_cores) || 0);
      const familyIds = [req.primary_family, req.alt_family].filter(Boolean);
      const crossSub = Object.entries(perSubResults)
        .filter(([subId]) => subId && subId !== targetSubscriptionId)
        .map(([subId, subResult]) => {
          const donor = _bestSubscriptionQuotaMatchClient(
            subResult && subResult.subscription_quota,
            regionShort,
            familyIds,
            effectiveShortfall,
            subId,
          );
          if (donor.headroom == null || donor.headroom <= 0) return null;
          donor.sufficient = donor.headroom >= effectiveShortfall;
          return donor;
        })
        .filter(Boolean)
        .sort((a, b) => (Number(b.headroom) || 0) - (Number(a.headroom) || 0));
      let overallStatus = "unknown";
      let satisfiedBy = null;
      if (targetEval.overall_status === "sufficient_sub") {
        overallStatus = "sufficient";
        satisfiedBy = "subscription";
      } else if (targetEval.overall_status === "sufficient_group") {
        overallStatus = "sufficient";
        satisfiedBy = "quota_group";
      } else if (targetEval.overall_status === "insufficient") {
        overallStatus = "insufficient";
      }
      const entryForDeficit = {
        required_cores: req.required_cores,
        tier1_sub_quota: targetEval.tier1_sub_quota,
        tier2_quota_group: targetEval.tier2_quota_group,
      };
      return {
        family: req.primary_family || "—",
        family_label: req.primary_label || req.primary_family || "—",
        alt_family: req.alt_family || null,
        alt_label: req.alt_label || null,
        alt_subscription: altSubscription,
        required: req.required_cores,
        subscription: targetEval.tier1_sub_quota || null,
        quota_group: targetEval.tier2_quota_group || null,
        cross_sub: crossSub,
        subscription_id: targetSubscriptionId,
        region_short: region.short || regionShort || "",
        deficit: _quotaRowDeficit(entryForDeficit),
        status: overallStatus === "sufficient"
          ? (satisfiedBy === "subscription" ? "sufficient_sub"
            : (satisfiedBy === "quota_group" ? "sufficient_group" : "sufficient"))
          : overallStatus,
        overall_status: overallStatus,
        satisfied_by: satisfiedBy,
      };
    });
  } else {
    rows = _buildQuotaGroupRowsFromSnapshot(region, regionShort, reqs);
  }
  return {
    rows,
    summaries: Array.isArray(region.quota_subscriptions) ? region.quota_subscriptions : [],
    region,
    subscription_id: targetSubscriptionId || null,
  };
}

function _quotaRowDeficit(entry) {
  const required = Number(entry && entry.required_cores);
  if (!Number.isFinite(required) || required <= 0) return 0;
  const subHeadroom = Number(entry && entry.tier1_sub_quota && entry.tier1_sub_quota.headroom);
  const groupShortfall = Number(entry && entry.tier2_quota_group && entry.tier2_quota_group.shortfall);
  if (Number.isFinite(groupShortfall) && groupShortfall > 0) return groupShortfall;
  if (Number.isFinite(subHeadroom)) return Math.max(0, required - subHeadroom);
  return required;
}

function _currentQuotaRequestBomId() {
  return String(STATE.activeBomId || "").trim();
}

function _quotaRequestBomId(state) {
  return String((state && state.bomId) || _currentQuotaRequestBomId()).trim();
}

function _quotaRequestTargetsActiveBom(bomId) {
  return String(bomId || "").trim() === _currentQuotaRequestBomId();
}

function _quotaRequestKey(regionShort, family, bomId = _currentQuotaRequestBomId()) {
  return `${String(regionShort || "").toLowerCase()}::${String(family || "").toLowerCase()}::${String(bomId || "").toLowerCase()}`;
}

async function _saveQuotaRequestToDb(state) {
  const bomId = _quotaRequestBomId(state);
  if (!state || !state.regionShort || !state.family || !state.subscriptionId || !state.requestedAt || !bomId) return;
  const displayStatus = _quotaRequestDisplayStatus(state);
  try {
    await apiJson("/api/quota/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        region: state.regionShort,
        family: state.family,
        subscription_id: state.subscriptionId,
        bom_id: bomId,
        subscription_name: state.subscriptionName || "",
        requested_limit: state.requestedLimit || 0,
        status: displayStatus,
        message: state.message || "",
        request_id: state.requestId || "",
        requested_at: state.requestedAt,
        completed_at: state.completedAt || null,
        provisioning_state: (state.response && state.response.provisioning_state) || "",
      }),
    });
  } catch (e) {
    // Best-effort persistence
  }
}

async function _restoreQuotaRequestsFromDb() {
  const bomId = _currentQuotaRequestBomId();
  if (!bomId) {
    renderPendingQuotaPanel();
    return;
  }
  try {
    const params = new URLSearchParams({ bom_id: bomId });
    const data = await apiJson(`/api/quota/history?${params.toString()}`);
    const entries = data.entries || [];
    if (!entries.length) {
      renderPendingQuotaPanel();
      return;
    }

    entries.forEach((entry) => {
      const key = _quotaRequestKey(entry.region, entry.family, entry.bom_id);
      // Don't overwrite active in-memory states
      if (STATE.quotaRequests[key]) return;
      STATE.quotaRequests[key] = {
        regionShort: entry.region,
        regionName: entry.region,
        family: entry.family,
        familyLabel: entry.family,
        subscriptionId: entry.subscription_id,
        bomId: entry.bom_id || bomId,
        subscriptionName: entry.subscription_name || "",
        requestedLimit: entry.requested_limit,
        requestedAt: entry.requested_at,
        completedAt: entry.completed_at,
        requestId: entry.request_id || "",
        message: entry.message || "",
        status: entry.status === "pending" ? "success" : (entry.status === "failed" ? "error" : "success"),
        pollTimedOut: entry.status === "pending",
        response: {
          status: entry.status,
          provisioning_state: entry.provisioning_state || "",
        },
        restoredFromDb: true,
      };
    });

    // Re-check pending requests against the API
    entries.filter((e) => e.status === "pending").forEach((entry) => {
      startQuotaPolling(entry.region, entry.family, entry.subscription_id, entry.requested_limit, entry.bom_id || bomId);
    });

    renderPendingQuotaPanel();
  } catch (e) {
    // Best-effort restore
  }
}

function _getQuotaRequestState(regionShort, family, bomId = _currentQuotaRequestBomId()) {
  return STATE.quotaRequests[_quotaRequestKey(regionShort, family, bomId)] || null;
}

function _quotaRequestDisplayStatus(state) {
  if (!state) return "pending";
  if (state.status === "error") return "failed";
  if (state.response && state.response.status === "failed") return "failed";
  if (state.response && state.response.status === "approved") return "approved";
  return "pending";
}

function _quotaRequestShouldDisplay(state, now = Date.now()) {
  return !!(state && state.requestedAt && _quotaRequestTargetsActiveBom(_quotaRequestBomId(state)));
}

function _pruneQuotaRequestStates(now = Date.now()) {
  let changed = false;
  Object.entries(STATE.quotaRequests).forEach(([key, state]) => {
    if (!state) {
      delete STATE.quotaRequests[key];
      changed = true;
      return;
    }
    if (state.restoredFromDb) return;
    if (!state.requestedAt && !state.editing) {
      delete STATE.quotaRequests[key];
      changed = true;
    }
  });
  return changed;
}

function _ensureQuotaRequestPanelTicker() {
  if (_ensureQuotaRequestPanelTicker._started) return;
  _ensureQuotaRequestPanelTicker._started = true;
  setInterval(() => {
    const changed = _pruneQuotaRequestStates();
    if (changed || Object.keys(STATE.quotaRequests).length) renderPendingQuotaPanel();
  }, 1000);
}

function _pendingQuotaRequestCount() {
  return Object.values(STATE.quotaRequests).filter((state) => _quotaRequestShouldDisplay(state)
    && _quotaRequestDisplayStatus(state) === "pending").length;
}

function _updateQuotaTabBadge() {
  const badge = document.getElementById("quota-tab-badge");
  if (!badge) return;
  const count = _pendingQuotaRequestCount();
  badge.textContent = String(count);
  badge.classList.toggle("hidden", count === 0);
}

function _setQuotaRequestState(regionShort, family, next) {
  const key = _quotaRequestKey(regionShort, family, next && next.bomId);
  const row = _findQuotaRow(regionShort, family);
  const region = _findRegionByShort(regionShort);
  const prev = STATE.quotaRequests[key] || {};
  const merged = {
    ...prev,
    ...next,
    regionShort: regionShort || prev.regionShort || "",
    regionName: (region && region.name) || prev.regionName || regionShort || "",
    family: family || prev.family || "",
    familyLabel: (row && (row.family_label || row.family)) || prev.familyLabel || family || "",
    bomId: (next && next.bomId) || prev.bomId || _currentQuotaRequestBomId(),
    updatedAt: Date.now(),
  };
  if (!merged.subscriptionName && merged.subscriptionId) merged.subscriptionName = _subNameById(merged.subscriptionId);
  if (merged.status === "loading") {
    merged.requestedAt = Date.now();
    merged.completedAt = null;
    merged.pollTimedOut = false;
  }
  const displayStatus = _quotaRequestDisplayStatus(merged);
  if (displayStatus !== "pending" && !merged.completedAt) merged.completedAt = Date.now();
  if (displayStatus === "pending") merged.completedAt = null;
  STATE.quotaRequests[key] = merged;
  // Persist to SQLite (non-blocking) — skip editing/loading transient states
  if (merged.status !== "loading" && !merged.editing && merged.requestedAt) {
    _saveQuotaRequestToDb(merged);
  }
  renderPendingQuotaPanel();
}

function _formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function renderPendingQuotaPanel() {
  _ensureQuotaRequestPanelTicker();
  const panel = document.getElementById("pending-quota-panel");
  if (!panel) return;
  const now = Date.now();
  _pruneQuotaRequestStates(now);
  const items = Object.values(STATE.quotaRequests)
    .filter((state) => _quotaRequestShouldDisplay(state, now))
    .sort((a, b) => {
      const ap = _quotaRequestDisplayStatus(a) === "pending" ? 0 : 1;
      const bp = _quotaRequestDisplayStatus(b) === "pending" ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return (Number(b.requestedAt) || 0) - (Number(a.requestedAt) || 0);
    });
  _updateQuotaTabBadge();
  if (!items.length) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  const pendingCount = items.filter((state) => _quotaRequestDisplayStatus(state) === "pending").length;
  const bodyClass = STATE.pendingQuotaPanelCollapsed ? "pending-quota-panel__body hidden" : "pending-quota-panel__body";
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="pending-quota-panel__card">
    <button type="button" class="pending-quota-panel__toggle" data-pending-quota-toggle="1" aria-expanded="${STATE.pendingQuotaPanelCollapsed ? "false" : "true"}">
      <span>Quota Request History${items.length ? ` <span class="pending-quota-panel__count">${escapeHtml(String(items.length))}</span>` : ""}</span>
      <span class="pending-quota-panel__caret" aria-hidden="true">${STATE.pendingQuotaPanelCollapsed ? "▸" : "▾"}</span>
    </button>
    <div class="${bodyClass}">
      ${items.map((state) => {
    const status = _quotaRequestDisplayStatus(state);
    const statusIcon = status === "approved"
      ? "✓"
      : (status === "failed" ? "✕" : `<span class="quota-request-spinner" aria-hidden="true"></span>`);
    const statusText = status === "approved" ? "Approved" : (status === "failed" ? "Failed" : "Pending");
    const subName = state.subscriptionName || _subNameById(state.subscriptionId || "") || "—";
    const timeLabel = status === "pending" ? "Elapsed" : "Submitted";
    const timeValue = status === "pending"
      ? _formatElapsed(now - Number(state.requestedAt || now))
      : new Date(Number(state.requestedAt)).toLocaleString();
    const failMessage = (status === "failed" && state.message) ? state.message : "";
    return `<div class="pending-quota-item pending-quota-item--${escapeHtml(status)}">
          <div class="pending-quota-item__grid">
            <div><span class="pending-quota-item__label">Family</span><strong>${escapeHtml(state.familyLabel || state.family || "—")}</strong></div>
            <div><span class="pending-quota-item__label">Region</span><strong>${escapeHtml(state.regionName || state.regionShort || "—")}</strong></div>
            <div><span class="pending-quota-item__label">Subscription</span><strong>${escapeHtml(subName)}</strong></div>
            <div><span class="pending-quota-item__label">Requested limit</span><strong>${escapeHtml(_formatQuotaNumber(state.requestedLimit))}</strong></div>
            <div><span class="pending-quota-item__label">Status</span><strong class="pending-quota-item__status">${statusIcon} ${escapeHtml(statusText)}</strong></div>
            <div><span class="pending-quota-item__label">${escapeHtml(timeLabel)}</span><strong>${escapeHtml(timeValue)}</strong></div>
          </div>${failMessage ? `<div class="pending-quota-item__message">${escapeHtml(failMessage)}</div>` : ""}
        </div>`;
  }).join("")}
    </div>
  </div>`;
}

function _quotaPollerKey(regionShort, family, bomId = _currentQuotaRequestBomId()) {
  return `${String(regionShort || "").toLowerCase()}:${String(family || "").toLowerCase()}:${String(bomId || "").toLowerCase()}`;
}

function _roundUpToNearest(value, increment) {
  const n = Number(value);
  const step = Number(increment);
  if (!Number.isFinite(n) || !Number.isFinite(step) || step <= 0) return 0;
  return Math.ceil(n / step) * step;
}

function _quotaRequestLimitForRow(row) {
  const currentLimit = Number(row && row.subscription && row.subscription.limit);
  const required = Number(row && row.required);
  const deficit = Number(row && row.deficit);
  const hasDeficit = Number.isFinite(deficit) && deficit > 0;
  const base = (Number.isFinite(currentLimit) ? currentLimit : 0) + (hasDeficit ? deficit : 50);
  return Math.max(
    _roundUpToNearest(base, 50),
    Number.isFinite(currentLimit) ? currentLimit + 50 : 0,
    Number.isFinite(required) ? required : 0,
  );
}

function _findRegionByShort(regionShort) {
  return (STATE.snapshot && Array.isArray(STATE.snapshot.regions))
    ? STATE.snapshot.regions.find(r => (r.short || "").toLowerCase() === String(regionShort || "").toLowerCase())
    : null;
}

function _findQuotaRow(regionShort, family) {
  const result = buildQuotaGroupRowsForRegion(STATE.snapshot, regionShort);
  const familyLower = String(family || "").toLowerCase();
  // Direct match on primary family
  const direct = result.rows.find((row) => String(row.family || "").toLowerCase() === familyLower);
  if (direct) return direct;
  // Match on alt family — return a synthetic row with alt family as the primary
  const parentRow = result.rows.find((row) => String(row.alt_family || "").toLowerCase() === familyLower);
  if (parentRow) {
    return {
      ...parentRow,
      family: parentRow.alt_family,
      family_label: parentRow.alt_label || parentRow.alt_family,
      _is_alt: true,
    };
  }
  return null;
}

function _quotaSummarySource(row) {
  switch (row && row.satisfied_by) {
    case "subscription": return "subscription quota";
    case "quota_group": return "quota group";
    default: return "available quota";
  }
}

function _quotaSubscriptionSummary(info) {
  if (!info) return "N/A";
  if (info.limit != null && info.usage != null) {
    return `${_formatQuotaNumber(info.usage)} used / ${_formatQuotaNumber(info.limit)} limit (${_formatQuotaNumber(info.headroom)} free)`;
  }
  const pill = _quotaGroupStatusLabel(info.status || "unknown");
  return pill.text;
}

function _quotaGroupSummary(info) {
  if (!info || info.limit == null || info.usage == null) return "N/A";
  const prefix = info.group ? `${info.group} · ` : "";
  return `${prefix}${_formatQuotaNumber(info.usage)} used / ${_formatQuotaNumber(info.limit)} limit (${_formatQuotaNumber(info.headroom)} free)`;
}

function _quotaCrossSubSummary(row) {
  const donors = Array.isArray(row && row.cross_sub) ? row.cross_sub : [];
  if (!donors.length) return "N/A";
  const best = donors[0];
  return `${_formatQuotaNumber(best.headroom)} free across ${donors.length} sub${donors.length === 1 ? "" : "s"} (informational)`;
}

function _quotaRequestStatusText(state) {
  if (!state) return "";
  if (state.status === "success") {
    const subName = state.subscriptionName || "";
    const responseStatus = state.response && state.response.status;
    const pending = responseStatus === "pending";
    const failed = responseStatus === "failed";
    let text = failed ? "✗ Denied" : pending ? "⏳ Pending approval" : "✓ Approved";
    if (pending && state.pollTimedOut) text += " — check Azure portal";
    if (subName) text += ` · Sub: ${subName}`;
    if (state.requestId) text += ` · ID: ${state.requestId}`;
    return text;
  }
  if (state.status === "error") {
    return state.message || "Request failed";
  }
  return "Submitting request…";
}

function _renderQuotaRequestAction(row) {
  const state = _getQuotaRequestState(row.region_short, row.family);
  const canRequest = !!row.subscription_id;
  const busy = state && state.status === "loading";
  const isEditing = !!(state && state.editing && !busy);
  const approvalStatus = state && state.response && state.response.status;
  const statusCls = state && state.status === "error"
    ? "error"
    : (approvalStatus === "failed" ? "error" : approvalStatus === "approved" ? "success" : "pending");
  let buttonHtml = "";
  if (canRequest && isEditing) {
    const suggested = Number(state && state.requestedLimit) || _quotaRequestLimitForRow(row);
    buttonHtml = `<div class="quota-request-form">
      <label for="quota-limit-${escapeHtml(_quotaRequestKey(row.region_short, row.family))}">New limit (vCPU):</label>
      <input id="quota-limit-${escapeHtml(_quotaRequestKey(row.region_short, row.family))}" type="number" class="quota-limit-input" value="${escapeHtml(String(suggested))}" min="1" />
      <div class="quota-request-form-actions">
        <button class="btn-secondary quota-submit-btn" type="button"
          data-quota-submit="1"
          data-region="${escapeHtml(row.region_short || "")}"
          data-family="${escapeHtml(row.family || "")}">Submit</button>
        <button class="btn-link quota-cancel-btn" type="button"
          data-quota-cancel="1"
          data-region="${escapeHtml(row.region_short || "")}"
          data-family="${escapeHtml(row.family || "")}">Cancel</button>
      </div>
    </div>`;
  } else if (canRequest && (!state || state.status === "error" || (state.status === "success" && !busy))) {
    buttonHtml = `<button class="btn-secondary quota-request-btn" type="button"
      data-quota-request="1"
      data-region="${escapeHtml(row.region_short || "")}"
      data-family="${escapeHtml(row.family || "")}"
      ${busy ? "disabled" : ""}>${busy ? `<span class="quota-request-spinner" aria-hidden="true"></span>Requesting…` : `Request Increase (${escapeHtml(row.family_label || row.family || "")})`}</button>`;
  }
  const statusText = !canRequest ? "Subscription quota details unavailable for this family." : "";
  return `<div class="quota-card-action">${buttonHtml}${statusText ? `<div class="quota-request-status ${statusCls}">${escapeHtml(statusText)}</div>` : ""}</div>`;
}

function _quotaTicketRequestButton(row, useAlt, tag) {
  // All quota increases are created and tracked on the Tickets tab. This button
  // simply routes the user there with the ticket pre-filled (region, SKU family,
  // suggested new limit) — it does not perform any inline request itself.
  const family = useAlt ? row.alt_family : row.family;
  if (!family) return "";
  const label = useAlt ? (row.alt_label || row.alt_family) : (row.family_label || row.family);
  const suggested = _quotaRequestLimitForRow(row);
  const tagHtml = tag
    ? `<span class="sku-tag sku-tag--${tag === "Primary" ? "primary" : "fallback"}">${escapeHtml(tag)}</span> `
    : "";
  return `<button type="button" class="btn-secondary quota-ticket-btn"
    data-open-ticket="quota"
    data-region="${escapeHtml(row.region_short || "")}"
    data-family="${escapeHtml(family)}"
    data-limit="${escapeHtml(String(suggested || ""))}"
    title="Create and track a quota-increase support ticket for ${escapeHtml(label)}">${tagHtml}Request increase (${escapeHtml(label)}) ↗</button>`;
}

function _renderQuotaTicketActions(row) {
  const primary = _quotaTicketRequestButton(row, false, row.alt_family ? "Primary" : "");
  const fallback = row.alt_family ? _quotaTicketRequestButton(row, true, "Fallback") : "";
  if (!primary && !fallback) return `<span class="muted">—</span>`;
  return `<div class="quota-ticket-actions">${primary}${fallback}</div>`;
}

function _renderQuotaActionCell(row) {
  return _renderQuotaTicketActions(row);
}

function _registrationRequiredHtml(list, opts = {}) {
  // Dedupe by provider namespace — one card per provider. Each card resolves
  // its true state (registerable / registering / not available on this
  // subscription) via a live status check, so we never show a "Register"
  // action that can't actually work.
  const byProvider = new Map();
  for (const item of list) {
    const prov = item.provider || "";
    if (!byProvider.has(prov)) byProvider.set(prov, []);
    byProvider.get(prov).push(item.service);
  }
  let html = "";
  for (const [prov, services] of byProvider.entries()) {
    const svcList = services.map(escapeHtml).join(", ");
    const provLabel = prov ? escapeHtml(prov) : "provider";
    html +=
      `<div class="reg-provider-block" data-reg-provider="${escapeHtml(prov)}" ` +
      `style="font-size:12px;padding:6px 8px;margin:3px 0;background:rgba(244,167,38,0.10);` +
      `border-left:3px solid #f4a726;color:#f4a726;border-radius:4px">` +
      `<div><strong>${svcList}</strong></div>` +
      `<div style="font-size:11px;opacity:0.9;margin-top:2px">Provider <code>${provLabel}</code> ` +
      `isn't registered on this subscription, so its availability can't be confirmed.</div>` +
      `<div class="reg-status" data-reg-status style="margin-top:6px;font-size:11px;color:#f4a726">` +
      `Checking availability on this subscription…</div>` +
      `<div class="reg-cli" data-reg-cli style="margin-top:6px;font-size:11px;display:none">` +
      `<div style="opacity:0.9;margin-bottom:3px">Ask a subscription Owner/Contributor to run:</div>` +
      `<code class="reg-cli-text" data-reg-cli-text style="display:block;padding:6px 8px;background:rgba(0,0,0,0.35);` +
      `border-radius:4px;white-space:pre-wrap;word-break:break-all;cursor:pointer" ` +
      `title="Click to copy"></code></div>` +
      `<div class="reg-actions" data-reg-actions style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap"></div>` +
      `</div>`;
  }
  return html;
}

function _regBlockFor(el) {
  return el ? el.closest(".reg-provider-block") : null;
}

function _regSetStatus(block, text, color) {
  if (!block) return;
  const el = block.querySelector("[data-reg-status]");
  if (!el) return;
  el.textContent = text;
  el.style.color = color || "#f4a726";
  el.style.display = text ? "block" : "none";
}

function _regShowCli(block, cliCmd) {
  if (!block) return;
  const wrap = block.querySelector("[data-reg-cli]");
  const code = block.querySelector("[data-reg-cli-text]");
  if (!wrap || !code) return;
  code.textContent = cliCmd;
  wrap.style.display = "block";
}

function _regHideCli(block) {
  if (!block) return;
  const wrap = block.querySelector("[data-reg-cli]");
  if (wrap) wrap.style.display = "none";
}

const _REG_BTN_STYLE =
  "font-size:11px;padding:3px 10px;border:1px solid #f4a726;" +
  "background:rgba(244,167,38,0.15);color:#f4a726;border-radius:4px;cursor:pointer";

function _regRenderActions(block, provider, kinds) {
  if (!block) return;
  const host = block.querySelector("[data-reg-actions]");
  if (!host) return;
  const p = escapeHtml(provider);
  const labels = {
    register: `Register ${p}`,
    recheck: "Recheck",
    status: "Check status",
  };
  host.innerHTML = kinds.map(k =>
    `<button data-reg-action="${k}" data-provider="${p}" style="${_REG_BTN_STYLE}${k === "register" ? "" : ";opacity:0.85"}">${labels[k] || k}</button>`
  ).join("");
}

// Resolve one card's true state via a live status check and render the
// appropriate message + actions.
async function _resolveRegistrationCard(provider, block) {
  if (!provider || !block) return;
  const subscriptionId = focusedSubscriptionId() || "";
  const subName = _subNameById ? _subNameById(subscriptionId) : subscriptionId;
  if (!subscriptionId) {
    _regSetStatus(block, "⛔ No subscription selected — pick one to check availability.", "#e57373");
    _regRenderActions(block, provider, ["recheck"]);
    return;
  }
  _regSetStatus(block, `Checking availability on ${subName}…`, "#f4a726");
  _regRenderActions(block, provider, []);
  try {
    const params = new URLSearchParams({ subscription_id: subscriptionId, provider });
    const data = await apiJson(`/api/providers/status?${params.toString()}`);
    const state = String((data && data.registration_state) || "Unknown");
    if (data && data.registered) {
      _regHideCli(block);
      _regSetStatus(block,
        `✅ Registered on ${subName}. Re-run the analysis to see this service's regional availability.`,
        "#81c784");
      _regRenderActions(block, provider, ["recheck"]);
    } else if (data && data.absent) {
      // Namespace not known to this subscription — cannot be self-registered.
      _regHideCli(block);
      _regSetStatus(block,
        `⛔ Not available on ${subName}. Azure doesn't recognise “${provider}” on this ` +
        `subscription, so it can't be registered here — the service may not be offered ` +
        `for this subscription's tenant/offer. Try a different subscription, or open an ` +
        `Azure support request to have it enabled.`,
        "#e57373");
      _regRenderActions(block, provider, ["recheck"]);
    } else if (state.toLowerCase() === "registering") {
      _regHideCli(block);
      _regSetStatus(block, `⏳ Registering on ${subName}… click Recheck in a moment.`, "#f4a726");
      _regRenderActions(block, provider, ["recheck"]);
    } else {
      // Present but NotRegistered → genuinely registerable.
      _regHideCli(block);
      _regSetStatus(block,
        `Not registered yet on ${subName}. Registering is free and self-service.`,
        "#f4a726");
      _regRenderActions(block, provider, ["register", "recheck"]);
    }
  } catch (err) {
    _regSetStatus(block, `Couldn't check availability — ${(err && err.message) || String(err)}`, "#e57373");
    _regRenderActions(block, provider, ["recheck"]);
  }
}

// Auto-resolve any registration cards that haven't been resolved yet.
function _scanRegistrationCards(root) {
  const scope = root && root.querySelectorAll ? root : document;
  const blocks = scope.querySelectorAll(".reg-provider-block[data-reg-provider]:not([data-reg-resolved])");
  blocks.forEach(block => {
    const provider = block.getAttribute("data-reg-provider");
    if (!provider) return;
    block.setAttribute("data-reg-resolved", "1");
    _resolveRegistrationCard(provider, block);
  });
}

async function registerBomProvider(provider, block) {
  if (!provider) return;
  const subscriptionId = focusedSubscriptionId() || "";
  const subName = _subNameById ? _subNameById(subscriptionId) : subscriptionId;
  const cli = `az provider register --namespace ${provider}` +
    (subscriptionId ? ` --subscription ${subscriptionId}` : "");
  if (!subscriptionId) {
    _regSetStatus(block, "⛔ No subscription selected to register against.", "#e57373");
    return;
  }
  const regBtn = block && block.querySelector('[data-reg-action="register"]');
  if (regBtn) { regBtn.disabled = true; regBtn.textContent = "Registering…"; }
  _regSetStatus(block, `Submitting registration on ${subName}…`, "#f4a726");
  try {
    const data = await apiJson("/api/providers/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription_id: subscriptionId, provider }),
    });
    const state = (data && data.registration_state) || "Registering";
    _regHideCli(block);
    _regSetStatus(block,
      `✅ Registration started on ${subName} (state: ${state}). Azure usually finishes in 1–5 minutes — ` +
      `click Recheck, then re-run the analysis once it shows Registered.`,
      "#81c784");
    _regRenderActions(block, provider, ["recheck"]);
    showToast(`✓ Registering ${provider} on ${subName}…`, "success");
  } catch (err) {
    const body = err && err.body;
    const code = body && body.error;
    if (code === "not_available" || err.status === 404) {
      // Provider isn't available on this subscription — a register command
      // would just fail again, so don't offer one.
      _regHideCli(block);
      _regSetStatus(block,
        (body && body.message) ||
        `⛔ Not available on ${subName}. Azure doesn't recognise “${provider}” on this subscription.`,
        "#e57373");
      _regRenderActions(block, provider, ["recheck"]);
      showToast(`✗ ${provider} isn't available on ${subName}.`, "error");
      return;
    }
    const forbidden = code === "forbidden" || err.status === 403;
    const cliCmd = (body && body.cli_command) || cli;
    if (forbidden) {
      _regSetStatus(block,
        `⛔ Not registered. Your account can't register providers on ${subName} ` +
        `(missing “${provider}/register/action” permission).`,
        "#e57373");
      _regShowCli(block, cliCmd);
    } else {
      _regSetStatus(block, `⛔ Not registered — ${(err && err.message) || String(err)}`, "#e57373");
      _regShowCli(block, cliCmd);
    }
    _regRenderActions(block, provider, ["register", "recheck"]);
    showToast(`✗ Couldn't auto-register ${provider}. See the card for details.`, "error");
  }
}

function _handleRegisterProviderInteraction(ev) {
  const cliText = ev.target.closest("[data-reg-cli-text]");
  if (cliText && cliText.textContent) {
    ev.preventDefault();
    ev.stopPropagation();
    navigator.clipboard.writeText(cliText.textContent).then(
      () => showToast("Register command copied to clipboard.", "info"),
      () => {},
    );
    return;
  }
  const btn = ev.target.closest("[data-reg-action]");
  if (!btn) return;
  ev.preventDefault();
  ev.stopPropagation();
  const block = _regBlockFor(btn);
  const provider = btn.dataset.provider;
  if (btn.dataset.regAction === "register") {
    registerBomProvider(provider, block);
  } else {
    // recheck / status
    _resolveRegistrationCard(provider, block);
  }
}

function _handleQuotaRequestInteraction(ev) {
  const openTicketBtn = ev.target.closest("[data-open-ticket]");
  if (openTicketBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    _supportPrefill(openTicketBtn.dataset.openTicket || "quota", openTicketBtn.dataset.region, {
      family: openTicketBtn.dataset.family || "",
      newLimit: openTicketBtn.dataset.limit || null,
    });
    return;
  }
  const btn = ev.target.closest("[data-quota-request]");
  const submitBtn = ev.target.closest("[data-quota-submit]");
  const cancelBtn = ev.target.closest("[data-quota-cancel]");
  if (!btn && !submitBtn && !cancelBtn) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (btn) {
    const row = _findQuotaRow(btn.dataset.region, btn.dataset.family);
    _setQuotaRequestState(btn.dataset.region, btn.dataset.family, {
      editing: true,
      requestedLimit: _quotaRequestLimitForRow(row),
    });
    _refreshQuotaUi(btn.dataset.region);
    return;
  }
  if (cancelBtn) {
    _setQuotaRequestState(cancelBtn.dataset.region, cancelBtn.dataset.family, { editing: false });
    _refreshQuotaUi(cancelBtn.dataset.region);
    return;
  }
  const form = submitBtn.closest(".quota-request-form");
  const input = form && form.querySelector(".quota-limit-input");
  if (!input || !input.reportValidity()) return;
  _setQuotaRequestState(submitBtn.dataset.region, submitBtn.dataset.family, { editing: false });
  requestQuotaIncrease(submitBtn.dataset.region, submitBtn.dataset.family, Number(input.value));
}

function _renderDrilldownQuotaCard(row) {
  const title = escapeHtml(row.family_label || row.family || "—");
  const alt = row.alt_label ? `<span class="quota-card-alt">${escapeHtml(row.alt_label)} <span class="sku-tag sku-tag--fallback">Fallback</span></span>` : "";
  const need = escapeHtml(_formatQuotaNumber(row.required));
  const isSufficient = row.overall_status === "sufficient";
  const deficit = Math.max(0, Number(row.deficit) || 0);
  const statusText = isSufficient
    ? `✓ Sufficient via ${_quotaSummarySource(row)}`
    : row.overall_status === "insufficient"
    ? `⚠ Insufficient — need ${_formatQuotaNumber(deficit)} more vCPU`
    : "Quota details unavailable";
  const statusCls = isSufficient ? "ok" : (row.overall_status === "insufficient" ? "warn" : "muted");
  const cardCls = isSufficient ? " quota-card--sufficient" : "";

  return `<div class="quota-card${cardCls}">
    <div class="quota-card-header">
      <div>
        <div class="quota-card-title">${title} ${alt}</div>
      </div>
      <div class="quota-card-need">Need: ${need} vCPU</div>
    </div>
    <div class="quota-card-divider"></div>
    <div class="quota-card-lines">
      <div class="quota-card-line"><span class="quota-card-label">${escapeHtml(row.family_label || row.family)}: <span class="sku-tag sku-tag--primary">Primary</span></span><span class="quota-card-value">${escapeHtml(_quotaSubscriptionSummary(row.subscription))}</span></div>
      ${row.alt_family && row.alt_subscription ? `<div class="quota-card-line"><span class="quota-card-label">${escapeHtml(row.alt_label || row.alt_family)}: <span class="sku-tag sku-tag--fallback">Fallback</span></span><span class="quota-card-value">${escapeHtml(_quotaSubscriptionSummary(row.alt_subscription))}</span></div>` : ""}
      <div class="quota-card-line"><span class="quota-card-label">Quota Group:</span><span class="quota-card-value">${escapeHtml(_quotaGroupSummary(row.quota_group))}</span></div>
    </div>
    <div class="quota-card-divider"></div>
    <div class="quota-card-summary ${statusCls}">${escapeHtml(statusText)}</div>
    ${(isSufficient || row.overall_status === "insufficient") ? _renderQuotaTicketActions(row) : ""}
  </div>`;
}

function renderDrilldownQuotaSection(region, quotaResult, quotaPill) {
  const cards = quotaResult.rows.map((row) => _renderDrilldownQuotaCard(row)).join("");
  return `<div class="quota-cards" data-region="${escapeHtml(region.short || "")}">${cards}</div>`;
}

function _deriveQuotaRegionStatus(rows, fallbackStatus = "unknown") {
  if (!Array.isArray(rows) || !rows.length) return fallbackStatus;
  const statuses = rows.map((row) => row.overall_status || "unknown");
  if (statuses.every((status) => status === "sufficient")) return "sufficient";
  if (statuses.some((status) => status === "insufficient")) {
    return statuses.some((status) => status === "sufficient") ? "partial" : "insufficient";
  }
  return "unknown";
}

function _refreshQuotaUi(regionShort) {
  renderPendingQuotaPanel();
  if (STATE.view === "quota") {
    renderQuotaTab();
    return;
  }
  if (!STATE.activeDrilldownRegion) return;
  const current = _findRegionByShort(STATE.activeDrilldownRegion);
  if (current && (!regionShort || String(current.short || "").toLowerCase() === String(regionShort || "").toLowerCase())) {
    openDrilldown(current);
  }
}

function _applyApprovedQuotaLimit(regionShort, family, subscriptionId, requestedLimit, currentLimit) {
  const snap = STATE.snapshot;
  if (!snap || !snap.per_sub_results || !subscriptionId) return;
  const subResult = snap.per_sub_results[subscriptionId] || null;
  const regions = subResult && subResult.subscription_quota && subResult.subscription_quota.regions;
  const regionInfo = regions && regions[String(regionShort || "").toLowerCase()];
  const families = regionInfo && regionInfo.families;
  if (!families) return;

  const familyKey = Object.keys(families).find((key) => key.toLowerCase() === String(family || "").toLowerCase());
  if (!familyKey) return;
  const entry = families[familyKey];
  const nextLimit = Math.max(
    Number.isFinite(Number(currentLimit)) ? Number(currentLimit) : 0,
    Number.isFinite(Number(requestedLimit)) ? Number(requestedLimit) : 0
  );
  if (!Number.isFinite(nextLimit) || nextLimit <= 0) return;
  entry.limit = nextLimit;
  if (entry.usage != null) entry.headroom = nextLimit - Number(entry.usage);

  if (Array.isArray(snap.quota_remediation)) {
    snap.quota_remediation = snap.quota_remediation.filter((item) => !(
      String(item.region || "").toLowerCase() === String(regionShort || "").toLowerCase()
      && String(item.family || "").toLowerCase() === String(family || "").toLowerCase()
      && String(item.subscription_id || "").toLowerCase() === String(subscriptionId || "").toLowerCase()
      && nextLimit >= Number(item.new_limit_recommended || requestedLimit || 0)
    ));
  }

  const region = _findRegionByShort(regionShort);
  if (!region) return;
  const result = buildQuotaGroupRowsForRegion(snap, regionShort, subscriptionId);
  region.quota_status = _deriveQuotaRegionStatus(result.rows, region.quota_status || "unknown");
  if (region.quota_tiers) region.quota_tiers.status = region.quota_status;
  const regionFamilies = (region.quota_tiers && region.quota_tiers.families) || {};
  const tierKey = Object.keys(regionFamilies).find((key) => key.toLowerCase() === String(family || "").toLowerCase());
  const row = result.rows.find((item) => String(item.family || "").toLowerCase() === String(family || "").toLowerCase());
  if (tierKey && row) {
    regionFamilies[tierKey].tier1_sub_quota = row.subscription;
    regionFamilies[tierKey].tier2_quota_group = row.quota_group;
    regionFamilies[tierKey].tier3_cross_sub = row.cross_sub;
    regionFamilies[tierKey].overall_status = row.overall_status;
    regionFamilies[tierKey].satisfied_by = row.satisfied_by;
  }
}

function startQuotaPolling(regionShort, family, subscriptionId, requestedLimit, bomId = _currentQuotaRequestBomId()) {
  const key = _quotaPollerKey(regionShort, family, bomId);
  if (_quotaPollers.has(key)) return;

  let attempts = 0;
  const maxAttempts = 10;
  const interval = setInterval(async () => {
    attempts += 1;
    try {
      const params = new URLSearchParams({
        subscription_id: subscriptionId,
        region: regionShort,
        family,
        requested_limit: String(requestedLimit),
      });
      const resp = await apiJson(`/api/quota/request-status?${params.toString()}`);
      if (resp.status === "failed") {
        clearInterval(interval);
        _quotaPollers.delete(key);
        const state = _getQuotaRequestState(regionShort, family, bomId) || {};
        _setQuotaRequestState(regionShort, family, {
          ...state,
          bomId,
          status: "error",
          pollTimedOut: false,
          message: resp.message || "Quota increase request was denied by Azure.",
          response: { ...(state.response || {}), ...resp, status: "failed" },
        });
        if (_quotaRequestTargetsActiveBom(bomId)) {
          _refreshQuotaUi(regionShort);
          showToast(`✗ Quota increase denied for ${family}`, "error");
        }
      } else if (resp.status === "approved") {
        clearInterval(interval);
        _quotaPollers.delete(key);
        const state = _getQuotaRequestState(regionShort, family, bomId) || {};
        _setQuotaRequestState(regionShort, family, {
          ...state,
          bomId,
          status: "success",
          pollTimedOut: false,
          response: { ...(state.response || {}), ...resp, status: "approved" },
        });
        if (_quotaRequestTargetsActiveBom(bomId)) {
          _applyApprovedQuotaLimit(regionShort, family, subscriptionId, requestedLimit, resp.current_limit);
          _refreshQuotaUi(regionShort);
          showToast(`✓ Quota increase approved for ${family}!`, "success");
        }
      } else if (attempts >= maxAttempts) {
        clearInterval(interval);
        _quotaPollers.delete(key);
        const state = _getQuotaRequestState(regionShort, family, bomId) || {};
        _setQuotaRequestState(regionShort, family, {
          ...state,
          bomId,
          status: "success",
          pollTimedOut: true,
          response: { ...(state.response || {}), ...resp, status: "pending" },
        });
        if (_quotaRequestTargetsActiveBom(bomId)) {
          _refreshQuotaUi(regionShort);
          showToast(`Quota increase for ${family} still pending — check Azure portal`, "warning");
        }
      }
    } catch (e) {
      if (attempts >= maxAttempts) {
        clearInterval(interval);
        _quotaPollers.delete(key);
        const state = _getQuotaRequestState(regionShort, family, bomId) || {};
        _setQuotaRequestState(regionShort, family, {
          ...state,
          bomId,
          status: "success",
          pollTimedOut: true,
          response: { ...(state.response || {}), status: "pending" },
        });
        if (_quotaRequestTargetsActiveBom(bomId)) {
          _refreshQuotaUi(regionShort);
          showToast(`Quota increase for ${family} still pending — check Azure portal`, "warning");
        }
      }
    }
  }, 30000);
  _quotaPollers.set(key, interval);
}

async function requestQuotaIncrease(regionShort, family, requestedLimit) {
  const row = _findQuotaRow(regionShort, family);
  if (!row) return;

  const bomId = _currentQuotaRequestBomId();
  const subscriptionId = focusedSubscriptionId() || row.subscription_id || "";
  const subName = _subNameById(subscriptionId);
  const newLimit = Math.max(1, Number(requestedLimit || _quotaRequestLimitForRow(row)) || 0);
  if (!subscriptionId) {
    const message = "No subscription available for this request.";
    _setQuotaRequestState(regionShort, family, {
      bomId,
      status: "error",
      message,
      subscriptionId,
      subscriptionName: subName,
      requestedLimit: newLimit,
    });
    showToast(`✗ Quota increase failed for ${row.family}: ${message}`, "error");
    const region = _findRegionByShort(regionShort);
    const quotaTabActive = document.querySelector('.tab.active[data-view="quota"]');
    if (region && !quotaTabActive) openDrilldown(region);
    return;
  }

  _setQuotaRequestState(regionShort, family, {
    bomId,
    status: "loading",
    subscriptionId,
    subscriptionName: subName,
    requestedLimit: newLimit,
    response: null,
    message: "",
  });
  const region = _findRegionByShort(regionShort);
  const quotaTabActive = document.querySelector('.tab.active[data-view="quota"]');
  if (region && !quotaTabActive) openDrilldown(region);

  try {
    const data = await apiJson("/api/quota/request-increase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription_id: subscriptionId,
        region: regionShort,
        family: row.family,
        new_limit: newLimit,
      }),
    });
    const requestId = data.request_id
      || (data.response && (data.response.id || data.response.name))
      || "";
    const approvalStatus = String(data.status || "").toLowerCase() === "pending" ? "pending" : "approved";
    _setQuotaRequestState(regionShort, family, {
      bomId,
      status: "success",
      requestId,
      subscriptionName: subName,
      subscriptionId,
      requestedLimit: newLimit,
      pollTimedOut: false,
      response: { ...data, status: approvalStatus },
    });
    if (approvalStatus === "pending") {
      showToast(`⏳ Quota increase pending for ${row.family} on ${subName}`, "warning");
      startQuotaPolling(regionShort, row.family, subscriptionId, newLimit, bomId);
    } else {
      if (_quotaRequestTargetsActiveBom(bomId)) {
        showToast(`✓ Quota increase approved for ${row.family} on ${subName}`, "success");
        _applyApprovedQuotaLimit(regionShort, row.family, subscriptionId, newLimit, newLimit);
      }
    }
  } catch (err) {
    const message = (err && err.body && (err.body.message || err.body.error)) || err.message || "Quota request failed.";
    _setQuotaRequestState(regionShort, family, {
      bomId,
      status: "error",
      message,
      subscriptionId,
      subscriptionName: subName,
      requestedLimit: newLimit,
    });
    showToast(`✗ Quota increase failed for ${row.family}: ${message}`, "error");
  }

  if (_quotaRequestTargetsActiveBom(bomId)) {
    const refreshedRegion = _findRegionByShort(regionShort);
    if (refreshedRegion && STATE.view !== "quota") openDrilldown(refreshedRegion);
    renderTable();
    if (STATE.view === "quota") renderQuotaTab();
  }
}

function _renderSubscriptionQuotaCell(row) {
  const info = row.subscription || null;
  if (!info) return `<span class="muted">—</span>`;
  if (info.limit != null && info.usage != null) {
    return `${escapeHtml(_formatQuotaNumber(info.usage))}/${escapeHtml(_formatQuotaNumber(info.limit))} ` +
      `<span class="muted">(${escapeHtml(_formatQuotaNumber(info.headroom))} free)</span>`;
  }
  const pill = _quotaGroupStatusLabel(info.status || "unknown");
  return `<span class="pill ${pill.cls}">${escapeHtml(pill.text)}</span>`;
}

function _renderQuotaGroupCell(row) {
  const info = row.quota_group || null;
  if (!info) return `<span class="muted">—</span>`;
  if (info.limit != null && info.usage != null) {
    const name = info.group ? `<div>${escapeHtml(info.group)}</div>` : "";
    const shortfall = info.shortfall != null
      ? `<div class="muted">shortfall ${escapeHtml(_formatQuotaNumber(info.shortfall))}</div>` : "";
    return `${name}<div>${escapeHtml(_formatQuotaNumber(info.usage))}/${escapeHtml(_formatQuotaNumber(info.limit))} ` +
      `<span class="muted">(${escapeHtml(_formatQuotaNumber(info.headroom))} free)</span></div>${shortfall}`;
  }
  const pill = _quotaGroupStatusLabel(info.status || "unknown");
  return `<span class="pill ${pill.cls}">${escapeHtml(pill.text)}</span>`;
}

function _subNameById(subId) {
  const subs = window._loadedSubscriptions || [];
  const match = subs.find(s => s.id === subId);
  return match ? match.name : subId;
}

function _quotaRemediationPriority(priority) {
  switch (String(priority || "").toLowerCase()) {
    case "critical": return { text: "🔴 Critical", cls: "pill-fail" };
    default: return { text: "🟡 High", cls: "pill-warn" };
  }
}

async function _copyTextToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}

function _quotaRemediationGeneratedDate(snap) {
  const meta = (snap && snap.meta) || {};
  const iso = meta.compiled_at || meta.created_at || "";
  if (iso) {
    const dt = new Date(iso);
    if (!Number.isNaN(dt.getTime())) return dt.toISOString().slice(0, 10);
  }
  const viewed = viewedSnapshotTime();
  if (viewed != null) {
    const dt = new Date(viewed);
    if (!Number.isNaN(dt.getTime())) return dt.toISOString().slice(0, 10);
  }
  return new Date().toISOString().slice(0, 10);
}

function _buildQuotaRemediationCopyText(snap, items) {
  const meta = (snap && snap.meta) || {};
  const lines = [
    `Quota Increase Requests for BOM: ${bomDisplayName(meta)}`,
    `Generated: ${_quotaRemediationGeneratedDate(snap)}`,
    "",
  ];
  let lastRegion = "";
  let lastSubscription = "";
  for (const item of items) {
    const regionName = item.region_display || item.region || "Unknown region";
    const subId = item.subscription_id || "";
    const subName = _subNameById(subId);
    if (regionName !== lastRegion) {
      if (lastRegion) lines.push("");
      lines.push(`Region: ${regionName}`);
      lastRegion = regionName;
      lastSubscription = "";
    }
    if (subId !== lastSubscription) {
      lines.push(`  Subscription: ${subName}${subName && subName !== subId ? ` (${subId})` : ""}`);
      lastSubscription = subId;
    }
    const familyName = item.family_label || item.family || "Unknown family";
    lines.push(
      `  - ${familyName} vCPUs: current limit ${_formatQuotaNumber(item.current_limit)}, ` +
      `increase to ${_formatQuotaNumber(item.new_limit_recommended)} ` +
      `(need ${_formatQuotaNumber(item.increase_needed)} additional cores)`
    );
  }
  return lines.join("\n");
}

function renderQuotaRemediation() {
  const snap = STATE.snapshot || {};
  const section = document.getElementById("quota-remediation-section");
  const tbody = document.querySelector("#quota-remediation-table tbody");
  const copyBtn = document.getElementById("quota-remediation-copy");
  if (!section || !tbody || !copyBtn) return;

  const items = Array.isArray(snap.quota_remediation) ? snap.quota_remediation.slice() : [];
  if (!items.length) {
    section.classList.add("hidden");
    tbody.innerHTML = "";
    copyBtn.disabled = true;
    return;
  }

  section.classList.remove("hidden");
  copyBtn.disabled = false;
  tbody.innerHTML = items.map((item) => {
    const priority = _quotaRemediationPriority(item.priority);
    const subId = item.subscription_id || "";
    const subName = _subNameById(subId);
    const current = `${escapeHtml(_formatQuotaNumber(item.current_usage))}/${escapeHtml(_formatQuotaNumber(item.current_limit))}` +
      ` <span class="muted">(used/limit)</span>`;
    return `<tr>
      <td><span class="pill ${priority.cls}">${escapeHtml(priority.text)}</span></td>
      <td>${escapeHtml(item.region_display || item.region || "—")}</td>
      <td>${escapeHtml(subName)}${subName && subName !== subId ? `<div class="muted mono">${escapeHtml(subId)}</div>` : ""}</td>
      <td><strong>${escapeHtml(item.family_label || item.family || "—")}</strong><div class="muted mono">${escapeHtml(item.family || "—")}</div></td>
      <td>${current}</td>
      <td>${escapeHtml(_formatQuotaNumber(item.increase_needed))} <span class="muted">cores</span></td>
      <td>${escapeHtml(_formatQuotaNumber(item.new_limit_recommended))}</td>
    </tr>`;
  }).join("");

  if (!copyBtn._quotaRemediationBound) {
    copyBtn.addEventListener("click", async () => {
      const currentSnap = STATE.snapshot || {};
      const currentItems = Array.isArray(currentSnap.quota_remediation) ? currentSnap.quota_remediation.slice() : [];
      if (!currentItems.length) return;
      const original = copyBtn.textContent;
      try {
        await _copyTextToClipboard(_buildQuotaRemediationCopyText(currentSnap, currentItems));
        copyBtn.textContent = "Copied!";
      } catch (err) {
        console.error("Failed to copy quota remediation text", err);
        copyBtn.textContent = "Copy failed";
      }
      setTimeout(() => { copyBtn.textContent = original; }, 1800);
    });
    copyBtn._quotaRemediationBound = true;
  }
}

function _renderQuotaStatusCell(row) {
  const pill = _quotaGroupStatusLabel(row.status || "unknown");
  return `<span class="pill ${pill.cls}">${escapeHtml(pill.text)}</span>`;
}

function renderQuotaTab() {
  const snap = STATE.snapshot || {};
  const regions = Array.isArray(snap.regions) ? snap.regions.slice() : [];
  const container = document.getElementById("view-quota");
  const select = document.getElementById("quota-region-select");
  const tbody = document.querySelector("#quota-table tbody");
  const empty = document.getElementById("quota-empty");
  const status = document.getElementById("quota-region-status");
  if (!container || !select || !tbody) return;
  renderSubscriptionSwitcher();
  renderQuotaRemediation();
  if (!container._quotaRequestBound) {
    container.addEventListener("click", _handleQuotaRequestInteraction);
    container._quotaRequestBound = true;
  }

  // Populate region dropdown.
  regions.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  if (!regions.length) {
    select.innerHTML = "";
    tbody.innerHTML = "";
    if (empty) {
      empty.classList.remove("hidden");
      empty.textContent = "No regions in this analysis result.";
    }
    if (status) status.textContent = "";
    return;
  }

  const regionsWithData = new Set(
    regions.filter(r => {
      const region = (snap.regions || []).find(x => (x.short || "").toLowerCase() === (r.short || "").toLowerCase());
      return region && region.quota_tiers && Object.keys(region.quota_tiers.families || {}).length;
    }).map(r => (r.short || "").toLowerCase())
  );

  select.innerHTML = regions.map(r => {
    const hasMark = regionsWithData.has((r.short || "").toLowerCase()) ? "" : " (no quota data)";
    return `<option value="${escapeHtml(r.short)}">${escapeHtml(r.name)}${escapeHtml(hasMark)}</option>`;
  }).join("");

  // Pick which region to show: persisted choice if valid, else first region
  // with data, else first region.
  let saved = "";
  try { saved = localStorage.getItem("quotaRegion") || ""; } catch (e) {}
  const validShorts = new Set(regions.map(r => (r.short || "").toLowerCase()));
  let chosen = "";
  if (saved && validShorts.has(saved.toLowerCase())) chosen = saved;
  if (!chosen) {
    const firstWithData = regions.find(r => regionsWithData.has((r.short || "").toLowerCase()));
    chosen = (firstWithData && firstWithData.short) || regions[0].short;
  }
  select.value = chosen;
  // Bind change handler once.
  if (!select._quotaBound) {
    select.addEventListener("change", () => {
      try { localStorage.setItem("quotaRegion", select.value); } catch (e) {}
      _renderQuotaForSelectedRegion();
    });
    select._quotaBound = true;
  }

  _renderQuotaForSelectedRegion();
}

function _renderQuotaHierarchy(result) {
  const panel = document.getElementById("quota-hierarchy-panel");
  if (!panel) return;
  if (!result.rows.length) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }

  const subId = result.subscription_id || focusedSubscriptionId() || "";
  // Resolve a human-friendly subscription name with multiple fallbacks
  let subName = "Subscription";
  if (subId) {
    const resolved = _subNameById(subId);
    if (resolved && resolved !== subId) {
      subName = resolved;
    } else {
      // Try BOM metadata
      const meta = activeBomMeta();
      const metaName = meta && (meta.subscription_name || meta.customer_name || meta.tag);
      subName = metaName || focusedSubscriptionName() || subId;
    }
  }

  const isCollapsed = !!STATE.quotaHierarchyCollapsed;
  const bodyClass = isCollapsed ? "quota-hierarchy__body hidden" : "quota-hierarchy__body";

  const regionDisplay =
    (result.region && (result.region.name || result.region.display_name)) ||
    (result.rows[0].region_short || "");
  const regionShort = result.rows[0].region_short || "";

  // ── Block 1: existing quota on THIS subscription for the BOM ──────────────
  // Render one box per SKU family (primary + secondary/fallback shown separately
  // rather than combined) so the customer can see each SKU's headroom on its own.
  const skuBox = (label, tag, free, required) => {
    const meetsReq = free != null && free >= required;
    const short = required - (free || 0);
    const pill = meetsReq
      ? `<span class="qd-pill qd-pill--ok">Sufficient</span>`
      : `<span class="qd-pill qd-pill--fail">Short ${_formatQuotaNumber(Math.max(0, short))}</span>`;
    const tagHtml = tag
      ? ` <span class="sku-tag sku-tag--${tag === "Primary" ? "primary" : "fallback"}">${escapeHtml(tag)}</span>`
      : "";
    return `<div class="qd-fam">
      <div class="qd-fam-main">
        <span class="qd-fam-name">${escapeHtml(label)}${tagHtml}</span>
        ${pill}
      </div>
      <div class="qd-fam-meta">${free != null ? _formatQuotaNumber(free) : "?"} free of ${_formatQuotaNumber(required)} required</div>
    </div>`;
  };
  const famSummary = result.rows.map(row => {
    const required = Number(row.required) || 0;
    const hasAlt = !!row.alt_family;
    const primFree = row.subscription && row.subscription.headroom != null ? row.subscription.headroom : null;
    const boxes = [skuBox(row.family_label || row.family || "—", hasAlt ? "Primary" : "", primFree, required)];
    if (hasAlt) {
      const altFree = row.alt_subscription && row.alt_subscription.headroom != null ? row.alt_subscription.headroom : null;
      boxes.push(skuBox(row.alt_label || row.alt_family, "Fallback", altFree, required));
    }
    return boxes.join("");
  }).join("");

  const failingRows = result.rows.filter(r => r.overall_status === "insufficient");
  const hasShortfall = failingRows.length > 0;

  // ── Block 2: donor subscriptions (only meaningful when there's a shortfall) ─
  const snap = STATE.snapshot || {};
  const perSubResults = snap.per_sub_results || {};
  const bomSubIds = new Set(Object.keys(perSubResults).map(id => id.toLowerCase()));
  if (subId) bomSubIds.add(subId.toLowerCase());
  const bomMeta = activeBomMeta();
  for (const id of subscriptionList(bomMeta)) bomSubIds.add(id.toLowerCase());
  const allSubs = Array.isArray(window._loadedSubscriptions) ? window._loadedSubscriptions : [];
  const nonBomSubs = allSubs.filter(s => s && s.id && !bomSubIds.has(s.id.toLowerCase()));

  // case-insensitive family lookup within a donor's scanned families map
  const famInfo = (fams, famId) => {
    if (!fams || !famId) return null;
    if (fams[famId]) return fams[famId];
    const hit = Object.entries(fams).find(([k]) => k.toLowerCase() === famId.toLowerCase());
    return hit ? hit[1] : null;
  };

  // We only scan for the families of the FAILING rows — that's all a donor can help with.
  const neededFamilies = [];
  for (const row of failingRows) {
    if (row.family) neededFamilies.push(row.family);
    if (row.alt_family) neededFamilies.push(row.alt_family);
  }
  const neededLabels = [...new Set(neededFamilies.map(f => _donorFamilyLabel(f, result.rows)))].join(", ");

  const cacheKey = `${regionShort}::${subId}`;
  const cachedDonors = (STATE.donorQuotaCache || {})[cacheKey] || null;

  let donorHtml = "";
  if (!hasShortfall) {
    donorHtml = `<div class="qd-donor-note qd-donor-note--ok">✓ ${escapeHtml(subName)} already has enough quota for this BOM in ${escapeHtml(regionDisplay)} — no donor subscription needed.</div>`;
  } else if (nonBomSubs.length === 0) {
    donorHtml = `<div class="qd-donor-note">No other (non-BOM) subscriptions are available to pull quota from. Request an increase on this subscription instead.</div>`;
  } else if (cachedDonors && cachedDonors.status === "loaded") {
    const relevant = new Set();
    const primaryFams = new Set();
    const fallbackFams = new Set();
    failingRows.forEach(r => {
      if (r.family) { relevant.add(r.family.toLowerCase()); primaryFams.add(r.family.toLowerCase()); }
      if (r.alt_family) { relevant.add(r.alt_family.toLowerCase()); fallbackFams.add(r.alt_family.toLowerCase()); }
    });
    const famRole = (famLower) => primaryFams.has(famLower) ? "primary" : (fallbackFams.has(famLower) ? "fallback" : "");
    const evaluated = nonBomSubs.map(s => {
      const scan = cachedDonors.results[s.id] || null;
      if (!scan || scan.status !== "ok") return null;
      const fams = scan.families || {};
      let bestFree = 0;
      const chips = [];
      for (const [fam, info] of Object.entries(fams)) {
        if (!relevant.has(fam.toLowerCase())) continue;
        const fr = info && info.headroom != null ? info.headroom : 0;
        if (fr > 0) { bestFree = Math.max(bestFree, fr); chips.push({ label: _donorFamilyLabel(fam, result.rows), free: fr, role: famRole(fam.toLowerCase()) }); }
      }
      if (bestFree <= 0) return null;
      const coversAll = failingRows.every(r => {
        const cands = [r.family, r.alt_family].filter(Boolean);
        return cands.some(f => {
          const info = famInfo(fams, f);
          const fr = info && info.headroom != null ? info.headroom : 0;
          return fr >= (Number(r.deficit) || 0);
        });
      });
      chips.sort((a, b) => b.free - a.free);
      return { s, chips, bestFree, coversAll };
    }).filter(Boolean);

    evaluated.sort((a, b) => (Number(b.coversAll) - Number(a.coversAll)) || (b.bestFree - a.bestFree));

    if (!evaluated.length) {
      donorHtml = `<div class="qd-donor-note">Scanned ${nonBomSubs.length} subscription${nonBomSubs.length === 1 ? "" : "s"} — none currently have free quota for ${escapeHtml(neededLabels)}.</div>`;
    } else {
      const totalFree = evaluated.reduce((sum, e) => sum + (Number(e.bestFree) || 0), 0);
      const perSkuTotals = new Map();
      const perSkuRole = new Map();
      evaluated.forEach(e => (e.chips || []).forEach(c => {
        perSkuTotals.set(c.label, (perSkuTotals.get(c.label) || 0) + (Number(c.free) || 0));
        if (c.role && !perSkuRole.has(c.label)) perSkuRole.set(c.label, c.role);
      }));
      const skuTotalChips = [...perSkuTotals.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([label, free]) => `<span class="qd-chip qd-chip--total${perSkuRole.get(label) ? ` qd-chip--${perSkuRole.get(label)}` : ""}">${escapeHtml(label)} · ${_formatQuotaNumber(free)} free</span>`)
        .join("");
      const totalSummary = `<div class="qd-donor-total">
        <div class="qd-donor-total-head">Total free to pull across ${evaluated.length} donor subscription${evaluated.length === 1 ? "" : "s"}</div>
        <div class="qd-donor-total-chips">${skuTotalChips || `<span class="qd-chip qd-chip--total">${_formatQuotaNumber(totalFree)} vCPU</span>`}</div>
      </div>`;
      const cards = evaluated.map(({ s, chips, bestFree, coversAll }) => {
        const badge = coversAll
          ? `<span class="qd-donor-badge qd-donor-badge--ok">Can cover shortfall</span>`
          : "";
        const chipHtml = chips.map(c => `<span class="qd-chip${c.role ? ` qd-chip--${c.role}` : ""}">${escapeHtml(c.label)} · ${_formatQuotaNumber(c.free)} free</span>`).join("");
        return `<div class="qd-donor">
          <div class="qd-donor-head">
            <span class="qd-donor-name">${escapeHtml(s.name || s.id)}</span>
            ${badge}
          </div>
          <div class="qd-donor-chips">${chipHtml}</div>
        </div>`;
      }).join("");
      donorHtml = `${totalSummary}<div class="qd-donor-list">${cards}</div>
        <div class="qd-donor-foot">Quota is moved between subscriptions with an Azure <strong>quota group</strong>. Confirm the donor and BOM subscriptions share (or can join) the same quota group, then rebalance in the Azure portal.</div>`;
    }
  } else if (cachedDonors && cachedDonors.status === "scanning") {
    donorHtml = `<div class="qd-donor-note"><span class="quota-request-spinner" aria-hidden="true"></span> Scanning ${nonBomSubs.length} subscription${nonBomSubs.length === 1 ? "" : "s"} for available quota…</div>`;
  } else {
    _scanDonorSubscriptions(nonBomSubs, regionShort, neededFamilies, cacheKey);
    donorHtml = `<div class="qd-donor-note"><span class="quota-request-spinner" aria-hidden="true"></span> Scanning ${nonBomSubs.length} subscription${nonBomSubs.length === 1 ? "" : "s"} for available quota…</div>`;
  }

  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="quota-hierarchy__card">
    <button type="button" class="quota-hierarchy__toggle" data-quota-hierarchy-toggle="1" aria-expanded="${isCollapsed ? "false" : "true"}">
      <span>Quota &amp; donor options</span>
      <span aria-hidden="true">${isCollapsed ? "▸" : "▾"}</span>
    </button>
    <div class="${bodyClass}">
      <div class="qd-grid">
        <section class="qd-block">
          <div class="qd-block-title">Your subscription quota for this BOM</div>
          <div class="qd-block-sub">${escapeHtml(subName)} &middot; ${escapeHtml(regionDisplay)}</div>
          <div class="qd-fam-list">${famSummary}</div>
        </section>
        <section class="qd-block">
          <div class="qd-block-title">Pull quota from a donor subscription</div>
          <div class="qd-block-sub">Cover the shortfall by rebalancing quota from another subscription you own.</div>
          ${donorHtml}
        </section>
      </div>
    </div>
  </div>`;
}

function _donorFamilyLabel(familyId, rows) {
  const lower = (familyId || "").toLowerCase();
  for (const row of rows) {
    if ((row.family || "").toLowerCase() === lower) return row.family_label || row.family || familyId;
    if ((row.alt_family || "").toLowerCase() === lower) return row.alt_label || row.alt_family || familyId;
  }
  return familyId;
}

async function _scanDonorSubscriptions(nonBomSubs, regionShort, families, cacheKey) {
  if (!STATE.donorQuotaCache) STATE.donorQuotaCache = {};
  STATE.donorQuotaCache[cacheKey] = { status: "scanning", results: {} };
  try {
    const subIds = nonBomSubs.map(s => s.id).slice(0, 20);
    const resp = await apiFetch("/api/donor-quota-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription_ids: subIds,
        region: regionShort,
        families: [...new Set(families)],
      }),
    });
    if (!resp.ok) throw new Error(`Scan failed: ${resp.status}`);
    const data = await resp.json();
    STATE.donorQuotaCache[cacheKey] = { status: "loaded", results: data.results || {} };
  } catch (e) {
    console.warn("Donor quota scan failed:", e);
    STATE.donorQuotaCache[cacheKey] = { status: "loaded", results: {} };
  }
  // Re-render the hierarchy with the scan results
  if (STATE.view === "quota") _renderQuotaForSelectedRegion();
}

function _renderQuotaForSelectedRegion() {
  const snap = STATE.snapshot || {};
  const select = document.getElementById("quota-region-select");
  const tbody = document.querySelector("#quota-table tbody");
  const empty = document.getElementById("quota-empty");
  const status = document.getElementById("quota-region-status");
  if (!select || !tbody) return;
  const regionShort = select.value;
  const result = buildQuotaGroupRowsForRegion(snap, regionShort);

  if (status) {
    const satisfied = result.rows.filter(r => r.overall_status === "sufficient").length;
    const insufficient = result.rows.filter(r => r.overall_status === "insufficient").length;
    status.textContent = result.rows.length
      ? `${satisfied}/${result.rows.length} families satisfied${insufficient ? ` · ${insufficient} failing` : ""}`
      : "";
  }

  if (!result.rows.length) {
    tbody.innerHTML = "";
    if (empty) {
      empty.classList.remove("hidden");
      empty.textContent = "No quota details were captured for this region.";
    }
    _renderQuotaHierarchy(result);
    return;
  }
  if (empty) empty.classList.add("hidden");
  _renderQuotaHierarchy(result);
  tbody.innerHTML = result.rows.map((row) => {
    const altQuotaHtml = row.alt_subscription && row.alt_subscription.headroom != null
      ? `<div class="muted" style="margin-top:4px">${escapeHtml(_formatQuotaNumber(row.alt_subscription.usage))}/${escapeHtml(_formatQuotaNumber(row.alt_subscription.limit))} (${escapeHtml(_formatQuotaNumber(row.alt_subscription.headroom))} free)</div>`
      : "";
    return `<tr>
      <td><strong>${escapeHtml(row.family_label || row.family || "—")}</strong> <span class="sku-tag sku-tag--primary">Primary</span>${row.alt_label ? `<div class="muted">${escapeHtml(row.alt_label)} <span class="sku-tag sku-tag--fallback">Fallback</span></div>` : ""}</td>
      <td class="num">${escapeHtml(_formatQuotaNumber(row.required))}</td>
      <td>${_renderSubscriptionQuotaCell(row)}${altQuotaHtml}</td>
      <td>${_renderQuotaGroupCell(row)}</td>
      <td>${_renderQuotaStatusCell(row)}</td>
      <td class="quota-table-action">${_renderQuotaActionCell(row)}</td>
    </tr>`;
  }).join("");
}

// ---------------------------------------------------------------- Map

function refreshMap() {
  if (!_isRegionsSub("map") || !STATE.snapshot) return;
  if (!STATE.map) {
    STATE.map = L.map("map", { worldCopyJump: true }).setView([20, 10], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; OpenStreetMap',
      maxZoom: 8,
    }).addTo(STATE.map);
    _ensureLatencyPairControl();
  }
  if (STATE.mapLayer) {
    STATE.map.removeLayer(STATE.mapLayer);
  }
  // Markers get rebuilt on every refresh, so drop any stale latency selection
  // (its marker references would otherwise dangle).
  clearLatencyPair();
  STATE.mapMarkersByRegion = {};
  const layer = L.layerGroup();
  for (const r of STATE.filtered) {
    if (r.coords[0] == null) continue;
    const color = r.deployment_health === "Yes" ? "#107C10" : "#DA291C";
    const marker = L.circleMarker(r.coords, {
      radius: 8, color: "#fff", weight: 2, fillColor: color, fillOpacity: 0.9,
    });
    marker._regionName = r.name;
    marker._baseColor = color;
    STATE.mapMarkersByRegion[r.name] = marker;
    marker.bindPopup(`
      <div><strong>${escapeHtml(r.name)}</strong></div>
      <div style="font-size:11px;color:#6b7280">${escapeHtml(r.geo)}</div>
      <div style="margin:4px 0">Health: <strong>${r.deployment_health}</strong> &middot; ${escapeHtml(r.status)}</div>
      ${r.recommendation ? `<div style="font-size:12px">${escapeHtml(r.recommendation)}</div>` : ""}
      <div style="margin-top:6px"><a href="#" class="map-latency-link" data-region="${escapeHtml(r.name)}">Measure latency &harr;</a></div>
      <div style="margin-top:4px"><a href="#" class="map-details-link" data-region="${escapeHtml(r.name)}">Details &rarr;</a></div>
    `);
    marker.on("popupopen", function () {
      const el = marker.getPopup().getElement();
      const link = el.querySelector(".map-details-link");
      if (link) {
        link.addEventListener("click", function (e) {
          e.preventDefault();
          const regionName = this.getAttribute("data-region");
          const region = (STATE.snapshot.regions || []).find(x => x.name === regionName);
          if (region) openDrilldown(region);
        });
      }
      const latLink = el.querySelector(".map-latency-link");
      if (latLink) {
        latLink.addEventListener("click", function (e) {
          e.preventDefault();
          selectRegionForLatency(this.getAttribute("data-region"));
          marker.closePopup();
        });
      }
    });
    layer.addLayer(marker);
  }
  layer.addTo(STATE.map);
  STATE.mapLayer = layer;
}

// ── Map latency measurement (pick two regions → draw a line labelled with the
// published round-trip latency between them) ────────────────────────────────

function latencyBetween(nameA, nameB) {
  const matrix = (STATE.snapshot && STATE.snapshot.latency_matrix) || {};
  const map = buildDisplayToLatency();
  const a = map[String(nameA).toLowerCase()];
  const b = map[String(nameB).toLowerCase()];
  if (!a || !b) return null;
  let ms = matrix[a] && matrix[a][b];
  if (ms == null) ms = matrix[b] && matrix[b][a];
  return ms == null ? null : ms;
}

function _regionByName(name) {
  return (STATE.snapshot && STATE.snapshot.regions || []).find(x => x.name === name);
}

function _setMarkerSelected(name, selected) {
  const m = STATE.mapMarkersByRegion && STATE.mapMarkersByRegion[name];
  if (!m) return;
  m.setStyle(selected
    ? { color: "#0F6CBD", weight: 4, radius: 10, fillColor: m._baseColor }
    : { color: "#fff", weight: 2, radius: 8, fillColor: m._baseColor });
}

function selectRegionForLatency(name) {
  if (!name) return;
  STATE.latencyPair = STATE.latencyPair || { a: null, b: null };
  const p = STATE.latencyPair;
  if (!p.a) {
    p.a = name;
    _setMarkerSelected(name, true);
    _updateLatencyPairControl();
    return;
  }
  if (p.a === name) {
    // Re-clicking the source clears the whole selection.
    clearLatencyPair();
    return;
  }
  // A already chosen — this becomes B (or replaces a completed pair's B).
  if (p.b) _setMarkerSelected(p.b, false);
  p.b = name;
  _setMarkerSelected(name, true);
  _drawLatencyPair();
  _updateLatencyPairControl();
}

function _clearLatencyLine() {
  if (STATE.latencyLineLayer) {
    STATE.map.removeLayer(STATE.latencyLineLayer);
    STATE.latencyLineLayer = null;
  }
}

function clearLatencyPair() {
  const p = STATE.latencyPair;
  if (p) {
    if (p.a) _setMarkerSelected(p.a, false);
    if (p.b) _setMarkerSelected(p.b, false);
  }
  STATE.latencyPair = { a: null, b: null };
  _clearLatencyLine();
  _updateLatencyPairControl();
}

function _drawLatencyPair() {
  const p = STATE.latencyPair;
  if (!p || !p.a || !p.b) return;
  const ra = _regionByName(p.a);
  const rb = _regionByName(p.b);
  if (!ra || !rb || ra.coords[0] == null || rb.coords[0] == null) return;
  _clearLatencyLine();
  const ms = latencyBetween(p.a, p.b);
  const color = ms == null ? "#8A8886" : ms < 50 ? "#107C10" : ms < 120 ? "#D29200" : "#DA291C";
  const line = L.polyline([ra.coords, rb.coords], {
    color, weight: 3, opacity: 0.85,
    dashArray: ms == null ? "6 6" : null,
  });
  const label = ms == null
    ? "latency not published"
    : `${ms} ms round-trip`;
  const mid = [(ra.coords[0] + rb.coords[0]) / 2, (ra.coords[1] + rb.coords[1]) / 2];
  const badge = L.marker(mid, {
    interactive: false,
    icon: L.divIcon({
      className: "latency-pair-badge",
      html: `<span style="background:${color};color:#fff;padding:2px 8px;border-radius:10px;` +
            `font-size:11px;font-weight:600;white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,0.4)">` +
            `${escapeHtml(label)}</span>`,
      iconSize: null,
    }),
  });
  const grp = L.layerGroup([line, badge]);
  grp.addTo(STATE.map);
  STATE.latencyLineLayer = grp;
}

function _ensureLatencyPairControl() {
  if (STATE.latencyPairControl || !STATE.map) return;
  const ctl = L.control({ position: "topright" });
  ctl.onAdd = function () {
    const div = L.DomUtil.create("div", "latency-pair-control");
    div.style.cssText =
      "background:rgba(255,255,255,0.95);color:#201f1e;padding:8px 10px;border-radius:6px;" +
      "font-size:12px;box-shadow:0 1px 4px rgba(0,0,0,0.3);max-width:230px;line-height:1.35";
    L.DomEvent.disableClickPropagation(div);
    div.innerHTML = `<div id="latency-pair-body"></div>`;
    return div;
  };
  ctl.addTo(STATE.map);
  STATE.latencyPairControl = ctl;
  _updateLatencyPairControl();
}

function _updateLatencyPairControl() {
  const body = document.getElementById("latency-pair-body");
  if (!body) return;
  const p = STATE.latencyPair || { a: null, b: null };
  if (!p.a && !p.b) {
    body.innerHTML =
      `<strong>Measure latency</strong><br>` +
      `<span style="color:#605e5c">Click a region's <em>Measure latency &harr;</em> ` +
      `link, then pick a second region to draw the line.</span>`;
    return;
  }
  const ms = p.a && p.b ? latencyBetween(p.a, p.b) : null;
  const msHtml = p.a && p.b
    ? (ms == null
        ? `<span style="color:#a4262c">latency not published</span>`
        : `<strong>${ms} ms</strong> round-trip`)
    : `<span style="color:#605e5c">pick a second region…</span>`;
  body.innerHTML =
    `<div><strong>A:</strong> ${escapeHtml(p.a || "—")}</div>` +
    `<div><strong>B:</strong> ${escapeHtml(p.b || "—")}</div>` +
    `<div style="margin-top:3px">${msHtml}</div>` +
    `<div style="margin-top:6px"><a href="#" id="latency-pair-clear" style="color:#0F6CBD">Clear</a></div>`;
  const clear = document.getElementById("latency-pair-clear");
  if (clear) {
    clear.addEventListener("click", function (e) { e.preventDefault(); clearLatencyPair(); });
  }
}

window.openDrilldownByName = function (name) {
  const r = (STATE.snapshot.regions || []).find(x => x.name === name);
  if (r) openDrilldown(r);
};

// ---------------------------------------------------------------- Latency chart

function initSourceRegionDropdown() {
  const sel = document.getElementById("latency-source");
  sel.innerHTML = "";
  const regions = (STATE.snapshot.regions || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  for (const r of regions) {
    const opt = document.createElement("option");
    opt.value = r.name;
    opt.textContent = r.name + (r.deployment_health === "No" ? " (unhealthy)" : "");
    sel.appendChild(opt);
  }
  // Default the source region to the active BOM's saved preferred region
  // (stored as a short name) so the Latency tab and "Best regions" badges
  // measure from the origin the customer selected in the wizard. Only apply
  // when the user hasn't already picked a source this session.
  if (!sel.dataset.userPicked) {
    const meta = activeBomMeta();
    const pref = meta && meta.preferred_region ? String(meta.preferred_region).toLowerCase() : "";
    if (pref) {
      const match = regions.find(r => String(r.short || "").toLowerCase() === pref
        || String(r.name || "").toLowerCase() === pref);
      if (match) sel.value = match.name;
    }
  }
  sel.addEventListener("change", () => { sel.dataset.userPicked = "1"; }, { once: true });
}

function refreshLatencyChart() {
  if (!_isRegionsSub("latency") || !STATE.snapshot) return;
  const sel = document.getElementById("latency-source");
  if (!sel.value) return;

  const matrix = STATE.snapshot.latency_matrix || {};
  const DISPLAY_TO_LATENCY = buildDisplayToLatency();

  const srcLatencyName = DISPLAY_TO_LATENCY[sel.value.toLowerCase()];
  const info = document.getElementById("latency-info");

  if (!srcLatencyName || !matrix[srcLatencyName]) {
    info.textContent = "(MS does not publish latency data for this source region)";
    drawLatency([], []);
    return;
  }

  const healthy = (STATE.snapshot.regions || []).filter(r => r.deployment_health === "Yes").map(r => r.name);
  const data = [];
  for (const h of healthy) {
    if (h.toLowerCase() === sel.value.toLowerCase()) continue;
    const hL = DISPLAY_TO_LATENCY[h.toLowerCase()];
    if (!hL) continue;
    const ms = matrix[srcLatencyName][hL];
    if (ms != null) data.push({ region: h, ms });
  }
  data.sort((a, b) => a.ms - b.ms);
  info.textContent = `${data.length} healthy regions with latency data`;
  drawLatency(data.map(d => d.region), data.map(d => d.ms));
}

function drawLatency(labels, data) {
  const ctx = document.getElementById("latency-chart").getContext("2d");
  if (STATE.latencyChart) STATE.latencyChart.destroy();
  const colors = themeColors();
  STATE.latencyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Round-trip latency (ms)",
        data,
        backgroundColor: data.map(d => d < 50 ? "#107C10" : d < 120 ? "#FFB900" : "#DA291C"),
        borderColor: data.map(d => d < 50 ? "#107C10" : d < 120 ? "#D29200" : "#B71F14"),
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: {
          title: { display: true, text: "Round-trip latency (ms)", color: colors.text },
          ticks: { color: colors.muted },
          grid: { color: colors.grid },
        },
        y: {
          ticks: { color: colors.text },
          grid: { color: colors.grid },
        },
      },
    },
  });
}

// Build a {display_lower: latency_table_name} map by walking the published matrix
function buildDisplayToLatency() {
  const map = {};
  const matrix = STATE.snapshot.latency_matrix || {};
  // The matrix uses MS-standard names like "Germany West Central" - lowercase
  // them and map to themselves (case-corrected). Region display names match.
  for (const k of Object.keys(matrix)) {
    map[k.toLowerCase()] = k;
  }
  return map;
}

// ---------------------------------------------------------------- Compare

function initCompareDropdowns() {
  const regions = (STATE.snapshot.regions || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  document.querySelectorAll(".compare-select").forEach(sel => {
    const slot = sel.dataset.slot;
    sel.innerHTML = '<option value="">- pick a region -</option>';
    for (const r of regions) {
      const opt = document.createElement("option");
      opt.value = r.name;
      opt.textContent = r.name;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => {
      STATE.selectedSlots[slot] = sel.value;
      renderCompareSlot(slot);
    });
  });
}

function renderCompareSlot(slot) {
  const card = document.querySelectorAll(".compare-card")[slot];
  const content = card.querySelector(".compare-content");
  const name = STATE.selectedSlots[slot];
  if (!name) { content.innerHTML = ""; return; }
  const r = (STATE.snapshot.regions || []).find(x => x.name === name);
  if (!r) { content.innerHTML = ""; return; }

  // Determine primary labels from BOM
  const reqs = _getCoresRequirements(STATE.snapshot || {});
  const primaryLabels = new Set(reqs.map(rq => (rq.primary_label || "").toLowerCase()));

  // Zone & SKU availability grid
  let skuGrid = "";
  if (r.sku_zone_detail && Object.keys(r.sku_zone_detail).length) {
    skuGrid += `<div style="margin:10px 0"><strong>Zone &amp; SKU Availability:</strong></div><div class="kv" style="margin-bottom:10px">`;
    for (const [sku, zones] of Object.entries(r.sku_zone_detail)) {
      const cells = zones.map((z, i) =>
        `<span class="zone-cell ${z ? "green" : "red"}" style="margin-right:2px">${i + 1}</span>`
      ).join("");
      const isPrimary = primaryLabels.has(sku.toLowerCase());
      const tag = isPrimary
        ? ` <span class="sku-tag sku-tag--primary">Primary</span>`
        : ` <span class="sku-tag sku-tag--fallback">Fallback</span>`;
      const label = isPrimary ? `<strong>${escapeHtml(sku)}</strong>${tag}` : `${escapeHtml(sku)}${tag}`;
      skuGrid += `<div class="key">${label}</div><div>${cells}</div>`;
    }
    skuGrid += `</div>`;
  }

  // Build recommendation per BOM requirement
  let recLines = [];
  const hasMissingBom = r.missing_services && r.missing_services.length;
  if (hasMissingBom) {
    // If BOM services are missing, the region is not viable — recommend switching
    const svcNames = r.missing_services.map(ms => ms.service).join(", ");
    const bestAlt = (r.alt_regions && r.alt_regions.length) ? r.alt_regions[0] : null;
    const altNote = bestAlt ? `use ${bestAlt.region}${bestAlt.latency_ms != null ? ` (${bestAlt.latency_ms}ms)` : ""} instead` : "use an alternative region";
    recLines.push(`✗ Region cannot support BOM — missing: ${svcNames}`);
    recLines.push(`✗ Recommendation: ${altNote}`);
  } else {
    for (const rq of reqs) {
      const pLabel = rq.primary_label || "";
      const aLabel = rq.alt_label || "";
      const pZones = r.sku_zone_detail && r.sku_zone_detail[pLabel];
      const aZones = aLabel && r.sku_zone_detail && r.sku_zone_detail[aLabel];
      const pAll = pZones && pZones.every(Boolean);
      const aAll = aZones && aZones.every(Boolean);
      if (pAll) {
        recLines.push(`✓ Use Primary (${pLabel}) — available in all AZs`);
      } else if (aAll) {
        recLines.push(`✓ Use Fallback (${aLabel}) — available in all AZs`);
      } else {
        const bestAlt = (r.alt_regions && r.alt_regions.length) ? r.alt_regions[0] : null;
        const altNote = bestAlt ? `consider ${bestAlt.region}${bestAlt.latency_ms != null ? ` (${bestAlt.latency_ms}ms)` : ""}` : "consider alternative region";
        recLines.push(`✗ ${pLabel}${aLabel ? `/${aLabel}` : ""} blocked — open support ticket or ${altNote}`);
      }
    }
  }
  const recommendation = recLines.join("\n");

  // Blockers + fallbacks
  let issuesHtml = "";
  if (r.sku_blockers && r.sku_blockers.length) {
    issuesHtml += `<div style="margin-top:10px"><strong>Issues:</strong></div>`;
    for (const b of r.sku_blockers) {
      issuesHtml += `<div style="font-size:12px;padding:4px 8px;margin:2px 0;background:rgba(218,41,28,0.12);border-left:3px solid var(--brand-red-dark);color:var(--brand-red-dark);border-radius:4px">${escapeHtml(b)}</div>`;
    }
  }
  if (r.sku_fallbacks && r.sku_fallbacks.length) {
    issuesHtml += `<div style="margin-top:8px"><strong>Fallback Notes:</strong></div>`;
    for (const f of r.sku_fallbacks) {
      issuesHtml += `<div style="font-size:12px;padding:4px 8px;margin:2px 0;background:rgba(255,183,77,0.12);border-left:3px solid #f4a726;color:#f4a726;border-radius:4px">${escapeHtml(f)}</div>`;
    }
  }

  // Alternatives
  let altHtml = "";
  if (r.alt_regions && r.alt_regions.length) {
    altHtml += `<div style="margin-top:10px"><strong>Alternative regions based on health and latency:</strong></div>`;
    for (const a of r.alt_regions) {
      altHtml += `<div style="font-size:11px;padding-left:8px;color:var(--text-secondary)">${escapeHtml(a.region)}${a.latency_ms != null ? ` (${a.latency_ms}ms)` : ""}</div>`;
    }
  }

  // Missing BOM services
  let bomHtml = "";
  if (r.missing_services && r.missing_services.length) {
    bomHtml += `<div style="margin-top:10px"><strong>Missing BOM Services:</strong></div>`;
    for (const ms of r.missing_services) {
      bomHtml += `<div style="font-size:12px;padding:4px 8px;margin:2px 0;background:rgba(255,183,77,0.12);border-left:3px solid #f4a726;color:#f4a726;border-radius:4px">${escapeHtml(ms.service)}: ${escapeHtml(ms.detail || "not available")}</div>`;
    }
  }
  if (r.registration_required && r.registration_required.length) {
    bomHtml += `<div style="margin-top:10px"><strong>Registration Required:</strong></div>`;
    bomHtml += _registrationRequiredHtml(r.registration_required);
  }

  content.innerHTML = `
    <div class="name">${escapeHtml(r.name)}</div>
    <div style="font-size:12px;color:var(--text-secondary);margin-bottom:10px">${escapeHtml(r.geo)} &middot; ${escapeHtml(r.country || "")}</div>
    <div><strong>Status:</strong> <span class="status-pill ${statusClass(r.status)}">${escapeHtml(r.status)}</span></div>
    <div style="margin-top:8px"><strong>Recommendation:</strong></div>
    ${recLines.map(line => {
      const isBlocked = line.startsWith("✗");
      const color = isBlocked ? "var(--brand-red-dark)" : "#6fcf97";
      return `<div style="font-size:12px;padding:2px 8px;color:${color}">${escapeHtml(line)}</div>`;
    }).join("")}
    ${skuGrid}
    ${bomHtml}
    ${issuesHtml}
    ${altHtml}
  `;
  _scanRegistrationCards(content);
}

// ---------------------------------------------------------------- Export

function buildExportRows() {
  return STATE.filtered.map(r => {
    const noAz = _regionSupportsAz(r) === false;
    return {
    Region: r.name,
    Country: r.country,
    Geo: r.geo,
    "AZ Support": noAz ? "Regional only (no AZs)" : "AZ-enabled",
    "AZ1 Health": noAz ? "n/a" : r.zone_health[0],
    "AZ2 Health": noAz ? "n/a" : r.zone_health[1],
    "AZ3 Health": noAz ? "n/a" : r.zone_health[2],
    Status: r.status,
    "SKU Recommendation": r.recommendation,
    "Chosen SKUs": (r.chosen_skus || []).join("; "),
    "SKU Blockers": (r.sku_blockers || []).join(" | "),
    "Fallbacks Used": (r.sku_fallbacks || []).join(" | "),
    "Missing Services": (r.missing_services || []).map(m => `${m.service}: ${m.detail}`).join(" | "),
    "Zone Restrictions": (r.zone_restrictions || []).map((rest, i) => rest ? `AZ${i + 1}: ${rest}` : "").filter(Boolean).join(" | "),
    "Alternative Regions": (r.alt_regions || []).map(a => a.latency_ms != null ? `${a.region} (${a.latency_ms}ms)` : (a.source === "least_bad" && a.caveat ? `${a.region} (least-bad: ${a.caveat})` : a.region)).join("; "),
  };
  });
}

function exportCsv() {
  const rows = buildExportRows();
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const csv = [
    headers.join(","),
    ...rows.map(r => headers.map(h => csvEscape(r[h])).join(",")),
  ].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `capacity-${snapshotStamp()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function csvEscape(v) {
  if (v == null) return "";
  const s = String(v);
  if (s.includes('"') || s.includes(",") || s.includes("\n")) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function exportXlsx() {
  const rows = buildExportRows();
  if (!rows.length) return;
  const ws = XLSX.utils.json_to_sheet(rows);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Regions");
  XLSX.writeFile(wb, `capacity-${snapshotStamp()}.xlsx`);
}

function snapshotStamp() {
  const iso = (STATE.snapshot && STATE.snapshot.snapshot_iso) || new Date().toISOString();
  return iso.replace(/[:T]/g, "-").replace("Z", "");
}

// ---------------------------------------------------------------- Filters rail collapse + Clear filters

function applySidebarStateFromStorage() {
  let collapsed = false;
  try { collapsed = localStorage.getItem("filtersCollapsed") === "true"; } catch (e) {}
  // Move the marker class from <html> (set inline pre-paint) to .layout
  document.documentElement.classList.remove("filters-collapsed-init");
  document.getElementById("layout").classList.toggle("filters-collapsed", collapsed);
}

function toggleSidebar() {
  const layout = document.getElementById("layout");
  const collapsed = !layout.classList.contains("filters-collapsed");
  layout.classList.toggle("filters-collapsed", collapsed);
  try { localStorage.setItem("filtersCollapsed", collapsed ? "true" : "false"); } catch (e) {}

  // Leaflet needs a kick to recalc tiles after the grid resize animation
  setTimeout(() => {
    if (STATE.map && _isRegionsSub("map")) STATE.map.invalidateSize();
    if (STATE.latencyChart && _isRegionsSub("latency")) STATE.latencyChart.resize();
    Object.values(STATE.overviewCharts).forEach(c => c && c.resize());
  }, 280);
}

function applyBomnavStateFromStorage() {
  let collapsed = false;
  try { collapsed = localStorage.getItem("bomnavCollapsed") === "true"; } catch (e) {}
  // Move the marker class from <html> (set inline pre-paint) to .layout
  document.documentElement.classList.remove("bomnav-collapsed-init");
  document.getElementById("layout").classList.toggle("bomnav-collapsed", collapsed);
}

function toggleBomnav() {
  const layout = document.getElementById("layout");
  const collapsed = !layout.classList.contains("bomnav-collapsed");
  layout.classList.toggle("bomnav-collapsed", collapsed);
  try { localStorage.setItem("bomnavCollapsed", collapsed ? "true" : "false"); } catch (e) {}

  // Leaflet needs a kick to recalc tiles after the grid resize animation
  setTimeout(() => {
    if (STATE.map && _isRegionsSub("map")) STATE.map.invalidateSize();
    if (STATE.latencyChart && _isRegionsSub("latency")) STATE.latencyChart.resize();
    Object.values(STATE.overviewCharts).forEach(c => c && c.resize());
  }, 280);
}

function clearAllFilters() {
  document.getElementById("filter-search").value = "";
  // Subscription is deployment CONTEXT (drives quota verdicts), not a region
  // filter — keep the user's selection so "clear" doesn't silently change the
  // quota picture. It only resets the region-list filters below.
  document.querySelectorAll('[data-filter="verdict"]').forEach(el => { el.checked = true; });
  document.querySelectorAll('[data-filter="continent"]').forEach(el => { el.checked = true; });
  document.querySelectorAll('[data-filter="quota"]').forEach(el => { el.checked = true; });
  document.querySelectorAll('[data-filter="az"]').forEach(el => { el.checked = true; });
  document.getElementById("filter-missing-services").checked = false;
  document.getElementById("filter-v5-fallback").checked = false;
  document.getElementById("filter-restricted-only").checked = false;
  applyFilters();
}

// ---------------------------------------------------------------- Overview blade (donut KPIs)

function computeOverviewStats(regions) {
  let healthy = 0, unhealthy = 0;
  let fullBom = 0, missingBom = 0;
  let skuOk = 0, skuBlocked = 0;
  let primaryHealthy = 0, fallbackHealthy = 0;

  for (const r of regions) {
    const isHealthy = r.deployment_health === "Yes";
    if (isHealthy) healthy++; else unhealthy++;

    if ((r.missing_services || []).length === 0) fullBom++; else missingBom++;
    if ((r.sku_blockers || []).length === 0) skuOk++; else skuBlocked++;

    // primary_used / fell_back are the generic engine fields; fall back to
    // legacy v6_viable / sku_fallbacks for snapshots from older engines.
    const fellBack = (r.fell_back != null) ? r.fell_back : ((r.sku_fallbacks || []).length > 0);
    if (isHealthy && !fellBack) primaryHealthy++;
    else if (isHealthy) fallbackHealthy++;
  }

  return {
    total: regions.length,
    healthy, unhealthy,
    fullBom, missingBom,
    skuOk, skuBlocked,
    // legacy names retained so older code paths keep compiling
    v6Native: primaryHealthy,
    v5Fallback: fallbackHealthy,
    primaryHealthy, fallbackHealthy,
  };
}

function pct(part, total) {
  if (!total) return "0%";
  return Math.round((part / total) * 100) + "%";
}

function renderDonut(canvasId, labels, data, colors) {
  if (typeof Chart === "undefined") return;
  if (STATE.overviewCharts[canvasId]) {
    STATE.overviewCharts[canvasId].destroy();
  }
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const total = data.reduce((a, b) => a + b, 0);
  const allZero = total === 0;
  const finalData = allZero ? labels.map(() => 1) : data;
  const finalColors = allZero ? labels.map(() => "#E1DFDD") : colors;

  STATE.overviewCharts[canvasId] = new Chart(canvas.getContext("2d"), {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: finalData,
        backgroundColor: finalColors,
        borderColor: themeColors().surface,
        borderWidth: 2,
        hoverOffset: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "68%",
      animation: { duration: 350 },
      plugins: {
        legend: { display: false },
        tooltip: {
          enabled: !allZero,
          callbacks: {
            label: (ctx) => {
              const v = data[ctx.dataIndex] || 0;
              return `${ctx.label}: ${v} (${pct(v, total)})`;
            },
          },
        },
      },
    },
  });
}

function renderLegend(elId, items) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = "";
  const total = items.reduce((s, x) => s + x.value, 0);
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "legend-row";

    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = it.color;

    const lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.textContent = it.label;

    const val = document.createElement("span");
    val.className = "val";
    val.textContent = it.value;

    const p = document.createElement("span");
    p.className = "pct";
    p.textContent = pct(it.value, total);

    row.appendChild(sw);
    row.appendChild(lbl);
    row.appendChild(val);
    row.appendChild(p);
    el.appendChild(row);
  }
}

function renderOverviewCharts() {
  if (!STATE.snapshot) return;
  const blade = document.getElementById("overview-blade");
  if (!blade) return;
  const s = computeOverviewStats(STATE.filtered);
  blade.classList.toggle("empty", s.total === 0);

  // KPI center text
  document.getElementById("kpi-health-num").textContent = s.healthy;
  document.getElementById("kpi-health-lbl").textContent =
    s.total ? `${pct(s.healthy, s.total)} healthy` : "healthy";
  document.getElementById("kpi-bom-num").textContent = s.fullBom;
  document.getElementById("kpi-sku-num").textContent = s.skuOk;
  document.getElementById("kpi-mix-num").textContent = s.primaryHealthy;

  // Brand-aligned colors
  const COLORS = {
    green: "#107C10",
    red:   "#DA291C",
    blue:  "#0078D4",
    amber: "#FFB900",
    gray:  "#A19F9D",
  };

  renderDonut("chart-health", ["Healthy", "Unhealthy"],
    [s.healthy, s.unhealthy], [COLORS.green, COLORS.red]);
  renderLegend("legend-health", [
    { label: "Healthy",   value: s.healthy,   color: COLORS.green },
    { label: "Unhealthy", value: s.unhealthy, color: COLORS.red },
  ]);

  renderDonut("chart-bom", ["Full BOM", "Missing services"],
    [s.fullBom, s.missingBom], [COLORS.blue, COLORS.amber]);
  renderLegend("legend-bom", [
    { label: "Full BOM",         value: s.fullBom,    color: COLORS.blue  },
    { label: "Missing services", value: s.missingBom, color: COLORS.amber },
  ]);

  renderDonut("chart-sku", ["No blockers", "Has blockers"],
    [s.skuOk, s.skuBlocked], [COLORS.blue, COLORS.red]);
  renderLegend("legend-sku", [
    { label: "No blockers",  value: s.skuOk,      color: COLORS.blue },
    { label: "Has blockers", value: s.skuBlocked, color: COLORS.red  },
  ]);

  renderDonut("chart-mix", ["Primary used", "Fallback used", "Unhealthy"],
    [s.primaryHealthy, s.fallbackHealthy, s.unhealthy], [COLORS.green, COLORS.amber, COLORS.red]);
  renderLegend("legend-mix", [
    { label: "Primary used",  value: s.primaryHealthy,  color: COLORS.green },
    { label: "Fallback used", value: s.fallbackHealthy, color: COLORS.amber },
    { label: "Unhealthy",     value: s.unhealthy,       color: COLORS.red   },
  ]);
}

// ---------------------------------------------------------------- Subscription

async function loadSubscriptions() {
  // Populate the BOM navigator from subscription_metadata, then choose an
  // initial active BOM: the remembered one, else the first available.
  await refreshSubMetadataIndex();
  const remembered = (typeof localStorage !== "undefined" && localStorage.getItem("activeBomId")) || "";
  if (remembered && BOM_META.index[remembered]) {
    STATE.activeBomId = remembered;
  } else {
    const first = Object.keys(BOM_META.index)[0] || "";
    STATE.activeBomId = first;
  }
  STATE.activeSubscription = null;
  syncActiveSubscription();
  renderBomNav();
}

// ---- BOM metadata (in-app BOMs) --------------------------------------------
// BOM_META is a {bom_id: metadata} cache populated from /api/subscription_metadata
// so we can render the navigator + default the Refresh Analysis modal to a
// saved BOM. A BOM owns a subscription; multiple BOMs may share a subscription.
const BOM_META = { index: {}, loaded: false };

async function refreshSubMetadataIndex() {
  try {
    const r = await apiJson("/api/subscription_metadata");
    const idx = {};
    for (const item of (r.items || [])) {
      if (item && item.bom_id) idx[item.bom_id] = item;
    }
    BOM_META.index = idx;
    BOM_META.loaded = true;
  } catch (e) {
    console.warn("subscription_metadata index failed:", e);
    BOM_META.loaded = true;
  }
  return BOM_META.index;
}

function getBomMeta(bomId) {
  if (!bomId) return null;
  return BOM_META.index[bomId] || null;
}

// Availability target of a BOM. "zone_redundant" (default) means the workload
// needs Availability Zones, so a ZRS/HA restriction is a hard blocker.
// "regional" means single-zone tolerant, so ZRS restrictions are advisory only.
function _normalizeResilience(value) {
  return String(value || "").trim().toLowerCase() === "regional" ? "regional" : "zone_redundant";
}

function _bomResilience() {
  const meta = STATE.activeBomId ? getBomMeta(STATE.activeBomId) : null;
  return _normalizeResilience(meta && meta.resilience);
}

function _bomNeedsZoneRedundancy() {
  return _bomResilience() === "zone_redundant";
}

// Canonical BOM display name. Precedence: BOM Name (tag) → Customer name →
// Subscription ID. Used everywhere a BOM is identified so naming is consistent.
function bomDisplayName(meta) {
  if (!meta) return "(untitled BOM)";
  return meta.tag || meta.customer_name || meta.subscription_id || "(untitled BOM)";
}

// The next-most-specific identifier to show as a secondary line, skipping
// whatever is already used as the primary display name.
function bomSecondaryLabel(meta) {
  if (!meta) return "";
  const sub = primarySubscriptionId(meta);
  const shortSub = sub ? sub.slice(0, 8) + "…" + sub.slice(-4) : "";
  if (meta.tag) return meta.customer_name || shortSub;   // primary=tag
  if (meta.customer_name) return shortSub;               // primary=customer
  return "";                                             // primary=sub already
}

// ---------------------------------------------------------------- BOM modal

const BOM_EDIT = {
  catalog: null,           // [{name, provider, resource_type, zone_check, is_custom}, ...]
  catalogLoaded: false,
  regionsCatalog: null,    // [{name, display_name, has_az, is_custom}, ...]
  regionsLoaded: false,
  skuFamilies: null,       // ["standardDav6Family", ...] canonical, case-sensitive
  skuFamiliesSource: "",   // "arm+builtin" | "builtin"
  skuFamiliesLoaded: false,
  current: null,           // { subscription_id, tag, ... } from API, or null when new
  serviceTiers: {},        // { "Azure SQL Database": "business_critical", ... }
};

async function ensureBomCatalog(force = false) {
  if (BOM_EDIT.catalogLoaded && !force) return BOM_EDIT.catalog;
  try {
    const r = await apiJson("/api/bom/service_catalog");
    BOM_EDIT.catalog = r.services || [];
  } catch (e) {
    console.error("service catalog load failed:", e);
    BOM_EDIT.catalog = [];
  }
  BOM_EDIT.catalogLoaded = true;
  return BOM_EDIT.catalog;
}

async function ensureBomRegionsCatalog(force = false) {
  if (BOM_EDIT.regionsLoaded && !force) return BOM_EDIT.regionsCatalog;
  try {
    const r = await apiJson("/api/bom/region_catalog");
    BOM_EDIT.regionsCatalog = r.regions || [];
  } catch (e) {
    console.error("region catalog load failed:", e);
    BOM_EDIT.regionsCatalog = [];
  }
  BOM_EDIT.regionsLoaded = true;
  return BOM_EDIT.regionsCatalog;
}

// Canonical, case-sensitive VM SKU family IDs for the family pickers, loaded
// live from Azure (Microsoft.Compute/skus) via /api/bom/sku_families, with a
// bundled fallback so the dropdown is never empty.
async function ensureBomSkuFamilies(force = false) {
  if (BOM_EDIT.skuFamiliesLoaded && !force) return BOM_EDIT.skuFamilies;
  try {
    const r = await apiJson(`/api/bom/sku_families${force ? "?refresh=true" : ""}`);
    BOM_EDIT.skuFamilies = r.families || [];
    BOM_EDIT.skuFamiliesRich = r.families_rich || BOM_EDIT.skuFamilies.map(f => ({id: f, label: f}));
    BOM_EDIT.skuFamiliesSource = r.source || "";
  } catch (e) {
    console.error("sku family load failed:", e);
    BOM_EDIT.skuFamilies = BOM_EDIT.skuFamilies || [];
    BOM_EDIT.skuFamiliesRich = BOM_EDIT.skuFamiliesRich || [];
  }
  BOM_EDIT.skuFamiliesLoaded = true;
  renderBomSkuFamilyOptions();
  return BOM_EDIT.skuFamilies;
}

// Fill the shared <datalist> the SKU rows reference. Any saved/typed values in
// existing rows are appended so a legacy or not-yet-listed family still shows
// in the dropdown. Shows friendly labels (e.g. "Dav6 Series") alongside IDs.
function renderBomSkuFamilyOptions() {
  const dl = document.getElementById("bom-sku-family-options");
  if (!dl) return;
  // Build a map of id -> label from the rich data
  const labelMap = new Map();
  (BOM_EDIT.skuFamiliesRich || []).forEach(f => labelMap.set(f.id, f.label));
  // Collect all known families (rich list + any typed in existing rows)
  const families = new Set(BOM_EDIT.skuFamilies || []);
  document.querySelectorAll('#bom-skus-tbody input[data-col="primary_family"], #bom-skus-tbody input[data-col="alt_family"]')
    .forEach(inp => { const v = (inp.value || "").trim(); if (v) families.add(v); });
  dl.innerHTML = Array.from(families)
    .sort((a, b) => {
      // Sort by friendly label for easier scanning
      const la = (labelMap.get(a) || a).toLowerCase();
      const lb = (labelMap.get(b) || b).toLowerCase();
      return la.localeCompare(lb);
    })
    .map(f => {
      const label = labelMap.get(f) || f;
      // The label attr shows in the dropdown suggestion list; value is what
      // gets inserted. Users see "Dav6 Series (standardDav6Family)" in the
      // suggestion but the input gets the canonical ID.
      return `<option value="${escapeHtml(f)}" label="${escapeHtml(label)}"></option>`;
    })
    .join("");
  const status = document.getElementById("bom-skus-families-status");
  if (status) {
    const n = (BOM_EDIT.skuFamilies || []).length;
    status.textContent = n
      ? (BOM_EDIT.skuFamiliesSource === "arm+builtin"
          ? `${n} families · live from Azure`
          : `${n} families · built-in list (click Refresh families for the live Azure set)`)
      : "";
  }
}

function setBomStatus(html, kind = "info") {
  const el = document.getElementById("bom-status");
  el.dataset.kind = kind;
  el.innerHTML = html;
}

async function openBomModal(bomId, opts = {}) {
  document.getElementById("bom-overlay").classList.remove("hidden");
  document.getElementById("bom-modal").classList.remove("hidden");
  BOM_WIZARD.editing = !(opts.create);
  bomWizardGoTo(1);
  const titleEl = document.getElementById("bom-modal-title");
  if (titleEl) titleEl.textContent = opts.create ? "Create BOM" : "Edit BOM";
  setBomStatus("Loading…");
  const subEl = document.getElementById("bom-sub");
  subEl.innerHTML = '<option disabled>Loading subscriptions…</option>';
  document.getElementById("bom-tag").value = "";
  document.getElementById("bom-customer").value = "";
  { const rEl = document.getElementById("bom-resilience"); if (rEl) rEl.value = "zone_redundant"; }
  { const pr = document.getElementById("bom-preferred-region"); if (pr) pr.value = ""; }
  document.getElementById("bom-services-filter").value = "";
  document.getElementById("bom-regions-search").value = "";
  document.getElementById("bom-regions-filter").value = "all";
  document.getElementById("bom-skus-tbody").innerHTML = "";
  BOM_EDIT.current = null;
  BOM_EDIT.serviceTiers = {};

  // Prefill the ticket owner independently of the catalog loads below: those
  // can reject in a signed-out/empty environment, and owner prefill must not
  // depend on them.
  ensureSupportSettings().then(_prefillBomOwnerFields).catch(() => {});

  // Load catalogs + the SKU family list in parallel — none blocks the others.
  await Promise.all([ensureBomCatalog(), ensureBomRegionsCatalog(), ensureBomSkuFamilies(), loadSubscriptionsDropdown()]);
  renderBomServiceList();
  renderBomRegionsList();

  if (!opts.create && bomId) {
    await loadBomFromApi(bomId);
  } else {
    addBomSkuRow();
    // Start a new BOM from the full default region scope rather than an
    // empty grid.
    setBomSelectedRegions((BOM_EDIT.regionsCatalog || []).map(r => r.name));
    updateBomServiceCount();
    updateBomRegionsCount();
    setBomStatus("Fill out the form and click <strong>Save BOM</strong>.");
  }
  // Make sure the datalist reflects any rows just rendered (saved/legacy values).
  renderBomSkuFamilyOptions();

  // Guided walkthrough: a section-by-section tutorial that explains what each
  // part of the wizard is for. Auto-runs only on the user's FIRST BOM (no BOMs
  // saved yet) and only until they've seen/skipped it once; opts.guide (the
  // "Guide me" button / Getting Started) always forces it. Runs after the
  // wizard is fully populated so every target exists.
  const firstBomAuto = opts.create && !_hasExistingBoms() && !_bomWizardGuideSeen();
  if (opts.guide || firstBomAuto) {
    _setBomWizardGuideSeen();
    setTimeout(() => startBomWizardCoachTour(), 300);
  }
}

const BOM_GUIDE_KEY = "bom_wizard_guide_seen";
function _hasExistingBoms() {
  try { return Object.keys((BOM_META && BOM_META.index) || {}).length > 0; } catch (_e) { return false; }
}
function _bomWizardGuideSeen() {
  try { return localStorage.getItem(BOM_GUIDE_KEY) === "1"; } catch (_e) { return false; }
}
function _setBomWizardGuideSeen() {
  try { localStorage.setItem(BOM_GUIDE_KEY, "1"); } catch (_e) {}
}

function closeBomModal() {
  document.getElementById("bom-overlay").classList.add("hidden");
  document.getElementById("bom-modal").classList.add("hidden");
}

// ---------------------------------------------------------------- BOM wizard
// The editor modal is a 3-step wizard (Basics → Services & Regions → SKUs).
// These helpers only toggle which step is visible and drive the footer nav;
// the underlying inputs, catalogs and save logic are unchanged.
const BOM_WIZARD = { step: 1, total: 3, editing: false };

function bomWizardGoTo(step) {
  step = Math.max(1, Math.min(BOM_WIZARD.total, step));
  BOM_WIZARD.step = step;
  document.querySelectorAll("#bom-modal .bom-wizard-step").forEach(el => {
    el.classList.toggle("is-current", el.getAttribute("data-wstep") === String(step));
  });
  document.querySelectorAll("#bom-wizard-nav .bom-wizard-tab").forEach(tab => {
    const n = parseInt(tab.getAttribute("data-wstep"), 10);
    tab.classList.toggle("is-current", n === step);
    tab.classList.toggle("is-done", n < step);
  });
  const back = document.getElementById("bom-wizard-back");
  const next = document.getElementById("bom-wizard-next");
  const save = document.getElementById("bom-save");
  if (back) back.hidden = step === 1;
  const last = step === BOM_WIZARD.total;
  // Keep Next visible on the final step but grayed out/disabled, so it's clear
  // there's nothing further to advance to (Save takes over there).
  if (next) {
    next.hidden = false;
    next.disabled = last;
  }
  // When editing an existing BOM, Save is always available (users often tweak a
  // single field). When creating, Save appears only on the final step.
  if (save) save.hidden = BOM_WIZARD.editing ? false : !last;
  if (step === 3) renderBomServiceTiers();
}

// Validate the given step before advancing. Only step 1 (Basics) has required
// fields: a BOM name and at least one subscription.
function bomWizardValidateStep(step) {
  if (step === 1) {
    const tag = (document.getElementById("bom-tag").value || "").trim();
    const subSel = document.getElementById("bom-sub");
    const anySub = subSel && Array.from(subSel.selectedOptions || []).some(o => o.value);
    if (!tag) {
      setBomStatus('Enter a <strong>BOM name</strong> to continue.', "error");
      document.getElementById("bom-tag").focus();
      return false;
    }
    if (!anySub) {
      setBomStatus('Select at least one <strong>subscription</strong> to continue.', "error");
      subSel && subSel.focus();
      return false;
    }
    setBomStatus("");
  }
  return true;
}

function bomWizardNext() {
  if (!bomWizardValidateStep(BOM_WIZARD.step)) return;
  bomWizardGoTo(BOM_WIZARD.step + 1);
}

function bomWizardBack() {
  bomWizardGoTo(BOM_WIZARD.step - 1);
}

// ---------------------------------------------------------------- BOM navigator
// File-explorer style list in the left sidebar. Selecting a BOM loads its
// latest EXISTING snapshot (no new run).

function bomNavItemsSorted() {
  const items = Object.values(BOM_META.index || {});
  items.sort((a, b) => {
    const ka = (a.tag || a.customer_name || primarySubscriptionId(a) || "").toLowerCase();
    const kb = (b.tag || b.customer_name || primarySubscriptionId(b) || "").toLowerCase();
    return ka.localeCompare(kb);
  });
  return items;
}

function renderBomNav() {
  const list = document.getElementById("bomnav-list");
  if (!list) return;
  list.innerHTML = "";
  const items = bomNavItemsSorted();
  const countEl = document.getElementById("bomnav-count");
  if (countEl) countEl.textContent = items.length ? String(items.length) : "";
  if (!items.length) {
    list.innerHTML = '<div class="bomnav-empty">No BOMs yet.<br>Click <strong>+ New</strong> to create one.</div>';
    renderBomPanel();
    return;
  }
  for (const item of items) list.appendChild(buildBomNavRow(item));
  const search = document.getElementById("bomnav-search");
  if (search) filterBomNav(search.value);
  renderBomPanel();
}

function buildBomNavRow(item) {
  const bomId = item.bom_id || "";
  const sub = primarySubscriptionId(item);
  const name = bomDisplayName(item);
  const secondary = escapeHtml(bomSecondaryLabel(item));
  const subSummary = summarizeSubscriptions(item);

  const row = document.createElement("div");
  row.className = "bomnav-row" + (bomId === STATE.activeBomId ? " active" : "");
  row.dataset.bom = bomId;
  row.dataset.search = `${item.tag || ""} ${item.customer_name || ""} ${subscriptionList(item).join(" ")}`.toLowerCase();
  row.title = `${name}${secondary ? " — " + bomSecondaryLabel(item) : ""}\n${subSummary}`;
  row.innerHTML = `
    <span class="bomnav-icon" aria-hidden="true">▦</span>
    <span class="bomnav-label">
      <span class="bomnav-tag">${escapeHtml(name)}</span>
      <span class="bomnav-secondary">${secondary}</span>
    </span>`;

  row.addEventListener("click", () => selectBom(bomId));
  return row;
}

function markActiveBomNav() {
  document.querySelectorAll("#bomnav-list .bomnav-row").forEach(r => {
    r.classList.toggle("active", r.dataset.bom === STATE.activeBomId);
  });
}

// Select (open) a BOM: make it active and load its latest existing snapshot
// into the dashboard — without triggering a new analysis run.
async function selectBom(bomId) {
  if (!bomId) return;
  STATE.activeBomId = bomId;
  STATE.activeSubscription = null;
  syncActiveSubscription();
  try { localStorage.setItem("activeBomId", bomId); } catch (e) {}
  markActiveBomNav();
  renderBomPanel();
  await loadSnapshotsList();
  const picker = document.getElementById("snapshot-picker");
  await loadSnapshot(picker ? (picker.value || null) : null);
  await _restoreQuotaRequestsFromDb();
}

function filterBomNav(query) {
  const q = (query || "").trim().toLowerCase();
  document.querySelectorAll("#bomnav-list .bomnav-row").forEach(row => {
    const hit = !q || (row.dataset.search || "").includes(q);
    row.classList.toggle("hidden", !hit);
  });
}

// Set the BOM active and open the Run modal (this one DOES start a run).
function runBomFromManager(bomId) {
  if (!bomId) return;
  STATE.activeBomId = bomId;
  STATE.activeSubscription = null;
  syncActiveSubscription();
  try { localStorage.setItem("activeBomId", bomId); } catch (e) {}
  markActiveBomNav();
  openRunModal();
}

async function deleteBomFromNav(bomId, label) {
  if (!confirm(`Delete the BOM "${label}"? This cannot be undone.`)) return;
  try {
    const r = await apiFetch(`/api/subscription_metadata/${encodeURIComponent(bomId)}`, { method: "DELETE" });
    if (r.status !== 204 && !r.ok) {
      const body = await r.json().catch(() => ({}));
      alert(`Delete failed: ${body.message || body.error || r.statusText}`);
      return;
    }
    if (STATE.activeBomId === bomId) STATE.activeBomId = "";
    await refreshSubMetadataIndex();
    if (!STATE.activeBomId) {
      STATE.activeBomId = Object.keys(BOM_META.index)[0] || "";
    }
    STATE.activeSubscription = null;
    syncActiveSubscription();
    renderBomNav();
    if (STATE.activeBomId) {
      await loadSnapshotsList();
      await loadSnapshot(null);
      await _restoreQuotaRequestsFromDb();
    }
  } catch (e) {
    alert(`Network error: ${e.message}`);
  }
}

function renderBomServiceList() {
  const list = document.getElementById("bom-services-list");
  list.innerHTML = "";
  const cat = BOM_EDIT.catalog || [];

  const ORDER = [
    "Compute", "Containers", "Web", "Databases", "Storage", "Networking",
    "AI + Machine Learning", "Analytics", "Integration", "Internet of Things",
    "Security & Identity", "Management & Governance", "Developer Tools",
    "Migration", "Mixed Reality", "Media & Other",
  ];
  const groups = new Map();
  for (const svc of cat) {
    const c = (svc.category || "Other").trim() || "Other";
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c).push(svc);
  }
  const cats = Array.from(groups.keys()).sort((a, b) => {
    // "Other" always sinks to the bottom (long tail of niche providers).
    if (a === "Other") return 1;
    if (b === "Other") return -1;
    const ia = ORDER.indexOf(a), ib = ORDER.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });

  for (const c of cats) {
    const items = groups.get(c);
    const group = document.createElement("div");
    group.className = "svc-group";
    group.dataset.cat = c.toLowerCase();

    const header = document.createElement("button");
    header.type = "button";
    header.className = "svc-cat-header";
    header.setAttribute("aria-expanded", "false");
    header.innerHTML =
      `<span class="svc-cat-chevron" aria-hidden="true">&#9656;</span>` +
      `<span class="svc-cat-name">${escapeHtml(c)}</span>` +
      `<span class="svc-cat-count">${items.length}</span>` +
      `<span class="svc-cat-selected" hidden></span>`;
    header.addEventListener("click", () => {
      const open = group.classList.toggle("open");
      header.setAttribute("aria-expanded", open ? "true" : "false");
    });

    const body = document.createElement("div");
    body.className = "svc-group-items";
    for (const svc of items) {
      const id = "bom-svc-" + svc.name.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
      const lbl = document.createElement("label");
      // Searchable across friendly name, provider namespace and category.
      lbl.dataset.name = `${svc.name} ${svc.provider || ""} ${c}`.toLowerCase();
      if (svc.provider) lbl.title = svc.provider;   // namespace on hover, not inline
      const zone = svc.zone_check
        ? '<span class="svc-zone" title="Live zone availability via Microsoft.Compute/skus">zones</span>'
        : "";
      const delBtn = svc.is_custom
        ? `<button type="button" class="bom-custom-del" data-del-svc="${escapeHtml(svc.name)}" title="Remove this custom service">&times;</button>`
        : "";
      lbl.innerHTML = `<input type="checkbox" id="${id}" value="${escapeHtml(svc.name)}" data-bom-svc /> <span class="svc-name">${escapeHtml(svc.name)} ${zone}</span>${delBtn}`;
      body.appendChild(lbl);
    }

    group.appendChild(header);
    group.appendChild(body);
    list.appendChild(group);
  }
  updateSvcGroupBadges();
  // Re-apply the active filter (if any) so newly-added customs respect it.
  filterBomServices(document.getElementById("bom-services-filter").value);
}

function updateSvcGroupBadges() {
  document.querySelectorAll('#bom-services-list .svc-group').forEach(g => {
    const sel = g.querySelectorAll('input[data-bom-svc]:checked').length;
    const badge = g.querySelector('.svc-cat-selected');
    if (!badge) return;
    if (sel) { badge.textContent = `${sel} selected`; badge.hidden = false; g.classList.add("has-selected"); }
    else { badge.hidden = true; g.classList.remove("has-selected"); }
  });
}

function updateBomServiceCount() {
  const n = document.querySelectorAll('#bom-services-list input[data-bom-svc]:checked').length;
  document.getElementById("bom-edit-services-count").textContent = `${n} selected`;
  updateSvcGroupBadges();
}

function setBomSelectedServices(names) {
  const set = new Set((names || []).map(s => (s || "").toLowerCase()));
  document.querySelectorAll('#bom-services-list input[data-bom-svc]').forEach(cb => {
    cb.checked = set.has(cb.value.toLowerCase());
  });
  updateBomServiceCount();
}

function getBomSelectedServices() {
  return Array.from(document.querySelectorAll('#bom-services-list input[data-bom-svc]:checked'))
    .map(cb => {
      const rec = { name: cb.value };
      const tiers = _catalogTiersFor(cb.value);
      if (tiers.length) {
        const chosen = BOM_EDIT.serviceTiers[cb.value];
        if (chosen && tiers.some(t => t.id === chosen)) rec.tier = chosen;
      }
      return rec;
    });
}

// Return the catalog tier list ([{id,label,zone_redundant}]) for a service
// name, or [] when the service has no tiers.
function _catalogTiersFor(name) {
  const entry = (BOM_EDIT.catalog || []).find(s => s.name === name);
  return (entry && Array.isArray(entry.tiers)) ? entry.tiers : [];
}

// Render the step-3 "Service tiers" section: one dropdown per selected
// service that has catalog tiers. Selections persist in BOM_EDIT.serviceTiers
// so they survive step navigation and feed the save payload.
function renderBomServiceTiers() {
  const step = document.getElementById("bom-service-tiers-step");
  const list = document.getElementById("bom-service-tiers-list");
  if (!step || !list) return;
  const selected = getBomSelectedServices();
  const tiered = selected.filter(s => _catalogTiersFor(s.name).length);
  if (!tiered.length) {
    step.hidden = true;
    list.innerHTML = "";
    return;
  }
  step.hidden = false;
  list.innerHTML = "";
  for (const svc of tiered) {
    const tiers = _catalogTiersFor(svc.name);
    const cur = BOM_EDIT.serviceTiers[svc.name] || "";
    const row = document.createElement("div");
    row.className = "bom-tier-row";
    const opts = ['<option value="">— not specified —</option>']
      .concat(tiers.map(t => {
        const zr = t.zone_redundant ? " · zone-redundant capable" : "";
        return `<option value="${escapeHtml(t.id)}"${t.id === cur ? " selected" : ""}>${escapeHtml(t.label)}${zr}</option>`;
      }))
      .join("");
    row.innerHTML = `
      <span class="bom-tier-name">${escapeHtml(svc.name)}</span>
      <select data-tier-svc="${escapeHtml(svc.name)}">${opts}</select>`;
    const sel = row.querySelector("select");
    sel.addEventListener("change", () => {
      if (sel.value) BOM_EDIT.serviceTiers[svc.name] = sel.value;
      else delete BOM_EDIT.serviceTiers[svc.name];
    });
    list.appendChild(row);
  }
}

function filterBomServices(query) {
  const q = (query || "").trim().toLowerCase();
  const list = document.getElementById("bom-services-list");
  list.querySelectorAll('.svc-group').forEach(group => {
    let anyVisible = false;
    group.querySelectorAll('label').forEach(lbl => {
      const hit = !q || (lbl.dataset.name || "").includes(q);
      lbl.classList.toggle("hidden", !hit);
      if (hit) anyVisible = true;
    });
    // Hide whole category when nothing matches; auto-expand when searching,
    // collapse back to the tidy default when the filter is cleared.
    group.classList.toggle("hidden", !anyVisible);
    if (q) {
      group.classList.toggle("open", anyVisible);
    } else {
      group.classList.remove("open");
    }
    const header = group.querySelector('.svc-cat-header');
    if (header) header.setAttribute("aria-expanded", group.classList.contains("open") ? "true" : "false");
  });
}

// ---- Regions picker (parallel structure to services) ----

// Classify an Azure region into a geography/continent bucket for grouping.
// Uses specific location-name tokens (canonical name) so "eastus" ≠ "australia".
function regionGeo(name) {
  const n = (name || "").toLowerCase();
  const MAP = [
    ["United States", ["centralus", "eastus", "westus", "southcentralus", "northcentralus", "westcentralus", "unitedstates", "usgov", "usdod", "ussec", "usnat"]],
    ["Canada", ["canada"]],
    ["South America", ["brazil", "chile"]],
    ["Mexico", ["mexico"]],
    ["Europe", ["europe", "uksouth", "ukwest", "france", "germany", "norway", "switzerland", "sweden", "italy", "poland", "spain", "belgium", "austria", "denmark", "ireland", "netherlands", "finland", "greece"]],
    ["Asia Pacific", ["asia", "india", "japan", "korea", "australia", "newzealand", "zealand", "indonesia", "malaysia", "taiwan", "china", "hongkong", "singapore"]],
    ["Middle East", ["uae", "qatar", "israel", "saudi", "emirates"]],
    ["Africa", ["africa"]],
  ];
  for (const [geo, keys] of MAP) {
    if (keys.some(k => n.includes(k))) return geo;
  }
  return "Other";
}

const REGION_GEO_ORDER = [
  "United States", "Canada", "South America", "Mexico", "Europe",
  "Asia Pacific", "Middle East", "Africa", "Other",
];

function renderBomRegionsList() {
  const list = document.getElementById("bom-regions-list");
  list.innerHTML = "";
  const cat = BOM_EDIT.regionsCatalog || [];

  const groups = new Map();
  for (const rg of cat) {
    const g = regionGeo(rg.name);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(rg);
  }
  const geos = Array.from(groups.keys()).sort((a, b) => {
    const ia = REGION_GEO_ORDER.indexOf(a), ib = REGION_GEO_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });

  for (const g of geos) {
    const group = document.createElement("div");
    group.className = "svc-group";
    group.dataset.geo = g.toLowerCase();

    const header = document.createElement("button");
    header.type = "button";
    header.className = "svc-cat-header";
    header.setAttribute("aria-expanded", "false");
    header.innerHTML =
      `<span class="svc-cat-chevron" aria-hidden="true">&#9656;</span>` +
      `<span class="svc-cat-name">${escapeHtml(g)}</span>` +
      `<span class="svc-cat-count">${groups.get(g).length}</span>` +
      `<span class="svc-cat-selected" hidden></span>`;
    header.addEventListener("click", () => {
      const open = group.classList.toggle("open");
      header.setAttribute("aria-expanded", open ? "true" : "false");
    });

    const body = document.createElement("div");
    body.className = "svc-group-items";
    for (const rg of groups.get(g)) {
      const id = "bom-rg-" + rg.name.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
      const lbl = document.createElement("label");
      lbl.dataset.name = rg.name.toLowerCase();
      lbl.dataset.display = (rg.display_name || rg.name).toLowerCase();
      lbl.dataset.az = rg.has_az ? "1" : "0";
      const azChip = rg.has_az
        ? '<span class="svc-zone" title="Region has Availability Zones">AZ</span>'
        : '<span class="region-noaz" title="Region does not have AZs">no AZ</span>';
      const delBtn = rg.is_custom
        ? `<button type="button" class="bom-custom-del" data-del-rg="${escapeHtml(rg.name)}" title="Remove this custom region">&times;</button>`
        : "";
      lbl.innerHTML = `<input type="checkbox" id="${id}" value="${escapeHtml(rg.name)}" data-bom-rg /> <span>${escapeHtml(rg.display_name || rg.name)} <span class="muted">(${escapeHtml(rg.name)})</span> ${azChip}</span>${delBtn}`;
      body.appendChild(lbl);
    }

    group.appendChild(header);
    group.appendChild(body);
    list.appendChild(group);
  }
  updateRegionGroupBadges();
  filterBomRegions();
  populateBomPreferredRegionOptions();
}

// Fill the wizard's "Preferred source region" dropdown from the same region
// catalog as the region picker. Value = region short name (stable); label =
// display name. Preserves the current selection across re-renders.
function populateBomPreferredRegionOptions() {
  const sel = document.getElementById("bom-preferred-region");
  if (!sel) return;
  const prev = sel.value;
  const cat = (BOM_EDIT.regionsCatalog || []).slice()
    .sort((a, b) => (a.display_name || a.name).localeCompare(b.display_name || b.name));
  let html = '<option value="">— None (use default) —</option>';
  for (const rg of cat) {
    const label = (rg.display_name || rg.name) + " (" + rg.name + ")";
    html += `<option value="${escapeHtml(rg.name)}">${escapeHtml(label)}</option>`;
  }
  sel.innerHTML = html;
  if (prev) sel.value = prev;
}

function updateRegionGroupBadges() {
  document.querySelectorAll('#bom-regions-list .svc-group').forEach(g => {
    const sel = g.querySelectorAll('input[data-bom-rg]:checked').length;
    const badge = g.querySelector('.svc-cat-selected');
    if (!badge) return;
    if (sel) { badge.textContent = `${sel} selected`; badge.hidden = false; g.classList.add("has-selected"); }
    else { badge.hidden = true; g.classList.remove("has-selected"); }
  });
}

function updateBomRegionsCount() {
  const total = document.querySelectorAll('#bom-regions-list input[data-bom-rg]').length;
  const n = document.querySelectorAll('#bom-regions-list input[data-bom-rg]:checked').length;
  document.getElementById("bom-regions-count").textContent = `${n} of ${total} selected`;
  updateRegionGroupBadges();
}

function setBomSelectedRegions(names) {
  const set = new Set((names || []).map(s => (s || "").toLowerCase()));
  // Empty saved value means "leave the default" — we default-check all
  // visible regions on first open, but only when there are no saved
  // selections (caller decides).
  document.querySelectorAll('#bom-regions-list input[data-bom-rg]').forEach(cb => {
    cb.checked = set.has(cb.value.toLowerCase());
  });
  updateBomRegionsCount();
}

function getBomSelectedRegions() {
  return Array.from(document.querySelectorAll('#bom-regions-list input[data-bom-rg]:checked'))
    .map(cb => cb.value);
}

function filterBomRegions() {
  const q = (document.getElementById("bom-regions-search").value || "").trim().toLowerCase();
  const filt = document.getElementById("bom-regions-filter").value || "all";
  const active = !!q || filt !== "all";
  document.querySelectorAll('#bom-regions-list .svc-group').forEach(group => {
    let anyVisible = false;
    group.querySelectorAll('label').forEach(lbl => {
      let show = true;
      if (filt === "az" && lbl.dataset.az !== "1") show = false;
      if (filt === "noaz" && lbl.dataset.az !== "0") show = false;
      if (show && q) {
        show = lbl.dataset.name.includes(q) || lbl.dataset.display.includes(q);
      }
      lbl.classList.toggle("hidden", !show);
      if (show) anyVisible = true;
    });
    group.classList.toggle("hidden", !anyVisible);
    // Auto-expand while a search/AZ filter is active; collapse to the tidy
    // default when nothing is being filtered.
    if (active) group.classList.toggle("open", anyVisible);
    else group.classList.remove("open");
    const header = group.querySelector('.svc-cat-header');
    if (header) header.setAttribute("aria-expanded", group.classList.contains("open") ? "true" : "false");
  });
}

async function addCustomBomService() {
  const nameEl = document.getElementById("bom-custom-svc-name");
  const provEl = document.getElementById("bom-custom-svc-provider");
  const rtEl = document.getElementById("bom-custom-svc-rt");
  const zcEl = document.getElementById("bom-custom-svc-zone");
  const statusEl = document.getElementById("bom-custom-svc-status");
  const name = nameEl.value.trim();
  const provider = provEl.value.trim();
  const resource_type = rtEl.value.trim();
  if (!name || !provider || !resource_type) {
    statusEl.textContent = "Name, provider, and resource type are required.";
    statusEl.dataset.kind = "error";
    return;
  }
  statusEl.textContent = "Saving…";
  statusEl.dataset.kind = "info";
  try {
    const r = await apiFetch("/api/bom/service_catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, provider, resource_type, zone_check: !!zcEl.checked }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      statusEl.textContent = `${body.error || "error"}: ${body.message || r.statusText}`;
      statusEl.dataset.kind = "error";
      return;
    }
    // Preserve current selections + auto-select the new entry.
    const selected = getBomSelectedServices().map(s => s.name);
    selected.push(body.name);
    await ensureBomCatalog(true);
    renderBomServiceList();
    setBomSelectedServices(selected);
    nameEl.value = ""; provEl.value = ""; rtEl.value = ""; zcEl.checked = false;
    statusEl.textContent = `Added “${body.name}”.`;
    statusEl.dataset.kind = "ok";
  } catch (e) {
    statusEl.textContent = `Network error: ${e.message}`;
    statusEl.dataset.kind = "error";
  }
}

async function deleteCustomBomService(name) {
  if (!confirm(`Remove the custom service “${name}” from your catalog?`)) return;
  const selected = getBomSelectedServices().map(s => s.name).filter(n => n !== name);
  try {
    const r = await apiFetch(`/api/bom/service_catalog/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (r.status !== 204 && !r.ok) {
      const body = await r.json().catch(() => ({}));
      setBomStatus(errLine(body, r.statusText), "error");
      return;
    }
    await ensureBomCatalog(true);
    renderBomServiceList();
    setBomSelectedServices(selected);
  } catch (e) {
    setBomStatus(netErrLine(e), "error");
  }
}

async function addCustomBomRegion() {
  const nameEl = document.getElementById("bom-custom-rg-name");
  const dispEl = document.getElementById("bom-custom-rg-display");
  const azEl = document.getElementById("bom-custom-rg-az");
  const statusEl = document.getElementById("bom-custom-rg-status");
  const name = nameEl.value.trim().toLowerCase();
  const display_name = dispEl.value.trim();
  if (!name) {
    statusEl.textContent = "Short name is required.";
    statusEl.dataset.kind = "error";
    return;
  }
  statusEl.textContent = "Saving…";
  statusEl.dataset.kind = "info";
  try {
    const r = await apiFetch("/api/bom/region_catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, display_name, has_az: !!azEl.checked }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      statusEl.textContent = `${body.error || "error"}: ${body.message || r.statusText}`;
      statusEl.dataset.kind = "error";
      return;
    }
    const selected = getBomSelectedRegions();
    selected.push(body.name);
    await ensureBomRegionsCatalog(true);
    renderBomRegionsList();
    setBomSelectedRegions(selected);
    nameEl.value = ""; dispEl.value = ""; azEl.checked = false;
    statusEl.textContent = `Added “${body.display_name}”.`;
    statusEl.dataset.kind = "ok";
  } catch (e) {
    statusEl.textContent = `Network error: ${e.message}`;
    statusEl.dataset.kind = "error";
  }
}

async function deleteCustomBomRegion(name) {
  if (!confirm(`Remove the custom region “${name}” from your catalog?`)) return;
  const selected = getBomSelectedRegions().filter(n => n !== name);
  try {
    const r = await apiFetch(`/api/bom/region_catalog/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (r.status !== 204 && !r.ok) {
      const body = await r.json().catch(() => ({}));
      setBomStatus(errLine(body, r.statusText), "error");
      return;
    }
    await ensureBomRegionsCatalog(true);
    renderBomRegionsList();
    setBomSelectedRegions(selected);
  } catch (e) {
    setBomStatus(netErrLine(e), "error");
  }
}

function addBomSkuRow(row) {
  row = row || {};
  const tbody = document.getElementById("bom-skus-tbody");
  const tr = document.createElement("tr");
  // VM-type labels (primary_label / alt_label) are auto-derived
  // server-side via compile._short_label(), so we don't ask the user
  // for them here. Imported xlsx values still round-trip through
  // storage even though they're not shown.
  tr.innerHTML = `
    <td><input type="text" data-col="primary_family" list="bom-sku-family-options" autocomplete="off" spellcheck="false" placeholder="Type or pick a SKU family" value="${escapeHtml(row.primary_family || "")}" /></td>
    <td><input type="text" data-col="alt_family" list="bom-sku-family-options" autocomplete="off" spellcheck="false" placeholder="Optional fallback family" value="${escapeHtml(row.alt_family || "")}" /></td>
    <td><input type="number" data-col="required_cores" placeholder="100" min="0" step="1" value="${row.required_cores != null ? Number(row.required_cores) : ""}" /></td>
    <td><button type="button" class="skus-del" title="Remove row">&times;</button></td>
  `;
  tr.querySelector(".skus-del").addEventListener("click", () => tr.remove());
  // Auto-select text on focus so clicking the dropdown shows the full list
  tr.querySelectorAll('input[list]').forEach(inp => {
    inp.addEventListener("focus", () => inp.select());
  });
  tbody.appendChild(tr);
}

function getBomSkuRows() {
  const out = [];
  document.querySelectorAll("#bom-skus-tbody tr").forEach(tr => {
    const primary_family = tr.querySelector('[data-col="primary_family"]').value.trim();
    const alt_family = tr.querySelector('[data-col="alt_family"]').value.trim();
    const coresRaw = tr.querySelector('[data-col="required_cores"]').value.trim();
    if (!primary_family && !alt_family && !coresRaw) return; // blank
    const cores = coresRaw === "" ? 0 : Number(coresRaw);
    out.push({
      primary_family,
      // Labels are derived server-side — we send null so the backend
      // populates them via _short_label(). This keeps the saved JSON
      // self-describing on re-load.
      primary_label: null,
      alt_family: alt_family || null,
      alt_label: null,
      required_cores: Number.isFinite(cores) ? cores : 0,
    });
  });
  return out;
}

function applyBomToForm(meta) {
  const subSel = document.getElementById("bom-sub");
  const savedIds = new Set(subscriptionList(meta));
  Array.from(subSel.options).forEach(opt => {
    opt.selected = savedIds.has(opt.value);
  });
  document.getElementById("bom-tag").value = meta.tag || "";
  document.getElementById("bom-customer").value = meta.customer_name || "";
  { const rEl = document.getElementById("bom-resilience"); if (rEl) rEl.value = _normalizeResilience(meta.resilience); }
  { const pr = document.getElementById("bom-preferred-region"); if (pr) pr.value = meta.preferred_region || ""; }
  setBomSelectedServices((meta.services || []).map(s => s.name));
  // Seed per-service tier selections from the saved BOM so the step-3
  // tier pickers reflect what was chosen previously.
  BOM_EDIT.serviceTiers = {};
  for (const s of (meta.services || [])) {
    if (s && s.name && s.tier) BOM_EDIT.serviceTiers[s.name] = s.tier;
  }
  // Regions: empty saved list means "default — all known regions
  // selected" so the user sees the implicit default rather than an
  // empty grid that quietly means the same thing.
  const savedRegions = (meta.regions || []).map(r => (typeof r === "string" ? r : r.name));
  if (savedRegions.length) {
    setBomSelectedRegions(savedRegions);
  } else {
    setBomSelectedRegions((BOM_EDIT.regionsCatalog || []).map(r => r.name));
  }
  document.getElementById("bom-skus-tbody").innerHTML = "";
  for (const sku of (meta.required_skus || [])) addBomSkuRow(sku);
  if (!(meta.required_skus || []).length) addBomSkuRow();
}

async function loadBomFromApi(bomId) {
  try {
    const r = await apiFetch(`/api/subscription_metadata/${encodeURIComponent(bomId)}`);
    if (r.status === 404) {
      BOM_EDIT.current = null;
      addBomSkuRow();
      updateBomServiceCount();
      // Pre-select all regions for a new BOM so the user starts from
      // the full default scope rather than an empty grid.
      setBomSelectedRegions((BOM_EDIT.regionsCatalog || []).map(r => r.name));
      setBomStatus("BOM not found. Fill out the form and click <strong>Save BOM</strong> to create one.", "info");
      return;
    }
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      setBomStatus(errLine(body, r.statusText), "error");
      addBomSkuRow();
      return;
    }
    const meta = await r.json();
    BOM_EDIT.current = meta;
    applyBomToForm(meta);
    // Layer this BOM's saved support override on top of the global prefill.
    _overlayBomSupportOverride(meta.support_override);
    updateBomServiceCount();
    updateBomRegionsCount();
    setBomStatus(`Loaded — last updated ${escapeHtml(meta.bom_updated_at || "—")} by ${escapeHtml(meta.bom_updated_by || "—")}.`, "info");
  } catch (e) {
    setBomStatus(netErrLine(e), "error");
    addBomSkuRow();
  }
}

async function saveBom() {
  const subSel = document.getElementById("bom-sub");
  const subIds = Array.from(subSel.selectedOptions).map(o => o.value);
  if (!subIds.length) {
    return setBomStatus("Select at least one subscription.", "error");
  }
  const bad = subIds.find(id => !GUID_RE.test(id));
  if (bad) return setBomStatus(`Invalid subscription GUID: ${escapeHtml(bad)}`, "error");
  const sub = subIds[0];
  const tag = document.getElementById("bom-tag").value.trim();
  const customer_name = document.getElementById("bom-customer").value.trim();
  const resilience = _normalizeResilience((document.getElementById("bom-resilience") || {}).value);
  const preferred_region = ((document.getElementById("bom-preferred-region") || {}).value || "").trim();
  const services = getBomSelectedServices();
  const required_skus = getBomSkuRows();
  // Persist the explicit region selection so the BOM analyzes exactly what
  // was chosen — including when ALL regions are selected (previously "all"
  // collapsed to an empty list and silently fell back to a smaller default
  // set). An empty list is only sent when nothing is selected, which the
  // backend treats as "use the full region catalog".
  const selectedRegions = getBomSelectedRegions();
  const regions = selectedRegions.length ? selectedRegions : [];

  const payload = {
    subscription_id: sub,
    subscription_ids: subIds,
    tag, customer_name, resilience, preferred_region,
    services, regions, required_skus,
    support_override: _collectBomSupportOverride(),
  };

  // Editing an existing BOM updates it (PUT /{bom_id}); otherwise create a
  // brand-new BOM (POST) so we never clobber a different BOM that happens to
  // share this subscription.
  const editingId = BOM_EDIT.current && BOM_EDIT.current.bom_id;
  const url = editingId
    ? `/api/subscription_metadata/${encodeURIComponent(editingId)}`
    : `/api/subscription_metadata`;
  const method = editingId ? "PUT" : "POST";

  document.getElementById("bom-save").disabled = true;
  setBomStatus("Saving…", "info");
  try {
    const r = await apiFetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      setBomStatus(errLine(body, r.statusText), "error");
      document.getElementById("bom-save").disabled = false;
      return;
    }
    BOM_EDIT.current = body;
    document.getElementById("bom-save").disabled = false;
    await refreshSubMetadataIndex();
    // Make the saved/created BOM the active one so the panel + snapshots follow.
    if (body.bom_id) {
      STATE.activeBomId = body.bom_id;
      STATE.activeSubscription = null;
      try { localStorage.setItem("activeBomId", body.bom_id); } catch (e) {}
    }
    renderBomNav();
    renderBomPanel();
    closeBomModal();
    await loadSnapshotsList();
    await loadSnapshot(null);
    await _restoreQuotaRequestsFromDb();
  } catch (e) {
    setBomStatus(netErrLine(e), "error");
    document.getElementById("bom-save").disabled = false;
  }
}

// ---------------------------------------------------------------- Run modal

const TOKEN = {
  info: null,         // { token, expires_at, expires_in_seconds, az_user, ... }
  refreshTimer: null, // setTimeout handle for the countdown
};

async function refreshAuthToken({ force = false } = {}) {
  setTokenStatus("loading", force ? "Opening browser sign-in…" : "Checking sign-in…");
  // Delegated (multi-customer) mode: mint the ARM token in the browser first so
  // the follow-up /api/auth/signin call carries it. Silent unless force.
  if (APP_CONFIG && APP_CONFIG.delegated_mode) {
    try {
      const tok = await ensureDelegatedToken({ force });
      if (!tok) {
        if (force) {
          setTokenStatus("error", "Sign-in was cancelled or blocked. Please try again.");
        } else {
          setTokenStatus("warn", "Not signed in. Click <strong>Sign in</strong> to connect your Azure account.");
        }
        TOKEN.info = null;
        updateSigninChip();
        return null;
      }
    } catch (e) {
      setTokenStatus("error", netErrLine(e));
      TOKEN.info = null;
      updateSigninChip();
      return null;
    }
  }
  try {
    const r = await apiFetch("/api/auth/signin", { method: force ? "POST" : "GET" });
    const body = await r.json();
    if (!r.ok) {
      const err = body.error || body.code || "error";
      if (err === "not_signed_in" && !force) {
        // Silent check found no cached session — invite the user to sign in.
        setTokenStatus("warn", "Not signed in. Click <strong>Sign in</strong> to open the browser sign-in.");
        TOKEN.info = null;
        updateSigninChip();
        return null;
      }
      const msg = body.message || r.statusText;
      const ca = conditionalAccessGuidanceHtml(msg, err);
      if (ca) {
        setTokenStatus("error", ca);
      } else {
        const safe = escapeHtml(msg).replace(/\n/g, "<br>");
        setTokenStatus("error", `<strong>${escapeHtml(err)}:</strong><br>${safe}`);
      }
      TOKEN.info = null;
      updateSigninChip();
      return null;
    }
    TOKEN.info = body;
    renderTokenStatus();
    updateSigninChip();
    return body;
  } catch (e) {
    setTokenStatus("error", netErrLine(e));
    TOKEN.info = null;
    updateSigninChip();
    return null;
  }
}

// Detects a Conditional Access block of the default Azure CLI sign-in client
// (AADSTS53003 "you don't have access to this resource"). Returns guided-fix
// HTML for the sign-in status banner, or null if the error is unrelated.
// The default flow needs no app registration; this guidance only appears when
// the customer's tenant policy actually blocks the built-in client.
function conditionalAccessGuidanceHtml(message, code) {
  const m = String(message || "");
  const c = String(code || "");
  const isCaBlock =
    c === "ca_consent_required" ||
    /AADSTS53003/i.test(m) ||
    /conditional access/i.test(m) ||
    /you don'?t have access to this resource/i.test(m);
  if (!isCaBlock) return null;
  return (
    `<strong>Sign-in blocked by your organization (AADSTS53003).</strong><br>` +
    `Your tenant's Conditional Access policy is blocking the built-in ` +
    `<em>Microsoft Azure CLI</em> sign-in app. No app registration is needed ` +
    `normally — this only happens when a policy blocks that app.<br><br>` +
    `<strong>To fix, an admin can either:</strong>` +
    `<ol style="margin:6px 0 6px 18px;padding:0;">` +
    `<li>Exclude the <em>Microsoft Azure CLI</em> app (<code>04b07795-8ddb-461a-bbee-02f9e1bf7b46</code>) from the blocking policy, <strong>or</strong></li>` +
    `<li>Register a dedicated app and launch with these environment variables set:` +
    `<br><code>AZURE_CLIENT_ID</code>, <code>AZURE_TENANT_ID</code>, <code>AZURE_REDIRECT_URI=http://localhost</code>` +
    `<br>(Public client / <em>Allow public client flows</em> = Yes, redirect <code>http://localhost</code>, delegated <em>Azure Service Management → user_impersonation</em>.)</li>` +
    `</ol>` +
    `See the README section <em>“If sign-in is blocked by Conditional Access”</em> for full steps. ` +
    `If the policy requires MFA or a compliant device instead, complete that in the browser prompt and try again.`
  );
}

// ----- Header sign-in chip ---------------------------------------------------
function updateSigninChip() {
  const chip = document.getElementById("signin-chip");
  const text = document.getElementById("signin-chip-text");
  if (!chip || !text) return;
  const signedIn = !!(TOKEN.info && (TOKEN.info.expires_in_seconds || 0) > 0);
  if (signedIn) {
    const who = TOKEN.info.az_user || "signed in";
    chip.dataset.state = "in";
    text.textContent = who;
    chip.title = `Signed in as ${who}. Click for sign-in details.`;
  } else {
    chip.dataset.state = "out";
    text.textContent = "Sign in";
    chip.title = "Not signed in. Click for sign-in options.";
  }
  // Keep the onboarding stepper's step-1 state in sync with sign-in changes.
  if (!STATE.activeBomId) {
    const emptyEl = document.getElementById("bom-panel-empty");
    if (emptyEl && !emptyEl.classList.contains("hidden")) renderOnboardingStepper(emptyEl);
  }
}

async function onSigninChipClick() {
  openSigninModal();
}

// ----- Sign-in status modal --------------------------------------------------
function openSigninModal() {
  document.getElementById("signin-overlay").classList.remove("hidden");
  document.getElementById("signin-modal").classList.remove("hidden");
  // Silent status check — no browser popup; the user clicks Sign in explicitly.
  refreshAuthToken({ force: false });
}

function closeSigninModal() {
  document.getElementById("signin-overlay").classList.add("hidden");
  document.getElementById("signin-modal").classList.add("hidden");
}

async function doSignOut() {
  setTokenStatus("loading", "Signing out…");
  try {
    // Best-effort legacy server signout (harmless no-op in delegated mode).
    try { await apiFetch("/api/auth/signout", { method: "POST" }); } catch (_e) {}
    // Clear the browser-held MSAL account + token so the next sign-in is fresh.
    try { if (window.DelegatedAuth && DelegatedAuth.logout) await DelegatedAuth.logout(); } catch (_e) {}
    TOKEN.info = null;
    try { updateSigninChip(); } catch (_e) {}
    closeSigninModal();
    // Return the user to the login start page (auth gate).
    showAuthGate();
  } catch (e) {
    setTokenStatus("error", netErrLine(e));
  }
}

async function doSwitchDirectory() {
  setTokenStatus("loading", "Signing out and re-opening sign-in…");
  try {
    try { await apiFetch("/api/auth/signout", { method: "POST" }); } catch (_e) {}
    // Clear MSAL so the interactive prompt lets the user pick a different account.
    try { if (window.DelegatedAuth && DelegatedAuth.logout) await DelegatedAuth.logout(); } catch (_e) {}
    TOKEN.info = null;
    try { updateSigninChip(); } catch (_e) {}
    closeSigninModal();
    // Send the user back to the login start page to sign in again.
    showAuthGate();
  } catch (e) {
    setTokenStatus("error", netErrLine(e));
  }
}

async function loadSubscriptionsDropdown() {
  const sel = document.getElementById("bom-sub");
  sel.innerHTML = '<option disabled>Loading subscriptions…</option>';
  try {
    const r = await apiJson("/api/az/subscriptions");
    const subs = r.subscriptions || [];
    if (!subs.length) {
      sel.innerHTML = '<option disabled>No subscriptions found. Sign in first.</option>';
      return;
    }
    window._loadedSubscriptions = subs;
    sel.innerHTML = subs.map(s =>
      `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name)} (${s.id.substring(0, 8)}…)</option>`
    ).join("");
    sel.size = Math.min(subs.length, 8);
    renderSubscriptionSwitcher();
    renderSubscriptionFilter();
  } catch (e) {
    sel.innerHTML = `<option disabled>Error: ${e.message || e}</option>`;
  }
}

async function preloadSubscriptionNames() {
  if (window._loadedSubscriptions && window._loadedSubscriptions.length) return;
  try {
    const r = await apiJson("/api/az/subscriptions");
    const subs = r.subscriptions || [];
    if (subs.length) {
      window._loadedSubscriptions = subs;
      renderSubscriptionSwitcher();
      renderSubscriptionFilter();
      if (_isRegionsSub("table")) renderTable();
      if (STATE.view === "quota") _renderQuotaForSelectedRegion();
    }
  } catch (e) { /* silent — best effort */ }
}

function setTokenStatus(state, html) {
  const el = document.getElementById("run-token-status");
  if (!el) return;
  el.dataset.state = state;
  const icon = { ok: "✅", warn: "⚠️", error: "⛔", loading: "⏳" }[state] || "•";
  el.querySelector(".token-state-icon").textContent = icon;
  el.querySelector(".token-state-msg").innerHTML = html;
}

function renderTokenStatus() {
  if (!TOKEN.info) return;
  const sec = Math.max(0, TOKEN.info.expires_in_seconds || 0);
  const mins = Math.floor(sec / 60);
  const state = sec < 60 ? "error" : sec < 300 ? "warn" : "ok";
  const who = TOKEN.info.az_user
    ? ` for <code>${escapeHtml(TOKEN.info.az_user)}</code>`
    : "";
  const expires = sec >= 60
    ? `${mins} min ${sec % 60}s remaining`
    : sec > 0 ? `${sec}s remaining (refresh recommended)`
    : `expired`;
  setTokenStatus(state, `Token ready${who} — ${expires}.`);

  if (TOKEN.refreshTimer) { clearTimeout(TOKEN.refreshTimer); TOKEN.refreshTimer = null; }
  TOKEN.refreshTimer = setTimeout(renderTokenStatus, 1000);
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  if (!container || !message) return;
  const tone = type === "error" ? "error" : (type === "warning" ? "warning" : "success");
  const toast = document.createElement("div");
  toast.className = `toast toast--${tone}`;
  toast.setAttribute("role", "status");
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.classList.add("is-hiding"), 4700);
  setTimeout(() => toast.remove(), 5200);
}

// In-app confirmation dialog returning a Promise<boolean>. Native window.confirm
// can be silently suppressed by the browser (the "prevent additional dialogs"
// checkbox, sandboxed frames, or corporate policy), which would make a gated
// action — e.g. submitting a real Azure support ticket — quietly do nothing.
// This modal is a real DOM element, so it can never be suppressed.
function showConfirm(message, opts = {}) {
  const title = opts.title || "Please confirm";
  const confirmLabel = opts.confirmLabel || "Confirm";
  const cancelLabel = opts.cancelLabel || "Cancel";
  const danger = !!opts.danger;
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "app-confirm-overlay";
    const dialog = document.createElement("div");
    dialog.className = "app-confirm";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const h = document.createElement("h3");
    h.className = "app-confirm-title";
    h.textContent = title;
    const p = document.createElement("p");
    p.className = "app-confirm-msg";
    p.textContent = message;
    const actions = document.createElement("div");
    actions.className = "app-confirm-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn--sm";
    cancelBtn.textContent = cancelLabel;
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "btn btn--sm " + (danger ? "btn--danger" : "btn--accent");
    okBtn.textContent = confirmLabel;
    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    dialog.appendChild(h);
    dialog.appendChild(p);
    dialog.appendChild(actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    let done = false;
    const close = (val) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(val);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") close(false);
      else if (ev.key === "Enter") close(true);
    };
    cancelBtn.addEventListener("click", () => close(false));
    okBtn.addEventListener("click", () => close(true));
    overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(false); });
    document.addEventListener("keydown", onKey);
    setTimeout(() => okBtn.focus(), 0);
  });
}
// (which render via innerHTML). All dynamic fields from an API error envelope
// are escaped so a server- or user-derived message can't inject markup/script.
function errLine(body, fallback) {
  const code = escapeHtml((body && (body.error || body.code)) || "error");
  const msg = escapeHtml((body && body.message) || fallback || "");
  return `<strong>${code}</strong>${msg ? ": " + msg : ""}`;
}

// Safe "Network error: <message>" line for the innerHTML status banners.
function netErrLine(e) {
  return `Network error: ${escapeHtml((e && e.message) || String(e))}`;
}

const GUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

function bomPlanLabel(meta) {
  if (!meta) return "";
  const sub = primarySubscriptionId(meta);
  const shortSub = sub ? sub.slice(0, 8) + "…" + sub.slice(-4) : "";
  const parts = [];
  if (meta.tag) parts.push(meta.tag);
  if (meta.customer_name && meta.customer_name !== meta.tag) parts.push(meta.customer_name);
  // Fall back to the shared display-name helper (Tag → Customer → Subscription).
  const lead = parts.length ? parts.join(" — ") : bomDisplayName(meta);
  const label = (shortSub && lead !== sub) ? `${lead} · ${shortSub}` : lead;
  const ids = subscriptionList(meta);
  return ids.length > 1 ? `${label} · ${ids.length} subs` : label;
}

function listBomPlans() {
  // Sort by tag (case-insensitive), then customer, then sub.
  return Object.values(BOM_META.index || {})
    .filter(m => m && m.bom_id)
    .sort((a, b) => {
      const ka = (a.tag || a.customer_name || "").toLowerCase();
      const kb = (b.tag || b.customer_name || "").toLowerCase();
      if (ka !== kb) return ka < kb ? -1 : 1;
      return primarySubscriptionId(a).localeCompare(primarySubscriptionId(b));
    });
}

function populateBomPicker(preferredBomId) {
  const sel = document.getElementById("run-bom-pick");
  if (!sel) return;
  const plans = listBomPlans();
  const prev = preferredBomId || sel.value || STATE.activeBomId || "";
  sel.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = plans.length
    ? "— Select a saved BOM plan —"
    : "— No saved BOMs —";
  sel.appendChild(placeholder);

  for (const m of plans) {
    const opt = document.createElement("option");
    opt.value = m.bom_id;
    opt.textContent = bomPlanLabel(m);
    sel.appendChild(opt);
  }

  // Pre-select: preferred BOM → active BOM → only-one BOM → placeholder.
  let chosen = "";
  if (prev && plans.find(m => m.bom_id === prev)) chosen = prev;
  else if (plans.length === 1) chosen = plans[0].bom_id;
  sel.value = chosen;

  updateRunBomSummary();
}

function updateRunBomSummary() {
  const sel = document.getElementById("run-bom-pick");
  const summary = document.getElementById("run-bom-summary");
  const empty = document.getElementById("run-bom-empty");
  const runBtn = document.getElementById("run-go");
  const hasAnyPlan = listBomPlans().length > 0;
  const bomId = sel ? sel.value.trim() : "";
  const meta = bomId ? getBomMeta(bomId) : null;

  if (empty) empty.classList.toggle("hidden", hasAnyPlan);

  if (!meta) {
    if (summary) {
      summary.classList.add("hidden");
      summary.innerHTML = "";
    }
    if (runBtn) runBtn.disabled = true;
    return;
  }

  if (runBtn) runBtn.disabled = false;

  const svcCount = (meta.services || []).length;
  const rgCount = (meta.regions || []).length;
  const skuList = meta.required_skus || [];
  const skuCount = skuList.length;
  const totalCores = skuList.reduce((s, r) => s + (Number(r.required_cores) || 0), 0);
  const customer = meta.customer_name || "(no name)";
  const tag = meta.tag || "(untagged)";
  const updated = meta.bom_updated_at
    ? new Date(meta.bom_updated_at).toLocaleString()
    : "—";

  const item = (label, value, opts) => {
    const cls = (opts && opts.mono) ? "rbs-value mono" : "rbs-value";
    return `<div class="rbs-item">
      <span class="rbs-label">${escapeHtml(label)}</span>
      <span class="${cls}" title="${escapeHtml(String(value))}">${escapeHtml(String(value))}</span>
    </div>`;
  };

  const chip = (label, value, warn) => {
    const cls = warn ? "rbs-chip rbs-warn" : "rbs-chip";
    return `<span class="${cls}"><strong>${escapeHtml(String(value))}</strong>${escapeHtml(label)}</span>`;
  };

  summary.innerHTML =
    item("BOM Name", tag) +
    item("Customer", customer) +
    item("Primary subscription", primarySubscriptionId(meta) || "—", { mono: true }) +
    item("Subscriptions", summarizeSubscriptionCount(meta)) +
    `<div class="rbs-counts">
       ${chip(" services", svcCount, svcCount === 0)}
       ${chip(" regions", rgCount === 0 ? "all" : rgCount)}
       ${chip(" SKU families", skuCount, skuCount === 0)}
       ${chip(" total required cores", totalCores)}
       <span class="rbs-chip" title="${escapeHtml(updated)}">Updated by <strong>${escapeHtml(meta.bom_updated_by || "—")}</strong></span>
     </div>`;
  summary.classList.remove("hidden");
}

async function openRunModal() {
  document.getElementById("run-overlay").classList.remove("hidden");
  document.getElementById("run-modal").classList.remove("hidden");
  document.getElementById("run-status").innerHTML = "";
  resetRunProgress();
  document.getElementById("run-go").disabled = true;

  // Ensure we have an up-to-date list of saved BOMs (a save in another tab,
  // or fresh import, should appear in the picker without a full page reload).
  await refreshSubMetadataIndex();
  populateBomPicker(STATE.activeBomId || "");

  // Refresh sign-in status silently so the header chip stays current; sign-in
  // is managed from the account chip's modal, not here.
  refreshAuthToken({ force: false });
}

function closeRunModal() {
  document.getElementById("run-overlay").classList.add("hidden");
  document.getElementById("run-modal").classList.add("hidden");
  stopRunProgressPolling();
  if (TOKEN.refreshTimer) { clearTimeout(TOKEN.refreshTimer); TOKEN.refreshTimer = null; }
}

function setRunStatus(html, kind = "info") {
  const el = document.getElementById("run-status");
  el.dataset.kind = kind;
  el.innerHTML = html;
}

// ----- Run progress bar (polls /api/run_progress while POST is in flight) ----

const RUN_PROGRESS = { token: null, timer: null, lastPercent: 0 };

function resetRunProgress() {
  stopRunProgressPolling();
  RUN_PROGRESS.token = null;
  RUN_PROGRESS.lastPercent = 0;
  const wrap = document.getElementById("run-progress");
  if (!wrap) return;
  wrap.classList.add("hidden");
  wrap.removeAttribute("data-state");
  document.getElementById("run-progress-phase").textContent = "Initializing…";
  document.getElementById("run-progress-pct").textContent = "0%";
  const fill = document.getElementById("run-progress-fill");
  fill.style.width = "0%";
  const aria = document.getElementById("run-progress-bar-aria");
  aria.setAttribute("aria-valuenow", "0");
  document.getElementById("run-progress-sub").textContent = "";
  document.getElementById("run-progress-eta").textContent = "";
}

function stopRunProgressPolling() {
  if (RUN_PROGRESS.timer) {
    clearInterval(RUN_PROGRESS.timer);
    RUN_PROGRESS.timer = null;
  }
}

function showRunProgress(token) {
  RUN_PROGRESS.token = token;
  RUN_PROGRESS.lastPercent = 0;
  const wrap = document.getElementById("run-progress");
  wrap.classList.remove("hidden");
  wrap.dataset.state = "running";
  document.getElementById("run-progress-phase").textContent = "Submitting…";
  document.getElementById("run-progress-pct").textContent = "0%";
  document.getElementById("run-progress-fill").style.width = "2%";
  document.getElementById("run-progress-bar-aria").setAttribute("aria-valuenow", "0");
  document.getElementById("run-progress-sub").textContent = "Waiting for backend to register the run…";
  document.getElementById("run-progress-eta").textContent = "";
}

function formatEtaSeconds(s) {
  if (s == null || !Number.isFinite(s) || s < 0) return "";
  s = Math.round(s);
  if (s < 60) return `~${s}s remaining`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `~${m}m ${rs}s remaining`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `~${h}h ${rm}m remaining`;
}

function renderRunProgress(state) {
  const wrap = document.getElementById("run-progress");
  if (!wrap || wrap.classList.contains("hidden")) return;
  if (!state || state.found === false) {
    // Backend hasn't registered the run yet — leave the "submitting" copy in
    // place so the bar doesn't flicker between "initializing" and real data.
    return;
  }
  // Monotonic guard on percent so polling races never make the bar regress.
  let pct = Number(state.percent);
  if (!Number.isFinite(pct)) pct = 0;
  pct = Math.max(0, Math.min(100, pct));
  if (state.status === "running") pct = Math.max(pct, RUN_PROGRESS.lastPercent);
  RUN_PROGRESS.lastPercent = pct;
  wrap.dataset.state = state.status || "running";
  const phaseLabel = state.current_phase_label || "Working…";
  document.getElementById("run-progress-phase").textContent = phaseLabel;
  document.getElementById("run-progress-pct").textContent = `${Math.round(pct)}%`;
  document.getElementById("run-progress-fill").style.width = `${pct}%`;
  document.getElementById("run-progress-bar-aria").setAttribute("aria-valuenow", String(Math.round(pct)));
  // Sub-text: completed/total when the phase has items, else elapsed.
  let sub = "";
  if (Number.isFinite(Number(state.total)) && Number(state.total) > 0) {
    sub = `${state.completed || 0} / ${state.total} regions`;
  } else if (Number.isFinite(Number(state.elapsed_seconds))) {
    const e = Number(state.elapsed_seconds);
    sub = e < 60 ? `${e}s elapsed` : `${Math.floor(e / 60)}m ${e % 60}s elapsed`;
  }
  document.getElementById("run-progress-sub").textContent = sub;
  document.getElementById("run-progress-eta").textContent = formatEtaSeconds(state.eta_seconds);
}

async function pollRunProgress() {
  if (!RUN_PROGRESS.token) return;
  try {
    const res = await apiFetch(`/api/run_progress?token=${encodeURIComponent(RUN_PROGRESS.token)}`);
    if (!res.ok) return; // transient — keep polling
    const state = await res.json();
    renderRunProgress(state);
  } catch (e) {
    // Network blip; don't break the polling loop.
  }
}

function startRunProgressPolling(token) {
  stopRunProgressPolling();
  showRunProgress(token);
  // Kick off an immediate poll so the bar populates fast, then poll every 2s.
  pollRunProgress();
  RUN_PROGRESS.timer = setInterval(pollRunProgress, 2000);
}

async function submitRun() {
  const bomId = document.getElementById("run-bom-pick").value.trim();

  if (!bomId) {
    return setRunStatus(
      "Select a BOM plan above. If you don't have one yet, open <strong>Edit BOM</strong> in the header to create one.",
      "error",
    );
  }
  const meta = getBomMeta(bomId);
  if (!meta) {
    return setRunStatus(
      "That BOM is no longer available — it may have been deleted. Pick another or refresh.",
      "error",
    );
  }
  const subIds = subscriptionList(meta);
  const sub = (primarySubscriptionId(meta) || "").trim();
  if (!GUID_RE.test(sub)) {
    // Shouldn't happen because BOM subs are validated on save, but defend.
    return setRunStatus("Selected BOM has an invalid subscription ID.", "error");
  }

  const info = TOKEN.info && TOKEN.info.expires_in_seconds > 60
    ? TOKEN.info
    : await refreshAuthToken({ force: true });
  if (!info) {
    return setRunStatus(
      "Not signed in. Click <strong>Sign in</strong> above and complete the ARM sign-in flow.",
      "error",
    );
  }

  document.getElementById("run-go").disabled = true;
  setRunStatus(
    "Submitting… reading BOM, querying ARM SKU availability, and compiling the model.",
    "info",
  );

  const progressToken = (typeof crypto !== "undefined" && crypto.randomUUID)
    ? crypto.randomUUID()
    : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}-4000-8000-${Math.random().toString(16).slice(2, 14).padEnd(12, "0")}`;
  startRunProgressPolling(progressToken);

  // Derive customer metadata from the selected BOM — the BOM editor is
  // the source of truth.
  const customer = (meta.customer_name || "").trim();

  const fd = new FormData();
  fd.append("subscription_id", sub);
  if (subIds.length > 1) fd.append("subscription_ids", subIds.join(","));
  fd.append("bom_id", bomId);
  fd.append("use_saved_bom", "true");
  if (customer) fd.append("customer_name", customer);
  fd.append("progress_token", progressToken);

  let result;
  try {
    const res = await apiFetch("/api/runs", { method: "POST", body: fd });
    const raw = await res.text();
    let parseFailed = false;
    try {
      result = raw ? JSON.parse(raw) : {};
    } catch (parseErr) {
      result = {};
      parseFailed = true;
    }
    if (!res.ok) {
      stopRunProgressPolling();
      if (parseFailed || (!result.error && !result.message)) {
        const bodyDesc = raw
          ? `body: <code>${escapeHtml(raw.slice(0, 400))}</code>`
          : "empty body";
        setRunStatus(
          `Server returned <strong>${res.status} ${res.statusText}</strong> with ${bodyDesc}. ` +
          `This usually means the Python worker crashed before our handler could format an error. ` +
          `Check the terminal where <code>start-local.ps1</code> is running for a traceback.`,
          "error",
        );
      } else {
        setRunStatus(errLine(result, res.statusText), "error");
      }
      document.getElementById("run-go").disabled = false;
      return;
    }
  } catch (e) {
    stopRunProgressPolling();
    setRunStatus(netErrLine(e), "error");
    document.getElementById("run-go").disabled = false;
    return;
  }

  // POST returned 2xx — backend has fully finished. One final poll picks up
  // the terminal "succeeded" state so the bar fills cleanly, then we stop.
  await pollRunProgress();
  stopRunProgressPolling();

  const skuLine = formatSkusLine(result.skus_source, result.skus_resolved);
  setRunStatus(
    `✅ Snapshot generated (run ${escapeHtml(result.run_id)}).` +
    (skuLine ? `<br><span class="muted">${skuLine}</span>` : "") +
    " Loading…",
    "ok",
  );
  STATE.activeBomId = bomId;
  STATE.activeSubscription = null;
  syncActiveSubscription();
  try { localStorage.setItem("activeBomId", bomId); } catch (e) {}
  markActiveBomNav();
  renderBomPanel();
  await loadSnapshotsList();
  document.getElementById("snapshot-picker").value = result.run_id;
  await loadSnapshot(result.run_id);
  await _restoreQuotaRequestsFromDb();
  setTimeout(closeRunModal, 1200);
}

function formatSkusLine(source, resolved) {
  if (!Array.isArray(resolved) || resolved.length === 0) return "";
  const labels = resolved.map(e => {
    const pl = (e.primary_label || "").trim() || (e.primary_family || "?");
    const al = (e.alt_label || "").trim();
    return al ? `${pl}\u2192${al}` : pl;
  });
  const tag = source === "modal_override"
    ? "Modal override"
    : (source === "bom_sheet" ? "BOM sheet" : "Default skus.txt");
  return `SKU families (${escapeHtml(tag)}): ${escapeHtml(labels.join(", "))}`;
}

// ---------------------------------------------------------------- Getting Started
//
// A clear, one-step-at-a-time walkthrough (replaces the old wall-of-text guide).
// Each step has plain-language copy plus a real action button that DOES the thing
// — sign in, open Settings, create a BOM, or load sample data — so the guide
// removes barriers instead of just describing them. Auto-opens once on first
// visit; re-openable any time from the header "?" button.

const GS_SEEN_KEY = "gs_tour_seen";
function _gsTourSeen() {
  try { return localStorage.getItem(GS_SEEN_KEY) === "1"; } catch (_e) { return false; }
}
function _gsTourMarkSeen() {
  try { localStorage.setItem(GS_SEEN_KEY, "1"); } catch (_e) {}
}

// Build the step list fresh each render so sign-in state is reflected live.
function gettingStartedSteps() {
  const signedIn = _isSignedIn();
  const who = signedIn ? (TOKEN.info.az_user || "your account") : "";
  const demo = !!(APP_CONFIG && APP_CONFIG.demo_mode);

  const signInAction = {
    label: signedIn ? "Switch account" : "Sign in to Azure",
    className: signedIn ? "btn btn--sm" : "btn btn--accent btn--sm",
    run: () => { openSigninModal(); refreshAuthToken({ force: true }); },
  };

  const steps = [
    {
      title: "Sign in to Azure",
      body:
        `<p>A one-time browser sign-in mints a <strong>read-only ARM token</strong> so the ` +
        `dashboard can read SKU, region, and quota data. Nothing about the customer is ` +
        `stored on the server.</p>` +
        `<p class="muted">You need <em>Reader</em> on the customer's subscription — or have ` +
        `the customer run the dashboard in their own tenant (same steps, their sign-in).</p>` +
        (signedIn ? `<p class="gs-ok">✓ Signed in as <strong>${escapeHtml(who)}</strong>.</p>` : ""),
      actions: [signInAction],
    },
    {
      title: "Refresh your Azure data",
      body:
        `<p>Open <strong>Settings</strong> to set your <strong>support-ticket owner</strong> and ` +
        `refresh the <strong>region, latency, and SKU</strong> datasets, so every analysis uses ` +
        `the latest Azure data.</p>` +
        `<p class="muted">Choose <strong>Open Settings & guide me</strong> and arrows will walk you ` +
        `through exactly what to fill in.</p>`,
      actions: [{
        label: "⚙ Open Settings & guide me",
        className: "btn btn--accent btn--sm",
        run: () => {
          _setOnboardSettingsDone();
          dismissSettingsCoach(true);
          switchView("settings");
          setTimeout(() => startSettingsCoachTour(), 350);
        },
        close: true,
      }],
    },
    {
      title: "Create a BOM (Bill of Materials)",
      body:
        `<p>A <strong>BOM</strong> describes what the customer deploys: the Azure ` +
        `<strong>services</strong>, the VM <strong>SKU families</strong>, and <strong>required ` +
        `cores</strong>. Name it, pick the subscription(s), then choose services, regions, and SKUs.</p>` +
        `<p class="muted">Choose <strong>New BOM & guide me</strong> and arrows will point at each ` +
        `field as you go.</p>`,
      actions: [{
        label: "+ New BOM & guide me",
        className: "btn btn--accent btn--sm",
        run: () => { openBomModal(null, { create: true, guide: true }); },
        close: true,
      }],
    },
    {
      title: "Run the analysis",
      body:
        `<p>Select your BOM in the <strong>Bills of Materials</strong> list on the left, then click ` +
        `<strong>▶ Refresh analysis</strong>. A live progress bar streams SKU availability across ` +
        `<strong>~38 Azure regions</strong> — usually a few minutes for a full run.</p>`,
      actions: [],
    },
    {
      title: "Explore results & clear blockers",
      body:
        `<p>Use the tabs to explore your results: <strong>Overview</strong> KPIs, per-region ` +
        `<strong>Table</strong>, <strong>Map</strong>, <strong>Latency</strong>, <strong>Compare</strong>, ` +
        `and <strong>Best regions</strong>.</p>` +
        `<p>Where <strong>quota</strong> or <strong>zonal access</strong> blocks a region, you can ` +
        `<strong>open, submit, and track an Azure support ticket</strong> right from the dashboard.</p>`,
      // Offer the sample-data escape hatch here only when there's nothing to look at yet.
      actions: (demo || signedIn) ? [] : [{
        label: "▶ Explore with sample data",
        className: "btn btn--sm",
        run: () => loadSampleData(),
        close: true,
      }],
    },
  ];
  return steps;
}

// Reference to the Getting Started guide opener (set in setupGettingStarted).
// Lets coach-mark tours return the user to the guide after a hand-off task.
let _gsOpenAt = null;
function reopenGettingStarted(stepIdx) {
  if (typeof _gsOpenAt === "function") { try { _gsOpenAt(stepIdx || 0); } catch (_e) {} }
}

function setupGettingStarted() {
  const openBtn = document.getElementById("open-guide");
  const modal = document.getElementById("guide-modal");
  const overlay = document.getElementById("guide-overlay");
  const closeBtn = document.getElementById("guide-modal-close");
  const stepHost = document.getElementById("gs-tour-step");
  const dotsHost = document.getElementById("gs-tour-dots");
  const backBtn = document.getElementById("gs-tour-back");
  const nextBtn = document.getElementById("gs-tour-next");
  const skipBtn = document.getElementById("gs-tour-skip");
  if (!openBtn || !modal || !overlay || !stepHost || !dotsHost || !nextBtn) return;

  let idx = 0;

  const close = () => {
    overlay.classList.add("hidden");
    modal.classList.add("hidden");
    _gsTourMarkSeen();
  };
  const open = (start) => {
    idx = start || 0;
    render();
    overlay.classList.remove("hidden");
    modal.classList.remove("hidden");
  };

  function render() {
    const steps = gettingStartedSteps();
    idx = Math.max(0, Math.min(idx, steps.length - 1));
    const step = steps[idx];

    dotsHost.innerHTML = steps.map((s, i) =>
      `<button type="button" class="gs-dot${i === idx ? " is-active" : ""}${i < idx ? " is-done" : ""}" ` +
      `data-goto="${i}" role="tab" aria-selected="${i === idx}" ` +
      `title="Step ${i + 1}: ${escapeHtml(s.title)}"><span>${i + 1}</span></button>`
    ).join("");
    dotsHost.querySelectorAll("[data-goto]").forEach(d =>
      d.addEventListener("click", () => { idx = Number(d.dataset.goto); render(); }));

    stepHost.innerHTML =
      `<div class="gs-tour-count">Step ${idx + 1} of ${steps.length}</div>` +
      `<h3 class="gs-tour-title">${escapeHtml(step.title)}</h3>` +
      `<div class="gs-tour-copy">${step.body}</div>` +
      `<div class="gs-tour-actions" id="gs-tour-actions"></div>`;

    const acts = stepHost.querySelector("#gs-tour-actions");
    (step.actions || []).forEach(a => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = a.className || "btn btn--sm";
      b.textContent = a.label;
      b.addEventListener("click", () => {
        try { if (a.run) a.run(); } finally { if (a.close) close(); }
      });
      acts.appendChild(b);
    });

    if (backBtn) backBtn.disabled = idx === 0;
    nextBtn.textContent = idx === steps.length - 1 ? "Done" : "Next →";
  }

  if (backBtn) backBtn.addEventListener("click", () => { idx = Math.max(0, idx - 1); render(); });
  nextBtn.addEventListener("click", () => {
    const steps = gettingStartedSteps();
    if (idx >= steps.length - 1) { close(); }
    else { idx += 1; render(); }
  });
  if (skipBtn) skipBtn.addEventListener("click", close);
  openBtn.addEventListener("click", () => open(0));
  if (closeBtn) closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", close);

  // Expose the opener so coach tours can bring the user back to the guide when
  // they finish a hand-off task (e.g. after the Settings walkthrough).
  _gsOpenAt = open;

  // First-visit auto-open removes the discovery barrier — new users land
  // straight in the guided flow. Only once; the "?" button reopens it later.
  if (!_gsTourSeen()) {
    setTimeout(() => { if (!_gsTourSeen()) open(0); }, 700);
  }
}

// ---------------------------------------------------------------- Settings: activity log

const ACTIVITY_LOG = {
  knownEventTypes: [],
  populatedSubFilter: false,
  loading: false,
};

function _fmtActivityTime(iso) {
  if (!iso) return "—";
  // Render "2026-05-19 11:20:32 UTC" — short and unambiguous.
  return String(iso).replace("T", " ").replace(/\+00:?00$/, "").replace(/Z$/, "") + " UTC";
}

function _fmtDuration(ms) {
  if (ms == null || ms === "") return "—";
  const n = Number(ms);
  if (!Number.isFinite(n)) return "—";
  if (n < 1000) return `${n} ms`;
  return `${(n / 1000).toFixed(2)} s`;
}

function _fmtShortSub(sub) {
  if (!sub) return "—";
  if (sub.length > 13 && sub.includes("-")) {
    return `${sub.slice(0, 8)}…${sub.slice(-4)}`;
  }
  return sub;
}

function _populateActivityFilters(data) {
  const eventSel = document.getElementById("activity-filter-event");
  if (eventSel && !ACTIVITY_LOG.knownEventTypes.length) {
    ACTIVITY_LOG.knownEventTypes = data.known_event_types || [];
    const prev = eventSel.value;
    eventSel.innerHTML = `<option value="">All</option>` +
      ACTIVITY_LOG.knownEventTypes.map(t =>
        `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
    if (prev) eventSel.value = prev;
  }

  const subSel = document.getElementById("activity-filter-sub");
  if (subSel && !ACTIVITY_LOG.populatedSubFilter) {
    const subs = new Set();
    for (const ev of data.events || []) {
      if (ev.subscription_id) subs.add(ev.subscription_id);
    }
    for (const s of (STATE.snapshots || [])) {
      if (s && s.subscription_id) subs.add(s.subscription_id);
    }
    if (activeSubscriptionId()) subs.add(activeSubscriptionId());
    const prev = subSel.value;
    const sorted = Array.from(subs).sort();
    subSel.innerHTML = `<option value="">All</option>` +
      sorted.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
    if (prev) subSel.value = prev;
    ACTIVITY_LOG.populatedSubFilter = sorted.length > 0;
  }
}

function _renderActivityRow(ev) {
  const tr = document.createElement("tr");
  const statusKey = (ev.status || "info").toLowerCase();
  const scopeKey = (ev.api_scope || "local").toLowerCase();
  const detailsCell = ev.details_json
    ? `<details><summary>Details</summary><pre class="mono">${escapeHtml(_prettyJson(ev.details_json))}</pre></details>`
    : "";
  tr.innerHTML = `
    <td class="nowrap mono">${escapeHtml(_fmtActivityTime(ev.timestamp_iso))}</td>
    <td class="mono">${escapeHtml(ev.event_type || "?")}</td>
    <td><span class="activity-pill" data-status="${escapeHtml(statusKey)}">${escapeHtml(statusKey)}</span></td>
    <td><span class="activity-scope-pill" data-scope="${escapeHtml(scopeKey)}">${escapeHtml(scopeKey)}</span></td>
    <td class="mono" title="${escapeHtml(ev.subscription_id || "")}">${escapeHtml(_fmtShortSub(ev.subscription_id))}</td>
    <td class="mono">${escapeHtml(ev.run_id ? ev.run_id.slice(0, 8) : "—")}</td>
    <td>${escapeHtml(ev.actor_email || "—")}</td>
    <td class="nowrap">${escapeHtml(_fmtDuration(ev.duration_ms))}</td>
    <td class="truncate">${escapeHtml(ev.message || "")}${detailsCell}</td>`;
  return tr;
}

function _prettyJson(s) {
  if (!s) return "";
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch (_) {
    return String(s);
  }
}

async function loadActivityLog() {
  if (ACTIVITY_LOG.loading) return;
  ACTIVITY_LOG.loading = true;
  const status = document.getElementById("activity-status");
  const tbody = document.querySelector("#activity-table tbody");
  if (status) status.textContent = "Loading…";

  const params = new URLSearchParams();
  const limit = document.getElementById("activity-filter-limit")?.value || "200";
  const days = document.getElementById("activity-filter-days")?.value || "7";
  const sub = document.getElementById("activity-filter-sub")?.value || "";
  const ev = document.getElementById("activity-filter-event")?.value || "";
  params.set("limit", limit);
  params.set("max_days", days);
  if (sub) params.set("subscription_id", sub);
  if (ev) params.set("event_type", ev);

  let data;
  try {
    data = await apiJson(`/api/activity_log?${params.toString()}`);
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    if (tbody) tbody.innerHTML = `<tr><td colspan="9" class="muted">Failed to load activity log: ${escapeHtml(e.message)}</td></tr>`;
    ACTIVITY_LOG.loading = false;
    return;
  }

  _populateActivityFilters(data);

  if (tbody) {
    tbody.innerHTML = "";
    if (!data.events || !data.events.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted">No events in the last ${escapeHtml(days)} day(s).</td></tr>`;
    } else {
      const frag = document.createDocumentFragment();
      for (const ev of data.events) frag.appendChild(_renderActivityRow(ev));
      tbody.appendChild(frag);
    }
  }

  if (status) {
    const stamp = new Date().toLocaleTimeString();
    status.textContent = `${data.count} event(s) · refreshed ${stamp}`;
  }
  ACTIVITY_LOG.loading = false;
}

async function clearActivityLog() {
  if (!confirm("Clear the entire activity log? This drops the table and cannot be undone.")) return;
  const status = document.getElementById("activity-status");
  if (status) status.textContent = "Clearing…";
  try {
    await apiJson("/api/activity_log/clear", { method: "POST" });
    ACTIVITY_LOG.populatedSubFilter = false;  // re-derive sub filter after clear
    if (status) status.textContent = "Cleared.";
    await loadActivityLog();
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
  }
}

// ---------------------------------------------------------------- Support tickets

// Bootstrap config + support-feature state. APP_CONFIG is fetched once at boot
// and tells the SPA whether demo mode is on (which forces ticket dry-run and
// shows the demo banner).
let APP_CONFIG = { demo_mode: false, support_configured: false, snapshot_retention: 15 };
const SUPPORT = { settings: null, tickets: [], lastPreview: null, loaded: false };

// Fetch the global support/ticket-owner settings once (cached on SUPPORT).
// Used by the BOM wizard + gear Settings, which may run before the Tickets tab
// has ever been opened.
async function ensureSupportSettings() {
  if (SUPPORT.settings) return SUPPORT.settings;
  try {
    const s = await apiJson("/api/support/settings");
    SUPPORT.settings = s.settings;
    if (typeof APP_CONFIG === "object" && APP_CONFIG) APP_CONFIG.support_configured = s.configured;
  } catch (e) { /* non-fatal */ }
  return SUPPORT.settings;
}

// The BOM's support contact is the profile Azure tickets for that BOM are filed
// under. It is initialized from the global support settings but stored per-BOM
// (support_override) so each BOM can have different owners/contacts. These
// wizard field IDs map to the support_settings field names the backend merges.
const BOM_SUPPORT_FIELDS = {
  contact_first_name: "bom-owner-first",
  contact_last_name: "bom-owner-last",
  primary_email: "bom-owner-email",
  phone: "bom-owner-phone",
  country: "bom-owner-country",
  preferred_timezone: "bom-owner-tz",
  preferred_contact_method: "bom-owner-method",
  default_severity: "bom-owner-sev",
  additional_emails: "bom-owner-cc",
};

const _BOM_SUPPORT_DEFAULTS = {
  preferred_contact_method: "email",
  default_severity: "moderate",
};

// Prefill the wizard support fields from the GLOBAL settings (the per-BOM
// defaults). Called for both new and existing BOMs; for existing BOMs the
// BOM's own override is layered on top afterwards via _overlayBomSupportOverride.
function _prefillBomOwnerFields() {
  const s = SUPPORT.settings || {};
  for (const [key, id] of Object.entries(BOM_SUPPORT_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const v = s[key];
    el.value = (v !== undefined && v !== null && v !== "")
      ? v
      : (_BOM_SUPPORT_DEFAULTS[key] || "");
  }
}

// Layer a BOM's saved per-BOM override on top of the global-prefilled fields:
// only non-empty override values win, so unset fields keep inheriting global.
function _overlayBomSupportOverride(override) {
  if (!override || typeof override !== "object") return;
  for (const [key, id] of Object.entries(BOM_SUPPORT_FIELDS)) {
    const v = override[key];
    if (v === undefined || v === null || v === "") continue;
    const el = document.getElementById(id);
    if (el) el.value = v;
  }
}

// Collect the wizard support fields into a per-BOM override object. Only
// non-empty values are sent; empty fields are omitted so the BOM inherits the
// corresponding global default at ticket time.
function _collectBomSupportOverride() {
  const out = {};
  for (const [key, id] of Object.entries(BOM_SUPPORT_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const v = (el.value || "").trim();
    if (v) out[key] = v;
  }
  return out;
}

// Gear Settings → Ticket owner section.
// Switch the active panel within the Settings view. Lazy-loads each tab's
// data the first time (and on every re-open, so the content stays fresh).
function switchSettingsTab(tab) {
  const tabs = ["owner", "permissions", "datasets", "pricing", "activity", "data"];
  if (!tabs.includes(tab)) tab = "owner";
  STATE.settingsTab = tab;
  document.querySelectorAll("[data-settings-tab]").forEach(btn => {
    const active = btn.getAttribute("data-settings-tab") === tab;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-settings-panel]").forEach(p => {
    p.classList.toggle("is-active", p.getAttribute("data-settings-panel") === tab);
  });
  if (tab === "owner") loadOwnerSettings();
  else if (tab === "permissions") loadPermissionsSettings();
  else if (tab === "datasets") loadDatasetsSettings();
  else if (tab === "pricing") loadPricingSettings();
  else if (tab === "activity") loadActivityLog();
  else if (tab === "data") loadDataSettings();
}

// Render the "Data & storage" panel: the (mode-aware) storage-path line and
// the open-folder button, which only works when self-hosting locally.
function loadDataSettings() {
  const pathEl = document.getElementById("owner-storage-path");
  const isLocal = !!(APP_CONFIG && APP_CONFIG.local_mode);
  const pathLine = document.getElementById("owner-storage-path-line");
  const dir = (APP_CONFIG && (APP_CONFIG.snapshots_dir || APP_CONFIG.storage_dir)) || "";
  const dirText = dir || "(path unavailable — run a live, non-demo analysis to persist snapshots locally)";
  if (pathEl) pathEl.textContent = dirText;
  if (pathLine) {
    const lead = isLocal
      ? "Snapshots and local data are saved on this machine under:"
      : "Snapshots are stored in your private session for this hosted app (not a folder you can open). The server-side path is:";
    pathLine.innerHTML = `${escapeHtml(lead)}<br><code id="owner-storage-path">${escapeHtml(dirText)}</code>`;
  }
  const openBtn = document.getElementById("owner-open-folder");
  if (openBtn) openBtn.classList.toggle("hidden", !isLocal);
  // The browser backup gauge only applies to the hosted (delegated) mode where
  // localStorage is the durable store; hide it for the local desktop app.
  const gauge = document.getElementById("storage-usage-block");
  if (gauge) gauge.classList.toggle("hidden", !_stateSyncEnabled());
  const refreshBtn = document.getElementById("storage-usage-refresh");
  if (refreshBtn && !refreshBtn._wired) {
    refreshBtn._wired = true;
    refreshBtn.addEventListener("click", updateStorageGauge);
  }
  updateStorageGauge();
}

// Approximate the localStorage backup budget (~5MB, measured in UTF-16 code
// units) and render the current usage as a labelled progress bar. Called when
// the Data & storage tab opens and after every browser backup write.
const _BROWSER_BACKUP_BUDGET_BYTES = 5 * 1024 * 1024;
function updateStorageGauge() {
  const fill = document.getElementById("storage-usage-fill");
  const text = document.getElementById("storage-usage-text");
  if (!fill || !text) return;
  let usedBytes = 0;
  try {
    const raw = localStorage.getItem(_stateKey());
    // localStorage stores strings as UTF-16, so ~2 bytes per code unit.
    if (raw) usedBytes = raw.length * 2;
  } catch (_) {}
  const pct = Math.max(0, Math.min(100, (usedBytes / _BROWSER_BACKUP_BUDGET_BYTES) * 100));
  fill.style.width = pct.toFixed(1) + "%";
  fill.classList.toggle("is-warn", pct >= 75 && pct < 90);
  fill.classList.toggle("is-danger", pct >= 90);
  const bar = fill.parentElement;
  if (bar) bar.setAttribute("aria-valuenow", Math.round(pct));
  const fmt = (b) => b >= 1024 * 1024
    ? (b / (1024 * 1024)).toFixed(2) + " MB"
    : Math.max(1, Math.round(b / 1024)) + " KB";
  let msg = `${fmt(usedBytes)} of ~5 MB used (${pct.toFixed(0)}%).`;
  if (pct >= 90) msg += " Backup is nearly full — download a .zip below to preserve your data.";
  else if (pct >= 75) msg += " Getting full — consider downloading a .zip backup.";
  else msg += " Plenty of room for your BOMs and analysis history.";
  text.textContent = msg;
}

async function loadOwnerSettings() {
  await ensureSupportSettings();
  const s = SUPPORT.settings || {};
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  set("owner-first", s.contact_first_name || "");
  set("owner-last", s.contact_last_name || "");
  set("owner-email", s.primary_email || "");
  set("owner-cc", s.additional_emails || "");
  set("owner-phone", s.phone || "");
  set("owner-country", s.country || "US");
  set("owner-tz", s.preferred_timezone || "Pacific Standard Time");
  set("owner-sev", s.default_severity || "moderate");
  set("owner-valrg", _valRgForSub(focusedSubscriptionId()));
  const subLabelEl = document.getElementById("owner-valrg-sub");
  if (subLabelEl) {
    const subName = focusedSubscriptionName();
    subLabelEl.textContent = subName
      ? `Applies to the selected subscription: ${subName}`
      : "Optional — pick a subscription on the dashboard first, then set a resource group for it here.";
  }
  _loadValidationRgOptions();
}

// Populate the validation-RG datalist from the focused subscription so the user
// picks an existing RG instead of typing one that may not exist (which 404s at
// validate time and shows a misleading "not configured" state).
async function _loadValidationRgOptions() {
  const list = document.getElementById("owner-valrg-list");
  const hint = document.getElementById("owner-valrg-hint");
  // Populate the location datalist from the BOM's regions so a created RG can be
  // homed in a familiar region (RG location is metadata only — the deep check
  // validates resources in any region regardless of the RG's location).
  const locList = document.getElementById("owner-valrg-loc-list");
  if (locList) {
    const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
    const shorts = Array.from(new Set(regions.map(r => r.short).filter(Boolean))).sort();
    locList.innerHTML = shorts.map(s => `<option value="${escapeHtml(s)}"></option>`).join("");
    const locInput = document.getElementById("owner-valrg-loc");
    if (locInput && !locInput.value && shorts.length) locInput.value = shorts[0];
  }
  if (!list) return;
  const sub = focusedSubscriptionId() || "";
  const subName = focusedSubscriptionName() || sub;
  if (!sub) {
    if (hint) hint.textContent = "Select a subscription on the dashboard to load its resource groups.";
    return;
  }
  if (hint) hint.textContent = `Loading resource groups from ${subName}…`;
  try {
    const resp = await apiJson(`/api/az/resource-groups?subscription_id=${encodeURIComponent(sub)}`);
    const rgs = (resp && resp.resource_groups) || [];
    list.innerHTML = rgs.map(g =>
      `<option value="${escapeHtml(g.name)}">${escapeHtml(g.location || "")}</option>`).join("");
    if (hint) hint.textContent = rgs.length
      ? `${rgs.length} resource group(s) in ${subName}. The deep check runs against this subscription — the one selected on the dashboard.`
      : `No resource groups in ${subName}. Create one below, or leave blank for read-only checks.`;
  } catch (e) {
    if (hint) hint.textContent = "Could not load resource groups (you can still type a name).";
  }
}

// Create the validation resource group named in the field if it doesn't exist.
// A resource group is free and empty until resources are deployed — the deep
// check only validates against it. Gated by an explicit confirmation.
async function _createValidationRg() {
  const status = document.getElementById("owner-valrg-create-status");
  const name = ((document.getElementById("owner-valrg") || {}).value || "").trim();
  const location = ((document.getElementById("owner-valrg-loc") || {}).value || "").trim();
  const sub = focusedSubscriptionId() || "";
  const subName = focusedSubscriptionName() || sub;
  if (!sub) { if (status) status.textContent = "Select a subscription first."; return; }
  if (!name) { if (status) status.textContent = "Enter a resource group name above first."; return; }
  if (!location) { if (status) status.textContent = "Enter a location (e.g. eastus)."; return; }
  const ok = window.confirm(
    `Create resource group "${name}" in ${location}\n` +
    `in subscription: ${subName}?\n\n` +
    `This is a free, empty resource group used only so the deep check has somewhere to run Azure ` +
    `pre-flight validation. No resources are deployed and nothing is billed. Requires Contributor on the subscription.`
  );
  if (!ok) return;
  const btn = document.getElementById("owner-valrg-create");
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Creating…";
  try {
    const res = await apiJson("/api/az/resource-groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription_id: sub, name, location }),
    });
    if (status) status.textContent = res.created ? `✓ Created in ${res.location}` : `✓ Already exists in ${res.location}`;
    // Persist it as the validation RG for THIS subscription so the deep check
    // uses it immediately (an RG only exists inside one subscription).
    try {
      const saved = await apiJson("/api/support/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ validation_resource_groups: { [sub]: res.name } }),
      });
      SUPPORT.settings = saved.settings;
    } catch (_e) {}
    await _loadValidationRgOptions();
    showToast(res.created ? "Validation resource group created and saved." : "Resource group already existed — saved as your validation RG.", "success");
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    showToast(e.message || "Could not create resource group.", "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveOwnerSettings() {
  const status = document.getElementById("owner-status");
  const val = (id) => ((document.getElementById(id) || {}).value || "").trim();
  const body = {
    contact_first_name: val("owner-first"),
    contact_last_name: val("owner-last"),
    primary_email: val("owner-email"),
    additional_emails: val("owner-cc"),
    phone: val("owner-phone"),
    country: val("owner-country") || "US",
    preferred_timezone: val("owner-tz"),
    default_severity: (document.getElementById("owner-sev") || {}).value || "moderate",
  };
  // The validation RG is per-subscription. Only persist it when a subscription
  // is focused; an empty value clears that subscription's entry server-side.
  const valSub = focusedSubscriptionId();
  if (valSub) body.validation_resource_groups = { [valSub]: val("owner-valrg") };
  if (status) status.textContent = "Saving…";
  try {
    const res = await apiJson("/api/support/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    SUPPORT.settings = res.settings;
    if (typeof APP_CONFIG === "object" && APP_CONFIG) APP_CONFIG.support_configured = res.configured;
    if (status) status.textContent = res.configured ? "✓ Saved" : "Saved (name + email needed to submit tickets)";
    showToast("Ticket owner saved.", "success");
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
  }
}

// ---------------------------------------------------------------- Permissions

// Populate the subscription picker (reusing the already-loaded list) and default
// it to whatever subscription is focused on the dashboard.
async function loadPermissionsSettings() {
  const sel = document.getElementById("perm-sub");
  if (!sel) return;
  let subs = window._loadedSubscriptions || [];
  if (!subs.length) {
    sel.innerHTML = '<option disabled selected>Loading subscriptions…</option>';
    try {
      const r = await apiJson("/api/az/subscriptions");
      subs = r.subscriptions || [];
      window._loadedSubscriptions = subs;
    } catch (_e) { subs = []; }
  }
  if (!subs.length) {
    sel.innerHTML = '<option disabled selected>No subscriptions found — sign in first.</option>';
    return;
  }
  const focused = focusedSubscriptionId();
  sel.innerHTML = subs.map(s =>
    `<option value="${escapeHtml(s.id)}"${s.id === focused ? " selected" : ""}>${escapeHtml(s.name)} (${s.id.substring(0, 8)}…)</option>`
  ).join("");
}

async function checkPermissions() {
  const sel = document.getElementById("perm-sub");
  const statusEl = document.getElementById("perm-status");
  const summaryEl = document.getElementById("perm-summary");
  const resultsEl = document.getElementById("perm-results");
  const btn = document.getElementById("perm-check");
  const subId = sel && sel.value;
  if (!subId) {
    if (statusEl) statusEl.textContent = "Pick a subscription first.";
    return;
  }
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = "Checking…";
  if (summaryEl) { summaryEl.classList.add("hidden"); summaryEl.innerHTML = ""; }
  if (resultsEl) resultsEl.innerHTML = '<p class="muted">Reading your effective permissions…</p>';
  try {
    const r = await apiJson(`/api/permissions/check?subscription_id=${encodeURIComponent(subId)}`);
    if (statusEl) statusEl.textContent = "";
    renderPermissionResults(r);
  } catch (e) {
    if (statusEl) statusEl.textContent = "";
    if (summaryEl) summaryEl.classList.add("hidden");
    if (resultsEl) {
      resultsEl.innerHTML =
        `<p class="note warn">Couldn't complete the permission check: ${escapeHtml(e.message || String(e))}</p>`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _permRow(cap) {
  const granted = cap.granted;
  const badge = granted
    ? `<span class="perm-badge perm-verified">✓ Verified</span>`
    : `<span class="perm-badge perm-check">✕ Check</span>`;
  const tag = cap.required
    ? `<span class="perm-tag perm-required">Required</span>`
    : `<span class="perm-tag perm-optional">Optional</span>`;
  const acts = (cap.actions || []).map(a => {
    const isMissing = (cap.missing || []).includes(a);
    return `<code class="perm-action${isMissing ? " perm-action-missing" : ""}">${escapeHtml(a)}</code>`;
  }).join(" ");
  return `<li class="perm-row${granted ? "" : " perm-row-missing"}">
    <div class="perm-row-head">${badge}${tag}<strong class="perm-row-title">${escapeHtml(cap.title)}</strong></div>
    <div class="perm-row-why muted">${escapeHtml(cap.why)}</div>
    <div class="perm-row-actions">${acts}</div>
  </li>`;
}

function renderPermissionResults(data) {
  const summaryEl = document.getElementById("perm-summary");
  const resultsEl = document.getElementById("perm-results");
  const caps = (data && data.capabilities) || [];
  const sum = (data && data.summary) || {};
  if (summaryEl) {
    const reqOk = sum.all_required_ok;
    const cls = reqOk ? "perm-summary-ok" : "perm-summary-warn";
    const icon = reqOk ? "✅" : "⚠️";
    const headline = reqOk
      ? "All required permissions verified"
      : `${sum.required_total - sum.required_ok} required permission(s) missing`;
    summaryEl.className = `perm-summary ${cls}`;
    summaryEl.innerHTML = `<span class="perm-summary-icon">${icon}</span>
      <div><strong>${escapeHtml(headline)}</strong>
      <div class="muted">Required ${sum.required_ok}/${sum.required_total} · Optional ${sum.optional_ok}/${sum.optional_total} verified.
      ${reqOk ? "Optional gaps only limit automation features, not the core analysis." : "The core analysis needs every required capability — ask an owner for the missing role (e.g. Reader)."}</div></div>`;
    summaryEl.classList.remove("hidden");
  }
  if (resultsEl) {
    const required = caps.filter(c => c.required);
    const optional = caps.filter(c => !c.required);
    const section = (title, rows) => rows.length
      ? `<h3 class="perm-group-title">${title}</h3><ul class="perm-list">${rows.map(_permRow).join("")}</ul>`
      : "";
    resultsEl.innerHTML =
      section("Required for core analysis", required) +
      section("Optional — automation features", optional);
  }
}

// ---------------------------------------------------------------- Model datasets

function _fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function _fmtDatasetDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch (e) { return iso; }
}

async function loadDatasetsSettings() {
  const host = document.getElementById("datasets-list");
  if (!host) return;
  host.innerHTML = `<p class="muted">Loading datasets…</p>`;
  let datasets = [];
  try {
    const res = await apiJson("/api/datasets");
    datasets = (res && res.datasets) || [];
  } catch (e) {
    host.innerHTML = `<p class="muted">❌ Could not load datasets: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!datasets.length) {
    host.innerHTML = `<p class="muted">No managed datasets.</p>`;
    return;
  }
  host.innerHTML = datasets.map(_datasetCardHtml).join("");
  for (const ds of datasets) _wireDatasetCard(ds);
}

function _datasetSourceLine(ds) {
  const origin = ds.origin || (ds.source === "custom" ? "upload" : "builtin");
  let label, cls;
  if (origin === "arm") { label = "Azure ARM"; cls = "ds-src--arm"; }
  else if (origin === "url") { label = "Linked URL"; cls = "ds-src--url"; }
  else if (origin === "upload") { label = "Manual upload"; cls = "ds-src--upload"; }
  else { label = "Built-in seed"; cls = "ds-src--builtin"; }
  const bits = [`<span class="ds-src ${cls}">${escapeHtml(label)}</span>`];
  if (ds.fetched_at) bits.push(`<span>fetched ${_fmtDatasetDate(ds.fetched_at)}</span>`);
  if (ds.source_url) {
    const u = escapeHtml(ds.source_url);
    bits.push(`<a href="${u}" target="_blank" rel="noopener" class="ds-src-url" title="${u}">${u}</a>`);
  }
  return `<div class="dataset-source muted">${bits.join(" · ")}</div>`;
}

function _datasetCardHtml(ds) {
  const custom = ds.source === "custom";
  const badge = custom
    ? `<span class="ds-badge ds-badge--custom">Custom</span>`
    : `<span class="ds-badge ds-badge--builtin">Built-in</span>`;
  const summary = ds.summary ? escapeHtml(ds.summary) : "";
  const canArm = !!ds.can_refresh_arm;
  const hasUrl = !!ds.source_url;
  const id = escapeHtml(ds.id);
  // Primary "get fresh data" actions.
  const armBtn = canArm
    ? `<button type="button" class="btn btn--sm" data-ds-refresh="${id}" title="Regenerate this dataset live from your Azure subscription">↻ Refresh from Azure</button>`
    : "";
  const refetchBtn = hasUrl
    ? `<button type="button" class="btn btn--sm" data-ds-refetch="${id}" title="Re-download from the linked URL">↻ Refresh from URL</button>`
    : "";
  // One-click canonical public source (e.g. latency → Microsoft Docs markdown).
  const showPreset = ds.suggested_url && ds.source_url !== ds.suggested_url;
  const presetBtn = showPreset
    ? `<button type="button" class="btn btn--sm" data-ds-preset="${id}" data-ds-preset-url="${escapeHtml(ds.suggested_url)}" title="${escapeHtml(ds.suggested_url)}">↻ Refresh from ${escapeHtml(ds.suggested_label || "source")}</button>`
    : "";
  const downloadBtn = `<a class="btn btn--sm" href="/api/datasets/${encodeURIComponent(ds.id)}" download>Download</a>`;
  // Secondary configuration links.
  const linkBtn = ds.supports_url
    ? `<button type="button" class="link-btn" data-ds-link="${id}">${hasUrl ? "Change URL…" : "Link data URL…"}</button>`
    : "";
  const unlinkBtn = hasUrl
    ? `<button type="button" class="link-btn danger" data-ds-unlink="${id}">Unlink URL</button>`
    : "";
  const revertBtn = `<button type="button" class="link-btn danger" data-ds-reset="${id}" ${custom ? "" : "hidden"}>Revert to built-in</button>`;
  return `
    <div class="dataset-card" data-ds="${id}">
      <div class="dataset-head">
        <div class="dataset-title">
          <strong>${escapeHtml(ds.label)}</strong> ${badge}
          <code class="dataset-file">${escapeHtml(ds.filename)}</code>
        </div>
      </div>
      <p class="muted dataset-desc">${escapeHtml(ds.description || "")}</p>
      <div class="dataset-meta muted">
        ${summary ? `<span>${summary}</span>` : ""}
        <span>${_fmtBytes(ds.size)}</span>
        <span>Updated ${_fmtDatasetDate(ds.modified)}</span>
      </div>
      ${_datasetSourceLine(ds)}
      <div class="dataset-actions">
        ${armBtn}
        ${presetBtn}
        ${refetchBtn}
        ${downloadBtn}
        ${linkBtn}
        ${unlinkBtn}
        ${revertBtn}
        <span class="muted dataset-status" data-ds-status="${id}"></span>
      </div>
    </div>`;
}

function _wireDatasetCard(ds) {
  const reset = document.querySelector(`[data-ds-reset="${CSS.escape(ds.id)}"]`);
  if (reset) reset.addEventListener("click", () => _resetDataset(ds.id, ds.label));

  const refresh = document.querySelector(`[data-ds-refresh="${CSS.escape(ds.id)}"]`);
  if (refresh) refresh.addEventListener("click", () => _refreshDatasetArm(ds.id, ds.label));

  const link = document.querySelector(`[data-ds-link="${CSS.escape(ds.id)}"]`);
  if (link) link.addEventListener("click", () => _linkDatasetUrl(ds.id, ds.label, ds.source_url || ""));

  const refetch = document.querySelector(`[data-ds-refetch="${CSS.escape(ds.id)}"]`);
  if (refetch) refetch.addEventListener("click", () => _refetchDatasetUrl(ds.id));

  const preset = document.querySelector(`[data-ds-preset="${CSS.escape(ds.id)}"]`);
  if (preset) preset.addEventListener("click", () => _linkDatasetUrl(ds.id, ds.label, preset.getAttribute("data-ds-preset-url"), true));

  const unlink = document.querySelector(`[data-ds-unlink="${CSS.escape(ds.id)}"]`);
  if (unlink) unlink.addEventListener("click", () => _unlinkDatasetUrl(ds.id, ds.label));
}

function _datasetStatusEl(id) {
  return document.querySelector(`[data-ds-status="${CSS.escape(id)}"]`);
}

async function _resetDataset(id, label) {
  if (!confirm(`Revert “${label || id}” to the built-in dataset? Your custom file will be removed.`)) return;
  const status = _datasetStatusEl(id);
  if (status) status.textContent = "Reverting…";
  try {
    const res = await apiFetch(`/api/datasets/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      throw new Error((body && (body.message || body.error)) || res.statusText);
    }
    showToast(`Dataset “${id}” reverted to built-in.`, "success");
    await loadDatasetsSettings();
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    showToast(`Revert failed: ${e.message}`, "error");
  }
}

async function _refreshDatasetArm(id, label) {
  const status = _datasetStatusEl(id);
  if (status) status.textContent = "Refreshing from Azure…";
  try {
    const res = await apiFetch(`/api/datasets/${encodeURIComponent(id)}/refresh`, { method: "POST" });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      throw new Error((body && (body.message || body.error)) || res.statusText);
    }
    showToast(`“${label || id}” refreshed from Azure.`, "success");
    await loadDatasetsSettings();
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    showToast(`Refresh failed: ${e.message}`, "error");
  }
}

async function _linkDatasetUrl(id, label, currentUrl, usePreset) {
  let trimmed;
  if (usePreset) {
    // One-click canonical source — no prompt.
    trimmed = (currentUrl || "").trim();
    if (!trimmed) return;
  } else {
    const url = prompt(
      `Link a data URL for “${label || id}”.\n\n` +
      `Paste an https:// link to the raw file (e.g. a GitHub raw URL, or a ` +
      `github.com …/blob/… link which is converted automatically). ` +
      `The dashboard will fetch, validate, and use it — and you can re-fetch it later.`,
      currentUrl || "https://raw.githubusercontent.com/");
    if (url === null) return;
    trimmed = url.trim();
    if (!trimmed) return;
  }
  const status = _datasetStatusEl(id);
  if (status) status.textContent = "Fetching from URL…";
  try {
    const res = await apiFetch(`/api/datasets/${encodeURIComponent(id)}/source`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: trimmed }),
    });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      throw new Error((body && (body.message || body.error)) || res.statusText);
    }
    showToast(`“${label || id}” linked and fetched from URL.`, "success");
    await loadDatasetsSettings();
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    showToast(`URL fetch failed: ${e.message}`, "error");
  }
}

async function _refetchDatasetUrl(id) {
  const status = _datasetStatusEl(id);
  if (status) status.textContent = "Re-fetching…";
  try {
    const res = await apiFetch(`/api/datasets/${encodeURIComponent(id)}/source`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      throw new Error((body && (body.message || body.error)) || res.statusText);
    }
    showToast(`Dataset “${id}” re-fetched from its linked URL.`, "success");
    await loadDatasetsSettings();
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    showToast(`Re-fetch failed: ${e.message}`, "error");
  }
}

async function _unlinkDatasetUrl(id, label) {
  if (!confirm(`Unlink the source URL for “${label || id}”? The current file stays; it just won't be re-fetchable.`)) return;
  const status = _datasetStatusEl(id);
  if (status) status.textContent = "Unlinking…";
  try {
    const res = await apiFetch(`/api/datasets/${encodeURIComponent(id)}/source`, { method: "DELETE" });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      throw new Error((body && (body.message || body.error)) || res.statusText);
    }
    showToast(`Source URL unlinked for “${id}”.`, "success");
    await loadDatasetsSettings();
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
    showToast(`Unlink failed: ${e.message}`, "error");
  }
}

async function loadAppConfig() {
  try {
    APP_CONFIG = await apiJson("/api/app-config");
  } catch (e) { /* keep defaults */ }
  await initDelegatedAuth();
  applyDemoBanner();
}

// Initialize browser-side (delegated) auth when the server runs in
// multi-customer mode. Safe no-op otherwise. Never throws.
async function initDelegatedAuth() {
  try {
    if (!APP_CONFIG || !APP_CONFIG.delegated_mode || !window.DelegatedAuth) return;
    await window.DelegatedAuth.init({
      entra_client_id: APP_CONFIG.entra_client_id,
      entra_authority: APP_CONFIG.entra_authority,
      arm_scope: APP_CONFIG.arm_scope,
      login_hint: APP_CONFIG.user_name || "",
    });
    // Prefetch the ARM token silently (SSO via the Easy Auth session) so the
    // first data calls (subscriptions, snapshots) carry it without a prompt.
    try { await ensureDelegatedToken({ force: false }); } catch (e) {}
  } catch (e) { /* delegated auth optional; ignore */ }
}

function applyDemoBanner() {
  const el = document.getElementById("demo-banner");
  if (!el) return;
  if (APP_CONFIG && APP_CONFIG.demo_mode) {
    el.innerHTML = `<strong>Demo mode</strong> — you're viewing a bundled sample BOM and analysis.
      Support tickets are <em>preview-only</em> (no Azure calls). Sign in and create your own BOM to run a live analysis.`;
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

// Friendly, non-GUID label for a subscription (Tag/Customer · short-GUID).
function _supportSubLabel(subId) {
  if (!subId) return "—";
  let name = "";
  try { name = _subNameById(subId) || ""; } catch (e) {}
  const shortId = subId.length >= 12 ? subId.slice(0, 8) + "…" + subId.slice(-4) : subId;
  return name && name !== subId ? `${name} · ${shortId}` : shortId;
}

// ---------------------------------------------- Best region recommendation
// Rank every region for the current BOM on a composite of deployment verdict,
// verdict confidence, quota, AZ support, remediation effort, latency and cost,
// then surface the top picks with an at-a-glance trade-off explanation.
const _VERDICT_RANK = { ready: 0, ready_with_constraints: 1, needs_validation: 2, not_recommended: 3 };
const _CONF_RANK = { validated: 0, capability: 1, metadata: 2 };
const _QUOTA_RANK = { sufficient: 0, unknown: 1, insufficient: 2 };

function _latencyForRegion(regionName) {
  const sel = document.getElementById("latency-source");
  const src = sel && sel.value ? sel.value : "";
  if (!src) return null;
  const matrix = (STATE.snapshot && STATE.snapshot.latency_matrix) || {};
  const map = buildDisplayToLatency();
  const srcL = map[src.toLowerCase()];
  const dstL = map[String(regionName).toLowerCase()];
  if (!srcL || !dstL || !matrix[srcL]) return null;
  const ms = matrix[srcL][dstL];
  return (ms == null) ? null : ms;
}

function _scoreRegionForBom(r) {
  const dep = getDeploymentVerdictInfo(r);
  const conf = dep.confidence || (r._conf && r._conf.tier) || "metadata";
  const verdictRank = _VERDICT_RANK[dep.verdict] != null ? _VERDICT_RANK[dep.verdict] : 3;
  const confRank = _CONF_RANK[conf] != null ? _CONF_RANK[conf] : 2;
  const quotaBucket = _quotaFilterBucket(r);
  const quotaRank = _QUOTA_RANK[quotaBucket] != null ? _QUOTA_RANK[quotaBucket] : 1;
  const azRank = (_regionSupportsAz(r) === false) ? 1 : 0;
  const remediation = ((dep.blockers || []).length) + ((dep.constraints || []).length);
  const unverifiablePenalty = (dep.liveNote === "unverifiable") ? 1 : 0;
  const latency = _latencyForRegion(r.name);
  const cost = Number(r.est_monthly) || 0;
  // Composite: verdict dominates, then confidence, quota, remediation, AZ.
  const score = verdictRank * 1000
    + confRank * 120
    + quotaRank * 80
    + unverifiablePenalty * 60
    + remediation * 15
    + azRank * 8;
  return { r, dep, conf, verdictRank, quotaBucket, azRank, remediation, latency, cost, score };
}

function _rankRegionsForBom() {
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  const scored = regions.map(_scoreRegionForBom);
  scored.sort((a, b) => {
    if (a.score !== b.score) return a.score - b.score;
    // Tie-breakers: lower latency (if known), then lower cost, then name.
    const al = a.latency == null ? Infinity : a.latency;
    const bl = b.latency == null ? Infinity : b.latency;
    if (al !== bl) return al - bl;
    if (a.cost !== b.cost) return a.cost - b.cost;
    return String(a.r.name).localeCompare(String(b.r.name));
  });
  return scored;
}

function _bestRegionTradeoffs(s) {
  const bits = [];
  const dep = s.dep;
  bits.push({ label: dep.text, cls: `chip-verdict ${dep.cls}` });
  const cb = _confidenceBadge(dep);
  if (cb) bits.push({ label: cb.text + " confidence", cls: `chip-conf ${cb.cls}` });
  const quotaTxt = { sufficient: "Quota OK", insufficient: "Quota short", unknown: "Quota unknown" }[s.quotaBucket] || "Quota unknown";
  bits.push({ label: quotaTxt, cls: s.quotaBucket === "sufficient" ? "chip-ok" : (s.quotaBucket === "insufficient" ? "chip-bad" : "chip-warn") });
  bits.push({ label: s.azRank ? "No AZs (regional)" : "AZ-enabled", cls: s.azRank ? "chip-warn" : "chip-ok" });
  if (s.latency != null) bits.push({ label: `${s.latency} ms`, cls: s.latency < 50 ? "chip-ok" : (s.latency < 120 ? "chip-warn" : "chip-bad") });
  if (s.cost) bits.push({ label: `${_fmtMoney(s.cost, (PRICING.estimate && PRICING.estimate.currency) || "USD")}/mo`, cls: "chip-neutral" });
  return bits;
}

function renderBestRegionPanel() {
  const el = document.getElementById("best-region-panel");
  if (!el) return;
  const ranked = _rankRegionsForBom();
  if (!ranked.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }

  const top = ranked.slice(0, 3);
  const best = top[0];
  const cards = top.map((s, i) => {
    const chips = _bestRegionTradeoffs(s).map(c => `<span class="br-chip ${c.cls}">${escapeHtml(c.label)}</span>`).join("");
    const remedy = s.remediation
      ? `<div class="br-remedy">${s.remediation} item${s.remediation === 1 ? "" : "s"} to address — open region for the plan</div>`
      : `<div class="br-remedy br-remedy--ok">No blockers or constraints</div>`;
    return `<div class="br-card${i === 0 ? " br-card--best" : ""}" data-region-short="${escapeHtml(s.r.short || "")}">
        <div class="br-rank">${i === 0 ? "★ Best match" : "#" + (i + 1)}</div>
        <div class="br-name">${escapeHtml(s.r.name)}</div>
        <div class="br-geo">${escapeHtml(s.r.geo || "")}${s.r.country ? " • " + escapeHtml(s.r.country) : ""}</div>
        <div class="br-chips">${chips}</div>
        ${remedy}
        <button type="button" class="btn btn--sm br-open" data-region-short="${escapeHtml(s.r.short || "")}">View details →</button>
      </div>`;
  }).join("");

  const heading = best.verdictRank === 0
    ? `<span class="br-good">✅ ${escapeHtml(best.r.name)} is your best fit</span>`
    : `<span class="br-warn">⚠️ No region is fully clean — ${escapeHtml(best.r.name)} is the closest</span>`;

  el.innerHTML = `
    <div class="br-header">
      <div class="br-header-top">
        <div class="br-title">Best regions for your BOM
          <button type="button" class="br-legend-btn" id="br-ranking-btn" title="How are region rankings evaluated?">ⓘ How rankings work</button>
          <button type="button" class="br-legend-btn" id="br-legend-btn" title="What do the confidence levels mean?">Confidence levels</button>
        </div>
        <div class="br-actions">
          <button type="button" class="btn btn--sm" id="br-verify-cta" title="Run a read-only live probe across all regions to raise confidence">⚡ Raise confidence</button>
          <button type="button" class="btn btn--sm btn--primary" id="br-deploy-plan" title="Download a customer-ready deployment plan">📄 Deploy plan</button>
        </div>
      </div>
      <div class="br-lead">${heading} <span class="br-sub">ranked by readiness, confidence, quota, remediation effort, latency & cost</span></div>
    </div>
    <div class="br-cards">${cards}</div>`;
  el.classList.remove("hidden");
  el.querySelectorAll(".br-open").forEach(btn => {
    btn.addEventListener("click", () => {
      const short = btn.getAttribute("data-region-short");
      const region = _findRegionByShort(short);
      if (region) openDrilldown(region);
    });
  });
  const legendBtn = document.getElementById("br-legend-btn");
  if (legendBtn) legendBtn.addEventListener("click", _openConfidenceLegend);
  const rankingBtn = document.getElementById("br-ranking-btn");
  if (rankingBtn) rankingBtn.addEventListener("click", _openRankingLegend);
  const verifyCta = document.getElementById("br-verify-cta");
  if (verifyCta) verifyCta.addEventListener("click", () => {
    switchView("regions");
    if (typeof switchRegionsSub === "function") switchRegionsSub("table");
    verifyAllRegions();
  });
  const planBtn = document.getElementById("br-deploy-plan");
  if (planBtn) planBtn.addEventListener("click", exportDeployPlan);
}

// ---------------------------------------------------- Ranking evaluation help
const _RANKING_LEGEND_ROWS = [
  { label: "Deployment verdict", weight: "bucket ×1000", desc: "Ready maps to bucket 0, then ready with constraints, needs validation, and not recommended." },
  { label: "Evidence confidence", weight: "bucket ×120", desc: "Live-validated evidence maps to bucket 0, then ARM capability metadata, then baseline metadata." },
  { label: "Quota status", weight: "bucket ×80", desc: "Sufficient quota maps to bucket 0, then unknown quota, then quota shortfalls." },
  { label: "Unverifiable live probe", weight: "+60 penalty", desc: "Adds a caution penalty when a live check ran but could not produce a definitive result." },
  { label: "Remediation effort", weight: "count ×15", desc: "Each blocker or constraint adds effort so regions with fewer actions get a lower score." },
  { label: "Availability zones", weight: "+8 penalty", desc: "Regions without AZ support receive a small penalty when the BOM prefers zone-ready regions." },
];

function _openRankingLegend() {
  let overlay = document.getElementById("ranking-legend-overlay");
  if (overlay) { overlay.classList.remove("hidden"); return; }
  overlay = document.createElement("div");
  overlay.id = "ranking-legend-overlay";
  overlay.className = "conf-legend-overlay ranking-legend-overlay";
  const rows = _RANKING_LEGEND_ROWS.map(row =>
    `<li><span class="ranking-factor">${escapeHtml(row.label)}</span><span class="ranking-weight">${escapeHtml(row.weight)}</span><span class="ranking-desc">${escapeHtml(row.desc)}</span></li>`
  ).join("");
  overlay.innerHTML = `<div class="conf-legend-modal ranking-legend-modal" role="dialog" aria-label="How rankings are evaluated">
      <div class="conf-legend-head">
        <strong>How region rankings are evaluated</strong>
        <button type="button" class="conf-legend-close" aria-label="Close">✕</button>
      </div>
      <p class="muted">The dashboard assigns each analyzed region a lower-is-better ranking score. Each major factor is converted to a bucket where the best bucket is 0 before weights are applied. Hard deployment readiness dominates the score; latency, estimated monthly cost, and region name are tie-breakers after the weighted factors match.</p>
      <div class="ranking-formula">score = verdictBucket×1000 + confidenceBucket×120 + quotaBucket×80 + unverifiablePenalty + remediationCount×15 + AZ penalty</div>
      <ul class="ranking-legend-list">${rows}</ul>
      <p class="muted conf-legend-foot">Open a region's details to see the blockers and constraints behind its score. Use <strong>⚡ Raise confidence</strong> to replace metadata assumptions with read-only live probes where possible.</p>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.classList.add("hidden");
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  overlay.querySelector(".conf-legend-close").addEventListener("click", close);
}

// -------------------------------------------------- Confidence legend popover
const _CONF_LEGEND = [
  { cls: "conf-validated", text: "Verified live", desc: "A live per-subscription check confirmed the constrained tiers can (or cannot) deploy. Highest confidence." },
  { cls: "conf-capability", text: "ARM metadata", desc: "Backed by ARM SKU / provider / quota metadata from the last analysis. Run “Raise confidence” for a live confirmation." },
  { cls: "conf-metadata", text: "Baseline", desc: "Region/BOM baseline only — no ARM capability data. Re-run analysis for full signals." },
  { cls: "conf-unverifiable", text: "Unverifiable", desc: "A live check was attempted but couldn’t determine deployability (restricted subscription, throttling, or no authoritative API). Treat with caution." },
];

function _openConfidenceLegend() {
  let overlay = document.getElementById("conf-legend-overlay");
  if (overlay) { overlay.classList.remove("hidden"); return; }
  overlay = document.createElement("div");
  overlay.id = "conf-legend-overlay";
  overlay.className = "conf-legend-overlay";
  const rows = _CONF_LEGEND.map(t =>
    `<li><span class="conf-legend-key"><span class="conf-dot ${t.cls}"></span><span class="conf-badge ${t.cls}">${escapeHtml(t.text)}</span></span><span class="conf-legend-desc">${escapeHtml(t.desc)}</span></li>`
  ).join("");
  overlay.innerHTML = `<div class="conf-legend-modal" role="dialog" aria-label="Confidence levels">
      <div class="conf-legend-head">
        <strong>How confident is each verdict?</strong>
        <button type="button" class="conf-legend-close" aria-label="Close">✕</button>
      </div>
      <p class="muted">The colored <span class="conf-dot conf-validated" style="vertical-align:middle"></span> dot beside each region's verdict — in the <strong>Regions&nbsp;→&nbsp;Table</strong> and on each best-region card — shows how strong the evidence behind that verdict is:</p>
      <ul class="conf-legend-list">${rows}</ul>
      <p class="muted conf-legend-foot">Use <strong>⚡ Raise confidence</strong> to run read-only live probes across every region — it creates nothing.</p>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.classList.add("hidden");
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  overlay.querySelector(".conf-legend-close").addEventListener("click", close);
}

// ------------------------------------------------------ Deploy plan (export)
// A customer-ready Markdown plan: chosen region + runner-ups, per-region
// verdict/confidence, the consolidated remediation plan with ETAs, and any
// filed support tickets. This is the artifact a customer hands to their cloud
// team to actually execute the deployment.
function _mdEscape(s) { return String(s == null ? "" : s).replace(/\|/g, "\\|"); }

function exportDeployPlan() {
  const snap = STATE.snapshot;
  const regions = (snap && snap.regions) || [];
  if (!regions.length) { showToast("Run an analysis first — no regions to plan.", "warning"); return; }
  const meta = (snap && snap.meta) || {};
  const ranked = _rankRegionsForBom();
  const best = ranked[0];
  const genDate = _quotaRemediationGeneratedDate(snap);
  const bomName = (STATE.activeBomId && typeof getBomMeta === "function" && (getBomMeta(STATE.activeBomId) || {}).name)
    || meta.customer_name || "Azure BOM";
  const subLabel = _supportSubLabel(focusedSubscriptionId());
  const cur = (PRICING.estimate && PRICING.estimate.currency) || "USD";

  const L = [];
  L.push(`# Azure Deployment Plan — ${bomName}`);
  L.push("");
  L.push(`- **Generated:** ${genDate}`);
  L.push(`- **Subscription:** ${subLabel}`);
  if (meta.customer_name) L.push(`- **Customer:** ${meta.customer_name}`);
  L.push(`- **Regions analyzed:** ${regions.length}`);
  L.push("");

  // Recommendation
  L.push(`## Recommendation`);
  if (best) {
    const clean = best.verdictRank === 0;
    L.push(clean
      ? `**Deploy to ${best.r.name}** — it meets every requirement in your BOM.`
      : `**${best.r.name}** is the closest fit, but no region is fully clean. Address the items below or accept the noted constraints.`);
    L.push("");
    L.push(`| Rank | Region | Verdict | Confidence | Quota | AZs | Est. $/mo | To address |`);
    L.push(`|------|--------|---------|-----------|-------|-----|-----------|-----------|`);
    ranked.slice(0, 5).forEach((s, i) => {
      const cb = _confidenceBadge(s.dep);
      const quotaTxt = { sufficient: "OK", insufficient: "Short", unknown: "Unknown" }[s.quotaBucket] || "Unknown";
      const az = s.azRank ? "No AZs" : "AZ-enabled";
      const cost = s.cost ? _fmtMoney(s.cost, cur) : "—";
      L.push(`| ${i === 0 ? "★ 1" : i + 1} | ${_mdEscape(s.r.name)} | ${_mdEscape(s.dep.text)} | ${_mdEscape(cb.text)} | ${quotaTxt} | ${az} | ${cost} | ${s.remediation} |`);
    });
    L.push("");
  }

  // Remediation plan
  const plan = _buildRemediationPlan();
  L.push(`## Remediation plan`);
  if (!plan.length) {
    L.push(`No blockers across the analyzed regions — the BOM can deploy as-is. 🎉`);
  } else {
    L.push(`| Action | Typical lead time | Affected regions |`);
    L.push(`|--------|-------------------|------------------|`);
    plan.forEach(g => {
      const m = _REMEDIATION_META[g.type] || _REMEDIATION_META.other;
      const rgs = Array.from(g.regions.values()).join(", ");
      L.push(`| ${_mdEscape(m.label)} | ${_mdEscape(m.eta)} | ${_mdEscape(rgs)} |`);
    });
    L.push("");
    L.push(`> Lead times are typical Azure turnarounds and vary by region, SKU and subscription.`);
  }
  L.push("");

  // Filed tickets
  const tickets = (SUPPORT.tickets || []).filter(t => {
    const st = String(t.status || "").toLowerCase();
    return st && st !== "preview" && st !== "draft";
  });
  if (tickets.length) {
    L.push(`## Support tickets filed`);
    L.push(`| Ticket | Type | Status | Created |`);
    L.push(`|--------|------|--------|---------|`);
    tickets.forEach(t => {
      L.push(`| ${_mdEscape(t.azure_ticket_id || t.ticket_name || "—")} | ${_mdEscape(t.kind || t.type || "—")} | ${_mdEscape(t.azure_status || t.status || "—")} | ${_mdEscape((t.created_at || "").slice(0, 10))} |`);
    });
    L.push("");
  }

  L.push(`---`);
  L.push(`_Generated by the Azure BOM Region Support Dashboard. Verdicts reflect ARM capability metadata plus any live per-subscription verifications; confirm with a fresh analysis before deploying._`);

  const blob = new Blob([L.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `deploy-plan-${snapshotStamp()}.md`;
  a.click();
  URL.revokeObjectURL(url);
  showToast("Deploy plan downloaded (Markdown).", "success");
}

// -------------------------------------------------- Overview cockpit strip
// A compact strip above the donuts: snapshot freshness (with a stale nudge)
// and quota headroom at a glance, so the customer sees trust + capacity
// without leaving the Overview.
function _snapshotAgeDays(snap) {
  const meta = (snap && snap.meta) || {};
  const iso = meta.compiled_at || meta.created_at || "";
  if (!iso) return null;
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return null;
  return Math.floor((Date.now() - dt.getTime()) / 86400000);
}

function renderOverviewCockpit() {
  const el = document.getElementById("overview-cockpit");
  if (!el) return;
  const snap = STATE.snapshot;
  const regions = (snap && snap.regions) || [];
  if (!regions.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }

  const ageDays = _snapshotAgeDays(snap);
  const stale = ageDays != null && ageDays >= 7;
  let freshTxt, freshCls;
  if (ageDays == null) { freshTxt = "Freshness unknown"; freshCls = "cockpit-warn"; }
  else if (ageDays <= 0) { freshTxt = "Analyzed today"; freshCls = "cockpit-ok"; }
  else if (ageDays === 1) { freshTxt = "Analyzed yesterday"; freshCls = "cockpit-ok"; }
  else { freshTxt = `Analyzed ${ageDays} days ago`; freshCls = stale ? "cockpit-warn" : "cockpit-ok"; }

  let suff = 0, insuff = 0, unk = 0;
  regions.forEach(r => {
    const b = _quotaFilterBucket(r);
    if (b === "sufficient") suff++;
    else if (b === "insufficient") insuff++;
    else unk++;
  });

  // Live-verification coverage for the focused subscription.
  const sub = focusedSubscriptionId() || "";
  let verified = 0, inconclusive = 0;
  regions.forEach(r => {
    const c = PRICING.zonalCap[`${String(r.short).toLowerCase()}|${sub}`];
    if (!c) return;
    if (_zonalEntryConclusive(c)) verified++;
    else if (c.status === "done" || c.status === "error") inconclusive++;
  });

  const staleNudge = stale
    ? `<button type="button" class="cockpit-nudge" id="cockpit-verify">Raise confidence →</button>`
    : "";
  const inconclNote = inconclusive
    ? ` <span class="cockpit-sub" title="A live probe ran but returned no definitive answer for these regions (restricted subscription, throttling, or no authoritative API)">· ${inconclusive} inconclusive</span>`
    : "";
  el.innerHTML = `
    <div class="cockpit-chip ${freshCls}" title="Snapshot age from the last analysis">🕒 ${escapeHtml(freshTxt)}${staleNudge}</div>
    <div class="cockpit-chip" title="Quota verdicts across analyzed regions">📊 Quota: <strong class="cockpit-ok-text">${suff}</strong> OK · <strong class="cockpit-bad-text">${insuff}</strong> short · ${unk} unknown</div>
    <div class="cockpit-chip" title="Regions with a conclusive live per-subscription probe (definitive available/blocked/unavailable verdict)">⚡ Live-verified: <strong>${verified}</strong>/${regions.length}${inconclNote}</div>`;
  el.classList.remove("hidden");
  const nudge = document.getElementById("cockpit-verify");
  if (nudge) nudge.addEventListener("click", () => {
    switchView("regions");
    if (typeof switchRegionsSub === "function") switchRegionsSub("table");
    verifyAllRegions();
  });
}

// Plain-language recommendation banner rendered above the Overview donuts.
function renderOverviewReco() {
  const el = document.getElementById("overview-reco");
  if (!el) return;
  const snap = STATE.snapshot;
  const regions = (snap && snap.regions) || [];
  if (!regions.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }

  const ready = regions.filter(r => r.deployment_health === "Yes");
  const blocked = regions.filter(r => r.deployment_health !== "Yes");
  const readyNames = ready.map(r => r.name);
  // Prefer ready regions with the lowest latency footprint if available, else
  // just list the first few alphabetically.
  const topReady = readyNames.slice(0, 3);

  // Summarize the dominant blocker reasons across blocked regions.
  let quotaHits = 0, skuHits = 0, zoneHits = 0, svcHits = 0;
  for (const r of blocked) {
    if ((r.sku_blockers || []).length) skuHits++;
    if (r.has_zone_restriction) zoneHits++;
    if ((r.missing_services || []).length) svcHits++;
  }
  const subLabel = _supportSubLabel(focusedSubscriptionId());

  const parts = [];
  if (ready.length) {
    parts.push(`<span class="reco-good">✅ ${ready.length} region${ready.length === 1 ? "" : "s"} ready to deploy</span>${topReady.length ? ` — e.g. <strong>${escapeHtml(topReady.join(", "))}</strong>` : ""}`);
  } else {
    parts.push(`<span class="reco-bad">⚠️ No region is fully ready for this BOM yet</span>`);
  }
  const reasonBits = [];
  if (zoneHits) reasonBits.push(`${zoneHits} with zone/SKU restrictions`);
  if (skuHits) reasonBits.push(`${skuHits} missing required SKUs`);
  if (svcHits) reasonBits.push(`${svcHits} missing services`);
  if (blocked.length) {
    parts.push(`<span class="reco-warn">${blocked.length} need attention</span>${reasonBits.length ? ` (${escapeHtml(reasonBits.join(", "))})` : ""}`);
  }

  const action = blocked.length
    ? `<button type="button" class="btn btn--sm" id="reco-open-support">Open a support ticket for a blocker →</button>`
    : "";

  el.innerHTML = `<div class="reco-line">${parts.join(" &nbsp;·&nbsp; ")}</div>
    <div class="reco-sub">Subscription: <strong>${escapeHtml(subLabel)}</strong>${action ? " " + action : ""}</div>`;
  el.classList.remove("hidden");
  const btn = document.getElementById("reco-open-support");
  if (btn) btn.addEventListener("click", () => switchView("support"));
}

// Create the #view-support container lazily as a sibling of #view-settings so
// we don't have to hand-edit the views markup.
function ensureSupportView() {
  let el = document.getElementById("view-support");
  if (el) return el;
  const settings = document.getElementById("view-settings");
  el = document.createElement("div");
  el.className = "view hidden";
  el.id = "view-support";
  if (settings && settings.parentNode) settings.parentNode.insertBefore(el, settings.nextSibling);
  else document.querySelector("main").appendChild(el);
  return el;
}

async function renderSupportTab() {
  const view = ensureSupportView();
  view.classList.remove("hidden");  // switchView's loop ran before this existed
  view.innerHTML = `<div class="support-loading">Loading support…</div>`;
  try {
    const [s, t] = await Promise.all([
      apiJson("/api/support/settings"),
      apiJson("/api/support/tickets"),
    ]);
    SUPPORT.settings = s.settings;
    SUPPORT.tickets = t.tickets || [];
    SUPPORT.loaded = true;
  } catch (e) {
    view.innerHTML = `<div class="support-error">Could not load support data: ${escapeHtml(e.message)}</div>`;
    return;
  }
  view.innerHTML = _supportHtml();
  _wireSupportTab(view);
  _updateSupportBadge();
}

function _updateSupportBadge() {
  const badge = document.getElementById("support-tab-badge");
  if (!badge) return;
  // Count only genuinely open Azure tickets. Exclude local-only drafts
  // ("preview"), failed submission attempts ("failed"/"error"), and anything
  // closed/cancelled — none of those represent a live, open support request,
  // so they must not inflate the badge (previously any non-"closed" status,
  // including previews and failures, was counted).
  const NON_OPEN = new Set(["preview", "draft", "failed", "error", "closed", "cancelled", "canceled", "deleted"]);
  const open = (SUPPORT.tickets || []).filter(t => {
    const st = String(t.status || "").toLowerCase();
    const az = String(t.azure_status || "").toLowerCase();
    return !NON_OPEN.has(st) && az !== "closed" && az !== "cancelled" && az !== "canceled";
  }).length;
  badge.textContent = String(open);
  badge.classList.toggle("hidden", open === 0);
}

function _supportBlockedRegions() {
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  return regions.filter(r => r.deployment_health !== "Yes");
}

function _activeBomFamilies() {
  const meta = (typeof getBomMeta === "function") ? getBomMeta(STATE.activeBomId) : null;
  const skus = (meta && meta.required_skus) || [];
  const out = [];
  for (const s of skus) {
    if (s.primary_family) out.push({ family: s.primary_family, label: s.primary_label || s.primary_family, cores: s.required_cores || 0 });
    if (s.alt_family) out.push({ family: s.alt_family, label: s.alt_label || s.alt_family, cores: s.required_cores || 0 });
  }
  return out;
}

function _activeBomSubs() {
  const meta = (typeof getBomMeta === "function") ? getBomMeta(STATE.activeBomId) : null;
  const ids = (meta && meta.subscription_ids) || [];
  return ids.length ? ids : (focusedSubscriptionId() ? [focusedSubscriptionId()] : []);
}

// ---------------------------------------------- Consolidated remediation plan
// A cross-region view of every blocker grouped by the remediation action it
// needs, with a typical Azure lead-time ETA and the affected regions. This
// turns the per-region blocker list into an actionable, deduplicated plan.
const _REMEDIATION_META = {
  quota_insufficient: {
    icon: "📊", label: "Request a quota increase",
    eta: "Typically 1–3 business days", ticket: "quota",
    how: "File a Compute-VM (cores/vCPUs) quota increase for the affected family.",
  },
  no_access: {
    icon: "🔒", label: "Request zonal / restricted-SKU access",
    eta: "Typically 3–5 business days", ticket: "technical",
    how: "File a zonal access (subscription restriction) request for the SKU in the needed zones.",
  },
  zone_gap: {
    icon: "⚠️", label: "Close an availability-zone gap",
    eta: "Typically 3–5 business days", ticket: "technical",
    how: "Request zonal access for the SKU, or accept the fallback SKU across all zones.",
  },
  sku_unavailable: {
    icon: "⛔", label: "SKU not offered in region",
    eta: "No ticket — choose an alternate region", ticket: "",
    how: "This SKU isn't offered here. Pick an alternate region (see the region's suggestions) or a different family.",
  },
  missing_service: {
    icon: "🚫", label: "Service not available in region",
    eta: "No ticket — choose an alternate region", ticket: "",
    how: "A required service isn't offered in this region. Deploy it in an alternate region.",
  },
  other: {
    icon: "•", label: "Other checks to review",
    eta: "Review manually", ticket: "",
    how: "Open the region drilldown for details.",
  },
};

function _buildRemediationPlan() {
  const regions = (STATE.snapshot && STATE.snapshot.regions) || [];
  const groups = new Map();
  for (const r of regions) {
    const dep = getDeploymentVerdictInfo(r);
    (dep.blockers || []).forEach(b => {
      const type = (b && b.type) || "other";
      if (!groups.has(type)) groups.set(type, { type, regions: new Map(), messages: new Set() });
      const g = groups.get(type);
      g.regions.set(r.short || r.name, r.name);
      if (b && b.message) g.messages.add(b.message);
    });
  }
  // Order by remediation severity/priority.
  const order = ["missing_service", "sku_unavailable", "no_access", "zone_gap", "quota_insufficient", "other"];
  return order.filter(t => groups.has(t)).map(t => groups.get(t));
}

function _renderRemediationPlanHtml() {
  const plan = _buildRemediationPlan();
  if (!plan.length) {
    return `<section class="support-section remediation-plan">
      <h3>Remediation plan</h3>
      <p class="muted">No blockers across the analyzed regions 🎉 Everything in your BOM can deploy as-is.</p>
    </section>`;
  }
  const totalRegions = new Set();
  plan.forEach(g => g.regions.forEach((_n, k) => totalRegions.add(k)));
  const rows = plan.map(g => {
    const meta = _REMEDIATION_META[g.type] || _REMEDIATION_META.other;
    const regionChips = Array.from(g.regions.entries()).map(([short, name]) =>
      `<button type="button" class="remedy-region-chip" data-remedy-region="${escapeHtml(short)}" data-remedy-kind="${meta.ticket}" title="${meta.ticket ? "Prefill a ticket for this region" : "Open region details"}">${escapeHtml(name)}</button>`
    ).join("");
    const action = meta.ticket
      ? `<button type="button" class="btn btn--sm remedy-file-all" data-remedy-kind="${meta.ticket}" data-remedy-first="${escapeHtml(Array.from(g.regions.keys())[0] || "")}">File ${meta.ticket === "quota" ? "quota" : "access"} ticket →</button>`
      : `<span class="muted">Alternate region recommended</span>`;
    return `<tr>
      <td class="remedy-action"><span class="remedy-icon">${meta.icon}</span> <strong>${escapeHtml(meta.label)}</strong>
        <div class="remedy-how muted">${escapeHtml(meta.how)}</div></td>
      <td class="remedy-eta">${escapeHtml(meta.eta)}</td>
      <td class="remedy-regions">${regionChips}</td>
      <td class="remedy-do">${action}</td>
    </tr>`;
  }).join("");
  return `<section class="support-section remediation-plan">
    <div class="support-section-head">
      <h3>Remediation plan</h3>
      <span class="muted">${plan.length} action type${plan.length === 1 ? "" : "s"} across ${totalRegions.size} region${totalRegions.size === 1 ? "" : "s"}</span>
    </div>
    <p class="muted">Every blocker grouped by the action it needs, with a typical Azure lead-time. Click a region to prefill its ticket.</p>
    <table class="support-table remedy-table"><thead><tr>
      <th>Action</th><th>Typical lead time</th><th>Affected regions</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>
  </section>`;
}

function _supportHtml() {
  const s = SUPPORT.settings || {};
  const demo = !!(APP_CONFIG && APP_CONFIG.demo_mode);
  const blocked = _supportBlockedRegions();
  const families = _activeBomFamilies();
  const subs = _activeBomSubs();
  const sevOpts = ["minimal", "moderate", "critical"]
    .map(v => `<option value="${v}" ${((s.default_severity || "moderate") === v) ? "selected" : ""}>${v[0].toUpperCase() + v.slice(1)}</option>`).join("");

  const regionOpts = (STATE.snapshot && STATE.snapshot.regions || [])
    .slice().sort((a, b) => a.name.localeCompare(b.name))
    .map(r => `<option value="${escapeHtml(r.short || "")}" data-blocked="${r.deployment_health !== "Yes" ? 1 : 0}">${escapeHtml(r.name)}${r.deployment_health !== "Yes" ? " ⚠" : ""}</option>`).join("");
  const familyOpts = families.map(f => `<option value="${escapeHtml(f.family)}" data-label="${escapeHtml(f.label)}" data-cores="${f.cores}">${escapeHtml(f.label)} (${escapeHtml(f.family)})</option>`).join("")
    || `<option value="">— no BOM families —</option>`;
  const subOpts = subs.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(_supportSubLabel(id))}</option>`).join("")
    || `<option value="">— sign in / select a BOM —</option>`;

  const blockersHtml = blocked.length
    ? blocked.map(r => {
        const reasons = [];
        if (r.has_zone_restriction) reasons.push("zone/SKU restriction");
        if ((r.sku_blockers || []).length) reasons.push(`${r.sku_blockers.length} SKU blocker(s)`);
        if ((r.missing_services || []).length) reasons.push(`${r.missing_services.length} missing service(s)`);
        return `<tr data-blocker-region="${escapeHtml(r.short || "")}">
          <td>${escapeHtml(r.name)}</td>
          <td>${escapeHtml(reasons.join(", ") || "needs validation")}</td>
          <td class="support-blocker-actions">
            <button type="button" class="btn btn--sm" data-prefill="quota" data-region="${escapeHtml(r.short || "")}">Quota ticket</button>
            <button type="button" class="btn btn--sm" data-prefill="technical" data-region="${escapeHtml(r.short || "")}">Access ticket</button>
          </td></tr>`;
      }).join("")
    : `<tr><td colspan="3" class="muted">No blocked regions in the current analysis 🎉</td></tr>`;

  const sortedBlocked = blocked.slice().sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  const defaultBlockerRegion = sortedBlocked.length ? (sortedBlocked[0].short || "") : "";
  const blockerFilterOpts = blocked.length
    ? sortedBlocked
        .map(r => `<option value="${escapeHtml(r.short || "")}" ${(r.short || "") === defaultBlockerRegion ? "selected" : ""}>${escapeHtml(r.name || r.short || "")}</option>`).join("")
      + `<option value="">All blocked regions (${blocked.length})</option>`
    : "";

  return `
  <div class="support-wrap">
    <!-- tracked tickets removed: submitted tickets surface in the live Azure list below -->
    <div class="support-intro">
      <h2>Support tickets</h2>
      <p class="muted">Turn a deployment blocker into an Azure support request — a <strong>quota increase</strong>
      or a <strong>zonal / restricted-SKU access</strong> ticket, filed via <code>Microsoft.Support</code>.${demo ? " <strong>Demo mode: submission is disabled.</strong>" : ""}</p>
    </div>

    ${_renderRemediationPlanHtml()}

    <section class="support-section">
      <div class="support-section-head">
        <h3>Blockers in the current analysis</h3>
        ${blocked.length ? `<label class="support-blocker-filter">Region
          <select id="support-blocker-filter">${blockerFilterOpts}</select>
        </label>` : ""}
      </div>
      <table class="support-table"><thead><tr><th>Region</th><th>Why it's blocked</th><th>Create ticket</th></tr></thead>
      <tbody id="support-blockers-body">${blockersHtml}</tbody></table>
    </section>

    <section class="support-section">
      <h3>New ticket</h3>
      <div class="support-form-grid">
        <label>Type
          <select id="sup-kind">
            <option value="quota">Quota increase</option>
            <option value="technical">Zonal / SKU access (restriction)</option>
          </select></label>
        <label>Subscription <select id="sup-sub">${subOpts}</select></label>
        <label>Region <select id="sup-region">${regionOpts}</select></label>
        <label>SKU family <select id="sup-family">${familyOpts}</select></label>
        <label id="sup-limit-wrap">New vCPU limit <input type="number" id="sup-limit" min="1" value="100" />
          <small class="sup-limit-info muted" id="sup-limit-info"></small></label>
        <label id="sup-zones-wrap" class="hidden">Zones (comma-sep) <input type="text" id="sup-zones" placeholder="1,2,3" /></label>
        <label>Severity <select id="sup-sev">${sevOpts}</select></label>
      </div>
      <div class="support-form-actions">
        <button type="button" class="btn btn--accent" id="sup-submit" ${demo ? "disabled title='Disabled in demo mode'" : ""}>Submit to Azure</button>
      </div>
      <pre class="support-preview hidden" id="sup-preview-box"></pre>
    </section>

    <section class="support-section">
      <div class="support-section-head">
        <h3>Active Azure tickets on this subscription</h3>
        <div class="support-section-head-tools">
          <label class="support-inline-filter">Show
            <select id="sup-azure-filter">
              <option value="open" selected>Open only</option>
              <option value="closed">Closed only</option>
              <option value="all">All</option>
            </select>
          </label>
          <button type="button" class="btn btn--sm" id="sup-azure-refresh">↻ Refresh</button>
        </div>
      </div>
      <p class="muted">Pulled live from Azure — support tickets on your BOM subscription(s), including any created from this dashboard.</p>
      <table class="support-table"><thead><tr>
        <th>Type</th><th>Title</th><th>Severity</th><th>Status</th><th>Created</th><th></th>
      </tr></thead><tbody id="support-azure-tickets-body">
        <tr><td colspan="6" class="muted">Loading live tickets from Azure…</td></tr>
      </tbody></table>
    </section>
  </div>`;
}

function _supportAzureTicketRow(t) {
  const created = (t.created_at || "").replace("T", " ").replace("Z", "").slice(0, 19);
  const status = String(t.azure_status || "");
  const isClosed = status.toLowerCase() === "closed";
  const statusPill = status
    ? `<span class="pill ${isClosed ? "pill-ok" : "pill-warn"}">${escapeHtml(status)}</span>`
    : `<span class="pill pill-muted">—</span>`;
  const closeBtn = isClosed
    ? ""
    : `<button type="button" class="btn btn--sm" data-azure-ticket-close="${escapeHtml(t.ticket_name || "")}" data-azure-ticket-sub="${escapeHtml(t.subscription_id || "")}" data-azure-ticket-title="${escapeHtml(t.title || t.ticket_name || "")}">Close</button>`;
  return `<tr>
    <td>${escapeHtml(t.kind || "support")}</td>
    <td title="${escapeHtml(t.ticket_name || "")}">${escapeHtml(t.title || t.ticket_name || "")}</td>
    <td>${escapeHtml(t.severity || "")}</td>
    <td>${statusPill}</td>
    <td>${escapeHtml(created)}</td>
    <td class="support-ticket-actions">${closeBtn}</td>
  </tr>`;
}

// Real-time pull of Azure support tickets already on the BOM subscription(s),
// excluding the ones this dashboard created (tracked locally).
async function _supportLoadAzureTickets() {
  const body = document.getElementById("support-azure-tickets-body");
  if (!body) return;
  const filterSel = document.getElementById("sup-azure-filter");
  const filter = (filterSel && filterSel.value) || "open";
  const subs = _activeBomSubs().filter(Boolean);
  if (!subs.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">Sign in and select a BOM to see live tickets.</td></tr>`;
    return;
  }
  body.innerHTML = `<tr><td colspan="6" class="muted"><span class="quota-request-spinner" aria-hidden="true"></span> Loading live tickets from Azure…</td></tr>`;

  const seen = new Set();
  const collected = [];
  const errors = [];
  // Always fetch every ticket (open + closed) so the client-side filter can
  // switch between Open / Closed / All without re-hitting Azure.
  await Promise.all(subs.map(async (subId) => {
    try {
      const res = await apiJson(`/api/support/azure-tickets?subscription_id=${encodeURIComponent(subId)}&open_only=false`);
      for (const t of (res.tickets || [])) {
        const key = String(t.ticket_name || t.azure_ticket_id || "").toLowerCase();
        if (!key || seen.has(key)) continue;
        seen.add(key);
        collected.push(t);
      }
    } catch (e) {
      errors.push({ subId, message: e.message || String(e) });
    }
  }));

  collected.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));

  const isClosed = (t) => String(t.azure_status || "").toLowerCase() === "closed";
  const filtered = collected.filter(t => {
    if (filter === "closed") return isClosed(t);
    if (filter === "all") return true;
    return !isClosed(t);
  });

  if (!filtered.length) {
    const label = filter === "closed" ? "closed" : filter === "all" ? "" : "active";
    const note = errors.length && !collected.length
      ? `Could not load tickets for ${errors.length} subscription(s): ${escapeHtml(errors[0].message)}`
      : `No other ${label ? label + " " : ""}Azure support tickets on this subscription.`;
    body.innerHTML = `<tr><td colspan="6" class="muted">${note}</td></tr>`;
    return;
  }
  let html = filtered.map(_supportAzureTicketRow).join("");
  if (errors.length) {
    html += `<tr><td colspan="6" class="muted">Note: ${errors.length} subscription(s) could not be read (${escapeHtml(errors[0].message)}).</td></tr>`;
  }
  body.innerHTML = html;
}

// Close an Azure support ticket straight from the live list.
async function _supportCloseAzureTicket(ticketName, subId, title, btn) {
  ticketName = (ticketName || "").trim();
  subId = (subId || "").trim();
  if (!ticketName || !subId) { showToast("Missing ticket details to close.", "warning"); return; }
  const label = title || ticketName;
  const ok = await showConfirm(
    `Close Azure support ticket "${label}"?\n\nAzure only allows closing tickets that aren't actively assigned to an engineer.`,
    { title: "Close support ticket", confirmLabel: "Close ticket", danger: true }
  );
  if (!ok) return;
  const original = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Closing…"; }

  const closeOnce = () => apiJson("/api/support/azure-tickets/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subscription_id: subId, ticket_name: ticketName }),
  });

  try {
    try {
      await closeOnce();
    } catch (e) {
      // Azure gated this write behind MFA — step the user up and retry once.
      if (e && e.body && e.body.error === "mfa_required") {
        const claims = e.body.details && e.body.details.claims;
        if (btn) btn.textContent = "Verifying MFA…";
        showToast("Azure needs multi-factor authentication to close this ticket — please complete the sign-in prompt.", "warning");
        try {
          await stepUpDelegatedToken(claims);
        } catch (authErr) {
          showToast(`MFA sign-in was cancelled or blocked: ${authErr.message || authErr}`, "error");
          throw e;
        }
        if (btn) btn.textContent = "Closing…";
        await closeOnce();
      } else {
        throw e;
      }
    }
    showToast(`✓ Ticket closed: ${label}`, "success");
    await _supportLoadAzureTickets();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = original || "Close"; }
    showToast(e.message || "Could not close the ticket.", "error");
  }
}

function _supportTicketRow(t) {
  const statusPill = _supportStatusPill(t);
  const created = (t.created_at || "").replace("T", " ").replace("Z", "");
  const canRefresh = !t.dry_run && t.status === "submitted";
  return `<tr>
    <td>${escapeHtml(t.kind || "")}</td>
    <td title="${escapeHtml(t.ticket_name || "")}">${escapeHtml(t.title || "")}</td>
    <td>${escapeHtml(t.region || "")}</td>
    <td>${escapeHtml(t.severity || "")}</td>
    <td>${statusPill}</td>
    <td>${escapeHtml(created)}</td>
    <td class="support-ticket-actions">
      <button type="button" class="btn btn--sm" data-ticket-view="${escapeHtml(t.ticket_name || "")}">Payload</button>
      ${canRefresh ? `<button type="button" class="btn btn--sm" data-ticket-refresh="${escapeHtml(t.ticket_name || "")}">Refresh</button>` : ""}
    </td></tr>`;
}

function _supportStatusPill(t) {
  if (t.dry_run) return `<span class="pill pill-muted">Preview</span>`;
  const az = (t.azure_status || "").toLowerCase();
  if (t.status === "failed") return `<span class="pill pill-fail">Failed</span>`;
  if (az === "closed") return `<span class="pill pill-ok">Closed</span>`;
  if (t.status === "submitted") return `<span class="pill pill-warn">Open${t.azure_status ? " · " + escapeHtml(t.azure_status) : ""}</span>`;
  return `<span class="pill pill-muted">${escapeHtml(t.status || "—")}</span>`;
}

function _supportSyncKindFields() {
  const kind = (document.getElementById("sup-kind") || {}).value;
  const limitWrap = document.getElementById("sup-limit-wrap");
  const zonesWrap = document.getElementById("sup-zones-wrap");
  // Both quota and zonal ("technical") tickets need a target vCPU limit; only
  // zonal needs the availability-zone list.
  if (limitWrap) limitWrap.classList.remove("hidden");
  if (zonesWrap) zonesWrap.classList.toggle("hidden", kind !== "technical");
  _supportUpdateQuotaMath();
}

// Compute current-vs-needed quota math for the selected region + SKU family,
// prefill the "New vCPU limit" with the recommended value (still user-editable),
// and show the math as helper text under the field.
function _supportUpdateQuotaMath(opts) {
  opts = opts || {};
  const kind = (document.getElementById("sup-kind") || {}).value;
  const info = document.getElementById("sup-limit-info");
  const limitInput = document.getElementById("sup-limit");
  if (!info || !limitInput) return;
  if (kind !== "quota" && kind !== "technical") { info.textContent = ""; return; }

  const region = (document.getElementById("sup-region") || {}).value || "";
  const familySel = document.getElementById("sup-family");
  const family = (familySel || {}).value || "";
  if (!region || !family) { info.textContent = ""; return; }

  const row = _findQuotaRow(region, family);
  let current = null;
  let required = null;
  if (row) {
    if (row.subscription && row.subscription.limit != null) current = Number(row.subscription.limit);
    if (row.required != null) required = Number(row.required);
  }
  if (required == null && familySel && familySel.selectedOptions[0]) {
    const cores = Number(familySel.selectedOptions[0].getAttribute("data-cores"));
    if (Number.isFinite(cores) && cores > 0) required = cores;
  }
  // Suggested alternatives aren't in the snapshot's quota rows; fall back to the
  // real current limit captured from the live validation (stashed on the option).
  if (current == null && familySel && familySel.selectedOptions[0]) {
    const optLimit = Number(familySel.selectedOptions[0].getAttribute("data-current-limit"));
    if (Number.isFinite(optLimit)) current = optLimit;
  }

  // Recommended new limit: reuse the same logic the quota buttons use when we
  // have a full row; otherwise fall back to current + shortfall.
  let recommended;
  if (row) {
    recommended = _quotaRequestLimitForRow(row);
  } else if (current != null && required != null) {
    recommended = _roundUpToNearest(Math.max(current + Math.max(0, required - current), required), 50);
  } else if (required != null) {
    recommended = required;
  } else {
    recommended = Number(limitInput.value) || 100;
  }

  // Prefill unless the user has manually typed a higher value this session.
  if (opts.force || !limitInput.dataset.userEdited) {
    limitInput.value = String(Math.round(recommended));
  }

  if (current != null && required != null) {
    const shortfall = Math.max(0, required - current);
    info.innerHTML = shortfall > 0
      ? `Current limit <strong>${_formatQuotaNumber(current)}</strong> · BOM needs <strong>${_formatQuotaNumber(required)}</strong> → shortfall <strong>${_formatQuotaNumber(shortfall)}</strong>. New limit is the target ceiling, not the increase: <strong>${_formatQuotaNumber(current)}</strong> + <strong>${_formatQuotaNumber(shortfall)}</strong> = <strong>${_formatQuotaNumber(Math.round(recommended))}</strong> (editable).`
      : `Current limit <strong>${_formatQuotaNumber(current)}</strong> already covers the BOM need of <strong>${_formatQuotaNumber(required)}</strong>. Prefilled <strong>${_formatQuotaNumber(Math.round(recommended))}</strong> for extra headroom (editable).`;
  } else if (required != null) {
    info.innerHTML = `BOM needs <strong>${_formatQuotaNumber(required)}</strong> vCPU for this SKU. Prefilled new limit <strong>${_formatQuotaNumber(Math.round(recommended))}</strong> (editable).`;
  } else {
    info.textContent = "";
  }
}

function _supportGatherForm() {
  const kind = (document.getElementById("sup-kind") || {}).value || "quota";
  const familySel = document.getElementById("sup-family");
  const opt = familySel && familySel.selectedOptions[0];
  const body = {
    kind,
    subscription_id: (document.getElementById("sup-sub") || {}).value || "",
    region: (document.getElementById("sup-region") || {}).value || "",
    family: (document.getElementById("sup-family") || {}).value || "",
    family_label: opt ? opt.getAttribute("data-label") : "",
    severity: (document.getElementById("sup-sev") || {}).value || "moderate",
  };
  if (kind === "quota" || kind === "technical") {
    body.new_limit = parseInt((document.getElementById("sup-limit") || {}).value || "0", 10);
  }
  if (kind === "technical") {
    const z = ((document.getElementById("sup-zones") || {}).value || "").split(",").map(x => x.trim()).filter(Boolean);
    if (z.length) body.zones = z;
  }
  if (STATE.activeBomId) body.bom_id = STATE.activeBomId;
  return body;
}

async function _supportCreate() {
  const body = _supportGatherForm();
  if (!body.subscription_id) { showToast("Pick a subscription first.", "warning"); return; }
  if (!body.region) { showToast("Pick a region first.", "warning"); return; }
  if (!body.family) { showToast("Pick a SKU family first.", "warning"); return; }
  if (body.kind === "technical" && !(body.zones && body.zones.length)) {
    showToast("Enter at least one availability zone (e.g. 1,2,3) for a zonal access ticket.", "warning"); return;
  }
  if ((body.kind === "quota" || body.kind === "technical") && !(body.new_limit > 0)) {
    showToast("Enter a target vCPU limit.", "warning"); return;
  }
  const ok = await showConfirm(
    `Submit a real ${body.kind} support ticket to Azure for ${body.region}?`,
    { title: "Submit support ticket", confirmLabel: "Submit to Azure" }
  );
  if (!ok) return;
  const box = document.getElementById("sup-preview-box");
  const btn = document.getElementById("sup-submit");
  const original = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Submitting…"; }
  if (box) { box.classList.add("hidden"); box.textContent = ""; }

  const submitOnce = () => apiJson("/api/support/tickets", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });

  try {
    let res;
    try {
      res = await submitOnce();
    } catch (e) {
      // Azure rejected the write pending an MFA step-up. The browser-minted ARM
      // token is password-only; re-acquire an MFA token interactively and retry
      // once so the customer doesn't hit a dead end.
      const code = e && e.body && e.body.error;
      if (code === "mfa_required") {
        const claims = e.body && e.body.details && e.body.details.claims;
        if (btn) btn.textContent = "Verifying MFA…";
        showToast("Azure needs multi-factor authentication to file this ticket — please complete the sign-in prompt.", "warning");
        try {
          await stepUpDelegatedToken(claims);
        } catch (authErr) {
          showToast(`MFA sign-in was cancelled or blocked: ${authErr.message || authErr}`, "error");
          throw e;
        }
        if (btn) btn.textContent = "Submitting…";
        res = await submitOnce();
      } else {
        throw e;
      }
    }
    const ticket = res.ticket;
    showToast(`Ticket submitted: ${ticket.azure_ticket_id || ticket.ticket_name}`, "success");
    _autoRecheckAfterTicket(body.kind, body.region, body.subscription_id);
    await _supportReloadTickets();
  } catch (e) {
    showToast(`Ticket failed: ${e.message}`, "error");
    if (box && e.body) { box.textContent = JSON.stringify(e.body, null, 2); box.classList.remove("hidden"); }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original || "Submit to Azure"; }
  }
}

async function _supportReloadTickets() {
  try {
    const t = await apiJson("/api/support/tickets");
    SUPPORT.tickets = t.tickets || [];
    _updateSupportBadge();
    _supportLoadAzureTickets();
  } catch (e) { /* ignore */ }
}

// After a ticket is filed, automatically re-verify the affected region so the
// dashboard reflects the new state as soon as access is granted. Zonal/SKU
// access tickets are re-checked live (the authoritative capability API); quota
// tickets need a full analysis re-run, so we tell the user rather than imply a
// live confirmation we can't make.
async function _autoRecheckAfterTicket(kind, regionShort, sub) {
  const region = _findRegionByShort(regionShort);
  if (!region) return;
  const subId = sub || focusedSubscriptionId() || "";
  if (kind === "technical") {
    const key = `${String(regionShort).toLowerCase()}|${subId}`;
    delete PRICING.zonalCap[key];  // force a fresh probe (don't reuse cached "blocked")
    showToast(`Re-checking ${region.name} live…`, "info");
    try { await _verifyZonalForRegion(region); } catch (_e) {}
    applyFilters();
    _persistVerifyAll(subId).catch(() => {});
    const entry = PRICING.zonalCap[key];
    const stillBlocked = entry && entry.map && Object.values(entry.map)
      .some(v => v && (v.verdict === "blocked" || v.verdict === "unavailable"));
    if (entry && entry.status === "done") {
      showToast(stillBlocked
        ? `${region.name}: still restricted — access usually takes 3–5 business days to apply.`
        : `${region.name}: access looks granted — verdict updated ✅`, stillBlocked ? "warning" : "success");
    }
  } else if (kind === "quota") {
    showToast(`${region.name}: quota changes apply after approval — re-run analysis to refresh quota verdicts.`, "info");
  }
}

async function _supportSaveSettings() {
  const status = document.getElementById("set-status");
  const body = {
    contact_first_name: (document.getElementById("set-first") || {}).value || "",
    contact_last_name: (document.getElementById("set-last") || {}).value || "",
    primary_email: (document.getElementById("set-email") || {}).value || "",
    additional_emails: (document.getElementById("set-cc") || {}).value || "",
    phone: (document.getElementById("set-phone") || {}).value || "",
    country: (document.getElementById("set-country") || {}).value || "US",
    preferred_timezone: (document.getElementById("set-tz") || {}).value || "",
    default_severity: (document.getElementById("set-sev") || {}).value || "moderate",
  };
  if (status) status.textContent = "Saving…";
  try {
    const res = await apiJson("/api/support/settings", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    SUPPORT.settings = res.settings;
    APP_CONFIG.support_configured = res.configured;
    if (status) status.textContent = res.configured ? "✓ Saved" : "Saved (email + name needed to submit)";
    showToast("Support settings saved.", "success");
  } catch (e) {
    if (status) status.textContent = `❌ ${e.message}`;
  }
}

async function _supportViewPayload(name) {
  const box = document.getElementById("sup-preview-box");
  try {
    const res = await apiJson(`/api/support/tickets/${encodeURIComponent(name)}`);
    if (box) { box.textContent = JSON.stringify(res.ticket.payload, null, 2); box.classList.remove("hidden"); box.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  } catch (e) { showToast(e.message, "error"); }
}

async function _supportRefreshTicket(name) {
  try {
    await apiJson(`/api/support/tickets/${encodeURIComponent(name)}`, { method: "POST" });
    await _supportReloadTickets();
    showToast("Ticket status refreshed.", "success");
  } catch (e) { showToast(e.message, "error"); }
}

async function _supportWipe() {
  if (!confirm("Delete ALL local snapshots and analysis history? Support settings are kept. This cannot be undone.")) return;
  try {
    const r = await apiJson("/api/local-state/wipe", { method: "POST" });
    showToast(`Wiped ${r.snapshots_removed} snapshot(s).`, "success");
    location.reload();
  } catch (e) { showToast(e.message, "error"); }
}

// Open the snapshots folder in the OS file explorer — only works when the
// dashboard is self-hosted locally (the server shares the user's desktop).
async function _openSnapshotsFolder() {
  const status = document.getElementById("owner-snapshots-status");
  if (status) status.textContent = "Opening…";
  try {
    const r = await apiJson("/api/local-state/open-folder", { method: "POST" });
    if (status) status.textContent = `Opened ${r.path}`;
  } catch (e) {
    if (status) status.textContent = "";
    showToast(e.message || "Could not open the folder.", "error");
  }
}

// Download all snapshots as a zip. Works in both local and hosted mode — in the
// hosted app this is the portable substitute for "open the folder".
async function _downloadSnapshots() {
  const status = document.getElementById("owner-snapshots-status");
  if (status) status.textContent = "Preparing download…";
  try {
    const res = await apiFetch("/api/snapshots/export");
    if (!res.ok) {
      let msg = res.statusText;
      try { const b = await res.json(); msg = (b && (b.message || b.error)) || msg; } catch (e) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    let filename = "bom-snapshots.zip";
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename="?([^"]+)"?/.exec(cd);
    if (m) filename = m[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    if (status) status.textContent = "Downloaded.";
  } catch (e) {
    if (status) status.textContent = "";
    showToast(e.message || "Could not download snapshots.", "error");
  }
}

// Import a previously downloaded snapshots .zip back into your session/history.
async function _importSnapshots(file) {
  const status = document.getElementById("owner-snapshots-status");
  if (!file) return;
  if (status) status.textContent = "Importing…";
  try {
    const fd = new FormData();
    fd.append("file", file, file.name || "snapshots.zip");
    const res = await apiFetch("/api/snapshots/import", { method: "POST", body: fd });
    if (!res.ok) {
      let msg = res.statusText;
      try { const b = await res.json(); msg = (b && (b.message || b.error)) || msg; } catch (e) {}
      throw new Error(msg);
    }
    const r = await res.json();
    const bomsMsg = r.boms ? `, ${r.boms} BOM(s)` : "";
    if (status) status.textContent = `Imported ${r.imported} snapshot(s)${bomsMsg}.`;
    showToast(`Imported ${r.imported} snapshot(s)${bomsMsg}${r.skipped ? `, ${r.skipped} skipped` : ""}.`, "success");
    // Persist the restored state to the browser (hosted mode) before reloading
    // so the imported history survives the refresh, then reload to show it.
    if (typeof _stateSyncEnabled === "function" && _stateSyncEnabled()) {
      try { await saveStateToLocal(); } catch (e) {}
    }
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    if (status) status.textContent = "";
    showToast(e.message || "Could not import snapshots.", "error");
  }
}

function _supportPrefill(kind, regionShort, opts) {
  opts = opts || {};
  switchView("support");
  setTimeout(() => {
    const kindSel = document.getElementById("sup-kind");
    const regionSel = document.getElementById("sup-region");
    if (kindSel) kindSel.value = kind;
    if (regionSel) regionSel.value = regionShort;
    _supportSyncKindFields();
    const familySel = document.getElementById("sup-family");
    if (familySel && opts.family) {
      // Drop any previously-injected alternative so they don't accumulate.
      Array.from(familySel.querySelectorAll("option[data-alt-injected]"))
        .forEach(o => { if (o.value !== opts.family) o.remove(); });
      let opt = Array.from(familySel.options).find(o => o.value === opts.family);
      if (!opt) {
        // The suggested alternative isn't a BOM family, so it isn't in the list.
        // Inject it (carrying the cores + real current limit) so the ticket can
        // target the alternative SKU, then select it.
        opt = document.createElement("option");
        opt.value = opts.family;
        const lbl = opts.label || opts.family;
        opt.textContent = `${lbl} (${opts.family}) — suggested alt`;
        opt.setAttribute("data-label", lbl);
        opt.setAttribute("data-alt-injected", "1");
        familySel.appendChild(opt);
      }
      if (opts.cores != null && Number.isFinite(Number(opts.cores))) {
        opt.setAttribute("data-cores", String(Math.round(Number(opts.cores))));
      }
      if (opts.currentLimit != null && Number.isFinite(Number(opts.currentLimit))) {
        opt.setAttribute("data-current-limit", String(Math.round(Number(opts.currentLimit))));
      }
      familySel.value = opts.family;
    }
    const limitInput = document.getElementById("sup-limit");
    if (limitInput) {
      delete limitInput.dataset.userEdited;
      if (opts.newLimit != null && Number.isFinite(Number(opts.newLimit))) {
        limitInput.value = String(Math.round(Number(opts.newLimit)));
      }
    }
    _supportUpdateQuotaMath({ force: true });
    const box = document.getElementById("sup-create") || document.querySelector(".support-form-grid");
    if (box) box.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 60);
}

function _applyBlockerFilter(regionShort) {
  const body = document.getElementById("support-blockers-body");
  if (!body) return;
  const want = (regionShort || "").toLowerCase();
  body.querySelectorAll("tr[data-blocker-region]").forEach(tr => {
    const r = (tr.getAttribute("data-blocker-region") || "").toLowerCase();
    tr.classList.toggle("hidden", !!want && r !== want);
  });
}

function _wireSupportTab(view) {
  _supportSyncKindFields();
  const kindSel = view.querySelector("#sup-kind");
  if (kindSel) kindSel.addEventListener("change", _supportSyncKindFields);
  const regionSel = view.querySelector("#sup-region");
  if (regionSel) regionSel.addEventListener("change", () => _supportUpdateQuotaMath({ force: true }));
  const familySel = view.querySelector("#sup-family");
  if (familySel) familySel.addEventListener("change", () => _supportUpdateQuotaMath({ force: true }));
  const limitInput = view.querySelector("#sup-limit");
  if (limitInput) limitInput.addEventListener("input", () => { limitInput.dataset.userEdited = "1"; });
  const blockerFilter = view.querySelector("#support-blocker-filter");
  if (blockerFilter) {
    blockerFilter.addEventListener("change", () => _applyBlockerFilter(blockerFilter.value));
    // Default to a single region so the list doesn't fill the screen.
    _applyBlockerFilter(blockerFilter.value);
  }
  const azureRefresh = view.querySelector("#sup-azure-refresh");
  if (azureRefresh) azureRefresh.addEventListener("click", _supportLoadAzureTickets);
  const azureFilter = view.querySelector("#sup-azure-filter");
  if (azureFilter) azureFilter.addEventListener("change", _supportLoadAzureTickets);
  const azureBody = view.querySelector("#support-azure-tickets-body");
  if (azureBody) azureBody.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-azure-ticket-close]");
    if (!btn) return;
    _supportCloseAzureTicket(
      btn.getAttribute("data-azure-ticket-close"),
      btn.getAttribute("data-azure-ticket-sub"),
      btn.getAttribute("data-azure-ticket-title"),
      btn
    );
  });
  _supportLoadAzureTickets();
  const sub = view.querySelector("#sup-submit");
  if (sub) sub.addEventListener("click", () => _supportCreate());

  view.querySelector("#support-blockers-body").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-prefill]");
    if (!btn) return;
    _supportPrefill(btn.getAttribute("data-prefill"), btn.getAttribute("data-region"));
  });

  // Remediation plan: region chips + "File ticket" buttons prefill the form.
  const remedy = view.querySelector(".remediation-plan");
  if (remedy) remedy.addEventListener("click", (ev) => {
    const chip = ev.target.closest("[data-remedy-region]");
    if (chip) {
      const kind = chip.getAttribute("data-remedy-kind");
      const region = chip.getAttribute("data-remedy-region");
      if (kind) _supportPrefill(kind, region);
      else { const r = _findRegionByShort(region); if (r) { switchView("regions"); openDrilldown(r); } }
      return;
    }
    const fileBtn = ev.target.closest(".remedy-file-all");
    if (fileBtn) {
      _supportPrefill(fileBtn.getAttribute("data-remedy-kind"), fileBtn.getAttribute("data-remedy-first"));
    }
  });
}

// ---------------------------------------------------------------- Wiring

function init() {
  // Sync persisted sidebar state from <html> marker class to .layout
  applySidebarStateFromStorage();
  applyBomnavStateFromStorage();

  // Theme toggle: pre-paint inline script set the initial mode; this wires
  // up the button + system-preference watcher.
  initThemeController();

  setupGettingStarted();

  document.getElementById("signin-chip").addEventListener("click", onSigninChipClick);
  document.getElementById("signin-modal-close").addEventListener("click", closeSigninModal);
  document.getElementById("signin-overlay").addEventListener("click", closeSigninModal);
  document.getElementById("btn-signout").addEventListener("click", doSignOut);
  document.getElementById("btn-switch-dir").addEventListener("click", doSwitchDirectory);
  document.getElementById("filters-handle").addEventListener("click", toggleSidebar);
  document.getElementById("bomnav-handle").addEventListener("click", toggleBomnav);
  document.getElementById("clear-filters").addEventListener("click", clearAllFilters);

  document.getElementById("filter-search").addEventListener("input", applyFilters);
  document.getElementById("filter-subscription").addEventListener("change", (ev) => {
    syncActiveSubscription(ev.target.value || null);
    renderSubscriptionFilter();
    renderSubscriptionSwitcher();
    renderBomPanel();
    applyFilters();
    if (STATE.view === "quota") renderQuotaTab();
    if (STATE.activeDrilldownRegion) {
      const region = _findRegionByShort(STATE.activeDrilldownRegion);
      if (region) openDrilldown(region);
    }
  });
  document.querySelectorAll('[data-filter="verdict"]').forEach(el => el.addEventListener("change", applyFilters));
  document.querySelectorAll('[data-filter="quota"]').forEach(el => el.addEventListener("change", applyFilters));
  document.querySelectorAll('[data-filter="az"]').forEach(el => el.addEventListener("change", applyFilters));
  document.getElementById("filter-missing-services").addEventListener("change", applyFilters);
  document.getElementById("filter-v5-fallback").addEventListener("change", applyFilters);
  document.getElementById("filter-restricted-only").addEventListener("change", applyFilters);
  document.getElementById("pending-quota-panel").addEventListener("click", (ev) => {
    const toggle = ev.target.closest("[data-pending-quota-toggle]");
    if (!toggle) return;
    STATE.pendingQuotaPanelCollapsed = !STATE.pendingQuotaPanelCollapsed;
    renderPendingQuotaPanel();
  });
  document.getElementById("quota-hierarchy-panel").addEventListener("click", (ev) => {
    const toggle = ev.target.closest("[data-quota-hierarchy-toggle]");
    if (!toggle) return;
    STATE.quotaHierarchyCollapsed = !STATE.quotaHierarchyCollapsed;
    _renderQuotaForSelectedRegion();
  });

  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => switchView(t.dataset.view)));
  document.querySelectorAll(".region-subtab").forEach(t => t.addEventListener("click", () => switchRegionsSub(t.dataset.sub)));
  const openSettingsBtn = document.getElementById("open-settings");
  if (openSettingsBtn) openSettingsBtn.addEventListener("click", () => switchView("settings"));
  const ownerSaveBtn = document.getElementById("owner-save");
  if (ownerSaveBtn) ownerSaveBtn.addEventListener("click", saveOwnerSettings);
  const ownerValRgCreateBtn = document.getElementById("owner-valrg-create");
  if (ownerValRgCreateBtn) ownerValRgCreateBtn.addEventListener("click", _createValidationRg);
  const ownerWipeBtn = document.getElementById("owner-wipe");
  if (ownerWipeBtn) ownerWipeBtn.addEventListener("click", _supportWipe);
  { const ofb = document.getElementById("owner-open-folder"); if (ofb) ofb.addEventListener("click", _openSnapshotsFolder); }
  { const dsb = document.getElementById("owner-download-snapshots"); if (dsb) dsb.addEventListener("click", _downloadSnapshots); }
  { const isb = document.getElementById("owner-import-snapshots"); const ifi = document.getElementById("owner-import-file");
    if (isb && ifi) {
      isb.addEventListener("click", () => ifi.click());
      ifi.addEventListener("change", () => { const file = ifi.files && ifi.files[0]; ifi.value = ""; if (file) _importSnapshots(file); });
    } }
  document.querySelectorAll("[data-settings-tab]").forEach(btn => {
    btn.addEventListener("click", () => switchSettingsTab(btn.getAttribute("data-settings-tab")));
  });
  const pricingSaveBtn = document.getElementById("pricing-save");
  if (pricingSaveBtn) pricingSaveBtn.addEventListener("click", savePricingSettings);
  { const pc = document.getElementById("perm-check"); if (pc) pc.addEventListener("click", checkPermissions); }
  document.getElementById("btn-export-csv").addEventListener("click", exportCsv);
  document.getElementById("btn-export-xlsx").addEventListener("click", exportXlsx);
  { const vb = document.getElementById("btn-verify-all"); if (vb) vb.addEventListener("click", verifyAllRegions); }
  { const vc = document.getElementById("btn-verify-cancel"); if (vc) vc.addEventListener("click", cancelVerifyAll); }
  document.getElementById("drilldown-overlay").addEventListener("click", closeDrilldown);
  document.addEventListener("click", _handleRegisterProviderInteraction);
  document.getElementById("latency-source").addEventListener("change", refreshLatencyChart);
  document.getElementById("snapshot-compare-toggle").addEventListener("click", toggleSnapshotComparePicker);
  document.getElementById("snapshot-compare-picker").addEventListener("change", (ev) => {
    if (ev.target.value) openSnapshotDiff(ev.target.value);
  });
  document.getElementById("snapshot-diff-close").addEventListener("click", closeSnapshotDiffModal);
  document.getElementById("snapshot-diff-overlay").addEventListener("click", closeSnapshotDiffModal);

  document.querySelectorAll("#regions-table th[data-sort]").forEach(th => {
    th.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-subscription-switcher]")) return;
      const k = th.dataset.sort;
      if (STATE.sortKey === k) STATE.sortDir = -STATE.sortDir;
      else { STATE.sortKey = k; STATE.sortDir = 1; }
      applyFilters();
    });
  });

  // Top bar wiring
  document.getElementById("run-modal-close").addEventListener("click", closeRunModal);
  document.getElementById("run-cancel").addEventListener("click", closeRunModal);
  document.getElementById("run-overlay").addEventListener("click", closeRunModal);
  document.getElementById("run-go").addEventListener("click", submitRun);
  document.getElementById("run-token-signin").addEventListener("click", () => refreshAuthToken({ force: true }));
  document.getElementById("run-token-refresh").addEventListener("click", () => refreshAuthToken({ force: true }));
  document.getElementById("run-bom-pick").addEventListener("change", updateRunBomSummary);
  document.getElementById("run-bom-open-editor").addEventListener("click", (ev) => {
    ev.preventDefault();
    closeRunModal();
    openBomModal(null, { create: true });
  });

  // BOM navigator (sidebar) wiring
  document.getElementById("bomnav-new").addEventListener("click", () => openBomModal(null, { create: true }));
  document.getElementById("bomnav-search").addEventListener("input", (ev) => filterBomNav(ev.target.value));

  // BOM details/controls panel (main area)
  document.getElementById("bom-panel-run").addEventListener("click", () => { if (STATE.activeBomId) runBomFromManager(STATE.activeBomId); });
  document.getElementById("bom-panel-edit").addEventListener("click", () => { if (STATE.activeBomId) openBomModal(STATE.activeBomId); });
  document.getElementById("bom-panel-delete").addEventListener("click", () => {
    if (!STATE.activeBomId) return;
    const m = getBomMeta(STATE.activeBomId);
    deleteBomFromNav(STATE.activeBomId, (m && bomDisplayName(m)) || STATE.activeBomId);
  });
  document.addEventListener("change", (ev) => {
    const sel = ev.target && ev.target.closest ? ev.target.closest("[data-subscription-switcher]") : null;
    if (!sel) return;
    syncActiveSubscription(sel.value || null);
    renderSubscriptionFilter();
    renderSubscriptionSwitcher();
    renderBomPanel();
    applyFilters();
    if (STATE.view === "quota") renderQuotaTab();
    if (STATE.activeDrilldownRegion) {
      const region = _findRegionByShort(STATE.activeDrilldownRegion);
      if (region) openDrilldown(region);
    }
  });

  // BOM modal wiring
  document.getElementById("bom-modal-close").addEventListener("click", closeBomModal);
  { const gm = document.getElementById("bom-wizard-guide"); if (gm) gm.addEventListener("click", () => startBomWizardCoachTour()); }
  const ddClose = document.getElementById("dd-close");
  if (ddClose) ddClose.addEventListener("click", closeDrilldown);
  document.getElementById("bom-cancel").addEventListener("click", closeBomModal);
  document.getElementById("bom-overlay").addEventListener("click", closeBomModal);
  document.getElementById("bom-save").addEventListener("click", saveBom);
  document.getElementById("bom-wizard-back").addEventListener("click", bomWizardBack);
  document.getElementById("bom-wizard-next").addEventListener("click", bomWizardNext);
  document.querySelectorAll("#bom-wizard-nav .bom-wizard-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      const target = parseInt(tab.getAttribute("data-wstep"), 10) || 1;
      // Only allow jumping forward if step 1 validates.
      if (target > 1 && !bomWizardValidateStep(1)) return;
      bomWizardGoTo(target);
    });
  });
  document.getElementById("bom-skus-add").addEventListener("click", () => { addBomSkuRow(); renderBomSkuFamilyOptions(); });
  const skuRefreshBtn = document.getElementById("bom-skus-refresh");
  if (skuRefreshBtn) {
    skuRefreshBtn.addEventListener("click", async () => {
      const status = document.getElementById("bom-skus-families-status");
      if (status) status.textContent = "Refreshing from Azure…";
      await ensureBomSkuFamilies(true);
    });
  }
  document.getElementById("bom-services-filter").addEventListener("input", (ev) => filterBomServices(ev.target.value));
  document.getElementById("bom-services-select-all").addEventListener("click", () => {
    document.querySelectorAll('#bom-services-list label:not(.hidden) input[data-bom-svc]').forEach(cb => { cb.checked = true; });
    updateBomServiceCount();
  });
  document.getElementById("bom-services-clear").addEventListener("click", () => {
    document.querySelectorAll('#bom-services-list input[data-bom-svc]').forEach(cb => { cb.checked = false; });
    updateBomServiceCount();
  });
  // Live count update + delete-custom delegated handlers for the
  // services and regions pickers. Delegated so newly-inserted rows
  // (from "+ Add custom") get the handlers automatically.
  const svcList = document.getElementById("bom-services-list");
  svcList.addEventListener("change", (ev) => {
    if (ev.target && ev.target.matches('input[data-bom-svc]')) updateBomServiceCount();
  });
  svcList.addEventListener("click", (ev) => {
    const btn = ev.target.closest('button[data-del-svc]');
    if (btn) {
      ev.preventDefault();
      ev.stopPropagation();
      deleteCustomBomService(btn.getAttribute('data-del-svc'));
    }
  });
  document.getElementById("bom-custom-svc-add").addEventListener("click", addCustomBomService);

  // Regions picker
  document.getElementById("bom-regions-filter").addEventListener("change", filterBomRegions);
  document.getElementById("bom-regions-search").addEventListener("input", filterBomRegions);
  document.getElementById("bom-regions-select-all").addEventListener("click", () => {
    document.querySelectorAll('#bom-regions-list label:not(.hidden) input[data-bom-rg]').forEach(cb => { cb.checked = true; });
    updateBomRegionsCount();
  });
  document.getElementById("bom-regions-clear").addEventListener("click", () => {
    document.querySelectorAll('#bom-regions-list input[data-bom-rg]').forEach(cb => { cb.checked = false; });
    updateBomRegionsCount();
  });
  const rgList = document.getElementById("bom-regions-list");
  rgList.addEventListener("change", (ev) => {
    if (ev.target && ev.target.matches('input[data-bom-rg]')) updateBomRegionsCount();
  });
  rgList.addEventListener("click", (ev) => {
    const btn = ev.target.closest('button[data-del-rg]');
    if (btn) {
      ev.preventDefault();
      ev.stopPropagation();
      deleteCustomBomRegion(btn.getAttribute('data-del-rg'));
    }
  });
  document.getElementById("bom-custom-rg-add").addEventListener("click", addCustomBomRegion);

  // Activity log controls
  const actRefresh = document.getElementById("activity-refresh");
  if (actRefresh) actRefresh.addEventListener("click", loadActivityLog);
  const actClear = document.getElementById("activity-clear");
  if (actClear) actClear.addEventListener("click", clearActivityLog);
  for (const id of ["activity-filter-sub", "activity-filter-event",
                    "activity-filter-limit", "activity-filter-days"]) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", loadActivityLog);
  }

  (async () => {
    await loadAppConfig();
    // MSAL-only single sign-in: if the app runs in delegated (multi-customer)
    // mode, require a browser sign-in before loading any data. A silent attempt
    // reuses an existing MSAL session; otherwise a "Sign in to continue" gate is
    // shown and the app boots only after the user completes the single sign-in.
    if (APP_CONFIG && APP_CONFIG.delegated_mode) {
      const ok = await ensureSignedInOrGate();
      if (!ok) return; // gate is showing; startAppAfterAuth() runs on Sign in
    }
    await startAppAfterAuth();
  })().catch(e => console.error("init failed:", e));
}

// Everything that populates the dashboard once we have (or don't need) a signed
// -in session. Factored out so the sign-in gate can invoke it after a
// successful interactive sign-in.
async function startAppAfterAuth() {
  await hydrateStateFromLocal();
  await loadSubscriptions();
  await loadSnapshotsList();
  const picker = document.getElementById("snapshot-picker");
  await loadSnapshot(picker ? (picker.value || null) : null);
  maybeShowSettingsCoach();
  // Restore quota request history from the (browser-held) store
  await _restoreQuotaRequestsFromDb();
  // Populate the header sign-in chip (silent — never opens a browser).
  refreshAuthToken({ force: false }).then(() => {
    // Pre-load subscription names so quota displays show names not IDs.
    preloadSubscriptionNames().catch(() => {});
  }).catch(() => {});
}

// Try a silent sign-in (existing MSAL session in this tab). Returns true if
// signed in; otherwise shows the full-screen gate and returns false.
async function ensureSignedInOrGate() {
  try {
    const tok = await ensureDelegatedToken({ force: false });
    if (tok) { hideAuthGate(); return true; }
  } catch (e) { /* silent failure -> show the gate */ }
  showAuthGate();
  return false;
}

function _ensureAuthGate() {
  let g = document.getElementById("auth-gate");
  if (g) return g;
  const style = document.createElement("style");
  style.textContent = `
    #auth-gate{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;
      justify-content:center;background:linear-gradient(135deg,#0b1220,#1b2a4a);}
    #auth-gate .auth-card{background:#ffffff;color:#1a2233;
      max-width:440px;width:92%;padding:2.25rem;border-radius:16px;text-align:center;
      box-shadow:0 24px 70px rgba(0,0,0,.55);font-family:system-ui,sans-serif;}
    #auth-gate h2{margin:.25rem 0 .5rem;font-size:1.4rem;color:#0f1b33;font-weight:700;}
    #auth-gate p{margin:.25rem 0 1.4rem;color:#41506b;font-size:.96rem;line-height:1.5;}
    #auth-gate button{background:#2f6feb;color:#fff;border:0;border-radius:10px;
      padding:.8rem 1.6rem;font-size:1.02rem;cursor:pointer;font-weight:600;
      box-shadow:0 6px 18px rgba(47,111,235,.4);transition:background .15s;}
    #auth-gate button:hover:not(:disabled){background:#1f5fdb;}
    #auth-gate button:disabled{opacity:.6;cursor:default;}
    #auth-gate .auth-err{color:#d13438;min-height:1.2em;margin-top:.9rem;font-size:.9rem;}
    #auth-gate .brand{font-size:2.2rem;margin-bottom:.4rem;}
    #auth-gate.hidden{display:none;}`;
  document.head.appendChild(style);
  g = document.createElement("div");
  g.id = "auth-gate";
  g.innerHTML = `
    <div class="auth-card" role="dialog" aria-modal="true" aria-labelledby="auth-gate-title">
      <div class="brand">☁️</div>
      <h2 id="auth-gate-title">Azure BOM Region Dashboard</h2>
      <p>Sign in with your Microsoft Entra account to analyze your Bill of Materials
         against Azure region availability, quota and support. Your data stays in your
         browser — nothing is stored on the server.</p>
      <button type="button" data-gate-signin>Sign in to continue</button>
      <div class="auth-err" id="auth-gate-err" aria-live="polite"></div>
    </div>`;
  document.body.appendChild(g);
  g.querySelector("[data-gate-signin]").addEventListener("click", onGateSignIn);
  return g;
}

function showAuthGate() { _ensureAuthGate().classList.remove("hidden"); }
function hideAuthGate() {
  const g = document.getElementById("auth-gate");
  if (g) g.classList.add("hidden");
}

async function onGateSignIn(ev) {
  const btn = ev.currentTarget;
  const err = document.getElementById("auth-gate-err");
  btn.disabled = true;
  if (err) err.textContent = "Opening Microsoft sign-in…";
  try {
    const tok = await ensureDelegatedToken({ force: true }); // interactive popup
    if (!tok) throw new Error("Sign-in was cancelled. Please try again.");
    if (err) err.textContent = "";
    hideAuthGate();
    await startAppAfterAuth();
  } catch (e) {
    if (err) err.textContent = (e && e.message) ? e.message : "Sign-in failed. Please try again.";
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", init);

// ---------------------------------------------------------------- Coachmark
// First-run "Start here" pointer at the Settings gear, nudging the user to set
// their support contact and refresh region / latency / SKU data *before*
// building a BOM. Shows once, then remembers dismissal in localStorage. It is
// also dismissed the moment the user opens Settings by any means.
const COACH_KEY = "coach_settings_done";

function _coachDone() {
  try { return localStorage.getItem(COACH_KEY) === "1"; } catch (e) { return false; }
}

function dismissSettingsCoach(remember) {
  const el = document.getElementById("settings-coach");
  if (el) el.remove();
  const gear = document.getElementById("open-settings");
  if (gear) gear.classList.remove("coach-pulse");
  if (window.__coachReposition) {
    window.removeEventListener("resize", window.__coachReposition);
    window.removeEventListener("scroll", window.__coachReposition, true);
    window.__coachReposition = null;
  }
  if (remember) { try { localStorage.setItem(COACH_KEY, "1"); } catch (e) {} }
}

function maybeShowSettingsCoach() {
  if (_coachDone()) return;
  const gear = document.getElementById("open-settings");
  if (!gear || document.getElementById("settings-coach")) return;

  const coach = document.createElement("div");
  coach.id = "settings-coach";
  coach.className = "coach";
  coach.setAttribute("role", "dialog");
  coach.setAttribute("aria-label", "Getting started");
  coach.innerHTML = `
    <div class="coach-arrow" aria-hidden="true"></div>
    <div class="coach-body">
      <div class="coach-title">👋 Start here</div>
      <p class="coach-text">Open <strong>Settings</strong> to set your support contact and
        <strong>refresh your region, latency &amp; SKU data from Azure</strong> before building your first BOM.</p>
      <div class="coach-actions">
        <button type="button" class="btn btn--accent btn--sm" data-coach="open">Open Settings</button>
        <button type="button" class="link-btn" data-coach="dismiss">Maybe later</button>
      </div>
    </div>`;
  document.body.appendChild(coach);
  gear.classList.add("coach-pulse");

  const place = () => {
    const r = gear.getBoundingClientRect();
    coach.style.top = Math.round(r.bottom + 12) + "px";
    const rightGap = Math.max(12, Math.round(window.innerWidth - r.right - 2));
    coach.style.right = rightGap + "px";
    // Point the arrow up at the gear's horizontal centre.
    coach.style.setProperty("--coach-arrow-right",
      Math.round(gear.offsetWidth / 2 - 6) + "px");
  };
  place();
  window.__coachReposition = place;
  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);

  const openBtn = coach.querySelector('[data-coach="open"]');
  if (openBtn) openBtn.addEventListener("click", () => {
    dismissSettingsCoach(true);
    switchView("settings");
  });
  const dismissBtn = coach.querySelector('[data-coach="dismiss"]');
  if (dismissBtn) dismissBtn.addEventListener("click", () => dismissSettingsCoach(true));
  // Any click that opens Settings should also retire the coachmark.
  gear.addEventListener("click", () => dismissSettingsCoach(true), { once: true });
}

// ---------------------------------------------------------------- Coach-mark tour engine
//
// A reusable spotlight walkthrough: dims the page, rings a real UI element, and
// shows an arrow bubble ("click here / fill this out / this does xyz") with
// Back / Next / Skip. Each step can run a `before()` hook to open the right view,
// modal or wizard step before pointing at its `target` (a CSS selector or a
// function returning an element). Missing targets are skipped gracefully.

const CM_TOUR = { steps: null, i: 0, ring: null, bubble: null, arrow: null, inner: null, target: null, reposition: null, token: 0 };

function _cmSleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function _cmClamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

async function _cmResolveTarget(target, tries) {
  tries = tries || 12;
  for (let k = 0; k < tries; k++) {
    let el = null;
    try { el = (typeof target === "function") ? target() : document.querySelector(target); }
    catch (_e) { el = null; }
    if (el && el.getBoundingClientRect && el.offsetParent !== null) return el;
    await _cmSleep(120);
  }
  // Last attempt even if offsetParent is null (e.g. fixed elements).
  try { return (typeof target === "function") ? target() : document.querySelector(target); }
  catch (_e) { return null; }
}

function stopCoachmarkTour() {
  CM_TOUR.token++;
  if (CM_TOUR.reposition) {
    window.removeEventListener("resize", CM_TOUR.reposition);
    window.removeEventListener("scroll", CM_TOUR.reposition, true);
    CM_TOUR.reposition = null;
  }
  [CM_TOUR.ring, CM_TOUR.bubble].forEach(el => { if (el && el.parentNode) el.parentNode.removeChild(el); });
  CM_TOUR.ring = CM_TOUR.bubble = CM_TOUR.arrow = CM_TOUR.inner = CM_TOUR.target = CM_TOUR.steps = null;
  CM_TOUR.i = 0;
}

function startCoachmarkTour(steps, opts) {
  opts = opts || {};
  stopCoachmarkTour();
  const list = (steps || []).filter(Boolean);
  if (!list.length) return;
  const myToken = ++CM_TOUR.token;
  CM_TOUR.steps = list;
  CM_TOUR.i = 0;

  const ring = document.createElement("div"); ring.className = "cm-ring";
  const bubble = document.createElement("div");
  bubble.className = "cm-bubble";
  bubble.setAttribute("role", "dialog");
  bubble.setAttribute("aria-label", "Guided walkthrough");
  const arrow = document.createElement("div"); arrow.className = "cm-arrow";
  const inner = document.createElement("div"); inner.className = "cm-inner";
  bubble.appendChild(arrow); bubble.appendChild(inner);
  document.body.appendChild(ring); document.body.appendChild(bubble);
  CM_TOUR.ring = ring; CM_TOUR.bubble = bubble; CM_TOUR.arrow = arrow; CM_TOUR.inner = inner;

  function place() {
    const t = CM_TOUR.target;
    if (!t) return;
    const r = t.getBoundingClientRect();
    const pad = 6;
    ring.style.top = (r.top - pad) + "px";
    ring.style.left = (r.left - pad) + "px";
    ring.style.width = (r.width + pad * 2) + "px";
    ring.style.height = (r.height + pad * 2) + "px";

    const bw = bubble.offsetWidth, bh = bubble.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight, gap = 14;
    let placement;
    if (r.bottom + gap + bh <= vh) placement = "bottom";
    else if (r.top - gap - bh >= 0) placement = "top";
    else if (r.right + gap + bw <= vw) placement = "right";
    else placement = "left";

    arrow.style.top = arrow.style.left = arrow.style.right = arrow.style.bottom = "";
    let top, left;
    if (placement === "bottom" || placement === "top") {
      left = _cmClamp(r.left + r.width / 2 - bw / 2, 8, vw - bw - 8);
      const ax = _cmClamp(r.left + r.width / 2 - left - 7, 12, bw - 20);
      if (placement === "bottom") { top = r.bottom + gap; arrow.style.top = "-7px"; }
      else { top = r.top - gap - bh; arrow.style.bottom = "-7px"; }
      arrow.style.left = ax + "px";
    } else {
      top = _cmClamp(r.top + r.height / 2 - bh / 2, 8, vh - bh - 8);
      const ay = _cmClamp(r.top + r.height / 2 - top - 7, 12, bh - 20);
      if (placement === "right") { left = r.right + gap; arrow.style.left = "-7px"; }
      else { left = r.left - gap - bw; arrow.style.right = "-7px"; }
      arrow.style.top = ay + "px";
    }
    bubble.style.top = Math.round(top) + "px";
    bubble.style.left = Math.round(left) + "px";
  }
  CM_TOUR.reposition = place;
  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);

  async function show() {
    if (myToken !== CM_TOUR.token) return; // superseded/stopped
    const step = CM_TOUR.steps[CM_TOUR.i];
    if (!step) { finish("done"); return; }
    if (step.before) { try { await step.before(); } catch (_e) {} }
    if (myToken !== CM_TOUR.token) return;
    const el = await _cmResolveTarget(step.target);
    if (myToken !== CM_TOUR.token) return;
    if (!el) { CM_TOUR.i++; return show(); }
    CM_TOUR.target = el;
    try { el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" }); } catch (_e) {}
    await _cmSleep(step.settle || 240);
    if (myToken !== CM_TOUR.token) return;

    const last = CM_TOUR.i === CM_TOUR.steps.length - 1;
    inner.innerHTML =
      `<div class="cm-count">Step ${CM_TOUR.i + 1} of ${CM_TOUR.steps.length}</div>` +
      `<div class="cm-title">${escapeHtml(step.title || "")}</div>` +
      `<div class="cm-text">${step.text || ""}</div>` +
      `<div class="cm-actions">` +
        `<button type="button" class="link-btn" data-cm="skip">Skip</button>` +
        `<span class="cm-spacer"></span>` +
        (CM_TOUR.i > 0 ? `<button type="button" class="btn btn--sm" data-cm="back">Back</button>` : "") +
        `<button type="button" class="btn btn--accent btn--sm" data-cm="next">${last ? "Done" : "Next →"}</button>` +
      `</div>`;
    inner.querySelectorAll("[data-cm]").forEach(b => b.addEventListener("click", () => {
      const a = b.dataset.cm;
      if (a === "skip") return finish("skip");
      if (a === "back") { CM_TOUR.i = Math.max(0, CM_TOUR.i - 1); return show(); }
      if (CM_TOUR.i >= CM_TOUR.steps.length - 1) return finish("done");
      CM_TOUR.i++; show();
    }));
    place();
  }

  function finish(reason) {
    if (myToken !== CM_TOUR.token) return;
    stopCoachmarkTour();
    if (typeof opts.onDone === "function") { try { opts.onDone(reason || "done"); } catch (_e) {} }
  }

  show();
}

// Contextual walkthrough of the Settings screen — points at the ticket-owner
// fields and the datasets refresh. Launched from Getting Started step 2.
function startSettingsCoachTour() {
  startCoachmarkTour([
    {
      target: '#owner-first',
      title: "Set your ticket owner",
      text: "Fill in the contact <strong>name</strong>, <strong>email</strong> and <strong>country</strong>. " +
        "Azure requires these on every support ticket — the dashboard reuses them as the defaults.",
      before: () => switchSettingsTab("owner"),
    },
    {
      target: '#owner-save',
      title: "Save the owner",
      text: "Click <strong>Save owner</strong>. It's stored in your browser only — nothing goes to the server.",
    },
    {
      target: '[data-settings-tab="datasets"]',
      title: "Refresh your Azure data",
      text: "Now open <strong>Model datasets</strong> — the regions, latency and SKU reference data the analysis is built on.",
      before: () => switchSettingsTab("owner"),
    },
    {
      target: '#datasets-list',
      title: "Pull the latest from Azure",
      text: "Click <strong>Refresh from Azure</strong> on each dataset so your analysis uses current region &amp; SKU availability. " +
        "That's it — next we'll create your first BOM.",
      before: () => switchSettingsTab("datasets"),
    },
  ], {
    // When the Settings hand-off finishes, bring the user back to the guide at
    // the next step ("Create a BOM") so they always know what to do next.
    // Only on genuine completion — a Skip leaves them where they are.
    onDone: (reason) => {
      if (reason === "done") setTimeout(() => reopenGettingStarted(2), 300);
    },
  });
}

// Contextual walkthrough of the BOM wizard — points at each field and what it
// drives. Launched from Getting Started step 3 after the wizard opens.
function startBomWizardCoachTour() {
  startCoachmarkTour([
    // ---- Step 1 · Basics -----------------------------------------------
    {
      target: '#bom-tag',
      title: "Step 1 · Basics — Name the BOM",
      text: "This is a <strong>Bill of Materials</strong> for one deployment. Give it a short, memorable label (e.g. <code>Contoso-Prod</code>) so you can tell BOMs apart in the left-hand list.",
      before: () => bomWizardGoTo(1),
    },
    {
      target: '#bom-customer',
      title: "Customer name",
      text: "Optional. The customer or team this BOM belongs to — shown next to the name for context.",
    },
    {
      target: '#bom-resilience',
      title: "Availability target",
      text: "How the workload is deployed. <strong>Zone-redundant</strong> spreads across Availability Zones, so a tier that's restricted from zone redundancy in a region becomes a <strong>hard blocker</strong>. <strong>Regional</strong> makes those restrictions advisory only.",
    },
    {
      target: '#bom-preferred-region',
      title: "Preferred source region",
      text: "Optional: the customer's primary region. Latency to every other region is measured <em>from</em> here, and it becomes the default source on the Latency tab.",
    },
    {
      target: '#bom-owner-first',
      title: "Support contact",
      text: "Who Azure support tickets for <em>this</em> BOM are filed under. Defaults to your global Settings and can be overridden here. Expand <strong>Additional support details</strong> for phone, severity and CCs.",
    },
    {
      target: '#bom-sub',
      title: "Subscriptions",
      text: "Select the customer subscription(s) to analyze — hold Ctrl/Cmd for multiple. SKU, quota and region availability are all read from these.",
    },
    // ---- Step 2 · Services & Regions -----------------------------------
    {
      target: '#bom-services-list',
      title: "Step 2 · Services",
      text: "Pick the Azure <strong>services</strong> in the architecture. The dashboard checks each one's per-region availability when you run the BOM.",
      before: () => bomWizardGoTo(2),
    },
    {
      target: '#bom-regions-list',
      title: "Regions",
      text: "Choose the <strong>regions</strong> to score. Leave the full list checked to compare all of Azure, or narrow it to the regions a customer actually cares about.",
      before: () => bomWizardGoTo(2),
    },
    // ---- Step 3 · SKUs & Capacity --------------------------------------
    {
      target: '#bom-skus-tbody',
      title: "Step 3 · SKUs & cores",
      text: "Add the VM <strong>SKU families</strong> and the <strong>required cores</strong>. This drives the <strong>Quota Status</strong> check — it's what tells you where the customer needs a quota increase.",
      before: () => bomWizardGoTo(3),
    },
    {
      target: '#bom-service-tiers-step',
      title: "Service tiers",
      text: "Some services offer tiers (e.g. SQL Basic / Premium). Tiers marked <em>zone-redundant capable</em> are validated by the region readiness check.",
      before: () => bomWizardGoTo(3),
    },
    {
      target: '#bom-save',
      title: "Save, then run",
      text: "Click <strong>Save</strong> to store the BOM, then hit <strong>▶ Refresh analysis</strong> in the BOM's panel to score every region for blockers, quota and readiness.",
      before: () => bomWizardGoTo(3),
    },
  ]);
}
