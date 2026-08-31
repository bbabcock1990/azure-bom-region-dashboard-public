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
  const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers }));
  return res;
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
  const disabled = ids.length <= 1 ? " disabled" : "";
  hosts.forEach((host) => {
    const isQuotaTab = host.id === "quota-subscription-control";
    host.innerHTML = `<label class="quota-control quota-control--subscription${isQuotaTab ? " quota-control--inline" : ""}" data-subscription-switcher-wrap="1">
      <span>${isQuotaTab ? "Subscription:" : "Viewing quota for"}</span>
      <select data-subscription-switcher="1" aria-label="Select the subscription context for quota views"${disabled}>${options}</select>
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
  return {
    search,
    verdict: new Set(verdictChecked),
    continent: new Set(continentChecked),
    v6Only: document.getElementById("filter-v6-only").checked,
    v5Fallback: document.getElementById("filter-v5-fallback").checked,
    restrictedOnly: document.getElementById("filter-restricted-only").checked,
  };
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
    // primary_used / fell_back are the generic fields emitted by current
    // engine. Fall back to legacy v6_viable / sku_fallbacks shape so old
    // snapshots keep filtering correctly.
    const primaryUsed = (r.primary_used != null) ? r.primary_used : r.v6_viable;
    const fellBack = (r.fell_back != null) ? r.fell_back : ((r.sku_fallbacks || []).length > 0);
    if (f.v6Only && !primaryUsed) return false;
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
  for (const r of STATE.filtered) {
    const tr = document.createElement("tr");
    tr.dataset.region = r.name;
    const deployment = getDeploymentVerdictInfo(r);

    const zoneHtml = r.zone_health.map((z, i) =>
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
      <td><span class="pill ${deployment.cls}" title="${escapeHtml(deployment.title)}">${escapeHtml(deployment.text)}</span></td>
      <td>${escapeHtml(r.geo || "")}</td>
      <td><span class="zone-cells">${zoneHtml}</span></td>
      ${quotaCellHtml}
      <td class="rec-cell">${escapeHtml(r.recommendation || "—")}</td>
      <td class="alt-cell">${escapeHtml((r.alt_regions || []).map(a =>
        a.latency_ms != null ? `${a.region} (${a.latency_ms}ms)` : a.region
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
  html += renderDeploymentReadinessSection(r, deployment);

  const ddVerdict = getRegionQuotaVerdictForSubscription(STATE.snapshot, r.short, focusedSubscriptionId());
  const ddVerdictPill = _regionQuotaVerdictLabel(ddVerdict);
  const ddVerdictHtml = `<span class="pill ${ddVerdictPill.cls}" title="${escapeHtml(ddVerdictPill.title)}">${escapeHtml(ddVerdictPill.text)}</span>`;
  html += `<h4>Summary</h4>
    <div class="kv">
      <div class="key">Status</div><div><span class="status-pill ${statusClass(r.status)}">${escapeHtml(r.status)}</span></div>
      <div class="key">Region (short)</div><div>${escapeHtml(r.short)}</div>
      <div class="key">Quota</div><div>${ddVerdictHtml}</div>
    </div>`;

  if (r.sku_zone_detail && Object.keys(r.sku_zone_detail).length) {
    // Determine which SKU families are BOM primary vs fallback
    const reqs = _getCoresRequirements(STATE.snapshot || {});
    const primaryLabels = new Set(reqs.map(rq => (rq.primary_label || "").toLowerCase()));
    // Unified zone + SKU availability section
    html += `<h4>Zone &amp; SKU Availability</h4>
      <div class="kv">`;
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
      html += `<div class="key">${label}</div><div>${cells}</div>`;
    }
    html += `</div>`;
    // Show SKU blockers summary (zone grid already shows per-AZ red/green)
    const skuBlockers = r.sku_blockers || [];
    if (skuBlockers.length) {
      html += `<div class="drilldown-zone-restrictions">`;
      for (const issue of skuBlockers) {
        html += `<div class="note danger">${escapeHtml(issue)}</div>`;
      }
      html += `</div>`;
    }
  } else {
    // Fallback: just show zone health if no SKU detail available
    html += `<h4>Zone Health</h4>
      <div class="kv">`;
    for (let i = 0; i < 3; i++) {
      const z = r.zone_health[i];
      const restriction = r.zone_restrictions[i] || "(none reported)";
      html += `<div class="key">AZ ${i + 1}</div>
        <div><span class="zone-cell ${z}" style="margin-right:6px">${i + 1}</span> ${escapeHtml(restriction)}</div>`;
    }
    html += `</div>`;
  }

  if (r.chosen_skus && r.chosen_skus.length && !(r.sku_zone_detail && Object.keys(r.sku_zone_detail).length)) {
    html += `<h4>Recommended SKUs</h4>`;
    for (const sku of r.chosen_skus) {
      html += `<div class="note">${escapeHtml(sku)}</div>`;
    }
  }

  if (r.sku_fallbacks && r.sku_fallbacks.length) {
    html += `<h4>v5 Fallbacks</h4>`;
    for (const f of r.sku_fallbacks) {
      html += `<div class="note warn">${escapeHtml(f)}</div>`;
    }
  }

  if (r.missing_services && r.missing_services.length) {
    html += `<h4>Missing BOM Services</h4>`;
    for (const ms of r.missing_services) {
      html += `<div class="note danger"><strong>${escapeHtml(ms.service)}</strong>: ${escapeHtml(ms.detail)}</div>`;
    }
  }

  if (r.registration_required && r.registration_required.length) {
    html += `<h4>Registration Required</h4>`;
    html += _registrationRequiredHtml(r.registration_required);
  }

  if (r.alt_regions && r.alt_regions.length) {
    html += `<h4>Alternative regions based on health and latency</h4>`;
    for (const a of r.alt_regions) {
      const ms = a.latency_ms != null ? `${a.latency_ms} ms` : "geo proximity";
      html += `<div class="alt-row"><span>${escapeHtml(a.region)}</span><span class="ms">${ms}</span></div>`;
    }
  }

  const quotaResult = buildQuotaGroupRowsForRegion(STATE.snapshot, r.short);
  const quotaPill = _quotaGroupStatusLabel(_deriveQuotaRegionStatus(quotaResult.rows, r.quota_status || "unknown"));
  if (quotaResult.rows.length) {
    html += renderDrilldownQuotaSection(r, quotaResult, quotaPill);
  }

  body.innerHTML = html;
  renderSubscriptionSwitcher();
  _scanRegistrationCards(body);
  if (!body._quotaRequestBound) {
    body.addEventListener("click", _handleQuotaRequestInteraction);
    body._quotaRequestBound = true;
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
  switch (verdict) {
    case "ready":
      return {
        verdict,
        text: "Ready",
        cls: "pill-ok",
        title: "All required services, SKUs, and quota checks passed.",
        reasons,
        constraints,
        blockers,
      };
    case "ready_with_constraints":
      return {
        verdict,
        text: "Ready with constraints",
        cls: "pill-warn",
        title: "Core requirements pass, but there are caveats to validate before deployment.",
        reasons,
        constraints,
        blockers,
      };
    case "not_recommended":
      return {
        verdict,
        text: "Not recommended",
        cls: "pill-fail",
        title: "Critical blockers make this region a poor deployment target.",
        reasons,
        constraints,
        blockers,
      };
    case "needs_validation":
    default:
      return {
        verdict: "needs_validation",
        text: "Needs validation",
        cls: "pill-muted",
        title: "Automated checks could not fully validate deployment readiness.",
        reasons,
        constraints,
        blockers,
      };
  }
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

  let html = `<h4>Deployment Readiness</h4>
    <div class="deployment-readiness">
      <div class="deployment-readiness-header">
        <span class="pill pill-lg ${deployment.cls}" title="${escapeHtml(deployment.title)}">${escapeHtml(deployment.text)}</span>
        <span class="dd-verdict-desc">${escapeHtml(verdictDesc[deployment.verdict] || "")}</span>
      </div>`;

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
  const subName = focusedSubscriptionName();
  const cards = quotaResult.rows.map((row) => _renderDrilldownQuotaCard(row)).join("");
  return `<h4>Quota <span class="pill ${quotaPill.cls}" style="margin-left:.5rem;font-size:.7rem;vertical-align:middle;">${escapeHtml(quotaPill.text)}</span>${subName ? `<span class="dd-sub-context">Focused sub: ${escapeHtml(subName)}</span>` : ""}</h4>
    <div class="quota-cards" data-region="${escapeHtml(region.short || "")}">${cards}</div>`;
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
    failingRows.forEach(r => {
      if (r.family) relevant.add(r.family.toLowerCase());
      if (r.alt_family) relevant.add(r.alt_family.toLowerCase());
    });
    const evaluated = nonBomSubs.map(s => {
      const scan = cachedDonors.results[s.id] || null;
      if (!scan || scan.status !== "ok") return null;
      const fams = scan.families || {};
      let bestFree = 0;
      const chips = [];
      for (const [fam, info] of Object.entries(fams)) {
        if (!relevant.has(fam.toLowerCase())) continue;
        const fr = info && info.headroom != null ? info.headroom : 0;
        if (fr > 0) { bestFree = Math.max(bestFree, fr); chips.push({ label: _donorFamilyLabel(fam, result.rows), free: fr }); }
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
      evaluated.forEach(e => (e.chips || []).forEach(c => {
        perSkuTotals.set(c.label, (perSkuTotals.get(c.label) || 0) + (Number(c.free) || 0));
      }));
      const skuTotalChips = [...perSkuTotals.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([label, free]) => `<span class="qd-chip qd-chip--total">${escapeHtml(label)} · ${_formatQuotaNumber(free)} free</span>`)
        .join("");
      const totalSummary = `<div class="qd-donor-total">
        <div class="qd-donor-total-head">Total free to pull across ${evaluated.length} donor subscription${evaluated.length === 1 ? "" : "s"}</div>
        <div class="qd-donor-total-chips">${skuTotalChips || `<span class="qd-chip qd-chip--total">${_formatQuotaNumber(totalFree)} vCPU</span>`}</div>
      </div>`;
      const cards = evaluated.map(({ s, chips, bestFree, coversAll }) => {
        const badge = coversAll
          ? `<span class="qd-donor-badge qd-donor-badge--ok">Can cover shortfall</span>`
          : `<span class="qd-donor-badge qd-donor-badge--partial">Partial · ${_formatQuotaNumber(bestFree)} free</span>`;
        const chipHtml = chips.map(c => `<span class="qd-chip">${escapeHtml(c.label)} · ${_formatQuotaNumber(c.free)} free</span>`).join("");
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
  return STATE.filtered.map(r => ({
    Region: r.name,
    Country: r.country,
    Geo: r.geo,
    "AZ1 Health": r.zone_health[0],
    "AZ2 Health": r.zone_health[1],
    "AZ3 Health": r.zone_health[2],
    Status: r.status,
    "SKU Recommendation": r.recommendation,
    "Chosen SKUs": (r.chosen_skus || []).join("; "),
    "SKU Blockers": (r.sku_blockers || []).join(" | "),
    "Fallbacks Used": (r.sku_fallbacks || []).join(" | "),
    "Missing Services": (r.missing_services || []).map(m => `${m.service}: ${m.detail}`).join(" | "),
    "Zone Restrictions": (r.zone_restrictions || []).map((rest, i) => rest ? `AZ${i + 1}: ${rest}` : "").filter(Boolean).join(" | "),
    "Alternative Regions": (r.alt_regions || []).map(a => a.latency_ms != null ? `${a.region} (${a.latency_ms}ms)` : a.region).join("; "),
  }));
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

function clearAllFilters() {
  document.getElementById("filter-search").value = "";
  STATE.activeSubscription = null;
  syncActiveSubscription();
  renderSubscriptionFilter();
  renderSubscriptionSwitcher();
  document.querySelectorAll('[data-filter="verdict"]').forEach(el => { el.checked = true; });
  document.querySelectorAll('[data-filter="continent"]').forEach(el => { el.checked = true; });
  document.getElementById("filter-v6-only").checked = false;
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
  document.getElementById("bom-services-filter").value = "";
  document.getElementById("bom-regions-search").value = "";
  document.getElementById("bom-regions-filter").value = "all";
  document.getElementById("bom-skus-tbody").innerHTML = "";
  document.getElementById("bom-import-file").value = "";
  BOM_EDIT.current = null;

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
    .map(cb => ({ name: cb.value }));
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
  setBomSelectedServices((meta.services || []).map(s => s.name));
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
    tag, customer_name,
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

async function importBomFromXlsx() {
  const file = document.getElementById("bom-import-file").files[0];
  if (!file) return setBomStatus("Pick an xlsx file first.", "error");
  setBomStatus("Importing…", "info");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await apiFetch("/api/bom/import_xlsx", { method: "POST", body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      return setBomStatus(errLine(body, r.statusText), "error");
    }
    if (body.customer_name && !document.getElementById("bom-customer").value.trim()) {
      document.getElementById("bom-customer").value = body.customer_name;
    }
    if (Array.isArray(body.services)) {
      setBomSelectedServices(body.services.map(s => s.name || s));
    }
    if (Array.isArray(body.required_skus) && body.required_skus.length) {
      document.getElementById("bom-skus-tbody").innerHTML = "";
      body.required_skus.forEach(addBomSkuRow);
      renderBomSkuFamilyOptions();
    }
    const warns = (body.warnings || []).map(w => `<li>${escapeHtml(w)}</li>`).join("");
    const warnHtml = warns ? `<ul style="margin:.25rem 0 0 1rem;font-size:.8rem;">${warns}</ul>` : "";
    setBomStatus(
      `Prefilled from <code>${escapeHtml(body.source_format || "xlsx")}</code>. Review and click <strong>Save BOM</strong>.${warnHtml}`,
      "info",
    );
  } catch (e) {
    setBomStatus(netErrLine(e), "error");
  }
}

// ---------------------------------------------------------------- Run modal

const TOKEN = {
  info: null,         // { token, expires_at, expires_in_seconds, az_user, ... }
  refreshTimer: null, // setTimeout handle for the countdown
};

async function refreshAuthToken({ force = false } = {}) {
  setTokenStatus("loading", force ? "Opening browser sign-in…" : "Checking sign-in…");
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
    const r = await apiFetch("/api/auth/signout", { method: "POST" });
    if (r.ok) {
      TOKEN.info = null;
      updateSigninChip();
      setTokenStatus("warn", "Signed out. Click <strong>Sign in</strong> to sign in again.");
    } else {
      const body = await r.json().catch(() => ({}));
      setTokenStatus("error", escapeHtml(body.message || "Sign-out failed"));
    }
  } catch (e) {
    setTokenStatus("error", netErrLine(e));
  }
}

async function doSwitchDirectory() {
  setTokenStatus("loading", "Signing out and re-opening sign-in…");
  try {
    await apiFetch("/api/auth/signout", { method: "POST" });
    TOKEN.info = null;
    updateSigninChip();
    // Now trigger a fresh interactive sign-in
    await refreshAuthToken({ force: true });
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

// Build a safe "<strong>code</strong>: message" line for the status banners
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

function setupGettingStarted() {
  const openBtn = document.getElementById("open-guide");
  const modal = document.getElementById("guide-modal");
  const overlay = document.getElementById("guide-overlay");
  const closeBtn = document.getElementById("guide-modal-close");
  if (!openBtn || !modal || !overlay) return;

  const open = () => { overlay.classList.remove("hidden"); modal.classList.remove("hidden"); };
  const close = () => { overlay.classList.add("hidden"); modal.classList.add("hidden"); };

  openBtn.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", close);
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
  const tabs = ["owner", "datasets", "activity"];
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
  else if (tab === "datasets") loadDatasetsSettings();
  else if (tab === "activity") loadActivityLog();
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
  const pathEl = document.getElementById("owner-storage-path");
  if (pathEl) {
    const dir = (APP_CONFIG && (APP_CONFIG.snapshots_dir || APP_CONFIG.storage_dir)) || "";
    pathEl.textContent = dir || "(path unavailable — run a live, non-demo analysis to persist snapshots locally)";
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
  applyDemoBanner();
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
  const open = (SUPPORT.tickets || []).filter(t => t.status !== "closed" && (t.azure_status || "").toLowerCase() !== "closed").length;
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

  const blockerFilterOpts = blocked.length
    ? `<option value="">All blocked regions (${blocked.length})</option>` + blocked
        .slice().sort((a, b) => (a.name || "").localeCompare(b.name || ""))
        .map(r => `<option value="${escapeHtml(r.short || "")}">${escapeHtml(r.name || r.short || "")}</option>`).join("")
    : "";

  return `
  <div class="support-wrap">
    <!-- tracked tickets removed: submitted tickets surface in the live Azure list below -->
    <div class="support-intro">
      <h2>Support tickets</h2>
      <p class="muted">Turn a deployment blocker into an Azure support request — a <strong>quota increase</strong>
      or a <strong>zonal / restricted-SKU access</strong> ticket. Preview builds the exact request with no Azure call;
      submitting files it via <code>Microsoft.Support</code>.${demo ? " <strong>Demo mode: submission is disabled.</strong>" : ""}</p>
    </div>

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
        <button type="button" class="btn" id="sup-preview">Preview (dry-run)</button>
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
      <p class="muted">Pulled live from Azure — support tickets already on your BOM subscription(s), excluding the ones created here.</p>
      <table class="support-table"><thead><tr>
        <th>Type</th><th>Title</th><th>Severity</th><th>Status</th><th>Created</th>
      </tr></thead><tbody id="support-azure-tickets-body">
        <tr><td colspan="5" class="muted">Loading live tickets from Azure…</td></tr>
      </tbody></table>
    </section>
  </div>`;
}

function _supportAzureTicketRow(t) {
  const created = (t.created_at || "").replace("T", " ").replace("Z", "").slice(0, 19);
  const status = String(t.azure_status || "");
  const statusPill = status
    ? `<span class="pill ${status.toLowerCase() === "closed" ? "pill-ok" : "pill-warn"}">${escapeHtml(status)}</span>`
    : `<span class="pill pill-muted">—</span>`;
  return `<tr>
    <td>${escapeHtml(t.kind || "support")}</td>
    <td title="${escapeHtml(t.ticket_name || "")}">${escapeHtml(t.title || t.ticket_name || "")}</td>
    <td>${escapeHtml(t.severity || "")}</td>
    <td>${statusPill}</td>
    <td>${escapeHtml(created)}</td>
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
    body.innerHTML = `<tr><td colspan="5" class="muted">Sign in and select a BOM to see live tickets.</td></tr>`;
    return;
  }
  body.innerHTML = `<tr><td colspan="5" class="muted"><span class="quota-request-spinner" aria-hidden="true"></span> Loading live tickets from Azure…</td></tr>`;

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
    body.innerHTML = `<tr><td colspan="5" class="muted">${note}</td></tr>`;
    return;
  }
  let html = filtered.map(_supportAzureTicketRow).join("");
  if (errors.length) {
    html += `<tr><td colspan="5" class="muted">Note: ${errors.length} subscription(s) could not be read (${escapeHtml(errors[0].message)}).</td></tr>`;
  }
  body.innerHTML = html;
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
  if (limitWrap) limitWrap.classList.toggle("hidden", kind !== "quota");
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
  if (kind !== "quota") { info.textContent = ""; return; }

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
  if (kind === "quota") {
    body.new_limit = parseInt((document.getElementById("sup-limit") || {}).value || "0", 10);
  } else {
    const z = ((document.getElementById("sup-zones") || {}).value || "").split(",").map(x => x.trim()).filter(Boolean);
    if (z.length) body.zones = z;
  }
  if (STATE.activeBomId) body.bom_id = STATE.activeBomId;
  return body;
}

