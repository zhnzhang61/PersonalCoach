"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  ErrorBar,
  Scatter,
  XAxis,
  YAxis,
} from "recharts";
import { ArrowRightLeft } from "lucide-react";
import { ChartContainer, type ChartConfig } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import { effortColor, EFFORT_SHORT } from "@/lib/effort-colors";
import type { RespHrCandle, RespHrProfileResponse } from "@/lib/types";

// Where did today's efforts sit against my own history?
//
// Two views of one lap population, flippable:
//   HR → resp   x = the user's own RPE zones, y = respiration spread
//   resp → HR   x = respiration bands,        y = HR spread
// Each band is a candle (p10–p90 whisker, p25–p75 box, median tick);
// this run's per-effort centroids overlay as dots in the effort colors
// used everywhere else.
//
// Bands the server marks `reliable: false` render muted: below HR ~149
// respiration is decoupled from HR (it reflects HR noise, not effort),
// and above ~174 the strap's RSA-derived respiration compresses toward
// a ceiling well under population fRmax. Drawing them at full strength
// would invite reading a difference the instrument can't resolve.

type Mode = "hrToResp" | "respToHr";

// Baseline window: the curve shifts with fitness, so comparing today
// against two-year-old laps understates the current position. 18
// months keeps enough samples in the thin high-HR bands.
function sinceParam(): string {
  const d = new Date();
  d.setMonth(d.getMonth() - 18);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function paceStr(minPerMi: number | null | undefined): string | null {
  if (minPerMi == null) return null;
  const m = Math.floor(minPerMi);
  const s = Math.round((minPerMi - m) * 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// Recharts draws a floating bar as [invisible base, visible body], so
// the box is [p25, p75-p25] and the whisker rides as a symmetric
// ErrorBar around the box centre.
function toRow(c: RespHrCandle) {
  const centre = (c.p25 + c.p75) / 2;
  return {
    key: c.key,
    base: c.p25,
    box: c.p75 - c.p25,
    median: c.median,
    whisker: [centre - c.p10, c.p90 - centre] as [number, number],
    reliable: c.reliable,
    nLaps: c.n_laps,
    band: c.band,
    pace: paceStr(c.median_pace_min_mi),
  };
}

const CHART_CONFIG: ChartConfig = {
  box: { label: "25–75%", color: "#2a78d6" },
};

// A short rule across the box, not Recharts' default cross glyph — at
// candle width the cross reads as a marker competing with the run's
// own dots rather than as "the middle of this distribution".
function MedianTick(props: { cx?: number; cy?: number }) {
  const { cx, cy } = props;
  if (cx == null || cy == null) return null;
  return (
    <line
      x1={cx - 13}
      x2={cx + 13}
      y1={cy}
      y2={cy}
      stroke="currentColor"
      strokeWidth={2}
    />
  );
}

export function RespHrCandles({ activityId }: { activityId: number }) {
  const [mode, setMode] = useState<Mode>("hrToResp");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["runs", activityId, "resp-hr-profile"],
    queryFn: () =>
      apiGet<RespHrProfileResponse>(
        `/api/runs/${activityId}/resp-hr-profile?since=${sinceParam()}`,
      ),
    staleTime: Infinity,
    retry: false,
  });

  const bands = useMemo(
    () =>
      !data
        ? []
        : mode === "hrToResp"
          ? data.baseline.hr_to_resp
          : data.baseline.resp_to_hr,
    [data, mode],
  );

  // This run's efforts, bucketed into the band that contains each one
  // — by mean HR in HR→resp, by mean respiration when flipped.
  //
  // They ride ON the band rows (as today0, today1, … with a Scatter
  // per slot) rather than as a Scatter with its own `data` prop:
  // handing a child its own data in a ComposedChart makes Recharts
  // rebuild the categorical x domain from that array, which collapses
  // the candles down to only the bands the run happened to touch.
  // Slots keep two efforts in one band as two dots at their own
  // heights — that spread is the point — without averaging them away.
  const { rows, slots } = useMemo(() => {
    const byBand = new Map<string, { v: number; category: string }[]>();
    for (const p of data?.run_points ?? []) {
      const v = mode === "hrToResp" ? p.avg_hr : p.avg_resp;
      const band = bands.find((b) =>
        mode === "hrToResp"
          ? v >= (b.hr_low ?? -Infinity) && v <= (b.hr_high ?? Infinity)
          : v >= (b.resp_low ?? -Infinity) && v < (b.resp_high ?? Infinity),
      );
      if (!band) continue;
      const list = byBand.get(band.key) ?? [];
      list.push({
        v: mode === "hrToResp" ? p.avg_resp : p.avg_hr,
        category: p.category,
      });
      byBand.set(band.key, list);
    }
    const width = Math.max(0, ...[...byBand.values()].map((l) => l.length));
    return {
      slots: Array.from({ length: width }, (_, i) => i),
      rows: bands.map((b) => {
        const row = toRow(b);
        const hits = byBand.get(b.key) ?? [];
        const extra: Record<string, number | string | null> = {};
        for (let i = 0; i < width; i++) {
          extra[`today${i}`] = hits[i]?.v ?? null;
          extra[`cat${i}`] = hits[i]?.category ?? null;
        }
        return { ...row, ...extra };
      }),
    };
  }, [data, bands, mode]);

  // Recharts derives the domain from every series, and the invisible
  // `base` bar of a stacked float anchors at zero — left alone that
  // drags the axis down to 0 and squashes the candles into the top
  // third. Pin it to the actual whisker range instead.
  const domain = useMemo<[number, number]>(() => {
    const vals: number[] = bands.flatMap((b) => [b.p10, b.p90]);
    for (const r of rows) {
      for (const i of slots) {
        const v = r[`today${i}` as keyof typeof r];
        if (typeof v === "number") vals.push(v);
      }
    }
    if (vals.length === 0) return [0, 1];
    return [Math.floor(Math.min(...vals) - 2), Math.ceil(Math.max(...vals) + 2)];
  }, [bands, rows, slots]);

  if (isLoading) return <Skeleton className="h-64 w-full" />;
  // Per-card invariant: error state is distinct from empty state.
  if (isError) {
    return (
      <div className="flex h-44 items-center justify-center px-4 text-center text-xs text-rose-600 dark:text-rose-400">
        分布加载失败 — {(error as Error | null)?.message ?? "请重试。"}
      </div>
    );
  }
  if (!data || rows.length === 0) {
    return (
      <div className="flex h-44 items-center justify-center text-xs text-muted-foreground">
        呼吸数据不足，画不出分布。
      </div>
    );
  }

  const yLabel = mode === "hrToResp" ? "呼吸 (次/分)" : "心率 (bpm)";

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {mode === "hrToResp" ? "心率区间 → 呼吸" : "呼吸区间 → 心率"}
          <span className="ml-1.5 opacity-70">
            {yLabel} · 基线 {data.baseline.n_laps} 圈
          </span>
        </p>
        <button
          type="button"
          onClick={() =>
            setMode((m) => (m === "hrToResp" ? "respToHr" : "hrToResp"))
          }
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-background px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted/40"
        >
          <ArrowRightLeft className="size-3" aria-hidden />
          翻转
        </button>
      </div>

      <ChartContainer config={CHART_CONFIG} className="h-64 w-full">
        <ComposedChart
          data={rows}
          margin={{ top: 10, right: 8, bottom: 0, left: -4 }}
        >
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis
            dataKey="key"
            tickLine={false}
            axisLine={false}
            tickFormatter={(k: string) => EFFORT_SHORT[k] ?? k}
            tick={{ fontSize: 10 }}
            interval={0}
          />
          <YAxis
            domain={domain}
            allowDataOverflow
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 10 }}
            width={34}
          />
          <Bar dataKey="base" stackId="c" fill="transparent" isAnimationActive={false} />
          <Bar dataKey="box" stackId="c" barSize={26} radius={2} isAnimationActive={false}>
            {rows.map((r) => (
              <Cell key={r.key} fill="#2a78d6" fillOpacity={r.reliable ? 0.8 : 0.26} />
            ))}
            <ErrorBar
              dataKey="whisker"
              width={6}
              strokeWidth={1.5}
              stroke="#85B7EB"
              direction="y"
            />
          </Bar>
          <Scatter dataKey="median" shape={MedianTick} isAnimationActive={false} />
          {slots.map((i) => (
            <Scatter key={i} dataKey={`today${i}`} isAnimationActive={false}>
              {rows.map((r) => (
                <Cell
                  key={`t${i}-${r.key}`}
                  fill={effortColor(r[`cat${i}` as keyof typeof r] as string | null)}
                />
              ))}
            </Scatter>
          ))}
        </ComposedChart>
      </ChartContainer>

      <p className="text-[10px] leading-relaxed text-muted-foreground">
        柱 = 25–75%，须 = 10–90%，十字 = 中位。淡色柱表示该区间读数不可靠
        （心率低于 149 呼吸与心率脱钩，高于 174 心率带的呼吸估算压缩失真）。
        彩色点 = 本次跑该档的均值。
      </p>
    </div>
  );
}
