// Shared mutation key for "run a Garmin sync". Both triggers on the
// Setup page (the SyncSection button and the pull-to-refresh gesture)
// register their mutations under this key, so either side can see —
// via useIsMutating / queryClient.isMutating — that a sync is already
// in flight and refrain from starting a second one. The backend holds
// a lock as the backstop for triggers this key can't see (a second
// device, cron overlap).
export const GARMIN_SYNC_MUTATION_KEY = ["sync", "garmin", "run"] as const;
