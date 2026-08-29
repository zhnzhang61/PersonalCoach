"use client";

import { useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PullToRefresh } from "@/components/pull-to-refresh";
import { apiPost } from "@/lib/api";
import { GARMIN_SYNC_MUTATION_KEY } from "@/lib/sync";
import type { SyncResult } from "@/lib/types";

// Setup-page refresh gesture: pull down → run the (7-day) Garmin sync,
// then drop every query cache so the whole app rereads fresh data.
// Errors are swallowed here — the SyncSection card polls
// /api/sync/garmin/status and is the surface that reports outcomes,
// including the token-expired flow; a toast from the gesture would
// duplicate it.
export function SetupRefresh({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const sync = useMutation({
    mutationKey: GARMIN_SYNC_MUTATION_KEY,
    mutationFn: () => apiPost<SyncResult>("/api/sync/garmin"),
  });
  const { mutateAsync } = sync;
  const refresh = useCallback(async () => {
    // If a sync is already in flight (the button, or a previous pull),
    // don't stack another subprocess — just refetch what's on screen.
    if (qc.isMutating({ mutationKey: [...GARMIN_SYNC_MUTATION_KEY] }) === 0) {
      try {
        await mutateAsync();
      } catch {
        // status card reports it
      }
    }
    await qc.invalidateQueries();
  }, [qc, mutateAsync]);
  return <PullToRefresh onRefresh={refresh}>{children}</PullToRefresh>;
}