async function _supportCreate(dryRun) {
  const body = _supportGatherForm();
  body.dry_run = dryRun;
  if (!body.subscription_id) { showToast("Pick a subscription first.", "warning"); return; }
  if (!body.region) { showToast("Pick a region first.", "warning"); return; }
  if (!body.family) { showToast("Pick a SKU family first.", "warning"); return; }
  if (!dryRun && !confirm(`Submit a real ${body.kind} support ticket to Azure for ${body.region}?`)) return;
  const box = document.getElementById("sup-preview-box");
  try {
    const res = await apiJson("/api/support/tickets", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const ticket = res.ticket;
    if (dryRun) {
      SUPPORT.lastPreview = ticket;
      if (box) { box.textContent = JSON.stringify(ticket.payload, null, 2); box.classList.remove("hidden"); }
      showToast("Preview built (no Azure call).", "success");
    } else {
      showToast(`Ticket submitted: ${ticket.azure_ticket_id || ticket.ticket_name}`, "success");
    }
    await _supportReloadTickets();
  } catch (e) {
    showToast(`Ticket failed: ${e.message}`, "error");
    if (box && e.body) { box.textContent = JSON.stringify(e.body, null, 2); box.classList.remove("hidden"); }
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
      const has = Array.from(familySel.options).some(o => o.value === opts.family);
      if (has) familySel.value = opts.family;
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
  if (blockerFilter) blockerFilter.addEventListener("change", () => _applyBlockerFilter(blockerFilter.value));
  const azureRefresh = view.querySelector("#sup-azure-refresh");
  if (azureRefresh) azureRefresh.addEventListener("click", _supportLoadAzureTickets);
  const azureFilter = view.querySelector("#sup-azure-filter");
  if (azureFilter) azureFilter.addEventListener("change", _supportLoadAzureTickets);
  _supportLoadAzureTickets();
  const prev = view.querySelector("#sup-preview");
  if (prev) prev.addEventListener("click", () => _supportCreate(true));
  const sub = view.querySelector("#sup-submit");
  if (sub) sub.addEventListener("click", () => _supportCreate(false));

  view.querySelector("#support-blockers-body").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-prefill]");
    if (!btn) return;
    _supportPrefill(btn.getAttribute("data-prefill"), btn.getAttribute("data-region"));
  });
}

