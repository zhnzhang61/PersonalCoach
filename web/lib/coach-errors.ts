/**
 * Recognize Gemini / Groq / OpenAI-shape rate-limit errors and turn
 * them into a friendly user-facing message. Both cases need handling:
 *
 *   1. apiPost throws — non-2xx HTTP, e.g. dev proxy timeout from a
 *      retry-loop on 429. e.message looks like "500 Internal Server
 *      Error on /api/ai/chat".
 *   2. apiPost resolves but the action endpoint shaped a 200 with
 *      `error` set (api_server's ai_action catches and returns). The
 *      error string contains the underlying provider message — for
 *      Gemini that's "Error code: 429 - { ... RESOURCE_EXHAUSTED ... }".
 *
 * We squash both into a small set of recognized categories so the UI
 * can render a sensible message instead of a stack trace, and let the
 * caller decide whether to auto-retry.
 */

export type CoachErrorKind = "rate_limit" | "network" | "unknown";

export interface CoachErrorInfo {
  kind: CoachErrorKind;
  /** Friendly Chinese message safe to show to the user. */
  message: string;
  /**
   * Hint for callers: how many seconds to wait before a single auto-
   * retry. Null = don't retry automatically (let the user decide).
   */
  retryAfterSec: number | null;
}

const RATE_LIMIT_RE =
  /\b429\b|RESOURCE_EXHAUSTED|rate[\s_-]?limit|quota|too many requests|tokens? per minute|TPM|RPM/i;

/**
 * Dev-proxy timeouts surface as "500 Internal Server Error on /api/...".
 * They're often the *result* of a backend rate-limit retry loop blowing
 * past the proxy's 30s timeout, so we treat them as rate-limit-like.
 */
const PROXY_TIMEOUT_RE =
  /\b50[02-4]\b.*on\s+\/api\/ai\/(chat|action)|gateway timeout|timed out/i;

export function classifyCoachError(raw: string | null | undefined): CoachErrorInfo {
  const s = (raw ?? "").toString();
  if (!s) {
    return { kind: "unknown", message: "教练那边出了点问题，再试一次？", retryAfterSec: null };
  }

  if (RATE_LIMIT_RE.test(s)) {
    return {
      kind: "rate_limit",
      message: "教练正在喘口气，10 秒后再试一次…",
      retryAfterSec: 10,
    };
  }

  if (PROXY_TIMEOUT_RE.test(s)) {
    return {
      kind: "rate_limit",
      message: "教练这一轮想得有点久，10 秒后再试一次…",
      retryAfterSec: 10,
    };
  }

  // Network-ish errors (offline, fetch failed). No auto-retry — the
  // network has to come back first.
  if (/network|failed to fetch|load failed/i.test(s)) {
    return {
      kind: "network",
      message: "网络好像断了，确认一下连接再试。",
      retryAfterSec: null,
    };
  }

  return { kind: "unknown", message: s.slice(0, 200), retryAfterSec: null };
}
