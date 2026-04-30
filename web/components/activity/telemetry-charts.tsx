"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, Brush, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type { TelemetryResponse, TelemetryRow } from "@/lib/types";

type MetricKey = "HeartRate" | "Pace" | "StrideLength" | "Cadence" | "RespirationRate" | "Elevation";

interface MetricSpec {
  key: MetricKey;
  label: string;
  unit: string;
  color: string;
  invertY?: boolean;       // pace: lower = faster, so invert
  area?: boolean;          // elevation rendered as area for terrain feel
  clip?: [number, number]; // pace clipping to drop walks/standing
  formatAvg: (avg: number) => string;
  formatRange?: (min: number, max: number) => string;
}

const fmtPace = (decMinPerMi: number): string => {
  if (!Number.isFinite(decMinPerMi) || decMinPerMi <= 0) return "—";
  return `${Math.floor(decMinPerMi)}:${Math.round((decMinPerMi % 1) * 60)
    .toString()
    .padStart(2, "0")}`;
};

// Six tabs the user asked for. Pace gets clipped to a sensible run range so a
// few seconds of standing around at a stoplight don't blow up the y-axis.
const METRICS: MetricSpec[] = [
  {
    key: "HeartRate",
    label: "HR",
    unit: "bpm",
    color: "var(--chart-1)",
    formatAvg: (a) => `${Math.round(a)} bpm`,
    formatRange: (mn, mx) => `${Math.round(mn)}–${Math.round(mx)}`,
  },
  {
    key: "Pace",
    label: "Pace",
    unit: "min/mi",
    color: "var(--chart-2)",
    invertY: true,
    clip: [4, 14],
    formatAvg: (a) => `${fmtPace(a)} /mi`,
  },
  {
    key: "StrideLength",
    label: "Stride",
    unit: "cm",
    color: "var(--chart-3)",
    formatAvg: (a) => `${Math.round(a)} cm`,
  },
  {
    key: "Cadence",
    label: "Cadence",
    unit: "spm",
    color: "var(--chart-4)",
    formatAvg: (a) => `${Math.round(a)} spm`,
  },
  {
    key: "RespirationRate",
    label: "Resp",
    unit: "br/min",
    color: "var(--chart-5)",
    formatAvg: (a) => `${Math.round(a)} br/min`,
  },
  {
    key: "Elevation",
    label: "Elev",
    unit: "m",
    color: "var(--chart-1)",
    area: true,
    formatAvg: (a) => `${Math.round(a)} m`,
    formatRange: (mn, mx) => `gain ${Math.round(mx - mn)} m`,
  },
];

function downsampleEvery(rows: TelemetryRow[], step: number): TelemetryRow[] {
  if (step <= 1) return rows;
  return rows.filter((_, i) => i % step === 0);
}

function clipNonNumeric(rows: TelemetryRow[], key: MetricKey, range?: [number, number]): TelemetryRow[] {
  if (!range) return rows;
  const [lo, hi] = range;
  return rows.map((r) => {
    const v = r[key];
    if (v == null || typeof v !== "number" || v < lo || v > hi) {
      return { ...r, [key]: null };
    }
    return r;
  });
}

interface ChartPaneProps {
  rows: TelemetryRow[];
  spec: MetricSpec;
}

