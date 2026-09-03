/*
 * Delegated (multi-customer) browser auth for the hosted dashboard.
 *
 * In DELEGATED_MODE the server is stateless and reads Azure strictly as the
 * signed-in customer. This module mints the customer's ARM access token in the
 * browser (via MSAL.js), so tokens live only client-side; app.js forwards the
 * token per request in the X-Bom-Access-Token header. The user is already
 * signed in at the Easy Auth layer, so token acquisition is normally silent
 * (SSO), falling back to a popup only if silent SSO can't complete.
 *
 * Exposes window.DelegatedAuth = { init(config), getArmToken({interactive}), account }.
 * No-op (returns null) unless MSAL is present and a client id is configured.
 */
(function () {
  "use strict";

  var pca = null;
  var cfg = null;
  var account = null;

  function armScopes() {
    return [(cfg && cfg.arm_scope) || "https://management.azure.com/user_impersonation"];
  }

  async function init(config) {
    cfg = config || {};
    if (typeof msal === "undefined" || !cfg.entra_client_id) {
      return false;
    }
    pca = new msal.PublicClientApplication({
      auth: {
        clientId: cfg.entra_client_id,
        authority: cfg.entra_authority || "https://login.microsoftonline.com/organizations",
        // Multi-tenant: accept tokens from any org tenant's authority.
        knownAuthorities: [],
        redirectUri: window.location.origin + "/",
        navigateToLoginRequestUrl: false,
      },
      cache: {
        // Session-scoped so a closed tab drops the cached token; the durable
        // customer BOM lives elsewhere (browser localStorage), never here.
        cacheLocation: "sessionStorage",
        storeAuthStateInCookie: false,
      },
    });
    await pca.initialize();
    try {
      var resp = await pca.handleRedirectPromise();
      if (resp && resp.account) account = resp.account;
    } catch (e) { /* ignore */ }
    if (!account) {
      var accts = pca.getAllAccounts();
      if (accts && accts.length) {
        // Prefer the account matching the Easy Auth-signed-in user.
        if (cfg.login_hint) {
          account = accts.filter(function (a) {
            return (a.username || "").toLowerCase() === String(cfg.login_hint).toLowerCase();
          })[0] || accts[0];
        } else {
          account = accts[0];
        }
      }
    }
    return true;
  }

  async function getArmToken(opts) {
    opts = opts || {};
    if (!pca) return null;
    var scopes = armScopes();

    // 1) Silent with a known account (refresh from cache).
    if (account) {
      try {
        var r1 = await pca.acquireTokenSilent({ scopes: scopes, account: account });
        account = r1.account || account;
        return r1.accessToken;
      } catch (e) { /* fall through */ }
    }

    // 2) Silent SSO using the Easy Auth session (hidden iframe, no prompt).
    if (cfg.login_hint) {
      try {
        var r2 = await pca.ssoSilent({ scopes: scopes, loginHint: cfg.login_hint });
        account = r2.account || account;
        return r2.accessToken;
      } catch (e) { /* fall through to interactive */ }
    }

    // 3) Interactive (only on an explicit Sign in click).
    if (opts.interactive) {
      var req = { scopes: scopes };
      if (cfg.login_hint) req.loginHint = cfg.login_hint;
      var r3 = await pca.acquireTokenPopup(req);
      account = r3.account || account;
      return r3.accessToken;
    }
    return null;
  }

  window.DelegatedAuth = {
    init: init,
    getArmToken: getArmToken,
    logout: logout,
    get account() { return account; },
  };

  // Clear the browser-held MSAL account + cached tokens so the next sign-in is
  // a fresh interactive prompt (and the user can pick a different account).
  // Cache is sessionStorage-scoped; we clear MSAL's cache and, belt-and-braces,
  // remove any lingering msal.* keys.
  async function logout() {
    account = null;
    try { if (pca && pca.clearCache) { await pca.clearCache(); } } catch (e) { /* ignore */ }
    try {
      var kill = [];
      for (var i = 0; i < sessionStorage.length; i++) {
        var k = sessionStorage.key(i);
        if (k && k.toLowerCase().indexOf("msal") !== -1) kill.push(k);
      }
      kill.forEach(function (k) { try { sessionStorage.removeItem(k); } catch (e) {} });
    } catch (e) { /* ignore */ }
    return true;
  }
})();
