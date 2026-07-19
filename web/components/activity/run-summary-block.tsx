"use client";

import { useQuery } from "@tanstack/react-query";
import { Footprints } from "lucide-react";
import { apiGet } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { effortColor, EFFORT_SHORT } from "@/lib/effort-colors";
import type {
  LapsResponse,
  RunActivity,
  RunCategoryStat,
  TreadmillEstimate,
} from "@/lib/types";

const TREADMILL_TYPE_KEYS = new Set(["treadmill_running", "indoor_running"]);

export function isTreadmillRun(run: RunActivity): boolean {
  const t = run.activityType ?? {};
  return (
    TREADMILL_TYPE_KEYS.has(t.typeKey ?? "") ||
    TREADMILL_TYPE_KEYS.has((t as { subTypeKey?: string }).subTypeKey ?? "")
  );
}

interface LapRow {
  lap: number;
  category: string | null;
  pace_s: number | null;
  pace_str: string | null;
  distance_mi: number | null;
  avg_hr: number | null;
}

function fmtPace(paceS: number): string {
  const s = Math.round(paceS);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function fmtDuration(totalS: number): string {
  const s = Math.round(totalS);
  if (s >= 3600)
    return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// The one run-summary module both run types share (v3 design):
// headline stats → effort chips → per-lap effort bars. Outdoor runs
// feed it GPS truth; treadmill runs feed it the road-equivalent model
// (rows are still Garmin laps — the label coordinate system — but every
// number on them is model-derived, never the wrist guess).
export function RunSummaryBlock({
  run,
  activityId,
}: {
  run: RunActivity;
  activityId: number;
}) {
  const treadmill = isTreadmillRun(run);

  const estimateQuery = useQuery({
    queryKey: ["runs", activityId, "treadmill-estimate"],
    queryFn: () =>
      apiGet<TreadmillEstimate>(`/api/runs/${activityId}/treadmill-estimate`),
    staleTime: 5 * 60_000,
    retry: false,
    enabled: treadmill,
  });

  const lapsQuery = useQuery({
    queryKey: ["runs", activityId, "laps"],
    queryFn: () => apiGet<LapsResponse>(`/api/runs/${activityId}/laps`),
    enabled: !treadmill,
  });

  if (treadmill ? estimateQuery.isLoading : lapsQuery.isLoading) {
    return <Skeleton className="h-48 w-full" />;
  }

  if (treadmill && estimateQuery.isError) {
    const msg = (estimateQuery.error as Error)?.message ?? "";
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
  if (!treadmill && lapsQuery.isError) {
    return (
      <p className="rounded-md border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
        Could not load laps for this run.
      </p>
    );
  }

  // ---- normalize both sources into one row/stat shape ----
  let distanceMi: number;
  let durationS: number;
  let rows: LapRow[];
  let chips: RunCategoryStat[];

  if (treadmill) {
    const data = estimateQuery.data;
    if (!data) return null;
    distanceMi = data.estimate.total_distance_mi;
    durationS = data.estimate.duration_s;
    chips = data.estimate.category_stats_model;
    rows = data.estimate.laps.map((l) => ({
      lap: l.lap,
      category: l.category,
      pace_s: l.pace_s,
      pace_str: l.pace_str,
      distance_mi: l.model_distance_mi,
      avg_hr: l.avg_hr,
    }));
  } else {
    const data = lapsQuery.data;
    if (!data) return null;
    distanceMi = (run.distance ?? 0) / 1609.34;
    durationS = run.movingDuration ?? run.duration ?? 0;
    chips = run.manual_meta?.category_stats ?? [];
    rows = data.laps.map((l, i) => {
      const mi = l.distance / 1609.34;
      const paceS = mi > 0.01 ? l.duration / mi : null;
      return {
        lap: i + 1,
        category: l.category ?? null,
        pace_s: paceS,
        pace_str: paceS ? fmtPace(paceS) : null,
        distance_mi: mi,
        avg_hr: l.averageHR ?? null,
      };
    });
  }

  const avgPaceS = distanceMi > 0.01 ? durationS / distanceMi : null;
  // Elevation joins the stat row (moved out of the page header). The
  // watch reports none/zero indoors, so the stat self-hides on
  // treadmill runs.
  const elevFt = Math.round((run.elevationGain ?? 0) * 3.281);

  // Bar width ∝ speed, min-max amplified into 40–100% across the
  // non-Rest rows (raw ratios make near-equal laps indistinguishable).
  const paced = rows.filter((r) => r.category !== "Rest" && r.pace_s != null);
  const fast = Math.min(...paced.map((r) => r.pace_s!));
  const slow = Math.max(...paced.map((r) => r.pace_s!));
  const widthPct = (p: number) =>
    slow === fast ? 100 : 40 + (60 * (slow - p)) / (slow - fast);

  // Distance suffix on a lap number only when the lap is an outlier:
  // outdoor = not a 1.00mi autolap; treadmill = >25% off the median
  // model lap (watch-miles are uniformly fake there — repeating "0.90"
  // twelve times is noise, the short tail lap is signal).
  const medianMi = [...paced]
    .map((r) => r.distance_mi ?? 0)
    .sort((a, b) => a - b)[Math.floor(paced.length / 2)];
  const showDist = (r: LapRow) => {
    if (r.distance_mi == null) return false;
    if (!treadmill) return Math.abs(r.distance_mi - 1.0) > 0.02;
    return medianMi > 0 && Math.abs(r.distance_mi / medianMi - 1) > 0.25;
  };

  const presentCats = [
    ...new Set(rows.map((r) => r.category).filter((c): c is string => !!c)),
  ].filter((c) => c !== "Rest");

  return (
    <div
      className={
        treadmill
          ? "rounded-md border border-warm-accent/40 bg-warm-bg/40 p-4"
          : "rounded-md border border-border p-4"
      }
    >
      {treadmill && (
        <div className="mb-3 flex items-center gap-2">
          <Footprints className="size-4 text-muted-foreground" aria-hidden />
          <h3 className="font-heading text-lg font-semibold tracking-tight">
            Road-equivalent estimate
          </h3>
        </div>
      )}

      <div className="flex flex-wrap items-end gap-x-5 gap-y-2">
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Distance
          </div>
          <div className="font-heading text-2xl font-semibold">
            {distanceMi.toFixed(2)}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              mi
            </span>
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Avg pace
          </div>
          <div className="font-heading text-2xl font-semibold">
            {avgPaceS ? fmtPace(avgPaceS) : "—"}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              /mi
            </span>
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            Time
          </div>
          <div className="font-heading text-2xl font-semibold">
            {fmtDuration(durationS)}
          </div>
        </div>
        {elevFt > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wide text-muted-foreground">
              Elev
            </div>
            <div className="font-heading text-2xl font-semibold">
              ↑{elevFt.toLocaleString()}
              <span className="ml-1 text-sm font-normal text-muted-foreground">
                ft
              </span>
            </div>
          </div>
        )}
      </div>
      {treadmill && (
        <div className="mt-1 text-xs text-muted-foreground">
          distance is the number to enter in Garmin
        </div>
      )}

      {chips.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {chips.map((c) => (
            <span
              key={c.category}
              className="rounded-full border px-2 py-0.5 text-xs"
              style={{
                borderColor: effortColor(c.category),
                backgroundColor: `${effortColor(c.category)}2E`,
              }}
            >
              {c.category} · {c.distance_mi.toFixed(1)}mi · {c.pace}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3">
        <div className="grid grid-cols-[2.5rem_3.25rem_1fr_2.5rem] items-center gap-x-2 text-xs uppercase tracking-wide text-muted-foreground">
          <span>Lap</span>
          <span>Pace</span>
          <span />
          <span className="text-right">HR</span>
        </div>
        <div className="mt-1 space-y-1">
          {rows.map((r) =>
            r.category === "Rest" ? (
              <div
                key={r.lap}
                className="grid grid-cols-[2.5rem_3.25rem_1fr_2.5rem] items-center gap-x-2"
              >
                <span className="text-[10px] text-muted-foreground">rest</span>
                <span />
                <div className="h-1 w-[18%] rounded-full bg-border" />
                <span className="text-right font-mono text-[10px] text-muted-foreground">
                  {r.avg_hr ?? ""}
                </span>
              </div>
            ) : (
              <div
                key={r.lap}
                className="grid grid-cols-[2.5rem_3.25rem_1fr_2.5rem] items-center gap-x-2"
              >
                <span className="text-xs text-muted-foreground">
                  {r.lap}
                  {showDist(r) && (
                    <span className="ml-0.5 text-[10px]">
                      {r.distance_mi!.toFixed(2)}
                    </span>
                  )}
                </span>
                <span className="font-mono text-xs font-semibold">
                  {r.pace_str ?? "—"}
                </span>
                <div
                  className="h-3 rounded-full"
                  style={{
                    width: `${r.pace_s != null ? widthPct(r.pace_s) : 40}%`,
                    backgroundColor: effortColor(r.category),
                  }}
                />
                <span className="text-right font-mono text-xs text-muted-foreground">
                  {r.avg_hr ?? "—"}
                </span>
              </div>
            ),
          )}
        </div>
        {presentCats.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground">
            {presentCats.map((c) => (
              <span key={c} className="inline-flex items-center gap-1">
                <span
                  className="size-2 rounded-[3px]"
                  style={{ backgroundColor: effortColor(c) }}
                />
                {EFFORT_SHORT[c] ?? c}
              </span>
            ))}
          </div>
        )}
      </div>

      {treadmill && estimateQuery.data && (
        <p className="mt-3 text-xs text-muted-foreground">
          Rows are watch laps; distance and pace are integrated from HR +
          cadence curves — watch/belt speeds are not used. Model calibrated
          on {estimateQuery.data.model.n_laps} laps from{" "}
          {estimateQuery.data.model.n_runs} outdoor runs (last{" "}
          {estimateQuery.data.model.window_days} days
          {estimateQuery.data.model.cv_median_pct != null
            ? `, ±${estimateQuery.data.model.cv_median_pct}% typical`
            : ""}
          ).
        </p>
      )}
    </div>
  );
}