function ChartPane({ rows, spec }: ChartPaneProps) {
  const config: ChartConfig = {
    [spec.key]: { label: `${spec.label} (${spec.unit})`, color: spec.color },
  };

  const cleaned = clipNonNumeric(rows, spec.key, spec.clip);
  const haveData = cleaned.some((r) => r[spec.key] != null);
  if (!haveData) {
    return (
      <div className="flex h-44 items-center justify-center text-xs text-muted-foreground">
        No {spec.label.toLowerCase()} data for this run.
      </div>
    );
  }

  // Stable, locale-free time format. Always colon-delimited so labels can't
  // be confused with distance ("75m" used to read as 75 metres).
  //   < 1h:  MM:SS   (e.g. "25:00", "99:10")
  //   ≥ 1h:  H:MM    (e.g. "1:15"; appends ":SS" only when seconds non-zero)
  const xTickFormatter = (v: number | string | undefined) => {
    const n = typeof v === "number" ? v : Number(v);
    if (!Number.isFinite(n)) return "";
    const total = Math.round(n);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) {
      const base = `${h}:${String(m).padStart(2, "0")}`;
      return s === 0 ? base : `${base}:${String(s).padStart(2, "0")}`;
    }
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  if (spec.area) {
    return (
      <ChartContainer config={config} className="h-56 w-full">
        <AreaChart data={cleaned}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis
            dataKey="Second"
            tickLine={false}
            axisLine={false}
            tickMargin={6}
            fontSize={10}
            tickFormatter={xTickFormatter}
            type="number"
            domain={["dataMin", "dataMax"]}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            tickMargin={6}
            fontSize={10}
            domain={["auto", "auto"]}
            tickFormatter={(v) => Math.round(v).toString()}
          />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={(_v, payload) => xTickFormatter(payload?.[0]?.payload?.Second)} />} />
          <Area
            type="monotone"
            dataKey={spec.key}
            stroke={spec.color}
            fill={spec.color}
            fillOpacity={0.18}
            strokeWidth={1.5}
            isAnimationActive={false}
          />
          <Brush
            dataKey="Second"
            height={20}
            stroke="var(--muted-foreground)"
            travellerWidth={8}
            tickFormatter={xTickFormatter}
          />
        </AreaChart>
      </ChartContainer>
    );
  }

  return (
    <ChartContainer config={config} className="h-56 w-full">
      <LineChart data={cleaned}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis
          dataKey="Second"
          tickLine={false}
          axisLine={false}
          tickMargin={6}
          fontSize={10}
          tickFormatter={xTickFormatter}
          type="number"
          domain={["dataMin", "dataMax"]}
        />
        <YAxis
          tickLine={false}
          axisLine={false}
          tickMargin={6}
          fontSize={10}
          domain={["auto", "auto"]}
          reversed={!!spec.invertY}
          tickFormatter={(v) => Math.round(v).toString()}
        />
        <ChartTooltip content={<ChartTooltipContent labelFormatter={(_v, payload) => xTickFormatter(payload?.[0]?.payload?.Second)} />} />
        <Line
          type="monotone"
          dataKey={spec.key}
          stroke={spec.color}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
          connectNulls
        />
        <Brush
          dataKey="Second"
          height={20}
          stroke="var(--muted-foreground)"
          travellerWidth={8}
          tickFormatter={xTickFormatter}
        />
      </LineChart>
    </ChartContainer>
  );
}

export function TelemetryCharts({ activityId }: { activityId: number }) {
  // Charts use 5s downsample server-side + every-2nd client-side → about 1
  // point per 10s. Plenty of detail, keeps recharts snappy on phone.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["runs", activityId, "telemetry", 5],
    queryFn: () =>
      apiGet<TelemetryResponse>(
        `/api/runs/${activityId}/telemetry?downsample_sec=5`,
      ),
    staleTime: Infinity,
  });

  const [active, setActive] = useState<MetricKey>("HeartRate");

  if (isLoading) {
    return <Skeleton className="h-56 w-full" />;
  }
  if (isError || !data) {
    return (
      <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
        Telemetry not available for this run.
      </div>
    );
  }

  const rows = downsampleEvery(data.raw ?? [], 2);
  const spec = METRICS.find((m) => m.key === active) ?? METRICS[0];

  // Tab-aware caption: avg (and range when meaningful) computed from the
  // same data the chart is plotting, so a brushed sub-window's stats stay
  // honest with what's on screen. Pace clip is applied so a stoplight
  // stop doesn't drag the average up.
  const summaryFor = (key: MetricKey): { avg: number; min: number; max: number } | null => {
    const range = METRICS.find((m) => m.key === key)?.clip;
    const vals: number[] = [];
    for (const r of rows) {
      const v = r[key];
      if (typeof v !== "number" || !Number.isFinite(v)) continue;
      if (range && (v < range[0] || v > range[1])) continue;
      vals.push(v);
    }
    if (vals.length === 0) return null;
    return {
      avg: vals.reduce((a, b) => a + b, 0) / vals.length,
      min: Math.min(...vals),
      max: Math.max(...vals),
    };
  };
  const summary = summaryFor(active);
  const subtitle = summary
    ? spec.formatRange
      ? `avg ${spec.formatAvg(summary.avg)} · ${spec.formatRange(summary.min, summary.max)}`
      : `avg ${spec.formatAvg(summary.avg)}`
    : null;

  return (
    <div className="space-y-2">
      <div className="-mx-1 flex gap-1 overflow-x-auto pb-1">
        {METRICS.map((m) => {
          const isActive = m.key === active;
          return (
            <button
              key={m.key}
              type="button"
              onClick={() => setActive(m.key)}
              className={
                "shrink-0 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors " +
                (isActive
                  ? "border-foreground bg-foreground text-background"
                  : "border-border bg-background text-muted-foreground hover:text-foreground")
              }
            >
              {m.label}
            </button>
          );
        })}
      </div>
      {subtitle && (
        <p className="text-xs text-muted-foreground tabular-nums">{subtitle}</p>
      )}
      <ChartPane rows={rows} spec={spec} />
    </div>
  );
}
