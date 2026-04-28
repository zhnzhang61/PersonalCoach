"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow, parseISO } from "date-fns";
import { ExternalLink, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet, apiPost } from "@/lib/api";
import { GARMIN_SSO_URL } from "@/lib/sso";
import type {
  RefreshTokenResult,
  SyncResult,
  SyncStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export function SyncSection() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["sync", "garmin", "status"],
    queryFn: () => apiGet<SyncStatus>("/api/sync/garmin/status"),
    refetchInterval: 30_000,
  });

  const sync = useMutation({
    mutationFn: () => apiPost<SyncResult>("/api/sync/garmin"),
    onSettled: () => qc.invalidateQueries({ queryKey: ["sync", "garmin", "status"] }),
  });

  const lastSyncRel = status.data?.last_sync
    ? formatDistanceToNow(parseISO(status.data.last_sync), { addSuffix: true })
    : null;

  const showRefreshFlow =
    status.data?.outcome === "token_expired" ||
    sync.data?.reason === "token_expired";

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-4 p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="eyebrow">Garmin sync</div>
              <div className="font-heading mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
                {status.isLoading ? (
                  <Skeleton className="h-7 w-40" />
                ) : lastSyncRel ? (
                  `Updated ${lastSyncRel}`
                ) : (
                  "No data yet"
                )}
              </div>
              {status.data?.outcome === "ok" && (
                <p className="mt-1 text-sm text-emerald-700 dark:text-emerald-400">
                  Last attempt succeeded.
                </p>
              )}
              {status.data?.outcome === "error" && (
                <p className="mt-1 text-sm text-rose-700 dark:text-rose-400">
                  Last attempt failed (not a token issue).
                </p>
              )}
            </div>
            <Button
              type="button"
              size="lg"
              onClick={() => sync.mutate()}
              disabled={sync.isPending}
              className="shrink-0"
            >
              <RefreshCw
                className={cn("size-4", sync.isPending && "animate-spin")}
                aria-hidden
              />
              {sync.isPending ? "Syncing…" : "Sync now"}
            </Button>
          </div>

          {sync.data?.ok && (
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              Sync complete.
            </p>
          )}
          {sync.data?.reason === "error" && (
            <p className="text-sm text-rose-700 dark:text-rose-400">
              Sync failed: {sync.data.stderr?.trim() || "see server logs"}
            </p>
          )}
          {sync.error && (
            <p className="text-sm text-rose-700 dark:text-rose-400">
              Could not reach API: {(sync.error as Error).message}
            </p>
          )}
        </CardContent>
      </Card>

      {showRefreshFlow && <RefreshTokenCard />}
    </div>
  );
}

function RefreshTokenCard() {
  const qc = useQueryClient();
  const [ticket, setTicket] = useState("");

  const refresh = useMutation({
    mutationFn: (t: string) =>
      apiPost<RefreshTokenResult>("/api/sync/garmin/refresh-token", {
        ticket: t,
      }),
    onSuccess: (data) => {
      if (data.ok) {
        setTicket("");
        qc.invalidateQueries({ queryKey: ["sync", "garmin", "status"] });
      }
    },
  });

  return (
    <Card className="border-warm-accent/40 bg-warm-bg/40">
      <CardContent className="space-y-4 p-5 sm:p-6">
        <div>
          <div className="eyebrow">Token expired</div>
          <h3 className="font-heading mt-1 text-xl font-semibold tracking-tight sm:text-2xl">
            Refresh Garmin login
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            Garmin tokens expire every day or two. Tap below, sign in to
            Garmin, then copy the ticket from the redirect URL and paste it
            back here.
          </p>
        </div>

        <ol className="space-y-3 text-sm">
          <li className="flex items-start gap-3">
            <span className="font-heading mt-0.5 text-base font-semibold text-foreground">
              1.
            </span>
            <div className="flex-1 space-y-2">
              <p>Open the Garmin sign-in page in Safari.</p>
              <Button asChild variant="secondary" className="w-full sm:w-auto">
                <a
                  href={GARMIN_SSO_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink className="size-4" aria-hidden />
                  Open Garmin sign-in
                </a>
              </Button>
            </div>
          </li>
          <li className="flex items-start gap-3">
            <span className="font-heading mt-0.5 text-base font-semibold text-foreground">
              2.
            </span>
            <p className="flex-1">
              Sign in. The page will redirect to a &ldquo;Site can&rsquo;t be
              reached&rdquo; error — that&rsquo;s expected. Copy the entire
              URL from the address bar (or just the{" "}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">
                ST-...-sso
              </code>{" "}
              portion).
            </p>
          </li>
          <li className="flex items-start gap-3">
            <span className="font-heading mt-0.5 text-base font-semibold text-foreground">
              3.
            </span>
            <div className="flex-1 space-y-2">
              <p>
                Paste it below within ~1 minute (the ticket expires fast).
              </p>
              <Input
                value={ticket}
                onChange={(e) => setTicket(e.target.value)}
                placeholder="https://...?ticket=ST-... or ST-...-sso"
                className="font-mono text-xs"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
              />
              <Button
                type="button"
                onClick={() => refresh.mutate(ticket.trim())}
                disabled={!ticket.trim() || refresh.isPending}
              >
                {refresh.isPending ? "Exchanging…" : "Submit ticket"}
              </Button>
            </div>
          </li>
        </ol>

        {refresh.data?.ok && (
          <p className="text-sm text-emerald-700 dark:text-emerald-400">
            ✓ Token refreshed. You can hit Sync now to pull fresh data.
          </p>
        )}
        {refresh.data && !refresh.data.ok && (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Exchange failed: {refresh.data.stderr?.trim() || "unknown error"}
          </p>
        )}
        {refresh.error && (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Could not reach API: {(refresh.error as Error).message}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
