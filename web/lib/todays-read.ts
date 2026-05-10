/**
 * Local-day cache for the Health tab's "Today's read" card.
 *
 * Behavior:
 *   - User taps the card → fire `review_health` via the coach session
 *     and navigate to /coach. Backend takes ~5-10s; we don't block.
 *   - The fire-and-forget promise resolves (often after the user has
 *     already left this page) and writes the first sentence of the
 *     AI answer here.
 *   - When the user comes back to /health later that day, the card
 *     reads from here and shows the cached AI sentence as the
 *     headline instead of the rule-based default.
 *   - At local midnight, "today's date string" changes, so yesterday's
 *     cache no longer matches and the card falls back to the rule-
 *     based default until the user runs review_health again.
 *
 * Why localStorage and not server state:
 *   - "What was today's read" is a per-device UX detail, not a
 *     long-term truth. The actual coach answer lives in the session
 *     history on the server; this cache is just a thumbnail.
 *   - Survives page navigation (which is the whole point: the
 *     `apiPost` was kicked off from /health but resolves while the
 *     user is on /coach).
 */

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "personal-coach.todays-read";

export type TodaysReadStatus = "pending" | "ready";

export interface TodaysReadEntry {
  date: string;          // YYYY-MM-DD in the user's local timezone
  status: TodaysReadStatus;
  text?: string;         // first sentence of the review_health answer
  thread_id?: string;    // which coach session produced it (for debugging)
}

function todayLocal(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

/** Read today's cached entry. Returns null if missing or stale (yesterday). */
export function readTodaysRead(): TodaysReadEntry | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const entry = JSON.parse(raw) as TodaysReadEntry;
    if (entry.date !== todayLocal()) return null;
    return entry;
  } catch {
    return null;
  }
}

export function writeTodaysRead(entry: Omit<TodaysReadEntry, "date">): void {
  try {
    const full: TodaysReadEntry = { ...entry, date: todayLocal() };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(full));
  } catch {
    // Safari private mode etc. — degrade silently.
  }
}

export function clearTodaysRead(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
  notify();
}

// --- React subscription plumbing -----------------------------------
//
// useSyncExternalStore lets the card render the cached headline on
// mount without setState-in-effect. After we write the cache from the
// fire-and-forget review_health resolve, we call notify() so any
// mounted ReadinessCard re-renders.

type Listener = () => void;
const listeners = new Set<Listener>();

function notify(): void {
  for (const l of listeners) l();
}

function subscribe(cb: Listener): () => void {
  listeners.add(cb);
  // Cross-tab: another tab writing the same key wakes us via 'storage'.
  const onStorage = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) cb();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    window.removeEventListener("storage", onStorage);
  };
}

// Instance-level snapshot cache so getSnapshot returns referentially
// stable values when the underlying entry hasn't changed (otherwise
// useSyncExternalStore loops). We compare on the JSON string.
let _lastJson: string | null = null;
let _lastEntry: TodaysReadEntry | null = null;

function getSnapshot(): TodaysReadEntry | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    raw = null;
  }
  if (raw === _lastJson) return _lastEntry;
  _lastJson = raw;
  if (!raw) {
    _lastEntry = null;
    return null;
  }
  try {
    const entry = JSON.parse(raw) as TodaysReadEntry;
    if (entry.date !== todayLocal()) {
      _lastEntry = null;
      return null;
    }
    _lastEntry = entry;
    return entry;
  } catch {
    _lastEntry = null;
    return null;
  }
}

function getServerSnapshot(): TodaysReadEntry | null {
  return null;
}

/** Wrap writeTodaysRead to also notify React subscribers. */
export function setTodaysRead(entry: Omit<TodaysReadEntry, "date">): void {
  writeTodaysRead(entry);
  notify();
}

/** React hook: subscribe to today's-read cache. SSR returns null; on
 * client returns the entry for today (or null if missing/stale). */
export function useTodaysRead(): TodaysReadEntry | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/**
 * Best-effort first-sentence extraction for the AI's review_health
 * answer. The agent emits markdown with headings, bullets, occasional
 * pending-clarification prefaces, etc. We strip the obvious markdown
 * scaffolding then take the first sentence-ish chunk.
 *
 * Tested against the actual review_health style: "### 1. 恢复状态概览：
 * 绿灯 (Green)\n* 评分驱动因素…" — we want "恢复状态概览：绿灯".
 */
export function extractFirstSentence(answer: string): string {
  if (!answer) return "";
  // Strip an attribution footer like "[Generated by gemini-3.1-flash-lite]"
  const stripped = answer.replace(/\[Generated by [^\]]+\]\s*$/i, "").trim();

  // Walk lines, skipping pure markdown scaffolding, find the first
  // line with real content.
  const lines = stripped.split(/\n+/);
  for (const raw of lines) {
    const line = raw
      // strip leading heading markers (### / ##)
      .replace(/^#{1,6}\s+/, "")
      // strip leading bullet/number markers
      .replace(/^[-*+]\s+/, "")
      .replace(/^\d+\.\s+/, "")
      // strip wrapping bold/italic
      .replace(/\*\*/g, "")
      .replace(/\*/g, "")
      // strip leading/trailing pipe-table cell markers
      .replace(/^\|+|\|+$/g, "")
      .trim();
    if (!line) continue;
    if (line.length < 4) continue; // skip "1." "—" etc.

    // Take up to the first sentence terminator. CJK + Latin both.
    const m = line.match(/^[^。！？.!?\n]{4,200}[。！？.!?]?/);
    const sentence = (m ? m[0] : line.slice(0, 120)).trim();
    return sentence.endsWith("。") || sentence.endsWith(".") ||
      sentence.endsWith("!") || sentence.endsWith("?") ||
      sentence.endsWith("！") || sentence.endsWith("？")
      ? sentence
      : sentence + "…";
  }
  return stripped.slice(0, 120);
}
