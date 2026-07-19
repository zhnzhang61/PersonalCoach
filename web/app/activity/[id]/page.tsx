"use client";

import { use, useState } from "react";
import Link from "next/link";
import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Pencil, X } from "lucide-react";
import { apiGet } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDate } from "@/lib/format";
import type {
  RunActivity,
  RunDetailResponse,
  WeatherSnapshot,
} from "@/lib/types";
import { AskAiButton } from "@/components/activity/ask-ai-button";
import { EditRunForm } from "@/components/activity/edit-run-form";
import { TelemetryCharts } from "@/components/activity/telemetry-charts";
import {
  RunSummaryBlock,
  isTreadmillRun,
} from "@/components/activity/run-summary-block";

const RunMap = dynamic(
  () => import("@/components/activity/run-map").then((m) => m.RunMap),
  {
    ssr: false,
    loading: () => <Skeleton className="h-72 w-full" />,
  },
);

export default function ActivityDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const activityId = Number(id);

  // Key shape mirrors the other run sub-resources (weather/route/laps/
  // telemetry) so EditRunForm's `invalidateQueries({ queryKey: ["runs"] })`
  // refetches this after a save without needing its own explicit entry.
  const detailQuery = useQuery({
    queryKey: ["runs", activityId, "detail"],
    queryFn: () => apiGet<RunDetailResponse>(`/api/runs/${activityId}`),
    enabled: Number.isFinite(activityId),
  });

  const run: RunActivity | undefined = detailQuery.data?.run;

  const weatherQuery = useQuery({
    queryKey: ["runs", activityId, "weather"],
    queryFn: () => apiGet<WeatherSnapshot>(`/api/runs/${activityId}/weather`),
    staleTime: Infinity,
    retry: false,
    enabled: Number.isFinite(activityId),
  });

  const [editing, setEditing] = useState(false);

  if (detailQuery.isLoading) {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 pt-8 pb-12 sm:px-8">
        <Skeleton className="mb-6 h-6 w-32" />
        <Skeleton className="mb-3 h-10 w-2/3" />
        <Skeleton className="mb-2 h-4 w-1/2" />
        <Skeleton className="mb-6 h-4 w-1/3" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  if (detailQuery.isError || !run) {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 pt-8 pb-12 sm:px-8">
        <Link
          href="/activity"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to activities
        </Link>
        <p className="mt-6 rounded-md border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-700 dark:text-rose-300">
          Could not load this activity.
        </p>
      </div>
    );
  }

  const meta = run.manual_meta ?? {};
  const name = meta.name || run.activityName || "Run";
  const dateStr = run.startTimeLocal?.slice(0, 10);
  const w = weatherQuery.data;
  const showFeels =
    w?.apparent_temperature_f != null &&
    w.temperature_f != null &&
    Math.abs(w.apparent_temperature_f - w.temperature_f) >= 2;
  const datePart = dateStr ? fmtDate(dateStr, "EEE MMM d") : "—";
  const weatherSegments: string[] = [];
  if (w?.temperature_f != null) {
    weatherSegments.push(
      showFeels
        ? `${Math.round(w.temperature_f)}°F (feels ${Math.round(w.apparent_temperature_f!)}°F)`
        : `${Math.round(w.temperature_f)}°F`,
    );
  }
  if (w?.humidity_pct != null) weatherSegments.push(`${w.humidity_pct}% humidity`);

  return (
    <div className="mx-auto w-full max-w-4xl px-5 pt-8 pb-12 sm:px-8">
      <div className="mb-4 flex items-center justify-between">
        <Link
          href="/activity"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to activities
        </Link>
        <button
          type="button"
          onClick={() => setEditing((v) => !v)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-muted/40"
          aria-expanded={editing}
        >
          {editing ? (
            <>
              <X className="size-4" />
              Close
            </>
          ) : (
            <>
              <Pencil className="size-4" />
              Edit
            </>
          )}
        </button>
      </div>

      <div className="space-y-1">
        <h1 className="font-heading text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
          {name}
        </h1>
        <p className="text-sm text-muted-foreground">
          {[datePart, ...weatherSegments].join(" · ")}
        </p>
        {/* Distance/elevation and the per-effort chips all live inside
            RunSummaryBlock now — header carries only name/date/weather. */}
      </div>

      {editing && (
        <div className="mt-5 rounded-md border border-border bg-muted/20 p-4">
          <EditRunForm run={run} onClose={() => setEditing(false)} />
        </div>
      )}

      {meta.notes ? (
        <p className="mt-5 whitespace-pre-wrap rounded-md border border-border bg-muted/20 p-3 text-sm">
          {meta.notes}
        </p>
      ) : null}

      <div className="mt-5">
        <AskAiButton
          activityId={activityId}
          runDate={dateStr ?? null}
          runName={name}
        />
      </div>

      <div className="mt-6 space-y-4">
        {/* Block 1: map for outdoor runs; treadmill runs have no GPS, so
            no block at all. Block 2 (RunSummaryBlock) is the one shared
            module: headline stats + effort chips + per-lap effort bars —
            GPS-fed outdoors, model-fed on the treadmill. The telemetry
            line charts stay complete but fold away by default. */}
        {!isTreadmillRun(run) && <RunMap activityId={activityId} />}
        <RunSummaryBlock run={run} activityId={activityId} />
        <TelemetryDrawer activityId={activityId} />
      </div>
    </div>
  );
}

// Full telemetry line charts, folded by default — detailed but rarely
// visited; the fold keeps the page focused on the summary block.
function TelemetryDrawer({ activityId }: { activityId: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-sm text-muted-foreground hover:text-foreground"
        aria-expanded={open}
      >
        <span>Detailed telemetry</span>
        <span aria-hidden>{open ? "\u25be" : "\u25b8"}</span>
      </button>
      {open && (
        <div className="border-t border-border p-3">
          <TelemetryCharts activityId={activityId} />
        </div>
      )}
    </div>
  );
}
