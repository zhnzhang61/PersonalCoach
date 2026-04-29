"use client";

import { useSyncExternalStore } from "react";

const BLOCK_KEY = "pc.activeBlockId";
const WEEK_KEY = "pc.activeWeekLabel";
const SELECTION_EVENT = "pc:training-selection-changed";

export interface TrainingSelection {
  blockId: string | null;
  weekLabel: string | null;
  hydrated: boolean;
  setBlockId: (id: string) => void;
  setWeekLabel: (label: string) => void;
}

function subscribe(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  window.addEventListener(SELECTION_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(SELECTION_EVENT, callback);
  };
}

function getBlockId(): string | null {
  return localStorage.getItem(BLOCK_KEY);
}
function getWeekLabel(): string | null {
  return localStorage.getItem(WEEK_KEY);
}
const getServerSnapshot = (): string | null => null;

// localStorage-backed selection shared between Activity and Training tabs.
// useSyncExternalStore handles SSR (server snapshot = null) and avoids the
// "setState in effect" pattern. Cross-mount sync within a tab uses a custom
// event; cross-tab sync uses the native `storage` event.
export function useTrainingSelection(): TrainingSelection {
  const blockId = useSyncExternalStore(subscribe, getBlockId, getServerSnapshot);
  const weekLabel = useSyncExternalStore(
    subscribe,
    getWeekLabel,
    getServerSnapshot,
  );
  const hydrated = useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );

  const setBlockId = (id: string) => {
    if (id === blockId) return;
    localStorage.setItem(BLOCK_KEY, id);
    // Changing block invalidates the cached week — caller picks a new default.
    localStorage.removeItem(WEEK_KEY);
    window.dispatchEvent(new Event(SELECTION_EVENT));
  };

  const setWeekLabel = (label: string) => {
    if (label === weekLabel) return;
    localStorage.setItem(WEEK_KEY, label);
    window.dispatchEvent(new Event(SELECTION_EVENT));
  };

  return { blockId, weekLabel, hydrated, setBlockId, setWeekLabel };
}
