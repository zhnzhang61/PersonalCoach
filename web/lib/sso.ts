// The Garmin mobile SSO URL the README documents — login on this URL produces
// a one-shot ST-... ticket in the redirect URL after a "site can't be reached"
// page. Centralized here so both the Setup page and any deep-link Shortcut use
// the same source.
export const GARMIN_SSO_URL =
  "https://sso.garmin.com/mobile/sso/en_US/sign-in" +
  "?clientId=GCM_ANDROID_DARK" +
  "&service=https://mobile.integration.garmin.com/gcm/android";
