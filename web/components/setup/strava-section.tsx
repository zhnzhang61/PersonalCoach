"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { StravaStatus } from "@/lib/types";

// Strava connection. Once connected, saving a run's name also renames
// its Strava activity, and run pages get an "open in Strava" link.
//
// Authorizing has to happen on the Mac: Strava sends the browser back to
// http://localhost:8765, which only exists on the machine running the API.
export function StravaSection() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["strava", "status"],
    queryFn: () => apiGet<StravaStatus>("/api/strava/status"),
  });
  const disconnect = useMutation({
    mutationFn: () => apiPost<{ ok: boolean }>("/api/strava/disconnect"),
    onSettled: () => qc.invalidateQueries({ queryKey: ["strava", "status"] }),
  });

  const s = status.data;
  const headline = status.isError
    ? "Status unavailable"
    : !s
      ? null
      : !s.configured
        ? "Not set up"
        : !s.connected
          ? "Not connected"
          : s.can_write
            ? "Connected"
            : "Connected (read-only)";

  return (
    <Card>
      <CardContent className="space-y-3 p-5 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="eyebrow">Strava</div>
            <div className="font-heading mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
              {status.isLoading ? <Skeleton className="h-7 w-40" /> : headline}
            </div>
          </div>
          {s?.configured && !s.connected && (
            <a
              href="/oauth/strava/start"
              className={cn(buttonVariants({ size: "lg" }), "shrink-0 gap-1.5")}
            >
              <ExternalLink className="size-4" aria-hidden />
              Connect
            </a>
          )}
          {s?.connected && (
            <Button
              type="button"
              variant="outline"
              className="shrink-0"
              onClick={() => disconnect.mutate()}
              disabled={disconnect.isPending}
            >
              {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
            </Button>
          )}
        </div>

        {status.isError && (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Could not reach API: {(status.error as Error).message}
          </p>
        )}
        {s && !s.configured && (
          <p className="text-sm text-muted-foreground">
            Add STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET to .env (from
            strava.com/settings/api), then restart the API.
          </p>
        )}
        {s?.configured && !s.connected && (
          <p className="text-sm text-muted-foreground">
            Connect from the Mac that runs the server — Strava returns to
            localhost. Keep “edit activities” ticked so run names can be
            pushed.
          </p>
        )}
        {s?.connected && s.can_write && (
          <p className="text-sm text-muted-foreground">
            Saving a run’s name also renames it on Strava. Run pages link
            to the Strava activity.
          </p>
        )}
        {s?.connected && !s.can_write && (
          <p className="text-sm text-amber-700 dark:text-amber-400">
            Links work, but renaming needs the “edit activities”
            permission. Disconnect and connect again with it ticked.
          </p>
        )}
        {disconnect.isError && (
          <p className="text-sm text-rose-700 dark:text-rose-400">
            Disconnect failed: {(disconnect.error as Error).message}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