// ---------------------------------------------------------------- Wiring

function init() {
  // Sync persisted sidebar state from <html> marker class to .layout
  applySidebarStateFromStorage();

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
  document.getElementById("filter-v6-only").addEventListener("change", applyFilters);
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
  const ownerWipeBtn = document.getElementById("owner-wipe");
  if (ownerWipeBtn) ownerWipeBtn.addEventListener("click", _supportWipe);
  document.querySelectorAll("[data-settings-tab]").forEach(btn => {
    btn.addEventListener("click", () => switchSettingsTab(btn.getAttribute("data-settings-tab")));
  });
  document.getElementById("btn-export-csv").addEventListener("click", exportCsv);
  document.getElementById("btn-export-xlsx").addEventListener("click", exportXlsx);
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
  document.getElementById("bom-import-go").addEventListener("click", importBomFromXlsx);
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
    await loadSubscriptions();
    await loadSnapshotsList();
    const picker = document.getElementById("snapshot-picker");
    await loadSnapshot(picker.value || null);
    maybeShowSettingsCoach();
    // Restore quota request history from SQLite
    await _restoreQuotaRequestsFromDb();
    // Populate the header sign-in chip (silent — never opens a browser).
    refreshAuthToken({ force: false }).then(() => {
      // Pre-load subscription names so quota displays show names not IDs.
      preloadSubscriptionNames().catch(() => {});
    }).catch(() => {});
  })().catch(e => console.error("init failed:", e));
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
