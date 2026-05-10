"use client";

import { useCallback, useSyncExternalStore } from "react";

// localStorage key for the active coach session id. Persisted so a
// page refresh / app close-and-reopen continues the in-flight session
// per design doc Q1=A.
const STORAGE_KEY = "personal-coach.current_session_id";

/** Minted client-side so the user gets a stable id before the first
 * round-trip. Format mirrors the server (`/api/ai/sessions` POST):
 * `coach_<utc-yyyymmddThhmmssZ>` — sortable lex ≈ chronological. */
function mintSessionId(): string {
  const d = new Date();
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mi = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return `coach_${yyyy}${mm}${dd}T${hh}${mi}${ss}Z`;
}

// --- localStorage <-> useSyncExternalStore plumbing -----------------
//
// We use useSyncExternalStore (the React-recommended pattern for
// browser-API-backed state) so server-render sees null and the client
// hydrates without a setState-in-effect cascade. Multiple components
// reading currentId stay consistent because they all subscribe to the
// same module-level listener set + storage events.

type Listener = () => void;
const listeners = new Set<Listener>();

function notify() {
  for (const l of listeners) l();
}

function subscribe(cb: Listener): () => void {
  listeners.add(cb);
  // Cross-tab sync: another tab clearing/setting the same key fires a
  // 'storage' event here.
  const onStorage = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) cb();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    window.removeEventListener("storage", onStorage);
  };
}

function getSnapshot(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

// SSR / first-paint snapshot. We don't have access to localStorage on
// the server, so it's null until hydration.
function getServerSnapshot(): string | null {
  return null;
}

/**
 * Hook: own the "current active coach session" id.
 *
 * - `currentId` is the active thread; null = no session yet (fresh
 *   open of Coach tab with no prior unclosed session).
 * - `ensureCurrent()` returns the active id, minting a fresh one and
 *   persisting if there was none. Use right before any chat/action POST.
 * - `clearCurrent()` is what [End & Save] calls after the archive
 *   succeeds — drops localStorage so the next message creates a new
 *   session.
 * - `hydrated` flips true after the first client render — gates
 *   queries that depend on knowing whether a current session exists.
 */
export function useCoachSession(): {
  currentId: string | null;
  ensureCurrent: () => string;
  clearCurrent: () => void;
  hydrated: boolean;
} {
  const currentId = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getServerSnapshot,
  );
  // After hydration, getSnapshot starts returning the localStorage
  // value (or null if nothing's stored). The "hydrated" signal lets
  // callers distinguish the SSR null from a real client null.
  const hydrated = useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );

  const ensureCurrent = useCallback((): string => {
    const existing = getSnapshot();
    if (existing) return existing;
    const fresh = mintSessionId();
    try {
      window.localStorage.setItem(STORAGE_KEY, fresh);
    } catch {
      // ignore — Safari private mode etc.
    }
    notify();
    return fresh;
  }, []);

  const clearCurrent = useCallback(() => {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
    notify();
  }, []);

  return { currentId, ensureCurrent, clearCurrent, hydrated };
}
