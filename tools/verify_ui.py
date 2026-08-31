"""Headless UI smoke test for the onboarding stepper + BOM wizard.

Run against a fresh (empty, non-demo) dashboard instance. Verifies:
  1. The onboarding stepper renders with Sign-in + Create-BOM steps and a
     "Explore with sample data" button.
  2. Clicking "Explore with sample data" seeds and loads a BOM (panel body
     becomes visible).
  3. Opening the BOM editor shows the 3-step wizard; Next/Back navigate; step 1
     validation blocks advancing when required fields are empty (fresh create).
"""
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:4391"
errors = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        errors.append(name)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    console_errors = []
    failed_urls = []
    pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: console_errors.append(str(e)))
    pg.on("response", lambda r: failed_urls.append((r.status, r.url)) if r.status >= 400 else None)
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(1500)

    # 1) Onboarding stepper present
    onboard = pg.query_selector(".onboard")
    check("onboarding stepper renders", onboard is not None)
    check("has Sign in step", pg.query_selector('[data-onboard="signin"]') is not None)
    check("has New BOM step", pg.query_selector('[data-onboard="new"]') is not None)
    demo_btn = pg.query_selector('[data-onboard="demo"]')
    check("has Explore-with-sample-data button", demo_btn is not None)

    # 2) Click "Explore with sample data" -> BOM loads
    if demo_btn:
        demo_btn.click()
        pg.wait_for_timeout(2500)
        body_hidden = pg.eval_on_selector("#bom-panel-body", "el => el.classList.contains('hidden')")
        check("BOM panel body visible after seeding sample", body_hidden is False)

    # 3) Open BOM editor -> wizard
    pg.click("#bomnav-new")
    pg.wait_for_timeout(600)
    check("wizard nav visible", pg.eval_on_selector("#bom-wizard-nav", "el => !!el && el.offsetParent !== null"))
    check("step 1 is current", pg.eval_on_selector('.bom-wizard-step[data-wstep="1"]', "el => el.classList.contains('is-current')"))
    check("Back hidden on step 1", pg.eval_on_selector("#bom-wizard-back", "el => el.hidden === true"))
    check("Save hidden on step 1 (create)", pg.eval_on_selector("#bom-save", "el => el.hidden === true"))

    # Try to advance with empty name -> should be blocked (still step 1)
    pg.click("#bom-wizard-next")
    pg.wait_for_timeout(300)
    check("validation blocks advance with empty name", pg.eval_on_selector('.bom-wizard-step[data-wstep="1"]', "el => el.classList.contains('is-current')"))

    # Fill name + select a subscription, then advance. Without sign-in there are
    # no real subscriptions, so inject an enabled option to exercise navigation.
    pg.fill("#bom-tag", "Verify-BOM")
    pg.eval_on_selector("#bom-sub", """el => {
        const o = document.createElement('option');
        o.value = 'test-sub-0000'; o.textContent = 'Test Subscription'; o.selected = true;
        el.appendChild(o);
    }""")
    if True:
        pg.click("#bom-wizard-next")
        pg.wait_for_timeout(300)
        check("advanced to step 2 after valid basics", pg.eval_on_selector('.bom-wizard-step[data-wstep="2"]', "el => el.classList.contains('is-current')"))
        check("Back visible on step 2", pg.eval_on_selector("#bom-wizard-back", "el => el.hidden === false"))
        pg.click("#bom-wizard-next")
        pg.wait_for_timeout(300)
        check("advanced to step 3", pg.eval_on_selector('.bom-wizard-step[data-wstep="3"]', "el => el.classList.contains('is-current')"))
        check("Save visible on step 3", pg.eval_on_selector("#bom-save", "el => el.hidden === false"))
        check("Next visible but disabled on step 3 (last)",
              pg.eval_on_selector("#bom-wizard-next", "el => el.hidden === false && el.disabled === true"))
        pg.click("#bom-wizard-back")
        pg.wait_for_timeout(300)
        check("Back navigates to step 2", pg.eval_on_selector('.bom-wizard-step[data-wstep="2"]', "el => el.classList.contains('is-current')"))

    # Benign, app-handled responses in a signed-out test env:
    #  - /api/snapshots/latest 404 = "no snapshot yet"
    #  - /api/auth/signin 401/502 = "not signed in" (boot silent probe)
    #  - /api/az/subscriptions 401 = "not signed in" (ARM listing needs auth)
    benign = ("snapshots/latest", "auth/signin", "az/subscriptions")

    # ----- Phase 3: consolidated navigation -----
    # Close the BOM modal first.
    pg.click("#bom-cancel")
    pg.wait_for_timeout(200)
    primary_tabs = pg.eval_on_selector_all(".tabs .tab", "els => els.map(e => e.getAttribute('data-view'))")
    check("primary tabs are Overview/Regions/Quota/Tickets",
          primary_tabs == ["overview", "regions", "quota", "support"])
    check("no Settings tab in primary bar", "settings" not in primary_tabs)
    check("gear settings button present", pg.query_selector("#open-settings") is not None)

    # Regions tab shows the sub-tab bar and the table sub-view by default.
    pg.click('.tab[data-view="regions"]')
    pg.wait_for_timeout(300)
    check("region sub-tab bar visible on Regions", pg.eval_on_selector("#region-subtabs", "el => !el.classList.contains('hidden')"))
    check("table sub-view visible by default", pg.eval_on_selector("#view-table", "el => !el.classList.contains('hidden')"))
    check("filters rail visible on Regions", pg.eval_on_selector("#filters-rail", "el => !el.classList.contains('hidden')"))
    # Switch to Map sub-view.
    pg.click('.region-subtab[data-sub="map"]')
    pg.wait_for_timeout(300)
    check("map sub-view visible after sub-tab click", pg.eval_on_selector("#view-map", "el => !el.classList.contains('hidden')"))
    check("table sub-view hidden after switching to map", pg.eval_on_selector("#view-table", "el => el.classList.contains('hidden')"))

    # Quota hides the sub-tab bar and the filters rail.
    pg.click('.tab[data-view="quota"]')
    pg.wait_for_timeout(300)
    check("sub-tab bar hidden on Quota", pg.eval_on_selector("#region-subtabs", "el => el.classList.contains('hidden')"))
    check("filters rail hidden on Quota", pg.eval_on_selector("#filters-rail", "el => el.classList.contains('hidden')"))

    # Gear opens Settings (activity log) view.
    pg.click("#open-settings")
    pg.wait_for_timeout(300)
    check("settings view visible via gear", pg.eval_on_selector("#view-settings", "el => !el.classList.contains('hidden')"))

    # ----- Phase 4: contextual ticket escalation -----
    esc_short = pg.evaluate("""() => _quotaTicketEscalationHtml(
        { region_short: 'eastus', family: 'standardDSv3Family', overall_status: 'insufficient',
          required: 100, deficit: 40, subscription_id: 'sub-1', subscription: { limit: 60 } })""")
    check("escalation button shown for insufficient row",
          "Open support ticket" in esc_short and 'data-open-ticket="quota"' in esc_short)
    esc_ok = pg.evaluate("""() => _quotaTicketEscalationHtml(
        { region_short: 'eastus', family: 'standardDSv3Family', overall_status: 'sufficient',
          subscription_id: 'sub-1' })""")
    check("no escalation button for sufficient row", esc_ok == "")
    # Enhanced prefill routes to the support view and sets kind.
    pg.evaluate("""() => _supportPrefill('quota', document.querySelector('#sup-region option')?.value || '',
        { family: '', newLimit: 128 })""")
    pg.wait_for_timeout(300)
    check("prefill routes to support view", pg.eval_on_selector("#view-support", "el => !el.classList.contains('hidden')"))
    check("prefill sets ticket kind to quota", pg.eval_on_selector("#sup-kind", "el => el.value === 'quota'"))

    # ----- Ticket owner: capture at BOM setup + edit in Settings -----
    pg.click("#open-settings")
    pg.wait_for_timeout(300)
    check("gear Settings exposes ticket-owner fields",
          pg.query_selector("#owner-first") is not None and pg.query_selector("#owner-email") is not None)
    pg.fill("#owner-first", "Ada")
    pg.fill("#owner-last", "Lovelace")
    pg.fill("#owner-email", "ada@contoso.com")
    pg.click("#owner-save")
    pg.wait_for_timeout(700)
    saved_email = pg.evaluate("() => (typeof SUPPORT !== 'undefined' && SUPPORT.settings) ? SUPPORT.settings.primary_email : null")
    check("owner saved to support settings", saved_email == "ada@contoso.com")
    # BOM wizard prefills the saved owner.
    pg.click("#bomnav-new")
    pg.wait_for_timeout(1500)
    check("BOM wizard Basics has ticket-owner fields", pg.query_selector("#bom-owner-email") is not None)
    check("BOM wizard prefills saved owner email",
          pg.eval_on_selector("#bom-owner-email", "el => el.value") == "ada@contoso.com")
    pg.click("#bom-cancel")
    pg.wait_for_timeout(200)

    real_console = [e for e in console_errors if "404" not in e and "Failed to load resource" not in e]
    real_failed = [u for u in failed_urls if not any(b in u[1] for b in benign)]
    check("no unexpected console/page errors", len(real_console) == 0 and len(real_failed) == 0)
    if real_console:
        print("CONSOLE ERRORS:", real_console[:5])
    if real_failed:
        print("HTTP >=400:", real_failed[:10])

    b.close()

print("\nRESULT:", "ALL PASS" if not errors else f"{len(errors)} FAILED: {errors}")
sys.exit(1 if errors else 0)
