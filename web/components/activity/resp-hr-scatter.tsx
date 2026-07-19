"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  Scatter,
  XAxis,
  YAxis,
} from "recharts";
import { ChartContainer, type ChartConfig } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet } from "@/lib/api";
import { effortColor, EFFORT_SHORT, REST_COLOR } from "@/lib/effort-colors";
import type { RespHrResponse } from "@/lib/types";

// Resp × HR relationship view (PR #114). One run's pairs as a scatter
// (dot color = the effort label active at that moment, same vocabulary
// as chips/bars), the server's hinge fit drawn over it, and a dashed
// marker at the knee — the run's apparent ventilatory threshold. The
// user has felt that resp doesn't track HR linearly; this is that
// feeling, measured.

export function RespHrScatter({ activityId }: { activityId: number }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["runs", activityId, "resp-hr"],
    queryFn: () => apiGet<RespHrResponse>(`/api/runs/${activityId}/resp-hr`),
    staleTime: Infinity,
    retry: false,
  });

  if (isLoading) return <Skeleton className="h-56 w-full" />;
  // Per-card invariant: error ≠ empty. 404 = no respiration data.
  if (isError) {
    return (
      <div className="flex h-44 items-center justify-center text-xs text-muted-foreground">
        本次没有呼吸数据（需要支持 Resp 的表/带）。
      </div>
    );
  }
  if (!data) return null;

  // Group points by label so each effort gets one Scatter series in
  // its shared color; unlabeled falls back to the neutral tone.
  const byCategory = new Map<string, typeof data.points>();
  for (const p of data.points) {
    const key = p.category ?? "__none__";
    const arr = byCategory.get(key);
    if (arr) arr.push(p);
    else byCategory.set(key, [p]);
  }

  const [hrLo, hrHi] = data.hr_range;
  const fit = data.fit;
  // The fit polyline is pure rendering of server params: two straight
  // segments meeting at the knee.
  const fitLine = fit
    ? [hrLo, fit.breakpoint_hr, hrHi]
        .filter((h) => h >= hrLo && h <= hrHi)
        .map((h) => ({
          hr: h,
          resp:
            fit.intercept +
            (fit.slope_low_per_10bpm / 10) * h +
            (h > fit.breakpoint_hr
              ? ((fit.slope_high_per_10bpm - fit.slope_low_per_10bpm) / 10) *
                (h - fit.breakpoint_hr)
              : 0),
        }))
    : [];

  const config: ChartConfig = {
    resp: { label: "Resp", color: "var(--chart-5)" },
  };

  return (
    <div className="space-y-2">
      <ChartContainer config={config} className="h-56 w-full">
        <ComposedChart>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis
            type="number"
            dataKey="hr"
            name="HR"
            domain={[hrLo - 3, hrHi + 3]}
            tickLine={false}
            axisLine={false}
            tickMargin={6}
            fontSize={10}
            tickFormatter={(v: number) => `${Math.round(v)}`}
          />
          <YAxis
            type="number"
            dataKey="resp"
            name="Resp"
            domain={["auto", "auto"]}
            tickLine={false}
            axisLine={false}
            tickMargin={6}
            fontSize={10}
            width={30}
            tickFormatter={(v: number) => `${Math.round(v)}`}
          />
          {fit && (
            <ReferenceLine
              x={fit.breakpoint_hr}
              stroke="var(--muted-foreground)"
              strokeDasharray="4 3"
              label={{
                value: `拐点 ≈${fit.breakpoint_hr}`,
                position: "insideTopLeft",
                fontSize: 11,
                fill: "var(--muted-foreground)",
              }}
            />
          )}
          {[...byCategory.entries()].map(([cat, pts]) => (
            <Scatter
              key={cat}
              data={pts}
              fill={
                cat === "__none__"
                  ? REST_COLOR
                  : cat === "Rest"
                    ? REST_COLOR
                    : effortColor(cat)
              }
              fillOpacity={0.75}
              isAnimationActive={false}
              shape="circle"
            />
          ))}
          {fitLine.length >= 2 && (
            <Line
              data={fitLine}
              dataKey="resp"
              stroke="var(--muted-foreground)"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          )}
        </ComposedChart>
      </ChartContainer>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {[...byCategory.keys()]
          .filter((c) => c !== "__none__")
          .map((cat) => (
            <span key={cat} className="inline-flex items-center gap-1">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{
                  background: cat === "Rest" ? REST_COLOR : effortColor(cat),
                }}
              />
              {EFFORT_SHORT[cat] ?? cat}
            </span>
          ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {fit ? fit.summary : data.no_fit_reason}
      </p>
    </div>
  );
}
