"use client";

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { PullToRefresh } from "@/components/pull-to-refresh";
import { apiPost } from "@/lib/api";
import type { SyncResult } from "@/lib/types";

// Setup-page refresh gesture: pull down → run the (7-day) Garmin sync,
// then drop every query cache so the whole app rereads fresh data.
// Errors are swallowed here — the SyncSection card polls
// /api/sync/garmin/status and is the surface that reports outcomes,
// including the token-expired flow; a toast from the gesture would
// duplicate it.
export function SetupRefresh({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const refresh = useCallback(async () => {
    try {
      await apiPost<SyncResult>("/api/sync/garmin");
    } catch {
      // status card reports it
    }
    await qc.invalidateQueries();
  }, [qc]);
  return <PullToRefresh onRefresh={refresh}>{children}</PullToRefresh>;
}
