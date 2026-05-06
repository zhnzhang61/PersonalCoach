"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type { MonthlyStatsResponse } from "@/lib/types";

type ActivityKey =
  | "running"
  | "lap_swimming"
  | "stair_climbing"
  | "hiking"
  | "all";
type MetricKey =
  | "miles"
  | "hours"
  | "elevation_ft"
  | "avg_pace_dec"
  | "avg_hr";

// Hardcoded activity list rather than fetching presence — the user's data
// shape is stable across months and querying every type just to populate
// pills wastes round-trips. Empty months render as the "no data" message.
const ACTIVITIES: { key: ActivityKey; label: string }[] = [
  { key: "running", label: "Run" },
  { key: "lap_swimming", label: "Swim" },
  { key: "stair_climbing", label: "Stairs" },
  { key: "hiking", label: "Hike" },
  { key: "all", label: "All" },
];

const METRICS: {
  key: MetricKey;
  label: string;
  unit?: string;
  fmt: (n: number) => string;
}[] = [
  { key: "miles", label: "Miles", unit: "mi", fmt: (n) => n.toFixed(1) },
  { key: "hours", label: "Time", unit: "hrs", fmt: (n) => n.toFixed(1) },
  {
    key: "elevation_ft",
    label: "Elev",
    unit: "ft",
    fmt: (n) => Math.round(n).toLocaleString(),
  },
  // Pace lives in min/mi, only meaningful for running. Hidden in the toggle
  // when a non-running activity is selected.
  { key: "avg_pace_dec", label: "Pace", fmt: (n) => fmtPace(n) },
  {
    key: "avg_hr",
    label: "HR",
    unit: "bpm",
    fmt: (n) => Math.round(n).toString(),
  },
];

function fmtPace(d: number): string {
  if (!Number.isFinite(d) || d <= 0) return "—";
  return `${Math.floor(d)}:${String(Math.round((d % 1) * 60)).padStart(2, "0")}`;
}

// "2024-09" → "Sep '24". Short labels avoid wrapping when 24+ months stack.
function fmtMonth(m: string): string {
  const [y, mm] = m.split("-");
  const d = new Date(Number(y), Number(mm) - 1, 1);
  return `${d.toLocaleString("en", { month: "short" })} '${y.slice(2)}`;
}

export function MonthlyChart() {
  const [activity, setActivity] = useState<ActivityKey>("running");
  const [metric, setMetric] = useState<MetricKey>("miles");

  const { data, isLoading } = useQuery({
    queryKey: ["training", "monthly-stats", activity],
    queryFn: () =>
      apiGet<MonthlyStatsResponse>(
        `/api/training/monthly-stats?activity_type=${activity}`,
      ),
    staleTime: 60_000,
  });

  const visibleMetrics = METRICS.filter(
    (m) => activity === "running" || m.key !== "avg_pace_dec",
  );
  const metricSpec =
    visibleMetrics.find((m) => m.key === metric) ?? visibleMetrics[0];

  const months = data?.months ?? [];

  const chartConfig: ChartConfig = {
    [metric]: { label: metricSpec.label, color: "var(--chart-1)" },
  };

  // Pace lives in the 7-12 min/mi band, HR around 130-180; auto-domaining
  // from 0 squishes every bar to the same height. Tighten the y-domain so
  // month-to-month differences are actually visible.
  const yDomain: [number | "auto", number | "auto"] = (() => {
    if (metric !== "avg_pace_dec" && metric !== "avg_hr") return [0, "auto"];
    const vals = months
      .map((m) => m[metric])
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    if (vals.length === 0) return [0, "auto"];
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = metric === "avg_pace_dec" ? 0.5 : 5;
    return [Math.floor(lo - pad), Math.ceil(hi + pad)];
  })();

  return (
    <Card>
      <CardHeader className="space-y-3">
        <CardTitle className="text-base">Historical stats</CardTitle>
        <div className="-mx-1 flex gap-1 overflow-x-auto pb-1 [&::-webkit-scrollbar]:hidden [scrollbar-width:none]">
          {ACTIVITIES.map((a) => (
            <button
              key={a.key}
              type="button"
              onClick={() => {
                setActivity(a.key);
                // Pace is run-only — bounce to Miles when leaving running.
                if (a.key !== "running" && metric === "avg_pace_dec") {
                  setMetric("miles");
                }
              }}
              className={pillClass(activity === a.key)}
            >
              {a.label}
            </button>
          ))}
        </div>
        <div className="-mx-1 flex gap-1 overflow-x-auto pb-1 [&::-webkit-scrollbar]:hidden [scrollbar-width:none]">
          {visibleMetrics.map((m) => (
            <button
              key={m.key}
              type="button"
              onClick={() => setMetric(m.key)}
              className={pillClass(metric === m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : months.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No data for this activity yet.
          </p>
        ) : (
          <ChartContainer config={chartConfig} className="h-64 w-full">
            <BarChart data={months}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" />
              <XAxis
                dataKey="month"
                tickLine={false}
                axisLine={false}
                tickMargin={6}
                fontSize={10}
                tickFormatter={fmtMonth}
                // Recharts auto-thins ticks to fit the axis; keep first/last
                // visible so the time range is always anchored.
                interval="preserveStartEnd"
                minTickGap={20}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                tickMargin={6}
                fontSize={10}
                domain={yDomain}
                allowDataOverflow={false}
                tickFormatter={(v: number) =>
                  metric === "avg_pace_dec"
                    ? fmtPace(v)
                    : Math.round(v).toString()
                }
              />
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    labelFormatter={(_v, payload) =>
                      fmtMonth(payload?.[0]?.payload?.month ?? "")
                    }
                    formatter={(v) => {
                      const n = typeof v === "number" ? v : Number(v);
                      const value = Number.isFinite(n) ? metricSpec.fmt(n) : "—";
                      const label = metricSpec.unit
                        ? `${metricSpec.label} (${metricSpec.unit})`
                        : metricSpec.label;
                      return [value, label];
                    }}
                  />
                }
              />
              <Bar
                dataKey={metric}
                radius={[3, 3, 0, 0]}
                fill="var(--chart-1)"
              />
            </BarChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}

const pillClass = (active: boolean): string =>
  "shrink-0 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors " +
  (active
    ? "border-foreground bg-foreground text-background"
    : "border-border bg-background text-muted-foreground hover:text-foreground");
