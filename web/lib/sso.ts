// The Garmin mobile SSO URL the README documents — login on this URL produces
// a one-shot ST-... ticket in the redirect URL after a "site can't be reached"
// page. Centralized here so both the Setup page and any deep-link Shortcut use
// the same source.
export const GARMIN_SSO_URL =
  "https://sso.garmin.com/mobile/sso/en_US/sign-in" +
  "?clientId=GCM_ANDROID_DARK" +
  "&service=https://mobile.integration.garmin.com/gcm/android";

/**
 * Rewrite an http(s) URL into Chrome's iOS deep-link scheme so the link
 * opens in Google Chrome.
 *
 * Why this is needed: the app is a `display: "standalone"` PWA (see
 * `app/manifest.ts`). On iOS, an external link tapped from a standalone
 * PWA opens in an in-app Safari view (SFSafariViewController) — iOS
 * ignores the user's default-browser setting in this case, so even with
 * Chrome set as default the Garmin sign-in popped into Safari. The only
 * way to force a *specific* browser is that browser's URL scheme. Chrome
 * registers `googlechrome://` (for http) and `googlechromes://` (for
 * https); swapping just the scheme — host, path and query untouched —
 * launches the real Chrome app, where the address bar is reachable (the
 * Garmin flow needs the user to copy the post-login redirect URL from
 * it; an in-app Safari sheet makes that awkward).
 *
 * Caveat: if Chrome isn't installed the link silently does nothing.
 * Acceptable here — single-user app, Chrome is the user's default
 * browser. Non-http(s) input is returned unchanged.
 */
export function chromeDeepLink(url: string): string {
  if (url.startsWith("https://")) {
    return "googlechromes://" + url.slice("https://".length);
  }
  if (url.startsWith("http://")) {
    return "googlechrome://" + url.slice("http://".length);
  }
  return url;
}
