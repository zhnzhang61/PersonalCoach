"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import {
  EFFORT_COLORS,
  EFFORT_FALLBACK,
  EFFORT_SHORT,
  REST_COLOR,
} from "@/lib/effort-colors";
import { cn } from "@/lib/utils";
import type {
  RunFullStats as RunFullStatsData,
  RunLapStatRow,
  RunZoneRow,
} from "@/lib/types";

// The "open Garmin for the details" replacement: full lap table, every
// summary number, time in zones, best efforts. All values arrive
// formatted from the server (data_processor.get_run_full_stats) — this
// file only lays them out.

export function RunFullStats({ activityId }: { activityId: number }) {
  const q = useQuery({
    queryKey: ["runs", activityId, "stats"],
    queryFn: () => apiGet<RunFullStatsData>(`/api/runs/${activityId}/stats`),
    enabled: Number.isFinite(activityId),
  });

  if (q.isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <p className="rounded-md border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-700 dark:text-rose-300">
        Could not load the full stats
        {q.error ? `: ${(q.error as Error).message}` : "."}
      </p>
    );
  }
  const d = q.data;

  return (
    <div className="space-y-4">
      {d.laps.length > 0 && (
        <Section title="Laps" count={`${d.laps.length}`}>
          <LapTable laps={d.laps} />
        </Section>
      )}
      {d.groups.length > 0 && (
        <Section title="All stats">
          <div className="grid gap-x-8 gap-y-5 sm:grid-cols-2 xl:grid-cols-3">
            {d.groups.map((g) => (
              <div key={g.key}>
                <div className="eyebrow mb-1.5 text-xs">{g.title}</div>
                <dl className="divide-y divide-border/60">
                  {g.items.map((i) => (
                    <div
                      key={i.key}
                      className="flex items-baseline justify-between gap-3 py-1.5"
                    >
                      <dt className="text-sm text-muted-foreground">
                        {i.label}
                        {i.note && (
                          <span className="block text-xs text-muted-foreground/70">
                            {i.note}
                          </span>
                        )}
                      </dt>
                      <dd className="shrink-0 text-right text-sm font-medium tabular-nums">
                        {i.display}
                      </dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))}
          </div>
        </Section>
      )}
      {(d.hr_zones.length > 0 || d.power_zones.length > 0) && (
        <Section title="Time in zones">
          <div className="grid gap-6 sm:grid-cols-2">
            {d.hr_zones.length > 0 && (
              <ZoneBars title="Heart rate" zones={d.hr_zones} />
            )}
            {d.power_zones.length > 0 && (
              <ZoneBars title="Power" zones={d.power_zones} />
            )}
          </div>
        </Section>
      )}
      {d.best_efforts.length > 0 && (
        <Section title="Best efforts in this run">
          <div className="flex flex-wrap gap-x-8 gap-y-3">
            {d.best_efforts.map((b) => (
              <div key={b.key}>
                <div className="text-xs text-muted-foreground">{b.label}</div>
                <div className="text-base font-semibold tabular-nums">
                  {b.display}
                </div>
                {b.pace && (
                  <div className="text-xs text-muted-foreground tabular-nums">
                    {b.pace} /mi
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-sm text-muted-foreground hover:text-foreground"
        aria-expanded={open}
      >
        <span>
          {title}
          {count && <span className="ml-1.5 text-xs">· {count}</span>}
        </span>
        <span aria-hidden>{open ? "▾" : "▸"}</span>
      </button>
      {open && <div className="border-t border-border p-4">{children}</div>}
    </div>
  );
}

type Col = {
  key: keyof RunLapStatRow;
  label: string;
  title: string;
  fmt?: (v: number) => string;
};

const COLS: Col[] = [
  { key: "distance_mi", label: "Dist", title: "Distance (mi)", fmt: (v) => v.toFixed(2) },
  { key: "time", label: "Time", title: "Lap time" },
  { key: "pace", label: "Pace", title: "Pace (min/mi)" },
  { key: "gap", label: "GAP", title: "Grade-adjusted pace (min/mi)" },
  { key: "avg_hr", label: "HR", title: "Average heart rate", fmt: (v) => v.toFixed(0) },
  { key: "max_hr", label: "Max", title: "Max heart rate", fmt: (v) => v.toFixed(0) },
  { key: "resp_brpm", label: "Resp", title: "Breaths per minute", fmt: (v) => v.toFixed(0) },
  { key: "cadence_spm", label: "Cad", title: "Cadence (steps/min)" },
  { key: "stride_m", label: "Stride", title: "Stride length (m)", fmt: (v) => v.toFixed(2) },
  { key: "gct_ms", label: "GCT", title: "Ground contact time (ms)" },
  { key: "gct_balance_left", label: "L %", title: "Ground contact balance, left foot (%)", fmt: (v) => v.toFixed(1) },
  { key: "vertical_osc_cm", label: "V.osc", title: "Vertical oscillation (cm)", fmt: (v) => v.toFixed(1) },
  { key: "vertical_ratio", label: "V.ratio", title: "Vertical ratio (%)", fmt: (v) => v.toFixed(1) },
  { key: "avg_power_w", label: "Power", title: "Average power (W)" },
  { key: "elev_gain_ft", label: "+ft", title: "Elevation gain (ft)" },
  { key: "elev_loss_ft", label: "−ft", title: "Elevation loss (ft)" },
];

function isRest(lap: RunLapStatRow): boolean {
  return lap.category === "Rest" || (!lap.category && lap.intensity === "REST");
}

function LapTable({ laps }: { laps: RunLapStatRow[] }) {
  // A column nobody has a value for (GAP and elevation indoors, running
  // dynamics on a swim) is dropped instead of shown as a wall of dashes.
  const cols = COLS.filter((c) => laps.some((l) => l[c.key] != null));
  const labelled = laps.some((l) => l.category);
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full border-collapse text-sm tabular-nums">
        <thead>
          <tr className="text-xs text-muted-foreground">
            <th className="sticky left-0 z-10 bg-background py-1.5 pr-3 text-left font-normal">
              Lap
            </th>
            {labelled && <th className="py-1.5 pr-3 text-left font-normal">Effort</th>}
            {cols.map((c) => (
              <th
                key={c.key}
                title={c.title}
                className="whitespace-nowrap py-1.5 pl-3 text-right font-normal"
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {laps.map((lap) => {
            const rest = isRest(lap);
            const color = rest
              ? REST_COLOR
              : lap.category
                ? (EFFORT_COLORS[lap.category] ?? EFFORT_FALLBACK)
                : null;
            return (
              <tr
                key={lap.lap}
                className={cn(
                  "border-t border-border/60",
                  rest && "text-muted-foreground",
                )}
              >
                <td className="sticky left-0 z-10 bg-background py-1.5 pr-3 text-left">
                  {lap.lap}
                </td>
                {labelled && (
                  <td className="whitespace-nowrap py-1.5 pr-3 text-left">
                    {color && (
                      <span
                        aria-hidden
                        className="mr-1.5 inline-block size-2.5 rounded-full align-middle"
                        style={{ backgroundColor: color }}
                      />
                    )}
                    {lap.category
                      ? (EFFORT_SHORT[lap.category] ?? lap.category)
                      : ""}
                  </td>
                )}
                {cols.map((c) => {
                  const v = lap[c.key];
                  return (
                    <td key={c.key} className="whitespace-nowrap py-1.5 pl-3 text-right">
                      {v == null
                        ? "—"
                        : typeof v === "number" && c.fmt
                          ? c.fmt(v)
                          : v}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Light → dark with intensity, same warm scale as the effort labels.
const ZONE_COLORS = ["#F5C4B3", "#F0997B", "#D85A30", "#993C1D", "#712B13"];

function ZoneBars({ title, zones }: { title: string; zones: RunZoneRow[] }) {
  return (
    <div>
      <div className="eyebrow mb-2 text-xs">{title}</div>
      <div className="space-y-1.5">
        {[...zones].reverse().map((z) => (
          <div key={z.zone} className="flex items-center gap-2 text-sm">
            <div className="w-24 shrink-0 text-xs text-muted-foreground">
              Z{z.zone}
              {z.low != null && (
                <span className="ml-1 tabular-nums">
                  ≥{Math.round(z.low)} {z.unit}
                </span>
              )}
            </div>
            <div className="h-4 flex-1 rounded-sm bg-muted/40">
              <div
                className="h-4 rounded-sm"
                style={{
                  width: `${Math.max(z.pct, z.seconds > 0 ? 1 : 0)}%`,
                  backgroundColor: ZONE_COLORS[(z.zone - 1) % ZONE_COLORS.length],
                }}
              />
            </div>
            <div className="w-24 shrink-0 text-right tabular-nums">
              {z.display}
              <span className="ml-1.5 text-xs text-muted-foreground">
                {z.pct.toFixed(0)}%
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
