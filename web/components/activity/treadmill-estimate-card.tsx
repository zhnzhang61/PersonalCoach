"use client";

import { useQuery } from "@tanstack/react-query";
import { Footprints } from "lucide-react";
import { apiGet } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import type { TreadmillEstimate } from "@/lib/types";

// Road-equivalent estimate for treadmill runs, computed server-side from
// the HR + cadence curves (backend/treadmill_model.py). Shown INSTEAD of
// trusting the watch's accelerometer distance (underestimates ~1 min/mi)
// or the belt display (overstates, worse at speed). The model retrains
// itself from recent outdoor labeled runs, so this card also surfaces
// what the current fit is based on.
export function TreadmillEstimateCard({ activityId }: { activityId: number }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["runs", activityId, "treadmill-estimate"],
    queryFn: () =>
      apiGet<TreadmillEstimate>(`/api/runs/${activityId}/treadmill-estimate`),
    staleTime: 5 * 60_000,
    retry: false,
  });

  if (isLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (isError) {
    const msg = (error as Error)?.message ?? "";
    // 503 = not enough recent labeled outdoor runs to calibrate — a
    // fixable data condition, not a bug; say so instead of a red error.
    if (msg.includes("503")) {
      return (
        <div className="rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
          Road-pace estimate needs more labeled outdoor runs from the last
          few months to calibrate. Label recent outdoor runs, then revisit.
        </div>
      );
    }
    return (
      <p className="rounded-md border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
        Could not compute the road-pace estimate: {msg || "unknown error"}
      </p>
    );
  }

  if (!data) {
    return null;
  }

  const est = data.estimate;
  const model = data.model;

  return (
    <div className="rounded-md border border-warm-accent/40 bg-warm-bg/40 p-4">
      <div className="mb-3 flex items-center gap-2">
        <Footprints className="size-4 text-muted-foreground" aria-hidden />
        <h3 className="font-heading text-lg font-semibold tracking-tight">
          Road-equivalent estimate
        </h3>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Distance
          </div>
          <div className="font-heading text-2xl font-semibold">
            {est.total_distance_mi.toFixed(2)}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              mi
            </span>
          </div>
          <div className="text-xs text-muted-foreground">
            {est.total_distance_km.toFixed(2)} km · use this in Garmin
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Avg pace
          </div>
          <div className="font-heading text-2xl font-semibold">
            {est.avg_pace_str}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              /mi
            </span>
          </div>
          <div className="text-xs text-muted-foreground">
            over {est.duration_str}
          </div>
        </div>
        <div className="col-span-2 sm:col-span-1">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Mile splits
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {est.splits.map((s) => (
              <span
                key={s.mile}
                className="rounded bg-muted/60 px-1.5 py-0.5 font-mono text-xs"
                title={
                  s.partial_mi != null
                    ? `last ${s.partial_mi} mi`
                    : `mile ${s.mile}`
                }
              >
                {s.partial_mi != null ? `+${s.partial_mi}` : s.mile}·
                {s.pace_str}
              </span>
            ))}
          </div>
        </div>
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        From HR + cadence curves — watch/belt speeds are not used. Model
        calibrated on {model.n_laps} laps from {model.n_runs} outdoor runs
        (last {model.window_days} days, through {model.trained_through}
        {model.cv_median_pct != null
          ? `, ±${model.cv_median_pct}% typical`
          : ""}
        ).
      </p>
    </div>
  );
}
