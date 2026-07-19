"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Brush,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
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
import { effortColor, EFFORT_SHORT, REST_COLOR } from "@/lib/effort-colors";
import { RespHrScatter } from "@/components/activity/resp-hr-scatter";
import type {
  MetricSummary,
  TelemetryResponse,
  TelemetryRow,
  TelemetrySummaryKey,
  VerdictAnchor,
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

type XMode = "time" | "distance";

// ---- Chart decorations (PR #114) -----------------------------------------
// Effort-label washes + lap ticks + verdict receipt bands, all in the
// run's own time coordinates. Lap boundaries live on the cumulative
// lap-duration clock — the same clock the backend cuts verdict anchors
// on, so receipts land exactly where the verdict measured.

interface LapDuration {
  duration: number;
}

interface EffortBlock {
  label: string | null;
  start_sec: number;
  end_sec: number;
}

function effortBlocks(
  laps: LapDuration[],
  categories: string[],
): EffortBlock[] {
  const blocks: EffortBlock[] = [];
  let cursor = 0;
  laps.forEach((lap, i) => {
    const label = categories[i] ?? null;
    const end = cursor + (lap.duration ?? 0);
    const last = blocks[blocks.length - 1];
    if (last && last.label === label) {
      last.end_sec = end;
    } else {
      blocks.push({ label, start_sec: cursor, end_sec: end });
    }
    cursor = end;
  });
  return blocks;
}

// In distance mode the x-axis is cumulative miles, but blocks/anchors
// are in seconds — interpolate through the telemetry rows.
function makeSecToX(
  xMode: XMode,
  rows: TelemetryRow[],
): (sec: number) => number | null {
  if (xMode === "time") return (sec) => sec;
  const pts = rows
    .filter(
      (r) => typeof r.Distance === "number" && Number.isFinite(r.Distance),
    )
    .map((r) => [r.Second, r.Distance as number] as const);
  if (pts.length === 0) return () => null;
  return (sec) => {
    if (sec <= pts[0][0]) return pts[0][1];
    for (let i = 1; i < pts.length; i++) {
      if (pts[i][0] >= sec) {
        const [s0, d0] = pts[i - 1];
        const [s1, d1] = pts[i];
        return s1 === s0 ? d0 : d0 + ((d1 - d0) * (sec - s0)) / (s1 - s0);
      }
    }
    return pts[pts.length - 1][1];
  };
}

const RECEIPT_COLOR = "#EF9F27";

interface Decorations {
  blocks: EffortBlock[];
  lapBoundaries: number[];
  receipts: VerdictAnchor[];
  highlight: VerdictAnchor | null;
}

// Wash + rail in one custom ReferenceArea shape: the pale full-height
// wash, plus a full-saturation 6px rail at the top of the plot —
// adjacent effort tints are indistinguishable at wash opacity; the
// rail is what actually identifies the block (mockup-verified). A
// custom shape gets the computed pixel rect, so no phantom [0,1] axis
// is needed (recharts drops ReferenceAreas bound to an axis that has
// no series).
function washWithRail(fill: string) {
  return function WashWithRail(p: {
    x?: number;
    y?: number;
    width?: number;
    height?: number;
  }) {
    const { x = 0, y = 0, width = 0, height = 0 } = p;
    return (
      <g>
        <rect x={x} y={y} width={width} height={height} fill={fill} fillOpacity={0.14} />
        <rect x={x} y={y} width={width} height={6} fill={fill} fillOpacity={1} />
      </g>
    );
  };
}

// Returns recharts elements — must be spread directly into a chart's
// children (recharts dispatches on child type, so a wrapper component
// would be invisible to it).
function renderDecorations(
  dec: Decorations | null,
  secToX: (sec: number) => number | null,
  yAxisId?: string,
) {
  if (!dec) return [];
  const axisProps = yAxisId ? { yAxisId } : {};
  const out = [];
  for (const b of dec.blocks) {
    if (!b.label) continue;
    const x1 = secToX(b.start_sec);
    const x2 = secToX(b.end_sec);
    if (x1 == null || x2 == null) continue;
    const color = b.label === "Rest" ? REST_COLOR : effortColor(b.label);
    out.push(
      <ReferenceArea
        key={`wash-${b.start_sec}`}
        {...axisProps}
        x1={x1}
        x2={x2}
        fill={color}
        stroke="none"
        shape={washWithRail(color)}
      />,
    );
  }
  for (const sec of dec.lapBoundaries) {
    const x = secToX(sec);
    if (x == null) continue;
    out.push(
      <ReferenceLine
        key={`lap-${sec}`}
        {...axisProps}
        x={x}
        stroke="var(--muted-foreground)"
        strokeOpacity={0.25}
        strokeDasharray="2 6"
      />,
    );
  }
  for (const r of dec.receipts) {
    const x1 = secToX(r.start_sec);
    const x2 = secToX(r.end_sec);
    if (x1 == null || x2 == null) continue;
    const active =
      dec.highlight != null &&
      dec.highlight.start_sec === r.start_sec &&
      dec.highlight.end_sec === r.end_sec;
    out.push(
      <ReferenceArea
        key={`receipt-${r.start_sec}`}
        {...axisProps}
        x1={x1}
        x2={x2}
        fill={RECEIPT_COLOR}
        fillOpacity={active ? 0.32 : 0.16}
        stroke={active ? RECEIPT_COLOR : "none"}
        strokeOpacity={0.8}
      />,
    );
  }
  return out;
}

// Stable, locale-free time format. Always colon-delimited so labels can't
// be confused with distance ("75m" used to read as 75 metres).
//   < 1h:  MM:SS   (e.g. "25:00", "99:10")
//   ≥ 1h:  H:MM    (e.g. "1:15"; appends ":SS" only when seconds non-zero)
const fmtTimeTick = (v: number | string | undefined) => {
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

// Miles, one decimal under 10mi; whole numbers above that. Keeps tick
// width steady so labels don't visually jiggle as ticks scroll past.
const fmtDistanceTick = (v: number | string | undefined) => {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "";
  return n >= 10 && n % 1 < 0.05 ? `${Math.round(n)}` : n.toFixed(1);
};

const xKeyFor = (mode: XMode): "Second" | "Distance" =>
  mode === "time" ? "Second" : "Distance";
const xTickFor = (mode: XMode) =>
  mode === "time" ? fmtTimeTick : fmtDistanceTick;

interface ChartPaneProps {
  rows: TelemetryRow[];
  // 1 spec → single-axis chart (area allowed for elevation).
  // 2 specs → dual-axis line chart; first goes on the left, second on the right.
  specs: MetricSpec[];
  // x-axis mode: "time" plots seconds, "distance" plots cumulative miles.
  xMode: XMode;
  // Effort washes / lap ticks / verdict receipts (PR #114).
  decorations?: Decorations | null;
}

function ChartPane({ rows, specs, xMode, decorations = null }: ChartPaneProps) {
  const xKey = xKeyFor(xMode);
  const xTickFormatter = xTickFor(xMode);
  const secToX = makeSecToX(xMode, rows);
  const primary = specs[0];
  const secondary = specs[1] ?? null;

  // Always-on elevation silhouette: terrain rides at the bottom of
  // every chart as attribution context (a HR bump sitting on a hill
  // explains itself). Squashed into the bottom quarter via its own
  // hidden axis; skipped when Elevation is itself being plotted.
  const elevVals = rows
    .map((r) => r.Elevation)
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const showElevSilhouette =
    elevVals.length > 1 && !specs.some((s) => s.key === "Elevation");
  const elevMin = showElevSilhouette ? Math.min(...elevVals) : 0;
  const elevMax = showElevSilhouette ? Math.max(...elevVals) : 1;
  const elevDomain: [number, number] = [
    elevMin,
    elevMin + Math.max(elevMax - elevMin, 1) * 4,
  ];
  const config: ChartConfig = {
    [primary.key]: { label: primary.label, color: primary.color },
  };
  if (secondary) {
    config[secondary.key] = { label: secondary.label, color: secondary.color };
  }

  const haveData = rows.some((r) => specs.some((s) => r[s.key] != null));
  if (!haveData) {
    return (
      <div className="flex h-44 items-center justify-center text-xs text-muted-foreground">
        No data for this run.
      </div>
    );
  }

  const yTickFormatter = (s: MetricSpec) => (v: number) =>
    s.key === "GroundContactBalanceLeft"
      ? `${Number.isInteger(v) ? v : v.toFixed(1)}%`
      : `${Math.round(v)}`;

  // L/R renders centered on 50% with a symmetric domain sized to the
  // run's p99 deviation — the 49–51 comfort band and the distance the
  // line escapes it are the story, not the absolute value. (The user
  // has a chronic imbalance and reads this chart specifically.) p99
  // rather than max so a single-sample sensor glitch can't zoom the
  // axis out and flatten the band; the glitch itself gets clipped via
  // allowDataOverflow on the axis.
  const lrDevs = rows
    .map((r) => r.GroundContactBalanceLeft)
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v))
    .map((v) => Math.abs(v - 50))
    .sort((a, b) => a - b);
  const lrP99 = lrDevs.length
    ? lrDevs[Math.floor(0.99 * (lrDevs.length - 1))]
    : 0;
  const lrSpan = Math.max(1.5, lrP99 + 0.3);
  const lrDomain: [number, number] = [50 - lrSpan, 50 + lrSpan];
  const domainFor = (
    s: MetricSpec,
  ): [number | string, number | string] =>
    s.key === "GroundContactBalanceLeft"
      ? lrDomain
      : (s.yDomain ?? ["auto", "auto"]);

  // Single-spec mode keeps the elevation area shading. Dual-spec collapses
  // to a flat dual-axis line chart — overlaying area fills with another
  // line gets visually noisy.
  if (!secondary && primary.area) {
    const yDomain: [number | string, number | string] = primary.yDomain ?? ["auto", "auto"];
    return (
      <ChartContainer config={config} className="h-56 w-full">
        <AreaChart data={rows}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis
            dataKey={xKey}
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
            width={34}
            domain={yDomain}
            tickFormatter={yTickFormatter(primary)}
          />
          {renderDecorations(decorations, secToX)}
          <ChartTooltip
            content={
              <ChartTooltipContent
                labelFormatter={(_v, payload) =>
                  xTickFormatter(payload?.[0]?.payload?.[xKey])
                }
              />
            }
          />
          <Area
            type="monotone"
            dataKey={primary.key}
            stroke={primary.color}
            fill={primary.color}
            fillOpacity={0.18}
            strokeWidth={1.5}
            isAnimationActive={false}
          />
          <Brush
            dataKey={xKey}
            height={20}
            stroke="var(--muted-foreground)"
            travellerWidth={8}
            tickFormatter={xTickFormatter}
          />
        </AreaChart>
      </ChartContainer>
    );
  }

  const renderAxis = (s: MetricSpec, side: "left" | "right") => {
    const domain = domainFor(s);
    const isLR = s.key === "GroundContactBalanceLeft";
    return (
      <YAxis
        key={side}
        yAxisId={side}
        orientation={side}
        tickLine={false}
        axisLine={false}
        tickMargin={6}
        fontSize={10}
        width={34}
        domain={domain}
        // Band edges + center only — auto ticks on a fractional
        // domain produce noise like "46.1%".
        ticks={isLR ? [49, 50, 51] : undefined}
        allowDataOverflow={!!s.yDomain || isLR}
        reversed={!!s.invertY}
        tickFormatter={yTickFormatter(s)}
        // Tick text in the metric's color so users can read which axis is which.
        tick={{ fill: s.color }}
      />
    );
  };

  return (
    <ChartContainer config={config} className="h-56 w-full">
      <ComposedChart data={rows}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis
          dataKey={xKey}
          tickLine={false}
          axisLine={false}
          tickMargin={6}
          fontSize={10}
          tickFormatter={xTickFormatter}
          type="number"
          domain={["dataMin", "dataMax"]}
        />
        {renderAxis(primary, "left")}
        {secondary && renderAxis(secondary, "right")}
        {showElevSilhouette && (
          <YAxis yAxisId="elev" hide domain={elevDomain} />
        )}
        {renderDecorations(decorations, secToX, "left")}
        {showElevSilhouette && (
          <Area
            yAxisId="elev"
            type="monotone"
            dataKey="Elevation"
            stroke="none"
            fill="var(--muted-foreground)"
            fillOpacity={0.16}
            isAnimationActive={false}
            connectNulls
          />
        )}
        <ChartTooltip
          content={
            <ChartTooltipContent
              labelFormatter={(_v, payload) =>
                xTickFormatter(payload?.[0]?.payload?.[xKey])
              }
            />
          }
        />
        {primary.key === "GroundContactBalanceLeft" && [
          <ReferenceArea
            key="lr-band"
            yAxisId="left"
            y1={49}
            y2={51}
            fill="var(--muted-foreground)"
            fillOpacity={0.08}
            stroke="none"
          />,
          <ReferenceLine
            key="lr-50"
            yAxisId="left"
            y={50}
            stroke="var(--muted-foreground)"
            strokeDasharray="3 3"
            strokeOpacity={0.5}
          />,
        ]}
        {secondary?.key === "GroundContactBalanceLeft" && [
          <ReferenceArea
            key="lr-band-r"
            yAxisId="right"
            y1={49}
            y2={51}
            fill="var(--muted-foreground)"
            fillOpacity={0.08}
            stroke="none"
          />,
          <ReferenceLine
            key="lr-50-r"
            yAxisId="right"
            y={50}
            stroke="var(--muted-foreground)"
            strokeDasharray="3 3"
            strokeOpacity={0.5}
          />,
        ]}
        <Line
          yAxisId="left"
          type="monotone"
          dataKey={primary.key}
          stroke={primary.color}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
          connectNulls
        />
        {secondary && (
          <Line
            yAxisId="right"
            type="monotone"
            dataKey={secondary.key}
            stroke={secondary.color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
            connectNulls
          />
        )}
        <Brush
          dataKey={xKey}
          height={20}
          stroke="var(--muted-foreground)"
          travellerWidth={8}
          tickFormatter={xTickFormatter}
        />
      </ComposedChart>
    </ChartContainer>
  );
}

export function TelemetryCharts({
  activityId,
  laps,
  categories,
  receipts,
  highlight,
  lrThirdsDetail,
}: {
  activityId: number;
  // Garmin lap durations + the user's effort labels — drives the
  // wash/tick decorations. Either missing → plain charts, no washes.
  laps?: LapDuration[];
  categories?: string[];
  // Attention-verdict anchor windows (amber receipt bands); highlight
  // is the one the user tapped in the verdict rows.
  receipts?: VerdictAnchor[];
  highlight?: VerdictAnchor | null;
  // Preformatted thirds line from the lr_asymmetry verdict (e.g.
  // "+1.2% 右 → +0.1% 右 → +0.4% 左") — shown as the L/R subtitle.
  // Server-formatted per the no-shaping-in-dashboard rule.
  lrThirdsDetail?: string | null;
}) {
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

  // Up to two metrics overlay on the chart. active[0] owns the left Y axis,
  // active[1] (when present) the right. Click rules:
  //   • already-selected, len 2  → drop it (whichever slot it was in)
  //   • already-selected, len 1  → no-op (must always keep ≥1)
  //   • not selected,    len 1   → add as secondary
  //   • not selected,    len 2   → swap into the secondary slot
  // When the primary is dropped from a 2-up view, the surviving metric
  // takes over the left axis on the next render.
  const [active, setActive] = useState<TelemetrySummaryKey[]>(["HeartRate"]);
  // Garmin-style time/distance toggle. Default to time — runs without GPS
  // (treadmill, indoor) won't have distance, so we fall back to time below
  // even if the user previously picked distance.
  const [xMode, setXMode] = useState<XMode>("time");
  // Resp offers a second lens: the curve over time, or the Resp×HR
  // relationship scatter (knee ≈ ventilatory threshold).
  const [respRelation, setRespRelation] = useState(false);

  const onTabClick = (key: TelemetrySummaryKey) => {
    setActive((prev) => {
      const isSelected = prev.includes(key);
      if (isSelected) {
        return prev.length === 1 ? prev : prev.filter((k) => k !== key);
      }
      if (prev.length === 1) return [prev[0], key];
      return [prev[0], key];
    });
  };

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

  const rawRows = data.raw ?? [];
  // Distance mode is only useful if we actually have GPS distance samples
  // (indoor / treadmill runs come back without sumDistance). Hide the
  // toggle and force time when there's no usable distance series.
  const distanceAvailable = rawRows.some(
    (r) => typeof r.Distance === "number" && Number.isFinite(r.Distance),
  );
  const effectiveXMode: XMode =
    xMode === "distance" && distanceAvailable ? "distance" : "time";

  // In distance mode, drop rows whose Distance is null/undefined — recharts
  // would otherwise plot them at x=0 and skew the axis.
  const rows = downsampleEvery(
    effectiveXMode === "distance"
      ? rawRows.filter(
          (r) =>
            typeof r.Distance === "number" && Number.isFinite(r.Distance),
        )
      : rawRows,
    2,
  );
  const activeSpecs: MetricSpec[] = active
    .map((k) => metrics.find((m) => m.key === k))
    .filter((m): m is MetricSpec => m != null);
  // Defensive fallback if the saved selection no longer maps to a visible metric.
  const renderSpecs = activeSpecs.length > 0 ? activeSpecs : [metrics[0] ?? METRICS_BASE[0]];

  const blocks =
    laps && laps.length > 0 && categories && categories.length > 0
      ? effortBlocks(laps, categories)
      : [];
  // Cumulative lap-duration boundaries; the run's end isn't one.
  const lapBoundaries: number[] = [];
  let cum = 0;
  for (const l of laps ?? []) {
    cum += l.duration ?? 0;
    lapBoundaries.push(cum);
  }
  lapBoundaries.pop();
  const decorations: Decorations | null =
    blocks.length > 0 || (receipts?.length ?? 0) > 0
      ? {
          blocks,
          lapBoundaries,
          receipts: receipts ?? [],
          highlight: highlight ?? null,
        }
      : null;

  const subtitleParts = renderSpecs.map((s) => {
    // L/R gets the verdict's thirds line (fatigue trajectory) instead
    // of a flat average — the average hides exactly what the user
    // watches this metric for.
    if (s.key === "GroundContactBalanceLeft" && lrThirdsDetail) {
      return renderSpecs.length === 1
        ? `前⅓→后⅓: ${lrThirdsDetail}`
        : `${s.label}: ${lrThirdsDetail}`;
    }
    const sum = data.summary[s.key];
    if (!sum) return null;
    return renderSpecs.length === 1
      ? s.formatSubtitle(sum)
      : `${s.label}: ${s.formatSubtitle(sum)}`;
  }).filter((x): x is string => !!x);
  const subtitle = subtitleParts.join("  ·  ");

  return (
    <div className="space-y-2">
      <div className="-mx-1 flex gap-1 overflow-x-auto pb-1 [&::-webkit-scrollbar]:hidden [scrollbar-width:none]">
        {metrics.map((m) => {
          const idx = active.indexOf(m.key);
          const isActive = idx >= 0;
          // Tag the secondary tab with its axis side so the dual-axis
          // mapping is obvious without legend chrome.
          return (
            <button
              key={m.key}
              type="button"
              onClick={() => onTabClick(m.key)}
              className={
                "shrink-0 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors " +
                (isActive
                  ? "border-foreground bg-foreground text-background"
                  : "border-border bg-background text-muted-foreground hover:text-foreground")
              }
            >
              {m.label}
              {idx === 1 && (
                <span className="ml-1 opacity-70">▶</span>
              )}
            </button>
          );
        })}
      </div>
      <div className="flex items-center justify-between gap-2">
        {subtitle ? (
          <p className="min-w-0 truncate text-xs text-muted-foreground tabular-nums">
            {subtitle}
          </p>
        ) : (
          <span />
        )}
        {renderSpecs.some((s) => s.key === "RespirationRate") && (
          <div
            role="group"
            aria-label="Resp view"
            className="flex shrink-0 rounded-md border border-border bg-background p-0.5 text-[11px] font-medium"
          >
            {([false, true] as const).map((rel) => (
              <button
                key={String(rel)}
                type="button"
                onClick={() => setRespRelation(rel)}
                className={
                  "rounded px-2 py-0.5 transition-colors " +
                  (respRelation === rel
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground")
                }
                aria-pressed={respRelation === rel}
              >
                {rel ? "关系" : "曲线"}
              </button>
            ))}
          </div>
        )}
        {distanceAvailable && !respRelation && (
          <div
            role="group"
            aria-label="X-axis"
            className="flex shrink-0 rounded-md border border-border bg-background p-0.5 text-[11px] font-medium"
          >
            {(["time", "distance"] as const).map((m) => {
              const isActive = effectiveXMode === m;
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => setXMode(m)}
                  className={
                    "rounded px-2 py-0.5 transition-colors " +
                    (isActive
                      ? "bg-foreground text-background"
                      : "text-muted-foreground hover:text-foreground")
                  }
                  aria-pressed={isActive}
                >
                  {m === "time" ? "Time" : "Distance"}
                </button>
              );
            })}
          </div>
        )}
      </div>
      {respRelation && renderSpecs.some((s) => s.key === "RespirationRate") ? (
        <RespHrScatter activityId={activityId} />
      ) : (
        <>
          <ChartPane
            rows={rows}
            specs={renderSpecs}
            xMode={effectiveXMode}
            decorations={decorations}
          />
          {blocks.length > 0 && (
            // Which color is which effort — the washes/rail alone
            // don't identify adjacent tints of the warm scale.
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {[...new Set(blocks.map((b) => b.label))]
                .filter((l): l is string => l != null)
                .map((label) => (
                  <span key={label} className="inline-flex items-center gap-1">
                    <span
                      className="inline-block h-2 w-2 rounded-full"
                      style={{
                        background:
                          label === "Rest" ? REST_COLOR : effortColor(label),
                      }}
                    />
                    {EFFORT_SHORT[label] ?? label}
                  </span>
                ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
