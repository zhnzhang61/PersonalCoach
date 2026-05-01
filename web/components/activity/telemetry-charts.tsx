"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Brush,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import type {
  MetricSummary,
  TelemetryResponse,
  TelemetryRow,
  TelemetrySummaryKey,
} from "@/lib/types";

interface MetricSpec {
  key: TelemetrySummaryKey;
  label: string;
  color: string;
  invertY?: boolean;            // pace: lower = faster, so invert
  area?: boolean;               // elevation rendered as area for terrain feel
  yDomain?: [number, number];   // hard Y bounds (e.g., pace [4,14] to clip stoplight spikes visually)
  formatSubtitle: (s: MetricSummary) => string;
}

const fmtPace = (decMinPerMi: number): string => {
  if (!Number.isFinite(decMinPerMi) || decMinPerMi <= 0) return "—";
  return `${Math.floor(decMinPerMi)}:${Math.round((decMinPerMi % 1) * 60)
    .toString()
    .padStart(2, "0")}`;
};

// Tab specs. Subtitle formatting and Y-axis presentation rules live here;
// avg / min / max come from the server (data_processor.compute_telemetry_summary).
const METRICS_BASE: MetricSpec[] = [
  {
    key: "HeartRate",
    label: "HR",
    color: "var(--chart-1)",
    formatSubtitle: (s) =>
      `avg ${Math.round(s.avg)} bpm · ${Math.round(s.min)}–${Math.round(s.max)}`,
  },
  {
    key: "Pace",
    label: "Pace",
    color: "var(--chart-2)",
    invertY: true,
    formatSubtitle: (s) => `avg ${fmtPace(s.avg)} /mi`,
  },
  {
    key: "StrideLength",
    label: "Stride",
    color: "var(--chart-3)",
    formatSubtitle: (s) => `avg ${Math.round(s.avg)} cm`,
  },
  {
    key: "GroundContactBalanceLeft",
    label: "L/R",
    color: "var(--chart-2)",
    formatSubtitle: (s) =>
      `L ${s.avg.toFixed(1)}% / R ${(100 - s.avg).toFixed(1)}%`,
  },
  {
    key: "Cadence",
    label: "Cadence",
    color: "var(--chart-4)",
    formatSubtitle: (s) => `avg ${Math.round(s.avg)} spm`,
  },
  {
    key: "RespirationRate",
    label: "Resp",
    color: "var(--chart-5)",
    formatSubtitle: (s) => `avg ${Math.round(s.avg)} br/min`,
  },
  {
    key: "Elevation",
    label: "Elev",
    color: "var(--chart-1)",
    area: true,
    formatSubtitle: (s) =>
      `avg ${Math.round(s.avg)} m · gain ${Math.round(s.max - s.min)} m`,
  },
];

function downsampleEvery(rows: TelemetryRow[], step: number): TelemetryRow[] {
  if (step <= 1) return rows;
  return rows.filter((_, i) => i % step === 0);
}

interface ChartPaneProps {
  rows: TelemetryRow[];
  spec: MetricSpec;
}

function ChartPane({ rows, spec }: ChartPaneProps) {
  const config: ChartConfig = {
    [spec.key]: { label: spec.label, color: spec.color },
  };

  const haveData = rows.some((r) => r[spec.key] != null);
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

  const yDomain: [number | string, number | string] = spec.yDomain ?? ["auto", "auto"];
  const isBalance = spec.key === "GroundContactBalanceLeft";

  if (spec.area) {
    return (
      <ChartContainer config={config} className="h-56 w-full">
        <AreaChart data={rows}>
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
            domain={yDomain}
            tickFormatter={(v) => Math.round(v).toString()}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                labelFormatter={(_v, payload) =>
                  xTickFormatter(payload?.[0]?.payload?.Second)
                }
              />
            }
          />
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
      <LineChart data={rows}>
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
          domain={yDomain}
          allowDataOverflow={!!spec.yDomain}
          reversed={!!spec.invertY}
          tickFormatter={(v) => `${Math.round(v)}${isBalance ? "%" : ""}`}
        />
        <ChartTooltip
          content={
            <ChartTooltipContent
              labelFormatter={(_v, payload) =>
                xTickFormatter(payload?.[0]?.payload?.Second)
              }
            />
          }
        />
        {isBalance && (
          <ReferenceLine
            y={50}
            stroke="var(--muted-foreground)"
            strokeDasharray="3 3"
            strokeOpacity={0.5}
          />
        )}
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

  const [active, setActive] = useState<TelemetrySummaryKey>("HeartRate");

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

  // Hide tabs whose metric was never reported (e.g., L/R Balance for runs
  // recorded without a chest strap). Server's summary tells us this directly.
  const visibleMetrics = METRICS_BASE.filter((m) => data.summary[m.key] != null);

  // Pace's hard Y-axis bounds come from the server's pace_clip — the same
  // numbers used to compute the avg, so chart and subtitle agree.
  const paceClip = data.pace_clip;
  const metrics: MetricSpec[] = visibleMetrics.map((m) =>
    m.key === "Pace" ? { ...m, yDomain: paceClip } : m,
  );

  const rows = downsampleEvery(data.raw ?? [], 2);
  const spec =
    metrics.find((m) => m.key === active) ??
    metrics[0] ??
    METRICS_BASE[0];
  const summary = data.summary[spec.key] ?? null;
  const subtitle = summary ? spec.formatSubtitle(summary) : null;

  return (
    <div className="space-y-2">
      <div className="-mx-1 flex gap-1 overflow-x-auto pb-1">
        {metrics.map((m) => {
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
